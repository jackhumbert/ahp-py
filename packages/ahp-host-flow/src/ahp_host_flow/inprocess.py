"""Run one script through `FlowProvider` on an in-process host (`flow run --config`).

Nothing answers a confirmation or input an inner session asks for in a worker
chat, so the run fails at the first one (it is never auto-approved): use a
non-asking `approval_mode` in flow.toml.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable, Mapping
from pathlib import Path
from typing import Any

import ahp_client
from ahp_client import ResponsePartAdded, TurnCancelled, TurnCompleted, TurnFailed
from ahp_host.core import Host, LoopbackSingleUserPolicy
from ahp_protocol.transport import memory_pair

from ahp_host_flow.config import FlowConfig
from ahp_host_flow.provider import FlowProvider
from ahp_host_flow.sandbox import NO_ARGS

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


async def _unanswerable(host: Host, session_uri: str) -> str:
    """Wait for the first confirmation or input an inner session asks for; say what it is.

    This host has no client to answer, so the run would wait for it forever.
    """
    while True:
        state = host.sequencer.state_of(session_uri)
        needed = state.get("inputNeeded") if isinstance(state, Mapping) else None
        if isinstance(needed, list) and needed and isinstance(needed[0], Mapping):
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
) -> Any:
    """Run *source* as a fenced script in a session rooted at *root*; return its value."""
    host = Host(FlowProvider(config), LoopbackSingleUserPolicy())
    client_t, server_t = memory_pair()
    serving = asyncio.create_task(host.serve(server_t))
    try:
        async with (
            ahp_client.connect(transport=client_t) as client,
            await client.create_session(
                provider="flow", working_directories=[root.resolve().as_uri()]
            ) as session,
        ):
            text = "```js\n" + source + "\n```"
            if args is not NO_ARGS:
                text += "\n" + json.dumps(args)
            consume = asyncio.create_task(_consume(session.prompt(text), on_note))
            watcher = asyncio.create_task(_unanswerable(host, session.uri))
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
