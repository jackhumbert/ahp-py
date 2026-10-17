"""Run options for one workflow run: caps, budget, timeout and workflow lookup."""

from __future__ import annotations

import math
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Final

# plan §3 caps
DEFAULT_MAX_CONCURRENCY: Final = 16
# plan §3 caps
CPU_RESERVE: Final = 2
# plan §3 caps
MAX_AGENTS_PER_RUN: Final = 1000
# plan §3 caps
MAX_ITEMS_PER_CALL: Final = 4096


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

    def resolved_concurrency(self) -> int:
        return self.concurrency if self.concurrency is not None else default_concurrency()
