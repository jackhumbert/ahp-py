"""An ACP agent's session config options and modes, as AHP session config.

ACP (v1, stable) has two ways for an agent to be configured per session:

- **config options** (`configOptions` on `session/new`, `session/resume` and
  `session/load`; changed with `session/set_config_option`, reported with
  `config_option_update`): `select` options (a value id from a list, flat or
  grouped) and `boolean` ones. Every report is the *complete* set.
- **modes** (`modes` on the same responses; `session/set_mode`,
  `current_mode_update`), which ACP keeps for older agents: "Clients that
  support config options SHOULD use `configOptions` exclusively and ignore
  `modes`". Here, `modes` are used only when an agent reports no config
  options at all, as one select property, :data:`MODE_PROPERTY`.

AHP session config is a JSON-schema object (`SessionConfigState`) whose
*schema* is fixed when the session is created -- `session/configChanged`
carries values only, and the reducer ignores it for a session created without
a schema. So the schema a session gets is built from what the agent reported
*before* it existed (the provider's :mod:`~ahp_host_acp.catalogue`), and this
module only turns either side's values into the other's.

Each ACP option becomes a property with the option's own id, so an AHP value
is always the ACP value: a value id string for a select, a boolean for a
boolean. Options of category ``model`` are left out wherever the agent already
has a model picker (`AgentInfo.models`), which switches them itself.
"""

from __future__ import annotations

import copy
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

#: The property legacy ACP modes are published as.
MODE_PROPERTY: Final = "mode"
#: `SessionConfigOptionCategory` for the model selector.
MODEL_CATEGORY: Final = "model"


@dataclass(frozen=True)
class Choice:
    """One value of a select option."""

    value: str
    name: str
    description: str | None = None


@dataclass(frozen=True)
class Option:
    """One configurable thing, as the agent reported it."""

    id: str
    name: str
    #: `select` or `boolean`; anything else is not offered.
    kind: str
    current: str | bool | None
    choices: tuple[Choice, ...] = ()
    category: str | None = None
    description: str | None = None
    #: True for the property synthesized from ACP `modes`, which is set with
    #: `session/set_mode` rather than `session/set_config_option`.
    legacy_mode: bool = False

    def schema(self, *, pinned: Any = None) -> dict[str, Any]:
        """The `SessionConfigPropertySchema` for this option.

        *pinned* is a value the host's operator fixed in the config file: it
        is the default, and the property is `readOnly` and not
        `sessionMutable`, since the adapter sets it on every session whatever
        a client asks for.
        """
        prop: dict[str, Any] = {"title": self.name}
        if self.description:
            prop["description"] = self.description
        default = pinned if pinned is not None else self.current
        if self.kind == "boolean":
            prop["type"] = "boolean"
            if isinstance(default, bool):
                prop["default"] = default
        else:
            prop["type"] = "string"
            choices = list(self.choices)
            # The schema's enum is what a client may pick, and the reducer-side
            # validator checks values against it: a current value the agent
            # did not list would otherwise be unselectable *and* unshowable.
            for extra in (self.current, pinned):
                if isinstance(extra, str) and all(c.value != extra for c in choices):
                    choices.append(Choice(extra, extra))
            prop["enum"] = [c.value for c in choices]
            prop["enumLabels"] = [c.name for c in choices]
            if any(c.description for c in choices):
                prop["enumDescriptions"] = [c.description or "" for c in choices]
            if isinstance(default, str):
                prop["default"] = default
        if pinned is not None:
            prop["readOnly"] = True
        else:
            prop["sessionMutable"] = True
        return prop


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _choices(raw: Any) -> tuple[Choice, ...]:
    """A select's options, flat or grouped (`SessionConfigSelectOptions`)."""
    found: list[Choice] = []
    for item in raw if isinstance(raw, list) else ():
        if not isinstance(item, Mapping):
            continue
        if isinstance(item.get("options"), list):  # a group
            found.extend(_choices(item["options"]))
        elif isinstance(value := item.get("value"), str):
            found.append(
                Choice(value, _text(item.get("name")) or value, _text(item.get("description")))
            )
    return tuple(found)


def parse_option(raw: Any) -> Option | None:
    """A `SessionConfigOption`, or None for one this adapter cannot offer."""
    if not isinstance(raw, Mapping):
        return None
    option_id = _text(raw.get("id"))
    kind = raw.get("type")
    if option_id is None or kind not in ("select", "boolean"):
        return None
    current = raw.get("currentValue")
    if kind == "boolean":
        current = current if isinstance(current, bool) else None
    else:
        current = current if isinstance(current, str) else None
    return Option(
        id=option_id,
        name=_text(raw.get("name")) or option_id,
        kind=str(kind),
        current=current,
        choices=_choices(raw.get("options")) if kind == "select" else (),
        category=_text(raw.get("category")),
        description=_text(raw.get("description")),
    )


def parse_modes(raw: Any) -> Option | None:
    """ACP `SessionModeState` as one select option."""
    if not isinstance(raw, Mapping):
        return None
    choices = tuple(
        Choice(mode["id"], _text(mode.get("name")) or mode["id"], _text(mode.get("description")))
        for mode in raw.get("availableModes") or ()
        if isinstance(mode, Mapping) and isinstance(mode.get("id"), str)
    )
    if not choices:
        return None
    current = raw.get("currentModeId")
    return Option(
        id=MODE_PROPERTY,
        name="Mode",
        kind="select",
        current=current if isinstance(current, str) else None,
        choices=choices,
        category="mode",
        legacy_mode=True,
    )


class AgentOptions:
    """What one agent (or one of its sessions) reported it can be configured with.

    Holds the raw ACP values so they can be kept and reloaded verbatim (see
    :class:`~ahp_host_acp.catalogue.Catalogue`); everything else is derived.
    """

    def __init__(
        self, config_options: Sequence[Any] | None = None, modes: Mapping[str, Any] | None = None
    ) -> None:
        self.config_options: list[Any] | None = (
            copy.deepcopy(list(config_options)) if config_options is not None else None
        )
        self.modes: dict[str, Any] | None = copy.deepcopy(dict(modes)) if modes else None

    # -- what the agent says --------------------------------------------------

    def note(self, result: Mapping[str, Any]) -> bool:
        """Take in a response's `configOptions` and `modes`, where present.

        Each is the complete set, so it replaces what was known. Returns
        whether anything changed.
        """
        before = self._snapshot()
        options = result.get("configOptions")
        if isinstance(options, list):
            self.config_options = copy.deepcopy(options)
        modes = result.get("modes")
        if isinstance(modes, Mapping):
            self.modes = copy.deepcopy(dict(modes))
        return self._snapshot() != before

    def note_update(self, update: Mapping[str, Any]) -> bool:
        """Take in a `config_option_update` or `current_mode_update`."""
        kind = update.get("sessionUpdate")
        if kind == "config_option_update":
            return self.note({"configOptions": update.get("configOptions")})
        if kind == "current_mode_update" and isinstance(mode := update.get("currentModeId"), str):
            before = self._snapshot()
            if self.modes is not None:
                self.modes["currentModeId"] = mode
            # An agent that reports both is meant to keep them in step; one
            # that only says the mode changed is believed for the option too.
            for raw in self.config_options or ():
                option = parse_option(raw)
                if (
                    isinstance(raw, dict)
                    and option is not None
                    and option.category == "mode"
                    and any(c.value == mode for c in option.choices)
                ):
                    raw["currentValue"] = mode
            return self._snapshot() != before
        return False

    def assume(self, key: str, value: Any) -> None:
        """Record a value the agent accepted without reporting the new state.

        `session/set_mode` answers `{}`; a lenient agent may answer
        `session/set_config_option` the same way.
        """
        option = self.get(key)
        if option is None:
            return
        if option.legacy_mode and self.modes is not None:
            self.modes["currentModeId"] = value
            return
        for raw in self.config_options or ():
            if isinstance(raw, dict) and raw.get("id") == key:
                raw["currentValue"] = value

    def _snapshot(self) -> tuple[list[Any] | None, dict[str, Any] | None]:
        return (copy.deepcopy(self.config_options), copy.deepcopy(self.modes))

    # -- derived ---------------------------------------------------------------

    @property
    def options(self) -> tuple[Option, ...]:
        """The options in force: config options, else the legacy modes.

        An empty `configOptions` list does not hide `modes`: an agent with no
        options gives a client nothing to use "exclusively".
        """
        parsed = tuple(
            option for raw in self.config_options or () if (option := parse_option(raw)) is not None
        )
        if parsed:
            return parsed
        mode = parse_modes(self.modes)
        return (mode,) if mode is not None else ()

    def get(self, key: str) -> Option | None:
        return next((option for option in self.options if option.id == key), None)

    def value_of(self, key: str) -> str | bool | None:
        option = self.get(key)
        return option.current if option is not None else None

    @property
    def model_option(self) -> Option | None:
        """The option of category `model`, which switches the agent's model."""
        return next((o for o in self.options if o.category == MODEL_CATEGORY), None)

    def configurable(self, *, with_model: bool) -> tuple[Option, ...]:
        """The options a session's config offers.

        *with_model* False leaves the model selector to the agent's model
        picker, so a client does not get two controls for one setting.
        """
        return tuple(o for o in self.options if with_model or o.category != MODEL_CATEGORY)

    def properties(
        self, *, with_model: bool, pinned: Mapping[str, Any]
    ) -> dict[str, dict[str, Any]]:
        """`SessionConfigSchema.properties`, in the agent's order (which ACP
        says is significant: "higher-priority options first")."""
        return {
            option.id: option.schema(pinned=coerce(option, pinned[option.id]))
            if option.id in pinned
            else option.schema()
            for option in self.configurable(with_model=with_model)
        }

    def request_for(self, key: str, value: Any) -> tuple[str, dict[str, Any]]:
        """The ACP request that sets *key* to *value* (less `sessionId`)."""
        option = self.get(key)
        if option is not None and option.legacy_mode:
            return "session/set_mode", {"modeId": str(value)}
        if option is not None:
            value = coerce(option, value)
        if isinstance(value, bool):
            # `SetSessionConfigOptionRequest`: a boolean carries `type`; a
            # value id is the default when `type` is absent.
            return "session/set_config_option", {"configId": key, "type": "boolean", "value": value}
        return "session/set_config_option", {"configId": key, "value": str(value)}


def coerce(option: Option, value: Any) -> Any:
    """A config-file value as the option's own type ("true" for a boolean)."""
    if option.kind == "boolean" and isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in ("true", "false"):
            return lowered == "true"
    return value


def same(a: Any, b: Any) -> bool:
    """Equal and the same JSON type: `True` is not `1`."""
    return type(a) is type(b) and a == b
