from __future__ import annotations

import asyncio
import logging
from typing import Any

import pytest

from ahp_acp import (
    AcpClient,
    AgentMessageChunk,
    CompactionUpdate,
    NoticeUpdate,
    PlanUpdate,
    ProtocolVersionError,
    ToolCall,
    ToolCallUpdate,
    UnknownUpdate,
    UnsupportedError,
    allow_once,
    mcp_server_http,
    mcp_server_stdio,
    reject_once,
)
from tests.conftest import MakeClient
from tests.support import Recorder

TEXT = {"type": "text", "text": "hi"}


def update_msg(update: dict[str, Any], session: str = "s1") -> dict[str, Any]:
    params = {"sessionId": session, "update": update}
    return {"send": {"method": "session/update", "params": params}}


async def test_initialize_stores_what_the_agent_says(make_client: MakeClient) -> None:
    init = {
        "protocolVersion": 1,
        "agentCapabilities": {"loadSession": True},
        "agentInfo": {"name": "fake"},
        "authMethods": [{"id": "key", "name": "Key"}],
        "echo-me": 1,
    }
    async with make_client({"responses": {"initialize": init}, "echo": ["initialize"]}) as client:
        result = await client.initialize()
        assert client.agent_capabilities == {"loadSession": True}
        assert client.agent_info == {"name": "fake"}
        assert client.auth_methods == [{"id": "key", "name": "Key"}]
        assert client.protocol_version == 1
        assert result["echo"]["protocolVersion"] == 1
        assert result["echo"]["clientInfo"] == {"name": "test-client", "version": "0"}
        assert result["echo"]["clientCapabilities"] == {
            "fs": {"readTextFile": False, "writeTextFile": False},
            "terminal": False,
        }


async def test_version_mismatch_warns(
    make_client: MakeClient, caplog: pytest.LogCaptureFixture
) -> None:
    script = {"responses": {"initialize": {"protocolVersion": 2}}}
    with caplog.at_level(logging.WARNING, logger="ahp_acp.client"):
        async with make_client(script) as client:
            await client.initialize()
            assert client.protocol_version == 2
    assert any("agent chose 2" in record.message for record in caplog.records)


async def test_version_mismatch_strict(make_client: MakeClient) -> None:
    script = {"responses": {"initialize": {"protocolVersion": 2}}}
    async with make_client(script, strict_version=True) as client:
        with pytest.raises(ProtocolVersionError):
            await client.initialize()


async def test_new_session_and_prompt_round_trip_meta(make_client: MakeClient) -> None:
    script = {
        "responses": {
            "session/new": {"sessionId": "s1"},
            "session/prompt": {"stopReason": "end_turn", "_meta": {"usage": 3}},
        },
        "echo": ["session/new", "session/prompt"],
    }
    async with make_client(script) as client:
        await client.initialize()
        new = await client.new_session(
            "/work",
            [mcp_server_stdio("tool", "run")],
            meta={"trace": "t1"},
            _example_flag=True,
        )
        assert new["sessionId"] == "s1"
        assert new["echo"] == {
            "cwd": "/work",
            "mcpServers": [{"name": "tool", "command": "run", "args": [], "env": []}],
            "_meta": {"trace": "t1"},
            "_example_flag": True,
        }
        result = await client.prompt("s1", [TEXT], meta={"turn": 1})
        assert result["stopReason"] == "end_turn"
        assert result["_meta"] == {"usage": 3}
        assert result["echo"] == {"sessionId": "s1", "prompt": [TEXT], "_meta": {"turn": 1}}


async def test_every_typed_update_and_an_unknown_one(
    make_client: MakeClient, recorder: Recorder
) -> None:
    updates: list[dict[str, Any]] = [
        {"sessionUpdate": "agent_message_chunk", "content": TEXT},
        {"sessionUpdate": "tool_call", "toolCallId": "t", "title": "T"},
        {"sessionUpdate": "tool_call_update", "toolCallId": "t", "status": "completed"},
        {"sessionUpdate": "plan", "entries": []},
        {"sessionUpdate": "notice", "severity": "info", "title": "n"},
        {"sessionUpdate": "compaction_update", "compactionId": "c", "status": "completed"},
        {"sessionUpdate": "_example/future_kind", "payload": 1},
    ]
    script = {
        "before": {"session/prompt": [update_msg(u) for u in updates]},
        "responses": {"session/prompt": {"stopReason": "end_turn"}},
    }
    async with make_client(script) as client:
        await client.initialize()
        await client.prompt("s1", [TEXT])
    # Every update was delivered, in order, before the prompt's response.
    assert [type(u) for u in recorder.updates] == [
        AgentMessageChunk,
        ToolCall,
        ToolCallUpdate,
        PlanUpdate,
        NoticeUpdate,
        CompactionUpdate,
        UnknownUpdate,
    ]
    assert recorder.updates[-1].raw["payload"] == 1


async def test_capability_gating(make_client: MakeClient) -> None:
    script = {
        "responses": {
            "initialize": {
                "protocolVersion": 1,
                "agentCapabilities": {"sessionCapabilities": {"resume": {}, "close": None}},
            },
            "session/resume": {"resumed": True},
        }
    }
    async with make_client(script) as client:
        # Before initialize nothing is advertised.
        with pytest.raises(UnsupportedError):
            await client.resume_session("s1", "/work")
        await client.initialize()
        assert (await client.resume_session("s1", "/work"))["resumed"] is True
        for call in (
            client.load_session("s1", "/work"),
            client.fork_session("s1", "/work"),
            client.close_session("s1"),
            client.list_sessions(),
        ):
            with pytest.raises(UnsupportedError):
                await call


async def test_advertised_methods_are_sent(make_client: MakeClient) -> None:
    script = {
        "responses": {
            "initialize": {
                "protocolVersion": 1,
                "agentCapabilities": {
                    "loadSession": True,
                    "sessionCapabilities": {"fork": {}, "close": {}, "list": {}},
                },
            }
        },
        "echo": [
            "session/load",
            "session/fork",
            "session/close",
            "session/list",
            "session/set_mode",
            "session/set_config_option",
        ],
    }
    async with make_client(script) as client:
        await client.initialize()
        assert (await client.load_session("s1", "/w"))["echo"] == {
            "sessionId": "s1",
            "cwd": "/w",
            "mcpServers": [],
        }
        assert (await client.fork_session("s1", "/w"))["echo"]["sessionId"] == "s1"
        assert (await client.close_session("s1"))["echo"] == {"sessionId": "s1"}
        assert (await client.list_sessions("/w", "next"))["echo"] == {"cwd": "/w", "cursor": "next"}
        assert (await client.set_mode("s1", "code"))["echo"] == {
            "sessionId": "s1",
            "modeId": "code",
        }
        assert (await client.set_config_option("s1", "model", "fast"))["echo"] == {
            "sessionId": "s1",
            "configId": "model",
            "value": "fast",
        }
        assert (await client.set_config_option("s1", "verbose", True))["echo"]["type"] == "boolean"


def _ask_script(**extra: Any) -> dict[str, Any]:
    steps = [
        {"send": {"id": "r1", "method": "_example/ask", "params": {"q": 1}}},
        {"await": ["r1"]},
    ]
    return {
        "before": {"session/prompt": steps},
        "responses": {"session/prompt": {"$replies": True}},
        **extra,
    }


async def test_extension_request_goes_to_on_request(make_client: MakeClient) -> None:
    seen: list[tuple[str, Any]] = []

    async def on_request(method: str, params: Any) -> Any:
        seen.append((method, params))
        return {"answer": 2}

    async with make_client(_ask_script(), on_request=on_request) as client:
        await client.initialize()
        result = await client.prompt("s1", [TEXT])
    assert seen == [("_example/ask", {"q": 1})]
    assert result["replies"]["r1"] == {"answer": 2}


async def test_extension_request_without_a_handler_is_method_not_found(
    make_client: MakeClient,
) -> None:
    async with make_client(_ask_script()) as client:
        await client.initialize()
        result = await client.prompt("s1", [TEXT])
    assert result["replies"]["r1"]["code"] == -32601


async def test_unknown_notification_goes_to_on_notification_or_is_ignored(
    make_client: MakeClient,
) -> None:
    script = {
        "before": {"session/prompt": [{"send": {"method": "_example/note", "params": {"n": 1}}}]},
        "responses": {"session/prompt": {}},
    }
    seen: list[tuple[str, Any]] = []

    async def on_notification(method: str, params: Any) -> None:
        seen.append((method, params))

    async with make_client(script, on_notification=on_notification) as client:
        await client.initialize()
        await client.prompt("s1", [TEXT])
    assert seen == [("_example/note", {"n": 1})]
    async with make_client(script) as client:
        await client.initialize()
        await client.prompt("s1", [TEXT])


def _permission_steps(session: str = "s1") -> list[dict[str, Any]]:
    request = {
        "id": "p1",
        "method": "session/request_permission",
        "params": {
            "sessionId": session,
            "toolCall": {"toolCallId": "t"},
            "options": [{"optionId": "allow", "name": "Allow", "kind": "allow_once"}],
        },
    }
    return [{"send": request}, {"await": ["p1"]}]


async def test_permission_flow(make_client: MakeClient, recorder: Recorder) -> None:
    script = {
        "before": {"session/prompt": _permission_steps()},
        "responses": {"session/prompt": {"$replies": True}},
    }
    async with make_client(script) as client:
        await client.initialize()
        result = await client.prompt("s1", [TEXT])
    assert recorder.permissions[0]["options"][0]["optionId"] == "allow"
    assert result["replies"]["p1"] == {"outcome": {"outcome": "selected", "optionId": "allow"}}


async def test_cancel_settles_a_pending_permission_as_cancelled(make_client: MakeClient) -> None:
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def on_permission(params: dict[str, Any]) -> dict[str, Any]:
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise
        raise AssertionError("unreachable")

    script = {
        "before": {"session/prompt": _permission_steps()},
        "responses": {"session/prompt": {"stopReason": "cancelled", "$replies": True}},
    }
    async with make_client(script, on_permission=on_permission) as client:
        await client.initialize()
        turn = asyncio.create_task(client.prompt("s1", [TEXT]))
        await asyncio.wait_for(started.wait(), 5)
        await client.cancel("s1")
        result = await asyncio.wait_for(turn, 5)
    assert cancelled.is_set()
    assert result["stopReason"] == "cancelled"
    assert result["replies"]["p1"] == {"outcome": {"outcome": "cancelled"}}


async def test_settle_permissions_answers_cancelled_without_a_cancel_notification(
    make_client: MakeClient,
) -> None:
    started = asyncio.Event()

    async def on_permission(params: dict[str, Any]) -> dict[str, Any]:
        started.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    script = {
        "before": {"session/prompt": _permission_steps()},
        "responses": {"session/prompt": {"stopReason": "end_turn", "$replies": True}},
    }
    async with make_client(script, on_permission=on_permission) as client:
        await client.initialize()
        turn = asyncio.create_task(client.prompt("s1", [TEXT]))
        await asyncio.wait_for(started.wait(), 5)
        client.settle_permissions("s1")
        result = await asyncio.wait_for(turn, 5)
    assert result["replies"]["p1"] == {"outcome": {"outcome": "cancelled"}}


async def test_cancel_leaves_other_sessions_permissions_alone(make_client: MakeClient) -> None:
    release = asyncio.Event()
    started = asyncio.Event()

    async def on_permission(params: dict[str, Any]) -> dict[str, Any]:
        started.set()
        await release.wait()
        return {"outcome": "selected", "optionId": "allow"}

    script = {
        "before": {"session/prompt": _permission_steps("s2")},
        "responses": {"session/prompt": {"$replies": True}},
    }
    async with make_client(script, on_permission=on_permission) as client:
        await client.initialize()
        turn = asyncio.create_task(client.prompt("s2", [TEXT]))
        await asyncio.wait_for(started.wait(), 5)
        await client.cancel("s1")
        release.set()
        result = await asyncio.wait_for(turn, 5)
    assert result["replies"]["p1"]["outcome"]["outcome"] == "selected"


def _elicit_script() -> dict[str, Any]:
    steps = [
        {
            "send": {
                "id": "e1",
                "method": "elicitation/create",
                "params": {"message": "name?", "mode": "form", "requestedSchema": {}},
            }
        },
        {"await": ["e1"]},
    ]
    return {
        "before": {"session/prompt": steps},
        "responses": {"session/prompt": {"$replies": True}},
        "echo": ["initialize"],
    }


async def test_elicitation_is_routed_and_advertised(make_client: MakeClient) -> None:
    async def on_elicitation(params: dict[str, Any]) -> dict[str, Any]:
        return {"action": "accept", "content": {"name": params["message"]}}

    async with make_client(_elicit_script(), on_elicitation=on_elicitation) as client:
        init = await client.initialize()
        result = await client.prompt("s1", [TEXT])
    assert init["echo"]["clientCapabilities"]["elicitation"] == {"form": {}, "url": {}}
    assert result["replies"]["e1"] == {"action": "accept", "content": {"name": "name?"}}


async def test_elicitation_without_a_handler_is_method_not_found(make_client: MakeClient) -> None:
    async def on_request(method: str, params: Any) -> Any:
        raise AssertionError("elicitation must not fall through to on_request")

    async with make_client(_elicit_script(), on_request=on_request) as client:
        init = await client.initialize()
        result = await client.prompt("s1", [TEXT])
    assert "elicitation" not in init["echo"]["clientCapabilities"]
    assert result["replies"]["e1"]["code"] == -32601


async def test_request_and_notify_passthrough(make_client: MakeClient) -> None:
    script = {"responses": {"_example/ping": {"pong": True}}, "echo": ["_example/ping"]}
    async with make_client(script) as client:
        result = await client.request("_example/ping", {"a": 1})
        assert result["pong"] is True
        assert result["echo"] == {"a": 1}
        await client.notify("_example/note", {"b": 2})


def test_helpers() -> None:
    options = [
        {"optionId": "a", "name": "A", "kind": "allow_always"},
        {"optionId": "b", "name": "B", "kind": "allow_once"},
        {"optionId": "c", "name": "C", "kind": "reject_once"},
    ]
    assert allow_once(options) == {"outcome": "selected", "optionId": "b"}
    assert reject_once(options) == {"outcome": "selected", "optionId": "c"}
    # No matching option: cancelled, never a broader grant.
    assert allow_once(options[:1]) == {"outcome": "cancelled"}
    assert reject_once([]) == {"outcome": "cancelled"}
    assert mcp_server_http("srv", "https://example.com/mcp", {"Authorization": "Bearer x"}) == {
        "type": "http",
        "name": "srv",
        "url": "https://example.com/mcp",
        "headers": [{"name": "Authorization", "value": "Bearer x"}],
    }
    assert mcp_server_stdio("srv", "run", ["--flag"], {"K": "V"}) == {
        "name": "srv",
        "command": "run",
        "args": ["--flag"],
        "env": [{"name": "K", "value": "V"}],
    }


def test_needs_exactly_one_way_to_connect() -> None:
    async def nothing(*_: Any) -> Any:
        return None

    with pytest.raises(ValueError, match="exactly one"):
        AcpClient(client_info={}, on_update=nothing, on_permission=nothing)
