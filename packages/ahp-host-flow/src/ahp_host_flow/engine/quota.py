"""Per-user quotas: tokens and cost per day and per run, kept in a file (plan M16)."""

from __future__ import annotations

import json
import logging
import os
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

from ahp_host_flow.engine.numbers import money as _money
from ahp_host_flow.engine.numbers import whole as _whole

_log = logging.getLogger(__name__)

#: The ledger's file name, under the state dir.
QUOTA_FILE: Final = "quotas.json"
_VERSION: Final = 1


@dataclass(frozen=True)
class QuotaLimits:
    """Per-principal caps; None: no cap. Tokens are output plus input."""

    tokens_per_day: int | None = None
    tokens_per_run: int | None = None
    cost_per_day: float | None = None
    cost_per_run: float | None = None

    @property
    def any(self) -> bool:
        return any(
            v is not None
            for v in (
                self.tokens_per_day,
                self.tokens_per_run,
                self.cost_per_day,
                self.cost_per_run,
            )
        )


def _utc_today() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d")


class QuotaLedger:
    """What each principal has used today, persisted to *path* (None: memory only).

    A run with no principal is charged under the key '': anonymous runs share one bucket.
    """

    def __init__(
        self,
        path: Path | None,
        limits: QuotaLimits,
        *,
        today: Callable[[], str] = _utc_today,
    ) -> None:
        self._path = path
        self.limits = limits
        self._today = today
        # key -> (day, tokens, cost_usd)
        self._days: dict[str, tuple[str, int, float]] = {}
        self._load()

    def _load(self) -> None:
        if self._path is None:
            return
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            principals = data["principals"]
            if data.get("version") != _VERSION or not isinstance(principals, dict):
                raise ValueError("unknown layout")
            days = {
                key: (str(entry["day"]), _whole(entry.get("tokens")), _money(entry.get("costUsd")))
                for key, entry in principals.items()
            }
        except FileNotFoundError:
            return
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
            _log.warning("ignoring the unreadable quota file %s: %s", self._path, exc)
            return
        self._days = days

    def _save(self) -> None:
        if self._path is None:
            return
        data = {
            "version": _VERSION,
            "principals": {
                key: {"day": day, "tokens": tokens, "costUsd": cost}
                for key, (day, tokens, cost) in self._days.items()
            },
        }
        tmp = self._path.with_name(f"{self._path.name}.{uuid.uuid4().hex}.tmp")
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(data), encoding="utf-8")
            os.replace(tmp, self._path)
        except OSError as exc:
            _log.warning("cannot write the quota file %s: %s", self._path, exc)
            tmp.unlink(missing_ok=True)

    def day_used(self, key: str) -> tuple[int, float]:
        """Tokens and cost *key* used today (a stale day counts as 0)."""
        day, tokens, cost = self._days.get(key, ("", 0, 0.0))
        return (tokens, cost) if day == self._today() else (0, 0.0)

    def add(self, key: str, tokens: int, cost: float) -> None:
        used_tokens, used_cost = self.day_used(key)
        self._days[key] = (self._today(), used_tokens + tokens, used_cost + cost)
        self._save()

    def for_run(self, principal: str | None) -> RunQuota:
        return RunQuota(self, principal or "")


class RunQuota:
    """One run's view of the ledger: its own totals plus its principal's day."""

    def __init__(self, ledger: QuotaLedger, key: str) -> None:
        self._ledger = ledger
        self._key = key
        self._tokens = 0
        self._cost = 0.0

    def check(
        self, extra_tokens: int = 0, extra_cost: float = 0.0, *, live: bool = False
    ) -> str | None:
        """The first limit reached, as text, else None.

        *extra_* is usage not yet charged. Reached means used >= limit at admission and
        used > limit when *live*. The text names no principal and no other run.
        """
        limits = self._ledger.limits
        day_tokens, day_cost = self._ledger.day_used(self._key)
        rows: tuple[tuple[str, float, float | None], ...] = (
            ("tokens_per_run", self._tokens + extra_tokens, limits.tokens_per_run),
            ("tokens_per_day", day_tokens + extra_tokens, limits.tokens_per_day),
            ("cost_per_run", self._cost + extra_cost, limits.cost_per_run),
            ("cost_per_day", day_cost + extra_cost, limits.cost_per_day),
        )
        for name, used, limit in rows:
            if limit is None or not (used > limit if live else used >= limit):
                continue
            if name.startswith("cost"):
                return f"quota {name} reached: ${used:.4f} of ${limit:.4f}"
            return f"quota {name} reached: {int(used)} of {int(limit)}"
        return None

    def charge(self, tokens: int, cost: float) -> None:
        """Add a finished call's usage to this run and to today, and save the ledger."""
        tokens, cost = _whole(tokens), _money(cost)
        self._tokens += tokens
        self._cost += cost
        self._ledger.add(self._key, tokens, cost)
