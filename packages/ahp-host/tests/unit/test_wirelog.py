"""The ahp-inspector JSONL wire log."""

from __future__ import annotations

import json
from pathlib import Path

from agent_host_server.core.wirelog import WireLog


def test_writes_the_verbatim_message_plus_a_sidecar(tmp_path: Path) -> None:
    log = WireLog(tmp_path / "agent-host-test.jsonl")
    log.record("c2s", {"jsonrpc": "2.0", "id": 1, "method": "ping", "params": {}}, "client-1")
    log.close()

    entry = json.loads((tmp_path / "agent-host-test.jsonl").read_text().strip())
    # The message survives verbatim -- the reader parses it as a JSON-RPC frame.
    assert entry["jsonrpc"] == "2.0"
    assert entry["method"] == "ping"
    meta = entry["_ahpLog"]
    assert meta["dir"] == "c2s"
    assert meta["connectionId"] == "client-1"
    assert meta["ts"].endswith("Z"), "the reader parses ts with Date.parse"


def test_direction_is_always_explicit(tmp_path: Path) -> None:
    """Without `dir`, the reader's fallback inference mislabels every modern
    server-originated notification as client-to-server."""
    log = WireLog(tmp_path / "agent-host-test.jsonl")
    log.record("s2c", {"jsonrpc": "2.0", "method": "root/sessionAdded", "params": {}}, "c")
    log.close()
    entry = json.loads((tmp_path / "agent-host-test.jsonl").read_text().strip())
    assert entry["_ahpLog"]["dir"] == "s2c"


def test_an_unserialisable_frame_does_not_raise(tmp_path: Path) -> None:
    """Logging is best-effort: it must never be able to break a connection."""
    log = WireLog(tmp_path / "agent-host-test.jsonl")
    log.record("s2c", {"bad": {1, 2, 3}}, "c")  # a set is not JSON
    log.record("s2c", {"jsonrpc": "2.0", "method": "ping"}, "c")
    log.close()
    lines = (tmp_path / "agent-host-test.jsonl").read_text().splitlines()
    assert len(lines) == 1, "the good frame should still be written"


def test_records_after_close_are_dropped_quietly(tmp_path: Path) -> None:
    log = WireLog(tmp_path / "agent-host-test.jsonl")
    log.close()
    log.record("c2s", {"jsonrpc": "2.0", "method": "ping"}, "c")
