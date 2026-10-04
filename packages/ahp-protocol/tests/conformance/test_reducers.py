"""The upstream 308-fixture reducer corpus.

This is the project's central conformance claim: our reducers produce the same
state as the reference implementation for the same action sequence. It is the
same artifact the Rust, Go, Kotlin and Swift clients are gated on, consumed
unmodified.

Two rules the harness must honour, both learned the hard way from the other
ports:

* the clock is pinned to 9999 (the reducers are not pure);
* fixture ``null`` means "absent", so both sides are normalised with
  ``drop_none`` before comparing -- the OPPOSITE of the wire corpus.

Fixtures for channels outside the v0.1 scope are reported explicitly rather than
silently filtered: a skipped fixture and a passing one look identical in a
summary, and this file is the thing other implementers will read to decide
whether to trust us.
"""

from __future__ import annotations

import pytest

from ahp_protocol.conformance.corpus import ReducerFixture, reducer_fixtures
from ahp_protocol.reducers import REDUCERS
from ahp_protocol.reducers.clock import frozen_clock
from ahp_protocol.types import reduced_equal

#: Every channel in the corpus. v0.1 shipped root + session + chat; the
#: remaining four were ported in v0.2 (docs/roadmap.md §2), which takes the
#: conformance claim from 200/247 to the whole corpus.
#:
#: The reducers landed *before* their channels are registered, on purpose: ADR
#: 0004 makes the port all-or-nothing per channel, so a channel registered
#: without its reducer would broadcast client-dispatchable actions that nothing
#: applies -- the exact failure that froze session state in `docs/experiments.md`
#: §E12.
IN_SCOPE = frozenset(
    {
        "root",
        "session",
        "chat",
        "terminal",
        "changeset",
        "annotations",
        "resourceWatch",
        "automation",
        "automationRun",
        "canvas",
    }
)

OUT_OF_SCOPE: frozenset[str] = frozenset()

ALL_FIXTURES = list(reducer_fixtures())
SCOPED = [f for f in ALL_FIXTURES if f.reducer in IN_SCOPE]
UNSCOPED = [f for f in ALL_FIXTURES if f.reducer not in IN_SCOPE]


def _ids(fixtures: list[ReducerFixture]) -> list[str]:
    return [f.id for f in fixtures]


def test_corpus_is_intact() -> None:
    """Pin the corpus shape so a pin bump that changes it is noticed."""
    assert len(ALL_FIXTURES) == 308
    counts: dict[str, int] = {}
    for fixture in ALL_FIXTURES:
        counts[fixture.reducer] = counts.get(fixture.reducer, 0) + 1
    assert counts == {
        "chat": 149,
        "session": 92,
        "terminal": 19,
        "changeset": 17,
        "annotations": 10,
        "root": 7,
        "resourceWatch": 2,
        "automation": 7,
        "automationRun": 2,
        "canvas": 3,
    }


def test_scope_split_is_explicit() -> None:
    """State plainly how much of the corpus we run, so it cannot drift silently."""
    assert len(SCOPED) == 308, "the whole corpus is in scope"
    assert len(UNSCOPED) == 0
    assert {f.reducer for f in UNSCOPED} == OUT_OF_SCOPE


def test_every_in_scope_reducer_is_implemented() -> None:
    """ADR 0004: the port is all-or-nothing per channel. No partial reducers."""
    assert set(REDUCERS) >= IN_SCOPE, f"missing reducers: {sorted(IN_SCOPE - set(REDUCERS))}"


@pytest.mark.parametrize("fixture", SCOPED, ids=_ids(SCOPED))
def test_reducer_matches_upstream(fixture: ReducerFixture) -> None:
    reducer = REDUCERS[fixture.reducer]
    with frozen_clock():
        state = fixture.initial
        for action in fixture.actions:
            state = reducer(state, action)
    assert reduced_equal(state, fixture.expected), (
        f"{fixture.id}: {fixture.description}\n  got      {state}\n  expected {fixture.expected}"
    )


@pytest.mark.parametrize("fixture", SCOPED, ids=_ids(SCOPED))
def test_reducer_does_not_mutate_its_input(fixture: ReducerFixture) -> None:
    """Immutability cannot be expressed in the JSON fixtures, so it is asserted here.

    Upstream notes the gap directly: "Verifying that the reducer does not mutate
    the input state requires identity checks, which can't be expressed in JSON
    fixtures." A host that mutates in place corrupts its own replay log and any
    snapshot it has already handed out.
    """
    import copy

    reducer = REDUCERS[fixture.reducer]
    original = copy.deepcopy(fixture.initial)
    with frozen_clock():
        state = fixture.initial
        for action in fixture.actions:
            state = reducer(state, action)
    assert fixture.initial == original, f"{fixture.id}: reducer mutated its input state"
