"""The demo's changes are real, because a changeset is a RECORD.

The first version of this demo published proposals: `after` content for files
that were never written. VS Code opened `after.uri`, found nothing, and said
"The editor could not be opened because the file was not found."

The guide's own examples are "uncommitted working-tree edits, the diff between
two turns, the cumulative changes for the whole session, the staged index" --
every one a view of changes that have ALREADY happened. `FileEdit.before` is
documented as absent "for in-place file edits", which only makes sense if
`after` is the file on disk.
"""

from __future__ import annotations

from agent_host_server.provider.demo_changes import (
    BASELINE,
    DEMO_OPERATIONS,
    SCRATCH,
    demo_changeset,
    demo_file_changes,
    reset_scratch,
)


class TestTheWorkIsActuallyDone:
    def test_a_created_file_exists_on_disk(self) -> None:
        """The whole point. A client opens `after.uri` and must find a file."""
        demo_file_changes("hello")
        assert (SCRATCH / "new-file.txt").is_file()

    def test_a_deleted_file_is_actually_gone(self) -> None:
        demo_file_changes("hello")
        assert not (SCRATCH / "notes.md").exists()

    def test_an_edited_file_holds_the_reported_content(self) -> None:
        """`after` is not a story about the file; it IS the file."""
        changes = demo_file_changes("MARKER")
        edit = next(c for c in changes if c.uri.endswith("greeting.txt"))
        assert edit.after is not None
        assert (SCRATCH / "greeting.txt").read_bytes() == edit.after
        assert b"MARKER" in edit.after

    def test_every_reported_uri_matches_reality(self) -> None:
        """A `before` with no `after` must be gone; an `after` must exist."""
        from pathlib import Path
        from urllib.parse import unquote, urlparse

        for change in demo_file_changes("hello"):
            path = Path(unquote(urlparse(change.uri).path))
            if change.after is None:
                assert not path.exists(), f"{path.name} was reported deleted and is still here"
            else:
                assert path.is_file(), f"{path.name} was reported and does not exist"
                assert path.read_bytes() == change.after

    def test_all_three_diff_shapes_are_present(self) -> None:
        """A demo that only shows edits teaches nothing about the other two."""
        changes = demo_file_changes("hello")
        shapes = {
            ("edit" if c.before and c.after else "create" if c.after else "delete") for c in changes
        }
        assert shapes == {"edit", "create", "delete"}


class TestBlastRadius:
    def test_the_baseline_is_never_modified(self) -> None:
        before = {p.name: p.read_bytes() for p in BASELINE.iterdir() if p.is_file()}
        demo_file_changes("something destructive looking")
        after = {p.name: p.read_bytes() for p in BASELINE.iterdir() if p.is_file()}
        assert before == after

    def test_nothing_is_written_outside_scratch(self) -> None:
        """Every reported URI is inside the demo's own gitignored directory."""
        for change in demo_file_changes("hello"):
            assert change.uri.startswith(SCRATCH.as_uri()), change.uri

    def test_reset_restores_the_baseline(self) -> None:
        demo_file_changes("hello")
        reset_scratch()
        assert (SCRATCH / "notes.md").is_file()
        assert not (SCRATCH / "new-file.txt").exists()
        assert (SCRATCH / "greeting.txt").read_bytes() == (BASELINE / "greeting.txt").read_bytes()


class TestTheButtonsAreWellFormed:
    def test_every_operation_carries_the_required_fields(self) -> None:
        """One malformed operation makes the client's derived throw, and a
        throwing derived is swallowed -- so they all disappear together."""
        for operation in DEMO_OPERATIONS:
            wire = operation.to_wire()
            assert wire["scopes"]
            assert wire["status"] == "idle"

    def test_the_destructive_sounding_one_asks_first(self) -> None:
        reset = next(o for o in DEMO_OPERATIONS if o.id == "ahs-reset")
        assert reset.confirmation

    def test_the_changeset_reports_an_honest_kind(self) -> None:
        """These ARE working-tree edits that happened and are not committed."""
        assert demo_changeset().change_kind == "uncommitted"
