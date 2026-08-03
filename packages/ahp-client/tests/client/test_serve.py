"""The reverse direction, driven by a host that actually calls us."""

from __future__ import annotations

import asyncio
import base64
import re
from pathlib import Path
from typing import Any

import pytest

from agent_host_client import connect
from agent_host_client.client import AhpClient, ClientConfig
from agent_host_client.client.errors import (
    AlreadyExists,
    Conflict,
    InvalidParams,
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
    ResourceServer,
    VirtualResourceServer,
    file_uri,
    pending_inputs,
)
from agent_host_client.testing import FakeHost, FakeRpcError, echo_host

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


async def test_a_call_with_no_uri_is_invalid_params() -> None:
    """-32008 tells the caller the URI was fine and the resource was gone, so it
    stops asking. Coercing an absent `uri` to `""` reported exactly that for a
    frame that was never valid."""
    router = ResourceRouter()
    with pytest.raises(InvalidParams):
        await router("resourceRead", {"channel": "ahp-root://"})
    with pytest.raises(InvalidParams):
        await router("resourceCopy", {"channel": "ahp-root://", "destination": "file:///x"})


async def test_the_router_is_both_a_handler_and_a_resource_server(tmp_path: Path) -> None:
    """`connect(resources=...)` calls `.handle`; `set_server_request_handler`
    calls the object. The router is the documented way to serve more than one
    mount, and offering only `__call__` made every reverse call through the front
    door answer -32603 `'ResourceRouter' object has no attribute 'handle'`."""
    (tmp_path / "note.md").write_text("# hi", encoding="utf-8")
    router = ResourceRouter()
    router.mount("file://", FileResourceServer(tmp_path))
    assert isinstance(router, ResourceServer)

    host = echo_host()
    await host.start()
    async with connect(transport=host.transport(), resources=router):
        result = await asyncio.wait_for(
            host.call(
                "resourceRead", {"channel": "ahp-root://", "uri": file_uri(tmp_path / "note.md")}
            ),
            2,
        )
    assert result["data"] == "# hi"
    await host.stop()


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
        "data"
    ] == "from memory"
    assert (await router("resourceRead", {"uri": "virtual://plugins/deep/b.md"}))[
        "data"
    ] == "from the deeper mount"


# ── the jail ─────────────────────────────────────────────────────────────────


def _server(tmp_path: Path, **kwargs: Any) -> FileResourceServer:
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "inside.txt").write_text("hello", encoding="utf-8")
    return FileResourceServer(tmp_path, **kwargs)


async def test_a_file_inside_the_root_reads(tmp_path: Path) -> None:
    server = _server(tmp_path)
    result = await server.handle("resourceRead", {"uri": file_uri(tmp_path / "inside.txt")})
    # `ResourceReadResult` is `{data, encoding}`, both required; there is no
    # `content` property, and a host reading `data` got nothing at all.
    assert result == {"data": "hello", "encoding": "utf-8", "etag": result["etag"]}
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
        await server.handle("resourceWrite", {"uri": file_uri(tmp_path / "new.txt"), "data": "x"})


async def test_writing_works_once_granted(tmp_path: Path) -> None:
    server = _server(tmp_path, writable=True)
    target = tmp_path / "new.txt"
    await server.handle("resourceWrite", {"uri": file_uri(target), "data": "written"})
    assert target.read_text(encoding="utf-8") == "written"


async def test_create_only_refuses_an_existing_file(tmp_path: Path) -> None:
    server = _server(tmp_path, writable=True)
    with pytest.raises(AlreadyExists):
        await server.handle(
            "resourceWrite",
            {"uri": file_uri(tmp_path / "inside.txt"), "data": "x", "createOnly": True},
        )


async def test_if_match_refuses_a_stale_etag(tmp_path: Path) -> None:
    """The lost-update guard the etag exists for."""
    server = _server(tmp_path, writable=True)
    uri = file_uri(tmp_path / "inside.txt")
    with pytest.raises(Conflict):
        await server.handle("resourceWrite", {"uri": uri, "data": "x", "ifMatch": 'W/"stale-0"'})


async def test_if_match_accepts_the_current_etag(tmp_path: Path) -> None:
    server = _server(tmp_path, writable=True)
    uri = file_uri(tmp_path / "inside.txt")
    current = (await server.handle("resourceResolve", {"uri": uri}))["etag"]
    await server.handle("resourceWrite", {"uri": uri, "data": "x", "ifMatch": current})
    assert (tmp_path / "inside.txt").read_text(encoding="utf-8") == "x"


async def test_append_uses_byte_offsets(tmp_path: Path) -> None:
    server = _server(tmp_path, writable=True)
    uri = file_uri(tmp_path / "inside.txt")
    await server.handle("resourceWrite", {"uri": uri, "data": " world", "mode": "append"})
    assert (tmp_path / "inside.txt").read_text(encoding="utf-8") == "hello world"


async def test_insert_positions_are_bytes_not_string_indices(tmp_path: Path) -> None:
    """Any non-ASCII content makes the two disagree, and the spec says bytes."""
    server = _server(tmp_path, writable=True)
    target = tmp_path / "u.txt"
    target.write_text("héllo", encoding="utf-8")  # 'é' is two bytes
    await server.handle(
        "resourceWrite",
        {"uri": file_uri(target), "data": "X", "mode": "insert", "position": 3},
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
    infinite loop.

    `ResourceRequestResult` is "an empty object on success" and a denial "MUST
    respond with `PermissionDenied` (-32009)", so a successful
    ``{"granted": false}`` *is* the loop: the caller is told asking again would
    help, and it asks again.
    """
    server = _server(tmp_path)
    assert await server.handle("resourceRequest", {"uri": file_uri(tmp_path)}) == {}
    with pytest.raises(PermissionDenied):
        await server.handle("resourceRequest", {"uri": "file:///etc"})


async def test_a_write_request_is_answered_by_the_write_permission(tmp_path: Path) -> None:
    """The answer has to agree with what `resourceWrite` will actually do.

    Ignoring the `read`/`write` flags granted a write against a read-only mount,
    and the write that followed was refused -32009.
    """
    server = _server(tmp_path)
    uri = file_uri(tmp_path / "inside.txt")
    # "A request with neither flag set is treated as `read: true` by receivers."
    assert await server.handle("resourceRequest", {"uri": uri}) == {}
    assert await server.handle("resourceRequest", {"uri": uri, "read": True}) == {}
    with pytest.raises(PermissionDenied):
        await server.handle("resourceRequest", {"uri": uri, "write": True})
    writable = FileResourceServer(tmp_path, writable=True)
    assert await writable.handle("resourceRequest", {"uri": uri, "write": True}) == {}


# ── the shapes the schema declares ───────────────────────────────────────────


async def test_read_honours_a_requested_base64_encoding(tmp_path: Path) -> None:
    """ "The server SHOULD honor the `encoding` requested in the params"."""
    server = _server(tmp_path)
    result = await server.handle(
        "resourceRead", {"uri": file_uri(tmp_path / "inside.txt"), "encoding": "base64"}
    )
    assert result["encoding"] == "base64"
    assert base64.b64decode(result["data"]) == b"hello"


async def test_directory_entries_are_named_and_typed(tmp_path: Path) -> None:
    """`DirectoryEntry` is `{name, type}`. A host keying on `type` per the schema
    read `kind` as absent and treated every client directory as a file, so it
    tried to READ the directory instead of recursing into it."""
    server = _server(tmp_path)
    (tmp_path / "sub").mkdir()
    entries = (await server.handle("resourceList", {"uri": file_uri(tmp_path)}))["entries"]
    assert entries == [
        {"name": "inside.txt", "type": "file"},
        {"name": "sub", "type": "directory"},
    ]


async def test_resolve_reports_a_type_and_an_iso_mtime(tmp_path: Path) -> None:
    """`ResourceResolveResult` requires `uri` and `type`; `mtime` is "Last-modified
    time in ISO 8601 format", which epoch milliseconds are not."""
    server = _server(tmp_path)
    result = await server.handle("resourceResolve", {"uri": file_uri(tmp_path / "inside.txt")})
    assert result["type"] == "file"
    assert "kind" not in result
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z", str(result["mtime"]))


async def test_resolve_without_following_symlinks_reports_the_link(tmp_path: Path) -> None:
    """ "When `false`, stat the link itself (lstat semantics) and report
    `type: 'symlink'`" -- which the jail can serve because it still contains the
    link's directory and never opens the target."""
    server = _server(tmp_path)
    (tmp_path / "link").symlink_to(tmp_path / "inside.txt")
    # Built by hand: `file_uri` resolves, so it would name the target and the
    # flag would have nothing left to act on.
    uri = f"{file_uri(tmp_path)}/link"
    result = await server.handle("resourceResolve", {"uri": uri, "followSymlinks": False})
    assert result["type"] == "symlink"
    assert result["uri"] == uri


# ── writing, where the wrong key destroyed data ──────────────────────────────


async def test_a_spec_shaped_write_does_not_truncate_the_file(tmp_path: Path) -> None:
    """The data-loss case. Reading `content` rather than `data` decoded ``None``
    to ``b""`` and ran ``write_bytes(b"")``, so a conformant caller's payload was
    dropped, the file was emptied, and the call answered success."""
    server = _server(tmp_path, writable=True)
    target = tmp_path / "inside.txt"
    await server.handle(
        "resourceWrite", {"uri": file_uri(target), "data": "kept", "encoding": "utf-8"}
    )
    assert target.read_bytes() == b"kept"


async def test_a_write_with_no_data_is_refused_rather_than_emptying_the_file(
    tmp_path: Path,
) -> None:
    """`data` is required, and an empty write is never the right reading of its
    absence -- it is the one outcome the caller cannot undo."""
    server = _server(tmp_path, writable=True)
    target = tmp_path / "inside.txt"
    with pytest.raises(InvalidParams):
        await server.handle("resourceWrite", {"uri": file_uri(target)})
    assert target.read_bytes() == b"hello"


async def test_base64_data_round_trips(tmp_path: Path) -> None:
    server = _server(tmp_path, writable=True)
    target = tmp_path / "b.bin"
    await server.handle(
        "resourceWrite",
        {
            "uri": file_uri(target),
            "data": base64.b64encode(b"\x00\x01").decode(),
            "encoding": "base64",
        },
    )
    assert target.read_bytes() == b"\x00\x01"


async def test_truncate_is_rooted_at_the_start_of_the_file(tmp_path: Path) -> None:
    """ "`truncate`: offset from the start of the file at which to truncate before
    writing", so the result is `existing[0..position] + data`."""
    server = _server(tmp_path, writable=True)
    uri = file_uri(tmp_path / "inside.txt")
    await server.handle("resourceWrite", {"uri": uri, "data": "P", "position": 2})
    assert (tmp_path / "inside.txt").read_bytes() == b"heP"


async def test_append_position_counts_backwards_from_eof(tmp_path: Path) -> None:
    """ "`position` counts bytes backwards from EOF... `position: 5` inserts `data`
    5 bytes before the current EOF"."""
    server = _server(tmp_path, writable=True)
    uri = file_uri(tmp_path / "inside.txt")
    await server.handle("resourceWrite", {"uri": uri, "data": "X", "mode": "append", "position": 3})
    assert (tmp_path / "inside.txt").read_bytes() == b"heXllo"


async def test_mkdir_creates_the_directory_it_was_asked_for(tmp_path: Path) -> None:
    """ "The receiver MUST create any missing parent directories" for `uri` itself.
    Creating the contained PARENT instead produced `made` and reported success
    for `made/deeper`."""
    server = _server(tmp_path, writable=True)
    await server.handle("resourceMkdir", {"uri": file_uri(tmp_path / "made" / "deeper")})
    assert (tmp_path / "made" / "deeper").is_dir()


async def test_mkdir_over_a_file_is_already_exists(tmp_path: Path) -> None:
    """ "If `uri` already exists but is **not** a directory, the server MUST fail
    with `AlreadyExists`"."""
    server = _server(tmp_path, writable=True)
    with pytest.raises(AlreadyExists):
        await server.handle("resourceMkdir", {"uri": file_uri(tmp_path / "inside.txt")})


@pytest.mark.parametrize("method", ["resourceCopy", "resourceMove"])
async def test_fail_if_exists_refuses_to_overwrite(tmp_path: Path, method: str) -> None:
    """ "If `true`, the server MUST fail if the destination already exists instead
    of overwriting it." The caller set the flag precisely because it did not want
    those bytes replaced, and they were replaced anyway."""
    server = _server(tmp_path, writable=True)
    destination = tmp_path / "dst.txt"
    destination.write_text("PRE-EXISTING", encoding="utf-8")
    with pytest.raises(AlreadyExists):
        await server.handle(
            method,
            {
                "source": file_uri(tmp_path / "inside.txt"),
                "destination": file_uri(destination),
                "failIfExists": True,
            },
        )
    assert destination.read_text(encoding="utf-8") == "PRE-EXISTING"


async def test_an_empty_directory_deletes_without_recursive(tmp_path: Path) -> None:
    """Only a NON-empty directory needs `recursive`. Demanding it for an empty one
    makes the caller pass a flag that also authorises deleting a whole tree."""
    server = _server(tmp_path, writable=True)
    (tmp_path / "empty").mkdir()
    await server.handle("resourceDelete", {"uri": file_uri(tmp_path / "empty")})
    assert not (tmp_path / "empty").exists()


# ── in-memory plugins ────────────────────────────────────────────────────────


async def test_a_virtual_plugin_lists_its_intermediate_directories() -> None:
    """A host expands a plugin by walking DOWN from its root. Registering only
    each blob's immediate parent left `plugins` listing empty, so the skill under
    `plugins/skills/<name>/` was never reached."""
    virtual = VirtualResourceServer()
    virtual.put("plugins/skills/linting/SKILL.md", "# Lint")
    virtual.put("plugins/agents/reviewer.md", "# Reviewer")

    top = (await virtual.handle("resourceList", {"uri": "virtual://plugins"}))["entries"]
    assert sorted(top, key=lambda e: str(e["name"])) == [
        {"name": "agents", "type": "directory"},
        {"name": "skills", "type": "directory"},
    ]
    skills = (await virtual.handle("resourceList", {"uri": "virtual://plugins/skills"}))["entries"]
    assert skills == [{"name": "linting", "type": "directory"}]


async def test_an_unpublished_virtual_uri_is_denied_not_granted() -> None:
    """Nothing outside what was published can ever be served, so a grant would be
    a lie the caller acts on."""
    virtual = VirtualResourceServer()
    virtual.put("plugins/a.md", "a")
    assert await virtual.handle("resourceRequest", {"uri": "virtual://plugins/a.md"}) == {}
    with pytest.raises(PermissionDenied):
        await virtual.handle("resourceRequest", {"uri": "virtual://nope"})


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
    assert result["data"] == "# hi"
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
    # `PermissionDeniedErrorData.request` is a `ResourceRequestParams`, so it
    # carries the required `channel` and the read/write flag -- a payload the
    # caller can feed straight back to `resourceRequest`, which a bare
    # `{"reason": ...}` was not.
    assert caught.value.error["data"]["request"] == {
        "channel": "ahp-root://",
        "uri": "file:///etc/passwd",
        "read": True,
    }
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
        return {
            "success": True,
            "pastTenseMessage": "Echoed",
            "content": [{"type": "text", "text": "ran " + action["toolName"]}],
        }

    tools.register({"name": "echo"}, echo)
    action = {
        "type": "chat/toolCallStart",
        "turnId": "turn-1",
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
    # Required, and what the reducer matches on before it will touch the call.
    assert complete["turnId"] == "turn-1"
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
        CHAT,
        {"type": "chat/toolCallStart", "turnId": "turn-1", "toolCallId": "t1", "toolName": "nope"},
    )
    await asyncio.sleep(0.02)
    action = [m for m in host.received if m.get("method") == "dispatchAction"][-1]["params"][
        "action"
    ]
    assert action["type"] == "chat/toolCallConfirmed"
    assert action["turnId"] == "turn-1"
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
        CHAT,
        {
            "type": "chat/toolCallStart",
            "turnId": "turn-1",
            "toolCallId": "t1",
            "toolName": "broken",
        },
    )
    await asyncio.sleep(0.02)
    result = [m for m in host.received if m.get("method") == "dispatchAction"][-1]["params"][
        "action"
    ]["result"]
    # `ToolCallResult` requires both of these and has no `isError` -- that is
    # MCP's spelling, and a result carrying it reads as a *success* here.
    assert result["success"] is False
    assert result["pastTenseMessage"]
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


#: One `SessionChatInputRequest` as a host really mirrors it. The entry's own
#: `id` is opaque and deliberately NOT the request id here -- "the host derives
#: it however it likes (for example from the chat URI plus the underlying
#: request id); consumers MUST treat it as opaque".
ELICITATION = {
    "kind": "chatInput",
    "id": f"{CHAT}#in-1",
    "chat": CHAT,
    "request": {
        "id": "in-1",
        "message": "Echo it back how?",
        "questions": [
            {
                "id": "style",
                "kind": "single-select",
                "message": "Style?",
                "options": [{"id": "shout", "label": "SHOUT"}],
            }
        ],
    },
}


def _last_action(host: FakeHost) -> dict[str, Any]:
    sent = [m for m in host.received if m.get("method") == "dispatchAction"]
    params = sent[-1]["params"]
    assert isinstance(params, dict)
    return params


async def test_an_elicitation_is_completed_on_the_chat_it_names() -> None:
    """Answered without subscribing to that chat -- the whole point of the
    `SessionState.inputNeeded` aggregate -- and answered with the action that
    actually resolves it. `chat/inputAnswerChanged`, which this sent, only syncs
    a draft: the host stays parked, the chat stays `InputNeeded`, and the turn
    hangs forever. The id is `request.id`, never the entry's opaque `id`."""
    host = echo_host()
    await host.start()
    client = AhpClient(host.transport())
    await client.connect()
    mirror = StateMirror(client_id="me")
    mirror.apply_snapshot(
        {
            "resource": SESSION,
            "state": {"lifecycle": "ready", "inputNeeded": [ELICITATION]},
            "fromSeq": 1,
        },
        reducer_name="session",
    )
    entries = pending_inputs(mirror, SESSION)
    assert len(entries) == 1

    responder = InputResponder(client, mirror)
    responder.answer(entries[0], answers={"style": "shout"})
    await asyncio.sleep(0.02)
    params = _last_action(host)
    assert params["channel"] == CHAT
    assert params["action"] == {
        "type": "chat/inputCompleted",
        "requestId": "in-1",
        "response": "accept",
        "answers": {
            "style": {"state": "submitted", "value": {"kind": "selected", "value": "shout"}}
        },
    }
    await client.shutdown()
    await host.stop()


async def test_declining_needs_no_answers_at_all() -> None:
    """`decline` and `cancel` are outcomes of the same action; an empty
    `answers` map is omitted rather than sent."""
    host = echo_host()
    await host.start()
    client = AhpClient(host.transport())
    await client.connect()
    responder = InputResponder(client, StateMirror(client_id="me"))
    responder.answer(ELICITATION, response="decline")
    await asyncio.sleep(0.02)
    assert _last_action(host)["action"] == {
        "type": "chat/inputCompleted",
        "requestId": "in-1",
        "response": "decline",
    }
    await client.shutdown()
    await host.stop()


async def test_a_draft_syncs_one_question_without_resolving_the_request() -> None:
    """`chat/inputAnswerChanged` requires `[type, requestId, questionId]` and
    carries a **singular** `answer` -- there is no `answers` map and no `kind` on
    it. It is how "a user can answer one question on client A and another on
    client B" works, and it is not an answer to the request."""
    host = echo_host()
    await host.start()
    client = AhpClient(host.transport())
    await client.connect()
    responder = InputResponder(client, StateMirror(client_id="me"))

    responder.sync_draft(ELICITATION, "style", "shout")
    await asyncio.sleep(0.02)
    assert _last_action(host)["action"] == {
        "type": "chat/inputAnswerChanged",
        "requestId": "in-1",
        "questionId": "style",
        "answer": {"state": "draft", "value": {"kind": "selected", "value": "shout"}},
    }

    # "Dispatching with `answer: undefined` removes that question's answer
    # draft" -- and the reducer keys the removal on the key being ABSENT, so a
    # serialised `"answer": null` would be stored as a value instead.
    responder.sync_draft(ELICITATION, "style", None)
    await asyncio.sleep(0.02)
    assert _last_action(host)["action"] == {
        "type": "chat/inputAnswerChanged",
        "requestId": "in-1",
        "questionId": "style",
    }
    await client.shutdown()
    await host.stop()


async def test_the_questions_are_read_from_the_open_request_part() -> None:
    """There is no `ChatState.inputRequests`; a live request is the `request` of
    the `kind: "inputRequest"` response part on the active turn, which is where
    the questions and every client's drafts are, and where the host reads the
    final answers back out of. A caller holding only an id still gets a
    correctly encoded answer, because the kind is found there."""
    host = echo_host()
    await host.start()
    client = AhpClient(host.transport())
    await client.connect()
    mirror = StateMirror(client_id="me")
    mirror.apply_snapshot(
        {"resource": CHAT, "state": {"turns": []}, "fromSeq": 1}, reducer_name="chat"
    )
    mirror.apply(
        {
            "channel": CHAT,
            "serverSeq": 2,
            "action": {"type": "chat/turnStarted", "turnId": "t1", "message": {"text": "hi"}},
        }
    )
    mirror.apply(
        {
            "channel": CHAT,
            "serverSeq": 3,
            "action": {
                "type": "chat/inputRequested",
                "turnId": "t1",
                "request": ELICITATION["request"],
            },
        }
    )
    responder = InputResponder(client, mirror)
    assert responder.open_request(CHAT, "in-1")["message"] == "Echo it back how?"

    responder.answer({"chat": CHAT, "requestId": "in-1"}, answers={"style": "shout"})
    await asyncio.sleep(0.02)
    assert _last_action(host)["action"]["answers"] == {
        "style": {"state": "submitted", "value": {"kind": "selected", "value": "shout"}}
    }
    await client.shutdown()
    await host.stop()


async def test_result_confirmation_is_implemented() -> None:
    """`ahpx` ignores this, which hangs any turn using requiresResultConfirmation."""
    host = echo_host()
    await host.start()
    client = AhpClient(host.transport())
    await client.connect()
    responder = InputResponder(client, StateMirror(client_id="me"))
    # The entry shape `SessionState.inputNeeded` really publishes: `turnId` and
    # the nested `toolCall` are both required on it, "so a client can answer by
    # dispatching the ordinary chat action without having subscribed".
    responder.confirm_result(
        {"id": f"{CHAT}#t1", "chat": CHAT, "turnId": "turn-1", "toolCall": {"toolCallId": "t1"}},
        approved=True,
    )
    await asyncio.sleep(0.02)
    action = [m for m in host.received if m.get("method") == "dispatchAction"][-1]["params"][
        "action"
    ]
    assert action["type"] == "chat/toolCallResultConfirmed"
    assert action["turnId"] == "turn-1"
    assert action["toolCallId"] == "t1"
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
