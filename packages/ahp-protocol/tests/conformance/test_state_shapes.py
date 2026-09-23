"""`reducer_for_state` against the whole reducer corpus.

This is the guard on the one routing mistake that fails *silently*. The
reference TypeScript client picks a reducer by URI scheme; VS Code mints
`<provider>:/<uuid>` for sessions and three `agenthost-terminal:` forms for
terminals, none matching the scheme table, so scheme routing binds no reducer at
all -- and a channel with no reducer keeps receiving actions while its state
freezes. Nothing raises. Nothing logs.

The corpus makes the shape classifier free to verify: every fixture declares the
reducer it belongs to, so 256 fixtures are 256 cases written by neither peer.
"""

from __future__ import annotations

from collections import Counter

from agent_host_protocol.channels import reducer_for_state
from agent_host_protocol.conformance.corpus import reducer_fixtures
from agent_host_protocol.reducers import REDUCERS

#: Measured, then pinned. A pin bump that changes the corpus's composition
#: should be noticed rather than absorbed.
EXPECTED_PER_REDUCER = {
    "root": 7,
    "session": 79,
    "chat": 123,
    "terminal": 19,
    "changeset": 16,
    "resourceWatch": 2,
    "annotations": 10,
}


def test_every_fixture_classifies_to_its_declared_reducer() -> None:
    counts: Counter[str] = Counter()
    mismatches: list[str] = []
    unclassified: list[str] = []

    for fixture in reducer_fixtures():
        got = reducer_for_state(fixture.initial)
        if got is None:
            unclassified.append(f"{fixture.id} (declares {fixture.reducer})")
        elif got != fixture.reducer:
            mismatches.append(f"{fixture.id}: declares {fixture.reducer}, classified {got}")
        else:
            counts[got] += 1

    assert not mismatches, "shape classifier disagrees with the corpus:\n" + "\n".join(mismatches)
    assert not unclassified, (
        "fixtures whose initial state carries no discriminating key:\n" + "\n".join(unclassified)
    )
    assert dict(counts) == EXPECTED_PER_REDUCER
    assert sum(counts.values()) == 256


def test_every_classification_names_a_real_reducer() -> None:
    """A name that is not in REDUCERS routes to nothing, which is the bug."""
    for name in EXPECTED_PER_REDUCER:
        assert name in REDUCERS


def test_ambiguous_and_hostile_inputs_decline_rather_than_guess() -> None:
    """`None` is the honest answer; a wrong reducer corrupts state silently."""
    assert reducer_for_state({}) is None
    assert reducer_for_state({"somethingElse": 1}) is None
    assert reducer_for_state(None) is None
    assert reducer_for_state([]) is None
    assert reducer_for_state("ahp-root://") is None


def test_session_wins_over_the_keys_it_inlines() -> None:
    """SessionState carries `annotations` and `changesets` of its own.

    An annotations-first table would classify every session channel as an
    annotations channel, and the corpus would not catch it because both are
    real reducers. The precedence order in `_SHAPE_ORDER` is what prevents it.
    """
    session_like = {"lifecycle": "ready", "annotations": [], "changesets": []}
    assert reducer_for_state(session_like) == "session"


def test_root_state_wins_over_a_resource_watch_root() -> None:
    """`RootState.agents` is checked before `ResourceWatchState.root`."""
    assert reducer_for_state({"agents": []}) == "root"
    assert reducer_for_state({"root": "file:///tmp", "recursive": True}) == "resourceWatch"
