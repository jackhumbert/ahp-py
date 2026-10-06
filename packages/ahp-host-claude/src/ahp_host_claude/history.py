"""Where each AHP turn sits in Claude Code's transcript, so a rewind can cut it.

Edit-and-resend (`chat/truncated`) drops turns from the chat every client sees;
`TruncatesHistory` is how the agent forgets them too. Claude Code can resume a
conversation *up to* a message (`resume_session_at`: "only load the
conversation up to and including the message with this UUID"), so each turn
remembers two of its transcript entries:

* ``prompt`` - the user message that opened it. Our own messages carry a uuid
  we chose, which Claude Code records as theirs; a turn typed elsewhere
  (claude.ai) carries the uuid the CLI replayed it with.
* ``last`` - the last entry the stream showed for it: an assistant message or
  a tool result, both of which carry their transcript uuid.

Truncating after turn *T* resumes at *T*'s ``last``, and when exactly one turn
goes, names its ``prompt`` as the turn being dropped (`resume_drops_turn`), so
Claude Code refuses the cut if anything else would go with it - a queued
message it absorbed, a task notification. A refused cut is retried without that
check: a cut that drops a background notification with the turn is still the
cut the user asked for.

Not forked (`fork_session`): resuming at a message and carrying on appends a
branch to the same transcript, which is how Claude Code's own rewind works, so
the conversation keeps its id, and every uuid recorded here stays valid for the
next cut. Until the branch has a first message, a plain resume would load the
old end, so the pending cut is kept - in the resume state too - until then.

A turn this does not know (a session from before it existed, or a host that
does not say which turn a sink is for) cannot be cut precisely, so the whole
conversation is forgotten instead: an agent that knows less than the user was
shown is a nuisance, one that remembers what the user saw being taken back is
the failure `TruncatesHistory` exists to prevent.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

__all__ = ["MAX_MARKS", "Cut", "Rewind", "TurnMark", "branch_point", "plan"]

#: Turns remembered per session; a cut older than this forgets everything.
MAX_MARKS: Final = 1000


@dataclass
class TurnMark:
    """One AHP turn's place in the transcript."""

    turn: str | None
    prompt: str
    last: str

    def to_wire(self) -> dict[str, Any]:
        return {"turn": self.turn, "prompt": self.prompt, "last": self.last}

    @classmethod
    def from_wire(cls, value: Any) -> TurnMark | None:
        if not isinstance(value, Mapping):
            return None
        turn, prompt, last = value.get("turn"), value.get("prompt"), value.get("last")
        if not isinstance(prompt, str) or not isinstance(last, str):
            return None
        return cls(turn=turn if isinstance(turn, str) else None, prompt=prompt, last=last)


@dataclass(frozen=True)
class Rewind:
    """A cut not yet taken: resume at ``at``, dropping the turn ``drops`` opened.

    ``fork``: take it as a new conversation (`fork_session`) rather than a
    branch of the same one - a chat forked from another chat, whose source
    must stay exactly as it is.
    """

    at: str
    drops: str | None = None
    fork: bool = False

    def to_wire(self) -> dict[str, Any]:
        wire: dict[str, Any] = {"at": self.at}
        if self.drops is not None:
            wire["drops"] = self.drops
        if self.fork:
            wire["fork"] = True
        return wire

    @classmethod
    def from_wire(cls, value: Any) -> Rewind | None:
        if not isinstance(value, Mapping) or not isinstance(value.get("at"), str):
            return None
        drops = value.get("drops")
        return cls(
            at=value["at"],
            drops=drops if isinstance(drops, str) else None,
            fork=value.get("fork") is True,
        )


@dataclass(frozen=True)
class Cut:
    """What a truncation comes to.

    ``forget_all``: start a new conversation. Otherwise keep ``keep`` and, if
    ``rewind`` is set, resume at it; with neither, nothing was dropped.
    """

    keep: Sequence[TurnMark] = ()
    rewind: Rewind | None = None
    forget_all: bool = False

    @property
    def changes_nothing(self) -> bool:
        return not self.forget_all and self.rewind is None


def plan(marks: Sequence[TurnMark], turn_id: str | None, *, pending: bool = False) -> Cut:
    """The cut that forgets everything after *turn_id* (everything, if None).

    *pending*: a cut is already waiting to be taken, so the transcript still
    holds turns `marks` has forgotten, and naming one turn as the only one
    dropped would be refused.
    """
    if turn_id is None:
        return Cut(forget_all=True)
    index = next((i for i, mark in enumerate(marks) if mark.turn == turn_id), None)
    if index is None:
        return Cut(forget_all=True)
    keep = list(marks[: index + 1])
    dropped = marks[index + 1 :]
    if not dropped:
        return Cut(keep=keep)
    drops = dropped[0].prompt if len(dropped) == 1 and not pending else None
    return Cut(keep=keep, rewind=Rewind(at=keep[-1].last, drops=drops))


def branch_point(
    marks: Sequence[TurnMark], turn_id: str | None, *, running: TurnMark | None = None
) -> tuple[list[TurnMark], str] | None:
    """Where a chat forked at *turn_id* starts: the turns it keeps, and the entry to cut at.

    *turn_id* None is the whole chat - its completed turns, so not *running*,
    the turn still in flight. None if the turn is not one this adapter marked.
    """
    if turn_id is None:
        done = [mark for mark in marks if mark is not running]
        return (list(done), done[-1].last) if done else None
    index = next((i for i, mark in enumerate(marks) if mark.turn == turn_id), None)
    if index is None:
        return None
    return list(marks[: index + 1]), marks[index].last
