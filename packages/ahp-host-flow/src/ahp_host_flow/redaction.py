"""Redaction after publishing (plan §4.9, the after-publishing layer).

`redact_step` takes one `agent()` call out of the shared view through
`Host.redact` (the worker chat's parts and the `agent` tool call on the main
chat) and out of the run's journal, so a resume replays `[redacted]`.

What it does NOT reach: the inner agent's own context and provider state; the
worker turn's message (the prompt) and the run's final ```json``` value (use
`Host.redact_message` / `Host.redact` for those); and runs resumed from this
run, which keep their own journal copies and are listed as `descendants`.

A running process asks for a redaction through a request spool under the state
dir (`queue_request`), which the server's poller (`start_poller`) works off.
"""

from __future__ import annotations

import asyncio
import getpass
import json
import logging
import os
import time
import uuid
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

from ahp_host import Host
from ahp_host.core.automations import iso
from ahp_host.core.redaction import RedactionError as HostRedactionError
from ahp_protocol.ext import flow
from ahp_protocol.ext.redaction import (
    REDACTABLE_PART_KINDS,
    find_part,
    marker_of,
    part_id_of,
)

from ahp_host_flow import engine
from ahp_host_flow.engine import RUN_ID, JournalError
from ahp_host_flow.provider import FlowProvider

_log = logging.getLogger(__name__)

#: Under the state dir: where `flow redact` leaves requests for a running server.
REQUESTS_DIR: Final = "redact"
REQUEST_SUFFIX: Final = ".request.json"
RESULT_SUFFIX: Final = ".result.json"
POLL_SECONDS: Final = 1.0
#: The audit entry's actor for a spooled request that names none.
ACTOR: Final = "flow redact (local operator)"


def local_actor() -> str:
    """Who is running `flow redact` here: the operating-system user, as a stated actor."""
    try:
        return f"{ACTOR} {getpass.getuser()}"
    except Exception:
        return ACTOR


class RedactionError(Exception):
    """A redaction that cannot be done; the message is for the user.

    *done* lists what had already been redacted when it failed (the journal, some
    worker-chat parts), so an operator knows what a retry will finish; empty when
    nothing was changed.
    """

    def __init__(self, message: str, *, done: Sequence[str] = ()) -> None:
        super().__init__(message)
        self.done = tuple(done)


@dataclass(frozen=True)
class StepLocation:
    chat: str
    turn_id: str
    tool_call_id: str
    worker_chat: str | None
    #: The call's own turn on the worker chat (`stepStarted.turnId`); None when the step
    #: does not name one (a run from before it was recorded), and then every turn is its.
    worker_turn: str | None = None


@dataclass(frozen=True)
class StepRedaction:
    run_id: str
    ordinal: int
    reason: str
    redacted_at: str
    main_chat: str | None
    tool_call_id: str | None
    worker_chat: str | None
    worker_parts: int
    skipped_parts: int
    descendants: tuple[str, ...]
    #: Every attempt's `agent` tool call and worker chat, in order (`tool_call_id` and
    #: `worker_chat` are the first attempt's).
    tool_call_ids: tuple[str, ...] = ()
    worker_chats: tuple[str, ...] = ()

    def to_json(self) -> dict[str, Any]:
        return {
            "runId": self.run_id,
            "ordinal": self.ordinal,
            "reason": self.reason,
            "redactedAt": self.redacted_at,
            "mainChat": self.main_chat,
            "toolCallId": self.tool_call_id,
            "workerChat": self.worker_chat,
            "toolCallIds": list(self.tool_call_ids),
            "workerChats": list(self.worker_chats),
            "workerParts": self.worker_parts,
            "skippedParts": self.skipped_parts,
            "descendants": list(self.descendants),
            "journal": True,
        }


def _turns_of(state: Any) -> list[Mapping[str, Any]]:
    """Every turn of a chat state: the finished ones, then the active one."""
    if not isinstance(state, Mapping):
        return []
    turns = state.get("turns")
    found = [t for t in turns if isinstance(t, Mapping)] if isinstance(turns, list) else []
    active = state.get("activeTurn")
    if isinstance(active, Mapping):
        found.append(active)
    return found


def _parts_of(turn: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    parts = turn.get("responseParts")
    return [p for p in parts if isinstance(p, Mapping)] if isinstance(parts, list) else []


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _names_run(turn: Mapping[str, Any], run_id: str) -> bool:
    for part in _parts_of(turn):
        if part.get("kind") != "systemNotification":
            continue
        started = (flow.descriptor_of(part.get("_meta")) or {}).get("runStarted")
        if isinstance(started, Mapping) and started.get("runId") == run_id:
            return True
    return False


def find_steps(host: Host, run_id: str, ordinal: int) -> list[StepLocation]:
    """Every `agent` tool call of call *ordinal* of run *run_id* on this host, in order.

    A call has one per attempt: `agent-<n>`, then `agent-<n>-a<k>` for each `retry_empty`
    attempt and fallback provider, each with its own worker chat.
    """
    found: list[StepLocation] = []
    for uri in host.sequencer.channels("chat"):
        for turn in _turns_of(host.sequencer.state_of(uri)):
            if not _names_run(turn, run_id):
                continue
            for part in _parts_of(turn):
                call = part.get("toolCall")
                if part.get("kind") != "toolCall" or not isinstance(call, Mapping):
                    continue
                started = (flow.descriptor_of(call.get("_meta")) or {}).get("stepStarted")
                if not isinstance(started, Mapping) or not _is_int(started.get("ordinal")):
                    continue
                call_id = call.get("toolCallId")
                if started["ordinal"] == ordinal and isinstance(call_id, str):
                    worker, worker_turn = started.get("chat"), started.get("turnId")
                    found.append(
                        StepLocation(
                            uri,
                            str(turn.get("id")),
                            call_id,
                            worker if isinstance(worker, str) else None,
                            worker_turn if isinstance(worker_turn, str) else None,
                        )
                    )
    return found


def find_step(host: Host, run_id: str, ordinal: int) -> StepLocation | None:
    """Where call *ordinal* of run *run_id* is shown on this host (its first attempt), or None."""
    found = find_steps(host, run_id, ordinal)
    return found[0] if found else None


def descendants(state_dir: Path, run_id: str) -> tuple[str, ...]:
    """Runs resumed from *run_id*, transitively (they hold their own journal copies)."""
    children: dict[str, list[str]] = {}
    for journal in sorted((state_dir / "runs").glob("*/journal.jsonl")):
        try:
            with journal.open(encoding="utf-8") as fh:
                header = json.loads(fh.readline())
        except (OSError, ValueError):
            continue
        if not isinstance(header, Mapping):
            continue
        parent, own = header.get("resumedFrom"), header.get("runId")
        if isinstance(parent, str) and isinstance(own, str):
            children.setdefault(parent, []).append(own)
    found: set[str] = set()
    pending = [run_id]
    while pending:
        for child in children.get(pending.pop(), ()):
            if child not in found:
                found.add(child)
                pending.append(child)
    return tuple(sorted(found))


async def redact_step(
    host: Host,
    *,
    state_dir: Path,
    run_id: str,
    ordinal: int,
    reason: str,
    actor: str,
    live_runs: Collection[str] = frozenset(),
    now: datetime | None = None,
) -> StepRedaction:
    """Redact call *ordinal* of run *run_id*: the journal, then the shared view.

    The journal goes first so a bad run or ordinal fails before anything else
    changes. With no chat on this host naming the run, only the journal is
    rewritten (`main_chat` is `None`).

    A live run is refused two ways: *live_runs* (the runs this process is
    executing) and the lock a run's `JournalWriter` holds on its journal, which
    also catches a writer in another process (`JournalBusyError`).

    It is not atomic. If it fails part way, the `RedactionError` says what had
    already been redacted (`.done`); redacting again completes it, because parts
    that already carry a marker are skipped and the journal rewrite is idempotent.
    """
    if run_id in live_runs:
        raise RedactionError(f"run {run_id} is still running; wait for it to end or abort it")
    redacted_at = iso(now or datetime.now(UTC))
    try:
        engine.redact_agent_record(
            state_dir, run_id, ordinal, reason=reason, redacted_at=redacted_at
        )
    except (JournalError, ValueError) as exc:
        raise RedactionError(str(exc)) from exc
    done = [f"journal of run {run_id}, call {ordinal}"]
    locs = find_steps(host, run_id, ordinal)
    workers = list(dict.fromkeys(loc.worker_chat for loc in locs if loc.worker_chat is not None))
    worker_parts = skipped_parts = 0
    try:
        for worker in workers:
            if host.sequencer.reducer_of(worker) != "chat":
                continue
            # A named chat (`opts.x.chat`) holds other calls' turns too: only this call's.
            own = {loc.worker_turn for loc in locs if loc.worker_chat == worker}
            for turn in _turns_of(host.sequencer.state_of(worker)):
                if None not in own and turn.get("id") not in own:
                    continue
                for part in _parts_of(turn):
                    part_id = part_id_of(part)
                    if marker_of(part) is not None:
                        continue
                    if part.get("kind") in REDACTABLE_PART_KINDS and part_id is not None:
                        await host.redact(
                            worker,
                            str(turn.get("id")),
                            part_id,
                            reason,
                            actor=actor,
                            now=now,
                        )
                        worker_parts += 1
                        done.append(f"part {part_id} of worker chat {worker}")
                    else:
                        skipped_parts += 1
        for loc in locs:
            call_part = find_part(host.sequencer.state_of(loc.chat), loc.turn_id, loc.tool_call_id)
            call = call_part.get("toolCall") if call_part is not None else None
            desc = flow.descriptor_of(call.get("_meta")) if isinstance(call, Mapping) else None
            keep = flow.meta(flow.redacted_step(desc)) if desc is not None else None
            await host.redact(
                loc.chat,
                loc.turn_id,
                loc.tool_call_id,
                reason,
                actor=actor,
                keep_meta=keep,
                now=now,
            )
            done.append(f"tool call {loc.tool_call_id} of chat {loc.chat}")
    except HostRedactionError as exc:
        raise RedactionError(
            f"{exc}. Partly redacted: {'; '.join(done)}. The call's tool calls on the "
            f"main chat may not be. Redact the same call again to finish it",
            done=done,
        ) from exc
    first = locs[0] if locs else None
    return StepRedaction(
        run_id=run_id,
        ordinal=ordinal,
        reason=reason,
        redacted_at=redacted_at,
        main_chat=first.chat if first else None,
        tool_call_id=first.tool_call_id if first else None,
        worker_chat=first.worker_chat if first else None,
        worker_parts=worker_parts,
        skipped_parts=skipped_parts,
        descendants=descendants(state_dir, run_id),
        tool_call_ids=tuple(loc.tool_call_id for loc in locs),
        worker_chats=tuple(workers),
    )


def queue_request(
    state_dir: Path, run_id: str, ordinal: int, reason: str, *, actor: str | None = None
) -> Path:
    """Leave a redaction request for the server's poller; returns the request file.

    *actor* is who asked (default: `local_actor()`, the operating-system user);
    the poller records it in the audit.

    The file appears whole (written aside, then renamed), so a poller never
    reads half of it. Its answer lands in `result_path(request)`.
    """
    if not RUN_ID.fullmatch(run_id):
        raise ValueError(f"invalid run id: {run_id!r}")
    if not _is_int(ordinal) or ordinal < 0:
        raise ValueError(f"invalid ordinal: {ordinal!r}")
    d = state_dir / REQUESTS_DIR
    d.mkdir(parents=True, exist_ok=True)
    name = f"{time.time_ns():020d}-{uuid.uuid4().hex[:8]}"
    part = d / (name + ".part")
    part.write_text(
        json.dumps(
            {
                "runId": run_id,
                "ordinal": ordinal,
                "reason": reason,
                "actor": actor if actor else local_actor(),
            }
        ),
        encoding="utf-8",
    )
    final = d / (name + REQUEST_SUFFIX)
    os.replace(part, final)
    return final


def result_path(request: Path) -> Path:
    return request.with_name(request.name.removesuffix(REQUEST_SUFFIX) + RESULT_SUFFIX)


def _parse(request: Path) -> tuple[str, int, str, str] | None:
    try:
        body = json.loads(request.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(body, Mapping):
        return None
    run_id, ordinal, reason = body.get("runId"), body.get("ordinal"), body.get("reason")
    if (
        not isinstance(run_id, str)
        or not RUN_ID.fullmatch(run_id)
        or not isinstance(ordinal, int)
        or isinstance(ordinal, bool)
        or ordinal < 0
        or not isinstance(reason, str)
    ):
        return None
    actor = body.get("actor")
    return run_id, ordinal, reason, actor if isinstance(actor, str) and actor.strip() else ACTOR


async def process_requests(host: Host, provider: FlowProvider) -> list[Path]:
    """Work off every queued request; returns the result files written, in order."""
    state_dir = provider.config.state_dir
    d = state_dir / REQUESTS_DIR
    if not d.is_dir():
        return []
    written: list[Path] = []
    for request in sorted(d.glob("*" + REQUEST_SUFFIX)):
        parsed = _parse(request)
        result: dict[str, Any]
        if parsed is None:
            result = {"ok": False, "error": "malformed redaction request"}
        else:
            run_id, ordinal, reason, actor = parsed
            try:
                report = await redact_step(
                    host,
                    state_dir=state_dir,
                    run_id=run_id,
                    ordinal=ordinal,
                    reason=reason,
                    actor=actor,
                    live_runs=provider.live_run_ids(),
                )
                result = {"ok": True, **report.to_json()}
            except RedactionError as exc:
                result = {"ok": False, "error": str(exc), "done": list(exc.done)}
        out = result_path(request)
        tmp = out.with_name(out.name + ".tmp")
        tmp.write_text(json.dumps(result), encoding="utf-8")
        os.replace(tmp, out)
        request.unlink(missing_ok=True)
        written.append(out)
    return written


async def poll_requests(
    host: Host, provider: FlowProvider, *, interval: float = POLL_SECONDS
) -> None:
    """Work off the request spool until cancelled."""
    while True:
        try:
            await process_requests(host, provider)
        except Exception:
            _log.exception("redaction request processing failed")
        await asyncio.sleep(interval)


def start_poller(host: Host) -> asyncio.Task[None] | None:
    """A task polling the spool when *host* serves a `FlowProvider`, else `None`."""
    provider = host.provider
    if not isinstance(provider, FlowProvider):
        return None
    return asyncio.create_task(poll_requests(host, provider))
