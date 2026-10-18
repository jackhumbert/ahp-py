"""Removes the subagent preamble from what a test fake is sent; not a test module.

The default `preamble = true` puts `DEFAULT_PREAMBLE` before every prompt an inner agent
gets, and the fakes script by prompt text. They read the message through `stripped`.
"""

from __future__ import annotations

from dataclasses import replace

from ahp_host.provider.base import UserMessage

from ahp_host_flow.engine import DEFAULT_PREAMBLE


def strip_preamble(text: str) -> str:
    """*text* without a leading preamble; anything else unchanged."""
    return text.removeprefix(DEFAULT_PREAMBLE + "\n\n")


def stripped(message: UserMessage) -> UserMessage:
    """*message* with the preamble taken off its text and its raw text."""
    raw = message.raw
    text = raw.get("text") if raw else None
    if isinstance(text, str):
        raw = {**raw, "text": strip_preamble(text)}
    return replace(message, text=strip_preamble(message.text), raw=raw)
