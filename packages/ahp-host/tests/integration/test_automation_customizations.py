"""`AutomationSessionTemplate.customizations` (1.0.0): plugins captured at save time.

Runs start when no client is connected, so the host copies each template
plugin from the dispatching client when the definition is saved, serves the
copy under its own URI, and hands it to every run session.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from ahp_protocol.channels import AUTOMATIONS_URI, ROOT_URI
from ahp_protocol.transport import memory_pair

from ahp_host.core import (
    FileAutomationStore,
    Host,
    InMemoryAutomationStore,
    LoopbackSingleUserPolicy,
)
from ahp_host.provider import EchoProvider

from .test_host_end_to_end import FakeClient

pytestmark = pytest.mark.anyio

AUTOMATION = "ahp-automation:/nightly"
_PLUGIN = "virtual://my-client/house"

_TREE: dict[str, str] = {
    f"{_PLUGIN}/review.prompt.md": "# Review Prompt\nbody",
    f"{_PLUGIN}/skills/deploy/SKILL.md": "steps, with no front-matter name",
}


class ServingClient(FakeClient):
    """Answers the host's `resourceList` / `resourceRead`, from a tree or forever."""

    def __init__(self, transport: Any, *, bottomless: bool = False) -> None:
        super().__init__(transport)
        self.served: list[str] = []
        self.bottomless = bottomless

    async def serve(self, *, seconds: float = 1.0) -> None:
        deadline = asyncio.get_running_loop().time() + seconds
        while (remaining := deadline - asyncio.get_running_loop().time()) > 0:
            try:
                message = await asyncio.wait_for(self.transport.receive(), timeout=remaining)
            except TimeoutError:
                return
            if message is None:
                return
            if "method" not in message or "id" not in message:
                if "id" not in message:
                    self.notifications.append(message)
                continue
            await self._answer(message)

    async def _answer(self, message: dict[str, Any]) -> None:
        method, uri = message["method"], (message.get("params") or {}).get("uri", "")
        self.served.append(f"{method} {uri}")
        result: Any = None
        if method == "resourceList" and self.bottomless:
            result = {"entries": [{"name": "deeper", "type": "directory"}]}
        elif method == "resourceList":
            prefix = f"{uri.rstrip('/')}/"
            names: dict[str, str] = {}
            for key in _TREE:
                if key.startswith(prefix):
                    head, _, rest = key[len(prefix) :].partition("/")
                    names[head] = "directory" if rest else "file"
            if names:
                result = {"entries": [{"name": n, "type": t} for n, t in names.items()]}
        elif method == "resourceRead" and uri in _TREE:
            result = {"data": _TREE[uri], "encoding": "utf-8"}
        if result is None:
            error = {"code": -32008, "message": "no such resource"}
            await self.transport.send({"jsonrpc": "2.0", "id": message["id"], "error": error})
            return
        await self.transport.send({"jsonrpc": "2.0", "id": message["id"], "result": result})


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


async def _client(host: Host, **kw: Any) -> tuple[ServingClient, dict[str, Any]]:
    client_transport, server_transport = memory_pair()
    task = asyncio.create_task(host.serve(server_transport))
    client = ServingClient(client_transport, **kw)
    client.serve_task = task  # type: ignore[attr-defined]
    reply = await client.request(
        "initialize",
        {
            "channel": ROOT_URI,
            "clientId": "c1",
            "protocolVersions": ["1.0.0"],
            "initialSubscriptions": [ROOT_URI, AUTOMATIONS_URI],
        },
    )
    return client, reply["result"]


def _plugin(nonce: str = "n1") -> dict[str, Any]:
    return {
        "type": "plugin",
        "id": "house",
        "uri": _PLUGIN,
        "name": "House rules",
        "nonce": nonce,
        "enablement": [{"kind": "session", "enabled": True}],
    }


def _definition(**session: Any) -> dict[str, Any]:
    return {
        "title": "Nightly",
        "message": {"text": "go", "origin": {"kind": "automation"}},
        "session": {"provider": "echo", **session},
        "enabled": True,
        "triggers": [],
    }


async def _dispatch(client: ServingClient, action: dict[str, Any], seq: int = 1) -> None:
    await client.notify(
        "dispatchAction", {"channel": AUTOMATIONS_URI, "clientSeq": seq, "action": action}
    )
    await client.serve(seconds=0.6)


def _entry(host: Host) -> dict[str, Any] | None:
    entries = (host.sequencer.state_of(AUTOMATIONS_URI) or {}).get("entries") or []
    return dict(entries[0]) if entries else None


async def _create(client: ServingClient, definition: dict[str, Any]) -> None:
    await _dispatch(
        client,
        {"type": "automation/createRequested", "resource": AUTOMATION, "definition": definition},
    )


async def test_the_capability_is_advertised() -> None:
    host = Host(EchoProvider(), LoopbackSingleUserPolicy(), automations=InMemoryAutomationStore())
    try:
        _, result = await _client(host)
        assert result["automations"]["customizations"] == {}
    finally:
        await host.aclose()


async def test_a_plugin_is_captured_and_served_as_the_hosts_own() -> None:
    host = Host(EchoProvider(), LoopbackSingleUserPolicy(), automations=InMemoryAutomationStore())
    try:
        client, _ = await _client(host)
        await _create(client, _definition(customizations=[_plugin()]))
        entry = _entry(host)
        assert entry is not None
        (copy,) = entry["customizations"]
        assert copy["id"] == "house"
        assert copy["uri"].startswith("ahp-plugin-copy:/")
        assert "clientId" not in copy
        assert copy["load"] == {"kind": "loaded"}
        assert sorted((c["type"], c["name"]) for c in copy["children"]) == [
            ("prompt", "Review Prompt"),
            ("skill", "deploy"),
        ]
        # Browsable with `resourceRead`, from the host, no client involved.
        child = next(c for c in copy["children"] if c["type"] == "prompt")
        read = await client.request("resourceRead", {"channel": ROOT_URI, "uri": child["uri"]})
        assert read["result"]["data"] == "# Review Prompt\nbody"
        listed = await client.request("resourceList", {"channel": ROOT_URI, "uri": copy["uri"]})
        assert {e["name"] for e in listed["result"]["entries"]} == {"review.prompt.md", "skills"}
    finally:
        await host.aclose()


async def test_an_unchanged_entry_keeps_its_copy_and_a_new_nonce_recaptures() -> None:
    host = Host(EchoProvider(), LoopbackSingleUserPolicy(), automations=InMemoryAutomationStore())
    try:
        client, _ = await _client(host)
        await _create(client, _definition(customizations=[_plugin()]))
        reads = len(client.served)
        assert reads > 0

        same = {"session": {"provider": "echo", "customizations": [_plugin()]}}
        await _dispatch(
            client,
            {"type": "automation/updateRequested", "resource": AUTOMATION, "changes": same},
            seq=2,
        )
        assert len(client.served) == reads, "an unchanged entry was captured again"

        newer = {"session": {"provider": "echo", "customizations": [_plugin("n2")]}}
        await _dispatch(
            client,
            {"type": "automation/updateRequested", "resource": AUTOMATION, "changes": newer},
            seq=3,
        )
        assert len(client.served) > reads
        assert host._automations[AUTOMATION].captures["house"].nonce == "n2"
    finally:
        await host.aclose()


async def test_a_failed_capture_rejects_the_whole_action() -> None:
    host = Host(EchoProvider(), LoopbackSingleUserPolicy(), automations=InMemoryAutomationStore())
    try:
        client, _ = await _client(host, bottomless=True)
        await _create(client, _definition(customizations=[_plugin()]))
        assert _entry(host) is None
        echoes = [
            n["params"]
            for n in client.notifications
            if n.get("method") == "action"
            and n["params"]["action"]["type"] == "automation/createRequested"
        ]
        assert echoes
        assert "capture failed" in echoes[-1]["rejectionReason"]
    finally:
        await host.aclose()


async def test_a_run_session_gets_the_copy_with_the_templates_enablement() -> None:
    host = Host(EchoProvider(), LoopbackSingleUserPolicy(), automations=InMemoryAutomationStore())
    try:
        client, _ = await _client(host)
        await _create(client, _definition(customizations=[_plugin()]))
        reply = await client.request(
            "runAutomation",
            {"channel": AUTOMATIONS_URI, "automation": AUTOMATION, "requestId": "r1"},
        )
        run = reply["result"]["resource"]

        def session_of_run() -> str | None:
            sessions = (host.sequencer.state_of(run) or {}).get("sessions") or []
            return sessions[0] if sessions else None

        deadline = asyncio.get_running_loop().time() + 5
        while asyncio.get_running_loop().time() < deadline:
            uri = session_of_run()
            state = host.sequencer.state_of(uri) if uri else None
            if state and any(c.get("id") == "house" for c in state.get("customizations") or []):
                break
            await asyncio.sleep(0.02)
        uri = session_of_run()
        assert uri is not None
        state = host.sequencer.state_of(uri) or {}
        copy = next(c for c in state["customizations"] if c["id"] == "house")
        assert copy["uri"].startswith("ahp-plugin-copy:/")
        assert copy["enablement"] == [{"kind": "session", "enabled": True}]
        assert "clientId" not in copy
    finally:
        await host.aclose()


async def test_copies_survive_a_restart(tmp_path: Path) -> None:
    host = Host(
        EchoProvider(), LoopbackSingleUserPolicy(), automations=FileAutomationStore(tmp_path)
    )
    try:
        client, _ = await _client(host)
        await _create(client, _definition(customizations=[_plugin()]))
        assert _entry(host) is not None
    finally:
        await host.aclose()
    again = Host(
        EchoProvider(), LoopbackSingleUserPolicy(), automations=FileAutomationStore(tmp_path)
    )
    try:
        await _client(again)
        entry = _entry(again)
        assert entry is not None
        (copy,) = entry["customizations"]
        assert len(copy["children"]) == 2
    finally:
        await again.aclose()


async def test_duplicate_ids_are_rejected() -> None:
    host = Host(EchoProvider(), LoopbackSingleUserPolicy(), automations=InMemoryAutomationStore())
    try:
        client, _ = await _client(host)
        await _create(client, _definition(customizations=[_plugin(), _plugin()]))
        assert _entry(host) is None
    finally:
        await host.aclose()
