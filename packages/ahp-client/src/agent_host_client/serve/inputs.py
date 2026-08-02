"""Answering the agent: elicitation, tool confirmation, and client-owned tools.

These are the surfaces no reference consumer implements, and the omission is not
cosmetic. `ahpx` ignores ``chat/toolCallResultConfirmed`` entirely, which hangs
any turn using ``requiresResultConfirmation`` **forever** -- the agent is waiting
on an answer nothing will ever send.

Everything here is dispatched as an ordinary ``chat/*`` action to the chat the
request names, taken from ``SessionState.inputNeeded``. That aggregate exists so
a client can answer without subscribing to the chat, which is the whole point:
a session list can resolve a prompt without opening the conversation.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Literal

from agent_host_protocol.types import JsonObject

from agent_host_client.client.client import AhpClient
from agent_host_client.client.mirror import StateMirror

__all__ = ["ClientToolHost", "InputResponder", "ToolExecutor", "pending_inputs"]

ToolExecutor = Any  # Callable[[ToolCallContext], Awaitable[Mapping[str, Any]]]


def pending_inputs(mirror: StateMirror, session_uri: str) -> list[JsonObject]:
    """``SessionState.inputNeeded`` -- everything waiting on a human."""
    state = mirror.state(session_uri)
    if not isinstance(state, Mapping):
        return []
    entries = state.get("inputNeeded")
    return [dict(e) for e in entries if isinstance(e, Mapping)] if isinstance(entries, list) else []


class InputResponder:
    """Answers the four kinds of request an agent can block on."""

    def __init__(self, client: AhpClient, mirror: StateMirror) -> None:
        self._client = client
        self._mirror = mirror

    def answer(
        self,
        entry: Mapping[str, Any],
        *,
        answers: Mapping[str, Any],
        kind: Literal["accept", "decline", "cancel"] = "accept",
    ) -> None:
        """Answer an elicitation.

        ``chat/inputAnswerChanged`` **merges** per-question answers rather than
        replacing them, so the current map is read from the mirror and overlaid.
        Sending only our own answers would clobber a partial answer another
        client is midway through giving.
        """
        chat = str(entry.get("chat", ""))
        request_id = entry.get("requestId") or entry.get("id")
        existing = self._existing_answers(chat, str(request_id))
        self._client.dispatch(
            chat,
            {
                "type": "chat/inputAnswerChanged",
                "requestId": request_id,
                "answers": {**existing, **dict(answers)},
                "kind": kind,
            },
        )

    def confirm_tool(
        self,
        entry: Mapping[str, Any],
        *,
        approved: bool,
        option_id: str | None = None,
        edited_tool_input: Any = None,
        reason: Literal["denied", "skipped"] | None = None,
    ) -> None:
        """Approve or refuse a tool call.

        A host arbitrates: the **first** answer wins and later ones come back
        with a ``rejectionReason``. That is not an error on our side -- another
        client got there first, and the mirror reverts our optimistic effect.
        """
        action: JsonObject = {
            "type": "chat/toolCallConfirmed",
            "toolCallId": entry.get("toolCallId"),
            "approved": approved,
        }
        if option_id is not None:
            action["optionId"] = option_id
        if edited_tool_input is not None:
            action["editedToolInput"] = edited_tool_input
        if reason is not None:
            action["reason"] = reason
        self._client.dispatch(str(entry.get("chat", "")), action)

    def confirm_result(self, entry: Mapping[str, Any], *, approved: bool) -> None:
        """Answer ``requiresResultConfirmation``.

        Omitting this is what hangs a turn forever; `ahpx` does not implement it.
        """
        self._client.dispatch(
            str(entry.get("chat", "")),
            {
                "type": "chat/toolCallResultConfirmed",
                "toolCallId": entry.get("toolCallId"),
                "approved": approved,
            },
        )

    async def authenticate(
        self, entry: Mapping[str, Any], *, token: str, scopes: Sequence[str] = ()
    ) -> None:
        """The one input that is **not** a chat action.

        ``kind == "toolAuthentication"`` is resolved by pushing a token for the
        resource the tool call names, through the `authenticate` command --
        dispatching a chat action here would leave the agent blocked.
        """
        tool_call = entry.get("toolCall")
        auth = tool_call.get("auth") if isinstance(tool_call, Mapping) else None
        resource = auth.get("resource") if isinstance(auth, Mapping) else None
        await self._client.authenticate(
            resource=str(resource or ""), token=token, scopes=list(scopes)
        )

    def _existing_answers(self, chat: str, request_id: str) -> JsonObject:
        state = self._mirror.state(chat)
        if not isinstance(state, Mapping):
            return {}
        for request in state.get("inputRequests") or []:
            if isinstance(request, Mapping) and str(request.get("requestId")) == request_id:
                answers = request.get("answers")
                return dict(answers) if isinstance(answers, Mapping) else {}
        return {}


class ClientToolHost:
    """Executes tool calls the host marked as ours.

    ``contributor: {"kind": "client", "clientId": …}`` means the agent asked for
    a tool only this client can run -- the editor's own tools, with no
    filesystem API on the host at all. An unrecognised name is **denied**, never
    ignored: a call nobody answers blocks the turn.
    """

    def __init__(self, client: AhpClient, *, client_id: str) -> None:
        self._client = client
        self._client_id = client_id
        self._tools: dict[str, tuple[JsonObject, ToolExecutor]] = {}

    def register(self, definition: Mapping[str, Any], executor: ToolExecutor) -> None:
        self._tools[str(definition["name"])] = (dict(definition), executor)

    def definitions(self) -> list[JsonObject]:
        """What goes on ``createSession.activeClient.tools``."""
        return [definition for definition, _ in self._tools.values()]

    def owns(self, action: Mapping[str, Any]) -> bool:
        contributor = action.get("contributor")
        if not isinstance(contributor, Mapping) or contributor.get("kind") != "client":
            return False
        owner = contributor.get("clientId")
        # An absent clientId means "whichever client is active", which is us.
        return owner is None or owner == self._client_id

    async def execute(self, chat: str, action: Mapping[str, Any]) -> None:
        """Run one tool call and report the result."""
        tool_call_id = action.get("toolCallId")
        name = str(action.get("toolName", ""))
        entry = self._tools.get(name)
        if entry is None:
            self._client.dispatch(
                chat,
                {
                    "type": "chat/toolCallConfirmed",
                    "toolCallId": tool_call_id,
                    "approved": False,
                    "reason": "denied",
                },
            )
            return
        _definition, executor = entry
        try:
            result = await executor(action)
        except Exception as exc:  # a failing tool is a result, not a crash
            result = {"isError": True, "content": [{"kind": "text", "text": str(exc)}]}
        self._client.dispatch(
            chat,
            {
                "type": "chat/toolCallComplete",
                "toolCallId": tool_call_id,
                "result": result,
            },
        )

    def attach_action(self, tools: Sequence[Mapping[str, Any]] | None = None) -> JsonObject:
        """``session/activeClientSet``.

        A full-entry upsert keyed by ``clientId``; there is no tools-only
        action, so re-registering means resending the whole entry.
        """
        return {
            "type": "session/activeClientSet",
            "activeClient": {
                "clientId": self._client_id,
                "tools": list(tools) if tools is not None else self.definitions(),
            },
        }

    def detach_action(self) -> JsonObject:
        """``session/activeClientRemoved`` -- which **is** client-dispatchable.

        Verified against the generated table, contradicting the prose that says
        a client never unsets itself.
        """
        return {"type": "session/activeClientRemoved", "clientId": self._client_id}
