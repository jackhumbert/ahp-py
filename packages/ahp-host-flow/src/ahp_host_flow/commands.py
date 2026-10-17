"""Parse the message that starts a run."""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

from ahp_host_flow.config import SOURCES
from ahp_host_flow.engine import WORKFLOW_NAME
from ahp_host_flow.sandbox import NO_ARGS

_FENCE = re.compile(
    r"\A\s*```(?:js|javascript)[ \t]*\r?\n(?P<body>.*?)\r?\n```[ \t]*(?P<rest>.*)\Z",
    re.DOTALL,
)

HELP: Final = (
    "Start a workflow run with `/<name> [json args]`, `/workflow <name> [json args]`, "
    "or a message that starts with a ```js block holding a script "
    "(JSON args may follow the block)."
)


@dataclass(frozen=True)
class Help:
    """The message does not start a run."""


@dataclass(frozen=True)
class BadRequest:
    """The message tried to start a run and got it wrong."""

    message: str


@dataclass(frozen=True)
class StartRun:
    """Start a run of a named workflow or an inline script (exactly one is set)."""

    name: str | None
    source: str | None
    script: str | None
    args: object


def _args(rest: str) -> object:
    rest = rest.strip()
    if not rest:
        return NO_ARGS
    try:
        return json.loads(rest)
    except ValueError as exc:
        raise _BadArgsError(f"Arguments must be JSON: {exc}") from exc


class _BadArgsError(Exception):
    pass


def _split(text: str) -> tuple[str, str]:
    parts = text.split(None, 1)
    return parts[0], (parts[1] if len(parts) > 1 else "")


def parse_message(text: str) -> Help | BadRequest | StartRun:
    """Classify the text of a user message."""
    try:
        fence = _FENCE.match(text)
        if fence:
            return StartRun(None, None, fence.group("body"), _args(fence.group("rest")))
        stripped = text.strip()
        if not stripped.startswith("/"):
            return Help()
        head, remainder = _split(stripped)
        if head == "/workflow":
            if not remainder.strip():
                return BadRequest("Usage: /workflow <name> [json args]")
            ref, rest = _split(remainder.strip())
        else:
            ref, rest = head[1:], remainder
        source: str | None = None
        name = ref
        prefix, sep, tail = ref.partition(":")
        if sep:
            if prefix not in SOURCES or not WORKFLOW_NAME.fullmatch(tail):
                return BadRequest(f"Not a workflow name: {ref!r}")
            source, name = prefix, tail
        elif not WORKFLOW_NAME.fullmatch(ref):
            return BadRequest(f"Not a workflow name: {ref!r}")
        return StartRun(name, source, None, _args(rest))
    except _BadArgsError as exc:
        return BadRequest(str(exc))


def _suffix(names: Sequence[str]) -> str:
    if names:
        return "\n\nWorkflows: " + ", ".join(f"/{n}" for n in names)
    return "\n\nNo workflows found."


def help_text(names: Sequence[str]) -> str:
    """The reply to a message that does not start a run."""
    return HELP + _suffix(names)


def unknown_text(ref: str, names: Sequence[str]) -> str:
    """The reply to a run request that names no known workflow."""
    return f"Unknown workflow: /{ref}." + _suffix(names)
