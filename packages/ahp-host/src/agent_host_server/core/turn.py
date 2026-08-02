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

import json
import time
import uuid
from collections.abc import Mapping
from typing import Any

from agent_host_server.core.pending import PendingRequest, PendingRequests
from agent_host_server.core.sequencer import Sequencer
from agent_host_server.provider.base import (
    AgentSession,
    AuthChallenge,
    ClientToolCall,
    InputOutcome,
    InputRequest,
    ModelSelection,
    ToolConfirmation,
    ToolConfirmationOutcome,
    ToolResult,
    UserMessage,
)

__all__ = ["ActionTurnSink", "TurnRunner", "turn_scope"]


def turn_scope(channel: str, turn_id: str) -> str:
    """The lifetime a suspended request is bound to (ADR 0005).

    The turn, not the session and not the connection: a cancelled turn must free
    everything waiting under it, and the connection that started the turn is
    frequently not the one that answers.
    """
    return f"{channel}#{turn_id}"


class ActionTurnSink:
    """A :class:`~agent_host_server.provider.base.TurnSink` that publishes actions."""

    def __init__(
        self,
        sequencer: Sequencer,
        channel: str,
        turn_id: str,
        pending: PendingRequests | None = None,
        session_uri: str | None = None,
    ) -> None:
        self._sequencer = sequencer
        self._channel = channel
        self._turn_id = turn_id
        self._pending = pending if pending is not None else PendingRequests()
        #: Where `session/inputNeeded` entries are mirrored. Optional so a bare
        #: sink stays constructible in a test without a session around it.
        self._session_uri = session_uri
        self._markdown_part_id: str | None = None
        self._reasoning_part_id: str | None = None

    async def _ensure_markdown_part(self) -> str:
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
    ) -> None:
        # `chat/toolCallStart` creates its own toolCall response part; emitting
        # an extra `chat/responsePart` for it would duplicate the part.
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
        if tool_input is not None:
            action["toolInput"] = _encoded_tool_input(tool_input)
        await self._sequencer.publish(self._channel, action)

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
        await self._sequencer.publish(self._channel, action)

    async def turn_failed(
        self, message: str, error_type: str = "agent.turn", duration_ms: int = 0
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
        """
        await self._sequencer.publish(
            self._channel,
            {
                "type": "chat/error",
                "turnId": self._turn_id,
                "error": {"errorType": error_type, "message": message},
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
        parked = self._pending.open(turn_scope(self._channel, self._turn_id), "input")

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

    async def confirm_tool_call(self, call: ToolConfirmation) -> ToolConfirmationOutcome:
        """Publish `chat/toolCallReady` and suspend until a client confirms.

        The tool call itself must already exist -- `chat/toolCallReady` moves an
        existing call into `pendingConfirmation`, it does not create one -- so a
        provider calls `tool_call_started` first.
        """
        parked = self._pending.open(
            turn_scope(self._channel, self._turn_id), "confirm", key=call.call_id
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
        if call.confirmation_title is not None:
            action["confirmationTitle"] = call.confirmation_title
        if call.editable:
            action["editable"] = True
        await self._sequencer.publish(self._channel, action)

        await self._mirror_input_needed(
            parked.id,
            {
                "kind": "toolConfirmation",
                "turnId": self._turn_id,
                "toolCall": {
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
                },
            },
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
        return ToolConfirmationOutcome(approved=outcome.response == "accept", tool_input=edited)

    async def request_authentication(self, call_id: str, challenge: AuthChallenge) -> None:
        """Pause a tool call on an auth challenge, and suspend until it clears.

        The server-level and tool-call-level challenges "remain separate on
        purpose: the server saying 'I need auth' and a tool invocation saying 'I
        am waiting on that auth' are different facts that can be true
        independently." This is the second one -- it pauses one call, and says
        nothing about the MCP server's own state.
        """
        parked = self._pending.open(
            turn_scope(self._channel, self._turn_id), "auth", key=f"auth:{call_id}"
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
            turn_scope(self._channel, self._turn_id), "clienttool", key=call.call_id
        )

        action: dict[str, Any] = {
            "type": "chat/toolCallStart",
            "turnId": self._turn_id,
            "toolCallId": call.call_id,
            "toolName": call.name,
            "displayName": call.display_name or call.name,
            "contributor": {"kind": "client", "clientId": call.client_id},
        }
        if call.tool_input is not None:
            action["toolInput"] = _encoded_tool_input(call.tool_input)
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
        await self._sequencer.publish(self._channel, ready)

        await self._mirror_input_needed(
            parked.id,
            {
                "kind": "toolClientExecution",
                "turnId": self._turn_id,
                "clientId": call.client_id,
                "toolCall": {
                    "toolCallId": call.call_id,
                    "toolName": call.name,
                    "displayName": call.display_name or call.name,
                    "status": "running",
                },
            },
        )

        outcome = await parked.future
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
    """A `ToolCallResult` with the two fields the protocol makes mandatory."""
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
    ) -> None:
        self._sequencer = sequencer
        self._channel = channel
        self._pending = pending if pending is not None else PendingRequests()
        self._session_uri = session_uri
        #: Requests still parked when the turn ended. The caller retracts their
        #: `session/inputNeeded` entries -- not this class, because the turn task
        #: is frequently the one being cancelled and cannot be relied on to
        #: finish another await.
        self.abandoned: list[PendingRequest] = []

    async def run(self, agent_session: AgentSession | None, started: Mapping[str, Any]) -> None:
        turn_id = started.get("turnId")
        if not isinstance(turn_id, str):
            return
        sink = ActionTurnSink(
            self._sequencer, self._channel, turn_id, self._pending, self._session_uri
        )

        started_at = time.monotonic()

        if agent_session is None:
            # Distinct from a provider crash, and named the way the reference
            # host names it: the session could not be resumed.
            await sink.turn_failed("no agent session", "provider.resumeSession")
            return

        message = started.get("message") or {}
        text = message.get("text") if isinstance(message, Mapping) else None
        try:
            await agent_session.send_user_message(
                UserMessage(
                    text=text if isinstance(text, str) else "",
                    raw=message,
                    # Lifted out of `raw` and named. The client sends both on
                    # every turn; leaving them buried meant no adapter could
                    # find them without knowing the wire format.
                    model=ModelSelection.from_wire(message.get("model")),
                    agent_uri=_agent_uri(message.get("agent")),
                ),
                sink,
            )
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
