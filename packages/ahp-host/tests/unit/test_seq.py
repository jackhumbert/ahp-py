"""Sequence allocation across a restart.

The failure this exists to prevent is not a lost message, it is a **permanent**
one: the reference TypeScript client records `lastSeenServerSeq` with a
*maximum*, so a host that restarts at 0 leaves that client holding a number the
host will not reach again for hours. Its reconnects then degrade to a full state
transfer every time, forever, with nothing to notice it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ahp_host.core.seq import FileSequence, InMemorySequence


class TestInMemory:
    def test_starts_at_one_and_increases(self) -> None:
        allocator = InMemorySequence()
        assert [allocator.next() for _ in range(3)] == [1, 2, 3]


class TestFileSequence:
    def test_never_reissues_a_number_across_a_restart(self, tmp_path: Path) -> None:
        path = tmp_path / "seq"
        first = FileSequence(path, block=10)
        issued = [first.next() for _ in range(4)]

        # A crash: no clean shutdown, no flush of the in-flight value.
        second = FileSequence(path, block=10)
        resumed = [second.next() for _ in range(4)]

        assert min(resumed) > max(issued), (
            "a reused serverSeq lets a client mistake new state for old"
        )

    def test_a_clean_restart_skips_at_most_one_block(self, tmp_path: Path) -> None:
        """Skipping is safe -- nothing requires `serverSeq` to be contiguous --
        but skipping unboundedly would exhaust the number space."""
        path = tmp_path / "seq"
        FileSequence(path, block=10).next()
        resumed = FileSequence(path, block=10).next()
        assert resumed <= 1 + 10 + 1

    def test_crossing_a_block_boundary_stays_monotonic(self, tmp_path: Path) -> None:
        allocator = FileSequence(tmp_path / "seq", block=4)
        issued = [allocator.next() for _ in range(20)]
        assert issued == sorted(issued)
        assert len(set(issued)) == 20

        # And the persisted ceiling is still ahead of everything handed out.
        assert int((tmp_path / "seq").read_text()) >= max(issued)

    def test_a_missing_file_starts_from_zero(self, tmp_path: Path) -> None:
        assert FileSequence(tmp_path / "nested" / "seq").next() == 1

    def test_a_corrupt_counter_refuses_rather_than_restarting(self, tmp_path: Path) -> None:
        """Silently restarting at zero is the exact failure this class prevents,
        so a file that cannot be read is an error, not a fresh start."""
        path = tmp_path / "seq"
        path.write_text("not a number")
        with pytest.raises(ValueError, match="not a sequence counter"):
            FileSequence(path)

    def test_the_persisted_ceiling_is_never_behind_what_was_issued(self, tmp_path: Path) -> None:
        """The file holds the reserved ceiling, not the last number handed out.
        If it ever lagged, a restart would reissue numbers a client had seen."""
        path = tmp_path / "seq"
        allocator = FileSequence(path, block=4)
        for _ in range(20):
            issued = allocator.next()
            assert int(path.read_text()) >= issued

    def test_the_counter_file_is_replaced_not_rewritten(self, tmp_path: Path) -> None:
        """A truncated prefix could parse as a *smaller* number, so the write is
        atomic and no temporary file is left behind to be read instead."""
        path = tmp_path / "seq"
        allocator = FileSequence(path, block=2)
        for _ in range(6):
            allocator.next()
        assert sorted(p.name for p in tmp_path.iterdir()) == ["seq"]
