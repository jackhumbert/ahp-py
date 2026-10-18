"""Credential masking over a real in-memory host: nothing a worker prints is published or stored."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from tests.flow_fakes import (
    AWS,
    BEARER,
    CONFIRM_COMMAND,
    GH,
    INPUT_QUESTION,
    KV,
    PASSWORD,
    SECRETS,
    USER,
    AskingProvider,
    SecretProvider,
    StructuredSecretProvider,
    reset_fakes,
)
from tests.flow_hosting import SESSION, FlowFixture, boot_flow, final_json
from tests.hosting import (
    all_channels,
    found,
    leaks,
    notifications,
    run_turn,
    start_turn,
    state,
    text_of,
    wait_turn,
    worker_chats,
)

pytestmark = pytest.mark.node

TOML = """
[providers.echo]
load = "tests.flow_fakes:StrippedEchoProvider"

[providers.sec]
load = "tests.flow_fakes:SecretProvider"

[providers.ss]
load = "tests.flow_fakes:StructuredSecretProvider"

[providers.sf]
load = "tests.flow_fakes:StructuredSecretProvider"
config = { capable = false }

[providers.es]
load = "tests.flow_fakes:StrippedEchoProvider"
config = { structured = true }

[providers.ask]
load = "tests.flow_fakes:AskingProvider"

[routing]
default = "echo"
rules = [
  { provider = "sec", label = "sec*" },
  { provider = "ss", label = "ss*" },
  { provider = "sf", label = "sf*" },
  { provider = "es", label = "es*" },
  { provider = "ask", label = "ask*" },
]
"""

REDACT = """
[redact]
patterns = ["sekrit-[0-9]+"]
"""

SHOW = """```js
const r = await agent('show', {label: 'sec-1'});
log('AKIA' + 'TESTONLY'.repeat(2));
const f = await agent('fail', {label: 'sec-2'});
return {r, f}
```"""

STRUCT_SCHEMA = (
    "{type: 'object', properties: {k: {type: 'string'}, password: {type: 'string'}},"
    " required: ['k']}"
)
RAW_SECRETS = (*SECRETS, BEARER, KV, PASSWORD)


@pytest.fixture(autouse=True)
def _fresh_fakes() -> None:
    reset_fakes()


def journal_text(tmp_path: Path) -> str:
    (journal,) = (tmp_path / "state" / "runs").glob("*/journal.jsonl")
    return journal.read_text(encoding="utf-8")


def assert_nothing_raw(fx: FlowFixture, tmp_path: Path, *values: object) -> None:
    """No secret in any channel the host knows, in the journal, or in the values given."""
    assert len(all_channels(fx.host)) >= 3  # root, the session, the main chat, a worker
    assert leaks(fx.host, *RAW_SECRETS) == []
    stored = journal_text(tmp_path)
    for secret in RAW_SECRETS:
        assert secret not in stored
        for value in values:
            assert secret not in json.dumps(value)


async def boot(tmp_path: Path, node_path: str, redact: str = REDACT) -> FlowFixture:
    return await boot_flow(tmp_path, node_path, TOML + redact)


async def test_masking_secret_never_published_or_stored(tmp_path: Path, node_path: str) -> None:
    fx = await boot(tmp_path, node_path)
    try:
        turn = await run_turn(fx.wire, fx.chat, "t1", SHOW)
        assert turn["state"] == "complete"
        result = final_json(turn)
        assert "[redacted]" in result["r"]
        assert result["f"] is None
        workers = worker_chats(fx.host, SESSION)
        assert len(workers) == 2
        assert leaks(fx.host, *SECRETS) == []
        stored = journal_text(tmp_path)
        for secret in SECRETS:
            assert secret not in stored
            assert secret not in json.dumps(result)
            assert secret not in text_of(turn)
        assert any("[redacted]" in text_of(state(fx.host, str(w["resource"]))) for w in workers)
        assert any(n.startswith("agent() #1 failed: boom [redacted]") for n in notifications(turn))
    finally:
        await fx.shut()


async def test_masking_inner_agent_sees_the_original(tmp_path: Path, node_path: str) -> None:
    SecretProvider.instances.clear()
    fx = await boot(tmp_path, node_path)
    try:
        prompt = "'use ' + 'AKIA' + 'TESTONLY'.repeat(2)"
        script = f"```js\nreturn await agent({prompt}, {{label: 'sec-3'}})\n```"
        turn = await run_turn(fx.wire, fx.chat, "t1", script)
        assert turn["state"] == "complete"
        sessions = [s for p in SecretProvider.instances for s in p.sessions]
        assert [m.text for s in sessions for m in s.messages] == ["use " + AWS]
    finally:
        await fx.shut()


async def test_masking_builtin_off(tmp_path: Path, node_path: str) -> None:
    fx = await boot(tmp_path, node_path, "[redact]\nbuiltin = false\n")
    try:
        turn = await run_turn(fx.wire, fx.chat, "t1", SHOW)
        assert turn["state"] == "complete"
        assert AWS in json.dumps(final_json(turn))
        assert GH in json.dumps(final_json(turn))
        assert USER in json.dumps(final_json(turn))
    finally:
        await fx.shut()


async def run_structured(fx: FlowFixture, label: str, prompt_js: str) -> dict[str, Any]:
    call = f"agent({prompt_js}, {{label: '{label}', schema: {STRUCT_SCHEMA}}})"
    script = f"```js\nreturn await {call}\n```"
    turn = await run_turn(fx.wire, fx.chat, "t1", script)
    assert turn["state"] == "complete"
    return turn


async def test_masking_structured_tool_call_never_published(tmp_path: Path, node_path: str) -> None:
    fx = await boot(tmp_path, node_path)
    try:
        turn = await run_structured(fx, "ss-1", "'go'")
        result = final_json(turn)
        assert result == {"k": "x [redacted]", "password": "[redacted]"}
        assert_nothing_raw(fx, tmp_path, result, text_of(turn))
        # The StructuredOutput call really was published (masked), not skipped.
        worker = str(worker_chats(fx.host, SESSION)[0]["resource"])
        assert "StructuredOutput" in all_channels(fx.host)[worker]
        (session,) = StructuredSecretProvider.instances[0].sessions
        (answer,) = session.tool_results
        # The agent's own view of the accepted call is untouched.
        assert AWS in json.dumps(answer.value)
        assert PASSWORD in json.dumps(answer.value)
    finally:
        await fx.shut()


async def test_masking_structured_fallback_reply_never_published(
    tmp_path: Path, node_path: str
) -> None:
    fx = await boot(tmp_path, node_path)
    try:
        turn = await run_structured(fx, "sf-1", "'go'")
        result = final_json(turn)
        assert result == {"k": "x [redacted]", "password": "[redacted]"}
        assert_nothing_raw(fx, tmp_path, result, text_of(turn))
    finally:
        await fx.shut()


async def test_masking_echo_structured_never_published(tmp_path: Path, node_path: str) -> None:
    # The prompt spells the key with a JSON escape, so only the parsed result is a credential.
    prompt = '{"k": "x AKIA\\u0054ESTONLY' + 'TESTONLY"}'
    assert AWS not in prompt
    fx = await boot(tmp_path, node_path)
    try:
        turn = await run_structured(fx, "es-1", json.dumps(prompt))
        assert final_json(turn) == {"k": "x [redacted]"}
        assert_nothing_raw(fx, tmp_path, text_of(turn))
    finally:
        await fx.shut()


async def test_masking_confirmation_and_input_prompts_never_published(
    tmp_path: Path, node_path: str
) -> None:
    fx = await boot(tmp_path, node_path)
    try:
        script = "```js\nreturn await agent('ask', {label: 'ask-1'})\n```"
        await start_turn(fx.wire, fx.chat, "t1", script)
        assert await fx.wire.until(lambda: len(worker_chats(fx.host, SESSION)) == 1)
        worker = str(worker_chats(fx.host, SESSION)[0]["resource"])
        await fx.wire.request("subscribe", {"channel": worker})

        def pending() -> list[Any]:
            active = state(fx.host, worker).get("activeTurn") or {}
            return list(found(active, lambda m: m.get("status") == "pending-confirmation"))

        assert await fx.wire.until(lambda: bool(pending()))
        assert leaks(fx.host, *RAW_SECRETS) == []
        assert "[redacted]" in json.dumps(pending())
        await fx.wire.dispatch(
            worker,
            {
                "type": "chat/toolCallConfirmed",
                "turnId": state(fx.host, worker)["activeTurn"]["id"],
                "toolCallId": pending()[0]["toolCallId"],
                "approved": True,
                "confirmed": "user-action",
            },
        )

        # The request is published on the worker chat and mirrored to the session's inputNeeded.
        def asked() -> list[Any]:
            needed = state(fx.host, SESSION).get("inputNeeded") or []
            return [n for n in needed if n.get("kind") == "chatInput"]

        assert await fx.wire.until(lambda: bool(asked()))
        assert leaks(fx.host, *RAW_SECRETS) == []
        assert "[redacted]" in json.dumps(asked())
        await fx.wire.dispatch(
            worker,
            {
                "type": "chat/inputCompleted",
                "requestId": asked()[0]["id"],
                "response": "accept",
                "answers": {"q": {"state": "submitted", "value": "yes"}},
            },
        )
        turn = await wait_turn(fx.wire, fx.chat, "t1")
        assert turn["state"] == "complete"
        assert final_json(turn) == "approved=True answer=accept"
        assert_nothing_raw(fx, tmp_path, text_of(turn))
        (session,) = AskingProvider.instances[0].sessions
        # The agent's own request objects are untouched, and the answers came back unchanged.
        assert session.confirmation is not None
        assert CONFIRM_COMMAND in session.confirmation.invocation_message
        assert session.confirmation.tool_input == {"command": CONFIRM_COMMAND}
        assert session.request is not None
        assert session.request.questions[0].message == INPUT_QUESTION
        assert session.outcome is not None
        assert session.outcome.approved
        assert session.answer is not None
        assert session.answer.answers == {"q": {"state": "submitted", "value": "yes"}}
    finally:
        await fx.shut()
