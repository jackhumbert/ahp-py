"""The ahp-inspector JSONL wire log."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_host_server.core.wirelog import REDACTED, WireLog, redact


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


class TestRedaction:
    """A wire log is written unencrypted to a path chosen for debugging.
    `authenticate` carries a live bearer token. The two must not meet.
    """

    def test_an_authenticate_token_never_reaches_disk(self, tmp_path: Path) -> None:
        log = WireLog(tmp_path / "agent-host-test.jsonl")
        log.record(
            "c2s",
            {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "authenticate",
                "params": {
                    "channel": "ahp-root://",
                    "resource": "https://api.example.invalid",
                    "token": "gho_a_real_looking_secret",
                    "scopes": ["repo"],
                },
            },
            "c",
        )
        log.close()

        raw = (tmp_path / "agent-host-test.jsonl").read_text()
        assert "gho_a_real_looking_secret" not in raw
        entry = json.loads(raw.strip())
        # The shape survives, so the handshake is still debuggable.
        assert entry["params"]["token"] == REDACTED
        assert entry["params"]["resource"] == "https://api.example.invalid"
        assert entry["params"]["scopes"] == ["repo"]

    def test_the_outbound_frame_is_not_mutated(self, tmp_path: Path) -> None:
        """The mapping handed to `record` is the live frame on its way to the
        socket. Redacting in place would send the marker to the peer."""
        frame = {"method": "authenticate", "params": {"token": "secret"}}
        log = WireLog(tmp_path / "agent-host-test.jsonl")
        log.record("c2s", frame, "c")
        log.close()
        assert frame["params"] == {"token": "secret"}

    @pytest.mark.parametrize(
        "key",
        ["token", "Token", "accessToken", "refresh_token", "apiKey", "Authorization", "tkn"],
    )
    def test_credential_keys_are_matched_case_and_separator_insensitively(self, key: str) -> None:
        assert redact({key: "s"})[key] == REDACTED

    def test_redaction_reaches_arbitrary_depth(self) -> None:
        """A provider adapter may put its own credentials in `_meta`, and a log
        is the wrong place to find out that it did."""
        got = redact({"params": {"_meta": {"nested": [{"token": "s"}, {"safe": 1}]}}})
        assert got["params"]["_meta"]["nested"] == [{"token": REDACTED}, {"safe": 1}]

    def test_non_credential_frames_survive_verbatim(self) -> None:
        frame = {"jsonrpc": "2.0", "method": "ping", "params": {"channel": "ahp-root://"}}
        assert redact(frame) == frame

    def test_the_log_file_is_owner_only(self, tmp_path: Path) -> None:
        """Redaction covers credentials; it cannot redact the conversation."""
        path = tmp_path / "agent-host-test.jsonl"
        log = WireLog(path)
        log.close()
        assert path.stat().st_mode & 0o077 == 0, "group/other can read the transcript"
