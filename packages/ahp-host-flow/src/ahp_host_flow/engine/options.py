"""Run options for one workflow run: caps, budget, timeout and workflow lookup."""

from __future__ import annotations

import math
import os
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

# plan §3 caps
DEFAULT_MAX_CONCURRENCY: Final = 16
# plan §3 caps
CPU_RESERVE: Final = 2
# plan §3 caps
MAX_AGENTS_PER_RUN: Final = 1000
# plan §3 caps
MAX_ITEMS_PER_CALL: Final = 4096
# A run id names a directory under the state dir: no dots, slashes or leading dash.
RUN_ID: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")


def default_concurrency(cpu_count: int | None = None) -> int:
    """`min(16, CPUs - 2)`, at least 1; an unknown CPU count counts as 1."""
    cpus = os.cpu_count() if cpu_count is None else cpu_count
    return max(1, min(DEFAULT_MAX_CONCURRENCY, (cpus or 1) - CPU_RESERVE))


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


@dataclass(frozen=True)
class RunOptions:
    """Per-run limits and lookup roots; `None` means the documented default."""

    concurrency: int | None = None
    max_agents: int = MAX_AGENTS_PER_RUN
    budget_total: int | None = None
    timeout: float | None = None
    workflow_dirs: tuple[Path, ...] = ()
    run_root: Path | None = None
    # Journals go to <state_dir>/runs/<run_id>/journal.jsonl; None means no journal.
    state_dir: Path | None = None
    # Id of the new run; None means generated.
    run_id: str | None = None
    # Id of an earlier run whose journal serves cached agent() calls.
    resume_from: str | None = None
    # Masks every value the journal stores (plan §4.9); None stores values as they are.
    redact: Callable[[Any], Any] | None = field(default=None, compare=False, repr=False)

    def __post_init__(self) -> None:
        if self.concurrency is not None and not (
            _is_int(self.concurrency) and self.concurrency >= 1
        ):
            raise ValueError("concurrency must be an integer >= 1")
        if not (_is_int(self.max_agents) and self.max_agents >= 1):
            raise ValueError("max_agents must be an integer >= 1")
        if self.budget_total is not None and not (
            _is_int(self.budget_total) and self.budget_total >= 0
        ):
            raise ValueError("budget_total must be an integer >= 0")
        if self.timeout is not None and not (
            isinstance(self.timeout, int | float)
            and not isinstance(self.timeout, bool)
            and math.isfinite(self.timeout)
            and self.timeout > 0
        ):
            raise ValueError("timeout must be a number > 0")
        for name in ("run_id", "resume_from"):
            value = getattr(self, name)
            if value is not None and not (isinstance(value, str) and RUN_ID.fullmatch(value)):
                raise ValueError(f"{name} must match [A-Za-z0-9][A-Za-z0-9_-]{{0,63}}")
        if (self.run_id is not None or self.resume_from is not None) and self.state_dir is None:
            raise ValueError("run_id and resume_from need state_dir")
        if self.run_id is not None and self.run_id == self.resume_from:
            raise ValueError("run_id must differ from resume_from")
        if self.redact is not None and not callable(self.redact):
            raise ValueError("redact must be callable")

    def resolved_concurrency(self) -> int:
        return self.concurrency if self.concurrency is not None else default_concurrency()
