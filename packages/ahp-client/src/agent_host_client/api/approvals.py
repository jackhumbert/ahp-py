"""Deciding whether a tool call runs.

The default is **manual**: the event surfaces and the caller answers. Silently
approving is the obvious wrong default; silently denying is the less obvious
one, because a denied call still ends the turn and the user never learns why.
So a drained stream under the manual default waits ``approval_timeout`` for an
answer from *somewhere* -- an ``async for`` consumer, another client, an
:class:`~agent_host_client.serve.InputResponder` -- and then raises
:class:`UnansweredToolCallError`, a loud, specific error (plan §7).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Collection, Mapping
from typing import TYPE_CHECKING, Any, TypeAlias

from agent_host_client.client.errors import AhpClientError

if TYPE_CHECKING:
    from agent_host_client.api.events import ToolCallReady

__all__ = [
    "ApprovalPolicy",
    "UnansweredToolCallError",
    "approve_all",
    "ask",
    "auto",
    "deny_all",
    "resolve_policy",
]

#: Returns True to approve. Async so a policy can ask a human.
ApprovalPolicy: TypeAlias = Callable[["ToolCallReady"], Awaitable[bool]]


class UnansweredToolCallError(AhpClientError):
    """Nobody answered a tool call the manual default surfaced.

    Raised by a drained :class:`~agent_host_client.api.TurnStream` after
    ``approval_timeout``, carrying the two facts a handler needs to say what
    was stuck. Both silent answers are worse: silently approving runs a tool
    the user never saw, and silently denying ends the turn with the user never
    learning why.
    """

    def __init__(self, tool_call_id: str, tool_name: str = "") -> None:
        named = f" ({tool_name})" if tool_name else ""
        super().__init__(
            f"tool call {tool_call_id}{named} was never answered: the manual default "
            "surfaces the event and waits; approve it from another surface, or pass "
            'approvals= ("all" / "none" / "reads" / auto(...)) to decide in-process'
        )
        self.tool_call_id = tool_call_id
        self.tool_name = tool_name


class ManualPolicy:
    """The default: surface, wait, and refuse **loudly** if nobody answers.

    A sentinel rather than a plain closure so :class:`TurnStream` can recognise
    it and wait on the mirror for an *external* answer instead of calling it.
    Called directly -- outside a stream, where there is nothing to wait on -- it
    raises immediately, because both silent answers are documented as worse.
    """

    async def __call__(self, call: ToolCallReady) -> bool:
        raise UnansweredToolCallError(call.tool_call_id, call.tool_name)


def ask() -> ApprovalPolicy:
    """The manual policy, spelled as a constructor like its siblings."""
    return ManualPolicy()


def approve_all() -> ApprovalPolicy:
    async def policy(_call: ToolCallReady) -> bool:
        return True

    return policy


def deny_all() -> ApprovalPolicy:
    async def policy(_call: ToolCallReady) -> bool:
        return False

    return policy


def auto(
    *,
    allow: Collection[str] = (),
    deny: Collection[str] = (),
    read_only: bool = False,
    otherwise: bool = False,
) -> ApprovalPolicy:
    """Approve by name, or by the tool's own read-only hint.

    Both facts are read from :attr:`ToolCallReady.tool_name` and
    :attr:`ToolCallReady.annotations`, which `event_for` resolves from state --
    **neither is on the action**. `chat/toolCallReady` carries no `toolName`,
    and `annotations` is a property of `ToolDefinition` alone. Reading them off
    the action, as this once did, makes every name in *allow* and *deny* match
    nothing and every hint absent, so every call falls through to *otherwise*:
    `approvals="reads"` then denies read-only tools, the exact opposite of its
    name, and the denial is indistinguishable from a deliberate one.

    `read_only=True` really does inspect `ToolAnnotations.readOnlyHint` and falls
    back to *otherwise* when the annotation is absent. `ahpx`'s equivalent
    silently degrades to prompting for everything, and its own documentation
    says otherwise -- a policy that quietly does something else is worse than
    one that is simply strict.
    """

    async def policy(call: ToolCallReady) -> bool:
        if call.tool_name in deny:
            return False
        if call.tool_name in allow:
            return True
        if read_only:
            annotations = call.annotations
            if isinstance(annotations, Mapping) and "readOnlyHint" in annotations:
                return bool(annotations["readOnlyHint"])
        return otherwise

    return policy


def resolve_policy(value: ApprovalPolicy | str | None) -> ApprovalPolicy:
    """Accept a policy, one of the string shorthands, or nothing."""
    if value is None or value in {"manual", "ask"}:
        # The plan §7 default: neither silent answer. A drained stream waits
        # `approval_timeout` for an external answer, then raises
        # `UnansweredToolCallError` -- a loud, specific error beats both.
        return ManualPolicy()
    if callable(value):
        return value
    if value == "all":
        return approve_all()
    if value == "none":
        return deny_all()
    if value == "reads":
        return auto(read_only=True)
    raise ValueError(f"unknown approval policy {value!r}")


if TYPE_CHECKING:
    _: Any = None
