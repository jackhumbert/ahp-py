"""Remote Control: the session, also on claude.ai and in the Claude apps.

Claude Code can publish a running session to the account it is signed in to,
where it can be read, driven and approved from a phone. Terminal sessions do it
when the user's `remoteControlAtStartup` setting is on; this makes sessions
started through the host do the same, so they are not stranded on the machine
that happens to run them.

Two consequences shape the rest of the adapter:

- Turns can start **elsewhere**. A message typed on the phone runs a turn the
  host never asked for; `provider.py` opens it as an external turn.
- Approvals are put to **both** sides. Whichever answers first wins; the CLI
  withdraws the other one (`control_cancel_request`), and the host is told
  through `TurnSink.tool_call_confirmed`.

The SDK has no method for this. The request is the one Claude Code's own IDE
hosts send (`remote_control`), over the SDK's control channel; it is kept to
this module so an SDK that changes shape breaks one place, and every caller
treats a failure as "no Remote Control" rather than "no session".
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final

from claude_agent_sdk import ClaudeSDKClient

#: The session config property: whether this session is also on claude.ai.
CONFIG_KEY: Final = "remoteControl"


@dataclass(frozen=True)
class Bridge:
    """A session's Remote Control identity: where to open it, and how to reattach."""

    session_url: str
    bridge_session_id: str


def auto_enable(server_info: Mapping[str, Any] | None) -> bool:
    """Whether Claude Code would turn Remote Control on at start-up.

    Its own verdict (`remote_control_auto_enable`), reported to hosts at
    initialisation: the user's `remoteControlAtStartup`, then any org policy.
    Absent - an older CLI - means off, which is what the CLI itself says.
    """
    return (server_info or {}).get("remote_control_auto_enable") is True


def bridge_of(reply: Mapping[str, Any] | None) -> Bridge | None:
    """The reply to an enabling request, or None if it is not one we understand."""
    reply = reply or {}
    url, bridge_id = reply.get("session_url"), reply.get("bridge_session_id")
    if isinstance(url, str) and url and isinstance(bridge_id, str) and bridge_id:
        return Bridge(session_url=url, bridge_session_id=bridge_id)
    return None


def property_schema(default: bool) -> Mapping[str, Any]:
    return {
        "type": "boolean",
        "title": "Remote Control",
        "description": (
            "Also show this session on claude.ai and in the Claude apps, where "
            "anyone signed in to this machine's Claude account can send it "
            "messages and approve its tool calls."
        ),
        "default": default,
        "sessionMutable": True,
    }


class RemoteControlClient(ClaudeSDKClient):
    """`ClaudeSDKClient`, plus the one control request the SDK does not wrap."""

    async def remote_control(
        self, enabled: bool, *, reattach: str | None = None
    ) -> Mapping[str, Any]:
        query = self._query
        if query is None:
            raise RuntimeError("not connected")
        request: dict[str, Any] = {
            "subtype": "remote_control",
            "enabled": enabled,
            # A host restart must not archive the claude.ai session: the
            # next start reattaches to it (`reattach`), so a phone keeps
            # the same conversation rather than finding a new one.
            "keep_session_on_exit": True,
        }
        if reattach is not None:
            request["reattach_session_id"] = reattach
        reply = await query._send_control_request(request)
        return reply if isinstance(reply, Mapping) else {}
