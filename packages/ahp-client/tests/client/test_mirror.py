"""The state mirror and write-ahead reconciliation.

The property tests are the real gate: reconciliation has no runnable
counterparty (VS Code's implementation is internal and unversioned), so
self-consistency under arbitrary interleavings is the strongest evidence
available. The example tests pin the specific arms that reimplementations get
wrong.
"""

from __future__ import annotations

from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from ahp_client.client.events import ActionRejected, Diagnostic, SequenceGap
from ahp_client.client.mirror import (
    ApplyOutcome,
    GapPolicy,
    PendingPolicy,
    StateMirror,
)

CHAT = "ahp-chat://c/session"
ROOT = "ahp-root://"


def _mirror(**kwargs: Any) -> tuple[StateMirror, list[Diagnostic]]:
    seen: list[Diagnostic] = []
    mirror = StateMirror(client_id="me", on_diagnostic=seen.append, **kwargs)
    return mirror, seen


def _snapshot(uri: str, state: Any, from_seq: int = 0) -> dict[str, Any]:
    return {"resource": uri, "state": state, "fromSeq": from_seq}


def _envelope(
    uri: str,
    action: dict[str, Any],
    *,
    server_seq: int = 0,
    origin: dict[str, Any] | None = None,
    rejection: str | None = None,
) -> dict[str, Any]:
    envelope: dict[str, Any] = {"channel": uri, "action": action, "serverSeq": server_seq}
    if origin is not None:
        envelope["origin"] = origin
    if rejection is not None:
        envelope["rejectionReason"] = rejection
    return envelope


# ── binding ──────────────────────────────────────────────────────────────────


def test_reducers_bind_by_name_not_by_uri_scheme() -> None:
    """VS Code mints `<provider>:/<uuid>` for sessions and three
    `agenthost-terminal:` forms for terminals. A scheme-routed lookup binds
    nothing and freezes state while actions keep arriving."""
    mirror, _ = _mirror()
    channel = mirror.bind("copilot:/abc-123", "session")
    assert channel.reducer_name == "session"
    channel = mirror.bind("agenthost-terminal://shell/s/t", "terminal")
    assert channel.reducer_name == "terminal"


def test_an_unknown_reducer_name_fails_loudly() -> None:
    mirror, _ = _mirror()
    with pytest.raises(KeyError):
        mirror.bind(CHAT, "not-a-reducer")


def test_a_snapshot_we_did_not_ask_for_falls_back_to_its_shape() -> None:
    mirror, _ = _mirror()
    mirror.apply_snapshot(_snapshot(ROOT, {"agents": [], "activeSessions": 0}))
    assert mirror.channels[ROOT].reducer_name == "root"


def test_a_snapshot_with_no_discriminating_key_is_declined_not_guessed() -> None:
    mirror, _ = _mirror()
    mirror.apply_snapshot(_snapshot("mystery:/1", {}))
    assert "mystery:/1" not in mirror.channels


# ── ordering ─────────────────────────────────────────────────────────────────


def test_envelopes_before_a_snapshot_are_buffered_not_dropped() -> None:
    """The reference client drops these, losing whatever arrived during the
    subscribe round trip."""
    mirror, _ = _mirror()
    mirror.bind(ROOT, "root")
    outcome = mirror.apply(
        _envelope(ROOT, {"type": "root/activeSessionsChanged", "activeSessions": 3}, server_seq=5)
    )
    assert outcome is ApplyOutcome.BUFFERED
    mirror.apply_snapshot(_snapshot(ROOT, {"agents": [], "activeSessions": 0}, from_seq=4))
    assert mirror.state(ROOT)["activeSessions"] == 3


def test_buffered_envelopes_at_or_below_from_seq_are_already_in_the_snapshot() -> None:
    """`Snapshot.fromSeq` is the protocol's only formal ordering rule: every
    subsequent action has a strictly greater serverSeq."""
    mirror, _ = _mirror()
    mirror.bind(ROOT, "root")
    mirror.apply(
        _envelope(ROOT, {"type": "root/activeSessionsChanged", "activeSessions": 99}, server_seq=3)
    )
    mirror.apply_snapshot(_snapshot(ROOT, {"agents": [], "activeSessions": 7}, from_seq=5))
    assert mirror.state(ROOT)["activeSessions"] == 7


def test_a_late_action_below_the_baseline_is_stale() -> None:
    mirror, _ = _mirror()
    mirror.apply_snapshot(_snapshot(ROOT, {"agents": []}, from_seq=10))
    outcome = mirror.apply(
        _envelope(ROOT, {"type": "root/activeSessionsChanged", "activeSessions": 1}, server_seq=9)
    )
    assert outcome is ApplyOutcome.STALE


def test_an_unregistered_channel_is_reported_not_silently_swallowed() -> None:
    mirror, _ = _mirror()
    assert mirror.apply(_envelope("nope:/1", {"type": "x"})) is ApplyOutcome.UNKNOWN_CHANNEL


def test_the_authorityless_root_spelling_is_the_same_channel() -> None:
    """VS Code matches the root channel by scheme -- `isAhpRootChannel`
    (sessionState.ts:478-487), which its doc says to always prefer over a
    direct `=== ROOT_STATE_URI` comparison -- because the authority-less form
    round-trips out of URI normalisation as `'ahp-root:'`. An exact-string
    lookup drops that variant as UNKNOWN_CHANNEL and silently freezes root
    state against a host that normalises URIs."""
    mirror, _ = _mirror()
    mirror.apply_snapshot(_snapshot("ahp-root:", {"agents": [], "activeSessions": 0}, from_seq=1))
    assert sorted(mirror.channels) == [ROOT]  # the snapshot bound the canonical key

    outcome = mirror.apply(
        _envelope(
            "ahp-root:", {"type": "root/activeSessionsChanged", "activeSessions": 3}, server_seq=2
        )
    )
    assert outcome is ApplyOutcome.APPLIED
    assert mirror.state(ROOT)["activeSessions"] == 3


# ── reconciliation ───────────────────────────────────────────────────────────


def test_own_echo_retires_its_pending_entry_and_applies() -> None:
    mirror, _ = _mirror()
    mirror.apply_snapshot(_snapshot(ROOT, {"agents": []}, from_seq=1))
    action = {"type": "root/agentsChanged", "agents": [{"id": "a"}]}
    mirror.record_pending(ROOT, action, 1)
    assert mirror.state(ROOT)["agents"] == [{"id": "a"}]
    assert mirror.confirmed(ROOT)["agents"] == []

    mirror.apply(_envelope(ROOT, action, server_seq=2, origin={"clientId": "me", "clientSeq": 1}))
    assert mirror.pending(ROOT) == ()
    assert mirror.confirmed(ROOT)["agents"] == [{"id": "a"}]


def test_a_rejected_echo_reverts_without_applying() -> None:
    mirror, seen = _mirror()
    mirror.apply_snapshot(_snapshot(ROOT, {"agents": []}, from_seq=1))
    action = {"type": "root/agentsChanged", "agents": [{"id": "a"}]}
    mirror.record_pending(ROOT, action, 1)

    outcome = mirror.apply(
        _envelope(
            ROOT,
            action,
            server_seq=2,
            origin={"clientId": "me", "clientSeq": 1},
            rejection="not allowed",
        )
    )
    assert outcome is ApplyOutcome.REJECTED
    assert mirror.pending(ROOT) == ()
    assert mirror.state(ROOT)["agents"] == []
    assert any(isinstance(d, ActionRejected) and d.reason == "not allowed" for d in seen)


def test_another_clients_rejected_action_is_not_applied_either() -> None:
    """The headline divergence. `rejectionReason` is on the ENVELOPE, and the
    host fans a refused action out to every subscriber of the channel while
    leaving its own state untouched. A second client that reduces it because the
    origin is not its own is wrong forever -- nothing later corrects it."""
    mirror, seen = _mirror()
    mirror.apply_snapshot(_snapshot(ROOT, {"agents": [], "activeSessions": 1}, from_seq=1))

    outcome = mirror.apply(
        _envelope(
            ROOT,
            {"type": "root/activeSessionsChanged", "activeSessions": 99},
            server_seq=2,
            origin={"clientId": "someone-else", "clientSeq": 4},
            rejection="not permitted",
        )
    )
    assert outcome is ApplyOutcome.REJECTED
    assert mirror.confirmed(ROOT)["activeSessions"] == 1
    # Somebody else's refusal is not our diagnostic: we had no optimistic
    # effect to revert.
    assert not [d for d in seen if isinstance(d, ActionRejected)]


def test_a_rejection_advances_the_high_water_mark_it_consumed() -> None:
    """The host numbers a rejection exactly as it numbers an applied action and
    logs it for replay, so the number is not a hole. Leaving it behind made
    every rejection manufacture a `SequenceGap` on the stream that exists to be
    trusted."""
    mirror, seen = _mirror()
    mirror.apply_snapshot(_snapshot(ROOT, {"agents": []}, from_seq=1))
    action = {"type": "root/agentsChanged", "agents": []}
    for seq in (2, 3, 4):
        mirror.record_pending(ROOT, action, seq)
        mirror.apply(
            _envelope(
                ROOT,
                action,
                server_seq=seq,
                origin={"clientId": "me", "clientSeq": seq},
                rejection="no",
            )
        )
    assert mirror.channels[ROOT].last_seq == 4
    assert not [d for d in seen if isinstance(d, SequenceGap)]


def test_an_own_echo_with_no_matching_pending_entry_still_applies() -> None:
    """`agentSubscription.ts:327-328`, and the arm every reimplementation of
    this algorithm leaves out. Dropping it loses state on any echo whose
    clientSeq we no longer hold -- which happens after a reconnect clears the
    queue."""
    mirror, _ = _mirror()
    mirror.apply_snapshot(_snapshot(ROOT, {"agents": []}, from_seq=1))
    mirror.apply(
        _envelope(
            ROOT,
            {"type": "root/agentsChanged", "agents": [{"id": "z"}]},
            server_seq=2,
            origin={"clientId": "me", "clientSeq": 99},
        )
    )
    assert mirror.confirmed(ROOT)["agents"] == [{"id": "z"}]


def test_a_foreign_action_applies_and_pending_rebases() -> None:
    mirror, _ = _mirror()
    mirror.apply_snapshot(_snapshot(ROOT, {"agents": [], "activeSessions": 0}, from_seq=1))
    mirror.record_pending(ROOT, {"type": "root/activeSessionsChanged", "activeSessions": 5}, 1)
    mirror.apply(
        _envelope(
            ROOT,
            {"type": "root/agentsChanged", "agents": [{"id": "other"}]},
            server_seq=2,
            origin={"clientId": "someone-else", "clientSeq": 4},
        )
    )
    state = mirror.state(ROOT)
    assert state["agents"] == [{"id": "other"}]  # theirs, confirmed
    assert state["activeSessions"] == 5  # ours, still pending on top
    assert len(mirror.pending(ROOT)) == 1


def test_an_absent_origin_and_an_explicit_null_origin_are_the_same_thing() -> None:
    mirror, _ = _mirror()
    mirror.apply_snapshot(_snapshot(ROOT, {"agents": []}, from_seq=1))
    for origin in (None, {"clientId": None}):
        envelope = _envelope(
            ROOT, {"type": "root/agentsChanged", "agents": [{"id": "s"}]}, server_seq=2
        )
        if origin is not None:
            envelope["origin"] = origin
        assert mirror.apply(envelope) is ApplyOutcome.APPLIED


def test_matching_is_exact_not_cumulative() -> None:
    """Cumulative ack drops a pending entry whenever a host echoes a later
    clientSeq first, which is legal. That trades a bounded leak for silent
    divergence, so we take the leak."""
    mirror, _ = _mirror()
    mirror.apply_snapshot(_snapshot(ROOT, {"agents": []}, from_seq=1))
    for seq in (1, 2, 3):
        mirror.record_pending(ROOT, {"type": "root/agentsChanged", "agents": []}, seq)
    mirror.apply(
        _envelope(
            ROOT,
            {"type": "root/agentsChanged", "agents": []},
            server_seq=2,
            origin={"clientId": "me", "clientSeq": 3},
        )
    )
    assert [p.client_seq for p in mirror.pending(ROOT)] == [1, 2]


def test_a_foreign_terminal_action_promotes_the_pending_turn_start() -> None:
    """`_promotePendingTurnStartIfTerminal` (agentSubscription.ts:483-501): a
    backend-originated `chat/turnComplete` can arrive without ever echoing the
    `chat/turnStarted` we dispatched -- no clientSeq, so `_retire` never
    matches. Without the promotion the reducer's `_end_turn` no-ops, the
    pending entry survives with nothing that can ever retire it, `optimistic`
    renders a stuck active turn forever, and the turn never reaches confirmed
    history."""
    mirror, _ = _mirror()
    mirror.apply_snapshot(_snapshot(CHAT, {"turns": [], "status": 1}, from_seq=1))
    mirror.record_pending(CHAT, {"type": "chat/turnStarted", "turnId": "t1"}, 1)
    assert mirror.state(CHAT)["activeTurn"]["id"] == "t1"

    outcome = mirror.apply(
        _envelope(CHAT, {"type": "chat/turnComplete", "turnId": "t1"}, server_seq=2)
    )
    assert outcome is ApplyOutcome.APPLIED
    assert mirror.pending(CHAT) == ()
    confirmed = mirror.confirmed(CHAT)
    assert [turn["id"] for turn in confirmed["turns"]] == ["t1"]
    assert confirmed.get("activeTurn") is None
    assert mirror.state(CHAT).get("activeTurn") is None


def test_the_promotion_only_retires_the_turn_the_terminal_action_names() -> None:
    """The reference matches on `turnId`, not on "any pending start": a
    terminal action for someone else's turn must leave our optimistic turn
    alone."""
    mirror, _ = _mirror()
    mirror.apply_snapshot(_snapshot(CHAT, {"turns": [], "status": 1}, from_seq=1))
    mirror.record_pending(CHAT, {"type": "chat/turnStarted", "turnId": "mine"}, 1)

    mirror.apply(_envelope(CHAT, {"type": "chat/turnCancelled", "turnId": "theirs"}, server_seq=2))
    assert [p.client_seq for p in mirror.pending(CHAT)] == [1]
    assert mirror.state(CHAT)["activeTurn"]["id"] == "mine"


# ── gaps ─────────────────────────────────────────────────────────────────────


def test_a_gap_is_reported_and_still_applied() -> None:
    mirror, seen = _mirror()
    mirror.apply_snapshot(_snapshot(ROOT, {"agents": []}, from_seq=1))
    mirror.apply(_envelope(ROOT, {"type": "root/agentsChanged", "agents": []}, server_seq=2))
    outcome = mirror.apply(
        _envelope(ROOT, {"type": "root/agentsChanged", "agents": [{"id": "x"}]}, server_seq=9)
    )
    assert outcome is ApplyOutcome.APPLIED
    gaps = [d for d in seen if isinstance(d, SequenceGap)]
    assert gaps == [SequenceGap(ROOT, 3, 9)]


def test_ignore_says_nothing() -> None:
    mirror, seen = _mirror(gap_policy=GapPolicy.IGNORE)
    mirror.apply_snapshot(_snapshot(ROOT, {"agents": []}, from_seq=1))
    mirror.apply(_envelope(ROOT, {"type": "root/agentsChanged", "agents": []}, server_seq=2))
    mirror.apply(_envelope(ROOT, {"type": "root/agentsChanged", "agents": []}, server_seq=9))
    assert not [d for d in seen if isinstance(d, SequenceGap)]


def test_ordinary_interleaving_of_two_channels_is_not_a_gap() -> None:
    """`serverSeq` is one host-global counter every channel draws from, so
    per-channel contiguity is not a property the protocol provides. Two
    subscribed channels were enough to make every reported gap a false
    positive."""
    mirror, seen = _mirror()
    mirror.apply_snapshot(_snapshot(ROOT, {"agents": [], "activeSessions": 0}, from_seq=1))
    mirror.apply_snapshot(_snapshot(CHAT, {"turns": [], "status": 1}, from_seq=1))
    for seq, uri in ((2, ROOT), (3, CHAT), (4, ROOT), (5, CHAT), (6, ROOT)):
        action: dict[str, Any] = (
            {"type": "root/activeSessionsChanged", "activeSessions": seq}
            if uri == ROOT
            else {"type": "chat/titleChanged", "title": str(seq)}
        )
        mirror.apply(_envelope(uri, action, server_seq=seq))
    assert not [d for d in seen if isinstance(d, SequenceGap)]


def test_a_late_subscriptions_baseline_carries_the_global_mark_with_it() -> None:
    """`Snapshot.fromSeq` is a reading of the same global counter, so it also
    says how far that counter has run. A channel that was quiet while another
    was subscribed to at 50 must not report its own next action as a
    48-envelope hole."""
    mirror, seen = _mirror()
    mirror.apply_snapshot(_snapshot(ROOT, {"agents": []}, from_seq=1))
    mirror.apply(_envelope(ROOT, {"type": "root/agentsChanged", "agents": []}, server_seq=2))
    mirror.apply_snapshot(_snapshot(CHAT, {"turns": [], "status": 1}, from_seq=50))
    mirror.apply(_envelope(ROOT, {"type": "root/agentsChanged", "agents": []}, server_seq=51))
    assert not [d for d in seen if isinstance(d, SequenceGap)]


def test_a_hole_right_after_a_from_seq_zero_snapshot_is_still_a_hole() -> None:
    """A fresh host's snapshot legitimately reports `fromSeq: 0`, and zero is a
    real baseline, not the absence of one. Gating the check on `not previous`
    made a loss in the stream's very first envelopes -- seq 5 arriving first --
    the one gap this check could never see."""
    mirror, seen = _mirror()
    mirror.apply_snapshot(_snapshot(ROOT, {"agents": []}, from_seq=0))
    mirror.apply(_envelope(ROOT, {"type": "root/agentsChanged", "agents": []}, server_seq=5))
    assert [d for d in seen if isinstance(d, SequenceGap)] == [SequenceGap(ROOT, 1, 5)]

    # And the contiguous case: seq 1 is exactly what baseline 0 expects next.
    mirror, seen = _mirror()
    mirror.apply_snapshot(_snapshot(ROOT, {"agents": []}, from_seq=0))
    mirror.apply(_envelope(ROOT, {"type": "root/agentsChanged", "agents": []}, server_seq=1))
    assert not [d for d in seen if isinstance(d, SequenceGap)]


def test_reseed_cannot_attribute_a_global_hole_so_it_marks_every_channel() -> None:
    """The lost envelope belonged to whichever channel the host numbered it on,
    which is exactly the information the hole destroyed. Reseeding only the
    channel that revealed it leaves the actual victim silently wrong."""
    mirror, _ = _mirror(gap_policy=GapPolicy.RESEED)
    mirror.apply_snapshot(_snapshot(ROOT, {"agents": []}, from_seq=1))
    mirror.apply_snapshot(_snapshot(CHAT, {"turns": [], "status": 1}, from_seq=1))
    mirror.apply(_envelope(ROOT, {"type": "root/agentsChanged", "agents": []}, server_seq=2))
    mirror.apply(_envelope(ROOT, {"type": "root/agentsChanged", "agents": []}, server_seq=9))
    assert mirror.stale == frozenset({ROOT, CHAT})


def test_a_gap_is_never_fatal_in_any_policy() -> None:
    for policy in GapPolicy:
        mirror, _ = _mirror(gap_policy=policy)
        mirror.apply_snapshot(_snapshot(ROOT, {"agents": []}, from_seq=1))
        mirror.apply(_envelope(ROOT, {"type": "root/agentsChanged", "agents": []}, server_seq=2))
        assert (
            mirror.apply(
                _envelope(ROOT, {"type": "root/agentsChanged", "agents": []}, server_seq=500)
            )
            is ApplyOutcome.APPLIED
        )


# ── reconnect ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("policy", "arm", "expected"),
    [
        (PendingPolicy.VSCODE, "replay", [2]),
        (PendingPolicy.VSCODE, "snapshot", []),
        (PendingPolicy.SPEC, "replay", []),
        (PendingPolicy.SPEC, "snapshot", []),
        (PendingPolicy.RESEND_ALL, "replay", [1, 2]),
        (PendingPolicy.RESEND_ALL, "snapshot", [1, 2]),
    ],
)
def test_pending_policy_across_a_reconnect(
    policy: PendingPolicy, arm: str, expected: list[int]
) -> None:
    """Three references disagree; the switch makes the disagreement visible
    rather than silently picking a side."""
    mirror, _ = _mirror()
    mirror.apply_snapshot(_snapshot(ROOT, {"agents": []}, from_seq=1))
    mirror.record_pending(ROOT, {"type": "root/agentsChanged", "agents": []}, 1)
    mirror.record_pending(ROOT, {"type": "root/agentsChanged", "agents": []}, 2)
    mirror.on_reconnect(policy=policy, arm=arm, acknowledged=[1])
    assert [p.client_seq for p in mirror.pending(ROOT)] == expected


@pytest.mark.parametrize(
    ("policy", "arm", "expected"),
    [
        (PendingPolicy.VSCODE, "replay", [2]),
        (PendingPolicy.VSCODE, "snapshot", []),
        (PendingPolicy.SPEC, "replay", []),
        (PendingPolicy.SPEC, "snapshot", []),
        (PendingPolicy.RESEND_ALL, "replay", [1, 2]),
        (PendingPolicy.RESEND_ALL, "snapshot", [1, 2]),
    ],
)
def test_every_surviving_pending_entry_is_handed_back_to_be_resent(
    policy: PendingPolicy, arm: str, expected: list[int]
) -> None:
    """Keeping an entry without putting it back on the wire is the one outcome
    none of the three references describes: `optimistic` then shows a turn the
    host has never heard of, with no echo that can ever retire it."""
    mirror, _ = _mirror()
    mirror.apply_snapshot(_snapshot(ROOT, {"agents": []}, from_seq=1))
    mirror.record_pending(ROOT, {"type": "root/agentsChanged", "agents": []}, 1)
    mirror.record_pending(ROOT, {"type": "root/agentsChanged", "agents": []}, 2)
    resend = mirror.on_reconnect(policy=policy, arm=arm, acknowledged=[1])
    assert [entry.client_seq for _uri, entry in resend] == expected
    assert {uri for uri, _entry in resend} <= {ROOT}


def test_what_is_resent_is_ordered_by_the_clientseq_it_was_dispatched_with() -> None:
    """Two channels' queues interleave; the host must see them in the order the
    caller sent them, not grouped by whichever channel iterates first."""
    mirror, _ = _mirror()
    mirror.apply_snapshot(_snapshot(ROOT, {"agents": []}, from_seq=1))
    mirror.apply_snapshot(_snapshot(CHAT, {"turns": [], "status": 1}, from_seq=1))
    mirror.record_pending(CHAT, {"type": "chat/titleChanged", "title": "a"}, 2)
    mirror.record_pending(ROOT, {"type": "root/agentsChanged", "agents": []}, 1)
    mirror.record_pending(CHAT, {"type": "chat/titleChanged", "title": "b"}, 3)
    resend = mirror.on_reconnect(policy=PendingPolicy.VSCODE, arm="replay")
    assert [entry.client_seq for _uri, entry in resend] == [1, 2, 3]


def test_missing_channels_are_forgotten() -> None:
    mirror, _ = _mirror()
    mirror.apply_snapshot(_snapshot(ROOT, {"agents": []}))
    mirror.mark_missing([ROOT])
    assert ROOT not in mirror.channels


# ── the optimistic read ──────────────────────────────────────────────────────


def test_a_dispatch_that_races_the_snapshot_stays_pending_without_reducing() -> None:
    """VS Code's `_recomputeOptimistic` replays only onto a base that exists
    (agentSubscription.ts:509-526) and its value stays `undefined` until the
    snapshot. The reducers spread `{**state}` and raise TypeError on `None`, so
    without the same guard a dispatch in the subscribe round-trip window makes
    the very next read blow up."""
    mirror, _ = _mirror()
    mirror.bind(CHAT, "chat")
    mirror.record_pending(CHAT, {"type": "chat/turnStarted", "turnId": "t1"}, 1)
    assert mirror.state(CHAT) is None  # pending, not reduced -- and not a TypeError

    mirror.apply_snapshot(_snapshot(CHAT, {"turns": [], "status": 1}, from_seq=1))
    assert mirror.state(CHAT)["activeTurn"]["id"] == "t1"  # replayed once there is a base


def test_two_reads_with_nothing_in_between_agree() -> None:
    """The chat reducer stamps `modifiedAt` from the clock on every run, so
    replaying pending per READ means two consecutive reads never compare equal
    and drift with the wall clock while nothing arrives -- defeating the
    equality-based change detection the reference's compute-on-event model
    supports. The replay is cached against the channel version and recomputed
    only after a write."""
    mirror, _ = _mirror()
    mirror.apply_snapshot(_snapshot(CHAT, {"turns": [], "status": 1}, from_seq=1))
    mirror.record_pending(CHAT, {"type": "chat/turnStarted", "turnId": "t1"}, 1)

    first = mirror.state(CHAT)
    assert mirror.state(CHAT) is first  # identical, not merely equal

    # A write invalidates: a foreign action rebases the pending queue.
    mirror.apply(
        _envelope(CHAT, {"type": "chat/activityChanged", "activity": "thinking"}, server_seq=2)
    )
    rebased = mirror.state(CHAT)
    assert rebased is not first
    assert rebased["activity"] == "thinking"
    assert rebased["activeTurn"]["id"] == "t1"


# ── properties ───────────────────────────────────────────────────────────────

_ACTIONS = st.integers(min_value=0, max_value=20).map(
    lambda n: {"type": "root/activeSessionsChanged", "activeSessions": n}
)


@settings(max_examples=200, deadline=None)
@given(
    script=st.lists(
        st.tuples(
            st.sampled_from(["own", "own-rejected", "foreign"]),
            _ACTIONS,
        ),
        min_size=1,
        max_size=25,
    )
)
def test_optimistic_is_always_confirmed_plus_pending(
    script: list[tuple[str, dict[str, Any]]],
) -> None:
    """The invariant the whole design rests on: optimistic state is never
    stored, so it cannot drift from its inputs."""
    mirror, _ = _mirror()
    mirror.apply_snapshot(_snapshot(ROOT, {"agents": [], "activeSessions": 0}, from_seq=0))
    channel = mirror.channels[ROOT]
    seq = 1
    client_seq = 1

    for kind, action in script:
        seq += 1
        if kind == "foreign":
            mirror.apply(_envelope(ROOT, action, server_seq=seq, origin={"clientId": "them"}))
            continue
        mirror.record_pending(ROOT, action, client_seq)
        mirror.apply(
            _envelope(
                ROOT,
                action,
                server_seq=seq,
                origin={"clientId": "me", "clientSeq": client_seq},
                rejection="no" if kind == "own-rejected" else None,
            )
        )
        client_seq += 1

        expected = channel.confirmed
        for entry in channel.pending:
            expected = channel.reducer(expected, entry.action)
        assert channel.optimistic == expected


@settings(max_examples=200, deadline=None)
@given(
    acknowledged=st.lists(st.integers(min_value=1, max_value=10), unique=True),
    outstanding=st.lists(st.integers(min_value=1, max_value=10), unique=True, min_size=1),
)
def test_pending_never_retains_an_acknowledged_client_seq(
    acknowledged: list[int], outstanding: list[int]
) -> None:
    mirror, _ = _mirror()
    mirror.apply_snapshot(_snapshot(ROOT, {"agents": []}, from_seq=0))
    for seq in outstanding:
        mirror.record_pending(ROOT, {"type": "root/agentsChanged", "agents": []}, seq)
    for index, seq in enumerate(acknowledged, start=2):
        mirror.apply(
            _envelope(
                ROOT,
                {"type": "root/agentsChanged", "agents": []},
                server_seq=index,
                origin={"clientId": "me", "clientSeq": seq},
            )
        )
    remaining = {p.client_seq for p in mirror.pending(ROOT)}
    assert remaining.isdisjoint(acknowledged)


@settings(max_examples=100, deadline=None)
@given(seqs=st.lists(st.integers(min_value=1, max_value=50), min_size=2, max_size=20))
def test_applying_is_total_and_never_raises(seqs: list[int]) -> None:
    """A reducer fault or a hostile envelope must not take the connection with
    it. Unknown actions return state unchanged, by protocol requirement."""
    mirror, _ = _mirror()
    mirror.apply_snapshot(_snapshot(ROOT, {"agents": []}, from_seq=0))
    for seq in seqs:
        mirror.apply(_envelope(ROOT, {"type": "root/somethingFromTomorrow"}, server_seq=seq))
    assert mirror.confirmed(ROOT) == {"agents": []}


def test_using_the_mirror_from_two_threads_fails_loudly() -> None:
    """`asyncio.to_thread(mirror.apply, envelope)` breaks the module-global
    clock invariant *silently* otherwise, producing wrong `modifiedAt` stamps
    under a concurrent `frozen_clock`."""
    import threading

    mirror, _ = _mirror()
    mirror.apply_snapshot(_snapshot(ROOT, {"agents": []}, from_seq=0))
    caught: list[BaseException] = []

    def other_thread() -> None:
        try:
            mirror.apply(
                _envelope(ROOT, {"type": "root/agentsChanged", "agents": []}, server_seq=2)
            )
        except BaseException as exc:
            caught.append(exc)

    worker = threading.Thread(target=other_thread)
    worker.start()
    worker.join()
    assert len(caught) == 1
    assert isinstance(caught[0], RuntimeError)
    assert "two threads" in str(caught[0])


def test_reading_optimistic_state_off_thread_fails_as_loudly_as_writing() -> None:
    """`state()` REDUCES whenever the pending queue is dirty, so
    `asyncio.to_thread(render, mirror.state(uri))` is the same silent clock
    hazard as an off-loop `apply()` -- invariant 13's failure must be loud on
    the read path too."""
    import threading

    mirror, _ = _mirror()
    mirror.apply_snapshot(_snapshot(ROOT, {"agents": []}, from_seq=0))
    caught: list[BaseException] = []

    def other_thread() -> None:
        try:
            mirror.state(ROOT)
        except BaseException as exc:
            caught.append(exc)

    worker = threading.Thread(target=other_thread)
    worker.start()
    worker.join()
    assert len(caught) == 1
    assert isinstance(caught[0], RuntimeError)


async def test_the_running_loops_thread_wins_the_pin_over_a_pre_loop_reader() -> None:
    """Invariant 13 names the RUNNING LOOP's thread as the authority, not
    whichever thread called first. A pre-loop reader that won the pin would
    otherwise make the client's own read loop the "wrong" thread and take the
    connection down with the complaint -- the RuntimeError must land on the
    off-loop reader instead."""
    import threading

    mirror, _ = _mirror()
    outcomes: list[BaseException | None] = []

    def off_loop_read() -> None:
        try:
            mirror.state(ROOT)
            outcomes.append(None)
        except BaseException as exc:
            outcomes.append(exc)

    early = threading.Thread(target=off_loop_read)
    early.start()
    early.join()
    assert outcomes == [None]  # the pre-loop reader pinned first, harmlessly

    # The loop's thread adopts the pin rather than dying on it...
    mirror.apply_snapshot(_snapshot(ROOT, {"agents": []}, from_seq=0))
    assert mirror.state(ROOT) == {"agents": []}

    # ...and the eventual complaint lands on the off-loop reader.
    late = threading.Thread(target=off_loop_read)
    late.start()
    late.join()
    assert isinstance(outcomes[1], RuntimeError)
