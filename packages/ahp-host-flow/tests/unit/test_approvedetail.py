"""`approve()` detail: option checks, plain gate keys, replay and masking (plan §8 M15)."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from typing import Any

from ahp_host_flow.engine import GateCall, GateRecord, GateRunner
from ahp_host_flow.engine.gates import gate_key, options_error, wants_detail
from ahp_host_flow.engine.journal import canonical_json
from tests.fakes import FakeGates


class _Journal:
    def __init__(self) -> None:
        self.gates: list[GateRecord] = []

    def append_gate(self, record: GateRecord) -> None:
        self.gates.append(record)


def test_approvedetail_options_error() -> None:
    message = "approve() x must be an object of {detail: boolean}"
    assert options_error("approve", {"x": 1}) == message
    assert options_error("approve", {"x": {"detail": "y"}}) == message
    assert options_error("approve", {"x": {"other": 1}}) == message
    assert options_error("approve", {"x": {"detail": True}}) is None
    assert options_error("approve", {"x": {"detail": False}}) is None
    assert options_error("approve", {"diff": "-a\n+b\n"}) is None
    assert options_error("approve", {"diff": 3}) is not None
    assert wants_detail({"x": {"detail": True}})
    assert not wants_detail({"x": {"detail": False}})
    assert not wants_detail({})


def test_approvedetail_plain_gate_key_unchanged() -> None:
    payload = canonical_json({"kind": "approve", "options": {}, "question": "Ship?"})
    assert gate_key("approve", "Ship?", {}) == hashlib.sha256(payload.encode("utf-8")).hexdigest()


async def test_approvedetail_replay_and_masking() -> None:
    journal = _Journal()
    secret = "token=abcdefgh12345678"
    gates = FakeGates(approvals={"Drop?": False}, reasons={"Drop?": f"no, {secret}"})

    def mask(value: Any) -> Any:
        return value.replace(secret, "[redacted]") if isinstance(value, str) else value

    runner = GateRunner(gates, mask=mask, journal=journal)  # type: ignore[arg-type]
    options: Mapping[str, Any] = {"x": {"detail": True}}
    call, cached = runner.issue("approve", "Drop?", options)
    assert cached is None
    got = await runner.run(call)
    expected = {"approved": False, "reason": "no, [redacted]", "answeredBy": None}
    assert got == expected
    assert journal.gates[-1].answer == expected

    replay = GateRunner(None, journal=journal)  # type: ignore[arg-type]
    detail_call = GateCall(0, "approve", "Ship?", options)
    record = GateRecord(0, "k", "approve", "answered", True)
    assert await replay.run(detail_call, record) == {
        "approved": True,
        "reason": None,
        "answeredBy": None,
    }
    plain_call = GateCall(1, "approve", "Ship?", {})
    assert await replay.run(plain_call, GateRecord(1, "k", "approve", "answered", True)) is True
    mapped = GateRecord(2, "k", "approve", "answered", expected)
    assert await replay.run(plain_call, mapped) is False
