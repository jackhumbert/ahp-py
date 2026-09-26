"""Terminals: claims, shell-integration stripping, and a backend that says no.

This module holds everything a terminal channel needs **except the ability to run
a process**, and that omission is the design rather than an unfinished edge.

**No PTY backend ships here.** :class:`~ahp_host.core.policy.Policy`
cannot authenticate a peer -- `reconnect` resumes on a client-asserted
`clientId` carrying no credential -- and the README says single-trust-domain. In
a library shaped like that, importable arbitrary command execution in the default
wheel is the wrong default *even behind a constructor argument*: the next
person's mistake is :class:`TerminalBackend` being one import away, not
`allow_remote=True` (`docs/roadmap.md` §6). So the backend is a Protocol with no
implementation in this distribution, :class:`RefusingTerminalBackend` is what a
host gets until it supplies one, and a POSIX pty belongs in a separate
distribution or a `[pty]` extra. Nothing here imports `pty`, `os.forkpty`,
`subprocess` or `ptyprocess`, and nothing should.

**Claims are the only ownership the protocol has, and three of the five
client-dispatchable terminal actions ignore them.** `terminal/cleared` is
client-dispatchable, so *any* peer that can name a terminal channel can wipe
another peer's scrollback -- and `terminal/resized` reflows a pty someone else is
looking at, and `terminal/titleChanged` relabels it. None of that is a bug in the
spec; it is a spec that assumes one trust domain. :data:`CONTESTED_ACTIONS` names
them so a host can pass :data:`STRICT_CLAIM_GATED_ACTIONS` -- the
``claim_gated_actions`` constructor argument on ``Host`` -- and close the hole
deliberately, instead of the hole being invisible.

**`disposeTerminal` is deliberately NOT claim-gated**, and that asymmetry --
a peer refused `terminal/input` can still destroy the terminal and kill its
shell -- is the answer, not an oversight. Three reasons, in order of weight.
The spec attaches no ownership rule to the command at all: `DisposeTerminalParams`
carries a channel and nothing else, and the only SHOULD about holding a claim is
on the `terminal/claimed` *action*. Gating it would make a handed-over terminal
immortal: a session claim is held by no client, so the moment a client hands its
terminal to a session -- the guide's detach flow, and every tool-call terminal --
*nobody* would be able to dispose it, and the shell would run until the host
stopped. And disposal is a **command**, so it is gated where every other command
is: :meth:`~ahp_host.core.policy.Policy.may_see_channel`, which a host
serving more than one trust domain narrows. :data:`CLAIM_GATED_ACTIONS` is about
actions, and adding a command to it would be a category error.

Upstream disagrees with itself about `terminal/claimed`:
`types/channels-terminal/actions.ts:74` says the server SHOULD reject a claim
from a peer that does not hold it, while the guide's "client detaches a terminal"
flow (`docs/guide/terminals.md:255`) has a client narrowing a *session's* claim,
which it certainly does not hold. :func:`terminal_dispatch_rejection` reconciles
them: the holder may hand a terminal anywhere, and anyone may re-scope a claim
**within the same owner**, but nobody may take a terminal from a different owner.

**Shell integration sequences MUST be stripped** before `terminal/data`
(`terminal-channel.md:112`), because a client's "did this command produce real
output" check counts them as output. :class:`ShellIntegrationParser` does that,
and four properties are what make it safe rather than merely present:

* It keeps state between :meth:`~ShellIntegrationParser.feed` calls, because a
  read from a pty ends wherever the kernel says it does -- routinely mid-escape.
* It strips at the **byte** level and decodes UTF-8 **incrementally**, so a
  multi-byte codepoint split across two reads is buffered rather than turned into
  two replacement characters. For the same reason the C1 single-byte string
  terminator `0x9C` is *not* honoured: in a UTF-8 stream that byte is a
  continuation byte, and treating it as a terminator corrupts text.
* An unterminated sequence is abandoned at :data:`DEFAULT_MAX_OSC_BODY` and its
  bytes are emitted verbatim. A parser that waits forever for a terminator that
  never arrives eats the rest of the terminal, which is a worse failure than
  leaking one escape sequence.
* It returns text and events **interleaved in stream order**
  (:attr:`ParsedOutput.items`), because a single read can carry
  `output ESC]633;D ESC]633;C output`, and a host that flattened that to
  "text plus events" would append the second half of the output to the wrong
  content part.

OSC 633 (VS Code) and OSC 133 (FinalTerm/iTerm2, the widely-implemented
alternative) are both recognised and both stripped whole: every sequence in those
two namespaces is an out-of-band marker with no visual content.

**OSC 1337 is split by key, not stripped and not ignored.** iTerm2's 1337 is not
a shell-integration protocol; it is a vendor channel that also carries
`File=<base64>` inline images, `SetUserVar`, badges and annotations -- payloads
that *are* the output. Stripping the namespace would delete content the MUST
never referred to; passing it whole would leave `CurrentDir=` and
`ShellIntegrationVersion=` in the stream, which are shell integration by any
reading. So the three shell-integration keys are stripped (and `CurrentDir`
surfaces as :class:`CwdReported`) and everything else passes through byte-exact.
Because abandonment is also byte-exact, an inline image far larger than the
buffer cap still reaches the client intact.

**Scrollback trims silently.** :func:`trim_scrollback` drops from the head and
publishes nothing. The guide permits either (`terminals.md:81`: "Both sides MAY
independently trim their `content` to bound memory") and the reference host
publishes a server-side `terminal/cleared` instead; we deliberately do not, for
two reasons. The protocol has no partial-trim action, so `terminal/cleared` is a
*whole-buffer* wipe -- dropping the oldest 10% on the server would blank the
screen of every subscriber, including ones with memory to spare. And a
synthesised `cleared` is indistinguishable at the client from the user's own
clear, so a build log vanishing mid-build looks like someone pressed a key. The
cost is that a long-connected client keeps more history than the server: bounded,
visible only when two clients compare notes, and exactly what "independently" in
that sentence licenses. No fixture covers either behaviour; this comment and the
tests are the whole record.
"""

from __future__ import annotations

import codecs
import re
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final, Protocol, runtime_checkable

from ahp_protocol.errors import AhpError
from ahp_protocol.reducers import js
from ahp_protocol.types import AHP_ERROR_CODES, IS_CLIENT_DISPATCHABLE

__all__ = [
    "CLAIM_GATED_ACTIONS",
    "CONTESTED_ACTIONS",
    "DEFAULT_MAX_OSC_BODY",
    "DEFAULT_SCROLLBACK_CHARS",
    "REFUSAL_REASON",
    "STRICT_CLAIM_GATED_ACTIONS",
    "CommandFinished",
    "CommandLine",
    "CommandStart",
    "CwdReported",
    "OutputItem",
    "ParsedOutput",
    "RefusingTerminalBackend",
    "ShellIntegrationEvent",
    "ShellIntegrationParser",
    "TerminalBackend",
    "TerminalClaim",
    "TerminalClientClaim",
    "TerminalProcess",
    "TerminalRequest",
    "TerminalSessionClaim",
    "claim_from_wire",
    "holds_claim",
    "same_owner",
    "terminal_dispatch_rejection",
    "terminal_refused",
    "trim_scrollback",
]


# ─── Claims ──────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class TerminalClientClaim:
    """``{ kind: 'client', clientId }`` -- a peer driving the terminal itself."""

    client_id: str

    def to_wire(self) -> dict[str, Any]:
        return {"kind": "client", "clientId": self.client_id}


@dataclass(frozen=True)
class TerminalSessionClaim:
    """``{ kind: 'session', session, chat, turnId?, toolCallId? }``.

    `chat` -- the chat that owns the terminal -- is required since 0.9.0. The
    optional pair is the whole difference between "a tool call is using this
    right now" and "backgrounded, still owned". Narrowing to bare ``session``
    and ``chat`` is how a tool call detaches.
    """

    session: str
    chat: str
    turn_id: str | None = None
    tool_call_id: str | None = None

    def to_wire(self) -> dict[str, Any]:
        claim: dict[str, Any] = {"kind": "session", "session": self.session, "chat": self.chat}
        # Absent, not `null`: `TerminalSessionClaim` declares these optional and
        # an explicit null is a different document to a missing key.
        if self.turn_id is not None:
            claim["turnId"] = self.turn_id
        if self.tool_call_id is not None:
            claim["toolCallId"] = self.tool_call_id
        return claim


TerminalClaim = TerminalClientClaim | TerminalSessionClaim


def claim_from_wire(value: Any) -> TerminalClaim | None:
    """Parse a claim payload, or ``None`` if it is not one.

    `terminal/claimed` is client-dispatchable, so *value* is arbitrary peer JSON.
    Every field is type-checked rather than coerced: a `clientId` of ``123`` must
    not become the claim of a client named ``"123"``, and only a `kind` that is
    the exact string matches -- which is also what makes the ``==`` comparisons
    in :func:`holds_claim` and :func:`same_owner` safe to write plainly.
    """
    kind = js.get(value, "kind")
    if js.strict_equal(kind, "client"):
        client_id = js.get(value, "clientId")
        # Non-empty, and the client's own parser agrees. An empty id is
        # schema-legal and strands the terminal permanently: `terminal/claimed`
        # is itself claim-gated, so once the holder is a `clientId` no
        # connection has, no peer can take it back and disposal is the only
        # operation left. The host used to accept it as a real claim while
        # every client rendered the same payload as *unclaimed*.
        if not isinstance(client_id, str) or not client_id:
            return None
        return TerminalClientClaim(client_id)
    if js.strict_equal(kind, "session"):
        session = js.get(value, "session")
        chat = js.get(value, "chat")
        # `chat` is required since 0.9.0; a claim without one names no owner a
        # current client can resolve, so it is not a claim.
        if not isinstance(session, str) or not isinstance(chat, str):
            return None
        turn_id = js.get(value, "turnId")
        tool_call_id = js.get(value, "toolCallId")
        return TerminalSessionClaim(
            session,
            chat,
            turn_id if isinstance(turn_id, str) else None,
            tool_call_id if isinstance(tool_call_id, str) else None,
        )
    return None


def holds_claim(claim: TerminalClaim | None, client_id: str) -> bool:
    """Whether *client_id* is the peer this terminal belongs to.

    A session claim is held by no client, so a session-owned terminal takes no
    input from anyone -- which is the point: it is the agent's.
    """
    return isinstance(claim, TerminalClientClaim) and claim.client_id == client_id


def same_owner(left: TerminalClaim, right: TerminalClaim) -> bool:
    """Whether two claims name the same owner, ignoring turn and tool scope."""
    if isinstance(left, TerminalClientClaim) and isinstance(right, TerminalClientClaim):
        return left.client_id == right.client_id
    if isinstance(left, TerminalSessionClaim) and isinstance(right, TerminalSessionClaim):
        return left.session == right.session
    return False


#: Terminal actions refused from a peer that does not hold the claim. Both are
#: named by a document: `terminal/input` by the roadmap's security gates (it is
#: keystrokes into someone else's process), `terminal/claimed` by
#: `actions.ts:74`'s SHOULD.
CLAIM_GATED_ACTIONS: Final[frozenset[str]] = frozenset({"terminal/input", "terminal/claimed"})

#: Client-dispatchable, **not** gated by any upstream text, and each visible to
#: every other subscriber of the channel. `terminal/cleared` is the sharp one --
#: it destroys scrollback the peer that produced it may still need. A host that
#: serves more than one trust domain passes
#: :data:`STRICT_CLAIM_GATED_ACTIONS` (via ``Host(claim_gated_actions=...)``)
#: and accepts that a viewer can no longer resize the pty to its own window.
CONTESTED_ACTIONS: Final[frozenset[str]] = frozenset(
    {"terminal/cleared", "terminal/resized", "terminal/titleChanged"}
)

#: Every terminal action this module would gate if it were only asked about
#: safety. Not the default, because it also refuses the guide's "client detaches
#: a terminal" resize and rename.
STRICT_CLAIM_GATED_ACTIONS: Final[frozenset[str]] = CLAIM_GATED_ACTIONS | CONTESTED_ACTIONS


def terminal_dispatch_rejection(
    action: Mapping[str, Any],
    *,
    claim: TerminalClaim | None,
    client_id: str,
    gated: Collection[str] = CLAIM_GATED_ACTIONS,
) -> str | None:
    """A `rejectionReason`, or ``None`` to accept.

    Shaped to drop into the host's existing validation chain, which returns the
    same thing. A rejected client action is echoed with the reason, never
    dropped -- silence leaves the client's optimistic prediction applied forever.

    *claim* is the terminal's **current** owner; ``None`` means the state carries
    a claim this module cannot parse, which denies every gated action rather than
    defaulting open.
    """
    action_type = action.get("type")
    if not isinstance(action_type, str):
        return "action has no type"
    # Restated from the generated table rather than from a hand-written list, so
    # a spec bump that makes a terminal action client-dispatchable cannot leave
    # this file disagreeing with the wire.
    if not IS_CLIENT_DISPATCHABLE.get(action_type, False):
        return f"{action_type} is not client-dispatchable"
    if action_type == "terminal/claimed":
        return _claim_transfer_rejection(action, claim, client_id, gated)
    if action_type in gated and not holds_claim(claim, client_id):
        return f"{action_type} requires holding the terminal's claim"
    return None


def _claim_transfer_rejection(
    action: Mapping[str, Any],
    claim: TerminalClaim | None,
    client_id: str,
    gated: Collection[str],
) -> str | None:
    requested = claim_from_wire(action.get("claim"))
    if requested is None:
        # Checked even when transfers are ungated. The reducer applies the
        # payload verbatim, so an unparseable claim reaching the state means
        # `claim` is `None` from then on and every gated action is denied to
        # everybody -- a terminal bricked by one malformed frame.
        return "terminal/claimed carries no usable claim"
    if "terminal/claimed" not in gated:
        return None
    if holds_claim(claim, client_id):
        return None
    if claim is not None and same_owner(claim, requested):
        # The guide's detach flow: a client narrows a session's claim from
        # {session, turnId, toolCallId} to {session}. It never held that claim,
        # and refusing it would break a documented interaction.
        return None
    return "terminal/claimed from a peer that does not hold the terminal"


# ─── Scrollback ──────────────────────────────────────────────────────────────

#: Characters of terminal content retained by default. A `git clone` of a large
#: repository is a few hundred KiB of progress bars; this keeps one of those and
#: bounds a runaway process at roughly a quarter of a megabyte per terminal.
DEFAULT_SCROLLBACK_CHARS: Final = 256 * 1024


def trim_scrollback(
    content: Sequence[Any], *, max_chars: int = DEFAULT_SCROLLBACK_CHARS
) -> list[Any]:
    """Bound a terminal's content, oldest first. Publishes nothing.

    Returns a new list; parts are never mutated in place. Two parts are never
    dropped, for reasons that are not about memory:

    * the **last** part, because `terminal/data` appends to the tail and losing
      it would restart the buffer with an unclassified run mid-command;
    * an **incomplete command** part, because a later `terminal/commandFinished`
      matches it by `commandId` and would otherwise complete nothing at all.

    Those are truncated from the head instead, which keeps the id and the
    ordering while still releasing the memory.
    """
    parts = list(content)
    total = sum(_part_length(part) for part in parts)
    if total <= max_chars:
        return parts

    while len(parts) > 1 and total > max_chars and _droppable(parts[0]):
        total -= _part_length(parts[0])
        parts.pop(0)

    index = 0
    while total > max_chars and index < len(parts):
        length = _part_length(parts[index])
        keep = max(0, length - (total - max_chars))
        if keep < length:
            parts[index] = _truncate_head(parts[index], keep)
            total -= length - keep
        index += 1
    return parts


def _text_key(part: Any) -> str | None:
    """Which field holds this part's text, or ``None`` for a part we cannot read."""
    part_type = js.get(part, "type")
    if part_type == "command":
        return "output"
    if part_type == "unclassified":
        return "value"
    return None


def _part_length(part: Any) -> int:
    key = _text_key(part)
    if key is None:
        return 0
    value = js.get(part, key)
    return len(value) if isinstance(value, str) else 0


def _droppable(part: Any) -> bool:
    if _text_key(part) is None:
        # A part type from a newer spec. It costs nothing to keep and deleting
        # content we cannot even name is not a trade worth making.
        return False
    # `not isComplete` is JavaScript truthiness, matching the reducer's tail test
    # exactly: a command part carrying no `isComplete` at all is still running.
    # Do not "fix" this to `is False` -- see reducers/terminal.py.
    return not (js.get(part, "type") == "command" and not js.get(part, "isComplete"))


def _truncate_head(part: Any, keep: int) -> dict[str, Any]:
    key = _text_key(part)
    value = js.get(part, key) if key is not None else None
    text = value if isinstance(value, str) else ""
    # `text[-0:]` is the WHOLE string, not the empty one. Trimming a part to
    # nothing via a negative slice silently keeps everything.
    kept = text[-keep:] if keep > 0 else ""
    return {**part, str(key): kept}


# ─── Shell integration ───────────────────────────────────────────────────────

_ESC: Final = 0x1B
_BEL: Final = 0x07
_OSC_INTRODUCER: Final = 0x5D  # ']'
_ST_FINAL: Final = 0x5C  # '\'
_BACKSLASH: Final = 0x5C
_HEX_DIGITS: Final = b"0123456789abcdefABCDEF"

_VSCODE: Final = b"633"
_FINALTERM: Final = b"133"
_ITERM: Final = b"1337"

#: iTerm2 keys that are shell integration rather than terminal decoration.
_ITERM_SHELL_KEYS: Final = (b"CurrentDir", b"RemoteHost", b"ShellIntegrationVersion")

#: Bytes an unterminated escape sequence may buffer before it is abandoned and
#: flushed as text. Generous for every real shell-integration sequence -- the
#: longest is a command line -- and small enough that a peer cannot make a
#: terminal hold memory by opening an OSC and never closing it.
DEFAULT_MAX_OSC_BODY: Final = 8192

#: Longest exit-code field accepted. `int()` takes underscore separators
#: (`int(b"1_0")` is 10), non-ASCII digits, and raises above 4300 digits -- so
#: the field is length-checked and pattern-checked before conversion.
_MAX_EXIT_CODE_LENGTH: Final = 11
_EXIT_CODE = re.compile(rb"-?[0-9]+")


@dataclass(frozen=True)
class CommandStart:
    """Execution began: everything after this is the command's output.

    OSC 633 sends the command line (`E`) *before* this, and OSC 133 never sends
    one at all -- recovering it from the echoed keystrokes between `B` and `C`
    is terminal-emulator work this library declines. A host therefore holds the
    most recent :class:`CommandLine` and mints `terminal/commandExecuted` here.
    """


@dataclass(frozen=True)
class CommandLine:
    """The command line the shell was given, already unescaped."""

    command_line: str


@dataclass(frozen=True)
class CommandFinished:
    """Execution ended. ``exit_code`` is ``None`` when the shell reported none."""

    exit_code: int | None = None


@dataclass(frozen=True)
class CwdReported:
    """The shell announced its working directory -- as a path, not a URI."""

    cwd: str


ShellIntegrationEvent = CommandStart | CommandLine | CommandFinished | CwdReported
OutputItem = str | ShellIntegrationEvent


@dataclass(frozen=True)
class ParsedOutput:
    """Cleaned text and recognised events, interleaved in stream order."""

    items: tuple[OutputItem, ...] = ()

    @property
    def text(self) -> str:
        """Everything a `terminal/data` may carry, escape sequences removed."""
        return "".join(item for item in self.items if isinstance(item, str))

    @property
    def events(self) -> tuple[ShellIntegrationEvent, ...]:
        return tuple(item for item in self.items if not isinstance(item, str))


class ShellIntegrationParser:
    """Strips shell-integration sequences from a pty byte stream.

    One instance per terminal, fed every read in order. It is deliberately not a
    terminal emulator: CSI, charset selection, title-setting OSCs and iTerm2
    decoration all pass through untouched, because a client renders them and
    the MUST-strip rule is about markers, not about display state.
    """

    def __init__(self, *, max_osc_body: int = DEFAULT_MAX_OSC_BODY) -> None:
        self.max_osc_body = max_osc_body
        self._buffer = bytearray()
        self._decoder = codecs.getincrementaldecoder("utf-8")("replace")

    def feed(self, chunk: bytes) -> ParsedOutput:
        """Consume one read. Anything incomplete is held for the next one."""
        self._buffer.extend(chunk)
        items: list[OutputItem] = []
        del self._buffer[: self._scan(items, final=False)]
        return ParsedOutput(tuple(items))

    def flush(self) -> ParsedOutput:
        """Release whatever is held, for a pty that exited mid-sequence.

        Without this the last bytes of a process's output are lost whenever it
        happens to end inside an escape or a codepoint.
        """
        items: list[OutputItem] = []
        del self._buffer[: self._scan(items, final=True)]
        self._emit_text(items, self._decoder.decode(b"", True))
        self._decoder.reset()
        return ParsedOutput(tuple(items))

    def reset(self) -> None:
        """Forget all partial state -- for a restarted process, not a cleared one."""
        self._buffer.clear()
        self._decoder.reset()

    def _scan(self, items: list[OutputItem], *, final: bool) -> int:
        """Consume as much of the buffer as is unambiguous; return how much."""
        buffer = self._buffer
        position = 0
        while position < len(buffer):
            start = buffer.find(_ESC, position)
            if start < 0:
                self._decode(items, buffer[position:])
                return len(buffer)
            if start > position:
                self._decode(items, buffer[position:start])
                position = start

            if len(buffer) - position < 2:
                # A read that ended on a bare ESC. Holding one byte back is the
                # entire reason this parser has state.
                if not final:
                    return position
                self._decode(items, buffer[position:])
                return len(buffer)

            if buffer[position + 1] != _OSC_INTRODUCER:
                self._decode(items, buffer[position : position + 2])
                position += 2
                continue

            end, terminator = _find_terminator(buffer, position + 2)
            if end < 0:
                if not final and len(buffer) - position <= self.max_osc_body:
                    return position
                # No terminator and no more patience. Emitting the bytes is the
                # only outcome that does not swallow the rest of the terminal,
                # and it is byte-exact, so a pass-through sequence larger than
                # the cap still reaches the client whole.
                self._decode(items, buffer[position:])
                return len(buffer)
            if terminator == 0:
                # ESC inside the body, not followed by `\`: the sequence is
                # malformed. Emit what we have and re-read from that ESC, rather
                # than trusting a marker that never closed.
                self._decode(items, buffer[position:end])
                position = end
                continue

            strip, event = _shell_integration(bytes(buffer[position + 2 : end]))
            if not strip:
                self._decode(items, buffer[position : end + terminator])
            elif event is not None:
                items.append(event)
            position = end + terminator
        return len(buffer)

    def _decode(self, items: list[OutputItem], raw: bytes | bytearray) -> None:
        # Incremental: a multi-byte codepoint split across two reads is held
        # here rather than decoded into replacement characters. `replace` and
        # not `strict` because a pty carries whatever the process wrote, and a
        # `cat` of a binary must not raise out of a read loop.
        self._emit_text(items, self._decoder.decode(bytes(raw)))

    @staticmethod
    def _emit_text(items: list[OutputItem], text: str) -> None:
        if not text:
            return
        last = items[-1] if items else None
        if isinstance(last, str):
            items[-1] = last + text
        else:
            items.append(text)


def _find_terminator(buffer: bytearray, start: int) -> tuple[int, int]:
    """Locate the string terminator: ``(index, length)``.

    ``(-1, 0)`` means "not yet decidable"; ``(index, 0)`` means the body is
    malformed and ends at *index*. The C1 terminator ``0x9C`` is deliberately not
    recognised: in UTF-8 that byte is a continuation byte, and honouring it would
    cut a codepoint in half on ordinary text.
    """
    index = start
    while index < len(buffer):
        byte = buffer[index]
        if byte == _BEL:
            return index, 1
        if byte == _ESC:
            if index + 1 >= len(buffer):
                return -1, 0
            return (index, 2) if buffer[index + 1] == _ST_FINAL else (index, 0)
        index += 1
    return -1, 0


def _shell_integration(body: bytes) -> tuple[bool, ShellIntegrationEvent | None]:
    """``(strip, event)`` for one OSC body, identifier included."""
    identifier, _, rest = body.partition(b";")
    if identifier == _VSCODE:
        return True, _vscode_event(rest)
    if identifier == _FINALTERM:
        return True, _finalterm_event(rest)
    if identifier == _ITERM:
        return _iterm(rest)
    # Exact identifiers only. `OSC 6330;C` is somebody else's sequence and a
    # prefix match would eat it.
    return False, None


def _vscode_event(rest: bytes) -> ShellIntegrationEvent | None:
    fields = rest.split(b";")
    command = fields[0]
    if command == b"C":
        return CommandStart()
    if command == b"D":
        return CommandFinished(_exit_code(fields[1]) if len(fields) > 1 else None)
    if command == b"E":
        # Splitting on `;` is safe *because* VS Code escapes a literal semicolon
        # in the command line as `\x3b`. Field 2 is the injection nonce: dropped
        # rather than surfaced, since it authenticates the sequence and belongs
        # in no state a client can read.
        return CommandLine(_unescape(fields[1])) if len(fields) > 1 else CommandLine("")
    if command == b"P":
        return _vscode_property(fields[1:])
    # `A`, `B`, `SetMark`, and whatever the next VS Code release adds: stripped,
    # because they are markers, but reported as nothing.
    return None


def _vscode_property(fields: Sequence[bytes]) -> ShellIntegrationEvent | None:
    for field in fields:
        key, separator, value = field.partition(b"=")
        if separator and key == b"Cwd":
            return CwdReported(_unescape(value))
    return None


def _finalterm_event(rest: bytes) -> ShellIntegrationEvent | None:
    fields = rest.split(b";")
    command = fields[0]
    if command == b"C":
        return CommandStart()
    if command == b"D":
        return CommandFinished(_exit_code(fields[1]) if len(fields) > 1 else None)
    # `A` (prompt start), `B` (prompt end), `L` (fresh line), `P` (properties).
    # FinalTerm carries no command line at all.
    return None


def _iterm(rest: bytes) -> tuple[bool, ShellIntegrationEvent | None]:
    key, separator, value = rest.partition(b"=")
    if key not in _ITERM_SHELL_KEYS:
        return False, None
    if separator and key == b"CurrentDir":
        # NOT unescaped: only VS Code escapes its values, and running a Windows
        # path such as `C:\x41` through that decoder would rewrite it.
        return True, CwdReported(value.decode("utf-8", "replace"))
    return True, None


def _exit_code(field: bytes) -> int | None:
    if len(field) > _MAX_EXIT_CODE_LENGTH or not _EXIT_CODE.fullmatch(field):
        return None
    return int(field)


def _unescape(raw: bytes) -> str:
    r"""Undo VS Code's value escaping: ``\\`` and ``\xHH``.

    Decoded to bytes first and to text last, because the escape is applied to
    the UTF-8 encoding -- a non-ASCII character arrives as several ``\xHH``
    pairs, and turning each into a codepoint would produce mojibake.
    """
    if _BACKSLASH not in raw:
        return raw.decode("utf-8", "replace")
    out = bytearray()
    index = 0
    while index < len(raw):
        byte = raw[index]
        following = raw[index + 1] if index + 1 < len(raw) else None
        if byte != _BACKSLASH or following is None:
            out.append(byte)
            index += 1
        elif following == _BACKSLASH:
            out.append(_BACKSLASH)
            index += 2
        elif following in (0x78, 0x58) and _is_hex(raw[index + 2 : index + 4]):
            out.append(int(raw[index + 2 : index + 4], 16))
            index += 4
        else:
            out.append(byte)
            index += 1
    return bytes(out).decode("utf-8", "replace")


def _is_hex(pair: bytes) -> bool:
    # Two digits exactly. `int(b" f", 16)` is 15 and `int(b"1_", 16)` raises;
    # neither is a thing this should have to think about at the call site.
    return len(pair) == 2 and all(byte in _HEX_DIGITS for byte in pair)


# ─── Backends ────────────────────────────────────────────────────────────────

#: What :class:`RefusingTerminalBackend` says.
#:
#: **This string ends up in a user-facing dialog.** VS Code renders it verbatim
#: in "The terminal process failed to launch: …", so it has to read like a
#: message to a person: one line, no rationale, no implementation detail. The
#: reasoning belongs in this module's docstring and the README, where somebody
#: who wants it can find it -- not in a toast somebody did not ask for.
REFUSAL_REASON: Final = "no terminal backend is configured for this host"


def terminal_refused(reason: str = REFUSAL_REASON) -> AhpError:
    """`PermissionDenied` (-32009): understood, and declined.

    Not `MethodNotFound`. Once a host registers `createTerminal` the method
    exists, and answering -32601 for a request it parsed and rejected would tell
    a client to stop asking for terminals entirely rather than that this one was
    refused.
    """
    return AhpError(AHP_ERROR_CODES["PermissionDenied"], f"Terminal refused: {reason}")


#: Where a backend delivers pty output. Return value is awaited when it is a
#: coroutine, matching the resource watcher's emit callback.
OutputSink = Callable[[bytes], Any]


@dataclass(frozen=True)
class TerminalRequest:
    """A `createTerminal`, plus what the host decided the backend may have.

    `cwd`, `env` and `command` are host decisions, not client ones:
    `CreateTerminalParams` carries no command and no environment, and a backend
    that filled either from its own process would inherit every credential in
    the host's environment into a shell a peer asked for. ``env=None`` means
    "the backend supplies nothing", never "inherit `os.environ`".
    """

    channel: str
    claim: TerminalClaim
    name: str | None = None
    cwd: str | None = None
    cols: int | None = None
    rows: int | None = None
    command: Sequence[str] | None = None
    env: Mapping[str, str] | None = None


class TerminalProcess(Protocol):
    """A live terminal, handed back by a backend.

    ``is_pty`` becomes `TerminalState.isPty`, which tells a client whether it
    needs to parse VT sequences at all.
    """

    is_pty: bool

    async def write(self, data: bytes) -> None: ...

    async def resize(self, cols: int, rows: int) -> None: ...

    async def kill(self) -> None: ...

    async def wait(self) -> int | None: ...


@runtime_checkable
class TerminalBackend(Protocol):
    """Something that can actually run a process. **Nothing here implements it.**

    See the module docstring: a POSIX pty belongs in its own distribution or a
    `[pty]` extra, so that adding execution to a host is a dependency a reviewer
    can see rather than an import.

    An implementation MUST raise :func:`terminal_refused` (or another
    :class:`~ahp_protocol.errors.AhpError`) rather than return a dead
    process, and MUST NOT strip escape sequences itself -- output goes to the
    sink raw and through :class:`ShellIntegrationParser`, which is the only place
    that can survive a sequence split across two reads.
    """

    async def create(self, request: TerminalRequest, output: OutputSink) -> TerminalProcess: ...


class RefusingTerminalBackend:
    """The default backend: declines every terminal, with a reason.

    This exists so that "no backend" is a *stated* answer rather than an
    `AttributeError` or a silent success that produces a terminal nothing ever
    writes to.
    """

    def __init__(self, reason: str = REFUSAL_REASON) -> None:
        self.reason = reason

    async def create(self, request: TerminalRequest, output: OutputSink) -> TerminalProcess:
        raise terminal_refused(self.reason)
