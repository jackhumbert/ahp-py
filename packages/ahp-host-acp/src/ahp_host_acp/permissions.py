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

**The host's tool policy** (`[policy]`, `tool_request`, `rejected_answer`) is
advisory here and comes first: a call it denies is answered with the agent's
reject option without asking anyone, and one it marks *ask* is never let
through by the relayed shortcut. An agent in its own allow-always mode never
asks, so this cannot see its calls.

The one exception is a call to a tool the client offered through the MCP relay
(`relayed_answer`): its owner decides, so it is allowed once without asking.

What does not reach the agent in the outcome: a denial's reason
(`reasonMessage`) and a suggestion of what to do instead (`userSuggestion`).
ACP's outcome has a field for the option and none for either -- only `_meta`,
which "implementations MUST NOT make assumptions about" -- so there is no
honest way to tell the agent why there. The host keeps both in the transcript,
where the user sees them, and (`decline_feedback`) sends them as a text block
on the chat's next prompt: `decline_line`.

**Confirmation hints are security decisions too.** `read_only_hint` says
whether a call only reads, from the same kinds the policy uses, so an automatic
answerer may trust it: only `read` and `search` are read-only; every other
kind, unknown ones included, is not claimed to be.
"""

from __future__ import annotations

import logging
import shlex
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Final

from ahp_acp import CANCELLED as _CANCELLED
from ahp_acp import allow_once, reject_once
from ahp_host.provider.base import ConfirmationOption, ToolConfirmationOutcome
from ahp_host.provider.policy import ToolRequest

log = logging.getLogger(__name__)

#: ACP kind -> (AHP kind, the group a client may divide on, a fallback label).
_KINDS: Final[Mapping[str, tuple[str, int, str]]] = {
    "allow_once": ("approve", 1, "Allow once"),
    "allow_always": ("approve", 1, "Always allow"),
    "reject_once": ("deny", 2, "Reject"),
    "reject_always": ("deny", 2, "Always reject"),
}

#: A `session/request_permission` result that grants nothing: `ahp_acp.CANCELLED`
#: as the response's `outcome`.
CANCELLED: Final = {"outcome": dict(_CANCELLED)}


def _selected(outcome: Mapping[str, Any]) -> dict[str, Any] | None:
    """*outcome* (`ahp_acp.allow_once`/`reject_once`) as a response, None if it picked nothing."""
    return {"outcome": outcome} if outcome.get("outcome") == "selected" else None


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
    pick = allow_once if outcome.approved else reject_once
    return _selected(pick(offered)) or CANCELLED


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
    return _selected(allow_once(offered))


#: ACP tool kind -> the policy's kind; any other kind is a plain `tool`.
#: Pinned by a test: a kind moved here changes what the policy checks.
POLICY_KINDS: Final[Mapping[str, str]] = {
    "read": "read",
    "search": "read",
    "edit": "write",
    "delete": "write",
    "move": "write",
    "execute": "command",
}

#: Policy kind -> whether a call of that kind only reads. A `tool` (any other
#: ACP kind) is absent: nothing is claimed about it. Security-relevant, pinned
#: by a test.
READ_ONLY_BY_POLICY_KIND: Final[Mapping[str, bool]] = {
    "read": True,
    "write": False,
    "command": False,
}


def read_only_hint(kind: str) -> bool | None:
    """Whether an ACP tool *kind* only reads: True, False, or None when not known.

    **A security decision:** an automatic answerer may approve on `True`, so only
    the kinds the policy treats as reads (`read`, `search`) say so.
    """
    return READ_ONLY_BY_POLICY_KIND.get(POLICY_KINDS.get(kind, "tool"))


#: The longest decline reason sent to the agent, in characters; chosen to keep
#: the block short, no external source.
DECLINE_REASON_MAX: Final = 300


def decline_line(tool: str, reason: str) -> str:
    """One line telling the agent why *tool* was declined, whitespace collapsed and capped."""
    text = " ".join(reason.split())
    if len(text) > DECLINE_REASON_MAX:
        text = text[: DECLINE_REASON_MAX - 1] + "…"
    return f"[declined: {tool}] {text}"


#: `rawInput` keys that name a path, for agents that send no `locations`.
RAW_PATH_KEYS: Final = ("path", "file_path", "filePath", "abs_path", "absolute_path", "destination")


def _command_of(raw: Mapping[str, Any]) -> str:
    command = raw.get("command")
    if isinstance(command, str):
        args = raw.get("args")
        if isinstance(args, list) and all(isinstance(a, str) for a in args):
            return shlex.join([command, *args])
        return command
    if isinstance(command, list) and all(isinstance(a, str) for a in command):
        return shlex.join(command)
    return ""


def _paths_of(update: Mapping[str, Any], raw: Mapping[str, Any]) -> tuple[str, ...]:
    found: list[str] = []
    locations = update.get("locations")
    for location in locations if isinstance(locations, list) else ():
        if isinstance(location, Mapping) and isinstance(location.get("path"), str):
            found.append(location["path"])
    content = update.get("content")
    for item in content if isinstance(content, list) else ():
        if (
            isinstance(item, Mapping)
            and item.get("type") == "diff"
            and isinstance(item.get("path"), str)
        ):
            found.append(item["path"])
    found.extend(raw[key] for key in RAW_PATH_KEYS if isinstance(raw.get(key), str))
    return tuple(dict.fromkeys(found))


def tool_request(update: Mapping[str, Any], *, cwd: Path, roots: tuple[Path, ...]) -> ToolRequest:
    """An ACP tool call as the host's :class:`ToolPolicy` judges it."""
    title = update.get("title")
    name = title if isinstance(title, str) and title else str(update.get("toolCallId", ""))
    raw_input = update.get("rawInput")
    raw = raw_input if isinstance(raw_input, Mapping) else {}
    kind = POLICY_KINDS.get(str(update.get("kind")), "tool")
    tool_input: Mapping[str, Any] = {}
    if kind == "command":
        tool_input = {"command": _command_of(raw)}
    elif kind in ("read", "write"):
        tool_input = {"paths": _paths_of(update, raw)}
    return ToolRequest(name=name, kind=kind, input=tool_input, cwd=cwd, roots=roots, raw=raw)


def rejected_answer(offered: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """The agent's own reject option, *once* before *always*, else `cancelled`."""
    if (once := _selected(reject_once(offered))) is not None:
        return once
    for option in offered:
        if option["kind"] == "reject_always":
            return {"outcome": {"outcome": "selected", "optionId": option["optionId"]}}
    return CANCELLED
