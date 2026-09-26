"""Host-owned automations (AHP 0.9.0): the catalogue, its schedules, its records.

An automation is a saved prompt plus a session template: every run creates a
fresh session from the template and sends the saved message as its first turn.
Runs start by hand (`runAutomation`) or from a schedule trigger evaluated here.
The host owns all of it -- "clients render this state and submit actions or
commands; they never run a fallback scheduler for a host-owned definition"
(`AutomationEntry`, state.schema.json).

This module is the part with no sessions in it: the cron grammar, when a
schedule is due, what a valid definition is, and the durable record. Running a
run -- creating its session, starting its turn, following it to the end --
needs the whole host and lives in `core/host.py`.

**Off unless the embedder names a store**, like sessions and the sequence
counter. A host with no `AutomationStore` advertises no `automations`
capability and registers no catalogue channel, which is what the spec says
absence means. Automations are durable by definition, so there is no
in-memory default on the host either: `InMemoryAutomationStore` exists for
tests, and an embedder who wants one passes it explicitly.

**Event triggers are refused.** Their types are host-defined and discovered
through `listAutomationTriggerDefinitions`; this host defines none, so that
command answers an empty list and a definition carrying an `event` trigger is
rejected rather than saved as a trigger that can never fire.
"""

from __future__ import annotations

import asyncio
import contextlib
import copy
import hashlib
import json
import logging
import os
from collections.abc import Collection, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, Final, Protocol
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ahp_host.core.store import _write_atomically

__all__ = [
    "AUTOMATION_SCHEME",
    "AutomationRecord",
    "AutomationStore",
    "CronExpression",
    "FileAutomationStore",
    "InMemoryAutomationStore",
    "Occurrence",
    "apply_patch",
    "definition_rejection",
    "due_occurrences",
    "next_run_at",
]

_log = logging.getLogger(__name__)

#: "Stable `ahp-automation:/<id>` resource identifier" (`AutomationEntry.resource`).
AUTOMATION_SCHEME: Final = "ahp-automation:"

#: Terminal lifecycles. A run in one of these never changes again.
TERMINAL_STATUSES: Final = frozenset({"completed", "failed", "cancelled"})

#: How late an occurrence may be noticed and still count as on time. The
#: scheduler sleeps until the next occurrence, but a sleep can overrun (a
#: suspended laptop, a busy loop), and an occurrence found a few seconds late
#: is not what `misfirePolicy` is about -- that is for occurrences missed while
#: "automatic execution was unavailable" (`AutomationMisfirePolicy`).
ON_TIME_GRACE: Final = timedelta(minutes=5)

#: How far ahead a schedule is searched for its next occurrence. `0 0 31 2 *`
#: (February 31st) is valid grammar that never fires; the search has to stop.
_HORIZON_DAYS: Final = 366 * 5

_FORMAT_VERSION = 1


# ─── the cron grammar ───────────────────────────────────────────────────────

_MONTHS: Final = {
    name: number
    for number, name in enumerate(
        ("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"),
        start=1,
    )
}
_WEEKDAYS: Final = {
    name: number for number, name in enumerate(("SUN", "MON", "TUE", "WED", "THU", "FRI", "SAT"))
}

_NO_NAMES: Final[Mapping[str, int]] = {}


def _value(text: str, low: int, high: int, names: Mapping[str, int]) -> int:
    named = names.get(text.upper())
    if named is not None:
        return named
    # `isdecimal`, not `isdigit`: "²" is a digit to Python and not to cron.
    if not text.isascii() or not text.isdecimal():
        raise ValueError(f"{text!r} is not a number")
    number = int(text)
    if not low <= number <= high:
        raise ValueError(f"{number} is outside {low}-{high}")
    return number


def _field(text: str, low: int, high: int, names: Mapping[str, int] = _NO_NAMES) -> frozenset[int]:
    """One field of the five, as the set of values it matches."""
    values: set[int] = set()
    for item in text.split(","):
        base, slash, step_text = item.partition("/")
        step = 1
        if slash:
            if not step_text.isascii() or not step_text.isdecimal() or int(step_text) < 1:
                # "A step MUST be a positive integer."
                raise ValueError(f"step {step_text!r} is not a positive integer")
            step = int(step_text)
        if base == "*":
            start, end = low, high
        elif "-" in base:
            first, _, last = base.partition("-")
            start, end = _value(first, low, high, names), _value(last, low, high, names)
            if start > end:
                raise ValueError(f"range {base!r} runs backwards")
        else:
            if slash:
                # "a step applied to `*` or a range" -- not to a single value.
                # Vixie cron reads `5/10` as `5-59/10`; AHP does not say so,
                # and guessing would fire a schedule the author never wrote.
                raise ValueError(f"a step needs `*` or a range, not {base!r}")
            start = end = _value(base, low, high, names)
        values.update(range(start, end + 1, step))
    return frozenset(values)


@dataclass(frozen=True)
class CronExpression:
    """A parsed five-field AHP cron expression (`AutomationSchedule`)."""

    minutes: frozenset[int]
    hours: frozenset[int]
    days: frozenset[int]
    months: frozenset[int]
    weekdays: frozenset[int]
    #: Whether each day field was `*`. Cron's day rule depends on it, not on
    #: the value set: `*` and `1-31` match the same days but combine with the
    #: weekday field differently.
    any_day: bool
    any_weekday: bool

    @classmethod
    def parse(cls, expression: str) -> CronExpression:
        fields = expression.split()
        if len(fields) != 5:
            # "exactly five whitespace-separated fields" -- no seconds, no
            # years, no `@daily`.
            raise ValueError(f"expected 5 fields, got {len(fields)}")
        minute, hour, day, month, weekday = fields
        weekdays = _field(weekday, 0, 7, _WEEKDAYS)
        return cls(
            minutes=_field(minute, 0, 59),
            hours=_field(hour, 0, 23),
            days=_field(day, 1, 31),
            months=_field(month, 1, 12, _MONTHS),
            # "both `0` and `7` mean Sunday"
            weekdays=frozenset(0 if each == 7 else each for each in weekdays),
            any_day=day == "*",
            any_weekday=weekday == "*",
        )

    def matches_day(self, day: date) -> bool:
        if day.month not in self.months:
            return False
        in_month = day.day in self.days
        # Python counts Monday as 0; cron counts Sunday as 0.
        in_week = (day.weekday() + 1) % 7 in self.weekdays
        if self.any_day and self.any_weekday:
            return True
        if self.any_day:
            return in_week
        if self.any_weekday:
            return in_month
        # "When both day-of-month and day-of-week are restricted (not `*`), an
        # occurrence matches when either day field matches, following Unix
        # cron semantics."
        return in_month or in_week

    def _candidates(self, start: datetime, zone: ZoneInfo, *, forward: bool) -> Iterator[datetime]:
        """Every matching wall-clock minute from `start`'s local day onward
        (or backward), as UTC instants, in order.

        Wall-clock times the zone skips (a spring-forward gap) do not exist and
        are not produced; a repeated hour (fall-back) is produced once, at its
        first occurrence. Both are what a user reading "9:00 in Europe/Berlin"
        means.
        """
        local_day = start.astimezone(zone).date()
        hours = sorted(self.hours, reverse=not forward)
        minutes = sorted(self.minutes, reverse=not forward)
        step = timedelta(days=1 if forward else -1)
        for offset in range(_HORIZON_DAYS):
            day = local_day + step * offset
            if not self.matches_day(day):
                continue
            for hour in hours:
                for minute in minutes:
                    wall = datetime(day.year, day.month, day.day, hour, minute, tzinfo=zone)
                    instant = wall.astimezone(UTC)
                    if instant.astimezone(zone).replace(tzinfo=None) != wall.replace(tzinfo=None):
                        continue
                    yield instant

    def next_after(self, after: datetime, zone: ZoneInfo) -> datetime | None:
        """The first occurrence strictly after `after`, or `None` if there is
        none within five years."""
        after = after.astimezone(UTC)
        for instant in self._candidates(after, zone, forward=True):
            if instant > after:
                return instant
        return None

    def last_at_or_before(self, before: datetime, zone: ZoneInfo) -> datetime | None:
        """The latest occurrence at or before `before`."""
        before = before.astimezone(UTC)
        for instant in self._candidates(before, zone, forward=False):
            if instant <= before:
                return instant
        return None


def _zone(name: Any) -> ZoneInfo:
    if not isinstance(name, str) or not name:
        raise ValueError("timeZone must be an IANA time zone name")
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(f"unknown time zone {name!r}") from exc


def _schedule_of(trigger: Mapping[str, Any]) -> tuple[CronExpression, ZoneInfo] | None:
    """A schedule trigger's parsed schedule, or `None` for anything else.

    Definitions are validated before they are saved, so `None` for a schedule
    trigger means a file edited by hand -- skipped rather than fatal.
    """
    if trigger.get("kind") != "schedule":
        return None
    schedule = trigger.get("schedule")
    if not isinstance(schedule, Mapping):
        return None
    try:
        return CronExpression.parse(str(schedule.get("expression"))), _zone(
            schedule.get("timeZone")
        )
    except ValueError:
        return None


# ─── validation ─────────────────────────────────────────────────────────────


def _trigger_rejection(trigger: Any) -> str | None:
    if not isinstance(trigger, Mapping):
        return "each trigger must be an object"
    if not isinstance(trigger.get("id"), str) or not trigger["id"]:
        return "each trigger needs a non-empty string id"
    kind = trigger.get("kind")
    if kind == "event":
        return "this host defines no event trigger types"
    if kind != "schedule":
        return f"unknown trigger kind {kind!r}"
    schedule = trigger.get("schedule")
    if not isinstance(schedule, Mapping):
        return "a schedule trigger needs a schedule"
    expression = schedule.get("expression")
    try:
        CronExpression.parse(expression if isinstance(expression, str) else "")
        _zone(schedule.get("timeZone"))
    except ValueError as exc:
        return f"trigger {trigger['id']!r}: {exc}"
    policy = trigger.get("misfirePolicy")
    if "misfirePolicy" in trigger and policy not in ("skip", "runOnce"):
        return f"trigger {trigger['id']!r}: unknown misfirePolicy {policy!r}"
    return None


def definition_rejection(definition: Any, providers: Collection[str]) -> str | None:
    """Why `definition` cannot be saved, or `None` if it can.

    Checked as the schema says it and no further: the session template is
    revalidated when a run starts, because a folder or a provider that exists
    today may not tomorrow ("The host revalidates every selection when the run
    starts", `AutomationSessionTemplate`).
    """
    if not isinstance(definition, Mapping):
        return "definition must be an object"
    if not isinstance(definition.get("title"), str):
        return "definition.title must be a string"
    message = definition.get("message")
    if not isinstance(message, Mapping) or not isinstance(message.get("text"), str):
        return "definition.message needs text"
    origin = message.get("origin")
    if not isinstance(origin, Mapping) or origin.get("kind") != "automation":
        # "Its `Message.origin` kind MUST be `MessageKind.Automation`."
        return "definition.message.origin.kind must be 'automation'"
    session = definition.get("session")
    if not isinstance(session, Mapping):
        return "definition.session must be an object"
    provider = session.get("provider")
    if "provider" in session and provider not in providers:
        return f"no agent for provider {provider!r}"
    directories = session.get("workingDirectories")
    if "workingDirectories" in session and (
        not isinstance(directories, list) or not all(isinstance(d, str) for d in directories)
    ):
        return "definition.session.workingDirectories must be a list of URIs"
    if "config" in session and not isinstance(session.get("config"), Mapping):
        return "definition.session.config must be an object"
    if not isinstance(definition.get("enabled"), bool):
        return "definition.enabled must be a boolean"
    triggers = definition.get("triggers")
    if not isinstance(triggers, list):
        return "definition.triggers must be a list"
    seen: set[str] = set()
    for trigger in triggers:
        rejection = _trigger_rejection(trigger)
        if rejection is not None:
            return rejection
        if trigger["id"] in seen:
            # "Identifier unique and stable within this automation definition."
            return f"two triggers share the id {trigger['id']!r}"
        seen.add(trigger["id"])
    if "_meta" in definition and not isinstance(definition.get("_meta"), Mapping):
        return "definition._meta must be an object"
    return None


_PATCHABLE: Final = ("title", "message", "session", "enabled", "triggers", "_meta")


def apply_patch(definition: Mapping[str, Any], changes: Mapping[str, Any]) -> dict[str, Any]:
    """`AutomationDefinitionPatch`: "Omitted fields are unchanged. Supplied
    arrays and objects replace their corresponding values in full"."""
    patched = copy.deepcopy(dict(definition))
    for key in _PATCHABLE:
        if key in changes:
            patched[key] = copy.deepcopy(changes[key])
    return patched


# ─── the durable record ─────────────────────────────────────────────────────


@dataclass
class AutomationRecord:
    """Everything the host keeps about one automation.

    `runs` holds full `AutomationRunState`s, newest first, so a run channel can
    be re-registered after a restart with the state it had. `cursors` records,
    per schedule trigger, the instant up to which its occurrences have been
    dealt with -- the thing that turns a restart into a misfire rather than a
    silent skip or a burst of duplicates.
    """

    resource: str
    definition: dict[str, Any]
    created_at: str
    modified_at: str
    cursors: dict[str, str] = field(default_factory=dict)
    runs: list[dict[str, Any]] = field(default_factory=list)
    #: `runAutomation.requestId` -> the run it created. "Retrying with the same
    #: key and automation MUST return the original run URI."
    requests: dict[str, str] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {
            "format": _FORMAT_VERSION,
            "resource": self.resource,
            "definition": self.definition,
            "createdAt": self.created_at,
            "modifiedAt": self.modified_at,
            "cursors": self.cursors,
            "runs": self.runs,
            "requests": self.requests,
        }

    @classmethod
    def from_json(cls, payload: Any) -> AutomationRecord:
        if not isinstance(payload, Mapping) or payload.get("format") != _FORMAT_VERSION:
            raise ValueError("not an automation record this version can read")
        resource = payload.get("resource")
        definition = payload.get("definition")
        if not isinstance(resource, str) or not isinstance(definition, dict):
            raise ValueError("automation record is missing its resource or definition")
        cursors = payload.get("cursors")
        runs = payload.get("runs")
        requests = payload.get("requests")
        return cls(
            resource=resource,
            definition=definition,
            created_at=str(payload.get("createdAt")),
            modified_at=str(payload.get("modifiedAt")),
            cursors={k: v for k, v in cursors.items() if isinstance(v, str)}
            if isinstance(cursors, dict)
            else {},
            runs=[run for run in runs if isinstance(run, dict)] if isinstance(runs, list) else [],
            requests={k: v for k, v in requests.items() if isinstance(v, str)}
            if isinstance(requests, dict)
            else {},
        )

    def run(self, uri: str) -> dict[str, Any] | None:
        return next((run for run in self.runs if run.get("resource") == uri), None)


class AutomationStore(Protocol):
    """Where automation records live between runs of the host."""

    async def load_all(self) -> Sequence[AutomationRecord]: ...

    async def save(self, record: AutomationRecord) -> None: ...

    async def delete(self, resource: str) -> None: ...


class InMemoryAutomationStore:
    """Keeps nothing past the process. For tests, and for an embedder that
    explicitly wants automations that vanish on restart."""

    def __init__(self) -> None:
        self.records: dict[str, dict[str, Any]] = {}

    async def load_all(self) -> Sequence[AutomationRecord]:
        return [AutomationRecord.from_json(copy.deepcopy(r)) for r in self.records.values()]

    async def save(self, record: AutomationRecord) -> None:
        self.records[record.resource] = copy.deepcopy(record.to_json())

    async def delete(self, resource: str) -> None:
        self.records.pop(resource, None)


class FileAutomationStore:
    """One JSON file per automation, named by a hash of its URI.

    The same rules as `FileSessionStore`, for the same reasons: the resource is
    client-chosen, so it never becomes a path; JSON only; atomic writes; an
    unreadable file is skipped rather than fatal; owner-only modes, because a
    definition's message is a prompt someone wrote.
    """

    def __init__(self, directory: Path) -> None:
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        with contextlib.suppress(OSError):
            os.chmod(self.directory, 0o700)
        self._writing = asyncio.Lock()

    def _path_for(self, resource: str) -> Path:
        digest = hashlib.sha256(resource.encode("utf-8", "surrogatepass")).hexdigest()
        return self.directory / f"{digest}.json"

    async def load_all(self) -> Sequence[AutomationRecord]:
        return await asyncio.to_thread(self._load_all)

    def _load_all(self) -> list[AutomationRecord]:
        records: list[AutomationRecord] = []
        for path in sorted(self.directory.glob("*.json")):
            try:
                records.append(
                    AutomationRecord.from_json(json.loads(path.read_text(encoding="utf-8")))
                )
            except (OSError, ValueError) as exc:
                _log.warning("skipping unreadable automation file %s: %s", path, exc)
        return records

    async def save(self, record: AutomationRecord) -> None:
        payload = json.dumps(record.to_json(), ensure_ascii=False)
        async with self._writing:
            await asyncio.to_thread(_write_atomically, self._path_for(record.resource), payload)

    async def delete(self, resource: str) -> None:
        async with self._writing:
            await asyncio.to_thread(self._path_for(resource).unlink, missing_ok=True)


# ─── when a schedule is due ─────────────────────────────────────────────────


def iso(instant: datetime) -> str:
    """ISO 8601 in UTC with a `Z`, the form `now_iso()` writes everywhere else."""
    return instant.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.") + (
        f"{instant.microsecond // 1000:03d}Z"
    )


def parse_iso(text: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _cursor(record: AutomationRecord, trigger_id: str) -> datetime:
    """Where a trigger's evaluation resumes: its cursor, else the record's
    last modification. Never "the beginning of time": a new automation must
    not catch up on every occurrence since 1970."""
    for text in (record.cursors.get(trigger_id), record.modified_at):
        if text is not None:
            parsed = parse_iso(text)
            if parsed is not None:
                return parsed
    return datetime.now(UTC)


@dataclass(frozen=True)
class Occurrence:
    """One run a schedule trigger asks for now."""

    trigger_id: str
    scheduled_for: datetime
    catch_up: bool


def due_occurrences(
    record: AutomationRecord, now: datetime
) -> tuple[list[Occurrence], dict[str, str]]:
    """The runs `record`'s schedules ask for at `now`, and the advanced cursors.

    Per trigger: an occurrence within `ON_TIME_GRACE` of now is on time and
    runs. Occurrences older than that were missed -- the host was down, asleep,
    or the automation was just re-enabled -- and `misfirePolicy` decides:
    `skip` drops them, `runOnce` ("Omission is equivalent to" it) collapses
    them into one catch-up run, which an on-time run also satisfies.
    """
    definition = record.definition
    if definition.get("enabled") is not True:
        return [], {}
    occurrences: list[Occurrence] = []
    cursors: dict[str, str] = {}
    triggers = definition.get("triggers")
    for trigger in triggers if isinstance(triggers, list) else ():
        if not isinstance(trigger, Mapping):
            continue
        parsed = _schedule_of(trigger)
        if parsed is None:
            continue
        cron, zone = parsed
        trigger_id = str(trigger.get("id"))
        first = cron.next_after(_cursor(record, trigger_id), zone)
        if first is None or first > now:
            continue
        cursors[trigger_id] = iso(now)
        on_time = cron.last_at_or_before(now, zone)
        if on_time is not None and on_time >= now - ON_TIME_GRACE and on_time >= first:
            occurrences.append(Occurrence(trigger_id, on_time, catch_up=False))
        elif trigger.get("misfirePolicy", "runOnce") == "runOnce":
            # Everything due is older than the grace: one catch-up run, for the
            # most recent occurrence missed.
            occurrences.append(Occurrence(trigger_id, on_time or first, catch_up=True))
    return occurrences, cursors


def next_run_at(record: AutomationRecord) -> datetime | None:
    """`AutomationEntry.nextRunAt`: "Earliest schedule occurrence awaiting
    evaluation ... It may be in the past while catch-up is pending."""
    if record.definition.get("enabled") is not True:
        return None
    earliest: datetime | None = None
    triggers = record.definition.get("triggers")
    for trigger in triggers if isinstance(triggers, list) else ():
        if not isinstance(trigger, Mapping):
            continue
        parsed = _schedule_of(trigger)
        if parsed is None:
            continue
        cron, zone = parsed
        upcoming = cron.next_after(_cursor(record, str(trigger.get("id"))), zone)
        if upcoming is not None and (earliest is None or upcoming < earliest):
            earliest = upcoming
    return earliest


def reset_cursors(record: AutomationRecord, now: datetime) -> None:
    """Forget what was missed. For a schedule that changed, or an automation
    that was just enabled: neither should catch up on occurrences it never had."""
    record.cursors = {}
    record.modified_at = iso(now)
