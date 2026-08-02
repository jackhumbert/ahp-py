"""ahp-inspector-compatible wire logs, with credentials redacted by construction."""

from __future__ import annotations

from agent_host_client.wirelog.jsonl import WireLog, logged, read_jsonl

__all__ = ["WireLog", "logged", "read_jsonl"]
