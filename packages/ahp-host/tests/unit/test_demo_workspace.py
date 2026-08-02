"""The demo's edits are real, and git is the independent check.

A changeset is a RECORD of changes that have happened. The previous demo
published proposals for files it never wrote, so a client opening `after.uri`
found nothing. These tests assert the filesystem, not the report -- if the two
ever disagree, the report is the thing that is wrong.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from urllib.parse import unquote, urlparse

import pytest

from agent_host_server.provider.demo_workspace import (
    DemoWorkspace,
    changeset_uris,
    workspace_changeset,
)


def _sandbox(root: Path) -> DemoWorkspace:
    (root / "src" / "greeter").mkdir(parents=True)
    (root / "docs").mkdir()
    (root / "src" / "greeter" / "core.py").write_text('GREETING = "hello"\nRETRIES = 1\n')
    (root / "config.json").write_text('{"retries": 1, "verbose": false}\n')
    (root / "docs" / "notes.md").write_text("# Notes\n")
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=root, check=True)
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "baseline"],
        cwd=root,
        check=True,
    )
    return DemoWorkspace(root)


def _path(uri: str) -> Path:
    return Path(unquote(urlparse(uri).path))


class TestTheEditsAreReal:
    def test_every_report_matches_the_filesystem(self, tmp_path: Path) -> None:
        """The assertion that matters: a deletion is gone, an `after` is there
        with exactly the reported bytes. A client opens these paths."""
        workspace = _sandbox(tmp_path)
        for change in workspace.apply_demo_edits("howdy"):
            path = _path(change.uri)
            if change.after is None:
                assert not path.exists(), f"{path.name} reported deleted, still present"
            else:
                assert path.is_file(), f"{path.name} reported, does not exist"
                assert path.read_bytes() == change.after

    def test_all_three_diff_shapes(self, tmp_path: Path) -> None:
        changes = _sandbox(tmp_path).apply_demo_edits("howdy")
        shapes = {
            ("edit" if c.before and c.after else "create" if c.after else "delete") for c in changes
        }
        assert shapes == {"edit", "create", "delete"}

    def test_before_comes_from_git_not_from_memory(self, tmp_path: Path) -> None:
        """`before` is the COMMITTED state, so a second turn still diffs
        against HEAD rather than against the previous turn's output."""
        workspace = _sandbox(tmp_path)
        workspace.apply_demo_edits("first")
        changes = workspace.apply_demo_edits("second")
        core = next(c for c in changes if c.uri.endswith("core.py"))
        assert core.before is not None
        assert b'GREETING = "hello"' in core.before

    def test_the_message_reaches_the_edit(self, tmp_path: Path) -> None:
        changes = _sandbox(tmp_path).apply_demo_edits("MARKER")
        core = next(c for c in changes if c.uri.endswith("core.py"))
        assert core.after is not None
        assert b'GREETING = "MARKER"' in core.after

    def test_json_stays_valid(self, tmp_path: Path) -> None:
        workspace = _sandbox(tmp_path)
        workspace.apply_demo_edits("howdy")
        assert json.loads((workspace.root / "config.json").read_text())["retries"] == 3


class TestGitOperations:
    def test_stage_then_commit_lands_a_commit(self, tmp_path: Path) -> None:
        workspace = _sandbox(tmp_path)
        changes = workspace.apply_demo_edits("howdy")
        uris = [c.uri for c in changes]

        workspace.stage(uris)
        workspace.commit("from the test")

        log = workspace.git("log", "--oneline").stdout
        assert "from the test" in log
        # And the tree is clean, which is the check that `-A` staged the
        # DELETION too rather than skipping it.
        assert workspace.status().strip() == ""

    def test_revert_restores_and_removes(self, tmp_path: Path) -> None:
        workspace = _sandbox(tmp_path)
        changes = workspace.apply_demo_edits("howdy")
        workspace.revert([c.uri for c in changes])

        assert 'GREETING = "hello"' in (workspace.root / "src/greeter/core.py").read_text()
        assert (workspace.root / "docs/notes.md").is_file()
        # A file HEAD never had is removed rather than "restored" to nothing.
        assert not (workspace.root / "NOTES-FROM-AGENT.md").exists()

    def test_revert_is_scoped_to_managed_paths(self, tmp_path: Path) -> None:
        """A blanket `git checkout -- .` would eat work the demo never made."""
        workspace = _sandbox(tmp_path)
        bystander = workspace.root / "src" / "greeter" / "__init__.py"
        bystander.write_text("# edited by the user, not the agent\n")
        changes = workspace.apply_demo_edits("howdy")

        workspace.revert([c.uri for c in changes] + [bystander.as_uri()])

        assert bystander.read_text() == "# edited by the user, not the agent\n"

    def test_a_resource_target_touches_only_that_file(self, tmp_path: Path) -> None:
        workspace = _sandbox(tmp_path)
        changes = workspace.apply_demo_edits("howdy")
        uris = [c.uri for c in changes]
        config = next(u for u in uris if u.endswith("config.json"))

        workspace.revert(uris, target=config)

        assert json.loads((workspace.root / "config.json").read_text())["retries"] == 1
        # The others are untouched, which is what `scopes: ["resource"]` means.
        assert 'GREETING = "howdy"' in (workspace.root / "src/greeter/core.py").read_text()


class TestWithoutGit:
    def test_a_plain_directory_still_works(self, tmp_path: Path) -> None:
        """The library must not require git. A workspace with none still edits
        files and reports them; it just has no committed baseline."""
        (tmp_path / "src" / "greeter").mkdir(parents=True)
        (tmp_path / "src" / "greeter" / "core.py").write_text('GREETING = "hello"\n')
        workspace = DemoWorkspace(tmp_path)
        assert not workspace.is_git

        changes = workspace.apply_demo_edits("howdy")
        assert changes
        assert workspace.stage([c.uri for c in changes]) == "nothing to stage"
        assert workspace.commit("x") == "not a git repository"


def test_the_changeset_reports_an_honest_kind(tmp_path: Path) -> None:
    uncommitted, _staged = changeset_uris("echo:/s")
    assert workspace_changeset(tmp_path, "Uncommitted", uncommitted).change_kind == "uncommitted"


def test_changeset_uris_differ_per_session() -> None:
    """Fixed constants here collided: a channel is registered globally, so the
    SECOND session's publish raised `channel already registered` -- which
    killed the turn AFTER the files were edited, leaving the tree changed and
    the Changes view empty."""
    a = changeset_uris("echo:/one")
    b = changeset_uris("echo:/two")
    assert a != b
    assert a[0] != a[1]
    # Stable within a session, so republishing REPLACES rather than appending
    # a new changeset on every turn.
    assert changeset_uris("echo:/one") == a


@pytest.mark.parametrize(
    "operation", workspace_changeset(Path("/x"), "l", changeset_uris("s")[0]).operations
)
def test_every_operation_is_well_formed(operation: object) -> None:
    """One malformed operation makes the client's derived throw, and a throwing
    derived is swallowed -- so they all vanish together."""
    wire = operation.to_wire()  # type: ignore[attr-defined]
    assert wire["scopes"]
    assert wire["status"] == "idle"
