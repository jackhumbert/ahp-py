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
    """`ClaudeSDKClient`, plus the control requests the SDK does not wrap.

    Three, all Claude Code's own: `remote_control`; `file_suggestions`, its
    ``@`` file index; and `get_context_usage` in its ``summary`` form, which
    the SDK's `get_context_usage()` cannot ask for (it always asks for the
    full breakdown, a token-count request per category). Each one's request
    and reply shape is read from the CLI bundled with the SDK, and a caller
    treats a failure as "not available" rather than as a broken session.
    """

    async def _control(self, request: dict[str, Any]) -> Mapping[str, Any]:
        query = self._query
        if query is None:
            raise RuntimeError("not connected")
        reply = await query._send_control_request(request)
        return reply if isinstance(reply, Mapping) else {}

    async def context_usage(self) -> Mapping[str, Any]:
        """`get_context_usage` from local estimates: no token-count requests.

        What the context window is (``rawMaxTokens``, ``maxTokens``) does not
        depend on counting, so asking for it should not cost a request per
        category, for every model in the picker, at every start-up.
        """
        return await self._control({"subtype": "get_context_usage", "detail": "summary"})

    async def file_suggestions(self, query: str) -> Mapping[str, Any]:
        """Claude Code's ``@`` completions for *query*: ``suggestions`` and ``cwd``.

        The index is built in the background when the process starts, so the
        first answers can be empty; a client asks again as the user types.
        """
        return await self._control({"subtype": "file_suggestions", "query": query})

    async def remote_control(
        self, enabled: bool, *, reattach: str | None = None, keep: bool = True
    ) -> Mapping[str, Any]:
        """Turn Remote Control on or off.

        `keep` is fixed when it is turned on, for as long as it stays on: a
        kept session is never archived, not when the process exits and not
        when it is turned off. That is what a restart needs (the next start
        reattaches to it); a deletion needs the opposite, so it turns it off,
        back on unkept, and off again (`ClaudeSession.disposed`).
        """
        request: dict[str, Any] = {
            "subtype": "remote_control",
            "enabled": enabled,
            "keep_session_on_exit": keep,
        }
        if reattach is not None:
            request["reattach_session_id"] = reattach
        return await self._control(request)
