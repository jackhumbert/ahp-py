"""The reverse direction, driven by a host that actually calls us."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from agent_host_client.client import AhpClient, ClientConfig
from agent_host_client.client.errors import (
    AlreadyExists,
    Conflict,
    MethodNotFound,
    NotFound,
    PermissionDenied,
)
from agent_host_client.client.mirror import StateMirror
from agent_host_client.serve import (
    ClientToolHost,
    FileResourceServer,
    InputResponder,
    ResourceRouter,
    VirtualResourceServer,
    file_uri,
    pending_inputs,
)
from agent_host_client.testing import FakeRpcError, echo_host

CHAT = "ahp-chat://c/s"
SESSION = "copilot:/s"


# ── routing ──────────────────────────────────────────────────────────────────


async def test_a_method_outside_servercommandmap_is_method_not_found() -> None:
    """AHP has no capability object, so -32601 is how a peer declines."""
    router = ResourceRouter()
    with pytest.raises(MethodNotFound):
        await router("createSession", {"uri": "file:///x"})


async def test_an_unmounted_prefix_is_not_found() -> None:
    router = ResourceRouter()
    with pytest.raises(NotFound):
        await router("resourceRead", {"uri": "file:///nowhere"})


async def test_longest_prefix_wins(tmp_path: Path) -> None:
    """A client commonly serves memory and disk under overlapping schemes."""
    router = ResourceRouter()
    virtual = VirtualResourceServer()
    virtual.put("plugins/a.md", "from memory")
    inner = VirtualResourceServer("virtual://plugins/deep/")
    inner.put("virtual://plugins/deep/b.md", "from the deeper mount")
    router.mount("virtual://", virtual)
    router.mount("virtual://plugins/deep/", inner)

    assert (await router("resourceRead", {"uri": "virtual://plugins/a.md"}))[
        "content"
    ] == "from memory"
    assert (await router("resourceRead", {"uri": "virtual://plugins/deep/b.md"}))[
        "content"
    ] == "from the deeper mount"


# ── the jail ─────────────────────────────────────────────────────────────────


def _server(tmp_path: Path, **kwargs: Any) -> FileResourceServer:
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "inside.txt").write_text("hello", encoding="utf-8")
    return FileResourceServer(tmp_path, **kwargs)


async def test_a_file_inside_the_root_reads(tmp_path: Path) -> None:
    server = _server(tmp_path)
    result = await server.handle("resourceRead", {"uri": file_uri(tmp_path / "inside.txt")})
    assert result["content"] == "hello"
    assert result["etag"].startswith('W/"')


async def test_a_path_outside_the_root_is_denied(tmp_path: Path) -> None:
    server = _server(tmp_path)
    with pytest.raises(PermissionDenied):
        await server.handle("resourceRead", {"uri": "file:///etc/passwd"})


async def test_traversal_out_of_the_root_is_denied(tmp_path: Path) -> None:
    server = _server(tmp_path / "root")
    outside = tmp_path / "secret.txt"
    outside.write_text("nope", encoding="utf-8")
    with pytest.raises(PermissionDenied):
        await server.handle(
            "resourceRead", {"uri": file_uri(tmp_path / "root" / ".." / "secret.txt")}
        )


async def test_a_symlink_escaping_the_root_is_denied(tmp_path: Path) -> None:
    """Containment is checked AFTER resolution. Checking the pre-resolution path
    and then opening it is the classic TOCTOU shape."""
    root = tmp_path / "root"
    root.mkdir()
    secret = tmp_path / "secret.txt"
    secret.write_text("nope", encoding="utf-8")
    (root / "escape").symlink_to(secret)
    server = FileResourceServer(root)
    with pytest.raises(PermissionDenied):
        await server.handle("resourceRead", {"uri": file_uri(root / "escape")})


async def test_a_missing_file_is_not_found_not_denied(tmp_path: Path) -> None:
    """The difference matters: denied means "ask for access", not found means
    "stop asking"."""
    server = _server(tmp_path)
    with pytest.raises(NotFound):
        await server.handle("resourceRead", {"uri": file_uri(tmp_path / "absent.txt")})


async def test_binary_content_comes_back_base64_not_mangled(tmp_path: Path) -> None:
    server = _server(tmp_path, writable=True)
    (tmp_path / "b.bin").write_bytes(b"\xff\xfe\x00\x01")
    result = await server.handle("resourceRead", {"uri": file_uri(tmp_path / "b.bin")})
    assert result["encoding"] == "base64"


# ── writing is a second opt-in ───────────────────────────────────────────────


async def test_writing_is_refused_by_default(tmp_path: Path) -> None:
    """Reading discloses and writing destroys; the two are not granted by the
    same gesture."""
    server = _server(tmp_path)
    with pytest.raises(PermissionDenied):
        await server.handle(
            "resourceWrite", {"uri": file_uri(tmp_path / "new.txt"), "content": "x"}
        )


async def test_writing_works_once_granted(tmp_path: Path) -> None:
    server = _server(tmp_path, writable=True)
    target = tmp_path / "new.txt"
    await server.handle("resourceWrite", {"uri": file_uri(target), "content": "written"})
    assert target.read_text(encoding="utf-8") == "written"


async def test_create_only_refuses_an_existing_file(tmp_path: Path) -> None:
    server = _server(tmp_path, writable=True)
    with pytest.raises(AlreadyExists):
        await server.handle(
            "resourceWrite",
            {"uri": file_uri(tmp_path / "inside.txt"), "content": "x", "createOnly": True},
        )


async def test_if_match_refuses_a_stale_etag(tmp_path: Path) -> None:
    """The lost-update guard the etag exists for."""
    server = _server(tmp_path, writable=True)
    uri = file_uri(tmp_path / "inside.txt")
    with pytest.raises(Conflict):
        await server.handle("resourceWrite", {"uri": uri, "content": "x", "ifMatch": 'W/"stale-0"'})


async def test_if_match_accepts_the_current_etag(tmp_path: Path) -> None:
    server = _server(tmp_path, writable=True)
    uri = file_uri(tmp_path / "inside.txt")
    current = (await server.handle("resourceResolve", {"uri": uri}))["etag"]
    await server.handle("resourceWrite", {"uri": uri, "content": "x", "ifMatch": current})
    assert (tmp_path / "inside.txt").read_text(encoding="utf-8") == "x"


async def test_append_uses_byte_offsets(tmp_path: Path) -> None:
    server = _server(tmp_path, writable=True)
    uri = file_uri(tmp_path / "inside.txt")
    await server.handle("resourceWrite", {"uri": uri, "content": " world", "mode": "append"})
    assert (tmp_path / "inside.txt").read_text(encoding="utf-8") == "hello world"


async def test_insert_positions_are_bytes_not_string_indices(tmp_path: Path) -> None:
    """Any non-ASCII content makes the two disagree, and the spec says bytes."""
    server = _server(tmp_path, writable=True)
    target = tmp_path / "u.txt"
    target.write_text("héllo", encoding="utf-8")  # 'é' is two bytes
    await server.handle(
        "resourceWrite",
        {"uri": file_uri(target), "content": "X", "mode": "insert", "position": 3},
    )
    assert target.read_bytes() == "héXllo".encode()


async def test_a_non_empty_directory_needs_recursive(tmp_path: Path) -> None:
    server = _server(tmp_path, writable=True)
    (tmp_path / "dir").mkdir()
    (tmp_path / "dir" / "f.txt").write_text("x", encoding="utf-8")
    with pytest.raises(Conflict):
        await server.handle("resourceDelete", {"uri": file_uri(tmp_path / "dir")})
    await server.handle("resourceDelete", {"uri": file_uri(tmp_path / "dir"), "recursive": True})
    assert not (tmp_path / "dir").exists()


async def test_resource_request_reports_what_is_actually_true(tmp_path: Path) -> None:
    """A client that always answers "granted" turns deny/request/deny into an
    infinite loop."""
    server = _server(tmp_path)
    assert (await server.handle("resourceRequest", {"uri": file_uri(tmp_path)}))["granted"]
    assert not (await server.handle("resourceRequest", {"uri": "file:///etc"}))["granted"]


# ── end to end, host-driven ──────────────────────────────────────────────────


async def test_a_host_can_read_a_file_back_out_of_the_client(tmp_path: Path) -> None:
    (tmp_path / "note.md").write_text("# hi", encoding="utf-8")
    router = ResourceRouter()
    router.mount("file://", FileResourceServer(tmp_path))

    host = echo_host()
    await host.start()
    client = AhpClient(host.transport(), ClientConfig())
    client.set_server_request_handler(router)
    await client.connect()

    result = await asyncio.wait_for(
        host.call("resourceRead", {"uri": file_uri(tmp_path / "note.md")}), 2
    )
    assert result["content"] == "# hi"
    await client.shutdown()
    await host.stop()


async def test_a_host_fetching_a_published_plugin_gets_its_children(tmp_path: Path) -> None:
    """This is why the reverse direction is not optional: a forward-only client
    publishes a plugin whose children never render."""
    virtual = VirtualResourceServer()
    virtual.put("plugins/skills/one.md", "skill one")
    virtual.put("plugins/skills/two.md", "skill two")
    router = ResourceRouter()
    router.mount("virtual://", virtual)

    host = echo_host()
    await host.start()
    client = AhpClient(host.transport())
    client.set_server_request_handler(router)
    await client.connect()

    listing = await asyncio.wait_for(
        host.call("resourceList", {"uri": "virtual://plugins/skills"}), 2
    )
    assert {e["name"] for e in listing["entries"]} == {"one.md", "two.md"}
    await client.shutdown()
    await host.stop()


async def test_a_denial_carries_the_request_that_would_be_needed(tmp_path: Path) -> None:
    router = ResourceRouter()
    router.mount("file://", FileResourceServer(tmp_path))
    host = echo_host()
    await host.start()
    client = AhpClient(host.transport())
    client.set_server_request_handler(router)
    await client.connect()

    with pytest.raises(FakeRpcError) as caught:
        await asyncio.wait_for(host.call("resourceRead", {"uri": "file:///etc/passwd"}), 2)
    assert caught.value.error["code"] == -32009
    assert "request" in caught.value.error["data"]
    await client.shutdown()
    await host.stop()


# ── client-owned tools ───────────────────────────────────────────────────────


async def test_a_client_owned_tool_executes_and_reports() -> None:
    """The agent gets the editor's own tools with no filesystem API on the host."""
    host = echo_host()
    await host.start()
    client = AhpClient(host.transport())
    await client.connect()
    tools = ClientToolHost(client, client_id="me")

    async def echo(action: dict[str, Any]) -> dict[str, Any]:
        return {"content": [{"kind": "text", "text": "ran " + action["toolName"]}]}

    tools.register({"name": "echo"}, echo)
    action = {
        "type": "chat/toolCallStart",
        "toolCallId": "t1",
        "toolName": "echo",
        "contributor": {"kind": "client", "clientId": "me"},
    }
    assert tools.owns(action)
    await tools.execute(CHAT, action)
    await asyncio.sleep(0.02)

    dispatched = [m for m in host.received if m.get("method") == "dispatchAction"]
    complete = dispatched[-1]["params"]["action"]
    assert complete["type"] == "chat/toolCallComplete"
    assert complete["result"]["content"][0]["text"] == "ran echo"
    await client.shutdown()
    await host.stop()


async def test_an_unknown_client_tool_is_denied_not_ignored() -> None:
    """A call nobody answers blocks the turn forever."""
    host = echo_host()
    await host.start()
    client = AhpClient(host.transport())
    await client.connect()
    tools = ClientToolHost(client, client_id="me")

    await tools.execute(
        CHAT, {"type": "chat/toolCallStart", "toolCallId": "t1", "toolName": "nope"}
    )
    await asyncio.sleep(0.02)
    action = [m for m in host.received if m.get("method") == "dispatchAction"][-1]["params"][
        "action"
    ]
    assert action["type"] == "chat/toolCallConfirmed"
    assert action["approved"] is False
    assert action["reason"] == "denied"
    await client.shutdown()
    await host.stop()


async def test_a_failing_tool_reports_an_error_result_rather_than_crashing() -> None:
    host = echo_host()
    await host.start()
    client = AhpClient(host.transport())
    await client.connect()
    tools = ClientToolHost(client, client_id="me")

    async def broken(action: dict[str, Any]) -> dict[str, Any]:
        raise RuntimeError("tool exploded")

    tools.register({"name": "broken"}, broken)
    await tools.execute(
        CHAT, {"type": "chat/toolCallStart", "toolCallId": "t1", "toolName": "broken"}
    )
    await asyncio.sleep(0.02)
    result = [m for m in host.received if m.get("method") == "dispatchAction"][-1]["params"][
        "action"
    ]["result"]
    assert result["isError"] is True
    assert "tool exploded" in result["content"][0]["text"]
    await client.shutdown()
    await host.stop()


def test_detach_uses_an_action_that_really_is_client_dispatchable() -> None:
    """Verified against the generated table, contradicting the prose that says
    a client never unsets itself."""
    from agent_host_protocol.types import IS_CLIENT_DISPATCHABLE

    host = ClientToolHost.__new__(ClientToolHost)
    host._client_id = "me"
    action = host.detach_action()
    assert IS_CLIENT_DISPATCHABLE[action["type"]] is True


# ── elicitation ──────────────────────────────────────────────────────────────


async def test_an_elicitation_is_answered_on_the_chat_it_names() -> None:
    """Answered without subscribing to that chat -- which is the whole point of
    the `SessionState.inputNeeded` aggregate."""
    host = echo_host()
    await host.start()
    client = AhpClient(host.transport())
    await client.connect()
    mirror = StateMirror(client_id="me")
    mirror.apply_snapshot(
        {
            "resource": SESSION,
            "state": {
                "lifecycle": "ready",
                "inputNeeded": [{"chat": CHAT, "requestId": "r1", "kind": "elicitation"}],
            },
            "fromSeq": 1,
        },
        reducer_name="session",
    )
    entries = pending_inputs(mirror, SESSION)
    assert len(entries) == 1

    responder = InputResponder(client, mirror)
    responder.answer(entries[0], answers={"name": "Ada"})
    await asyncio.sleep(0.02)
    action = [m for m in host.received if m.get("method") == "dispatchAction"][-1]["params"]
    assert action["channel"] == CHAT
    assert action["action"]["answers"] == {"name": "Ada"}
    await client.shutdown()
    await host.stop()


async def test_answers_merge_rather_than_clobbering_another_client() -> None:
    """`chat/inputAnswerChanged` merges per-question answers; replacing the map
    would discard a partial answer someone else is midway through."""
    host = echo_host()
    await host.start()
    client = AhpClient(host.transport())
    await client.connect()
    mirror = StateMirror(client_id="me")
    mirror.apply_snapshot(
        {
            "resource": CHAT,
            "state": {
                "turns": [],
                "inputRequests": [{"requestId": "r1", "answers": {"theirs": 1}}],
            },
            "fromSeq": 1,
        },
        reducer_name="chat",
    )
    responder = InputResponder(client, mirror)
    responder.answer({"chat": CHAT, "requestId": "r1"}, answers={"ours": 2})
    await asyncio.sleep(0.02)
    answers = [m for m in host.received if m.get("method") == "dispatchAction"][-1]["params"][
        "action"
    ]["answers"]
    assert answers == {"theirs": 1, "ours": 2}
    await client.shutdown()
    await host.stop()


async def test_result_confirmation_is_implemented() -> None:
    """`ahpx` ignores this, which hangs any turn using requiresResultConfirmation."""
    host = echo_host()
    await host.start()
    client = AhpClient(host.transport())
    await client.connect()
    responder = InputResponder(client, StateMirror(client_id="me"))
    responder.confirm_result({"chat": CHAT, "toolCallId": "t1"}, approved=True)
    await asyncio.sleep(0.02)
    action = [m for m in host.received if m.get("method") == "dispatchAction"][-1]["params"][
        "action"
    ]
    assert action["type"] == "chat/toolCallResultConfirmed"
    assert action["approved"] is True
    await client.shutdown()
    await host.stop()


async def test_tool_authentication_uses_the_authenticate_command_not_a_chat_action() -> None:
    """Dispatching a chat action here would leave the agent blocked."""
    host = echo_host()
    await host.start()
    host.on("authenticate", lambda _p: {})
    client = AhpClient(host.transport())
    await client.connect()
    responder = InputResponder(client, StateMirror(client_id="me"))
    await responder.authenticate(
        {
            "chat": CHAT,
            "kind": "toolAuthentication",
            "toolCall": {"auth": {"resource": "https://api.example"}},
        },
        token="t0ken",
    )
    sent = next(m for m in host.received if m.get("method") == "authenticate")
    assert sent["params"]["resource"] == "https://api.example"
    await client.shutdown()
    await host.stop()
