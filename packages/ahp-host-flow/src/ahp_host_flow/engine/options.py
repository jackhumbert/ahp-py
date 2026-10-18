"""Run options for one workflow run: caps, budget, timeout and workflow lookup."""

from __future__ import annotations

import math
import os
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

from ahp_host_flow.engine.registry import ClientWorkflow, WorkflowDir

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
    workflow_dirs: tuple[Path | WorkflowDir, ...] = ()
    # Workflows a client published, as plain data; `.js` only (plan §4.3).
    client_workflows: tuple[ClientWorkflow, ...] = ()
    run_root: Path | None = None
    # Directories `.py` workflows may load from, by name or {scriptPath}. None means a trusted
    # caller (the CLI and the library) and allows any `.py`; every path that runs a workflow on
    # behalf of someone else, the provider above all, must set it (a tuple, () for none).
    python_dirs: tuple[Path, ...] | None = None
    # Journals go to <state_dir>/runs/<run_id>/journal.jsonl; None means no journal.
    state_dir: Path | None = None
    # Id of the new run; None means generated.
    run_id: str | None = None
    # Id of an earlier run whose journal serves cached agent() calls.
    resume_from: str | None = None
    # Continue a run the host stopped, from its own journal snapshot: every finished call
    # replays by key, not only the longest unchanged prefix.
    continue_run: bool = False
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
        if not isinstance(self.continue_run, bool):
            raise ValueError("continue_run must be a bool")
        if self.continue_run and self.resume_from is None:
            raise ValueError("continue_run needs resume_from")
        if self.run_id is not None and self.run_id == self.resume_from:
            raise ValueError("run_id must differ from resume_from")
        if self.python_dirs is not None and not (
            isinstance(self.python_dirs, tuple)
            and all(isinstance(d, Path) for d in self.python_dirs)
        ):
            raise ValueError("python_dirs must be a tuple of paths or None")
        if not (
            isinstance(self.workflow_dirs, tuple)
            and all(isinstance(d, Path | WorkflowDir) for d in self.workflow_dirs)
        ):
            raise ValueError("workflow_dirs must be a tuple of paths or WorkflowDir")
        if not (
            isinstance(self.client_workflows, tuple)
            and all(isinstance(c, ClientWorkflow) for c in self.client_workflows)
        ):
            raise ValueError("client_workflows must be a tuple of ClientWorkflow")
        if self.redact is not None and not callable(self.redact):
            raise ValueError("redact must be callable")

    def resolved_concurrency(self) -> int:
        return self.concurrency if self.concurrency is not None else default_concurrency()
