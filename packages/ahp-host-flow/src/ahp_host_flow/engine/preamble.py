"""The subagent preamble: a line of context put before the prompt an inner agent is sent."""

from __future__ import annotations

from typing import Final

DEFAULT_PREAMBLE: Final = (
    "You are one step of an automated workflow, and your final message is this step's return "
    "value: the workflow script receives it as data. Reply with the result itself, with no "
    "commentary, preamble or sign-off around it. When a structured result is requested, the "
    "structured result is the return value."
)


def preamble_text(setting: bool | str) -> str | None:
    """The text a provider's `preamble` setting stands for: `True` is the default text."""
    if isinstance(setting, str):
        return setting
    return DEFAULT_PREAMBLE if setting else None


def with_preamble(prompt: str, preamble: str | None) -> str:
    """*prompt* with *preamble* before it; unchanged when there is none."""
    return f"{preamble}\n\n{prompt}" if preamble else prompt
