"""Wire logs, the conformance probe, and interop with the sibling host."""

from __future__ import annotations

import asyncio
import contextlib
import importlib.util
from pathlib import Path

import pytest

from agent_host_client.client.client import AhpClient
from agent_host_client.doctor import diagnose
from agent_host_client.testing import FakeRpcError, echo_host
from agent_host_client.wirelog.jsonl import WireLog, logged, read_jsonl

# ── wire logs ────────────────────────────────────────────────────────────────


def test_a_frame_is_written_verbatim_with_the_inspector_sidecar(tmp_path: Path) -> None:
    log = WireLog(tmp_path / "ahp-test.jsonl", connection_id="c1")
    log.record("c2s", {"jsonrpc": "2.0", "id": 1, "method": "ping", "params": {}})
    entry = next(iter(read_jsonl(log.path)))
    assert entry["method"] == "ping"
    assert entry["_ahpLog"]["dir"] == "c2s"
    assert entry["_ahpLog"]["connectionId"] == "c1"
    assert entry["_ahpLog"]["transport"] == "websocket"


def test_credentials_never_reach_the_file(tmp_path: Path) -> None:
    """A flag to log them verbatim is a flag somebody eventually sets on a
    machine they do not control. The client is the party that sends
    `authenticate{token}`, so this matters more here than on the host."""
    log = WireLog(tmp_path / "ahp-test.jsonl")
    log.record(
        "c2s",
        {
            "method": "authenticate",
            "params": {
                "token": "s3cret",
                "nested": {"apiKey": "also-secret", "Authorization": "Bearer x"},
                "resource": "https://api.example",
            },
        },
    )
    raw = (tmp_path / "ahp-test.jsonl").read_text(encoding="utf-8")
    assert "s3cret" not in raw
    assert "also-secret" not in raw
    assert "Bearer x" not in raw
    # Everything else survives -- a redacted log is still a usable log.
    assert "https://api.example" in raw


def test_redaction_does_not_mutate_the_frame_being_sent(tmp_path: Path) -> None:
    """Redacting in place would send the redaction."""
    log = WireLog(tmp_path / "ahp-test.jsonl")
    params = {"token": "keep-me"}
    log.record("c2s", {"method": "authenticate", "params": params})
    assert params["token"] == "keep-me"


def test_the_log_is_owner_only(tmp_path: Path) -> None:
    log = WireLog(tmp_path / "ahp-test.jsonl")
    assert log.path.stat().st_mode & 0o777 == 0o600


def test_the_default_filename_is_one_the_inspector_discovers(tmp_path: Path) -> None:
    import re

    log = WireLog(tmp_path / "ahp-client-1.jsonl")
    assert re.match(r"^(agenthost|agent-host|ahp).*\.jsonl$", log.path.name, re.I)


def test_a_truncated_final_line_does_not_end_the_read(tmp_path: Path) -> None:
    """Which is what a log looks like when the process was killed -- exactly
    when you want to read it."""
    path = tmp_path / "ahp-test.jsonl"
    path.write_text('{"a": 1}\n{"b": 2}\n{"c": ', encoding="utf-8")
    assert list(read_jsonl(path)) == [{"a": 1}, {"b": 2}]


def test_a_bom_and_crlf_are_tolerated(tmp_path: Path) -> None:
    path = tmp_path / "ahp-test.jsonl"
    path.write_bytes(b'\xef\xbb\xbf{"a": 1}\r\n{"b": 2}\r\n')
    assert list(read_jsonl(path)) == [{"a": 1}, {"b": 2}]


async def test_a_logged_transport_records_both_directions(tmp_path: Path) -> None:
    host = echo_host()
    await host.start()
    log = WireLog(tmp_path / "ahp-live.jsonl")
    client = AhpClient(logged(host.transport(), log))
    await client.connect()
    await client.initialize(client_id="c1")
    await client.shutdown()
    await host.stop()

    directions = {e["_ahpLog"]["dir"] for e in read_jsonl(log.path)}
    assert directions == {"c2s", "s2c"}


# ── doctor ───────────────────────────────────────────────────────────────────


async def test_a_conformant_host_passes() -> None:
    host = echo_host()
    await host.start()
    report = await diagnose(host.transport())
    assert report.ok, str(report)
    await host.stop()


async def test_a_root_uri_missing_a_slash_is_caught() -> None:
    """`ahp-root:/` is a different channel: the reference client compares the
    root URI with `===` in three places."""
    host = echo_host()
    host.on(
        "initialize",
        lambda _p: {
            "protocolVersion": "0.7.0",
            "serverSeq": 1,
            "snapshots": [{"resource": "ahp-root:/", "state": {"agents": []}, "fromSeq": 1}],
        },
    )
    await host.start()
    report = await diagnose(host.transport())
    assert not report.ok
    assert any("byte-exactly" in f.check for f in report.failures)
    await host.stop()


async def test_a_version_we_never_offered_is_caught() -> None:
    host = echo_host(protocol_version="0.3.0")
    await host.start()
    report = await diagnose(host.transport())
    assert any("one we offered" in f.check for f in report.failures)
    await host.stop()


async def test_a_missing_snapshots_array_is_caught() -> None:
    host = echo_host()
    host.on("initialize", lambda _p: {"protocolVersion": "0.7.0", "serverSeq": 1})
    await host.start()
    report = await diagnose(host.transport())
    assert any("snapshots is an array" in f.check for f in report.failures)
    await host.stop()


async def test_a_host_that_accepts_any_method_is_caught() -> None:
    """A host with no MethodNotFound cannot decline anything, and AHP has no
    capability object to decline with instead."""
    host = echo_host()
    host.on("thisMethodDoesNotExist", lambda _p: {"sure": True})
    await host.start()
    report = await diagnose(host.transport())
    assert any("-32601" in f.check for f in report.failures)
    await host.stop()


async def test_an_unimplemented_listsessions_is_a_warning_not_a_failure() -> None:
    """A SHOULD that failed is worth saying and is not a conformance failure."""
    host = echo_host()
    host.on(
        "listSessions",
        lambda _p: (_ for _ in ()).throw(FakeRpcError({"code": -32601, "message": "nope"})),
    )
    await host.start()
    report = await diagnose(host.transport())
    assert report.ok
    assert any(f.check == "listSessions" and not f.ok for f in report.findings)
    await host.stop()


async def test_every_finding_names_where_the_requirement_comes_from() -> None:
    """So a failure is a bug report rather than an opinion."""
    host = echo_host()
    await host.start()
    report = await diagnose(host.transport())
    assert all(f.source for f in report.findings)
    await host.stop()


# ── interop with the sibling host ────────────────────────────────────────────


@pytest.mark.skipif(
    importlib.util.find_spec("agent_host_server") is None,
    reason="the sibling host is not installed in this environment",
)
async def test_a_full_turn_against_the_sibling_python_host() -> None:
    """**Not independent evidence**, and the README says so in those words.

    Both peers share the reducers and were written from the same reading of the
    same spec, so a wrong-but-symmetric reducer passes both suites. What this
    does prove is that our framing, handshake, subscription and reconciliation
    interoperate with a real host implementation rather than only with a fake we
    also wrote.
    """
    from agent_host_protocol.transport import memory_pair
    from agent_host_server.core import Host, LoopbackSingleUserPolicy
    from agent_host_server.provider import EchoProvider

    from agent_host_client.api import Delta, connect

    host = Host(provider=EchoProvider(), policy=LoopbackSingleUserPolicy())
    client_side, host_side = memory_pair()
    served = asyncio.get_running_loop().create_task(host.serve(host_side))

    try:
        async with connect(transport=client_side) as client:
            assert client.protocol_version in {"0.7.0", "0.6.0"}
            assert client.agents(), "the host published no agents"

            # `AgentInfo.provider`, not `.id` -- and the FakeHost's placeholder
            # agent uses `id`, which is exactly why an interop test against a
            # real host is worth having even when it is not independent.
            provider = str(client.agents()[0]["provider"])
            session = await client.create_session(provider=provider, cwd=".")
            chat = await session.chat()
            text: list[str] = []
            async for event in chat.prompt("hello", idle_timeout=10.0):
                if isinstance(event, Delta):
                    text.append(event.text)
            assert "".join(text)
    finally:
        served.cancel()
        with contextlib.suppress(Exception, asyncio.CancelledError):
            await served
