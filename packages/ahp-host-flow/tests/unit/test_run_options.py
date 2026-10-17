"""Run options and the concurrency default."""

import os
from pathlib import Path
from typing import Any

import pytest

from ahp_host_flow.engine import (
    CPU_RESERVE,
    DEFAULT_MAX_CONCURRENCY,
    MAX_AGENTS_PER_RUN,
    MAX_ITEMS_PER_CALL,
    RunOptions,
    default_concurrency,
)


@pytest.mark.parametrize(
    ("cpu_count", "expected"),
    [(0, 1), (1, 1), (2, 1), (3, 1), (4, 2), (10, 8), (18, 16), (64, 16)],
)
def test_run_options_default_concurrency(cpu_count: int, expected: int) -> None:
    assert default_concurrency(cpu_count) == expected


def test_run_options_default_concurrency_unknown_cpus(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(os, "cpu_count", lambda: None)
    assert default_concurrency() == 1


def test_run_options_defaults() -> None:
    options = RunOptions()
    assert options.concurrency is None
    assert options.max_agents == 1000
    assert options.budget_total is None
    assert options.timeout is None
    assert options.workflow_dirs == ()
    assert options.run_root is None
    assert options.state_dir is None
    assert options.run_id is None
    assert options.resume_from is None
    assert RunOptions(concurrency=5).resolved_concurrency() == 5


@pytest.mark.parametrize(
    ("kwargs", "field"),
    [
        ({"concurrency": 0}, "concurrency"),
        ({"concurrency": True}, "concurrency"),
        ({"max_agents": 0}, "max_agents"),
        ({"budget_total": -1}, "budget_total"),
        ({"budget_total": 1.5}, "budget_total"),
        ({"timeout": 0}, "timeout"),
        ({"timeout": -1.0}, "timeout"),
        ({"timeout": float("inf")}, "timeout"),
        ({"timeout": float("nan")}, "timeout"),
        ({"run_id": "../x", "state_dir": Path("s")}, "run_id"),
        ({"run_id": "", "state_dir": Path("s")}, "run_id"),
        ({"run_id": "a" * 65, "state_dir": Path("s")}, "run_id"),
        ({"resume_from": "-x", "state_dir": Path("s")}, "resume_from"),
        ({"run_id": "r1"}, "state_dir"),
        ({"resume_from": "r1"}, "state_dir"),
        ({"run_id": "r1", "resume_from": "r1", "state_dir": Path("s")}, "differ"),
    ],
)
def test_run_options_rejects(kwargs: dict[str, Any], field: str) -> None:
    with pytest.raises(ValueError, match=field):
        RunOptions(**kwargs)


def test_run_options_accepts_run_ids() -> None:
    options = RunOptions(state_dir=Path("s"), run_id="a" * 64, resume_from="r_1-x")
    assert options.run_id == "a" * 64
    assert options.resume_from == "r_1-x"


def test_run_options_constants() -> None:
    assert MAX_AGENTS_PER_RUN == 1000
    assert MAX_ITEMS_PER_CALL == 4096
    assert DEFAULT_MAX_CONCURRENCY == 16
    assert CPU_RESERVE == 2
