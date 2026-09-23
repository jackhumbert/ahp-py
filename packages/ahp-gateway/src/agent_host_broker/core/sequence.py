"""One `serverSeq` for the surfaces, out of one per node.

Every node runs its own host-global counter, and a surface must see a single
monotonic stream (it resumes on `lastSeenServerSeq`, and it drops an action
whose seq is not above a snapshot's `fromSeq`). So the broker restamps every
action with its own counter, and has to translate the other place a node seq
appears: a snapshot's `fromSeq`.

The translation relies on one property: within a single node link, frames
arrive in the node's seq order, and the broker stamps them in arrival order.
Then "actions after node seq *k*" are exactly "actions this link stamped after
the last one whose node seq was <= *k*", and that stamp is the `fromSeq` the
surface needs. Actions another node sent in between have larger stamps too,
but `fromSeq` is per channel, and those are on channels of their own.
"""

from __future__ import annotations

import bisect
from typing import Final

__all__ = ["BrokerClock", "LinkSequence"]

#: How many (node seq, broker seq) pairs a link keeps. A snapshot's `fromSeq`
#: is the node's *current* counter at subscribe time, so the lookup always
#: lands near the tail; the window only has to outlast one subscribe round trip.
_WINDOW: Final = 4096


class BrokerClock:
    """The broker's own counter, shared by every link of one surface connection."""

    def __init__(self, start: int = 0) -> None:
        # A reconnecting surface has already seen `start`; a counter that began
        # again at zero would make every new action look older than its mirror.
        self._seq = start

    @property
    def current(self) -> int:
        return self._seq

    def tick(self) -> int:
        self._seq += 1
        return self._seq


class LinkSequence:
    """Stamps one node's actions and translates its `fromSeq` values."""

    def __init__(self, clock: BrokerClock, node_seq: int) -> None:
        self._clock = clock
        # Everything the node did before the link opened is "before" whatever
        # the broker counter said at that moment.
        self._floor = (node_seq, clock.current)
        self._node: list[int] = []
        self._broker: list[int] = []

    def stamp(self, node_seq: int | None) -> int:
        stamped = self._clock.tick()
        last = self._node[-1] if self._node else self._floor[0]
        if node_seq is not None and node_seq == last:
            # Several actions may share one node seq (the host's sequencer
            # replays a batch under a single number), and a snapshot at that
            # seq contains all of them - so the seq maps to the LAST stamp.
            if self._node:
                self._broker[-1] = stamped
            else:
                self._floor = (last, stamped)
            return stamped
        if node_seq is None or node_seq < last:
            # A missing or decreasing seq is a node violating its own
            # counter. The action is still stamped and relayed, but kept out
            # of the log: `translate` bisects it, and one out-of-order entry
            # would misplace every later `fromSeq` on this link.
            return stamped
        self._node.append(node_seq)
        self._broker.append(stamped)
        if len(self._node) > 2 * _WINDOW:
            dropped_node, dropped_broker = self._node[-_WINDOW - 1], self._broker[-_WINDOW - 1]
            self._floor = (dropped_node, dropped_broker)
            del self._node[:-_WINDOW]
            del self._broker[:-_WINDOW]
        return stamped

    def translate(self, node_from_seq: int) -> int:
        index = bisect.bisect_right(self._node, node_from_seq)
        if index:
            return self._broker[index - 1]
        # At or before the floor. For the link-open floor that is exact: the
        # snapshot predates every action this link has stamped. For a pruned
        # floor it is only reachable by a snapshot older than the whole window,
        # which a live subscribe cannot produce (see `_WINDOW`).
        return self._floor[1]
