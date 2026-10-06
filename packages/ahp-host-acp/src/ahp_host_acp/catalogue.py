"""What an ACP agent reported last, kept across its sessions and restarts.

Two things a client sees are decided before any agent process is running:

- A session's **config schema** is fixed when the session is created (AHP
  `session/configChanged` carries values only), but this adapter starts the
  agent lazily, on the session's first turn, and only the agent knows its
  modes and options.
- The **model picker** (`AgentInfo.models` on the root channel) is published
  once, when the host starts.

So the provider keeps what the agent's most recent `session/new` said -- its
config options or modes with their starting values, and its models -- plus
its latest slash commands, each model's context window and the capabilities
its last `initialize` declared (whether it can fork a session), and, given a
file, keeps them across restarts. Only `session/new` updates the options: a
value changed later in one session is that session's, not the default a new
one starts with.

The file holds only what the agent itself reported. A stale entry (an agent
upgrade that dropped an option) costs one refused `session/set_config_option`,
logged, and is replaced by the next `session/new`.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from ahp_host_acp.commands import Command, parse_commands
from ahp_host_acp.options import AgentOptions

log = logging.getLogger(__name__)

_VERSION = 1


class Catalogue:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path
        self.options = AgentOptions()
        self._commands: list[Any] = []
        #: `models.availableModels` from agents built on ACP's session-model
        #: API, which ACP removed (0.13.5) in favour of a `model` option.
        self._legacy_models: list[Any] = []
        self._legacy_current: str | None = None
        self.context_windows: dict[str, int] = {}
        #: `agentCapabilities` from the agent's last `initialize`.
        self.capabilities: dict[str, Any] = {}
        self._saved: str | None = None
        if path is not None:
            self._load(path)

    # -- persistence -----------------------------------------------------------

    def _wire(self) -> dict[str, Any]:
        return {
            "version": _VERSION,
            "configOptions": self.options.config_options,
            "modes": self.options.modes,
            "commands": self._commands,
            "models": {
                "availableModels": self._legacy_models,
                "currentModelId": self._legacy_current,
            },
            "contextWindows": self.context_windows,
            "agentCapabilities": self.capabilities,
        }

    def _load(self, path: Path) -> None:
        try:
            text = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return
        except OSError as exc:
            log.warning("could not read %s: %s", path, exc)
            return
        try:
            data = json.loads(text)
        except ValueError:
            log.warning("%s is not JSON; starting without it", path)
            return
        if not isinstance(data, Mapping) or data.get("version") != _VERSION:
            return
        options = data.get("configOptions")
        modes = data.get("modes")
        self.options = AgentOptions(
            options if isinstance(options, list) else None,
            modes if isinstance(modes, Mapping) else None,
        )
        commands = data.get("commands")
        self._commands = list(commands) if isinstance(commands, list) else []
        self._note_legacy(data.get("models"))
        windows = data.get("contextWindows")
        if isinstance(windows, Mapping):
            self.context_windows = {
                str(k): v for k, v in windows.items() if isinstance(v, int) and v > 0
            }
        capabilities = data.get("agentCapabilities")
        self.capabilities = dict(capabilities) if isinstance(capabilities, Mapping) else {}
        self._saved = json.dumps(self._wire(), sort_keys=True)

    def save(self) -> None:
        """Write the file if anything changed. Failing to is logged, not fatal."""
        if self.path is None:
            return
        text = json.dumps(self._wire(), sort_keys=True)
        if text == self._saved:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, temp = tempfile.mkstemp(prefix=".agent-", dir=self.path.parent)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write(text)
                os.replace(temp, self.path)
            except BaseException:
                with contextlib.suppress(OSError):
                    os.unlink(temp)
                raise
        except OSError as exc:
            log.warning("could not save %s: %s", self.path, exc)
            return
        self._saved = text

    # -- what the agent said -----------------------------------------------------

    def remember_new_session(self, result: Mapping[str, Any]) -> None:
        """A `session/new` response: the options and models a session starts with."""
        changed = self.options.note(result)
        changed = self._note_legacy(result.get("models")) or changed
        if changed:
            self.save()

    def _note_legacy(self, raw: Any) -> bool:
        if not isinstance(raw, Mapping):
            return False
        before = (self._legacy_models, self._legacy_current)
        available = raw.get("availableModels")
        self._legacy_models = list(available) if isinstance(available, list) else []
        current = raw.get("currentModelId")
        self._legacy_current = current if isinstance(current, str) else None
        return (self._legacy_models, self._legacy_current) != before

    def remember_capabilities(self, raw: Any) -> bool:
        """An `initialize` answer's `agentCapabilities`. True if they changed."""
        capabilities = dict(raw) if isinstance(raw, Mapping) else {}
        if capabilities == self.capabilities:
            return False
        self.capabilities = capabilities
        self.save()
        return True

    def remember_commands(self, raw: Any) -> None:
        if isinstance(raw, list) and raw != self._commands:
            self._commands = list(raw)
            self.save()

    def remember_context_window(self, model: str | None, size: Any) -> None:
        if model is None or not isinstance(size, int) or isinstance(size, bool) or size <= 0:
            return
        if self.context_windows.get(model) != size:
            self.context_windows[model] = size
            self.save()

    # -- derived -----------------------------------------------------------------

    def session_capability(self, name: str) -> bool:
        """Whether `sessionCapabilities.<name>` is declared (``{}`` means yes)."""
        session = self.capabilities.get("sessionCapabilities")
        return isinstance(session, Mapping) and isinstance(session.get(name), Mapping)

    @property
    def commands(self) -> tuple[Command, ...]:
        return parse_commands(self._commands)

    def models(self) -> list[tuple[str, str]]:
        """The agent's models as `(id, name)`, its starting model first.

        From the `model` config option when there is one (ACP's current way),
        else the removed session-model API. First, because a client picks the
        first by default, and that must not switch every new session away
        from the model the agent would have used.
        """
        option = self.options.model_option
        if option is not None:
            found = [(c.value, c.name) for c in option.choices]
            current = option.current if isinstance(option.current, str) else None
        else:
            found = [
                (str(m["modelId"]), str(m.get("name") or m["modelId"]))
                for m in self._legacy_models
                if isinstance(m, Mapping) and isinstance(m.get("modelId"), str)
            ]
            current = self._legacy_current
        found.sort(key=lambda pair: pair[0] != current)  # stable: the rest keep their order
        return found
