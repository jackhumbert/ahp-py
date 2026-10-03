"""How much effort Claude puts into a reply: a session setting, `effort`.

Claude Code takes it at start-up (`--effort`, the SDK's `ClaudeAgentOptions.effort`)
and has no call to change it on a running client, so a change mid-session
restarts Claude on the same conversation, as a changed folder does. `default`
leaves it to Claude Code (the model's own default). Which levels a model takes
is in its `supportedEffortLevels` metadata on the agent's model list; a client
can offer only those. A level the model does not take is Claude Code's to
refuse or round, not this host's.
"""

from __future__ import annotations

from typing import Any, Final

CONFIG_KEY: Final = "effort"
DEFAULT: Final = "default"
LEVELS: Final = ("low", "medium", "high", "xhigh", "max")

PROPERTY: Final = {
    "type": "string",
    "title": "Effort",
    "description": (
        "How much effort Claude puts into a reply. Default: the model's own. "
        "Higher levels think longer and use more of your plan's allowance."
    ),
    "enum": [DEFAULT, *LEVELS],
    "enumLabels": ["Default", "Low", "Medium", "High", "Extra high", "Max"],
    "default": DEFAULT,
    # Changeable during a session: Claude restarts on the same conversation.
    "sessionMutable": True,
}


def effort_level(value: Any) -> str:
    """A client's `effort` value, or `default` for anything else."""
    return value if isinstance(value, str) and value in LEVELS else DEFAULT


def sdk_effort(level: str) -> str | None:
    """What the SDK takes: no value for `default`."""
    return None if level == DEFAULT else level
