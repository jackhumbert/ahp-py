"""The agent event mapper: neutral provider events -> AHP chat actions.

This is the layer ADR 0003 exists to create. Providers report what the agent did
(`text_delta`, `tool_call_started`, ...) and this translates that into the
protocol's action vocabulary, so an adapter never encodes protocol ordering
rules and survives a spec bump untouched.

The ordering it guarantees is pinned by conformance fixture
``161-chat-turn-lifecycle-on-chat.json``: a markdown ``chat/responsePart`` must
exist before any ``chat/delta`` targets it. An adapter emitting raw actions has
to know that; here it is impossible to get wrong.
"""

from __future__ import annotations

import copy
import json
import logging
import time
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

from ahp_protocol.types import IS_CLIENT_DISPATCHABLE

from ahp_host.core.changesets import ContentStore, file_edit
from ahp_host.core.pending import PendingRequest, PendingRequests, RequestOutcome
from ahp_host.core.sequencer import Sequencer
from ahp_host.provider.base import (
    AgentSession,
    AttachedChat,
    AuthChallenge,
    ClientToolCall,
    InputOutcome,
    InputRequest,
    ModelSelection,
    ResumesTurns,
    ToolConfirmation,
    ToolConfirmationOutcome,
    ToolResult,
    UserMessage,
)
from ahp_host.provider.changes import FileChange

__all__ = [
    "ActionTurnSink",
    "TurnRunner",
    "attached_chats",
    "markdown_text",
    "tool_call_dispatch_rejection",
    "turn_scope",
    "user_message",
]

_log = logging.getLogger(__name__)

#: Called after the sink changes something the *session summary* projects, so
#: the host can re-mirror it. A turn's ordinary output is deliberately not
#: mirrored per action -- that would be one root notification per token -- but
#: activity changes at most once per tool call, and the session list is the only
#: place it renders.
SessionChanged = Callable[[], Awaitable[None]]

#: A reduced tool call's status while it waits for approval.
_PENDING_CONFIRMATION: Final = "pending-confirmation"


#: The tool-call actions a client may originate. DERIVED from the generated
#: table rather than listed, so a tool-call action a future spec makes
#: client-dispatchable is validated the day it appears instead of the day
#: somebody remembers this constant. The rest of the family is server-only and
#: never arrives through `dispatchAction` at all.
_TOOL_CALL_ACTIONS: Final = frozenset(
    action
    for action, dispatchable in IS_CLIENT_DISPATCHABLE.items()
    if dispatchable and action.startswith("chat/toolCall")
)


def tool_call_dispatch_rejection(action: Mapping[str, Any]) -> str | None:
    """A `rejectionReason` for a malformed tool-call action, or ``None``.

    The client-dispatchable tool-call actions are the ones where a missing
    REQUIRED field does damage instead of nothing: every reducer keys on
    `turnId`, so a confirmation without one is fanned out to every subscriber
    and then applied by nobody -- while the host, which resolves the parked
    request by `toolCallId` alone, runs the tool anyway. State says the call was
    never confirmed; the tool has already run. An approval missing `confirmed`
    lands as `confirmed: null` ("nobody was asked"), and a denial missing
    `reason` cancels a call with no reason to show for it.

    Presence and JSON type only, never enum membership: a peer may speak a newer
    spec than we do, and refusing an unknown `confirmed` reason would break
    against exactly the client we most want to keep working (invariant 4).

    Required lists quoted verbatim from the vendored `actions.schema.json`.
    """
    action_type = action.get("type")
    if action_type not in _TOOL_CALL_ACTIONS:
        return None
    # `ToolCallActionBase.required: ["turnId", "toolCallId"]`, both `string`.
    for name in ("turnId", "toolCallId"):
        if not isinstance(action.get(name), str):
            return f"{action_type} needs a string {name!r}"

    if action_type == "chat/toolCallComplete":
        # `ChatToolCallCompleteAction.required: [..., "result"]`.
        if not isinstance(action.get("result"), Mapping):
            return f"{action_type} needs a result object"
        return None

    if action_type == "chat/toolCallContentChanged":
        # `ChatToolCallContentChangedAction.required: [..., "content"]`.
        if not isinstance(action.get("content"), list):
            return f"{action_type} needs a content array"
        return None

    # Both remaining actions are keyed on `approved`, and its ABSENCE is not a
    # denial: the reducer reads it with JS truthiness, so a missing key silently
    # becomes "denied" for one peer and "approved" for the host that asked.
    if "approved" not in action:
        return f"{action_type} needs an 'approved'"
    if action_type == "chat/toolCallResultConfirmed":
        return None

    # `ChatToolCallApprovedAction.required: [..., "approved", "confirmed"]`;
    # `ChatToolCallDeniedAction.required: [..., "approved", "reason"]`. Which of
    # the two a frame is gets decided by `approved`'s truthiness, because that
    # is how the reducer decides it.
    required = "confirmed" if action.get("approved") else "reason"
    if not isinstance(action.get(required), str):
        return f"{action_type} needs a string {required!r}"
    return None


@dataclass
class _StreamingCall:
    """What the sink has said about a call that is still in `streaming`.

    Kept because `chat/toolCallReady` is where a call's *final* input and its
    invocation message live, and the reducer takes both FROM THAT ACTION when a
    `streaming` call moves on: `invocationMessage: action.invocationMessage` and
    `toolInput: action.toolInput ?? null`. A ready that omits them therefore
    stores nulls over everything the provider streamed, rather than leaving it
    alone -- so the ready has to restate it, which means remembering it.
    """

    display_name: str
    intention: str | None = None
    tool_input: Any = None
    invocation_message: str | None = None
    #: Parameters streamed through `chat/toolCallDelta`. The reducer accumulates
    #: these into `partialInput` and then DROPS that key at ready, so this is the
    #: final input for a provider that only ever streamed one.
    partial_input: str = ""


def _input_key(key: str) -> str:
    """The pending-registry key an `InputRequest.key` is parked under."""
    return f"input:{key}"


def turn_scope(channel: str, turn_id: str) -> str:
    """The lifetime a suspended request is bound to (ADR 0005).

    The turn, not the session and not the connection: a cancelled turn must free
    everything waiting under it, and the connection that started the turn is
    frequently not the one that answers.
    """
    return f"{channel}#{turn_id}"


class ActionTurnSink:
    """A :class:`~ahp_host.provider.base.TurnSink` that publishes actions."""

    def __init__(
        self,
        sequencer: Sequencer,
        channel: str,
        turn_id: str,
        pending: PendingRequests | None = None,
        session_uri: str | None = None,
        session_changed: SessionChanged | None = None,
        advertise_resource: Callable[[str], None] | None = None,
        *,
        content: ContentStore | None = None,
        resumable: bool = False,
    ) -> None:
        self._sequencer = sequencer
        self._channel = channel
        self._turn_id = turn_id
        self._pending = pending if pending is not None else PendingRequests()
        #: Where `file_edit` and confirmation previews park their bytes: the
        #: session's store, so `resourceRead` serves them under the session's
        #: visibility. A bare sink gets a private one, which nothing serves --
        #: fine for a test, and the refs are still well-formed.
        self._content = content if content is not None else ContentStore()
        #: Whether the agent running this turn can answer `chat/turnResume`
        #: (`ResumesTurns`). Without it a `resumable` error part would offer
        #: users a button that does nothing, so `turn_failed` drops the flag.
        self._resumable = resumable
        #: Where `session/inputNeeded` entries are mirrored. Optional so a bare
        #: sink stays constructible in a test without a session around it.
        self._session_uri = session_uri
        self._session_changed = session_changed
        #: Tells the host a `chat/toolCallAuthRequired` is about to name a
        #: protected resource. "Servers MUST accept any `resource` value they
        #: have themselves advertised" -- including ones advertised only
        #: through a live challenge -- so the host has to hear about the
        #: advertisement, not just the park.
        self._advertise_resource = advertise_resource
        self._markdown_part_id: str | None = None
        self._reasoning_part_id: str | None = None
        self._segment: str | None = None
        #: Calls still in `streaming`, i.e. announced but never moved on by a
        #: `chat/toolCallReady`, and what was published about each of them. See
        #: `_ensure_runnable`.
        self._streaming: dict[str, _StreamingCall] = {}

    @property
    def turn_id(self) -> str:
        """The turn this sink publishes into (`IdentifiesTurn`). Read-only."""
        return self._turn_id

    @property
    def chat_uri(self) -> str:
        """The chat that turn runs on. Read-only."""
        return self._channel

    async def steered(self, text: str) -> None:
        """Note in the transcript that the user steered this turn.

        A `systemNotification` part marked ``_meta.steering`` (clients that
        don't know the key still show the text), and a segment boundary, so
        whatever the agent says next starts below it rather than continuing a
        part above.
        """
        await self.system_notification(text, meta={"steering": True})

    async def system_notification(
        self, text: str, *, markdown: bool = False, meta: Mapping[str, Any] | None = None
    ) -> None:
        """Publish a `SystemNotificationResponsePart` into the running turn.

        Exactly the declared shape -- `kind`, `content`, optional `_meta` --
        and no `id`: the part has none (`channels-chat/state.ts`
        SystemNotificationResponsePart), and nothing targets it afterwards,
        unlike a markdown or reasoning part that deltas append to. The steering
        note used to carry an invented one.
        """
        part: dict[str, Any] = {
            "kind": "systemNotification",
            # `StringOrMarkdown`: "A plain `string` is rendered as-is (no
            # Markdown processing)", `{markdown}` is rendered as Markdown.
            "content": {"markdown": text} if markdown else text,
        }
        if meta is not None:
            part["_meta"] = dict(meta)
        await self._sequencer.publish(
            self._channel,
            {"type": "chat/responsePart", "turnId": self._turn_id, "part": part},
        )
        self._segment = None
        self._markdown_part_id = None
        self._reasoning_part_id = None

    async def file_edit(self, change: FileChange) -> dict[str, Any]:
        """`ToolResultFileEditContent` for *change*, its bytes in the session store.

        The content refs are what make it a diff rather than a label: the
        client fetches each side with `resourceRead`, which the host answers
        from this store under the owning session's visibility -- the same path
        a changeset's files take. Publishes nothing; the provider places the
        returned item in a result's `content`.
        """
        return {"type": "fileEdit", **file_edit(change, self._content)}

    def _open_segment(self, kind: str) -> None:
        """Start a new response part when the kind of output changes.

        The client renders parts in the order they were created, and appends a
        delta to whichever part its id names -- so one markdown part per turn
        put prose written *after* a tool call above it, out of order with the
        thing it was commenting on. A run of the same kind still shares a part;
        only the switch is a boundary.
        """
        if kind == self._segment:
            return
        self._segment = kind
        self._markdown_part_id = None
        self._reasoning_part_id = None

    async def _ensure_markdown_part(self) -> str:
        self._open_segment("markdown")
        if self._markdown_part_id is None:
            self._markdown_part_id = f"md-{uuid.uuid4()}"
            await self._sequencer.publish(
                self._channel,
                {
                    "type": "chat/responsePart",
                    "turnId": self._turn_id,
                    "part": {
                        "kind": "markdown",
                        "id": self._markdown_part_id,
                        "content": "",
                    },
                },
            )
        return self._markdown_part_id

    async def text_delta(self, text: str) -> None:
        part_id = await self._ensure_markdown_part()
        await self._sequencer.publish(
            self._channel,
            {
                "type": "chat/delta",
                "turnId": self._turn_id,
                "partId": part_id,
                "content": text,
            },
        )

    async def reasoning_delta(self, text: str) -> None:
        self._open_segment("reasoning")
        if self._reasoning_part_id is None:
            self._reasoning_part_id = f"re-{uuid.uuid4()}"
            await self._sequencer.publish(
                self._channel,
                {
                    "type": "chat/responsePart",
                    "turnId": self._turn_id,
                    "part": {
                        "kind": "reasoning",
                        "id": self._reasoning_part_id,
                        "content": "",
                    },
                },
            )
        await self._sequencer.publish(
            self._channel,
            {
                "type": "chat/reasoning",
                "turnId": self._turn_id,
                "partId": self._reasoning_part_id,
                "content": text,
            },
        )

    async def tool_call_started(
        self,
        call_id: str,
        name: str,
        tool_input: Any = None,
        *,
        display_name: str | None = None,
        intention: str | None = None,
        meta: Mapping[str, Any] | None = None,
    ) -> None:
        # `chat/toolCallStart` creates its own toolCall response part; emitting
        # an extra `chat/responsePart` for it would duplicate the part.
        self._open_segment("tool")
        action: dict[str, Any] = {
            "type": "chat/toolCallStart",
            "turnId": self._turn_id,
            "toolCallId": call_id,
            "toolName": name,
            # REQUIRED and non-optional in the action type. A call that sits in
            # `streaming` for any length of time -- which is what
            # `chat/toolCallDelta` exists for -- renders unlabelled without it,
            # and the client drops it from its tool-label map entirely.
            "displayName": display_name or name,
        }
        if intention is not None:
            action["intention"] = intention
        if meta:
            action["_meta"] = dict(meta)
        # NOT `toolInput`. `ChatToolCallStartAction` declares `toolName`,
        # `displayName`, `intention` and `contributor` and nothing else -- the
        # input belongs on `chat/toolCallReady` ("Final tool input"), and the
        # reducer builds the streaming state from the declared fields only. So
        # every input published here was dropped on the floor, and no
        # non-confirming tool -- including the host's own `!command` -- ever
        # showed one. Held until the ready instead.
        await self._sequencer.publish(self._channel, action)
        self._streaming[call_id] = _StreamingCall(
            display_name=action["displayName"], intention=intention, tool_input=tool_input
        )
        await self.set_activity(action["displayName"])

    async def _ensure_runnable(self, call_id: str) -> None:
        """Move a call out of `streaming` before anything tries to finish it.

        `chat/toolCallStart` leaves a call in `streaming`, and the validation
        table only accepts `chat/toolCallComplete` from `running`,
        `pendingConfirmation` or `authRequired` -- so the simplest possible
        provider, which announces a call and then completes it, had its
        completion SILENTLY DROPPED and the call cancelled when the turn ended.
        The reducer is right and fixture-verified; what was missing is the
        transition, which the confirm and client-tool paths happened to publish
        for their own reasons and nothing else did.

        `confirmed: "not-needed"` is the spec's own wording for a call that
        needs no approval: it "transitions directly to `running`".
        """
        draft = self._streaming.pop(call_id, None)
        if draft is None:
            return
        action: dict[str, Any] = {
            "type": "chat/toolCallReady",
            "turnId": self._turn_id,
            "toolCallId": call_id,
            # REQUIRED by `ChatToolCallReadyAction`, and this frame published
            # neither it nor `toolInput` -- so a call that was auto-confirmed had
            # both nulled by the reducer at the moment it started running, and
            # everything the provider had streamed about it went with them.
            # `intention` is the honest second choice: it is the provider's own
            # sentence about what this invocation is for.
            "invocationMessage": draft.invocation_message
            or draft.intention
            or f"Running {draft.display_name}",
            "confirmed": "not-needed",
        }
        # `is None`, never truthiness: `{}` and `[]` are inputs a provider can
        # legitimately have proposed, and both are falsy in Python (invariant 5).
        final_input = draft.tool_input
        if final_input is None and draft.partial_input != "":
            final_input = draft.partial_input
        if final_input is not None:
            action["toolInput"] = _encoded_tool_input(final_input)
        await self._sequencer.publish(self._channel, action)

    async def tool_call_delta(
        self,
        call_id: str,
        content: str | None = None,
        *,
        invocation_message: str | None = None,
        meta: Mapping[str, Any] | None = None,
    ) -> None:
        """Stream a tool call's parameters, or update its progress line.

        Without this a call that takes a while renders as one static row and
        then everything at once. `content` appends to the parameters as the
        model produces them; `invocation_message` replaces the line under the
        tool's name, which is where progress belongs -- it is a *message*, not
        an appended log.

        `chat/toolCallDelta` only reaches a call that is still `streaming` --
        the reducer's updater returns the call untouched from any other status,
        and fixture 095 pins that. Parameters can only stream before the call is
        ready, so `content` after that point is dropped exactly as upstream
        drops it; a *progress message* still has somewhere to go, and goes
        there. Publishing the delta regardless is what made every progress line
        the shipped `EchoProvider(confirm_tools=True)` emits disappear.
        """
        draft = self._streaming.get(call_id)
        if draft is None:
            if invocation_message is not None:
                await self._update_running_invocation(call_id, invocation_message, meta)
            return
        action: dict[str, Any] = {
            "type": "chat/toolCallDelta",
            "turnId": self._turn_id,
            "toolCallId": call_id,
        }
        if content is not None:
            action["content"] = content
            draft.partial_input += content
        if invocation_message is not None:
            action["invocationMessage"] = invocation_message
            draft.invocation_message = invocation_message
        if meta:
            action["_meta"] = dict(meta)
        await self._sequencer.publish(self._channel, action)

    async def _update_running_invocation(
        self, call_id: str, invocation_message: str, meta: Mapping[str, Any] | None
    ) -> None:
        """Move the progress line of a call that is already `running`.

        A second `chat/toolCallReady` is the only action that reaches a running
        call's `invocationMessage`, and it keeps the call running only when it
        carries `confirmed` -- without it the reducer sends the call back to
        `pendingConfirmation`, i.e. asks the user to approve a tool that is
        already executing.
        """
        tool_call = self._tool_call_state(call_id)
        if tool_call is None or tool_call.get("status") != "running":
            # Nothing the reducer would accept, so nothing is published: a frame
            # it drops still costs a `serverSeq` and a fan-out to every client.
            return
        # RESTATED from state, not invented: writing `not-needed` over a call the
        # user explicitly approved would record that nobody was asked, which is
        # the one thing this field is read for. The test is truthiness because
        # the reducer's is (`if confirmed:`) -- an empty string there would send
        # a running call back for approval -- and for a string enum truthiness
        # means the same thing in both languages.
        confirmed = tool_call.get("confirmed")
        ready: dict[str, Any] = {
            "type": "chat/toolCallReady",
            "turnId": self._turn_id,
            "toolCallId": call_id,
            "invocationMessage": invocation_message,
            "confirmed": confirmed if confirmed else "not-needed",
        }
        if meta:
            ready["_meta"] = dict(meta)
        await self._sequencer.publish(self._channel, ready)
        content = tool_call.get("content")
        if content is not None:
            # The ready transition REBUILDS the call from its base fields, and
            # `content` is not one of them -- so a progress update silently
            # erases everything `tool_call_output` has streamed so far. Put it
            # back rather than leaving live output to vanish mid-run.
            await self._sequencer.publish(
                self._channel,
                {
                    "type": "chat/toolCallContentChanged",
                    "turnId": self._turn_id,
                    "toolCallId": call_id,
                    "content": content,
                },
            )

    def _tool_call_state(self, call_id: str) -> Mapping[str, Any] | None:
        """The reduced tool call, i.e. what every client's mirror holds for it.

        Read rather than remembered: the fields this is consulted for --
        `status` and `confirmed` -- are the ones a *client* writes, so the
        sink's own bookkeeping cannot know them.
        """
        state = self._sequencer.state_of(self._channel)
        active = state.get("activeTurn") if isinstance(state, Mapping) else None
        if not isinstance(active, Mapping) or active.get("id") != self._turn_id:
            return None
        parts = active.get("responseParts")
        for part in parts if isinstance(parts, list) else []:
            tool_call = part.get("toolCall") if isinstance(part, Mapping) else None
            if isinstance(tool_call, Mapping) and tool_call.get("toolCallId") == call_id:
                return tool_call
        return None

    async def tool_call_output(
        self,
        call_id: str,
        content: Sequence[Mapping[str, Any]],
        *,
        meta: Mapping[str, Any] | None = None,
    ) -> None:
        """Show what a still-running tool has produced so far.

        REPLACES the running call's content rather than appending to it -- the
        action is `contentChanged`, and the reducer assigns. A caller streaming
        a command's output passes everything so far each time.

        `meta` is where the well-known `ptyTerminal` key goes: "a `ptyTerminal`
        key with `{ input: string; output: string }` indicates the tool operated
        on a terminal", which is what makes a client render the terminal widget
        instead of a plain row.
        """
        # A tool that produces output IS running, and `contentChanged` reaches
        # only a `running` call. Without this the plain path -- announce, stream,
        # complete -- stayed in `streaming` until completion, so every partial
        # this method exists to publish was dropped by the reducer and the live
        # feedback appeared for the first time as the finished result.
        await self._ensure_runnable(call_id)
        action: dict[str, Any] = {
            "type": "chat/toolCallContentChanged",
            "turnId": self._turn_id,
            "toolCallId": call_id,
            "content": [dict(item) for item in content],
        }
        if meta:
            action["_meta"] = dict(meta)
        await self._sequencer.publish(self._channel, action)

    async def usage(
        self,
        *,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        cache_read_tokens: int | None = None,
        model: str | None = None,
        meta: Mapping[str, Any] | None = None,
    ) -> None:
        """Report the turn's token usage.

        The client's rule for the context gauge is literally "no usage, no
        gauge" -- it renders nothing at all rather than a zero -- so a host that
        never publishes this has a UI element that does not exist as far as its
        users can tell.

        Every field is optional because a provider that knows only some of them
        should send what it has. Sending none of them is still meaningful: it
        says a turn happened and the numbers are unknown.
        """
        usage: dict[str, Any] = {}
        for key, value in (
            ("inputTokens", input_tokens),
            ("outputTokens", output_tokens),
            ("cacheReadTokens", cache_read_tokens),
            ("model", model),
        ):
            if value is not None:
                usage[key] = value
        if meta:
            usage["_meta"] = dict(meta)
        await self._sequencer.publish(
            self._channel,
            {"type": "chat/usage", "turnId": self._turn_id, "usage": usage},
        )

    async def tool_call_completed(
        self,
        call_id: str,
        result: Any = None,
        *,
        success: bool = True,
        past_tense_message: str | None = None,
    ) -> None:
        action: dict[str, Any] = {
            "type": "chat/toolCallComplete",
            "turnId": self._turn_id,
            "toolCallId": call_id,
            # Both REQUIRED by `ToolCallResult`. Omitting them left the client
            # computing `completed && success` as falsey, so it never used the
            # past-tense label and the row stayed in the present tense forever.
            "result": _tool_result(result, success, past_tense_message),
        }
        await self._ensure_runnable(call_id)
        await self._sequencer.publish(self._channel, action)
        # Back to the client's fallback rather than to a guess. What the agent
        # does between tool calls is something only the provider knows.
        await self.set_activity(None)

    async def turn_failed(
        self,
        message: str,
        error_type: str = "agent.turn",
        duration_ms: int = 0,
        *,
        resumable: bool = False,
    ) -> None:
        """End the turn in error.

        `errorType` is REQUIRED by `ErrorInfo` and we omitted it, so the client
        rendered every failure as the literal string `Error: (undefined) ...`.
        Its first mapper wants `_meta.chatError.fetchError.type` and returns
        undefined without it, so the `??` fallback -- `Error: ({0}) {1}` --
        always won, with `errorType` interpolated as `undefined`.

        The vocabulary is NOT a contract: the schema says `errorType: string`
        with no enum, and the client only ever interpolates it into a display
        string. The dotted tokens here match the reference host
        (`agent.turn`, `provider.resumeSession`) rather than the
        `somethingFailed` style VS Code's own host emits, which is produced by
        its host-side code and consumed by nobody.

        Cancellation does NOT come through here. `chat/turnCancelled` is its
        own action, carries no ErrorInfo, and settles the turn as `cancelled`;
        routing a user's stop through `chat/error` would paint it red.

        Since 0.9.0 the error is an `ErrorResponsePart` the reducer appends to
        the ended turn, so it stays in the transcript. It is marked
        `resumable` only when the provider asked AND the agent running the
        turn is `ResumesTurns`: "Only `true` enables resume", and a resumable
        part from an agent that cannot resume would invite a `chat/turnResume`
        nothing would answer. The host rejects such a resume anyway; not
        offering it is the honest half.
        """
        part: dict[str, Any] = {
            "kind": "error",
            "error": {"errorType": error_type, "message": message},
        }
        if resumable:
            if self._resumable:
                part["resumable"] = True
            else:
                _log.warning(
                    "turn_failed(resumable=True) from an agent session that is not "
                    "ResumesTurns; publishing the error as not resumable"
                )
        await self._sequencer.publish(
            self._channel,
            {
                "type": "chat/error",
                "turnId": self._turn_id,
                "part": part,
                "duration": duration_ms,
            },
        )

    async def request_input(self, request: InputRequest) -> InputOutcome:
        """Publish an input request and suspend until a client resolves it.

        The id is minted by the registry, never by the provider (ADR 0005) --
        the provider names its questions, the host names the request.

        Note the request is *parked before it is published*. Publishing first
        would open a window where a very fast client could dispatch
        `chat/inputCompleted` against a request the registry does not know
        about yet, and the host would drop the answer.
        """
        parked = self._pending.open(
            turn_scope(self._channel, self._turn_id),
            "input",
            # Namespaced: tool calls park under their bare `toolCallId` in the
            # same (channel, key) table, and a provider's question id must not
            # be able to shadow one.
            key=_input_key(request.key) if request.key is not None else None,
            channel=self._channel,
        )

        wire: dict[str, Any] = {"id": parked.id}
        if request.message is not None:
            wire["message"] = request.message
        if request.url is not None:
            wire["url"] = request.url
        if request.questions:
            wire["questions"] = [
                {
                    **question.extra,
                    "kind": question.kind,
                    "id": question.id,
                    "message": question.message,
                    **({"options": list(question.options)} if question.options else {}),
                }
                for question in request.questions
            ]

        await self._sequencer.publish(
            self._channel,
            {"type": "chat/inputRequested", "turnId": self._turn_id, "request": wire},
        )
        # Mirrored to the session so a client can answer WITHOUT having
        # subscribed to this chat -- "this is the channel a client dispatches
        # its response to; it does not need to have subscribed to that chat
        # first" -- and so the session's status carries InputNeeded, which is
        # what a session list renders.
        await self._mirror_input_needed(parked.id, {"kind": "chatInput", "request": wire})

        outcome = await parked.future
        answers = outcome.payload if isinstance(outcome.payload, Mapping) else {}
        return InputOutcome(response=outcome.response, answers=answers)

    async def input_resolved(
        self,
        key: str,
        *,
        response: str = "accept",
        answers: Mapping[str, Any] | None = None,
    ) -> bool:
        """An input request was answered somewhere clients here cannot see.

        The `request_input` twin of :meth:`tool_call_confirmed`. The request is
        found by the provider's own `InputRequest.key` -- the id clients know
        is host-minted, so the provider never learned it.

        `chat/inputCompleted` is client-dispatchable, and a host may originate
        a client-dispatchable action the way `tool_call_confirmed` publishes
        `chat/toolCallConfirmed`: the reducer records the response and answers
        on the open part and drops the chat out of `InputNeeded`, so the
        transcript says how the question was settled and no client can answer
        it a second time. Published BEFORE the park is resolved (ADR 0005), and
        only while the part is still open -- a frame the reducer would drop
        still costs a `serverSeq`.
        """
        request_id = self._pending.id_for_key(_input_key(key), channel=self._channel)
        request = self._pending.get(request_id) if request_id is not None else None
        if request is None or request.kind != "input":
            return False
        if not self._open_input_part(request.id):
            # A client's answer is already in state and on its way to the park
            # (`_react` resolves it right after the reducer): that answer wins,
            # and this one would only contradict it.
            return False
        action: dict[str, Any] = {
            "type": "chat/inputCompleted",
            "requestId": request.id,
            "response": response,
        }
        if answers is not None:
            action["answers"] = {str(k): v for k, v in answers.items()}
        await self._sequencer.publish(self._channel, action)
        # Read back, merged with any drafts a client had synced -- the same
        # payload a client's answer would have produced.
        self._pending.resolve(
            request.id,
            RequestOutcome(response=response, payload=self._input_answers(request.id)),
        )
        await self._retract_input_needed(request.id)
        return True

    def _input_parts(self) -> list[Mapping[str, Any]]:
        state = self._sequencer.state_of(self._channel)
        active = state.get("activeTurn") if isinstance(state, Mapping) else None
        if not isinstance(active, Mapping) or active.get("id") != self._turn_id:
            return []
        parts = active.get("responseParts")
        return [
            part
            for part in (parts if isinstance(parts, list) else [])
            if isinstance(part, Mapping) and part.get("kind") == "inputRequest"
        ]

    def _open_input_part(self, request_id: str) -> bool:
        """Whether *request_id*'s part is in this turn and still unanswered."""
        for part in self._input_parts():
            request = part.get("request")
            if isinstance(request, Mapping) and request.get("id") == request_id:
                return "response" not in part
        return False

    def _input_answers(self, request_id: str) -> dict[str, Any]:
        for part in self._input_parts():
            request = part.get("request")
            if isinstance(request, Mapping) and request.get("id") == request_id:
                answers = request.get("answers")
                return dict(answers) if isinstance(answers, Mapping) else {}
        return {}

    async def confirm_tool_call(self, call: ToolConfirmation) -> ToolConfirmationOutcome:
        """Publish `chat/toolCallReady` and suspend until a client confirms.

        The tool call itself must already exist -- `chat/toolCallReady` moves an
        existing call into `pendingConfirmation`, it does not create one -- so a
        provider calls `tool_call_started` first.
        """
        parked = self._pending.open(
            turn_scope(self._channel, self._turn_id),
            "confirm",
            key=call.call_id,
            channel=self._channel,
        )

        action: dict[str, Any] = {
            "type": "chat/toolCallReady",
            "turnId": self._turn_id,
            "toolCallId": call.call_id,
            "invocationMessage": call.invocation_message,
        }
        if call.tool_input is not None:
            # Encoded, like every other site. This one was missed when the
            # other three were fixed, so the CONFIRM path alone kept publishing
            # a bare object where `ToolInput = string | ContentRef` -- and a
            # partial fix is worse than none, because the surface that still
            # works hides the one that does not.
            action["toolInput"] = _encoded_tool_input(call.tool_input)
        self._streaming.pop(call.call_id, None)
        if call.confirmation_title is not None:
            action["confirmationTitle"] = call.confirmation_title
        if call.editable:
            action["editable"] = True
        # Both are declared on `ChatToolCallReadyAction` and carried into
        # `ToolCallPendingConfirmationState` by the reducer (`options`,
        # `edits`), so a client can offer "Approve in this Session" and show
        # the diff before the user decides. Omitted when empty: an empty
        # `options` list would tell a client to render no choices at all.
        options = [option.to_wire() for option in call.options]
        if options:
            action["options"] = options
        if call.edits:
            action["edits"] = {"items": [file_edit(edit, self._content) for edit in call.edits]}
        await self._sequencer.publish(self._channel, action)

        pending_call: dict[str, Any] = {
            "toolCallId": call.call_id,
            "toolName": call.name,
            "displayName": call.display_name or call.name,
            # REQUIRED by ToolCallPendingConfirmationState, and omitted
            # -- so the whole `session/inputNeeded` entry failed its
            # own declared shape. The session-level mirror is what a
            # client renders when the approval is surfaced OUTSIDE the
            # chat, so a malformed one loses the approval entirely.
            "invocationMessage": call.invocation_message,
            # KEBAB-case. The enum is `pending-confirmation`, and we
            # sent `pendingConfirmation` -- every other discriminant
            # nearby is camelCase, which is exactly why this was not
            # noticed. Same for `auth-required`.
            "status": "pending-confirmation",
        }
        # The mirror is a whole `ToolCallPendingConfirmationState`, so the
        # fields that decide the answer travel with it: a client answering from
        # the session list, without the chat, otherwise approves blind -- no
        # input, no diff, and only approve/deny where the agent offered more.
        for key in ("toolInput", "confirmationTitle", "editable", "options", "edits"):
            if key in action:
                pending_call[key] = action[key]
        await self._mirror_input_needed(
            parked.id,
            {"kind": "toolConfirmation", "turnId": self._turn_id, "toolCall": pending_call},
        )

        outcome = await parked.future
        payload = outcome.payload if isinstance(outcome.payload, Mapping) else {}
        # The APPROVED input, not the proposed one: `editable` lets a client
        # rewrite the parameters, and running the original would execute
        # something nobody agreed to.
        # DECODED on the way back. The client edits the string we sent, so what
        # returns is a JSON string, not the object an adapter expects -- the
        # encoding is a wire concern and must not leak into the provider API.
        edited = _decoded_tool_input(payload.get("toolInput", call.tool_input))
        approved = outcome.response == "accept"
        # Validation has already refused an id this call did not offer, so the
        # lookup only fails for a confirmation resolved some other way.
        selected_id = payload.get("selectedOptionId")
        selected = next((o for o in call.options if o.id == selected_id), None)
        reason = payload.get("reason")
        suggestion = payload.get("userSuggestion")
        return ToolConfirmationOutcome(
            approved=approved,
            tool_input=edited,
            selected_option=selected,
            reason=reason if isinstance(reason, str) and not approved else None,
            # What the user said about the denial -- the field that was dropped
            # on the floor here, so an agent told "no, use the other file" only
            # ever heard "no" and tried the same thing again.
            reason_message=markdown_text(payload.get("reasonMessage")),
            user_suggestion=user_message(suggestion) if isinstance(suggestion, Mapping) else None,
        )

    async def tool_call_confirmed(
        self, call_id: str, *, approved: bool, reason_message: str | None = None
    ) -> None:
        """A confirmation was answered somewhere clients here cannot see.

        An agent driven from two places at once (Claude Code under Remote
        Control) asks both, and withdraws the question here when the other one
        answers -- by cancelling its own `confirm_tool_call`. That cancellation
        leaves the park in place on purpose: a cancelled *turn* goes through
        the same `CancelledError`, and only the provider knows which it was.
        This is the provider saying so, and what the other side answered.

        Without it every client keeps showing an approval prompt for a call
        that is already running, and the session stays `InputNeeded` until the
        turn ends.
        """
        request_id = self._pending.id_for_key(call_id, channel=self._channel)
        request = self._pending.get(request_id) if request_id is not None else None
        if request is not None and request.kind == "confirm":
            # The provider's await is already gone; resolving only discards the
            # park, so a client answering the stale prompt finds nothing to hit.
            self._pending.resolve(
                request.id, RequestOutcome(response="accept" if approved else "decline")
            )
            await self._retract_input_needed(request.id)
        tool_call = self._tool_call_state(call_id)
        if tool_call is None or tool_call.get("status") != _PENDING_CONFIRMATION:
            # A client answered first, or the call never asked: the reducer
            # would drop the frame, and a dropped frame still costs a serverSeq.
            return
        action: dict[str, Any] = {
            "type": "chat/toolCallConfirmed",
            "turnId": self._turn_id,
            "toolCallId": call_id,
            "approved": approved,
        }
        if approved:
            action["confirmed"] = "user-action"
        else:
            action["reason"] = "denied"
            if reason_message is not None:
                action["reasonMessage"] = reason_message
        await self._sequencer.publish(self._channel, action)

    async def request_authentication(self, call_id: str, challenge: AuthChallenge) -> None:
        """Pause a tool call on an auth challenge, and suspend until it clears.

        The server-level and tool-call-level challenges "remain separate on
        purpose: the server saying 'I need auth' and a tool invocation saying 'I
        am waiting on that auth' are different facts that can be true
        independently." This is the second one -- it pauses one call, and says
        nothing about the MCP server's own state.
        """
        # The canonical identifier is the metadata's own `resource` member
        # (RFC 9728; `session-state.ts:1282-1287`), and it is what keys the
        # park: `authenticate` resolves only the calls whose challenge named
        # the pushed resource, so it has to be stored here.
        resource = challenge.resource.get("resource")
        resource_id = resource if isinstance(resource, str) else None
        if resource_id is not None and self._advertise_resource is not None:
            # Before the publish, so no client can see a challenge whose
            # resource the host would still refuse a token for.
            self._advertise_resource(resource_id)
        parked = self._pending.open(
            turn_scope(self._channel, self._turn_id),
            "auth",
            key=f"auth:{call_id}",
            channel=self._channel,
            resource=resource_id,
            required_scopes=challenge.required_scopes,
        )
        await self._sequencer.publish(
            self._channel,
            {
                "type": "chat/toolCallAuthRequired",
                "turnId": self._turn_id,
                "toolCallId": call_id,
                "auth": challenge.to_wire(),
            },
        )
        await self._mirror_input_needed(
            parked.id,
            {
                "kind": "toolAuthentication",
                "turnId": self._turn_id,
                "toolCall": {"toolCallId": call_id, "status": "auth-required"},
            },
        )
        await parked.future
        # Back to `running`, "preserving the fields it had before pausing".
        await self._sequencer.publish(
            self._channel,
            {
                "type": "chat/toolCallAuthResolved",
                "turnId": self._turn_id,
                "toolCallId": call_id,
            },
        )

    async def run_client_tool(self, call: ClientToolCall) -> ToolResult:
        """Ask a client to execute one of its own tools, and suspend.

        `contributor: {kind: 'client', clientId}` is what makes the client
        responsible: "the identified client is responsible for executing the
        tool and dispatching `chat/toolCallComplete` with the result."
        """
        if not self._is_active_client(call.client_id):
            # Refused rather than parked. A request addressed to a client that
            # is not in the session can never be answered, and a provider that
            # awaits it blocks its turn until somebody cancels -- an error the
            # adapter can handle beats a hang it cannot see.
            raise LookupError(f"{call.client_id!r} is not an active client of this session")

        parked = self._pending.open(
            turn_scope(self._channel, self._turn_id),
            "clienttool",
            key=call.call_id,
            channel=self._channel,
            # Only this client may answer: it is the one being asked to run the
            # tool, and it is the one whose departure has to end the call.
            owner=call.client_id,
        )

        action: dict[str, Any] = {
            "type": "chat/toolCallStart",
            "turnId": self._turn_id,
            "toolCallId": call.call_id,
            "toolName": call.name,
            "displayName": call.display_name or call.name,
            "contributor": {"kind": "client", "clientId": call.client_id},
        }
        # No `toolInput` here either: the action does not declare one, the
        # reducer drops it, and the ready below carries it -- which is where the
        # executing client reads it from.
        await self._sequencer.publish(self._channel, action)

        # WITHOUT THIS THE TOOL NEVER RUNS. The client's executor reads
        # `"toolInput" in call`, and on a call still in `streaming` it returns
        # without invoking anything -- so the host parked on `parked.future`
        # forever and the turn hung until someone cancelled it. Client-provided
        # tools are exactly the case the spec calls out: "the server typically
        # sets `confirmed` to `'not-needed'` so the tool transitions directly
        # to `running`, where the owning client can begin execution".
        ready: dict[str, Any] = {
            "type": "chat/toolCallReady",
            "turnId": self._turn_id,
            "toolCallId": call.call_id,
            "invocationMessage": call.invocation_message
            or f"Running {call.display_name or call.name}",
            "confirmed": "not-needed",
            # "MUST NOT change execution ownership established at
            # `chat/toolCallStart`" -- same clientId, deliberately repeated.
            "contributor": {"kind": "client", "clientId": call.client_id},
        }
        if call.tool_input is not None:
            ready["toolInput"] = _encoded_tool_input(call.tool_input)
        self._streaming.pop(call.call_id, None)
        await self._sequencer.publish(self._channel, ready)

        await self._mirror_input_needed(
            parked.id,
            {
                "kind": "toolClientExecution",
                "turnId": self._turn_id,
                "clientId": call.client_id,
                # A `ToolCallRunningState` since 0.9.0, whose required fields
                # include the two `chat/toolCallReady` above just set.
                "toolCall": {
                    "toolCallId": call.call_id,
                    "toolName": call.name,
                    "displayName": call.display_name or call.name,
                    "invocationMessage": ready["invocationMessage"],
                    "confirmed": "not-needed",
                    "status": "running",
                    # "whose `contributor` is a client `ToolCallClientContributor`
                    # whose `clientId` matches the denormalized `clientId`" -- and
                    # the one thing a client that is not subscribed to the chat
                    # can tell its own calls by.
                    "contributor": {"kind": "client", "clientId": call.client_id},
                    **({"toolInput": ready["toolInput"]} if "toolInput" in ready else {}),
                },
            },
        )

        outcome = await parked.future
        if outcome.response != "accept":
            # `outcome.response` used to be dropped on the floor here, so the
            # two ways this can end WITHOUT a result -- the owning client
            # refusing the call, and the host cancelling it because that client
            # left -- both reached the provider as `ToolResult(value={})`. A
            # refusal that reads as an empty success is the worst possible
            # rendering of it: the agent believes it ran the editor's tool.
            reason = outcome.payload if isinstance(outcome.payload, str) else None
            if isinstance(outcome.payload, Mapping):
                # A completion with `success: false` is a tool that ran and
                # failed, and its `content` says why ("no such node"). Dropping
                # it left the agent with a bare refusal it could not act on.
                reason = _result_text(outcome.payload) or reason
            return ToolResult(response=outcome.response, reason=reason)
        return ToolResult(value=outcome.payload)

    def _is_active_client(self, client_id: str) -> bool:
        if self._session_uri is None:
            return False
        state = self._sequencer.state_of(self._session_uri)
        clients = state.get("activeClients") if isinstance(state, Mapping) else None
        if not isinstance(clients, list):
            return False
        return any(
            isinstance(entry, Mapping) and entry.get("clientId") == client_id for entry in clients
        )

    async def set_activity(self, activity: str | None) -> None:
        """Publish what the session is doing right now, or clear it.

        A session with no activity renders as the client's own literal fallback,
        "Working...", which is the same for every session in the list. The
        reference host writes something better here with a small model; we
        cannot, so the only honest string we have is the tool's own display
        name -- which the provider already chose for a human to read.

        So this is set when a tool call starts and cleared when it finishes,
        rather than being invented for the gaps. `activity` is omitted rather
        than nulled to clear it: the schema says "or `undefined` to clear", and
        the field is optional.

        What is already published is read from the session's own state, not
        from a cache on this sink. There are two writers -- this and
        `SessionPublisher.activity_changed`, which a provider may call at any
        time, including outside a turn -- and a per-sink cache only ever knew
        about one of them. So a provider that set an activity itself left this
        sink believing there was nothing to clear, and the `set_activity(None)`
        in the turn's `finally` deduped itself away: the string outlived the
        turn and the session sat idle claiming to be editing a file.
        """
        if self._session_uri is None:
            return
        state = self._sequencer.state_of(self._session_uri)
        if activity == (state.get("activity") if isinstance(state, Mapping) else None):
            return
        action: dict[str, Any] = {"type": "session/activityChanged"}
        if activity is not None:
            action["activity"] = activity
        await self._sequencer.publish(self._session_uri, action)
        if self._session_changed is not None:
            await self._session_changed()

    async def _mirror_input_needed(self, request_id: str, request: dict[str, Any]) -> None:
        """Publish one `session/inputNeeded` entry for a parked request."""
        if self._session_uri is None:
            return
        await self._sequencer.publish(
            self._session_uri,
            {
                "type": "session/inputNeededSet",
                "request": {**request, "id": request_id, "chat": self._channel},
            },
        )

    async def _retract_input_needed(self, request_id: str) -> None:
        """Take back one `session/inputNeeded` entry, and re-mirror the summary."""
        if self._session_uri is None:
            return
        await self._sequencer.publish(
            self._session_uri, {"type": "session/inputNeededRemoved", "id": request_id}
        )
        if self._session_changed is not None:
            await self._session_changed()


def _elapsed_ms(started_at: float) -> int:
    """Milliseconds since *started_at*, from the monotonic clock.

    Monotonic rather than wall clock: a turn that straddles an NTP correction
    or a DST change must not report a negative duration.
    """
    return max(0, int((time.monotonic() - started_at) * 1000))


def _agent_uri(value: Any) -> str | None:
    """`AgentSelection.uri`, which is the whole of that type."""
    if isinstance(value, Mapping):
        uri = value.get("uri")
        return uri if isinstance(uri, str) else None
    return None


def markdown_text(value: Any) -> str | None:
    """A `StringOrMarkdown` as text: the string, or the `{markdown}` source."""
    if isinstance(value, str):
        return value
    if isinstance(value, Mapping) and isinstance(value.get("markdown"), str):
        return str(value["markdown"])
    return None


def user_message(
    message: Mapping[str, Any],
    *,
    chat_uri: str | None = None,
    attached: Sequence[AttachedChat] = (),
) -> UserMessage:
    """A wire `Message` in provider terms.

    The model and agent selections are lifted out of `raw` and named. The
    client sends both on every turn; leaving them buried meant no adapter
    could find them without knowing the wire format.
    """
    text = message.get("text")
    return UserMessage(
        text=text if isinstance(text, str) else "",
        raw=message,
        model=ModelSelection.from_wire(message.get("model")),
        agent_uri=_agent_uri(message.get("agent")),
        chat_uri=chat_uri,
        attached_chats=tuple(attached),
    )


def attached_chats(sequencer: Sequencer, message: Any) -> tuple[AttachedChat, ...]:
    """Resolve a message's `MessageChatAttachment`s against the chats' state.

    "When accepting the message, the host MUST resolve the referenced chat's
    retained transcript from its first turn through the supplied `endTurn`
    ... and supply it as model context." Validation has already refused an
    unknown chat or `endTurn` and pinned an absent one, so this only reads;
    a chat pruned since then resolves to whatever it still retains.

    Deep-copied: the provider gets an independent transcript, and an edit to
    the source chat must not reach back into a message already sent.
    """
    attachments = message.get("attachments") if isinstance(message, Mapping) else None
    resolved: list[AttachedChat] = []
    for attachment in attachments if isinstance(attachments, list) else []:
        if not isinstance(attachment, Mapping) or attachment.get("type") != "chat":
            continue
        resource = attachment.get("resource")
        if not isinstance(resource, str):
            continue
        end = attachment.get("endTurn")
        end_turn = end if isinstance(end, str) else None
        label = attachment.get("label")
        state = sequencer.state_of(resource)
        turns = state.get("turns") if isinstance(state, Mapping) else None
        transcript: list[Mapping[str, Any]] = []
        if end_turn is not None and isinstance(turns, list):
            for turn in turns:
                if isinstance(turn, Mapping):
                    transcript.append(copy.deepcopy(dict(turn)))
                    if turn.get("id") == end_turn:
                        break
            else:
                # `endTurn` is gone (truncated away since it was pinned):
                # nothing can stand in for it, so nothing is supplied.
                transcript = []
        resolved.append(
            AttachedChat(
                resource=resource,
                end_turn=end_turn,
                label=label if isinstance(label, str) else None,
                turns=tuple(transcript),
            )
        )
    return tuple(resolved)


def _result_text(result: Mapping[str, Any]) -> str | None:
    """The readable part of a `ToolCallResult`: its text content, else `error`."""
    content = result.get("content")
    parts = content if isinstance(content, list) else []
    texts = [
        part["text"]
        for part in parts
        if isinstance(part, Mapping)
        and part.get("type") == "text"
        and isinstance(part.get("text"), str)
    ]
    if texts:
        return "\n".join(texts)
    error = result.get("error")
    message = error.get("message") if isinstance(error, Mapping) else None
    return message if isinstance(message, str) else None


def _encoded_tool_input(value: Any) -> Any:
    """`ToolInput = string | ContentRef`, so a bare object is not valid.

    We published the parameters as a JSON OBJECT. The shipping client requires
    `JSON.parse(toolInput)` to yield an object, and when the value is not a
    string it aborts the invocation and synthesises a failure -- which is why
    client-contributed tool execution, a feature this host advertises, could
    never actually run. A ContentRef (an object with a `uri`) is the one
    object form the type allows, so it passes through untouched.
    """
    if isinstance(value, str):
        return value
    if isinstance(value, Mapping) and "uri" in value:
        return dict(value)
    return json.dumps(value)


def _decoded_tool_input(value: Any) -> Any:
    """The inverse of :func:`_encoded_tool_input`, for a value coming back.

    A client that edits an `editable` tool call returns the JSON STRING it was
    given. A provider asked for a mapping and should get one; leaving the
    string to leak through means every adapter has to know the wire encoding.
    Anything that is not JSON is returned untouched rather than being lost.
    """
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except ValueError:
        return value


def _tool_result(result: Any, success: bool, past_tense_message: str | None) -> dict[str, Any]:
    """A `ToolCallResult` with the two fields the protocol makes mandatory.

    A bare content list is the result's `content`: the natural thing to pass
    once `file_edit` hands back content items, and it used to become `{}` --
    the edit silently dropped from a call that reported success.
    """
    if isinstance(result, Sequence) and not isinstance(result, str | bytes):
        result = {"content": [dict(item) for item in result if isinstance(item, Mapping)]}
    wire: dict[str, Any] = dict(result) if isinstance(result, Mapping) else {}
    wire.setdefault("success", success)
    wire.setdefault(
        "pastTenseMessage",
        past_tense_message or ("Ran the tool" if success else "The tool failed"),
    )
    return wire


class TurnRunner:
    """Runs one turn: hand the message to the agent, publish what comes back."""

    def __init__(
        self,
        sequencer: Sequencer,
        channel: str,
        pending: PendingRequests | None = None,
        session_uri: str | None = None,
        session_changed: SessionChanged | None = None,
        advertise_resource: Callable[[str], None] | None = None,
        *,
        content: ContentStore | None = None,
    ) -> None:
        self._sequencer = sequencer
        self._channel = channel
        self._pending = pending if pending is not None else PendingRequests()
        self._session_uri = session_uri
        self._session_changed = session_changed
        self._advertise_resource = advertise_resource
        self._content = content
        #: The chat this turn ran on. The caller drains that chat's queue when
        #: the turn ends, and it should not have to remember which one.
        self.channel = channel
        #: Requests still parked when the turn ended. The caller retracts their
        #: `session/inputNeeded` entries -- not this class, because the turn task
        #: is frequently the one being cancelled and cannot be relied on to
        #: finish another await.
        self.abandoned: list[PendingRequest] = []
        #: The sink this turn published through, once it has one. The caller
        #: clears its activity, for the same reason it retracts the requests.
        self.sink: ActionTurnSink | None = None

    def _sink(self, agent_session: AgentSession | None, turn_id: str) -> ActionTurnSink:
        sink = ActionTurnSink(
            self._sequencer,
            self._channel,
            turn_id,
            self._pending,
            self._session_uri,
            self._session_changed,
            self._advertise_resource,
            content=self._content,
            # Decided by the agent that RUNS the turn, not the session's: an
            # external or worker turn's agent is the provider's callback, which
            # has nothing to resume with.
            resumable=isinstance(agent_session, ResumesTurns),
        )
        # However the turn ends -- return, raise or cancellation -- the session
        # must not be left advertising a tool that is no longer running. A
        # cancelled turn is exactly the case that would strand it.
        self.sink = sink
        return sink

    async def run(self, agent_session: AgentSession | None, started: Mapping[str, Any]) -> None:
        turn_id = started.get("turnId")
        if not isinstance(turn_id, str):
            return
        sink = self._sink(agent_session, turn_id)
        if agent_session is None:
            # Distinct from a provider crash, and named the way the reference
            # host names it: the session could not be resumed.
            await sink.turn_failed("no agent session", "provider.resumeSession")
            return

        raw = started.get("message")
        message: Mapping[str, Any] = raw if isinstance(raw, Mapping) else {}
        await self._drive(
            turn_id,
            sink,
            lambda: agent_session.send_user_message(
                user_message(
                    message,
                    # Which chat. One agent session serves every chat of the
                    # session, and nothing told it which one a message was for.
                    chat_uri=self._channel,
                    attached=attached_chats(self._sequencer, message),
                ),
                sink,
            ),
        )

    async def resume(self, agent_session: AgentSession | None, turn_id: str) -> None:
        """Continue a failed turn the reducer has just reopened (`chat/turnResume`).

        Same turn id, a fresh sink, and the same endings as :meth:`run`: what
        the provider adds lands after the error part, and the turn completes,
        fails or is cancelled like any other. `duration` measures this
        resumption -- the host's own work on it -- not the time the turn sat
        failed in between.
        """
        sink = self._sink(agent_session, turn_id)
        if not isinstance(agent_session, ResumesTurns):
            # Accepted only because the session's agent was not running yet
            # and might have resumed; it came back without the ability. The
            # reopened turn still has to end, and a resumable error here would
            # only invite the same dead end again.
            await sink.turn_failed(
                "this agent cannot resume a failed turn", "provider.resumeTurn", resumable=False
            )
            return
        await self._drive(
            turn_id, sink, lambda: agent_session.resume_turn(self._channel, turn_id, sink)
        )

    async def _drive(
        self, turn_id: str, sink: ActionTurnSink, call: Callable[[], Awaitable[None]]
    ) -> None:
        """Run the agent's half of a turn, then end it the one way it should end."""
        started_at = time.monotonic()
        try:
            await call()
        except Exception as exc:
            if self._is_active(turn_id):
                await sink.turn_failed(
                    f"{type(exc).__name__}: {exc}",
                    "agent.turn",
                    _elapsed_ms(started_at),
                )
            return
        finally:
            # ADR 0005: the turn is the scope. However this turn ended -- return,
            # raise or cancellation -- nothing may still be parked under it, or
            # the provider stays blocked on a future nobody will ever resolve
            # and the chat sits in `InputNeeded` until the session is disposed.
            self.abandoned = self._pending.cancel_scope(
                turn_scope(self._channel, turn_id), "turn ended"
            )

        # A provider that returns normally after being cancelled would otherwise
        # complete a turn a client already ended. The reducer would no-op on it,
        # but it still burns a serverSeq and broadcasts a `turnComplete` for a
        # turn every client has already seen cancelled.
        if not self._is_active(turn_id):
            return

        await self._sequencer.publish(
            self._channel,
            # Measured, not zero. The client renders it as the turn's elapsed
            # time (`elapsedMs: c.duration`), so a hardcoded 0 made every turn
            # in the transcript look instantaneous.
            {
                "type": "chat/turnComplete",
                "turnId": turn_id,
                "duration": _elapsed_ms(started_at),
            },
        )

    def _is_active(self, turn_id: str) -> bool:
        """Whether *turn_id* is still the channel's active turn."""
        state = self._sequencer.state_of(self._channel)
        if not isinstance(state, Mapping):
            return False
        active = state.get("activeTurn")
        return isinstance(active, Mapping) and active.get("id") == turn_id
