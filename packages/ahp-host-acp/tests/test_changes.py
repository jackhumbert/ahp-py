"""ACP `diff` tool content -> the session's changeset, checked against the disk."""

from __future__ import annotations

from pathlib import Path

from ahp_host_acp.changes import Diff, SessionEdits, diffs_of
from ahp_host_acp.roots import Roots


def _edits(root: Path) -> SessionEdits:
    return SessionEdits(Roots.single(root))


def _only(edits: SessionEdits) -> tuple[bytes | None, bytes | None]:
    (change,) = edits.changes()
    return change.before, change.after


def test_diffs_are_read_from_tool_content() -> None:
    content: list[dict[str, object]] = [
        {"type": "content", "content": {"type": "text", "text": "x"}},
        {"type": "diff", "path": "/a", "oldText": None, "newText": "n"},
        {"type": "diff", "path": "/b", "newText": 3},
    ]
    assert diffs_of(content) == [Diff("/a", None, "n")]


def test_whole_file_diff_announced_before_the_write(tmp_path: Path) -> None:
    path = tmp_path / "notes.txt"
    path.write_text("one\n")
    edits = _edits(tmp_path)
    diff = Diff(str(path), "one\n", "one\ntwo\n")
    edits.announced([diff], tmp_path)
    path.write_text("one\ntwo\n")
    assert edits.completed([diff], tmp_path)
    assert _only(edits) == (b"one\n", b"one\ntwo\n")
    assert edits.changes()[0].uri == path.as_uri()


def test_a_fragment_diff_is_rebuilt_into_whole_files(tmp_path: Path) -> None:
    # Claude Code's ACP adapter sends an Edit's old_string/new_string.
    path = tmp_path / "code.py"
    path.write_text("a = 1\nb = 3\nc = 4\n")
    edits = _edits(tmp_path)
    assert edits.completed([Diff(str(path), "b = 2", "b = 3")], tmp_path)
    assert _only(edits) == (b"a = 1\nb = 2\nc = 4\n", b"a = 1\nb = 3\nc = 4\n")


def test_a_fragment_keeps_the_files_line_endings(tmp_path: Path) -> None:
    path = tmp_path / "code.py"
    path.write_bytes(b"a = 1\r\nb = 3\r\n")
    edits = _edits(tmp_path)
    edits.completed([Diff(str(path), "b = 2", "b = 3")], tmp_path)
    assert _only(edits)[0] == b"a = 1\r\nb = 2\r\n"


def test_an_ambiguous_fragment_is_left_out_rather_than_guessed(tmp_path: Path) -> None:
    path = tmp_path / "code.py"
    path.write_text("x = 3\ny = 3\n")
    edits = _edits(tmp_path)
    assert not edits.completed([Diff(str(path), "2", "3")], tmp_path)
    assert edits.changes() == []


def test_a_creation_has_no_before(tmp_path: Path) -> None:
    path = tmp_path / "new.txt"
    path.write_text("hello\n")
    edits = _edits(tmp_path)
    edits.completed([Diff("new.txt", None, "hello\n")], tmp_path)  # relative to the cwd
    assert _only(edits) == (None, b"hello\n")


def test_an_overwrite_announced_as_a_creation_keeps_the_old_file(tmp_path: Path) -> None:
    path = tmp_path / "a.txt"
    path.write_text("old\n")
    edits = _edits(tmp_path)
    diff = Diff(str(path), None, "new\n")
    edits.announced([diff], tmp_path)
    path.write_text("new\n")
    edits.completed([diff], tmp_path)
    assert _only(edits) == (b"old\n", b"new\n")


def test_files_outside_the_roots_are_not_read(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "secret.txt"
    outside.write_text("what is really on disk\n")
    edits = _edits(root)
    edits.announced([Diff(str(outside), "a\n", "b\n")], root)
    edits.completed([Diff(str(outside), "a\n", "b\n")], root)
    assert _only(edits) == (b"a\n", b"b\n")  # the agent's word, nothing read


def test_a_failed_call_counts_for_nothing(tmp_path: Path) -> None:
    path = tmp_path / "notes.txt"
    path.write_text("one\n")
    edits = _edits(tmp_path)
    diff = Diff(str(path), "one\n", "gone\n")
    edits.announced([diff], tmp_path)
    edits.abandoned([diff], tmp_path)
    assert edits.changes() == []
    # A later, real edit is measured from the file as it is then.
    path.write_text("one\ntwo\n")
    edits.completed([Diff(str(path), "one\n", "one\ntwo\n")], tmp_path)
    assert _only(edits) == (b"one\n", b"one\ntwo\n")


def test_later_edits_keep_the_first_before_and_a_revert_drops_out(tmp_path: Path) -> None:
    path = tmp_path / "notes.txt"
    path.write_text("v2\n")
    edits = _edits(tmp_path)
    edits.completed([Diff(str(path), "v1\n", "v2\n")], tmp_path)
    path.write_text("v3\n")
    edits.completed([Diff(str(path), "v2\n", "v3\n")], tmp_path)
    assert _only(edits) == (b"v1\n", b"v3\n")
    path.write_text("v1\n")
    edits.completed([Diff(str(path), "v3\n", "v1\n")], tmp_path)
    assert edits.changes() == []
    assert edits.changeset.change_kind == "session"
