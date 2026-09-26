"""Terminals: the claim, the stream, the exit, and the ``!`` shorthand.

The terminal channel is the only one in this protocol whose state is
**contested**. Everywhere else a client is either the originator of a change or a
spectator of one; here `terminal/claimed` is an *arbitration*, and the answer
decides whether the caller may type at all. Three consequences shape this module:

* **The claim is read from confirmed state, never optimistic.** ``terminal/claimed``
  is client-dispatchable and the host may refuse it, so replaying our own
  un-echoed claim would answer "yes, you hold this" for an arbitration that has
  not happened yet. *Render optimistic, trust confirmed* is the mirror's rule;
  a claim is the case where the difference is the whole question.
* **A refused claim is somebody's news, not everybody's state.** The host fans a
  rejected envelope out to *every* subscriber with its own state untouched --
  the interop run caught each non-originating peer applying it. The mirror
  already declines to reduce it; :class:`TerminalRefused` is how a stream reader
  hears about it, with :attr:`TerminalRefused.mine` separating "your keystrokes
  went nowhere" from "another client just lost an argument".
* **Only :meth:`Terminal.write` is gated locally.** ``dispatchAction`` is a
  notification, so a refused keystroke comes back as a `rejectionReason` on a
  stream nobody is obliged to read: the failure a caller reports is "typing does
  nothing". Raising :class:`TerminalNotHeld` names the holder instead. The
  sibling actions are deliberately *not* gated -- see :meth:`Terminal.hand_to`
  and :meth:`Terminal.resize`.

**Nothing here routes on a URI scheme.** The reducer is bound at subscribe time
from the kind we already know. VS Code mints three ``agenthost-terminal:`` forms,
the spec's examples use ``ahp-terminal:``, and a scheme test binds no reducer at
all -- which freezes the state silently while output keeps arriving.

**Disposal is not claim-gated, on purpose.** The sibling host's
`core/terminals.py` explains why and we do not fight it: a session claim is held
by no client, so gating disposal would make every handed-over terminal immortal
-- the shell would outlive the client that asked for it. :meth:`Terminal.dispose`
therefore works from any peer that can name the channel, and that asymmetry is
documented rather than hidden.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import TracebackType
from typing import TYPE_CHECKING, Any, Final, Self, TypeAlias, TypeGuard

from ahp_protocol.types import JsonObject

from ahp_client.client.errors import AhpClientError, InvalidArgument, RequestTimeout
from ahp_client.client.events import ActionEvent

if TYPE_CHECKING:  # pragma: no cover - a type-only edge, and a cycle at runtime
    from ahp_client.api.client import Client

__all__ = [
    "UNREADABLE_CLAIM",
    "ClientClaim",
    "SessionClaim",
    "Terminal",
    "TerminalClaim",
    "TerminalClaimed",
    "TerminalCleared",
    "TerminalCommand",
    "TerminalCommandDetected",
    "TerminalCommandExecuted",
    "TerminalCommandFinished",
    "TerminalCwdChanged",
    "TerminalEvent",
    "TerminalExited",
    "TerminalInfo",
    "TerminalNotHeld",
    "TerminalOutput",
    "TerminalRefused",
    "TerminalResized",
    "TerminalStream",
    "TerminalTitleChanged",
    "UnknownTerminalEvent",
    "claim_from_wire",
    "describe_claim",
    "new_terminal_uri",
    "split_terminal_command",
    "terminal_event_for",
    "terminal_uris",
]


# ── claims ───────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class ClientClaim:
    """``{kind: 'client', clientId}`` -- a peer driving the terminal itself.

    This is the claim that lets you type. :attr:`Terminal.held_by_us` is an
    equality on ``clientId``: nothing else in the protocol confers input rights
    on a terminal.
    """

    client_id: str

    def __post_init__(self) -> None:
        # An empty id is schema-legal and permanently strands the terminal:
        # `terminal/claimed` is itself claim-gated, so once the holder is a
        # `clientId` no connection has, *no* peer can take it back and disposal
        # is the only operation left. One unset config value gets there.
        if not self.client_id:
            raise InvalidArgument(
                "a client claim needs a clientId; an empty one names no connection, "
                "and terminal/claimed is claim-gated so nobody could take it back"
            )

    def to_wire(self) -> JsonObject:
        return {"kind": "client", "clientId": self.client_id}


@dataclass(frozen=True, slots=True)
class SessionClaim:
    """``{kind: 'session', session, chat, turnId?, toolCallId?}``.

    ``chat`` -- the chat that owns the terminal -- is required on the wire since
    0.9.0, and a 0.9.0 host refuses a session claim without one. It is optional
    here only because a host on an earlier version sends claims that lack it,
    and this client still has to read those.

    The optional pair is the entire difference between "a tool call is using
    this right now" and "backgrounded, still owned by the session". Narrowing to
    a bare ``session`` is how the guide's detach flow works, and it is why
    :meth:`Terminal.hand_to` refuses nothing locally.

    **A session claim is held by no client**, so a terminal in this state takes
    input from nobody -- which is the point: it belongs to the agent.
    """

    session: str
    turn_id: str | None = None
    tool_call_id: str | None = None
    chat: str | None = None

    def __post_init__(self) -> None:
        # Same trap as `ClientClaim`, and the same permanence: a terminal handed
        # to a session nothing owns cannot be taken back by any peer.
        if not self.session:
            raise InvalidArgument(
                "a session claim needs a session URI; handing a terminal to an empty one "
                "strands it, because terminal/claimed is claim-gated"
            )

    def to_wire(self) -> JsonObject:
        claim: JsonObject = {"kind": "session", "session": self.session}
        if self.chat is not None:
            claim["chat"] = self.chat
        # Absent, not `null`: both are declared optional, and an explicit null is
        # a different document -- one an unconditional JS spread writes through.
        if self.turn_id is not None:
            claim["turnId"] = self.turn_id
        if self.tool_call_id is not None:
            claim["toolCallId"] = self.tool_call_id
        return claim


TerminalClaim: TypeAlias = ClientClaim | SessionClaim


def claim_from_wire(value: Any) -> TerminalClaim | None:
    """Parse a ``TerminalClaim``, or ``None`` for anything that is not one.

    Every field is type-*checked* rather than coerced. ``terminal/claimed`` is
    client-dispatchable, so this payload is arbitrary peer JSON: a ``clientId``
    of ``123`` must not become the claim of a client named ``"123"``, because
    that is the comparison :attr:`Terminal.held_by_us` makes before deciding a
    caller may type into somebody else's shell.

    ``None`` therefore means *unreadable*, and every caller here treats it the
    way it treats "held by someone else" -- closed, not open. An **empty**
    ``clientId`` or ``session`` is unreadable in that sense too: it is
    schema-legal and names nothing, so it must not compare equal to a real
    connection. Never raises -- this parses whatever arrived.
    """
    if not isinstance(value, Mapping):
        return None
    kind = value.get("kind")
    if kind == "client":
        client_id = value.get("clientId")
        return ClientClaim(client_id) if isinstance(client_id, str) and client_id else None
    if kind == "session":
        session = value.get("session")
        if not isinstance(session, str) or not session:
            return None
        turn_id = value.get("turnId")
        tool_call_id = value.get("toolCallId")
        chat = value.get("chat")
        return SessionClaim(
            session,
            turn_id if isinstance(turn_id, str) else None,
            tool_call_id if isinstance(tool_call_id, str) else None,
            chat if isinstance(chat, str) and chat else None,
        )
    return None


#: What :func:`describe_claim` says when the claim is present and unparseable.
#: Kept distinct from the two *absence* answers :attr:`Terminal.holder` gives,
#: because "this build cannot read the claim" sends the reader hunting a host
#: bug and the far commoner causes -- disposed, never subscribed, resume
#: failed -- are not host bugs at all. ``TerminalState.claim`` is **required**,
#: so a missing one is always a missing *channel*, never a malformed claim.
UNREADABLE_CLAIM: Final = "an unreadable claim"


def describe_claim(claim: TerminalClaim | None) -> str:
    """Who holds this terminal, for a message a person reads.

    ``None`` here means *the claim in state could not be parsed*, which is a
    real answer rather than an absence to render blank. It does **not** mean
    "no claim": see :attr:`Terminal.holder`, which separates the three.
    """
    if isinstance(claim, ClientClaim):
        return f"client {claim.client_id!r}"
    if isinstance(claim, SessionClaim):
        scope = "".join(
            f", {name}={value!r}"
            for name, value in (
                ("chat", claim.chat),
                ("turnId", claim.turn_id),
                ("toolCallId", claim.tool_call_id),
            )
            if value is not None
        )
        return f"session {claim.session!r}{scope}"
    return UNREADABLE_CLAIM


class TerminalNotHeld(AhpClientError):
    """This client does not hold the terminal, so its input would be refused."""

    def __init__(
        self, uri: str, claim: TerminalClaim | None, client_id: str, *, holder: str | None = None
    ) -> None:
        super().__init__(
            f"client {client_id!r} may not type into terminal {uri}: it is held by "
            f"{holder or describe_claim(claim)}. Input from a peer that does not hold the "
            "claim is refused."
        )
        self.uri = uri
        self.claim = claim
        #: Us. Carried rather than only formatted into the message, because a
        #: caller comparing it against `TerminalInfo.claim` is the one branch
        #: worth taking on this error.
        self.client_id = client_id


# ── the `!` shorthand ────────────────────────────────────────────────────────


def split_terminal_command(text: str, prefix: str | None) -> str | None:
    """The command *text* asks the **host** to run, or ``None`` for a message.

    ``InitializeResult.terminalCommandPrefix`` is a "prefix that the host
    recognizes at the start of a user `Message.text` as a shorthand for executing
    the remainder as a terminal command". So a `chat/turnStarted` carrying
    ``"!ls"`` never reaches the agent -- and until the handshake fix landed, no
    client could learn that, which is why this is the affordance the surface is
    for.

    *prefix* is the **negotiated** one and ``None`` is a real answer: "absence
    means the host does not support command prefixes". A client that hardcodes
    ``"!"`` offers the shortcut to hosts that never claimed it, and withholds it
    from a host that spells it differently.

    **The strip and the blank-remainder rule are a heuristic, not the protocol.**
    The spec says only "shorthand for executing the remainder as a terminal
    command"; the trimming below is calibrated against the reference host's
    `_terminal_command`, where ``command = text[len(prefix):].strip()`` and an
    empty one goes to the agent -- so ``"!"`` alone is a message, and badging it
    "will run" would promise a shell on something a model is about to answer.
    A host that trims nothing will be mispredicted at the edges, and a caller who
    needs only what the protocol guarantees should read
    :attr:`~ahp_client.api.client.Client.terminal_command_prefix` itself.
    """
    if not prefix or not text.startswith(prefix):
        return None
    return text[len(prefix) :].strip() or None


# ── events ───────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class _TerminalBase:
    envelope: JsonObject

    @property
    def channel(self) -> str:
        return str(self.envelope.get("channel", ""))

    @property
    def action(self) -> JsonObject:
        raw = self.envelope.get("action")
        return raw if isinstance(raw, dict) else {}


@dataclass(frozen=True, slots=True)
class TerminalOutput(_TerminalBase):
    """``terminal/data`` -- output from the pty, server-authoritative.

    ``data`` may carry VT escape sequences unless :attr:`Terminal.is_pty` is
    ``False``. Shell-integration markers are stripped by the host before this is
    published; nothing else is.
    """

    @property
    def data(self) -> str:
        return str(self.action.get("data", ""))


@dataclass(frozen=True, slots=True)
class TerminalResized(_TerminalBase):
    @property
    def cols(self) -> int:
        return _as_int(self.action.get("cols"))

    @property
    def rows(self) -> int:
        return _as_int(self.action.get("rows"))


@dataclass(frozen=True, slots=True)
class TerminalClaimed(_TerminalBase):
    """The terminal changed hands -- **and it was accepted**.

    A refused handover never becomes one of these: it arrives as
    :class:`TerminalRefused`, because the host left its own state untouched and
    no peer may apply it.
    """

    #: Whether **this client dispatched** the handover -- the same question
    #: :attr:`TerminalRefused.mine` answers, deliberately. One field name on a
    #: union must ask one question: `mine` used to mean "I originated it" on a
    #: refusal and "I am the new holder" here, so `case ...(mine=True)` over
    #: `TerminalEvent` silently changed meaning per arm.
    mine: bool = False
    #: Whether the new holder is this client. The reason this is a field rather
    #: than something the caller recomputes: it is the answer to "may I still
    #: type", and deriving it needs a `clientId` an event does not carry.
    held_by_us: bool = False

    @property
    def claim(self) -> TerminalClaim | None:
        return claim_from_wire(self.action.get("claim"))


@dataclass(frozen=True, slots=True)
class TerminalTitleChanged(_TerminalBase):
    @property
    def title(self) -> str:
        return str(self.action.get("title", ""))


@dataclass(frozen=True, slots=True)
class TerminalCwdChanged(_TerminalBase):
    @property
    def cwd(self) -> str:
        """A **URI**, not a path -- as is ``TerminalState.cwd``."""
        return str(self.action.get("cwd", ""))


@dataclass(frozen=True, slots=True)
class TerminalCleared(_TerminalBase):
    """Somebody wiped the scrollback. ``terminal/cleared`` is client-dispatchable
    and ungated, so "somebody" is any peer that can name this channel."""


@dataclass(frozen=True, slots=True)
class TerminalExited(_TerminalBase):
    """The process ended. Terminal for the *process*, not for the channel: the
    state survives until someone disposes it, so the scrollback stays readable."""

    @property
    def exit_code(self) -> int | None:
        """``None`` when the process was killed without reporting one -- which
        the action expresses by omitting the field, not by sending null."""
        return _as_exit_code(self.action.get("exitCode"))


@dataclass(frozen=True, slots=True)
class TerminalCommandDetected(_TerminalBase):
    """``terminal/commandDetectionAvailable`` -- shell integration has loaded.

    It can arrive long after the terminal was created, which is why
    :attr:`Terminal.supports_command_detection` is a flag to check rather than a
    property of the creation.
    """


@dataclass(frozen=True, slots=True)
class TerminalCommandExecuted(_TerminalBase):
    """A command was submitted. Every :class:`TerminalOutput` until the matching
    :class:`TerminalCommandFinished` is this command's output."""

    @property
    def command_id(self) -> str:
        return str(self.action.get("commandId", ""))

    @property
    def command_line(self) -> str:
        return str(self.action.get("commandLine", ""))

    @property
    def timestamp(self) -> float:
        """Unix ms, **measured on the server**. A number, not an ISO string."""
        return _as_float(self.action.get("timestamp"))


@dataclass(frozen=True, slots=True)
class TerminalCommandFinished(_TerminalBase):
    @property
    def command_id(self) -> str:
        return str(self.action.get("commandId", ""))

    @property
    def exit_code(self) -> int | None:
        """``None`` when the shell reported none -- distinct from ``0``."""
        return _as_exit_code(self.action.get("exitCode"))

    @property
    def duration_ms(self) -> float | None:
        """Measured by the shell-integration script on the server.

        ``None`` rather than ``0.0`` when absent: a shell that reports no
        duration and a command that took no time are different facts, and
        rendering the second for the first makes every command look instant.
        """
        raw = self.action.get("durationMs")
        return float(raw) if isinstance(raw, int | float) and not isinstance(raw, bool) else None


@dataclass(frozen=True, slots=True)
class TerminalRefused(_TerminalBase):
    """An action on this terminal the host **did not apply**.

    The envelope is fanned out to every subscriber while the host's own state is
    left alone, so this is the one event that must not be reduced -- by anybody.
    The interop run found each non-originating peer applying a refused
    ``terminal/claimed``, after which its idea of the owner was permanently wrong
    and no later action corrected it.

    :attr:`mine` is the difference between a report and a diagnosis: for our own
    action it is the answer to "why did nothing happen when I typed".
    """

    reason: str = ""
    #: Whether this client originated the refused action.
    mine: bool = False


@dataclass(frozen=True, slots=True)
class UnknownTerminalEvent(_TerminalBase):
    """A terminal action this build does not model. Delivered anyway -- the
    reducer applied it, and forward compatibility is a protocol requirement."""


TerminalEvent: TypeAlias = (
    TerminalOutput
    | TerminalResized
    | TerminalClaimed
    | TerminalTitleChanged
    | TerminalCwdChanged
    | TerminalCleared
    | TerminalExited
    | TerminalCommandDetected
    | TerminalCommandExecuted
    | TerminalCommandFinished
    | TerminalRefused
    | UnknownTerminalEvent
)

#: Action type -> event class. `tests/client/test_terminals.py` holds this map
#: plus `_NOT_MODELLED` against every `terminal/` entry in `ACTION_TYPES`, so an
#: invented key -- which fails exactly the way a missing one does, silently, as
#: `UnknownTerminalEvent` -- cannot survive.
_BY_TYPE: dict[str, Any] = {
    "terminal/data": TerminalOutput,
    "terminal/resized": TerminalResized,
    "terminal/claimed": TerminalClaimed,
    "terminal/titleChanged": TerminalTitleChanged,
    "terminal/cwdChanged": TerminalCwdChanged,
    "terminal/cleared": TerminalCleared,
    "terminal/exited": TerminalExited,
    "terminal/commandDetectionAvailable": TerminalCommandDetected,
    "terminal/commandExecuted": TerminalCommandExecuted,
    "terminal/commandFinished": TerminalCommandFinished,
}

#: The one terminal action with no event, and the reason it earns the omission:
#: `terminal/input` is side-effect-only and the reducer no-ops it, so the echo is
#: our own keystrokes coming back. Surfacing it would double every character a
#: caller renders from `terminal/data`.
_NOT_MODELLED: frozenset[str] = frozenset({"terminal/input"})


def terminal_event_for(envelope: Mapping[str, Any], *, client_id: str = "") -> TerminalEvent:
    """Wrap one ``ActionEnvelope`` from a terminal channel. Never raises.

    *client_id* answers the two "is this about me" questions -- a refusal and a
    handover -- which the envelope alone cannot, since ``origin`` is absent on
    anything the server originated.
    """
    raw_action = envelope.get("action")
    action: Mapping[str, Any] = raw_action if isinstance(raw_action, Mapping) else {}
    payload = dict(envelope)
    origin = envelope.get("origin")
    mine = isinstance(origin, Mapping) and origin.get("clientId") == client_id

    rejection = envelope.get("rejectionReason")
    if isinstance(rejection, str):
        # BEFORE the type lookup, deliberately: a refused `terminal/claimed`
        # decoded as `TerminalClaimed` tells the caller the terminal changed
        # hands when the host's state says it did not.
        return TerminalRefused(payload, rejection, mine)

    cls = _BY_TYPE.get(str(action.get("type")), UnknownTerminalEvent)
    if cls is TerminalClaimed:
        claim = claim_from_wire(action.get("claim"))
        held = isinstance(claim, ClientClaim) and claim.client_id == client_id
        return TerminalClaimed(payload, mine, held)
    return cls(payload)  # type: ignore[no-any-return]


def is_modelled(envelope: Mapping[str, Any]) -> bool:
    """Whether a terminal envelope is one a stream reader should be handed.

    **The refusal check comes first**, exactly as `TurnStream` orders it, and for
    a reason that is sharper here: the only action in ``_NOT_MODELLED`` is
    ``terminal/input``, and ``terminal/input`` is one of the two the host gates
    on the claim. An *accepted* echo of our own keystrokes is noise -- the pty
    sends the same bytes back as ``terminal/data`` -- while a *refused* one is
    the single event this whole module exists to deliver, and it arrives on a
    notification with no reply to inspect. Filtering by action type alone
    destroyed it one line before the decoder that would have named it.
    """
    if isinstance(envelope.get("rejectionReason"), str):
        return True
    raw = envelope.get("action")
    action_type = raw.get("type") if isinstance(raw, Mapping) else None
    return str(action_type) not in _NOT_MODELLED


# ── state views ──────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class TerminalCommand:
    """One ``command`` content part -- a command the shell has run.

    The durable half of what :class:`TerminalCommandExecuted` and
    :class:`TerminalCommandFinished` report, and typed the same way on purpose:
    ``exitCode`` and ``durationMs`` are optional ``number``s, and ``0`` is a
    different fact from "not reported" in both places.
    """

    raw: JsonObject

    @property
    def id(self) -> str:
        return str(self.raw.get("commandId", ""))

    @property
    def command_line(self) -> str:
        return str(self.raw.get("commandLine", ""))

    @property
    def output(self) -> str:
        """This command's own output, already separated by the host."""
        return str(self.raw.get("output", ""))

    @property
    def complete(self) -> bool:
        return self.raw.get("isComplete") is True

    @property
    def exit_code(self) -> int | None:
        return _as_exit_code(self.raw.get("exitCode"))

    @property
    def duration_ms(self) -> float | None:
        raw = self.raw.get("durationMs")
        return float(raw) if _is_number(raw) else None

    @property
    def timestamp(self) -> float:
        """Unix ms, measured on the server."""
        return _as_float(self.raw.get("timestamp"))


@dataclass(frozen=True, slots=True)
class TerminalInfo:
    """One ``RootState.terminals`` entry -- the catalogue row, not the terminal.

    Typed because of :attr:`claim`. It is "what a client reads to decide whether
    to offer an input box at all", and deciding it off a raw dict means writing
    ``info["claim"]["clientId"] == client_id`` by hand -- the exact comparison
    :func:`claim_from_wire` exists to guard, on the exact payload (arbitrary peer
    JSON, where a ``clientId`` of ``123`` must not match the client named
    ``"123"``) it exists to guard it on.
    """

    raw: JsonObject
    #: This connection's id, so :attr:`held_by_us` needs no second argument.
    client_id: str = ""

    @property
    def uri(self) -> str:
        """``TerminalInfo.resource`` -- the channel to
        :meth:`~ahp_client.api.client.Client.open_terminal`."""
        return str(self.raw.get("resource", ""))

    @property
    def title(self) -> str:
        return str(self.raw.get("title", ""))

    @property
    def claim(self) -> TerminalClaim | None:
        return claim_from_wire(self.raw.get("claim"))

    @property
    def held_by_us(self) -> bool:
        claim = self.claim
        return isinstance(claim, ClientClaim) and claim.client_id == self.client_id

    @property
    def holder(self) -> str:
        return describe_claim(self.claim)

    @property
    def exit_code(self) -> int | None:
        return _as_exit_code(self.raw.get("exitCode"))


def terminal_uris(content: Sequence[Any]) -> Sequence[str]:
    """The terminal URIs inside a tool call's content parts.

    The one **receiver-assigned** terminal channel there is.
    ``CreateTerminalParams.channel`` is client-chosen, so
    :meth:`~ahp_client.api.client.Client.create_terminal` mints its own --
    but ``ToolResultTerminalContent.resource`` arrives from the host inside
    `ToolCallContentChanged.content`, and "clients can subscribe to the
    terminal's URI to stream its output in real time, providing live feedback
    while a tool is executing". Watching the agent's shell command is the
    marquee use of this surface and the only place the spec's *subscribe to what
    comes back* advice applies to a terminal, so digging it out with
    ``[c for c in content if c["type"] == "terminal"]`` should not be the caller's
    job. Hand the result to
    :meth:`~ahp_client.api.client.Client.open_terminal`.
    """
    return [
        str(part["resource"])
        for part in content
        if isinstance(part, Mapping)
        and part.get("type") == "terminal"
        and isinstance(part.get("resource"), str)
    ]


# ── the stream ───────────────────────────────────────────────────────────────


class TerminalStream:
    """Everything happening on one terminal, from now.

    Built on ``client.events()`` rather than on a per-channel subscription for
    the same reason :class:`~ahp_client.api.client.ChatWatch` is: the fan-in
    stream carries the refused envelopes too, and a refusal is the event this
    surface exists to deliver.
    """

    def __init__(self, client: Client, uri: str) -> None:
        self._client = client
        self._uri = uri
        self._reader: Any = None

    def __aiter__(self) -> TerminalStream:
        return self

    def _open(self) -> None:
        if self._reader is None:
            # Attached before the first read, so output arriving while the caller
            # is still setting up is buffered rather than missed.
            self._reader = self._client._runtime.events()

    async def __anext__(self) -> TerminalEvent:
        self._open()
        while True:
            try:
                tagged = await self._reader.__anext__()
            except StopAsyncIteration:
                raise StopAsyncIteration from None
            if not isinstance(tagged.event, ActionEvent):
                continue
            envelope = tagged.event.envelope
            if envelope.get("channel") != self._uri or not is_modelled(envelope):
                continue
            return terminal_event_for(envelope, client_id=self._client.client_id)

    async def aclose(self) -> None:
        if self._reader is not None:
            await self._reader.aclose()

    async def __aenter__(self) -> Self:
        self._open()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()


# ── the terminal ─────────────────────────────────────────────────────────────


class Terminal:
    """One terminal channel: its claim, its scrollback, and its process."""

    def __init__(self, client: Client, uri: str, *, owned: bool) -> None:
        self._client = client
        self.uri = uri
        #: Created by us, so `__aexit__` disposes it. A terminal we merely opened
        #: is somebody else's, and killing another client's shell on a `with`
        #: exit is the kind of surprise that loses trust.
        self._owned = owned

    # ── state ────────────────────────────────────────────────────────────────

    @property
    def alive(self) -> bool:
        """Whether this client is still subscribed to the terminal's channel.

        ``False`` after :meth:`dispose`, after a reconnect the host could not
        resume, and for a URI no host ever registered. It matters because every
        other property degrades to a *plausible* empty value once the channel is
        gone -- ``title`` is ``""``, ``output()`` is ``""`` on a terminal that
        had 40 KiB of scrollback, ``exit_reported`` is ``False``, and the holder
        reads as unparseable. A confidently wrong answer is worse than silence
        for a surface whose stated job is naming the holder, so the dispatchers
        check this first.

        It does **not** mean the process is running: see :attr:`exists`.
        """
        return self._client._runtime.subscribed(self.uri)

    @property
    def exists(self) -> bool:
        """Whether the host still lists this terminal in ``RootState.terminals``.

        The catalogue is the only place a *peer's* disposal shows up: dropping
        the channel is the host's business and our subscription is not told.
        Without this, a `Terminal` whose shell somebody else killed keeps
        answering ``held_by_us`` and accepting :meth:`write`.
        """
        return any(info.uri == self.uri for info in self._client.terminals())

    @property
    def state(self) -> JsonObject:
        """``TerminalState``, optimistic -- what a widget renders."""
        state = self._client.mirror.state(self.uri)
        return state if isinstance(state, dict) else {}

    @property
    def claim(self) -> TerminalClaim | None:
        """Who holds this terminal, from **confirmed** state.

        Not optimistic, and the distinction is the whole reason this surface
        exists. ``terminal/claimed`` is an arbitration the host may refuse, so
        our own un-echoed handover replayed on top of confirmed state would
        answer a question that has not been decided yet -- and the answer it
        gives is always "you won".

        ``None`` means either no claim has been seen or the one in state is
        unparseable. Both close the input gate; neither opens it.
        """
        confirmed = self._client.mirror.confirmed(self.uri)
        return claim_from_wire(confirmed.get("claim") if isinstance(confirmed, Mapping) else None)

    @property
    def held_by_us(self) -> bool:
        """Whether this client may type into the terminal.

        A session claim is held by *no* client -- not even the one that created
        the terminal and handed it over -- which is what makes a tool call's
        terminal the agent's rather than the user's.
        """
        claim = self.claim
        return isinstance(claim, ClientClaim) and claim.client_id == self._client.client_id

    @property
    def holder(self) -> str:
        """Who holds it, phrased for a person -- and *three* answers, not one.

        ``TerminalState.claim`` is a **required** field, so a state with no claim
        in it is never a malformed claim: it is a channel this client is not
        receiving. Collapsing the two made a disposed terminal report "held by an
        unreadable claim", which is a protocol-corruption message for the most
        ordinary lifecycle event there is.
        """
        if not self.alive:
            return "nobody: this client is not subscribed to it (disposed, or never opened)"
        confirmed = self._client.mirror.confirmed(self.uri)
        if not isinstance(confirmed, Mapping) or "claim" not in confirmed:
            return "nobody yet: no claim has reached this client"
        return describe_claim(claim_from_wire(confirmed.get("claim")))

    @property
    def title(self) -> str:
        return str(self.state.get("title", ""))

    @property
    def cwd(self) -> str:
        """The process's working directory as a **URI**, or ``""``."""
        return str(self.state.get("cwd", ""))

    @property
    def size(self) -> tuple[int, int]:
        """``(cols, rows)``. Zeroes where the host has published neither."""
        return _as_int(self.state.get("cols")), _as_int(self.state.get("rows"))

    @property
    def is_pty(self) -> bool:
        """Whether output needs VT parsing at all.

        ``False`` means plain text -- a caller can print it as-is. ``isPty`` is
        optional (``TerminalState.required`` is ``title``, ``content``,
        ``claim``), and **absent defaults to ``True``** because the two mistakes
        are not symmetric: running a VT parser over plain text is a no-op, while
        printing an unparsed pty stream shows the user literal ``ESC[0m``.
        Default to the recoverable one.
        """
        return self.state.get("isPty") is not False

    @property
    def supports_command_detection(self) -> bool:
        """Whether ``terminal/command*`` will ever arrive.

        "Clients MUST check this flag before relying on command detection. Do NOT
        use the presence of a `command` part as a feature flag" -- parts are
        absent in the ordinary idle state, so the obvious test reports *no*
        support for a shell that simply has not run anything yet.
        """
        return self.state.get("supportsCommandDetection") is True

    @property
    def exit_code(self) -> int | None:
        """The process's exit code, or ``None``.

        ``None`` is three situations at once -- still running, killed without a
        code, or a host that sent an explicit null -- so it is not a liveness
        test. :attr:`exit_reported` separates the first from the others.

        Read from ``lifecycle`` (0.9.0), falling back to the top-level
        ``exitCode`` a host on an earlier version writes.
        """
        lifecycle = self.state.get("lifecycle")
        if isinstance(lifecycle, Mapping) and lifecycle.get("status") == "exited":
            return _as_exit_code(lifecycle.get("exitCode"))
        return _as_exit_code(self.state.get("exitCode"))

    @property
    def exit_reported(self) -> bool:
        """Whether the state says the process exited.

        Since 0.9.0 that is exact: ``TerminalState.lifecycle`` is
        ``{status: "exited", exitCode?}``, so an exit without a code is still an
        exit. A host on an earlier version only writes a top-level ``exitCode``,
        which a codeless exit omits -- leaving the state byte-identical to a
        running terminal -- so against such a host this is "an exit that
        reported a code", and :meth:`wait_for_exit` watching the stream is the
        only way to catch the rest.
        """
        lifecycle = self.state.get("lifecycle")
        if isinstance(lifecycle, Mapping):
            return lifecycle.get("status") == "exited"
        return "exitCode" in self.state

    def output(self) -> str:
        """The raw VT stream, reconstructed from the typed content parts.

        Exactly the reconstruction the schema documents:
        ``content.map(p => p.type === 'command' ? p.output : p.value).join('')``
        -- including for a part type from a newer spec, which contributes its
        ``value`` if it has one. The one deliberate difference is that a part
        with neither field contributes ``""`` rather than the literal
        ``"undefined"`` the JavaScript one-liner would splice into the stream.
        """
        parts = self.state.get("content")
        if not isinstance(parts, list):
            return ""
        return "".join(
            str(part.get("output" if part.get("type") == "command" else "value", ""))
            for part in parts
            if isinstance(part, Mapping)
        )

    def commands(self) -> Sequence[TerminalCommand]:
        """The ``command``-typed content parts, oldest first.

        Empty unless :attr:`supports_command_detection`, and empty on a shell
        integration-capable terminal that has not run anything -- which is why
        this list is not the feature flag.

        Typed for the same reason :class:`TerminalCommandFinished` is: a widget
        rendering scrollback reads *this*, not the event stream, and the raw part
        hands back `part.get("durationMs", 0)` -- reintroducing at the durable
        half of the surface the `?? 0` defect the transient half documents a
        decision about.
        """
        parts = self.state.get("content")
        if not isinstance(parts, list):
            return []
        return [
            TerminalCommand(p) for p in parts if isinstance(p, dict) and p.get("type") == "command"
        ]

    # ── driving it ───────────────────────────────────────────────────────────
    #
    # Synchronous, all of them. `dispatch` allocates a `clientSeq` and enqueues;
    # if these were `async def`, two coroutines could interleave between the two
    # halves and put clientSeq 5 on the wire before 4 (invariant 6). Nothing here
    # awaits a reply, because `dispatchAction` is a notification and has none.

    def write(self, data: str, *, force: bool = False) -> None:
        """Send keystrokes to the process.

        Raises :class:`TerminalNotHeld` when this client does not hold the claim.
        That is a **local** refusal and it is deliberate: ``dispatchAction`` is a
        notification, so a host that refuses the keystrokes answers with a
        `rejectionReason` on an envelope stream the caller is not obliged to be
        reading -- and the symptom, "typing does nothing", is exactly the one the
        interop run had to reverse-engineer.

        The rule being enforced is the *sibling host's* default gate rather than
        an upstream MUST -- upstream states the SHOULD only for
        ``terminal/claimed`` -- so ``force=True`` sends anyway, for a host whose
        policy is more permissive. It is a one-word escape hatch, not a
        ceiling.

        There is no echo to render: ``terminal/input`` is side-effect-only and
        the reducer no-ops it, because the pty sends the same bytes back as
        ``terminal/data``.
        """
        # The channel first, the claim second. A disposed terminal has no claim
        # to read, so the claim gate would otherwise answer "held by nobody" for
        # a terminal that does not exist -- a diagnosis about ownership for a
        # problem about lifetime.
        self._require_channel("terminal/input")
        if not force and not self.held_by_us:
            raise TerminalNotHeld(self.uri, self.claim, self._client.client_id, holder=self.holder)
        self._dispatch({"type": "terminal/input", "data": data})

    def resize(self, cols: int | float, rows: int | float) -> None:
        """Ask for a new size.

        Not claim-gated, here or in the sibling host: a viewer reflowing a pty to
        its own window is the ordinary case, and the guide's detach flow has a
        client resizing a terminal it does not hold. The cost is that any peer
        can reflow the process under you, which is a property of the protocol
        rather than of this method.
        """
        self._dispatch(
            {
                "type": "terminal/resized",
                "cols": terminal_dimension("cols", cols),
                "rows": terminal_dimension("rows", rows),
            }
        )

    def rename(self, title: str) -> None:
        """Relabel the terminal. Ungated, and visible to every subscriber.

        A blank title is refused. ``TerminalState.title`` is required and an
        empty string is schema-valid, so it reaches the root catalogue and
        renders as a nameless tab -- which is why the sibling host guards it with
        ``name or default`` on *creation*. Renaming walked around that guard.
        """
        if not title:
            raise InvalidArgument(
                "a terminal title may not be empty; it is required state and renders "
                "as a blank tab in every subscriber's catalogue"
            )
        self._dispatch({"type": "terminal/titleChanged", "title": title})

    def clear(self) -> None:
        """Wipe the scrollback **for everyone**.

        ``terminal/cleared`` is client-dispatchable, ungated, and whole-buffer:
        there is no partial-clear action, so this blanks the screen of every
        other subscriber too, including one that was reading the build log you
        just deleted.
        """
        self._dispatch({"type": "terminal/cleared"})

    def hand_to(self, claim: TerminalClaim) -> None:
        """Transfer the terminal to *claim*.

        Deliberately **not** gated locally, unlike :meth:`write`, because the two
        upstream statements disagree and only one of them is about us:
        ``actions.ts`` says a server SHOULD reject a claim from a peer that does
        not hold it, while the guide's detach flow has a client narrowing a
        *session's* claim -- which it certainly never held. Refusing that here
        would break a documented interaction to enforce a rule the host is the
        one entitled to apply.

        So this can be refused, and a refusal arrives as
        :class:`TerminalRefused` with ``mine=True`` on :meth:`events`. Nothing
        about the terminal changes in the meantime: :attr:`claim` reads confirmed
        state precisely so that a handover we asked for and did not get never
        reads as one that happened.
        """
        self._dispatch({"type": "terminal/claimed", "claim": claim.to_wire()})

    def take(self) -> None:
        """Claim the terminal for this client. Sugar over :meth:`hand_to`."""
        self.hand_to(ClientClaim(self._client.client_id))

    def _require_channel(self, action_type: str) -> None:
        # A dispatch into a channel this client dropped is the quietest failure
        # on the surface: it is numbered, enqueued and sent, the host applies it
        # or not, and nothing comes back here because we are not subscribed. The
        # symptom is the module's own headline -- typing does nothing.
        if not self.alive:
            raise AhpClientError(
                f"terminal {self.uri} is not subscribed, so {action_type!r} would go nowhere "
                "this client can observe; it was disposed, or never opened"
            )

    def _dispatch(self, action: Mapping[str, Any]) -> None:
        self._require_channel(str(action.get("type")))
        self._client.protocol.dispatch(self.uri, dict(action))

    # ── watching it ──────────────────────────────────────────────────────────

    def events(self) -> TerminalStream:
        """Output, commands, claim changes and refusals, from now.

        **Close it.** ``async with term.events()`` or an ``async for`` that runs
        to completion; a stream abandoned mid-iteration keeps its cursor
        attached, and `BroadcastQueue` reclaims only up to the *lowest* cursor,
        so one dead reader pins the fan-in buffer for every other reader on the
        connection.
        """
        return TerminalStream(self._client, self.uri)

    async def wait_for_exit(self, timeout: float | None = 30.0) -> int | None:
        """Block until the process ends; return its exit code.

        ``None`` is a real answer -- a process killed without one -- so it does
        not mean "timed out". A timeout raises
        :class:`~ahp_client.client.errors.RequestTimeout`, which is an
        ``AhpClientError`` like everything else here rather than the builtin.

        **The default is a timeout, not ``None``.** A peer's :meth:`dispose`
        publishes no ``terminal/exited`` -- the channel simply stops -- so a
        caller that took the signature's easiest path waited forever for an event
        that could no longer arrive. ``timeout=None`` is still honoured for a
        caller who means it.

        Three things end the wait, and the second two are why this does not read
        the terminal's own stream: ``terminal/exited``, the terminal leaving
        ``RootState.terminals`` (somebody else disposed it), and this client
        unsubscribing. Each raises rather than reporting a ``None`` exit code
        that would read as "killed without a code".

        The reader is attached **before** the state is checked, so an exit
        landing between the two is caught rather than lost in the gap. Against a
        pre-0.9.0 host the state check only catches an exit that reported a
        *code*: see :attr:`exit_reported` for why a codeless one that already
        happened is unrecoverable there, and start watching earlier if that
        matters.
        """
        reader = self._client._runtime.events()
        # Only meaningful if we ever saw it listed: a host that publishes no
        # terminal catalogue would otherwise make every wait raise immediately.
        listed = self.exists
        try:
            async with asyncio.timeout(timeout):
                if self.exit_reported:
                    return self.exit_code
                async for tagged in reader:
                    if (
                        isinstance(tagged.event, ActionEvent)
                        and tagged.channel == self.uri
                        and isinstance(terminal_event_for(tagged.event.envelope), TerminalExited)
                    ):
                        return self.exit_code
                    self._check_still_there(listed)
        except TimeoutError as exc:
            raise RequestTimeout(f"exit of terminal {self.uri}", timeout or 0.0) from exc
        finally:
            await reader.aclose()
        raise AhpClientError(f"the connection ended while waiting for terminal {self.uri} to exit")

    def _check_still_there(self, listed: bool) -> None:
        if not self.alive:
            raise AhpClientError(
                f"terminal {self.uri} is no longer subscribed, so its exit cannot be observed"
            )
        if listed and not self.exists:
            raise AhpClientError(
                f"terminal {self.uri} was disposed by another peer; a disposal publishes no "
                "terminal/exited, so there is no exit code to wait for"
            )

    # ── ending it ────────────────────────────────────────────────────────────

    async def dispose(self) -> None:
        """Kill the process and drop the channel.

        **Not claim-gated**, and that asymmetry -- a peer refused
        :meth:`write` can still destroy the terminal -- is the answer rather than
        an oversight. ``DisposeTerminalParams`` carries a channel and nothing
        else, and gating it would make a handed-over terminal immortal: a session
        claim is held by no client, so the moment a terminal is handed to a
        session *nobody* could dispose it and the shell would run until the host
        stopped. It is a command, so it is gated where commands are gated: the
        host's own permission check on the channel.

        Idempotent at the sibling host, which answers a second disposal with
        ``{}`` rather than an error.

        The subscription is released in a ``finally``: a refused
        ``disposeTerminal`` otherwise leaves this client bound to a channel it
        has stopped tracking as a terminal it owns, and the runtime re-requests
        that subscription on every reconnect for the rest of the connection.
        """
        try:
            await self._client.protocol.dispose_terminal(self.uri)
        finally:
            await self._client._runtime.unsubscribe(self.uri)

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if not self._owned:
            # Not ours to destroy -- but the subscription *is* ours, and leaving
            # it behind is a channel the runtime re-requests on every reconnect
            # for a terminal nothing in this process is reading.
            with contextlib.suppress(Exception):
                await self._client._runtime.unsubscribe(self.uri)
            return
        if exc is not None:
            # Only here: a failing `with` body must not have its exception
            # replaced by one from the cleanup, and a host that has already gone
            # away is the usual reason both fail at once.
            with contextlib.suppress(Exception):
                await self.dispose()
            return
        # Otherwise the failure is raised. A silently refused disposal leaves the
        # shell running with nothing anywhere saying so, and `with` promised to
        # end it.
        await self.dispose()


# ── helpers ──────────────────────────────────────────────────────────────────


def new_terminal_uri() -> str:
    """Mint a terminal URI. ``CreateTerminalParams.channel`` is client-chosen.

    ``ahp-terminal:`` is the spec's own example form and the one the shared
    layer's display-only `classify()` recognises, so a diagnostic naming this
    channel names it correctly. VS Code mints three different
    ``agenthost-terminal:`` forms and this library accepts all of them --
    **nothing anywhere routes on the scheme** (invariant 1), so the form is a
    convention and never a contract.
    """
    return f"ahp-terminal:/{uuid.uuid4()}"


def _is_number(value: Any) -> TypeGuard[int | float]:
    """JSON ``number``. ``bool`` is an ``int`` in Python and is never one here."""
    return isinstance(value, int | float) and not isinstance(value, bool)


def _as_int(value: Any) -> int:
    """A JSON ``number`` as an int, or ``0``.

    ``cols``, ``rows``, ``exitCode``, ``durationMs`` and ``timestamp`` are all
    declared ``"type": "number"`` -- the *same* JSON type -- so a host that
    serialises ``30`` as ``30.0`` is conformant. Reading three of them with
    ``isinstance(raw, int)`` made a legal ``cols: 100.0`` read as ``0`` and a
    clean ``exit 7`` read as "killed without a code".
    """
    return int(value) if _is_number(value) else 0


def _as_exit_code(value: Any) -> int | None:
    """An exit code, or ``None`` when none was reported. See :func:`_as_int`."""
    return int(value) if _is_number(value) else None


def _as_float(value: Any) -> float:
    return float(value) if _is_number(value) else 0.0


def terminal_dimension(name: str, value: int | float) -> int:
    """A column or row count on its way *out*.

    The client is a producer of these too, and the sibling host takes
    ``cols if isinstance(cols, int) else None`` -- so ``960 / 12`` is a float,
    schema-legal, silently dropped, and the pty quietly runs at the backend
    default with nothing raised and nothing on the stream. Integral floats are
    the ordinary result of arithmetic and are accepted; a fractional column is
    not a thing a terminal has.
    """
    if not _is_number(value) or value != int(value):
        raise InvalidArgument(f"terminal {name} must be a whole number of cells, not {value!r}")
    if int(value) <= 0:
        raise InvalidArgument(f"terminal {name} must be positive, not {value!r}")
    return int(value)
