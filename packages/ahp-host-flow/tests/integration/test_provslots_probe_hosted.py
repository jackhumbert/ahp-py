"""`concurrency = "probe"` end to end through flow.toml (M16 item 3).

The `ProviderSlotsRegistry` the `FlowProvider` builds runs `FlowProvider._probe_provider` before
the provider's first call; its answer is the semaphore every later call of the provider holds.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.flow_hosting import boot_flow, final_json
from tests.hosting import run_turn
from tests.limits_fakes import ProbedProvider

TOML = (
    '[providers.probed]\nload = "tests.limits_fakes:ProbedProvider"\nconcurrency = "probe"\n'
    '[routing]\ndefault = "probed"\n'
    # The run's own slots never serialise the three calls.
    "[limits]\nconcurrency = 4\n"
)
WORKFLOW = (
    "meta = {'name': 'pair', 'description': 'd'}\n"
    "\n"
    "async def main(args):\n"
    "    return await parallel([lambda: agent('a'), lambda: agent('b'), lambda: agent('c')])\n"
)


async def _run(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> list[str]:
    user = tmp_path / "user"
    user.mkdir()
    (user / "pair.py").write_text(WORKFLOW, encoding="utf-8")
    fx = await boot_flow(tmp_path, str(tmp_path / "no-node"), TOML)
    try:
        with caplog.at_level("INFO", logger="ahp_host_flow"):
            turn = await run_turn(fx.wire, fx.chat, "t1", "/pair")
            # A second run of the same provider reuses the probed limit: no second probe.
            again = await run_turn(fx.wire, fx.chat, "t2", "/pair")
    finally:
        await fx.shut()
    assert final_json(turn) == final_json(again) == ["ok", "ok", "ok"]
    return [r.getMessage() for r in caplog.records if "probe" in r.getMessage()]


async def test_provslots_probe_hosted_single_slot(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    ProbedProvider.reset(single=True)
    lines = await _run(tmp_path, caplog)
    assert lines == ["provider probed: probe found a single slot; concurrency 1"]
    assert ProbedProvider.probes == 2
    # The calls themselves would run at once: only the probed semaphore serialised them.
    assert ProbedProvider.max_in_flight == 1


async def test_provslots_probe_hosted_parallel(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    ProbedProvider.reset(single=False)
    lines = await _run(tmp_path, caplog)
    assert lines == ["provider probed: probe found parallel turns; no limit"]
    assert ProbedProvider.probes == 2
    assert ProbedProvider.max_in_flight == 3
