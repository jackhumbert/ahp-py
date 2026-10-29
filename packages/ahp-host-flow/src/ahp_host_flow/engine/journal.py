"""Run journal: one append-only JSONL file per run (plan §4.2).

The header record holds the script snapshot; each dispatched `agent()` call adds a
start record and, when it completes, an agent record; each `ask()`/`approve()` gate
adds a gate record (open when asked, answered when it has an answer), and each custom
verb call adds a verb record (open when called, done or error when it finished). A torn
final line (a killed process) is ignored on read. Record types other than
run/workflow/agent/started/gate/verb are ignored on load, on purpose, so a later record
kind stays readable by this code; keep it so. An error agent record may carry `kind`
(an ErrorKind); readers before M16 ignore it, so JOURNAL_VERSION stays 1.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any, Final, Literal, TypeGuard

from ahp_host_flow.engine.dispatcher import AgentCall
from ahp_host_flow.engine.options import RUN_ID
from ahp_host_flow.engine.redact import MASK

JOURNAL_VERSION: Final = 1
RUNS_DIR: Final = "runs"
JOURNAL_FILE: Final = "journal.jsonl"
#: Beside the journal: a lock the run's writer holds for as long as it is open.
#: `redact_agent_record` refuses to rewrite a journal while the lock is held.
LOCK_FILE: Final = "journal.lock"

JournalStatus = Literal["ok", "null", "error"]
_STATUSES: Final = frozenset({"ok", "null", "error"})

GateStatus = Literal["open", "answered"]
_GATE_STATUSES: Final = frozenset({"open", "answered"})
_GATE_RECORD_KINDS: Final = frozenset({"ask", "approve"})

VerbStatus = Literal["open", "done", "error"]
_VERB_STATUSES: Final = frozenset({"open", "done", "error"})


class JournalError(Exception):
    """A journal that cannot be written or read; the message is user-facing."""


class JournalBusyError(JournalError):
    """A journal a live writer holds; it must not be rewritten under it."""


def _try_lock(handle: IO[bytes]) -> bool:
    """Take an exclusive, non-blocking advisory lock on *handle*; `False` if held.

    The operating system drops it when the holder exits, however it exits, so a
    crashed run never leaves a stale lock behind.
    """
    try:
        if sys.platform == "win32":
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    return True


def _unlock(handle: IO[bytes]) -> None:
    try:
        if sys.platform == "win32":
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass


def canonical_json(value: Any) -> str:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )


#: `call_key`'s default: no resume-key hook, so the payload has no `extra` field and the key
#: is the one every journal before the hook holds.
_NO_EXTRA: Final = object()


def call_key(call: AgentCall, *, extra: object = _NO_EXTRA) -> str:
    # One JSON object, not string concatenation, so field boundaries are unambiguous.
    payload: dict[str, Any] = {
        "label": call.label,
        "opts": dict(call.opts),
        "phase": call.phase,
        "prompt": call.prompt,
    }
    if extra is not _NO_EXTRA:
        payload["extra"] = extra
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def text_sha(text: str) -> str:
    """sha256 of *text*: how a masked journal vouches for the script it was written from."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def value_sha(value: Any) -> str:
    return text_sha(canonical_json(value))


def verb_key(name: str, args: Sequence[Any]) -> str:
    """The replay key of a custom verb call: sha256 of the canonical JSON of name and args."""
    payload = {"args": list(args), "name": name}
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def verb_call_id(key: str, occurrence: int) -> str:
    """The call id of the *occurrence*-th use of *key*; stable across a resume because it
    never involves the ordinal."""
    return f"verb-{key[:16]}-{occurrence}"


def new_run_id() -> str:
    return uuid.uuid4().hex


def run_dir(state_dir: Path, run_id: str) -> Path:
    if not RUN_ID.fullmatch(run_id):
        raise ValueError(f"invalid run id: {run_id!r}")
    return state_dir / RUNS_DIR / run_id


@dataclass(frozen=True)
class RunHeader:
    run_id: str
    filename: str
    # None when the snapshot is missing; resume decides whether that is fatal.
    source: str | None
    has_args: bool
    args: Any
    max_agents: int
    budget_total: int | None
    resumed_from: str | None = None
    # How the run keyed its agent() calls: "default", or the resume-key hook's mode name.
    key_mode: str = "default"
    # sha256 of the real script and of the canonical JSON of the real args; None in a
    # journal written before M17. When the stored text no longer hashes to it, it is masked.
    source_sha: str | None = None
    args_sha: str | None = None
    # Who the run was started for (the turn's principal); None when none was recorded, which
    # is also what a journal written before this field reads as. Never part of a replay key.
    principal: str | None = None
    # The model the run was asked for; None when none was recorded (or an older journal).
    model: str | None = None


@dataclass(frozen=True)
class WorkflowSnapshot:
    # The ref as the script wrote it: a name, or {"scriptPath": ...}.
    ref: Any
    filename: str
    source: str
    # sha256 of the real source, set when `source` may be masked (read side).
    source_sha: str | None = None

    @property
    def ref_key(self) -> str:
        return canonical_json(self.ref)


@dataclass(frozen=True)
class AgentRecord:
    ordinal: int
    key: str
    status: JournalStatus
    value: Any
    output_tokens: int = 0
    error: str | None = None
    # Set once the value was redacted after publishing: {"reason", "redactedAt"}.
    redacted: Mapping[str, str] | None = None
    # Error records only: the ErrorKind of the failure (any string is kept on load).
    kind: str | None = None


@dataclass(frozen=True)
class StartRecord:
    ordinal: int
    key: str
    # The dispatch handle: provider name and worker chat URI, both None when unknown.
    provider: str | None = None
    chat: str | None = None
    # The node session (fleet) or remote session the call ran on, None when unknown.
    session: str | None = None
    # The in-process worker chat a restart can resume the call in (`DispatchHandle.worker_chat`).
    worker_chat: str | None = None


@dataclass(frozen=True)
class GateRecord:
    """One `ask()`/`approve()` gate: `open` when asked, `answered` once it has an answer."""

    ordinal: int
    key: str
    kind: str
    status: GateStatus
    # The answer the script got, masked; None while open. For ask that is the user's
    # answer; for approve a bool, or with `opts.x.detail` the `{approved, reason,
    # answeredBy}` object (`engine.gates.wants_detail`).
    answer: Any = None


@dataclass(frozen=True)
class VerbRecord:
    """One custom verb call: `open` when called, `done` or `error` once it finished."""

    ordinal: int
    key: str
    name: str
    call_id: str
    status: VerbStatus
    # The verb's return value (masked); None unless done.
    value: Any = None
    error: str | None = None


@dataclass(frozen=True)
class LoadedJournal:
    header: RunHeader
    workflows: Mapping[str, WorkflowSnapshot]
    agents: tuple[AgentRecord, ...]
    started: tuple[StartRecord, ...] = ()
    gates: tuple[GateRecord, ...] = ()
    verbs: tuple[VerbRecord, ...] = ()

    @property
    def unfinished(self) -> tuple[StartRecord, ...]:
        """Started calls with no agent record: in flight when the run was killed."""
        done = {a.ordinal for a in self.agents}
        return tuple(s for s in self.started if s.ordinal not in done)

    @property
    def open_gates(self) -> tuple[GateRecord, ...]:
        """Gates asked but never answered: open when the run was killed."""
        return tuple(g for g in self.gates if g.status == "open")

    @property
    def open_verbs(self) -> tuple[VerbRecord, ...]:
        """Verb calls never finished: open when the run was killed."""
        return tuple(v for v in self.verbs if v.status == "open")


def source_masked(header: RunHeader) -> bool:
    """True when the header holds a masked copy of the script, not the script itself."""
    return (
        header.source_sha is not None
        and header.source is not None
        and text_sha(header.source) != header.source_sha
    )


def args_masked(header: RunHeader) -> bool:
    """True when the header holds masked args, not the args the run was given."""
    return (
        header.has_args
        and header.args_sha is not None
        and value_sha(header.args) != header.args_sha
    )


def snapshot_masked(snap: WorkflowSnapshot) -> bool:
    """True when a child workflow snapshot holds a masked copy of the source."""
    return snap.source_sha is not None and text_sha(snap.source) != snap.source_sha


def _line(record: dict[str, Any]) -> str:
    return json.dumps(record, separators=(",", ":"), ensure_ascii=True, allow_nan=False) + "\n"


class JournalWriter:
    def __init__(
        self,
        path: Path,
        run_id: str,
        file: IO[str],
        mask: Callable[[Any], Any] | None = None,
        lock: IO[bytes] | None = None,
        keep_source: bool = False,
    ) -> None:
        self._keep_source = keep_source
        self._path = path
        self._run_id = run_id
        self._file: IO[str] | None = file
        self._mask = mask
        self._lock = lock

    @classmethod
    def create(
        cls,
        state_dir: Path,
        header: RunHeader,
        *,
        mask: Callable[[Any], Any] | None = None,
        keep_source: bool = False,
    ) -> JournalWriter:
        d = run_dir(state_dir, header.run_id)
        try:
            d.mkdir(parents=True, exist_ok=False)
        except FileExistsError:
            raise JournalError(f"run {header.run_id!r} already exists under {state_dir}") from None
        path = d / JOURNAL_FILE
        # newline="\n": no \r on Windows, so the file is byte-identical everywhere.
        file = open(path, "x", encoding="utf-8", newline="\n")  # noqa: SIM115
        # Held until `close`: how `redact_agent_record` (possibly in another
        # process) tells a live writer from a finished or crashed run.
        lock = open(d / LOCK_FILE, "a+b")  # noqa: SIM115
        if not _try_lock(lock):
            lock.close()
            file.close()
            path.unlink(missing_ok=True)
            raise JournalError(f"run {header.run_id!r} is already being written")
        writer = cls(path, header.run_id, file, mask, lock, keep_source)
        try:
            source: str | None = header.source
            args = header.args if header.has_args else None
            if mask is not None and not keep_source:
                if source is not None:
                    source = str(mask(source))
                if header.has_args:
                    args = mask(args)
            record: dict[str, Any] = {
                "type": "run",
                "version": JOURNAL_VERSION,
                "runId": header.run_id,
                "resumedFrom": header.resumed_from,
                "filename": header.filename,
                "source": source,
                "hasArgs": header.has_args,
                "args": args,
                "options": {
                    "maxAgents": header.max_agents,
                    "budgetTotal": header.budget_total,
                    "keyMode": header.key_mode,
                },
            }
            # Always the hash of the real text, so a resume can check what it is given.
            if header.source is not None:
                record["sourceSha"] = text_sha(header.source)
            if header.has_args:
                record["argsSha"] = value_sha(header.args)
            if header.principal is not None:
                record["principal"] = header.principal
            if header.model is not None:
                record["model"] = header.model
            writer._write(record)
        except BaseException:
            # A header that cannot be written (args with NaN, say) leaves no run
            # behind, so the same run id can be used again.
            writer.close()
            path.unlink(missing_ok=True)
            (d / LOCK_FILE).unlink(missing_ok=True)
            d.rmdir()
            raise
        return writer

    @property
    def path(self) -> Path:
        return self._path

    @property
    def run_id(self) -> str:
        return self._run_id

    def append_agent(self, record: AgentRecord) -> None:
        mask = self._mask
        wire: dict[str, Any] = {
            "type": "agent",
            "ordinal": record.ordinal,
            "key": record.key,
            "status": record.status,
            "value": mask(record.value) if mask is not None else record.value,
            "outputTokens": record.output_tokens,
        }
        if record.status == "error":
            error = record.error if record.error is not None else ""
            wire["error"] = mask(error) if mask is not None else error
            if record.kind is not None:
                wire["kind"] = record.kind
        if record.redacted is not None:
            wire["redacted"] = dict(record.redacted)
        self._write(wire)

    def append_started(self, record: StartRecord) -> None:
        handle: dict[str, Any] | None = None
        if (
            record.provider is not None
            or record.chat is not None
            or record.session is not None
            or record.worker_chat is not None
        ):
            handle = {"provider": record.provider, "chat": record.chat}
            if record.session is not None:
                handle["session"] = record.session
            if record.worker_chat is not None:
                handle["workerChat"] = record.worker_chat
        self._write(
            {"type": "started", "ordinal": record.ordinal, "key": record.key, "handle": handle}
        )

    def append_gate(self, record: GateRecord) -> None:
        mask = self._mask
        answer = record.answer if record.status == "answered" else None
        if mask is not None and record.status == "answered":
            answer = mask(answer)
        self._write(
            {
                "type": "gate",
                "ordinal": record.ordinal,
                "key": record.key,
                "kind": record.kind,
                "status": record.status,
                "answer": answer,
            }
        )

    def append_verb(self, record: VerbRecord) -> None:
        mask = self._mask
        value = record.value if record.status == "done" else None
        if mask is not None and record.status == "done":
            value = mask(value)
        error: str | None = None
        if record.status == "error":
            error = record.error or ""
            if mask is not None:
                error = mask(error)
        self._write(
            {
                "type": "verb",
                "ordinal": record.ordinal,
                "key": record.key,
                "name": record.name,
                "callId": record.call_id,
                "status": record.status,
                "value": value,
                "error": error,
            }
        )

    def append_workflow(self, snapshot: WorkflowSnapshot) -> None:
        mask = self._mask
        source = snapshot.source
        if mask is not None and not self._keep_source:
            source = str(mask(source))
        sha = snapshot.source_sha if snapshot.source_sha is not None else text_sha(snapshot.source)
        self._write(
            {
                "type": "workflow",
                "ref": snapshot.ref,
                "filename": snapshot.filename,
                "source": source,
                "sourceSha": sha,
            }
        )

    def close(self) -> None:
        file, self._file = self._file, None
        if file is not None:
            file.close()
        lock, self._lock = self._lock, None
        if lock is not None:
            _unlock(lock)
            lock.close()

    def _write(self, record: dict[str, Any]) -> None:
        # A dispatch can finish after the run ended; its record is dropped.
        if self._file is None:
            return
        # One write() then flush: a killed process loses at most the line in flight.
        self._file.write(_line(record))
        self._file.flush()


def _is_count(value: Any) -> TypeGuard[int]:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _redacted_marker(raw: Any) -> dict[str, str] | Literal[False] | None:
    """The `redacted` field of an agent record; False when it is malformed."""
    if raw is None:
        return None
    if (
        isinstance(raw, dict)
        and isinstance(raw.get("reason"), str)
        and isinstance(raw.get("redactedAt"), str)
    ):
        return {"reason": raw["reason"], "redactedAt": raw["redactedAt"]}
    return False


def load_journal(state_dir: Path, run_id: str) -> LoadedJournal:
    """Read a run's journal. Record types other than run/workflow/agent/started/gate/verb
    are ignored, on purpose, so a later record kind stays readable; keep it so.
    """
    path = run_dir(state_dir, run_id) / JOURNAL_FILE
    if not path.is_file():
        raise JournalError(f"no journal for run {run_id!r} under {state_dir}")
    text = path.read_bytes().decode("utf-8")
    # The last part is "" after a final newline, else a torn line; either way dropped.
    parts = text.split("\n")[:-1]
    header: RunHeader | None = None
    workflows: dict[str, WorkflowSnapshot] = {}
    agents: dict[int, AgentRecord] = {}
    started: dict[int, StartRecord] = {}
    gates: dict[int, GateRecord] = {}
    verbs: dict[int, VerbRecord] = {}
    for n, part in enumerate(parts, start=1):
        try:
            rec = json.loads(part)
        except ValueError:
            rec = None
        if not isinstance(rec, dict) or not isinstance(rec.get("type"), str):
            raise JournalError(f"journal line {n} is not a valid record")
        kind = rec["type"]
        if n == 1:
            if kind != "run":
                raise JournalError(f"journal for run {run_id!r} does not start with a run header")
            if rec.get("version") != JOURNAL_VERSION:
                raise JournalError(f"unsupported journal version: {rec.get('version')}")
            options = rec.get("options")
            options = options if isinstance(options, dict) else {}
            source = rec.get("source")
            key_mode = options.get("keyMode")
            source_sha, args_sha = rec.get("sourceSha"), rec.get("argsSha")
            header = RunHeader(
                run_id=str(rec.get("runId", run_id)),
                filename=str(rec.get("filename", "")),
                source=source if isinstance(source, str) else None,
                has_args=bool(rec.get("hasArgs")),
                args=rec.get("args"),
                max_agents=options.get("maxAgents", 0),
                budget_total=options.get("budgetTotal"),
                resumed_from=rec.get("resumedFrom"),
                key_mode=key_mode if isinstance(key_mode, str) else "default",
                source_sha=source_sha if isinstance(source_sha, str) else None,
                args_sha=args_sha if isinstance(args_sha, str) else None,
                principal=rec["principal"] if isinstance(rec.get("principal"), str) else None,
                model=rec["model"] if isinstance(rec.get("model"), str) else None,
            )
        elif kind == "workflow":
            filename, source = rec.get("filename"), rec.get("source")
            if not isinstance(filename, str) or not isinstance(source, str):
                raise JournalError(f"journal line {n}: bad workflow record")
            sha = rec.get("sourceSha")
            snap = WorkflowSnapshot(
                ref=rec.get("ref"),
                filename=filename,
                source=source,
                source_sha=sha if isinstance(sha, str) else None,
            )
            workflows.setdefault(snap.ref_key, snap)
        elif kind == "agent":
            ordinal, key, status = rec.get("ordinal"), rec.get("key"), rec.get("status")
            tokens, error = rec.get("outputTokens"), rec.get("error")
            err_kind = rec.get("kind")
            if (
                not _is_count(ordinal)
                or not isinstance(key, str)
                or status not in _STATUSES
                or not _is_count(tokens)
                or (error is not None and not isinstance(error, str))
                or (err_kind is not None and not isinstance(err_kind, str))
            ):
                raise JournalError(f"journal line {n}: bad agent record")
            # Written by redact_agent_record; readers before M9 ignore the key, so
            # JOURNAL_VERSION stays 1.
            redacted = _redacted_marker(rec.get("redacted"))
            if redacted is False:
                raise JournalError(f"journal line {n}: bad agent record")
            agents.setdefault(
                ordinal,
                AgentRecord(
                    ordinal=ordinal,
                    key=key,
                    status=status,
                    value=rec.get("value"),
                    output_tokens=tokens,
                    error=error,
                    redacted=redacted or None,
                    kind=err_kind,
                ),
            )
        elif kind == "started":
            ordinal, key, handle = rec.get("ordinal"), rec.get("key"), rec.get("handle")
            provider = chat = session = worker_chat = None
            if isinstance(handle, dict):
                provider, chat = handle.get("provider"), handle.get("chat")
                session, worker_chat = handle.get("session"), handle.get("workerChat")
            if (
                not _is_count(ordinal)
                or not isinstance(key, str)
                or (handle is not None and not isinstance(handle, dict))
                or (provider is not None and not isinstance(provider, str))
                or (chat is not None and not isinstance(chat, str))
                or (session is not None and not isinstance(session, str))
                or (worker_chat is not None and not isinstance(worker_chat, str))
            ):
                raise JournalError(f"journal line {n}: bad started record")
            # A fallback repeats the record under the provider it moved to; the last wins.
            started[ordinal] = StartRecord(ordinal, key, provider, chat, session, worker_chat)
        elif kind == "gate":
            ordinal, key = rec.get("ordinal"), rec.get("key")
            gate_kind, status = rec.get("kind"), rec.get("status")
            if (
                not _is_count(ordinal)
                or not isinstance(key, str)
                or gate_kind not in _GATE_RECORD_KINDS
                or status not in _GATE_STATUSES
            ):
                raise JournalError(f"journal line {n}: bad gate record")
            # An answered record wins over an open one, whatever the line order.
            have = gates.get(ordinal)
            if have is None or (have.status == "open" and status == "answered"):
                gates[ordinal] = GateRecord(ordinal, key, gate_kind, status, rec.get("answer"))
        elif kind == "verb":
            ordinal, key, name = rec.get("ordinal"), rec.get("key"), rec.get("name")
            call_id, status, error = rec.get("callId"), rec.get("status"), rec.get("error")
            if (
                not _is_count(ordinal)
                or not isinstance(key, str)
                or not isinstance(name, str)
                or not isinstance(call_id, str)
                or status not in _VERB_STATUSES
                or (error is not None and not isinstance(error, str))
            ):
                raise JournalError(f"journal line {n}: bad verb record")
            # A finished record wins over an open one, whatever the line order.
            have_verb = verbs.get(ordinal)
            if have_verb is None or (have_verb.status == "open" and status != "open"):
                verbs[ordinal] = VerbRecord(
                    ordinal, key, name, call_id, status, rec.get("value"), error
                )
    if header is None:
        raise JournalError(f"journal for run {run_id!r} does not start with a run header")
    return LoadedJournal(
        header,
        workflows,
        tuple(agents[o] for o in sorted(agents)),
        tuple(started[o] for o in sorted(started)),
        tuple(gates[o] for o in sorted(gates)),
        tuple(verbs[o] for o in sorted(verbs)),
    )


def journal_owner(state_dir: Path, run_id: str) -> tuple[bool, str | None]:
    """Whether the run's journal header can be read, and the principal it records.

    Reads only the first line, so a journal torn or corrupt further down still names its
    owner; `(False, None)` when there is no readable run header at all.
    """
    try:
        with open(run_dir(state_dir, run_id) / JOURNAL_FILE, "rb") as f:
            rec = json.loads(f.readline().decode("utf-8"))
    except (OSError, ValueError):
        return False, None
    if not isinstance(rec, dict) or rec.get("type") != "run":
        return False, None
    principal = rec.get("principal")
    return True, principal if isinstance(principal, str) else None


def journal_busy(state_dir: Path, run_id: str) -> bool:
    """Whether a live writer (this process or another) holds the run's `journal.lock`.

    A journal written before the lock existed has none to find, so a live writer of that
    vintage is not detected (as for `redact_agent_record`).
    """
    lock = run_dir(state_dir, run_id) / LOCK_FILE
    if not lock.is_file():
        return False
    try:
        guard = open(lock, "a+b")  # noqa: SIM115
    except OSError:
        return False
    try:
        if not _try_lock(guard):
            return True
        _unlock(guard)
        return False
    finally:
        guard.close()


def redact_agent_record(
    state_dir: Path, run_id: str, ordinal: int, *, reason: str, redacted_at: str
) -> AgentRecord:
    """Rewrite a finished agent() record in place: value and error become `[redacted]`
    and the record is marked `redacted`. Every other line stays byte-identical; the
    torn or empty tail is dropped (a torn line may hold a partial copy of the value).
    The file is replaced atomically.

    A run whose writer is still open (this process or another) holds `journal.lock`;
    this raises `JournalBusyError` instead of rewriting a file under it, and holds the
    lock itself for the rewrite. A journal written before the lock existed has none
    to find, so a live writer of that vintage is not detected. Runs resumed from this
    one hold their own copies and are not touched.
    """
    if not _is_count(ordinal):
        raise ValueError(f"ordinal must be a non-negative int: {ordinal!r}")
    path = run_dir(state_dir, run_id) / JOURNAL_FILE
    if not path.is_file():
        raise JournalError(f"no journal for run {run_id!r} under {state_dir}")
    guard = open(path.with_name(LOCK_FILE), "a+b")  # noqa: SIM115
    try:
        if not _try_lock(guard):
            raise JournalBusyError(
                f"run {run_id!r} is still being written by a live process; "
                "wait for it to end or abort it"
            )
        try:
            _rewrite(path, ordinal, reason, redacted_at)
        finally:
            _unlock(guard)
    finally:
        guard.close()
    for record in load_journal(state_dir, run_id).agents:
        if record.ordinal == ordinal:
            return record
    raise JournalError(f"run {run_id!r} has no finished agent() #{ordinal}")


def _rewrite(path: Path, ordinal: int, reason: str, redacted_at: str) -> None:
    lines = path.read_bytes().decode("utf-8").split("\n")[:-1]
    found = False
    for i, part in enumerate(lines):
        try:
            rec = json.loads(part)
        except ValueError:
            rec = None
        if not isinstance(rec, dict) or not isinstance(rec.get("type"), str):
            raise JournalError(f"journal line {i + 1} is not a valid record")
        if rec["type"] == "agent" and rec.get("ordinal") == ordinal:
            rec["value"] = MASK
            if "error" in rec:
                rec["error"] = MASK
            rec["redacted"] = {"reason": reason, "redactedAt": redacted_at}
            lines[i] = _line(rec).rstrip("\n")
            found = True
    if not found:
        raise JournalError(f"{path.parent.name!r} has no finished agent() #{ordinal}")
    tmp = path.with_name(f"{JOURNAL_FILE}.{uuid.uuid4().hex}.tmp")
    try:
        with open(tmp, "x", encoding="utf-8", newline="\n") as f:
            f.write("\n".join(lines) + "\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
