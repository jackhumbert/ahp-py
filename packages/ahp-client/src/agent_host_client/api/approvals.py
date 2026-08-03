"""Deciding whether a tool call runs.

The default is **manual**: the event surfaces and the caller answers. Silently
approving is the obvious wrong default; silently denying is the less obvious
one, because a denied call still ends the turn and the user never learns why.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Collection, Mapping
from typing import TYPE_CHECKING, Any, TypeAlias

if TYPE_CHECKING:
    from agent_host_client.api.events import ToolCallReady

__all__ = ["ApprovalPolicy", "approve_all", "auto", "deny_all", "resolve_policy"]

#: Returns True to approve. Async so a policy can ask a human.
ApprovalPolicy: TypeAlias = Callable[["ToolCallReady"], Awaitable[bool]]


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
    if value is None or value == "manual":
        # Draining a stream with no policy would otherwise hang on the first
        # approval; refusing loudly beats waiting silently.
        return deny_all()
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
