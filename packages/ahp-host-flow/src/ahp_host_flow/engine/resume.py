"""Replay cursor and the journal for one run, fresh or resumed (plan §4.2, §8 M2)."""

from __future__ import annotations

import dataclasses
from collections import deque
from collections.abc import Callable, Sequence
from typing import Any

from ahp_host_flow.engine.dispatcher import AgentCall
from ahp_host_flow.engine.journal import (
    AgentRecord,
    JournalError,
    JournalWriter,
    RunHeader,
    WorkflowSnapshot,
    call_key,
    canonical_json,
    load_journal,
    new_run_id,
)
from ahp_host_flow.engine.options import RunOptions
from ahp_host_flow.engine.registry import ResolvedScript


class Replay:
    """Serves an earlier run's calls until the first call it cannot match.

    A call matches by content, not by ordinal: the n-th call issued with a given
    key (prompt, opts, phase, label) gets the n-th record with that key. Ordinals
    are fixed at issue time only for calls issued synchronously; a later
    `pipeline()` stage issues in completion order, and a cached call completes at
    once, so on resume those calls arrive in a different order. Matching by key
    replays them anyway. The first call with no unconsumed record of its key ends
    the cache: it and every call issued after it run live, which keeps Claude
    Code's "longest unchanged prefix" rule for the calls whose order is
    deterministic. A served record is re-stamped with the new call's ordinal, so
    the new journal never has two records with one ordinal.
    """

    def __init__(
        self, records: Sequence[AgentRecord], *, on_log: Callable[[str], None] | None = None
    ) -> None:
        self._by_key: dict[str, deque[AgentRecord]] = {}
        for rec in sorted(records, key=lambda r: r.ordinal):
            self._by_key.setdefault(rec.key, deque()).append(rec)
        self._on_log = on_log
        self._live = False
        self._served: list[int] = []
        self._ended_at: int | None = None

    @property
    def live(self) -> bool:
        return self._live

    @property
    def served(self) -> tuple[int, ...]:
        return tuple(self._served)

    @property
    def ended_at(self) -> int | None:
        return self._ended_at

    def lookup(self, call: AgentCall) -> AgentRecord | None:
        """Call once per call, as it is issued; None means run this call live."""
        if self._live:
            return None
        queue = self._by_key.get(call_key(call))
        if queue:
            self._served.append(call.ordinal)
            return dataclasses.replace(queue.popleft(), ordinal=call.ordinal)
        self._live = True
        self._ended_at = call.ordinal
        if self._on_log is not None:
            self._on_log(
                f"resume: cache ended at agent() #{call.ordinal} (no cached call matches); "
                f"{len(self._served)} calls replayed, this call and later ones run live"
            )
        return None


class JournalRun:
    """The journal writer, replay cursor and script snapshots of one run."""

    def __init__(
        self,
        *,
        run_id: str,
        writer: JournalWriter,
        replay: Replay | None,
        source: str,
        filename: str,
        has_args: bool,
        args: Any,
        options: RunOptions,
        inherited: dict[str, WorkflowSnapshot],
    ) -> None:
        self.run_id = run_id
        self.writer = writer
        self.replay = replay
        self.source = source
        self.filename = filename
        self.has_args = has_args
        self.args = args
        self.options = options
        self._inherited = inherited
        self._recorded: dict[str, WorkflowSnapshot] = {}

    @classmethod
    def open(
        cls,
        options: RunOptions,
        *,
        source: str | None,
        filename: str,
        has_args: bool,
        args: Any,
        on_log: Callable[[str], None] | None = None,
    ) -> JournalRun:
        if options.state_dir is None:
            raise ValueError("journaling needs RunOptions.state_dir")
        replay: Replay | None = None
        inherited: dict[str, WorkflowSnapshot] = {}
        effective = options
        if options.resume_from is not None:
            old = load_journal(options.state_dir, options.resume_from)
            if has_args:
                raise ValueError("args come from the journal on resume")
            if source is None:
                if old.header.source is None:
                    raise JournalError(
                        f"run {options.resume_from!r} has no script snapshot; cannot resume it"
                    )
                source, filename = old.header.source, old.header.filename
            has_args, args = old.header.has_args, old.header.args
            # The old run's semantic options win, so caps and budget replay identically.
            effective = dataclasses.replace(
                options,
                max_agents=old.header.max_agents,
                budget_total=old.header.budget_total,
            )
            if on_log is not None:
                for rec in old.unfinished:
                    if rec.provider is not None:
                        where = f"started on {rec.provider} ({rec.chat or '-'})"
                    else:
                        where = "started"
                    on_log(
                        f"resume: agent() #{rec.ordinal} {where} but never finished; not replayed"
                    )
            replay = Replay(old.agents, on_log=on_log)
            inherited = dict(old.workflows)
        elif source is None:
            raise ValueError("a script source is needed unless resuming")
        run_id = options.run_id or new_run_id()
        writer = JournalWriter.create(
            options.state_dir,
            RunHeader(
                run_id,
                filename,
                source,
                has_args,
                args if has_args else None,
                effective.max_agents,
                effective.budget_total,
                resumed_from=options.resume_from,
            ),
            mask=effective.redact,
        )
        run = cls(
            run_id=run_id,
            writer=writer,
            replay=replay,
            source=source,
            filename=filename,
            has_args=has_args,
            args=args,
            options=effective,
            inherited=inherited,
        )
        # Every child snapshot the old run had goes into the new journal now, not
        # when the child is next reached: a resumed run that stops before then
        # must still be resumable without re-reading a file that may have changed.
        try:
            for snap in inherited.values():
                run.record_workflow(snap.ref, ResolvedScript(snap.source, snap.filename))
        except BaseException:
            writer.close()
            raise
        return run

    def snapshot_for(self, ref: Any) -> ResolvedScript | None:
        k = canonical_json(ref)
        snap = self._recorded.get(k) or self._inherited.get(k)
        return ResolvedScript(snap.source, snap.filename) if snap is not None else None

    def record_workflow(self, ref: Any, resolved: ResolvedScript) -> None:
        k = canonical_json(ref)
        if k in self._recorded:
            return
        snap = WorkflowSnapshot(ref, resolved.filename, resolved.source)
        self._recorded[k] = snap
        self.writer.append_workflow(snap)

    def close(self) -> None:
        self.writer.close()
