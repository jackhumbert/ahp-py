"""Turn events: frozen, ``match``-able, and carrying their own actions.

Wire values stay plain dicts forever (ADR 0001). These are **derived** -- they
never copy or mutate an envelope, they carry it -- which is the one place the
shared package's no-``Unknown``-class rule inverts: events are ours, not the
wire's, so :class:`UnknownEvent` is a feature rather than a lossy decode.

The actionable ones carry their own answer. ``ToolCallReady.approve()`` exists
because the alternative is handing a caller a tool-call id and a chat URI and
letting them assemble the action, which is where every consumer gets it wrong.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, TypeAlias

from agent_host_protocol.types import JsonObject

from agent_host_client.client import actions, elicitation

__all__ = [
    "Delta",
    "InputRequested",
    "Reasoning",
    "Reconnected",
    "ResponsePartAdded",
    "TitleChanged",
    "ToolCallCompleted",
    "ToolCallContentChanged",
    "ToolCallDelta",
    "ToolCallReady",
    "ToolCallResultReview",
    "ToolCallRunning",
    "ToolCallStarted",
    "ToolInfo",
    "ToolLookup",
    "TurnCancelled",
    "TurnCompleted",
    "TurnEvent",
    "TurnFailed",
    "TurnInProgress",
    "TurnStarted",
    "UnknownEvent",
    "Usage",
]

#: Sends one action on the caller's behalf. Injected rather than closed over so
#: an event stays a plain frozen value that a test can construct.
Dispatcher: TypeAlias = Callable[[str, Mapping[str, Any]], None]


@dataclass(frozen=True, slots=True)
class ToolInfo:
    """What a tool call *is*, which no ``chat/toolCall*`` action carries.

    ``toolName`` is required on ``ToolCallState`` and is published once, on
    ``chat/toolCallStart``; ``annotations`` is a property of ``ToolDefinition``
    only, carried in ``SessionState.serverTools`` and
    ``activeClients[].tools``. Both are therefore state, and an event that wants
    to answer "which tool is this, and is it read-only" has to be told.
    """

    name: str = ""
    #: ``ToolAnnotations`` -- advisory hints, `readOnlyHint` among them.
    annotations: JsonObject | None = None


#: Resolves :class:`ToolInfo` for ``(channel, toolCallId)``, from the mirror.
ToolLookup: TypeAlias = Callable[[str, str], ToolInfo]


@dataclass(frozen=True, slots=True)
class _Base:
    envelope: JsonObject

    @property
    def channel(self) -> str:
        return str(self.envelope.get("channel", ""))

    @property
    def action(self) -> JsonObject:
        raw = self.envelope.get("action")
        return raw if isinstance(raw, dict) else {}

    @property
    def turn_id(self) -> str:
        return str(self.action.get("turnId", ""))


@dataclass(frozen=True, slots=True)
class TurnStarted(_Base):
    pass


@dataclass(frozen=True, slots=True)
class TurnInProgress(_Base):
    """A turn that was **already running** when we attached.

    Synthetic: it corresponds to no envelope on the wire, and its ``envelope``
    is assembled from the mirror. Deliberately a distinct type rather than a
    fabricated :class:`TurnStarted` -- a consumer that cannot tell "this began
    now" from "this began before you were looking" will replay an animation, or
    log a turn start that already happened, and there is nothing in a forged
    `TurnStarted` to warn them.
    """

    #: What the turn has produced so far, read from the mirror.
    text: str = ""


@dataclass(frozen=True, slots=True)
class Delta(_Base):
    """A chunk of assistant text.

    ``text`` is the delta as sent. The authoritative text of a turn is the
    concatenation of its markdown response parts read from the mirror -- hosts
    fold a turn's opening characters into the subscribe snapshot rather than
    emitting them as deltas, so a consumer that renders only these shows
    "ANANA" for "BANANA".
    """

    @property
    def text(self) -> str:
        return str(self.action.get("content", ""))

    @property
    def part_id(self) -> str:
        return str(self.action.get("partId", ""))


@dataclass(frozen=True, slots=True)
class Reasoning(_Base):
    """A chunk of model reasoning, appended to a reasoning response part.

    The action is ``chat/reasoning``; there is no ``chat/reasoningDelta``,
    however symmetric with ``chat/delta`` that would be.
    """

    @property
    def text(self) -> str:
        return str(self.action.get("content", ""))

    @property
    def part_id(self) -> str:
        """The reasoning part this appends to.

        Required on the action, and a turn can carry more than one reasoning
        part, so concatenating `text` without it interleaves them.
        """
        return str(self.action.get("partId", ""))


@dataclass(frozen=True, slots=True)
class ResponsePartAdded(_Base):
    @property
    def part(self) -> JsonObject:
        raw = self.action.get("part")
        return raw if isinstance(raw, dict) else {}


@dataclass(frozen=True, slots=True)
class _ToolCall(_Base):
    @property
    def tool_call_id(self) -> str:
        return str(self.action.get("toolCallId", ""))


@dataclass(frozen=True, slots=True)
class ToolCallStarted(_ToolCall):
    @property
    def tool_name(self) -> str:
        """Required on this action, and on **no other** ``chat/toolCall*`` one.

        Every later action in the call's life is identified by ``toolCallId``
        alone, so a consumer that needs the name past this point reads it from
        the tool call's state -- which is what :class:`ToolInfo` is.
        """
        return str(self.action.get("toolName", ""))

    @property
    def contributor(self) -> JsonObject | None:
        raw = self.action.get("contributor")
        return raw if isinstance(raw, dict) else None


@dataclass(frozen=True, slots=True)
class ToolCallReady(_ToolCall):
    """The agent is blocked waiting for approval.

    Only a ready action **without** ``confirmed`` lands here; the auto-confirmed
    form is a :class:`ToolCallRunning`, because it is a transition and not a
    question.

    A host arbitrates and the **first** answer wins; a later one comes back with
    a ``rejectionReason`` and the mirror reverts it. That is another client
    getting there first, not an error.
    """

    dispatch: Dispatcher | None = None
    #: Resolved from state by :func:`event_for`, since the action omits both.
    #: An :data:`~agent_host_client.api.approvals.ApprovalPolicy` switching on
    #: either would otherwise fall through to `otherwise` for every call --
    #: silently, and looking exactly like a deliberate denial.
    tool_name: str = ""
    annotations: JsonObject | None = None

    @property
    def options(self) -> Sequence[JsonObject]:
        raw = self.action.get("options")
        return [o for o in raw if isinstance(o, dict)] if isinstance(raw, list) else []

    @property
    def tool_input(self) -> Any:
        return self.action.get("toolInput")

    def approve(
        self,
        *,
        option_id: str | None = None,
        edited_input: str | None = None,
        confirmed: actions.ConfirmationReason = "user-action",
    ) -> None:
        """Approve, as an explicit user action unless told otherwise.

        `confirmed` is required on the action and is the only record of *how*
        the call was allowed to run -- a standing setting reads differently from
        a human clicking yes, and a UI replaying the transcript has nothing else
        to distinguish them.

        *edited_input* is a **string** -- `editedToolInput?: string` -- because
        only the inline form of `ToolInput` is client-editable; a structured
        value here would put a shape on the wire no peer's reducer can render.
        """
        send = self._dispatcher()
        send(
            self.channel,
            actions.tool_call_approved(
                self.turn_id,
                self.tool_call_id,
                confirmed=confirmed,
                selected_option_id=option_id,
                edited_tool_input=edited_input,
            ),
        )

    def deny(self, *, reason: actions.DenialReason = "denied") -> None:
        send = self._dispatcher()
        send(
            self.channel,
            actions.tool_call_denied(
                self.turn_id, self.tool_call_id, reason=reason, selected_option_id=None
            ),
        )

    def _dispatcher(self) -> Dispatcher:
        """Resolved *before* the action is built.

        A caller that constructed this event without a dispatcher has a wiring
        bug of its own; complaining first about an id missing from the host's
        action would send them looking at the wrong peer.
        """
        if self.dispatch is None:
            raise RuntimeError("this event was constructed without a dispatcher")
        return self.dispatch


@dataclass(frozen=True, slots=True)
class ToolCallRunning(_ToolCall):
    """A tool call that was **auto-confirmed**: there is nothing to answer.

    ``chat/toolCallReady`` carrying ``confirmed`` "transitions directly to
    `running`", and a host emits exactly that for every call it does not gate --
    including every client-provided tool, where ``confirmed: "not-needed"`` is
    what hands execution to the owning client. Delivering those as a
    :class:`ToolCallReady` asks a human about the common path, and the approval
    that follows is refused with "no tool call awaiting that id", because the
    call was never pending in the first place.
    """

    tool_name: str = ""

    @property
    def confirmed(self) -> str:
        """``ToolCallConfirmationReason``: `not-needed`, `user-action`, `setting`."""
        return str(self.action.get("confirmed", ""))

    @property
    def contributor(self) -> JsonObject | None:
        """``{"kind": "client", "clientId": ...}`` means **we** run this one."""
        raw = self.action.get("contributor")
        return raw if isinstance(raw, dict) else None


@dataclass(frozen=True, slots=True)
class ToolCallDelta(_ToolCall):
    """Streaming partial *parameters*, and the progress line above them.

    Both fields are optional on the action: a host that only revises the
    invocation message sends no ``content``, and vice versa.
    """

    @property
    def content(self) -> str:
        """Partial parameter text to append -- not tool output.

        Tool output while running arrives as
        :class:`ToolCallContentChanged`, which *replaces* rather than appends.
        """
        return str(self.action.get("content", ""))

    @property
    def invocation_message(self) -> str:
        return _flatten_markdown(self.action.get("invocationMessage"))


@dataclass(frozen=True, slots=True)
class ToolCallContentChanged(_ToolCall):
    """Partial output from a tool that is still running.

    ``content`` **replaces** the running call's content array rather than
    extending it, so a consumer that appends these renders every prefix of a
    terminal's output in turn.
    """

    @property
    def content(self) -> Sequence[JsonObject]:
        raw = self.action.get("content")
        return [c for c in raw if isinstance(c, dict)] if isinstance(raw, list) else []


@dataclass(frozen=True, slots=True)
class ToolCallResultReview(_ToolCall):
    """``requiresResultConfirmation``. Ignoring this hangs the turn forever.

    The trigger is a ``chat/toolCallComplete`` carrying
    ``requiresResultConfirmation``, not an action type of its own --
    ``chat/toolCallResultReview``, which this was once keyed on, is in no
    version of the schema. So the review IS the completion, and a consumer
    matching only :class:`ToolCallCompleted` answers nothing while the tool call
    sits in ``pending-result-confirmation`` and the turn waits.
    """

    dispatch: Dispatcher | None = None
    tool_name: str = ""

    @property
    def result(self) -> Any:
        """``ToolCallResult`` -- what is being reviewed. There is no reviewing
        it without reading it."""
        return self.action.get("result")

    def confirm(self) -> None:
        self._send(True)

    def reject(self) -> None:
        self._send(False)

    def _send(self, approved: bool) -> None:
        if self.dispatch is None:
            raise RuntimeError("this event was constructed without a dispatcher")
        self.dispatch(
            self.channel,
            actions.tool_call_result_confirmed(self.turn_id, self.tool_call_id, approved=approved),
        )


@dataclass(frozen=True, slots=True)
class ToolCallCompleted(_ToolCall):
    @property
    def result(self) -> Any:
        return self.action.get("result")


@dataclass(frozen=True, slots=True)
class InputRequested(_Base):
    """An elicitation. Carries its own answer, like a tool approval does.

    Everything about the request is under ``action["request"]``:
    ``ChatInputRequestedAction`` is ``{type, request}`` and has no ``requestId``
    property at all. Reading one yields ``""``, and the host answers "no open
    input request" -- which reads like another client got there first.
    """

    dispatch: Dispatcher | None = None

    @property
    def request(self) -> JsonObject:
        """``ChatInputRequest``: ``{id, message?, url?, questions?, answers?}``."""
        raw = self.action.get("request")
        return raw if isinstance(raw, dict) else {}

    @property
    def request_id(self) -> str:
        return str(self.request.get("id", ""))

    @property
    def message(self) -> str:
        """The display message for the request as a whole."""
        return str(self.request.get("message", ""))

    @property
    def url(self) -> str:
        """The page the user should review, for a URL-style elicitation.

        Such a request often carries no questions at all, so a consumer that
        renders only :attr:`questions` shows an empty prompt for it.
        """
        return str(self.request.get("url", ""))

    @property
    def questions(self) -> Sequence[JsonObject]:
        """The ordered ``ChatInputQuestion``s, in the host's own order.

        A closed vocabulary of five kinds -- ``text``, ``number``, ``integer``,
        ``boolean``, ``single-select``, ``multi-select`` -- each with its own
        constraints (``format``, ``min``/``max``, ``options``,
        ``allowFreeformInput``, ``defaultValue``). Without them a consumer
        cannot render the prompt at all, whatever else the event exposes.
        """
        raw = self.request.get("questions")
        return [q for q in raw if isinstance(q, dict)] if isinstance(raw, list) else []

    @property
    def answers(self) -> JsonObject:
        """Drafts already synced, keyed by question id -- possibly another
        client's, mid-answer."""
        raw = self.request.get("answers")
        return raw if isinstance(raw, dict) else {}

    def answer(self, answers: Mapping[str, Any] | None = None) -> None:
        """Accept, submitting *answers* keyed by question id.

        Each value is encoded from its own question's kind, so ``{"style":
        "shout"}`` answers a single-select as ``selected`` rather than ``text``;
        a value that already carries a ``state`` is passed through untouched.
        Answers are an overlay on the synced drafts, not a replacement.
        """
        self._send("accept", elicitation.encode_answers(answers or {}, self.questions))

    def decline(self) -> None:
        """The user refuses. The agent is told and carries on -- unlike
        :meth:`cancel`, which says the interaction itself is over."""
        self._send("decline", {})

    def cancel(self) -> None:
        self._send("cancel", {})

    def _send(self, response: elicitation.ResponseKind, answers: JsonObject) -> None:
        if self.dispatch is None:
            raise RuntimeError("this event was constructed without a dispatcher")
        self.dispatch(
            self.channel,
            elicitation.input_completed(self.request_id, response, answers),
        )


@dataclass(frozen=True, slots=True)
class Usage(_Base):
    pass


@dataclass(frozen=True, slots=True)
class TitleChanged(_Base):
    @property
    def title(self) -> str:
        return str(self.action.get("title", ""))


@dataclass(frozen=True, slots=True)
class Reconnected(_Base):
    """The supervisor rebuilt the connection under a live turn."""


@dataclass(frozen=True, slots=True)
class TurnCompleted(_Base):
    #: Authoritative text, read from the mirror rather than accumulated.
    text: str = ""


@dataclass(frozen=True, slots=True)
class TurnFailed(_Base):
    """A turn that will produce nothing further. Terminal.

    Minted from a ``chat/error``, and also by :class:`TurnStream` for the two
    failures the protocol gives no action for -- a rejected ``chat/turnStarted``
    and a disposal mid-turn. ``reason`` is a field rather than a property for
    exactly that: those two have no ``ErrorInfo`` to read it from.
    """

    reason: str = ""

    @property
    def error_type(self) -> str:
        """``ErrorInfo.errorType`` -- the machine-readable half, e.g.
        ``agent.turn``. Empty for a failure this client synthesised."""
        raw = self.action.get("error")
        return str(raw.get("errorType", "")) if isinstance(raw, Mapping) else ""


@dataclass(frozen=True, slots=True)
class TurnCancelled(_Base):
    pass


@dataclass(frozen=True, slots=True)
class UnknownEvent(_Base):
    """An action type we do not model.

    It still reaches the caller with its envelope, and the reducer still applied
    it -- forward compatibility is a protocol requirement, not a courtesy.
    """


TurnEvent: TypeAlias = (
    TurnStarted
    | TurnInProgress
    | Delta
    | Reasoning
    | ResponsePartAdded
    | ToolCallStarted
    | ToolCallReady
    | ToolCallRunning
    | ToolCallDelta
    | ToolCallContentChanged
    | ToolCallResultReview
    | ToolCallCompleted
    | InputRequested
    | Usage
    | TitleChanged
    | Reconnected
    | TurnCompleted
    | TurnFailed
    | TurnCancelled
    | UnknownEvent
)

#: Action type -> event class. Anything absent becomes `UnknownEvent`.
#:
#: Every key is a real action type and every chat action is either here or in
#: `_NOT_MODELLED`; `tests/client/test_events.py` holds both halves against
#: `ACTION_TYPES`. Four keys here were once spelled from memory
#: (`chat/turnFailed`, `chat/reasoningDelta`, `chat/titleChanged`,
#: `chat/toolCallResultReview`), and an invented key fails exactly the way a
#: missing one does -- silently, as `UnknownEvent` -- so nothing caught them.
_BY_TYPE: dict[str, Any] = {
    "chat/turnStarted": TurnStarted,
    "chat/delta": Delta,
    "chat/reasoning": Reasoning,
    "chat/responsePart": ResponsePartAdded,
    "chat/toolCallStart": ToolCallStarted,
    "chat/toolCallReady": ToolCallReady,
    "chat/toolCallDelta": ToolCallDelta,
    "chat/toolCallContentChanged": ToolCallContentChanged,
    "chat/toolCallComplete": ToolCallCompleted,
    "chat/inputRequested": InputRequested,
    "chat/usage": Usage,
    "chat/turnComplete": TurnCompleted,
    "chat/error": TurnFailed,
    "chat/turnCancelled": TurnCancelled,
    # Not a chat action: a session's title lives on the session channel. The
    # turn views filter to their own chat URI, so this only fires for a caller
    # feeding `client.events()` through `event_for` directly.
    "session/titleChanged": TitleChanged,
}

#: Chat actions with no turn event, and the reason each earns the omission.
#: Listed rather than implied, so adding an action upstream fails a test instead
#: of quietly landing in `UnknownEvent`.
_NOT_MODELLED: frozenset[str] = frozenset(
    {
        # Dispatched by a client, never something a turn observer must react to.
        # The echo is the mirror's business, not the caller's.
        "chat/draftChanged",
        "chat/inputAnswerChanged",
        "chat/inputCompleted",
        "chat/pendingMessageRemoved",
        "chat/pendingMessageSet",
        "chat/queuedMessagesReordered",
        "chat/toolCallConfirmed",
        "chat/toolCallResultConfirmed",
        "chat/truncated",
        "chat/workingDirectoryRemoved",
        "chat/workingDirectorySet",
        # Chat-level state rather than turn progress: `activityChanged` drives a
        # spinner and `turnsLoaded` answers a `fetchTurns` for history.
        "chat/activityChanged",
        "chat/turnsLoaded",
        # A turn blocked on an OAuth flow the client cannot complete through
        # this surface. Modelling it without a way to answer it would offer a
        # handle that does nothing.
        "chat/toolCallAuthRequired",
        "chat/toolCallAuthResolved",
    }
)


def is_modelled(envelope: Mapping[str, Any]) -> bool:
    """Whether this envelope is one a turn observer should be handed.

    `UnknownEvent` is FORWARD COMPATIBILITY -- a newer host sent an action this
    build does not know, and the caller still gets to see it. `_NOT_MODELLED`
    is a decision: an action we know about and deliberately do not surface,
    mostly the caller's own writes echoing back.

    Delivering both as `UnknownEvent` conflates them, and the conflation has a
    cost: a caller watching for `UnknownEvent` to detect a version mismatch got
    one on every turn it approved a tool or answered a question.
    """
    raw = envelope.get("action")
    action_type = raw.get("type") if isinstance(raw, Mapping) else None
    return str(action_type) not in _NOT_MODELLED


#: `ToolCallReady` and `ToolCallResultReview` also answer, and are built in the
#: branches below, where their state-derived fields are resolved too.
_NEEDS_DISPATCH = {InputRequested}


def event_for(
    envelope: Mapping[str, Any],
    dispatch: Dispatcher | None = None,
    tools: ToolLookup | None = None,
) -> TurnEvent:
    """Wrap one envelope. Never raises; an unknown type is still delivered.

    Two action types decode to more than one event, because the schema
    overloads them and the difference is what the caller must do:
    ``chat/toolCallReady`` is a question only when it carries no ``confirmed``,
    and ``chat/toolCallComplete`` is a *review* when it carries
    ``requiresResultConfirmation``.

    *tools* supplies what no chat action carries -- see :class:`ToolInfo`.
    Without it the events still arrive; a policy that switches on the tool name
    or on `readOnlyHint` just has nothing to switch on.
    """
    raw_action = envelope.get("action")
    action: Mapping[str, Any] = raw_action if isinstance(raw_action, Mapping) else {}
    cls = _BY_TYPE.get(str(action.get("type")), UnknownEvent)
    payload = dict(envelope)
    if cls is ToolCallReady:
        info = _tool_info(tools, payload, action)
        # "If set, the tool was auto-confirmed and transitions directly to
        # `running`" -- a state transition, not a question.
        if action.get("confirmed"):
            return ToolCallRunning(payload, info.name)
        return ToolCallReady(payload, dispatch, info.name, info.annotations)
    if cls is ToolCallCompleted and action.get("requiresResultConfirmation"):
        return ToolCallResultReview(payload, dispatch, _tool_info(tools, payload, action).name)
    if cls in _NEEDS_DISPATCH:
        return cls(payload, dispatch)  # type: ignore[no-any-return]
    if cls is TurnFailed:
        # `reason` is the field the dataclass advertises, and a caller writing
        # `case TurnFailed(reason=r)` gets "" unless it is filled here --
        # `ErrorInfo.message` is required, so there is always something to read.
        return TurnFailed(payload, _error_message(payload))
    return cls(payload)  # type: ignore[no-any-return]


def _tool_info(
    tools: ToolLookup | None, envelope: Mapping[str, Any], action: Mapping[str, Any]
) -> ToolInfo:
    if tools is None:
        return ToolInfo()
    return tools(str(envelope.get("channel", "")), str(action.get("toolCallId", "")))


def _error_message(envelope: Mapping[str, Any]) -> str:
    action = envelope.get("action")
    error = action.get("error") if isinstance(action, Mapping) else None
    return str(error.get("message", "")) if isinstance(error, Mapping) else ""


def _flatten_markdown(raw: Any) -> str:
    """`StringOrMarkdown` is a bare string *or* `{markdown: str}`.

    Both render as text; only the formatting differs. Branching on that is the
    caller's problem to opt into via `.action`, not one to force on everyone.
    """
    if isinstance(raw, Mapping):
        return str(raw.get("markdown", ""))
    return str(raw) if isinstance(raw, str) else ""
