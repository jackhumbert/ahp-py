"""Run one script through `FlowProvider` on an in-process host (`flow run --config`).

Nothing answers a confirmation or input an inner session asks for in a worker
chat, so the run fails at the first one (it is never auto-approved): use a
non-asking `approval_mode` in flow.toml. The same goes for the workflow's own
`ask()` and `approve()` gates on the main chat.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import tempfile
from collections.abc import AsyncIterator, Callable, Mapping
from pathlib import Path
from typing import Any

import ahp_client
from ahp_client import ResponsePartAdded, TurnCancelled, TurnCompleted, TurnFailed
from ahp_host.core import Host, LoopbackSingleUserPolicy, Policy
from ahp_host.node import Roots
from ahp_protocol.transport import memory_pair

from ahp_host_flow.config import FlowConfig
from ahp_host_flow.engine import WORKFLOW_NAME
from ahp_host_flow.provider import FlowProvider
from ahp_host_flow.sandbox import NO_ARGS, language_of

_JSON_OPEN = "```json\n"
_FENCE_CLOSE = "\n```"


class FlowRunFailed(Exception):  # noqa: N818 -- the name the CLI and docs use
    """The run failed, was cancelled, or returned nothing."""


def _value(text: str) -> Any:
    start = text.rfind(_JSON_OPEN)
    if start >= 0:
        body = text[start + len(_JSON_OPEN) :]
        end = body.find(_FENCE_CLOSE)
        if end >= 0:
            try:
                return json.loads(body[:end])
            except json.JSONDecodeError as exc:
                raise FlowRunFailed(f"the run's result is not valid JSON: {exc}") from None
    raise FlowRunFailed("the run returned no result")


async def _consume(events: AsyncIterator[Any], on_note: Callable[[str], None] | None) -> str:
    """The run's final text; raises `FlowRunFailed` if the turn failed or was cancelled."""
    final = ""
    async for event in events:
        if isinstance(event, ResponsePartAdded):
            part = event.part
            if part.get("kind") == "systemNotification" and on_note is not None:
                on_note(str(part.get("content", "")))
        elif isinstance(event, TurnCompleted):
            final = event.text
        elif isinstance(event, TurnFailed):
            raise FlowRunFailed(event.reason)
        elif isinstance(event, TurnCancelled):
            raise FlowRunFailed("run cancelled")
    return final


def _describe(request: Mapping[str, Any]) -> str:
    tool = request.get("toolCall")
    tool = tool if isinstance(tool, Mapping) else {}
    name = tool.get("displayName") or tool.get("toolName") or request.get("kind") or "a request"
    what = tool.get("invocationMessage")
    return f"{name} ({what})" if what else str(name)


def _gate_description(entry: Mapping[str, Any]) -> str:
    request = entry.get("request")
    request = request if isinstance(request, Mapping) else {}
    tool = entry.get("toolCall")
    tool = tool if isinstance(tool, Mapping) else {}
    questions = request.get("questions")
    first = questions[0] if isinstance(questions, list) and questions else None
    for text in (
        request.get("message"),
        tool.get("invocationMessage"),
        first.get("message") if isinstance(first, Mapping) else None,
    ):
        if isinstance(text, str) and text:
            return text
    return "a question"


async def _unanswerable(host: Host, session_uri: str, main_chat: str) -> str:
    """Wait for the first confirmation or input a session asks for; say what it is.

    This host has no client to answer, so the run would wait for it forever. A
    gate on the run's main chat is the workflow's own `ask()`/`approve()`; any
    other entry is an inner session's.
    """
    while True:
        state = host.sequencer.state_of(session_uri)
        needed = state.get("inputNeeded") if isinstance(state, Mapping) else None
        if isinstance(needed, list) and needed and isinstance(needed[0], Mapping):
            entry = needed[0]
            if entry.get("chat") == main_chat:
                verb = "approve()" if "toolCall" in entry else "ask()"
                return (
                    f"the workflow's {verb} gate needs an answer: {_gate_description(entry)}. "
                    "Nothing answers it with --config (no client is connected); run the "
                    "workflow on a host a client can reach to answer it"
                )
            return (
                f"an agent() call asked for confirmation or input: {_describe(needed[0])}. "
                "Nothing answers it with --config; set approval_mode in flow.toml "
                '(for example "acceptEdits" or "auto") on the provider that serves the call.'
            )
        await asyncio.sleep(0.05)


async def run_in_process(
    config: FlowConfig,
    source: str,
    *,
    args: object = NO_ARGS,
    root: Path,
    on_note: Callable[[str], None] | None = None,
    filename: str = "script.js",
    policy: Policy | None = None,
) -> Any:
    """Run *source* as a script in a session rooted at *root*; return its value.

    JavaScript goes as a fenced message. A `.py` script is served from a temporary
    host directory and started as `/host:<name>`: whoever runs `flow run` is the
    operator, so the script is a trusted host workflow.
    """
    if language_of(filename) == "py":
        stem = Path(filename).stem
        name = stem if WORKFLOW_NAME.fullmatch(stem) else "script"
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / f"{name}.py").write_text(source, encoding="utf-8")
            workflows = config.workflows
            workflows = dataclasses.replace(
                workflows,
                dirs=(Path(tmp), *workflows.dirs),
                sources=workflows.sources
                if "host" in workflows.sources
                else (*workflows.sources, "host"),
                python_sources=workflows.python_sources
                if "host" in workflows.python_sources
                else (*workflows.python_sources, "host"),
            )
            text = f"/host:{name}"
            if args is not NO_ARGS:
                text += " " + json.dumps(args)
            return await _run_message(
                dataclasses.replace(config, workflows=workflows), text, root, on_note, policy
            )
    text = "```js\n" + source + "\n```"
    if args is not NO_ARGS:
        text += "\n" + json.dumps(args)
    return await _run_message(config, text, root, on_note, policy)


async def _run_message(
    config: FlowConfig,
    text: str,
    root: Path,
    on_note: Callable[[str], None] | None,
    policy: Policy | None = None,
) -> Any:
    provider = FlowProvider(config, roots=Roots.single(root.resolve()))
    host = Host(provider, policy if policy is not None else LoopbackSingleUserPolicy())
    client_t, server_t = memory_pair()
    serving = asyncio.create_task(host.serve(server_t))
    try:
        async with (
            ahp_client.connect(transport=client_t) as client,
            await client.create_session(
                provider="flow", working_directories=[root.resolve().as_uri()]
            ) as session,
        ):
            created = host.sequencer.state_of(session.uri)
            main_chat = created.get("defaultChat") if isinstance(created, Mapping) else None
            consume = asyncio.create_task(_consume(session.prompt(text), on_note))
            watcher = asyncio.create_task(_unanswerable(host, session.uri, str(main_chat)))
            try:
                await asyncio.wait({consume, watcher}, return_when=asyncio.FIRST_COMPLETED)
                if not consume.done():
                    # Nobody is here to answer, so the run would hang forever.
                    raise FlowRunFailed(watcher.result())
                return _value(consume.result())
            finally:
                for task in (consume, watcher):
                    task.cancel()
                await asyncio.gather(consume, watcher, return_exceptions=True)
    finally:
        serving.cancel()
        await host.aclose()
