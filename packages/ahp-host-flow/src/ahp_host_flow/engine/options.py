"""Run options for one workflow run: caps, budget, timeout and workflow lookup."""

from __future__ import annotations

import math
import os
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from ahp_host_flow.engine.registry import ClientWorkflow, WorkflowDir
from ahp_host_flow.engine.verbnames import verb_name_error

if TYPE_CHECKING:
    from ahp_host_flow.engine.dispatcher import AgentCall
    from ahp_host_flow.engine.quota import RunQuota
    from ahp_host_flow.engine.verbs import VerbHandler

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
    # More counters (M16), none journaled: a resume uses the resuming run's values.
    input_budget: int | None = None
    cost_budget: float | None = None
    # Whether cache-read tokens count toward input_budget.
    count_cache: bool = True
    # Seconds of wall time; unlike `timeout` it kills nothing: later steps stop in-flight calls
    # and refuse new ones.
    wall_budget: float | None = None
    # Output tokens reserved per live call at admission while budget_total is set, so parallel
    # calls cannot overshoot it. None (the default) means the mean output tokens of this run's
    # finished live calls, else 0; 0 reserves nothing.
    reserve: int | None = None
    # The principal's quota (M16): admission refuses at its limits, live usage stops calls at them.
    quota: RunQuota | None = field(default=None, compare=False, repr=False)
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
    # Custom verbs (plan M12): name -> handler, run by the host with no model.
    verbs: Mapping[str, VerbHandler] = field(default_factory=dict, compare=False, repr=False)
    # Resume-key hook (plan M12): the value it returns for a call joins that call's key, so a
    # cached call replays only for the same value. None keys calls by content alone.
    key_extra: Callable[[AgentCall], Any] | None = field(default=None, compare=False, repr=False)
    # Names the hook in the journal header; a resume under another name replays no agent() call.
    key_mode: str | None = None

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
        if self.input_budget is not None and not (
            _is_int(self.input_budget) and self.input_budget >= 0
        ):
            raise ValueError("input_budget must be an integer >= 0")
        if self.cost_budget is not None and not (
            isinstance(self.cost_budget, int | float)
            and not isinstance(self.cost_budget, bool)
            and math.isfinite(self.cost_budget)
            and self.cost_budget >= 0
        ):
            raise ValueError("cost_budget must be a number >= 0")
        if not isinstance(self.count_cache, bool):
            raise ValueError("count_cache must be a bool")
        if self.wall_budget is not None and not (
            isinstance(self.wall_budget, int | float)
            and not isinstance(self.wall_budget, bool)
            and math.isfinite(self.wall_budget)
            and self.wall_budget > 0
        ):
            raise ValueError("wall_budget must be a number > 0")
        if self.reserve is not None and not (_is_int(self.reserve) and self.reserve >= 0):
            raise ValueError("reserve must be an integer >= 0 or None")
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
        if not isinstance(self.verbs, Mapping):
            raise ValueError("verbs must be a mapping of verb name to handler")
        for name, handler in self.verbs.items():
            reason = verb_name_error(name)
            if reason is not None:
                raise ValueError(f"verbs: {reason}")
            if not callable(handler):
                raise ValueError(f"verbs.{name} must be callable")

        if self.key_extra is not None and not callable(self.key_extra):
            raise ValueError("key_extra must be callable")
        if self.key_mode is not None and not (isinstance(self.key_mode, str) and self.key_mode):
            raise ValueError("key_mode must be a non-empty string")
        if self.key_extra is None and self.key_mode not in (None, "default"):
            raise ValueError("key_mode needs key_extra")
        if self.key_extra is not None and self.key_mode == "default":
            raise ValueError("key_mode 'default' means no key_extra")

    def key_mode_name(self) -> str:
        """The name the journal header records for how this run keys its agent() calls."""
        fn = self.key_extra
        if fn is None:
            return "default"
        if self.key_mode is not None:
            return self.key_mode
        module = getattr(fn, "__module__", "?")
        return f"{module}:{getattr(fn, '__qualname__', type(fn).__qualname__)}"

    def resolved_concurrency(self) -> int:
        return self.concurrency if self.concurrency is not None else default_concurrency()
