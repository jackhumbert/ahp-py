"""One serverSeq out of many: stamping and `fromSeq` translation."""

from ahp_gateway.core.sequence import GatewayClock, LinkSequence


def test_stamps_are_one_monotonic_stream_across_links() -> None:
    clock = GatewayClock()
    a, b = LinkSequence(clock, 100), LinkSequence(clock, 7)
    stamps = [a.stamp(101), b.stamp(8), a.stamp(102), b.stamp(9)]
    assert stamps == [1, 2, 3, 4]


def test_from_seq_maps_to_the_last_action_at_or_before_it() -> None:
    clock = GatewayClock()
    link = LinkSequence(clock, 10)
    link.stamp(11)  # gateway 1
    link.stamp(15)  # gateway 2
    link.stamp(20)  # gateway 3
    assert link.translate(15) == 2
    assert link.translate(19) == 2
    assert link.translate(20) == 3


def test_a_snapshot_older_than_the_link_predates_every_stamp() -> None:
    clock = GatewayClock()
    clock.tick()
    clock.tick()
    link = LinkSequence(clock, 50)
    link.stamp(51)
    assert link.translate(50) == 2
    assert link.translate(10) == 2


def test_actions_after_a_snapshot_stay_above_its_translated_from_seq() -> None:
    # The property the whole translation rests on: an action the node sent
    # after the snapshot must survive the surface's `serverSeq > fromSeq` check,
    # even when another node's actions were stamped in between.
    clock = GatewayClock()
    node, other = LinkSequence(clock, 0), LinkSequence(clock, 0)
    node.stamp(1)
    node.stamp(2)
    other.stamp(1)
    later = node.stamp(3)
    other.stamp(2)
    assert later > node.translate(2)
    assert node.stamp(4) > node.translate(3)


def test_the_window_prunes_without_breaking_recent_lookups() -> None:
    clock = GatewayClock()
    link = LinkSequence(clock, 0)
    for seq in range(1, 20_000):
        link.stamp(seq)
    assert link.translate(19_999) == clock.current
    assert link.translate(19_990) == clock.current - 9


def test_actions_sharing_a_node_seq_map_to_the_last_of_them() -> None:
    # A snapshot at node seq 5 already contains every action numbered 5.
    clock = GatewayClock()
    link = LinkSequence(clock, 0)
    link.stamp(5)
    last = link.stamp(5)
    assert link.translate(5) == last


def test_an_out_of_order_seq_does_not_corrupt_later_translation() -> None:
    clock = GatewayClock()
    link = LinkSequence(clock, 0)
    link.stamp(10)
    link.stamp(3)
    link.stamp(None)
    at_eleven = link.stamp(11)
    assert link.translate(11) == at_eleven
    assert link.translate(10) == 1
