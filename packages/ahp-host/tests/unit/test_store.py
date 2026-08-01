"""The durable store, attacked and crashed.

Three classes of failure, none of which a round-trip test finds on its own:

* **Execution.** Everything in a stored session came off the wire -- the title,
  the transcript, the provider's opaque resume blob. The moment the format can
  reconstruct an object rather than a value, loading a session file runs
  whatever wrote it (`docs/roadmap.md` §10).
* **Escape.** The session URI is client-chosen and opaque, so it is an
  arbitrary-file-write primitive the instant it is used as a path component.
* **Loss.** A torn snapshot that still parses, one corrupt file that stops the
  host from starting at all, and a debounce that keeps every state of a turn
  except the last one.
"""

from __future__ import annotations

import ast
import asyncio
import json
import logging
import os
import stat
from pathlib import Path
from typing import Any

import pytest

from agent_host_server.core import store as store_module
from agent_host_server.core.store import (
    FileSessionStore,
    InMemorySessionStore,
    StoredSession,
)

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _session(uri: str = "echo:/one", **overrides: Any) -> StoredSession:
    fields: dict[str, Any] = {
        "uri": uri,
        "provider": "echo",
        "created_at": "2026-01-01T00:00:00.000Z",
        "title": "a title",
        "channels": {
            uri: {"title": "a title", "status": 0, "activeClients": {"items": []}},
            "ahp-chat://chat-1/ZWNobzovb25l": {"turns": {"items": []}},
            f"{uri}/annotations": {"annotations": {"items": []}},
        },
        "resume_state": {"thread": "t-1"},
    }
    fields.update(overrides)
    return StoredSession(**fields)


class TestFormat:
    def test_the_module_never_reaches_for_an_executable_format(self) -> None:
        """§10 calls this the most likely Python-shaped RCE in the roadmap, so
        the ban is asserted against the parsed source rather than left to
        review. Prose in the docstrings names these deliberately, which is why
        this reads imports and calls instead of grepping."""
        tree = ast.parse(Path(store_module.__file__).read_text(encoding="utf-8"))
        forbidden = {"pickle", "cPickle", "marshal", "shelve", "dill", "yaml"}
        imported: set[str] = set()
        called: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module is not None:
                imported.add(node.module.split(".")[0])
            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                called.add(node.func.id)
        assert not forbidden & imported
        assert not {"eval", "exec", "compile", "__import__"} & called

    async def test_a_saved_session_comes_back_verbatim(self, tmp_path: Path) -> None:
        store = FileSessionStore(tmp_path / "sessions")
        original = _session()
        await store.save(original)

        assert list(await FileSessionStore(tmp_path / "sessions").load_all()) == [original]

    async def test_an_absent_resume_state_stays_absent(self, tmp_path: Path) -> None:
        """`None` and `{}` mean different things to a provider being resumed,
        and JSON is where the two most easily collapse into one (invariant 18)."""
        store = FileSessionStore(tmp_path / "sessions")
        await store.save(_session(resume_state=None, title=None))

        (restored,) = await store.load_all()
        assert restored.resume_state is None
        assert restored.title is None

    async def test_an_empty_resume_state_is_not_an_absent_one(self, tmp_path: Path) -> None:
        store = FileSessionStore(tmp_path / "sessions")
        await store.save(_session(resume_state={}))

        (restored,) = await store.load_all()
        assert restored.resume_state == {}

    async def test_saving_twice_replaces_rather_than_accumulates(self, tmp_path: Path) -> None:
        store = FileSessionStore(tmp_path / "sessions")
        await store.save(_session(title="first"))
        await store.save(_session(title="second"))

        restored = await store.load_all()
        assert [s.title for s in restored] == ["second"]

    async def test_an_empty_store_loads_empty(self, tmp_path: Path) -> None:
        assert list(await FileSessionStore(tmp_path / "sessions").load_all()) == []


class TestAtomicWrites:
    async def test_a_finished_write_leaves_no_temporary_behind(self, tmp_path: Path) -> None:
        directory = tmp_path / "sessions"
        store = FileSessionStore(directory)
        for index in range(4):
            await store.save(_session(title=f"turn {index}"))

        assert [p.suffix for p in directory.iterdir()] == [".json"]

    async def test_a_failed_write_leaves_the_previous_file_intact(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The whole point of writing a sibling: a write that dies halfway must
        leave the old session readable, not a prefix of the new one that happens
        to parse into a shorter transcript."""
        directory = tmp_path / "sessions"
        store = FileSessionStore(directory)
        await store.save(_session(title="durable"))

        def explode(fileno: int) -> None:
            raise OSError("no space left on device")

        monkeypatch.setattr(os, "fsync", explode)
        with pytest.raises(OSError, match="no space"):
            await store.save(_session(title="lost"))
        monkeypatch.undo()

        (restored,) = await store.load_all()
        assert restored.title == "durable"
        assert [p.suffix for p in directory.iterdir()] == [".json"], "a temp file was orphaned"


class TestHostileSessionUris:
    """The URI is client-chosen and opaque (invariant 15). Every one of these is
    something a peer can put in `createSession`, and none of them may become a
    path component."""

    @pytest.mark.parametrize(
        "uri",
        [
            "../../../../etc/passwd",
            "/etc/cron.d/agent",
            "echo:/..%2f..%2fescape",
            "echo:/nul\x00.json",
            "echo:/" + "x" * 8192,
            "echo:/\ud800",  # a lone surrogate: legal JSON, illegal UTF-8
            "",
            ".",
            "..",
        ],
    )
    async def test_a_hostile_uri_cannot_escape_the_store_directory(
        self, tmp_path: Path, uri: str
    ) -> None:
        directory = tmp_path / "sessions"
        store = FileSessionStore(directory)
        await store.save(_session(uri=uri, channels={}))

        written = list(directory.iterdir())
        assert len(written) == 1
        assert written[0].parent == directory
        assert len(written[0].stem) == 64, "the name is a digest, not the URI"
        # Nothing appeared beside the store directory, above it, or at any
        # absolute path the URI named.
        assert [p.name for p in tmp_path.iterdir()] == ["sessions"]

        (restored,) = await store.load_all()
        assert restored.uri == uri, "the URI itself round-trips as data"

    async def test_two_hostile_uris_do_not_collide(self, tmp_path: Path) -> None:
        store = FileSessionStore(tmp_path / "sessions")
        await store.save(_session(uri="../a", channels={}))
        await store.save(_session(uri="../b", channels={}))

        assert {s.uri for s in await store.load_all()} == {"../a", "../b"}

    async def test_deleting_a_hostile_uri_removes_only_its_own_file(self, tmp_path: Path) -> None:
        directory = tmp_path / "sessions"
        store = FileSessionStore(directory)
        await store.save(_session(uri="echo:/keep", channels={}))
        await store.save(_session(uri="../../evil", channels={}))
        await store.delete("../../evil")

        assert [s.uri for s in await store.load_all()] == ["echo:/keep"]
        assert directory.is_dir()


class TestCorruption:
    @pytest.mark.parametrize(
        "content",
        [
            "",
            "{",
            '{"version": 1, "uri": "echo:/x", "provider": "echo", "createdAt": "z", "cha',
            "[1, 2, 3]",
            '"a string"',
            '{"version": 999, "uri": "echo:/x", "provider": "e", "createdAt": "z", "channels": {}}',
            '{"version": 1, "provider": "e", "createdAt": "z", "channels": {}}',
            '{"version": 1, "uri": 7, "provider": "e", "createdAt": "z", "channels": {}}',
            '{"version": 1, "uri": "e:/x", "provider": "e", "createdAt": "z", "channels": []}',
            '{"version": 1, "uri": "e:/x", "provider": "e", "createdAt": "z",'
            ' "channels": {"c": 1}}',
        ],
    )
    async def test_one_corrupt_file_does_not_stop_the_other_nine(
        self, tmp_path: Path, content: str
    ) -> None:
        directory = tmp_path / "sessions"
        store = FileSessionStore(directory)
        await store.save(_session(uri="echo:/healthy"))
        (directory / f"{'ab' * 32}.json").write_text(content, encoding="utf-8")

        assert [s.uri for s in await store.load_all()] == ["echo:/healthy"]

    async def test_a_skipped_file_is_reported(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Skipping is right; skipping *silently* means a host quietly forgets a
        session and nothing ever says which one."""
        directory = tmp_path / "sessions"
        store = FileSessionStore(directory)
        (directory / f"{'cd' * 32}.json").write_text("not json", encoding="utf-8")

        with caplog.at_level(logging.WARNING, logger=store_module.__name__):
            assert list(await store.load_all()) == []
        assert "cdcdcd" in caplog.text

    async def test_a_file_that_is_not_ours_is_ignored(self, tmp_path: Path) -> None:
        directory = tmp_path / "sessions"
        store = FileSessionStore(directory)
        await store.save(_session())
        (directory / "README").write_text("this directory holds sessions", encoding="utf-8")

        assert len(await store.load_all()) == 1


class TestDebounce:
    async def test_rapid_saves_for_one_uri_become_a_single_write(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        writes: list[str] = []
        real = store_module._write_atomically

        def counting(path: Path, text: str) -> None:
            writes.append(text)
            real(path, text)

        monkeypatch.setattr(store_module, "_write_atomically", counting)
        store = FileSessionStore(tmp_path / "sessions", debounce=0.05)
        for index in range(20):
            await store.save_soon(_session(title=f"turn {index}"))
        await store.flush()

        assert len(writes) == 1, "a streaming turn fsynced per token"
        assert json.loads(writes[0])["title"] == "turn 19"

    async def test_the_last_state_wins(self, tmp_path: Path) -> None:
        store = FileSessionStore(tmp_path / "sessions", debounce=0.05)
        for index in range(5):
            await store.save_soon(_session(title=f"turn {index}"))
        await store.flush()

        (restored,) = await store.load_all()
        assert restored.title == "turn 4"

    async def test_flush_really_waits_for_the_disk(self, tmp_path: Path) -> None:
        """`flush` returning while a write is still in a worker thread would
        make a clean shutdown lose the turn it was called to preserve."""
        directory = tmp_path / "sessions"
        store = FileSessionStore(directory, debounce=30.0)
        await store.save_soon(_session(title="only"))
        await store.flush()

        # Read the file directly: nothing in the store is consulted.
        (path,) = list(directory.glob("*.json"))
        assert json.loads(path.read_text(encoding="utf-8"))["title"] == "only"

    async def test_a_save_arriving_during_a_write_is_not_lost(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The dangerous interleaving: the drain has already taken its batch and
        is blocked on the disk when the newest state of the turn arrives."""
        real = store_module._write_atomically
        started = asyncio.Event()
        release = asyncio.Event()
        loop = asyncio.get_running_loop()

        def blocking(path: Path, text: str) -> None:
            loop.call_soon_threadsafe(started.set)
            asyncio.run_coroutine_threadsafe(_wait(release), loop).result(10)
            real(path, text)

        monkeypatch.setattr(store_module, "_write_atomically", blocking)
        store = FileSessionStore(tmp_path / "sessions", debounce=0)
        await store.save_soon(_session(title="first"))
        await started.wait()
        await store.save_soon(_session(title="second"))
        release.set()
        await store.flush()

        (restored,) = await store.load_all()
        assert restored.title == "second"

    async def test_deleting_a_session_cancels_the_save_that_would_revive_it(
        self, tmp_path: Path
    ) -> None:
        store = FileSessionStore(tmp_path / "sessions", debounce=0.05)
        await store.save_soon(_session())
        await store.delete("echo:/one")
        await store.flush()
        await asyncio.sleep(0.1)

        assert list(await store.load_all()) == []

    async def test_an_explicit_save_supersedes_a_queued_one(self, tmp_path: Path) -> None:
        store = FileSessionStore(tmp_path / "sessions", debounce=0.05)
        await store.save_soon(_session(title="queued"))
        await store.save(_session(title="explicit"))
        await asyncio.sleep(0.1)

        (restored,) = await store.load_all()
        assert restored.title == "explicit"

    async def test_a_write_that_fails_is_logged_rather_than_raised(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Nobody awaits a debounced save, so there is nowhere to raise -- but a
        store that has stopped persisting must not look like one that works."""

        def explode(path: Path, text: str) -> None:
            raise OSError("read-only file system")

        monkeypatch.setattr(store_module, "_write_atomically", explode)
        store = FileSessionStore(tmp_path / "sessions", debounce=0)
        with caplog.at_level(logging.WARNING, logger=store_module.__name__):
            await store.save_soon(_session())
            await store.flush()
        assert "read-only file system" in caplog.text

    async def test_closing_flushes_what_is_outstanding(self, tmp_path: Path) -> None:
        store = FileSessionStore(tmp_path / "sessions", debounce=30.0)
        await store.save_soon(_session(title="at shutdown"))
        await store.aclose()

        (restored,) = await store.load_all()
        assert restored.title == "at shutdown"

    async def test_saving_after_close_refuses_rather_than_silently_dropping(
        self, tmp_path: Path
    ) -> None:
        store = FileSessionStore(tmp_path / "sessions")
        await store.aclose()
        with pytest.raises(RuntimeError, match="closed"):
            await store.save_soon(_session())

    async def test_a_negative_debounce_is_refused(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="debounce"):
            FileSessionStore(tmp_path / "sessions", debounce=-1)


class TestFileModes:
    async def test_a_session_file_is_owner_only(self, tmp_path: Path) -> None:
        """It is the whole transcript, in plaintext, on a shared machine."""
        directory = tmp_path / "sessions"
        store = FileSessionStore(directory)
        await store.save(_session())

        (path,) = list(directory.glob("*.json"))
        assert stat.S_IMODE(path.stat().st_mode) == 0o600

    async def test_the_directory_is_owner_only_even_if_it_already_existed(
        self, tmp_path: Path
    ) -> None:
        """`mkdir(exist_ok=True)` accepts whatever mode it finds, so the mode is
        asserted on every open rather than only on creation."""
        directory = tmp_path / "sessions"
        directory.mkdir(mode=0o755)
        FileSessionStore(directory)

        assert stat.S_IMODE(directory.stat().st_mode) == 0o700


class TestInMemory:
    async def test_it_keeps_nothing_across_instances(self, tmp_path: Path) -> None:
        first = InMemorySessionStore()
        await first.save(_session())
        await first.flush()

        assert list(await InMemorySessionStore().load_all()) == []

    async def test_it_keeps_nothing_at_all(self) -> None:
        """Deliberate: the host already holds its live sessions, and a parallel
        copy of every transcript would be a leak nothing ever reads."""
        store = InMemorySessionStore()
        await store.save(_session())
        await store.save_soon(_session())
        await store.flush()

        assert list(await store.load_all()) == []

    async def test_delete_and_close_are_no_ops(self) -> None:
        store = InMemorySessionStore()
        await store.delete("echo:/never-existed")
        await store.aclose()


async def _wait(event: asyncio.Event) -> None:
    await event.wait()
