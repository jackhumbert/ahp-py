"""Run journal: one append-only JSONL file per run (plan §4.2).

The header record holds the script snapshot; each dispatched `agent()` call adds a
start record and, when it completes, an agent record. A torn final line (a killed
process) is ignored on read. Record types other than run/workflow/agent/started are
ignored on load, on purpose, so a later record kind (M10 gates) stays readable by
this code; keep it so.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any, Final, Literal, TypeGuard

from ahp_host_flow.engine.dispatcher import AgentCall
from ahp_host_flow.engine.options import RUN_ID

JOURNAL_VERSION: Final = 1
RUNS_DIR: Final = "runs"
JOURNAL_FILE: Final = "journal.jsonl"

JournalStatus = Literal["ok", "null", "error"]
_STATUSES: Final = frozenset({"ok", "null", "error"})


class JournalError(Exception):
    """A journal that cannot be written or read; the message is user-facing."""


def canonical_json(value: Any) -> str:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )


def call_key(call: AgentCall) -> str:
    # One JSON object, not string concatenation, so field boundaries are unambiguous.
    payload = {
        "label": call.label,
        "opts": dict(call.opts),
        "phase": call.phase,
        "prompt": call.prompt,
    }
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


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


@dataclass(frozen=True)
class WorkflowSnapshot:
    # The ref as the script wrote it: a name, or {"scriptPath": ...}.
    ref: Any
    filename: str
    source: str

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


@dataclass(frozen=True)
class StartRecord:
    ordinal: int
    key: str
    # The dispatch handle: provider name and worker chat URI, both None when unknown.
    provider: str | None = None
    chat: str | None = None


@dataclass(frozen=True)
class LoadedJournal:
    header: RunHeader
    workflows: Mapping[str, WorkflowSnapshot]
    agents: tuple[AgentRecord, ...]
    started: tuple[StartRecord, ...] = ()

    @property
    def unfinished(self) -> tuple[StartRecord, ...]:
        """Started calls with no agent record: in flight when the run was killed."""
        done = {a.ordinal for a in self.agents}
        return tuple(s for s in self.started if s.ordinal not in done)


def _line(record: dict[str, Any]) -> str:
    return json.dumps(record, separators=(",", ":"), ensure_ascii=True, allow_nan=False) + "\n"


class JournalWriter:
    def __init__(
        self,
        path: Path,
        run_id: str,
        file: IO[str],
        mask: Callable[[Any], Any] | None = None,
    ) -> None:
        self._path = path
        self._run_id = run_id
        self._file: IO[str] | None = file
        self._mask = mask

    @classmethod
    def create(
        cls,
        state_dir: Path,
        header: RunHeader,
        *,
        mask: Callable[[Any], Any] | None = None,
    ) -> JournalWriter:
        d = run_dir(state_dir, header.run_id)
        try:
            d.mkdir(parents=True, exist_ok=False)
        except FileExistsError:
            raise JournalError(f"run {header.run_id!r} already exists under {state_dir}") from None
        path = d / JOURNAL_FILE
        # newline="\n": no \r on Windows, so the file is byte-identical everywhere.
        file = open(path, "x", encoding="utf-8", newline="\n")  # noqa: SIM115
        writer = cls(path, header.run_id, file, mask)
        try:
            writer._write(
                {
                    "type": "run",
                    "version": JOURNAL_VERSION,
                    "runId": header.run_id,
                    "resumedFrom": header.resumed_from,
                    "filename": header.filename,
                    "source": header.source,
                    "hasArgs": header.has_args,
                    "args": header.args if header.has_args else None,
                    "options": {
                        "maxAgents": header.max_agents,
                        "budgetTotal": header.budget_total,
                    },
                }
            )
        except BaseException:
            # A header that cannot be written (args with NaN, say) leaves no run
            # behind, so the same run id can be used again.
            writer.close()
            path.unlink(missing_ok=True)
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
        self._write(wire)

    def append_started(self, record: StartRecord) -> None:
        handle: dict[str, Any] | None = None
        if record.provider is not None or record.chat is not None:
            handle = {"provider": record.provider, "chat": record.chat}
        self._write(
            {"type": "started", "ordinal": record.ordinal, "key": record.key, "handle": handle}
        )

    def append_workflow(self, snapshot: WorkflowSnapshot) -> None:
        self._write(
            {
                "type": "workflow",
                "ref": snapshot.ref,
                "filename": snapshot.filename,
                "source": snapshot.source,
            }
        )

    def close(self) -> None:
        file, self._file = self._file, None
        if file is not None:
            file.close()

    def _write(self, record: dict[str, Any]) -> None:
        # A dispatch can finish after the run ended; its record is dropped.
        if self._file is None:
            return
        # One write() then flush: a killed process loses at most the line in flight.
        self._file.write(_line(record))
        self._file.flush()


def _is_count(value: Any) -> TypeGuard[int]:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def load_journal(state_dir: Path, run_id: str) -> LoadedJournal:
    """Read a run's journal. Record types other than run/workflow/agent/started are
    ignored, on purpose, so a later record kind (M10 gates) stays readable; keep it so.
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
            header = RunHeader(
                run_id=str(rec.get("runId", run_id)),
                filename=str(rec.get("filename", "")),
                source=source if isinstance(source, str) else None,
                has_args=bool(rec.get("hasArgs")),
                args=rec.get("args"),
                max_agents=options.get("maxAgents", 0),
                budget_total=options.get("budgetTotal"),
                resumed_from=rec.get("resumedFrom"),
            )
        elif kind == "workflow":
            filename, source = rec.get("filename"), rec.get("source")
            if not isinstance(filename, str) or not isinstance(source, str):
                raise JournalError(f"journal line {n}: bad workflow record")
            snap = WorkflowSnapshot(ref=rec.get("ref"), filename=filename, source=source)
            workflows.setdefault(snap.ref_key, snap)
        elif kind == "agent":
            ordinal, key, status = rec.get("ordinal"), rec.get("key"), rec.get("status")
            tokens, error = rec.get("outputTokens"), rec.get("error")
            if (
                not _is_count(ordinal)
                or not isinstance(key, str)
                or status not in _STATUSES
                or not _is_count(tokens)
                or (error is not None and not isinstance(error, str))
            ):
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
                ),
            )
        elif kind == "started":
            ordinal, key, handle = rec.get("ordinal"), rec.get("key"), rec.get("handle")
            provider = chat = None
            if isinstance(handle, dict):
                provider, chat = handle.get("provider"), handle.get("chat")
            if (
                not _is_count(ordinal)
                or not isinstance(key, str)
                or (handle is not None and not isinstance(handle, dict))
                or (provider is not None and not isinstance(provider, str))
                or (chat is not None and not isinstance(chat, str))
            ):
                raise JournalError(f"journal line {n}: bad started record")
            started.setdefault(ordinal, StartRecord(ordinal, key, provider, chat))
    if header is None:
        raise JournalError(f"journal for run {run_id!r} does not start with a run header")
    return LoadedJournal(
        header,
        workflows,
        tuple(agents[o] for o in sorted(agents)),
        tuple(started[o] for o in sorted(started)),
    )
