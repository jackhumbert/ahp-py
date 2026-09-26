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

import base64
from collections.abc import Mapping, Sequence
from typing import Any

from ahp_protocol.types import JsonObject

from ahp_client.client import actions, elicitation
from ahp_client.client.client import AhpClient
from ahp_client.client.mirror import StateMirror

__all__ = ["ClientToolHost", "InputResponder", "ToolExecutor", "pending_inputs"]

ToolExecutor = Any  # Callable[[ToolCallContext], Awaitable[Mapping[str, Any]]]


def pending_inputs(mirror: StateMirror, session_uri: str) -> list[JsonObject]:
    """``SessionState.inputNeeded`` -- everything waiting on a human."""
    state = mirror.state(session_uri)
    if not isinstance(state, Mapping):
        return []
    entries = state.get("inputNeeded")
    return [dict(e) for e in entries if isinstance(e, Mapping)] if isinstance(entries, list) else []


def _tool_call_ids(entry: Mapping[str, Any]) -> tuple[str, str]:
    """``(turnId, toolCallId)`` out of one ``inputNeeded`` entry.

    Both are on the entry by design -- "each entry is self-sufficient: it
    carries the owning `chat` URI plus every identifier needed to construct the
    response" -- and `turnId` is required on every tool-call action, which is
    the whole reason an answer can be given without subscribing to the chat.

    The id is nested under ``toolCall`` in the aggregate; a flat ``toolCallId``
    is also read because a caller answering something it saw on the chat channel
    has the call, not the request entry.
    """
    call = entry.get("toolCall")
    tool_call_id = call.get("toolCallId") if isinstance(call, Mapping) else None
    return (
        str(entry.get("turnId", "")),
        str(tool_call_id if tool_call_id is not None else entry.get("toolCallId", "")),
    )


def _request_id(entry: Mapping[str, Any]) -> str:
    """``request.id`` -- **not** the aggregate entry's own ``id``.

    The entry's `id` is "a stable key ... the host derives it however it likes
    (for example from the chat URI plus the underlying request or tool-call
    id); consumers MUST treat it as opaque", while the action is keyed by
    `ChatInputRequest.id`. A host that derives one from the other makes them
    look interchangeable right up to the host that does not.

    The two fallbacks are for a caller holding something other than the
    aggregate entry -- a request it watched arrive on the chat channel.
    """
    request = entry.get("request")
    if isinstance(request, Mapping) and request.get("id") is not None:
        return str(request["id"])
    return str(entry.get("requestId") or entry.get("id") or "")


def _questions_of(request: Any) -> list[JsonObject]:
    raw = request.get("questions") if isinstance(request, Mapping) else None
    return [q for q in raw if isinstance(q, dict)] if isinstance(raw, list) else []


class InputResponder:
    """Answers the four kinds of request an agent can block on."""

    def __init__(self, client: AhpClient, mirror: StateMirror) -> None:
        self._client = client
        self._mirror = mirror

    def answer(
        self,
        entry: Mapping[str, Any],
        *,
        answers: Mapping[str, Any] | None = None,
        response: elicitation.ResponseKind = "accept",
    ) -> None:
        """Resolve an elicitation with ``chat/inputCompleted``.

        This is the action that **ends** the request. ``chat/inputAnswerChanged``
        -- which this used to send, in a shape that action does not even have --
        only syncs a draft: the host stays parked on the future it opened, the
        chat stays at ``InputNeeded``, and the turn never finishes. Draft sync is
        :meth:`sync_draft`.

        There is no merge to do here. ``answers`` is an overlay: the reducer
        lays it over the drafts already on the request part, and the host then
        reads the merged map back out of reduced state rather than off this
        action -- "a user can answer one question on client A and another on
        client B" is arranged by the reducer, not by re-deriving it here.
        """
        self._client.dispatch(
            str(entry.get("chat", "")),
            elicitation.input_completed(
                _request_id(entry),
                response,
                elicitation.encode_answers(answers or {}, self._questions_for(entry)),
            ),
        )

    def sync_draft(
        self,
        entry: Mapping[str, Any],
        question_id: str,
        value: Any,
        *,
        state: elicitation.AnswerState = "draft",
    ) -> None:
        """Publish one question's answer, without resolving the request.

        The point of the action: a user answering question one on this client is
        visible to a second client rendering the same prompt. ``value=None``
        **clears** the draft, which is not the same as answering ``skipped`` --
        for that, pass the answer itself, ``{"state": "skipped"}``.
        """
        question = next(
            (q for q in self._questions_for(entry) if str(q.get("id", "")) == question_id), None
        )
        answer = None if value is None else elicitation.encode_answer(value, question, state=state)
        self._client.dispatch(
            str(entry.get("chat", "")),
            elicitation.input_answer_changed(_request_id(entry), question_id, answer),
        )

    def confirm_tool(
        self,
        entry: Mapping[str, Any],
        *,
        approved: bool,
        option_id: str | None = None,
        edited_tool_input: str | None = None,
        reason: actions.DenialReason = "denied",
        confirmed: actions.ConfirmationReason = "user-action",
    ) -> None:
        """Approve or refuse a tool call.

        A host arbitrates: the **first** answer wins and later ones come back
        with a ``rejectionReason``. That is not an error on our side -- another
        client got there first, and the mirror reverts our optimistic effect.
        """
        chat = str(entry.get("chat", ""))
        turn_id, tool_call_id = _tool_call_ids(entry)
        if approved:
            self._client.dispatch(
                chat,
                actions.tool_call_approved(
                    turn_id,
                    tool_call_id,
                    confirmed=confirmed,
                    selected_option_id=option_id,
                    edited_tool_input=edited_tool_input,
                ),
            )
            return
        self._client.dispatch(
            chat,
            actions.tool_call_denied(
                turn_id, tool_call_id, reason=reason, selected_option_id=option_id
            ),
        )

    def confirm_result(self, entry: Mapping[str, Any], *, approved: bool) -> None:
        """Answer ``requiresResultConfirmation``.

        Omitting this is what hangs a turn forever; `ahpx` does not implement it.
        """
        turn_id, tool_call_id = _tool_call_ids(entry)
        self._client.dispatch(
            str(entry.get("chat", "")),
            actions.tool_call_result_confirmed(turn_id, tool_call_id, approved=approved),
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

    def _questions_for(self, entry: Mapping[str, Any]) -> list[JsonObject]:
        """The questions an answer is encoded against.

        The aggregate entry mirrors the whole request, so it usually carries
        them. A caller that built an entry by hand from what it saw on the chat
        channel has only ids, and the encoding needs the *kinds* -- so the
        chat's own state is the fallback.
        """
        questions = _questions_of(entry.get("request"))
        if questions:
            return questions
        return _questions_of(self.open_request(str(entry.get("chat", "")), _request_id(entry)))

    def open_request(self, chat: str, request_id: str) -> JsonObject:
        """The live ``ChatInputRequest``, read from the chat's own state.

        There is no ``ChatState.inputRequests``; an open request lives on the
        active turn, as the ``request`` of the response part with
        ``kind: "inputRequest"`` and no ``response`` yet. That is where its
        ``questions`` are, and where the drafts every client has synced are, and
        it is the same place the host reads the final answers back out of.

        Public because a client answering from a session list holds the
        aggregate entry, which mirrors the request -- but one answering
        something it watched arrive on the chat channel holds only an id.
        """
        state = self._mirror.state(chat)
        active = state.get("activeTurn") if isinstance(state, Mapping) else None
        parts = active.get("responseParts") if isinstance(active, Mapping) else None
        for part in parts if isinstance(parts, list) else ():
            if not isinstance(part, Mapping) or part.get("kind") != "inputRequest":
                continue
            # `response` present means resolved, however it was resolved.
            request = part.get("request")
            if "response" in part or not isinstance(request, Mapping):
                continue
            if str(request.get("id", "")) == request_id:
                return dict(request)
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
        #: ``None`` for a tool that is advertised but has no executor.
        self._tools: dict[str, tuple[JsonObject, ToolExecutor | None]] = {}

    def register(self, definition: Mapping[str, Any], executor: ToolExecutor) -> None:
        self._tools[str(definition["name"])] = (dict(definition), executor)

    def advertise(self, definitions: Sequence[Mapping[str, Any]]) -> None:
        """Publish tools with **no** executor behind them.

        For the caller that wants the definitions on ``activeClient`` and
        intends to answer the calls itself. Every call still gets an answer --
        a denial -- because the failure this exists to avoid is the one where a
        tool is advertised and nothing anywhere responds: the host parks the
        turn on a future nobody resolves, and the agent waits forever.
        """
        for definition in definitions:
            self._tools[str(definition["name"])] = (dict(definition), None)

    def definitions(self) -> list[JsonObject]:
        """What goes on ``createSession.activeClient.tools``."""
        return [definition for definition, _ in self._tools.values()]

    def owns(self, action: Mapping[str, Any]) -> bool:
        """Whether this client is the one expected to execute the call.

        ``ToolCallClientContributor`` requires **both** ``kind`` and
        ``clientId`` ("the identified client is responsible for executing the
        tool"), so a kind=client contributor without one is malformed data,
        not a broadcast -- treating it as "whichever client is active, which is
        us" made every active client execute or deny the same call, and the
        host must then reject all the losers.
        """
        contributor = action.get("contributor")
        if not isinstance(contributor, Mapping) or contributor.get("kind") != "client":
            return False
        return contributor.get("clientId") == self._client_id

    async def execute(self, chat: str, action: Mapping[str, Any], *, tool_name: str = "") -> None:
        """Run one tool call and report the result.

        Both answers are scoped to the turn the call belongs to, taken off the
        action that asked for it. Without that the reducer no-ops while the host
        resolves the provider's future on ``toolCallId`` alone: the tool ran,
        the agent used its output, and every transcript shows the call cancelled
        as ``skipped``.

        *tool_name* is for the caller driving this from the action that actually
        hands execution over. ``chat/toolCallStart`` is the only action carrying
        ``toolName``; the ``chat/toolCallReady`` that moves the call to
        ``running`` -- and carries the final ``toolInput`` an executor needs --
        does not, so a caller reacting to that one has to resolve the name from
        the call's state and say so here. Reading it off the ready action yields
        ``""``, which denies every call as unregistered.
        """
        turn_id = str(action.get("turnId", ""))
        tool_call_id = str(action.get("toolCallId", ""))
        name = tool_name or str(action.get("toolName", ""))
        _definition, executor = self._tools.get(name) or (None, None)
        if executor is None:
            # Unknown *and* advertised-without-an-executor land here together:
            # from the agent's side they are the same event, a call this client
            # cannot run, and both must be answered rather than dropped.
            self._client.dispatch(chat, actions.tool_call_denied(turn_id, tool_call_id))
            return
        try:
            result: Mapping[str, Any] = await executor(await self._with_resolved_input(action))
        except Exception as exc:  # a failing tool is a result, not a crash
            result = actions.tool_failure_result(str(exc))
        self._client.dispatch(
            chat, actions.tool_call_complete(turn_id, tool_call_id, result=result)
        )

    async def _with_resolved_input(self, action: Mapping[str, Any]) -> Mapping[str, Any]:
        """Resolve a referenced ``toolInput`` before the executor sees it.

        ``ToolInput`` is ``string | ContentRef``: the referenced form is a
        ``{uri, ...}`` the host stores the real payload behind, and an executor
        handed it raw receives an address where it expects arguments. Fetched
        with the forward ``resourceRead`` **fresh per invocation and never
        cached across confirmation** (plan §8) -- for referenced input the host
        replaces the resource contents on an edited approval, so a cached copy
        is precisely the pre-edit input the user rejected.

        A read that fails raises, and :meth:`execute` reports it as the tool's
        failure result: the input could not be obtained, so the call cannot
        have run.
        """
        tool_input = action.get("toolInput")
        if not isinstance(tool_input, Mapping) or not isinstance(tool_input.get("uri"), str):
            return action
        result = await self._client.resource_read(str(tool_input["uri"]))
        data = result.get("data")
        text = data if isinstance(data, str) else ""
        if result.get("encoding") == "base64":
            text = base64.b64decode(text, validate=True).decode("utf-8")
        return {**action, "toolInput": text}

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
