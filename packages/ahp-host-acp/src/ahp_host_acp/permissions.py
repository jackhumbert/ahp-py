"""ACP permission options <-> AHP confirmation options. The approval policy.

ACP's `session/request_permission` (stable) offers `options`, each an
`optionId`, a `name` and a `kind` -- `allow_once`, `allow_always`,
`reject_once`, `reject_always` -- and is answered with the `optionId` picked
(or `cancelled`). AHP 1.0.0 offers the same thing to a client as
`ConfirmationOption`s (`id`, `label`, `kind: approve | deny`, `group`), and
the host checks that what comes back is one of them and agrees with the
approve/deny answer.

**The policy, and it is a security decision:** the user is offered the
agent's options as the agent worded them, *allow always* included, and the
agent gets exactly the one the user picked. Nothing here ever picks for the
user. A client that answers with a plain approve or deny rather than an option
gets the narrowest reading: *allow once* or *reject once*, and if the agent
offered no such option, `cancelled` -- never an *always* the user did not
choose, since that would widen (or fix) the agent's policy in its own state,
where this host can neither see nor undo it.

The one exception is a call to a tool the client offered through the MCP relay
(`relayed_answer`): its owner decides, so it is allowed once without asking.

What does not reach the agent: a denial's reason (`reasonMessage`) and a
suggestion of what to do instead (`userSuggestion`). ACP's outcome has a field
for the option and none for either -- only `_meta`, which "implementations
MUST NOT make assumptions about" -- so there is no honest way to tell the
agent why. The host keeps both in the transcript, where the user sees them.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from typing import Any, Final

from ahp_host.provider.base import ConfirmationOption, ToolConfirmationOutcome

log = logging.getLogger(__name__)

#: ACP kind -> (AHP kind, the group a client may divide on, a fallback label).
_KINDS: Final[Mapping[str, tuple[str, int, str]]] = {
    "allow_once": ("approve", 1, "Allow once"),
    "allow_always": ("approve", 1, "Always allow"),
    "reject_once": ("deny", 2, "Reject"),
    "reject_always": ("deny", 2, "Always reject"),
}

CANCELLED: Final = {"outcome": {"outcome": "cancelled"}}


def acp_options(raw: Any) -> list[Mapping[str, Any]]:
    """The agent's `PermissionOption`s this adapter can offer, in its order.

    One with an unknown `kind` is left out: nothing says whether picking it
    would allow or reject, and the host must know which.
    """
    offered: list[Mapping[str, Any]] = []
    seen: set[str] = set()
    for option in raw if isinstance(raw, list) else ():
        if not isinstance(option, Mapping):
            continue
        option_id, kind = option.get("optionId"), option.get("kind")
        if isinstance(option_id, str) and option_id and kind in _KINDS and option_id not in seen:
            seen.add(option_id)
            offered.append(option)
    return offered


def confirmation_options(offered: Sequence[Mapping[str, Any]]) -> tuple[ConfirmationOption, ...]:
    """The agent's options as a client renders them, ids unchanged."""
    choices = []
    for option in offered:
        kind, group, fallback = _KINDS[str(option["kind"])]
        name = option.get("name")
        label = name.strip() if isinstance(name, str) and name.strip() else fallback
        choices.append(
            ConfirmationOption(id=str(option["optionId"]), label=label, kind=kind, group=group)
        )
    return tuple(choices)


def answer(
    offered: Sequence[Mapping[str, Any]], outcome: ToolConfirmationOutcome
) -> dict[str, Any]:
    """The `RequestPermissionOutcome` for what the user said."""
    if outcome.reason_message or outcome.user_suggestion is not None:
        log.info("the agent cannot be told why the call was declined: ACP has no field for it")
    picked = outcome.selected_option_id
    if picked is not None:
        for option in offered:
            if option["optionId"] == picked:
                approves = _KINDS[str(option["kind"])][0] == "approve"
                if approves == outcome.approved:
                    return {"outcome": {"outcome": "selected", "optionId": picked}}
        # The host checks both; an answer that still disagrees grants nothing.
        log.warning("a confirmation picked %r, which this call did not offer as such", picked)
        return CANCELLED
    wanted = "allow_once" if outcome.approved else "reject_once"
    for option in offered:
        if option["kind"] == wanted:
            return {"outcome": {"outcome": "selected", "optionId": option["optionId"]}}
    return CANCELLED


def relayed_answer(offered: Sequence[Mapping[str, Any]]) -> dict[str, Any] | None:
    """The answer that lets one call to a relayed tool through, else None.

    **A security decision.** A tool the client offered (or the host's own
    per-turn tool) runs in that client or in the host, not on this machine, so
    its owner decides whether it runs, as `ahp-host-claude` does. Asking the
    user as well would only ask twice. The agent's first *allow once* option is
    picked; never *allow always*, which would widen the agent's own stored
    policy. Without an *allow once* option the user is asked, as for any call.

    The harness decides which calls it asks about, but the *model* often
    chooses a call's title, so a title is not proof. `provider._is_relayed`
    therefore also requires an MCP-shaped kind and a raw input shaped like the
    tool's (`client_tools.looks_relayed`) before this is used.
    """
    for option in offered:
        if option["kind"] == "allow_once":
            return {"outcome": {"outcome": "selected", "optionId": option["optionId"]}}
    return None
