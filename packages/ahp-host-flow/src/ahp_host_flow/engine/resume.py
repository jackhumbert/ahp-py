"""Replay cursor and the journal for one run, fresh or resumed (plan §4.2, §8 M2)."""

from __future__ import annotations

import dataclasses
from collections import deque
from collections.abc import Callable, Sequence
from typing import Any, Final

from ahp_host_flow.engine.dispatcher import AgentCall, DispatchHandle
from ahp_host_flow.engine.gates import GateCall, gate_key
from ahp_host_flow.engine.journal import (
    AgentRecord,
    GateRecord,
    JournalError,
    JournalWriter,
    RunHeader,
    StartRecord,
    VerbRecord,
    WorkflowSnapshot,
    args_masked,
    call_key,
    canonical_json,
    load_journal,
    new_run_id,
    snapshot_masked,
    source_masked,
    text_sha,
    value_sha,
)
from ahp_host_flow.engine.options import NO_RESUME_ARGS, RunOptions
from ahp_host_flow.engine.registry import ResolvedScript

# The journal filename of a library run (`ahp_host_flow.run`), which has no script.
LIBRARY_FILENAME: Final = "<library>"


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
        self,
        records: Sequence[AgentRecord],
        *,
        on_log: Callable[[str], None] | None = None,
        prefix_only: bool = True,
        unfinished: Sequence[StartRecord] = (),
    ) -> None:
        # With prefix_only False (continuing an interrupted run) a miss does not end the
        # cache: that call runs live and later calls still look up.
        self._prefix_only = prefix_only
        self._by_key: dict[str, deque[AgentRecord]] = {}
        for rec in sorted(records, key=lambda r: r.ordinal):
            self._by_key.setdefault(rec.key, deque()).append(rec)
        # Cut-off calls that recorded a session (fleet, remote) or a resumable worker chat
        # (in-process), per call key in ordinal order: a continued run hands the handle to the
        # dispatcher so it can reattach.
        self._reattach: dict[str, deque[DispatchHandle]] = {}
        for start in sorted(unfinished, key=lambda r: r.ordinal):
            if start.session is not None or start.worker_chat is not None:
                self._reattach.setdefault(start.key, deque()).append(
                    DispatchHandle(start.provider, start.chat, start.session, start.worker_chat)
                )
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

    def lookup(self, call: AgentCall, key: str | None = None) -> AgentRecord | None:
        """Call once per call, as it is issued; None means run this call live. *key* is the
        call's key when a resume-key hook shaped it, else it is `call_key(call)`.
        """
        if self._live:
            return None
        queue = self._by_key.get(key if key is not None else call_key(call))
        if queue:
            self._served.append(call.ordinal)
            return dataclasses.replace(queue.popleft(), ordinal=call.ordinal)
        if not self._prefix_only:
            return None
        self._live = True
        self._ended_at = call.ordinal
        if self._on_log is not None:
            self._on_log(
                f"resume: cache ended at agent() #{call.ordinal} (no cached call matches); "
                f"{len(self._served)} calls replayed, this call and later ones run live"
            )
        return None

    def reattach(self, call: AgentCall, key: str | None = None) -> DispatchHandle | None:
        """The handle a cut-off earlier call with this key recorded, else None."""
        queue = self._reattach.get(key if key is not None else call_key(call))
        return queue.popleft() if queue else None


class GateReplay:
    """Serves an earlier run's answered gates by key.

    The n-th gate issued with a key gets the n-th answered record with it. Gates
    never end the agent() cache (they do not touch `Replay`) and match by key in a
    prefix resume and with `continue_run` alike: an answered gate whose question and
    options are unchanged replays its answer. Open records are not served, so the
    gate is asked again.
    """

    def __init__(
        self,
        records: Sequence[GateRecord],
        *,
        on_log: Callable[[str], None] | None = None,
    ) -> None:
        self._by_key: dict[str, deque[GateRecord]] = {}
        for rec in sorted(records, key=lambda r: r.ordinal):
            if rec.status == "answered":
                self._by_key.setdefault(rec.key, deque()).append(rec)
        self._on_log = on_log
        self.served: list[int] = []

    def lookup(self, call: GateCall) -> GateRecord | None:
        """Call once per gate, as it is issued; None means ask it live."""
        queue = self._by_key.get(gate_key(call.kind, call.question, call.options))
        if not queue:
            return None
        self.served.append(call.ordinal)
        return dataclasses.replace(queue.popleft(), ordinal=call.ordinal)


class VerbReplay:
    """Serves an earlier run's finished custom verb calls by call id and key.

    A call id is derived from the key and its occurrence, so it is stable across a
    resume; a record is served only when both match, and only once. Like gates, verbs
    never touch `Replay` and replay in a prefix resume and with `continue_run` alike.
    A call id that was only ever `open` was cut off mid-call: it runs again, and the
    caller can ask `was_open` to say so.
    """

    def __init__(self, records: Sequence[VerbRecord]) -> None:
        self._finished: dict[str, VerbRecord] = {}
        self._open: set[str] = set()
        for rec in sorted(records, key=lambda r: r.ordinal):
            if rec.status == "open":
                self._open.add(rec.call_id)
            else:
                self._finished.setdefault(rec.call_id, rec)
        self._open -= self._finished.keys()
        self.served: list[int] = []

    def lookup(self, call_id: str, key: str, ordinal: int) -> VerbRecord | None:
        """Call once per verb call, as it is issued; None means run it live."""
        rec = self._finished.get(call_id)
        if rec is None or rec.key != key:
            return None
        del self._finished[call_id]
        self.served.append(ordinal)
        return dataclasses.replace(rec, ordinal=ordinal)

    def was_open(self, call_id: str) -> bool:
        return call_id in self._open


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
        gate_replay: GateReplay | None = None,
        verb_replay: VerbReplay | None = None,
        masked: dict[str, str] | None = None,
    ) -> None:
        self.run_id = run_id
        self.writer = writer
        self.replay = replay
        self.gate_replay = gate_replay
        self.verb_replay = verb_replay
        self.source = source
        self.filename = filename
        self.has_args = has_args
        self.args = args
        self.options = options
        self._inherited = inherited
        self._recorded: dict[str, WorkflowSnapshot] = {}
        # ref key -> sha256 of the real source, for child snapshots the old journal masked.
        self._masked: dict[str, str] = masked if masked is not None else {}

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
        scriptless: bool = False,
    ) -> JournalRun:
        if options.state_dir is None:
            raise ValueError("journaling needs RunOptions.state_dir")
        if scriptless:
            if source is not None:
                raise ValueError("a scriptless run has no source")
            filename = LIBRARY_FILENAME
        replay: Replay | None = None
        gate_replay: GateReplay | None = None
        verb_replay: VerbReplay | None = None
        inherited: dict[str, WorkflowSnapshot] = {}
        masked: dict[str, WorkflowSnapshot] = {}
        effective = options
        if options.resume_from is not None:
            old = load_journal(options.state_dir, options.resume_from)
            source_sha = old.header.source_sha or ""
            # The library's way to hand a masked journal its real script and args: on the
            # options, filling in only what the caller did not pass.
            if source is None and not scriptless and options.resume_source is not None:
                source = options.resume_source
            if not has_args and options.resume_args is not NO_RESUME_ARGS:
                if args_masked(old.header):
                    has_args, args = True, options.resume_args
                elif value_sha(options.resume_args) != (
                    old.header.args_sha or value_sha(old.header.args)
                ):
                    raise JournalError(
                        f"run {options.resume_from!r}: resume_args differ from the args "
                        "the run started with; cannot resume it"
                    )
            if source is None and not scriptless:
                if old.header.source is None:
                    raise JournalError(
                        f"run {options.resume_from!r} has no script snapshot; cannot resume it"
                    )
                if source_masked(old.header):
                    raise JournalError(
                        f"run {options.resume_from!r}: its journal holds a masked copy of the "
                        f"script; resume it with the script itself (sha256 {source_sha[:12]}), "
                        "or set keep_source for new runs"
                    )
                source, filename = old.header.source, old.header.filename
            elif (
                source is not None and source_masked(old.header) and text_sha(source) != source_sha
            ):
                raise JournalError(
                    f"run {options.resume_from!r}: the script differs from the one its masked "
                    f"journal was written from (sha256 {source_sha[:12]}); cannot resume it"
                )
            if args_masked(old.header):
                if not has_args:
                    raise JournalError(
                        f"run {options.resume_from!r}: its journal holds masked args; "
                        "pass the same args to resume it"
                    )
                if value_sha(args) != old.header.args_sha:
                    raise JournalError(
                        f"run {options.resume_from!r}: the args differ from the ones its "
                        "masked journal was written with; cannot resume it"
                    )
            else:
                if has_args:
                    raise ValueError("args come from the journal on resume")
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
                    if options.continue_run and rec.session is not None:
                        # The engine cannot tell what the provider does with the session
                        # (a fleet call reattaches, a remote one is stopped and run
                        # again), so the dispatcher says so in a line of its own.
                        on_log(
                            f"resume: agent() #{rec.ordinal} {where} in session {rec.session} "
                            "but was cut off by a host restart; its dispatcher reattaches or "
                            "dispatches it again"
                        )
                    elif options.continue_run:
                        on_log(
                            f"resume: agent() #{rec.ordinal} {where} but was cut off by a host "
                            "restart; dispatching it again"
                        )
                    else:
                        on_log(
                            f"resume: agent() #{rec.ordinal} {where} but never finished; "
                            "not replayed"
                        )
            new_mode = options.key_mode_name()
            if old.header.key_mode != new_mode:
                # Keys made under another hook never match: say so once instead of ending
                # the cache at the first call. Gate and verb replays still apply.
                if on_log is not None:
                    on_log(
                        f"resume: run {options.resume_from!r} keyed agent() calls by "
                        f"{old.header.key_mode!r} and this run keys them by {new_mode!r}; "
                        "no cached call can match, so every agent() call runs live"
                    )
            else:
                # A call the hard cap stopped is never replayed as null: it runs live.
                replay = Replay(
                    [a for a in old.agents if a.kind != "budget"],
                    on_log=on_log,
                    prefix_only=not options.continue_run,
                    unfinished=old.unfinished if options.continue_run else (),
                )
            gate_replay = GateReplay(old.gates, on_log=on_log)
            verb_replay = VerbReplay(old.verbs)
            if on_log is not None:
                for gate in old.open_gates:
                    on_log(
                        f"resume: {gate.kind}() gate #{gate.ordinal} was still open; "
                        "it is asked again"
                    )
            for ref_key, snap in old.workflows.items():
                if snapshot_masked(snap):
                    masked[ref_key] = snap
                else:
                    inherited[ref_key] = snap
        elif source is None and not scriptless:
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
                key_mode=effective.key_mode_name(),
                principal=options.principal,
                model=options.model,
            ),
            mask=effective.redact,
            keep_source=effective.keep_source,
        )
        run = cls(
            run_id=run_id,
            writer=writer,
            replay=replay,
            source="" if source is None else source,
            filename=filename,
            has_args=has_args,
            args=args,
            options=effective,
            inherited=inherited,
            gate_replay=gate_replay,
            verb_replay=verb_replay,
            masked={k: s.source_sha or "" for k, s in masked.items()},
        )
        # Every child snapshot the old run had goes into the new journal now, not
        # when the child is next reached: a resumed run that stops before then
        # must still be resumable without re-reading a file that may have changed.
        # A masked one goes in as it is, with its sha, and is checked when reached.
        try:
            for snap in masked.values():
                writer.append_workflow(snap)
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
        expected = self._masked.pop(k, None)
        if expected is not None and text_sha(resolved.source) != expected:
            raise JournalError(
                f"workflow {k}: the journal holds a masked copy and the current script "
                f"differs (sha256 {expected[:12]}); cannot resume it"
            )
        snap = WorkflowSnapshot(ref, resolved.filename, resolved.source)
        self._recorded[k] = snap
        self.writer.append_workflow(snap)

    def close(self) -> None:
        self.writer.close()
