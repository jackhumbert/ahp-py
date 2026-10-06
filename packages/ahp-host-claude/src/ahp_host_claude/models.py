"""What each model in the picker can take: its context window and output limit.

A client needs these on `ModelInfo` - VS Code renders the usage meter against
``maxPromptTokens`` and refuses an image attachment without ``supportsVision``
- and Claude Code's model list (`get_server_info()["models"]`) carries none of
them. They are read from Claude Code anyway, not kept in a table here, because
they move: a model gains a 1M window, an account gets a different default.

* **The context window** comes from asking. At start-up the discovery probe
  switches its idle client to each model in turn (`set_model`) and asks
  `get_context_usage` (in its ``summary`` form, which answers from local
  estimates instead of making a token-count request per category):
  ``rawMaxTokens`` is the model's window and ``maxTokens`` the effective limit
  Claude Code works to, which is what a prompt can fill.
* **The output limit** Claude Code only reports after a model has answered,
  as ``maxOutputTokens`` in each result's `model_usage`. So it is learned:
  every session records what its results say, kept in
  ``<state_dir>/claude-models.json``, and the next start-up offers it. A model
  never used on this host has no output limit published, which is honest.
* **Vision** has no source at all, and needs none: every model Claude Code
  offers takes images, and this adapter sends a pasted image to whichever one
  is picked (`attachments.py`). So every entry says ``supportsVision``.

The picker is published once, when the host builds its root channel, so what a
session learns reaches clients at the next start.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

log = logging.getLogger(__name__)

__all__ = ["CACHE_FILE", "Learned", "Limits", "probe_limits"]

#: Under the state directory: limits learned from results, by model id.
CACHE_FILE: Final = "claude-models.json"
#: The whole probe may take this long before discovery gives up on limits.
PROBE_TIMEOUT: Final = 30.0
#: Claude Code's value for "whatever the account's default is".
_DEFAULT: Final = "default"


@dataclass(frozen=True)
class Limits:
    """One model's limits; any of them may be unknown."""

    context_window: int | None = None
    max_prompt: int | None = None
    max_output: int | None = None

    def merged(self, other: Limits) -> Limits:
        """These, with gaps filled from *other*."""
        return Limits(
            context_window=self.context_window
            if self.context_window is not None
            else other.context_window,
            max_prompt=self.max_prompt if self.max_prompt is not None else other.max_prompt,
            max_output=self.max_output if self.max_output is not None else other.max_output,
        )


def _int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


async def probe_limits(client: Any, entries: Sequence[Mapping[str, Any]]) -> dict[str, Limits]:
    """Each picker entry's context window, by its ``value``. Empty if it cannot ask.

    *client* is the discovery probe: connected, idle, about to be thrown away,
    so switching its model touches no session.
    """
    ask = getattr(client, "context_usage", None)
    if ask is None:
        return {}
    found: dict[str, Limits] = {}
    try:
        async with asyncio.timeout(PROBE_TIMEOUT):
            for entry in entries:
                value = entry.get("value")
                if not isinstance(value, str) or not value:
                    continue
                try:
                    await client.set_model(None if value == _DEFAULT else value)
                    usage = await ask()
                except Exception:
                    log.debug("no context limits for %s", value, exc_info=True)
                    continue
                if not isinstance(usage, Mapping):
                    continue
                window = _int(usage.get("rawMaxTokens"))
                effective = _int(usage.get("maxTokens"))
                if window is None and effective is None:
                    continue
                found[value] = Limits(
                    context_window=window if window is not None else effective,
                    max_prompt=effective if effective is not None else window,
                )
    except TimeoutError:
        log.warning("asking Claude Code for model limits took too long; some are missing")
    return found


class Learned:
    """Limits Claude Code reported in results, kept across restarts.

    Keyed by the model id a result's `model_usage` names - the id actually
    used, such as ``claude-opus-5-5`` - and matched to a picker entry by its
    ``value`` or its ``resolvedModel``. Never raises: a cache that cannot be
    read or written only means nothing was learned.
    """

    def __init__(self, path: Path | None) -> None:
        self.path = path
        self._limits: dict[str, Limits] = self._load()

    def _load(self) -> dict[str, Limits]:
        if self.path is None:
            return {}
        try:
            data = json.loads(self.path.read_text())
        except (OSError, ValueError):
            return {}
        if not isinstance(data, Mapping):
            return {}
        limits: dict[str, Limits] = {}
        for model, entry in data.items():
            if isinstance(model, str) and isinstance(entry, Mapping):
                limits[model] = Limits(
                    context_window=_int(entry.get("contextWindow")),
                    max_output=_int(entry.get("maxOutputTokens")),
                )
        return limits

    def _save(self) -> None:
        if self.path is None:
            return
        data = {
            model: {
                key: value
                for key, value in (
                    ("contextWindow", limits.context_window),
                    ("maxOutputTokens", limits.max_output),
                )
                if value is not None
            }
            for model, limits in sorted(self._limits.items())
        }
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(data, indent=1))
        except OSError:
            log.warning("could not save %s", self.path, exc_info=True)

    def record(self, model_usage: Any) -> bool:
        """Remember what one result's `model_usage` says about each model; whether it was new."""
        if not isinstance(model_usage, Mapping):
            return False
        changed = False
        for model, entry in model_usage.items():
            if not isinstance(model, str) or not isinstance(entry, Mapping):
                continue
            seen = Limits(
                context_window=_int(entry.get("contextWindow")),
                max_output=_int(entry.get("maxOutputTokens")),
            )
            if seen.context_window is None and seen.max_output is None:
                continue
            known = self._limits.get(model, Limits())
            updated = seen.merged(known)
            if updated != known:
                self._limits[model] = updated
                changed = True
        if changed:
            self._save()
        return changed

    def for_entry(self, entry: Mapping[str, Any]) -> Limits:
        """What was learned about a picker entry, under either of its names."""
        for key in (entry.get("value"), entry.get("resolvedModel")):
            if isinstance(key, str) and key in self._limits:
                return self._limits[key]
        return Limits()
