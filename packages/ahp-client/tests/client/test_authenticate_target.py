"""`authenticate{target}` (upstream `c5628cd4`): sent only when given, verbatim."""

from __future__ import annotations

from typing import Any

from ahp_client.client import AhpClient
from ahp_client.client.mirror import StateMirror
from ahp_client.serve import InputResponder
from ahp_client.testing import echo_host

CHAT = "ahp-chat://c/s"
TARGET = {"kind": "mcpServer", "session": "copilot:/s1", "customizationId": "github"}


async def _sent(drive: Any) -> list[dict[str, Any]]:
    host = echo_host()
    await host.start()
    host.on("authenticate", lambda _p: {})
    client = AhpClient(host.transport())
    await client.connect()
    try:
        await drive(client)
        return [m["params"] for m in host.received if m.get("method") == "authenticate"]
    finally:
        await client.shutdown()
        await host.stop()


async def test_no_target_is_sent_unless_given() -> None:
    """An untargeted push is today's behaviour, byte for byte."""

    async def drive(client: AhpClient) -> None:
        await client.authenticate(resource="https://api.example", token="t0ken")

    (params,) = await _sent(drive)
    assert "target" not in params


async def test_the_target_is_sent_verbatim_when_given() -> None:
    async def drive(client: AhpClient) -> None:
        await client.authenticate(
            resource="https://api.example", token="t0ken", scopes=["read"], target=TARGET
        )

    (params,) = await _sent(drive)
    assert params["target"] == TARGET
    assert params["scopes"] == ["read"]
    assert params["resource"] == "https://api.example"


async def test_a_target_kind_the_client_does_not_know_is_forwarded() -> None:
    """The kind enum is non-exhaustive; the host decides, not the client."""
    unknown = {"kind": "somethingNew", "x": 1}

    async def drive(client: AhpClient) -> None:
        await client.authenticate(resource="https://api.example", token="t", target=unknown)

    (params,) = await _sent(drive)
    assert params["target"] == unknown


async def test_the_input_responder_forwards_a_target() -> None:
    entry = {
        "chat": CHAT,
        "kind": "toolAuthentication",
        "toolCall": {"auth": {"resource": "https://api.example"}},
    }

    async def drive(client: AhpClient) -> None:
        responder = InputResponder(client, StateMirror(client_id="me"))
        await responder.authenticate(entry, token="t0ken", target=TARGET)
        await responder.authenticate(entry, token="t0ken")

    targeted, untargeted = await _sent(drive)
    assert targeted["target"] == TARGET
    assert "target" not in untargeted
