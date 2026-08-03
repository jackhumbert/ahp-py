"""The filesystem jail, attacked directly.

Every `resource*` command targets `ahp-root://`, so `may_see_channel` cannot
distinguish a source file from a private key, and the jail is the thing standing
between a peer that completed `initialize` and the host's disk.

The escapes worth testing are not "does `..` work" -- they are the ones that
beat a naive implementation:

* a symlink pointing outside the root;
* a symlink chain that leaves and returns;
* an absolute link target;
* `..` inside a link target;
* a link swapped in *after* the path was checked, which is what breaks
  `realpath`-then-open and is why this walks with `openat` instead.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest
from agent_host_protocol.errors import AhpError

from agent_host_server.core.resources import (
    NullResourceProvider,
    RootedFilesystemResourceProvider,
    WritableResourceProvider,
    is_writable,
)

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def jail(tmp_path: Path) -> tuple[RootedFilesystemResourceProvider, Path, Path]:
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    (root / "sub").mkdir(parents=True)
    outside.mkdir()
    (root / "hello.txt").write_text("inside\n")
    (root / "sub" / "nested.txt").write_text("nested\n")
    (outside / "secret.txt").write_text("SECRET\n")
    return RootedFilesystemResourceProvider(root), root, outside


def _uri(path: Path) -> str:
    return path.as_uri()


class TestReading:
    async def test_a_file_in_the_root_reads(
        self, jail: tuple[RootedFilesystemResourceProvider, Path, Path]
    ) -> None:
        provider, root, _ = jail
        content = await provider.read(_uri(root / "hello.txt"))
        assert content.data == b"inside\n"
        assert content.content_type == "text/plain"

    async def test_a_nested_file_reads(
        self, jail: tuple[RootedFilesystemResourceProvider, Path, Path]
    ) -> None:
        provider, root, _ = jail
        assert (await provider.read(_uri(root / "sub" / "nested.txt"))).data == b"nested\n"

    async def test_resolve_reports_type_size_and_an_etag(
        self, jail: tuple[RootedFilesystemResourceProvider, Path, Path]
    ) -> None:
        provider, root, _ = jail
        info = await provider.resolve(_uri(root / "hello.txt"))
        assert info.type == "file"
        assert info.size == 7
        assert info.etag is not None

    async def test_the_etag_changes_within_one_millisecond(
        self, jail: tuple[RootedFilesystemResourceProvider, Path, Path]
    ) -> None:
        """A size-plus-millisecond etag cannot distinguish two same-size writes
        inside one millisecond, which is a real lost-update window for the very
        `ifMatch` flow an etag exists to protect. Nanoseconds can."""
        provider, root, _ = jail
        target = root / "hello.txt"
        first = (await provider.resolve(_uri(target))).etag
        target.write_text("insid3\n")  # same length, immediately after
        assert (await provider.resolve(_uri(target))).etag != first

    async def test_listing_reports_entry_types(
        self, jail: tuple[RootedFilesystemResourceProvider, Path, Path]
    ) -> None:
        provider, root, _ = jail
        entries = {e.name: e.type for e in await provider.list_dir(_uri(root))}
        assert entries == {"hello.txt": "file", "sub": "directory"}


class TestEscapes:
    async def test_dot_dot_is_refused(
        self, jail: tuple[RootedFilesystemResourceProvider, Path, Path]
    ) -> None:
        provider, root, outside = jail
        with pytest.raises(AhpError) as caught:
            await provider.read(f"{_uri(root)}/../outside/secret.txt")
        assert caught.value.code == -32009

    async def test_an_absolute_path_outside_the_root_is_refused(
        self, jail: tuple[RootedFilesystemResourceProvider, Path, Path]
    ) -> None:
        provider, _root, outside = jail
        with pytest.raises(AhpError) as caught:
            await provider.read(_uri(outside / "secret.txt"))
        assert caught.value.code == -32009

    async def test_a_symlink_out_of_the_root_is_refused(
        self, jail: tuple[RootedFilesystemResourceProvider, Path, Path]
    ) -> None:
        provider, root, outside = jail
        (root / "escape").symlink_to(outside / "secret.txt")
        with pytest.raises(AhpError) as caught:
            await provider.read(_uri(root / "escape"))
        assert caught.value.code == -32009

    async def test_a_symlinked_directory_out_of_the_root_is_refused(
        self, jail: tuple[RootedFilesystemResourceProvider, Path, Path]
    ) -> None:
        """The interesting variant: the escape is a *parent* component, so the
        final open looks entirely innocent."""
        provider, root, outside = jail
        (root / "door").symlink_to(outside)
        with pytest.raises(AhpError) as caught:
            await provider.read(_uri(root / "door" / "secret.txt"))
        assert caught.value.code == -32009

    async def test_dot_dot_inside_a_link_target_is_refused(
        self, jail: tuple[RootedFilesystemResourceProvider, Path, Path]
    ) -> None:
        """`..` is legitimate inside a link, so it is normalised rather than
        rejected -- and the normalised result is then walked from the root like
        any other path, which is what stops it escaping."""
        provider, root, _outside = jail
        (root / "sub" / "up").symlink_to("../../outside/secret.txt")
        with pytest.raises(AhpError) as caught:
            await provider.read(_uri(root / "sub" / "up"))
        assert caught.value.code == -32009

    async def test_a_symlink_loop_terminates(
        self, jail: tuple[RootedFilesystemResourceProvider, Path, Path]
    ) -> None:
        provider, root, _ = jail
        (root / "a").symlink_to(root / "b")
        (root / "b").symlink_to(root / "a")
        with pytest.raises(AhpError):
            await provider.read(_uri(root / "a"))

    async def test_a_symlink_within_the_root_still_works(
        self, jail: tuple[RootedFilesystemResourceProvider, Path, Path]
    ) -> None:
        """The jail refuses escapes, not symlinks. A repository full of internal
        links has to stay readable or the feature is useless."""
        provider, root, _ = jail
        (root / "alias").symlink_to(root / "hello.txt")
        assert (await provider.read(_uri(root / "alias"))).data == b"inside\n"

    async def test_a_relative_symlink_within_the_root_still_works(
        self, jail: tuple[RootedFilesystemResourceProvider, Path, Path]
    ) -> None:
        provider, root, _ = jail
        (root / "sub" / "back").symlink_to("../hello.txt")
        assert (await provider.read(_uri(root / "sub" / "back"))).data == b"inside\n"

    async def test_a_component_swapped_after_the_check_cannot_escape(
        self, jail: tuple[RootedFilesystemResourceProvider, Path, Path]
    ) -> None:
        """The race that beats `realpath`-then-open.

        A naive provider canonicalises the path, compares the prefix, then opens
        by name. Between the compare and the open, a component is replaced with a
        symlink, and the open follows it: the check passed and the read escaped.
        Walking with `openat` has no such window, so the swap simply produces a
        refusal.
        """
        provider, root, outside = jail
        swappable = root / "swap"
        swappable.mkdir()
        (swappable / "target.txt").write_text("innocent\n")
        uri = _uri(swappable / "target.txt")
        assert (await provider.read(uri)).data == b"innocent\n"

        # The swap.
        os.rename(swappable / "target.txt", swappable / "moved.txt")
        (swappable / "target.txt").symlink_to(outside / "secret.txt")

        with pytest.raises(AhpError) as caught:
            await provider.read(uri)
        assert caught.value.code == -32009

    async def test_a_non_file_scheme_is_refused(
        self, jail: tuple[RootedFilesystemResourceProvider, Path, Path]
    ) -> None:
        """Treating an unknown scheme as a path is how a jail acquires a second
        entrance."""
        provider, _root, _ = jail
        for uri in ("git-blob:/abc", "virtual://client/x", "/etc/passwd"):
            with pytest.raises(AhpError) as caught:
                await provider.read(uri)
            assert caught.value.code == -32602

    async def test_a_device_file_is_refused(self, tmp_path: Path) -> None:
        """A fifo blocks forever and a character device streams forever. Neither
        is a resource anybody asked for."""
        root = tmp_path / "root"
        root.mkdir()
        os.mkfifo(root / "pipe")
        provider = RootedFilesystemResourceProvider(root)
        with pytest.raises(AhpError) as caught:
            await provider.read(_uri(root / "pipe"))
        assert caught.value.code == -32009


class TestNullProvider:
    async def test_it_exposes_nothing_and_says_not_found(self) -> None:
        """`NotFound`, not `PermissionDenied`: a host with no provider has no
        resources, and "denied" would tell a peer something is there."""
        provider = NullResourceProvider()
        for call in (
            provider.resolve("file:///etc/passwd"),
            provider.read("file:///etc/passwd"),
            provider.list_dir("file:///etc"),
        ):
            with pytest.raises(AhpError) as caught:
                await call
            assert caught.value.code == -32008


class TestWriting:
    """The write half. A second opt-in on top of installing the provider at
    all: reading discloses, writing destroys."""

    @pytest.fixture
    def writable(self, tmp_path: Path) -> tuple[RootedFilesystemResourceProvider, Path]:
        root = tmp_path / "w"
        root.mkdir()
        (root / "file.txt").write_text("hello")
        return RootedFilesystemResourceProvider(root, writable=True), root

    async def test_a_read_only_provider_refuses_every_mutation(
        self, jail: tuple[RootedFilesystemResourceProvider, Path, Path]
    ) -> None:
        provider, root, _ = jail
        with pytest.raises(AhpError) as caught:
            await provider.write(_uri(root / "new.txt"), b"x")
        assert caught.value.code == -32009

    async def test_truncate_overwrites(
        self, writable: tuple[RootedFilesystemResourceProvider, Path]
    ) -> None:
        provider, root = writable
        await provider.write(_uri(root / "file.txt"), b"bye")
        assert (root / "file.txt").read_bytes() == b"bye"

    async def test_append_writes_at_eof(
        self, writable: tuple[RootedFilesystemResourceProvider, Path]
    ) -> None:
        """`append` roots `position` at EOF and counts BACKWARDS, which is the
        easy thing to get inverted."""
        provider, root = writable
        await provider.write(_uri(root / "file.txt"), b"!", mode="append")
        assert (root / "file.txt").read_bytes() == b"hello!"

    async def test_append_with_a_position_inserts_before_eof(
        self, writable: tuple[RootedFilesystemResourceProvider, Path]
    ) -> None:
        provider, root = writable
        await provider.write(_uri(root / "file.txt"), b"-", mode="append", position=2)
        assert (root / "file.txt").read_bytes() == b"hel-lo"

    async def test_insert_keeps_the_tail(
        self, writable: tuple[RootedFilesystemResourceProvider, Path]
    ) -> None:
        provider, root = writable
        await provider.write(_uri(root / "file.txt"), b"XY", mode="insert", position=1)
        assert (root / "file.txt").read_bytes() == b"hXYello"

    async def test_create_only_refuses_an_existing_file(
        self, writable: tuple[RootedFilesystemResourceProvider, Path]
    ) -> None:
        provider, root = writable
        with pytest.raises(AhpError) as caught:
            await provider.write(_uri(root / "file.txt"), b"x", create_only=True)
        assert caught.value.code == -32010

    async def test_if_match_detects_a_concurrent_write(
        self, writable: tuple[RootedFilesystemResourceProvider, Path]
    ) -> None:
        """The whole reason an etag exists: somebody else wrote between the read
        and this write."""
        provider, root = writable
        etag = (await provider.resolve(_uri(root / "file.txt"))).etag
        (root / "file.txt").write_text("changed underneath")
        with pytest.raises(AhpError) as caught:
            await provider.write(_uri(root / "file.txt"), b"mine", if_match=etag)
        assert caught.value.code == -32011

    async def test_if_match_allows_an_unchanged_file(
        self, writable: tuple[RootedFilesystemResourceProvider, Path]
    ) -> None:
        provider, root = writable
        etag = (await provider.resolve(_uri(root / "file.txt"))).etag
        await provider.write(_uri(root / "file.txt"), b"mine", if_match=etag)
        assert (root / "file.txt").read_bytes() == b"mine"

    async def test_concurrent_appends_do_not_lose_data(
        self, writable: tuple[RootedFilesystemResourceProvider, Path]
    ) -> None:
        """ "The server MUST evaluate the effective EOF and write atomically with
        respect to other appenders." Two appends that each read EOF before
        either writes would otherwise overwrite one another."""
        import asyncio

        provider, root = writable
        (root / "log.txt").write_text("")
        uri = _uri(root / "log.txt")
        await asyncio.gather(
            *(provider.write(uri, f"{i}\n".encode(), mode="append") for i in range(20))
        )
        assert len((root / "log.txt").read_text().splitlines()) == 20

    async def test_mkdir_is_recursive_and_idempotent(
        self, writable: tuple[RootedFilesystemResourceProvider, Path]
    ) -> None:
        provider, root = writable
        await provider.mkdir(_uri(root / "a" / "b" / "c"))
        assert (root / "a" / "b" / "c").is_dir()
        await provider.mkdir(_uri(root / "a" / "b" / "c"))  # a no-op success

    async def test_mkdir_over_a_file_is_already_exists(
        self, writable: tuple[RootedFilesystemResourceProvider, Path]
    ) -> None:
        provider, root = writable
        with pytest.raises(AhpError) as caught:
            await provider.mkdir(_uri(root / "file.txt"))
        assert caught.value.code == -32010

    async def test_delete_refuses_a_non_empty_directory_unless_recursive(
        self, writable: tuple[RootedFilesystemResourceProvider, Path]
    ) -> None:
        provider, root = writable
        (root / "dir").mkdir()
        (root / "dir" / "x").write_text("x")
        with pytest.raises(AhpError):
            await provider.delete(_uri(root / "dir"))
        await provider.delete(_uri(root / "dir"), recursive=True)
        assert not (root / "dir").exists()

    async def test_a_write_cannot_escape_the_jail(self, tmp_path: Path) -> None:
        """Writes go through exactly the same walk as reads, so there is no
        second route out."""
        root = tmp_path / "w2"
        outside = tmp_path / "out2"
        root.mkdir()
        outside.mkdir()
        (outside / "target.txt").write_text("original")
        provider = RootedFilesystemResourceProvider(root, writable=True)
        (root / "escape").symlink_to(outside / "target.txt")

        with pytest.raises(AhpError) as caught:
            await provider.write(_uri(root / "escape"), b"overwritten")
        assert caught.value.code in (-32008, -32009)
        assert (outside / "target.txt").read_text() == "original"

    async def test_move_and_copy_stay_inside(
        self, writable: tuple[RootedFilesystemResourceProvider, Path]
    ) -> None:
        provider, root = writable
        await provider.copy(_uri(root / "file.txt"), _uri(root / "copy.txt"))
        assert (root / "copy.txt").read_text() == "hello"
        await provider.move(_uri(root / "copy.txt"), _uri(root / "moved.txt"))
        assert (root / "moved.txt").read_text() == "hello"
        assert not (root / "copy.txt").exists()


class TestTheWriteApiRefusesRatherThanGuesses:
    """The provider's own backstops, reachable without going through the wire.

    The dispatcher validates `mode` and `position` off the frame, but
    `WritableResourceProvider` is a public API an embedder calls directly -- and
    the destructive fallthrough lived down here, where an unrecognised mode
    became a full overwrite.
    """

    @pytest.fixture
    def writable(self, tmp_path: Path) -> tuple[RootedFilesystemResourceProvider, Path]:
        root = tmp_path / "w3"
        root.mkdir()
        (root / "file.txt").write_text("hello")
        return RootedFilesystemResourceProvider(root, writable=True), root

    async def test_an_unrecognised_mode_does_not_overwrite(
        self, writable: tuple[RootedFilesystemResourceProvider, Path]
    ) -> None:
        provider, root = writable
        with pytest.raises(AhpError) as caught:
            await provider.write(_uri(root / "file.txt"), b"x", mode="prepend")
        assert caught.value.code == -32602
        assert (root / "file.txt").read_text() == "hello"

    async def test_a_bad_mode_does_not_create_the_target_first(
        self, writable: tuple[RootedFilesystemResourceProvider, Path]
    ) -> None:
        """Validated before the open, not after: `O_CREAT` runs first, so a
        refusal on the far side of it leaves a 0-byte file behind."""
        provider, root = writable
        with pytest.raises(AhpError):
            await provider.write(_uri(root / "ghost.txt"), b"x", mode="prepend")
        assert not (root / "ghost.txt").exists()

    async def test_a_negative_position_is_refused(
        self, writable: tuple[RootedFilesystemResourceProvider, Path]
    ) -> None:
        provider, root = writable
        with pytest.raises(AhpError) as caught:
            await provider.write(_uri(root / "file.txt"), b"x", mode="append", position=-5)
        assert caught.value.code == -32602
        assert (root / "file.txt").read_bytes() == b"hello"


class TestIsWritable:
    """`isinstance(x, WritableResourceProvider)` answers a different question.

    It is structural, so a provider built `writable=False` satisfies it and then
    refuses every mutation -- which is how `resourceRequest(write=true)` came to
    be granted by a read-only host.
    """

    def test_a_read_only_rooted_provider_is_not_writable(self, tmp_path: Path) -> None:
        provider = RootedFilesystemResourceProvider(tmp_path)
        assert isinstance(provider, WritableResourceProvider), "the structural check still passes"
        assert not is_writable(provider)

    def test_a_writable_rooted_provider_is(self, tmp_path: Path) -> None:
        assert is_writable(RootedFilesystemResourceProvider(tmp_path, writable=True))

    def test_a_provider_that_exposes_nothing_is_not(self) -> None:
        assert not is_writable(NullResourceProvider())

    def test_an_embedders_provider_without_the_flag_is_taken_at_its_word(self) -> None:
        """No `writable` attribute means the question was never asked, and an
        embedder's own writable provider must keep working unchanged."""

        class Embedders:
            async def resolve(self, uri: str, *, follow_symlinks: bool = True) -> Any: ...
            async def read(self, uri: str) -> Any: ...
            async def list_dir(self, uri: str) -> Any: ...
            async def write(self, uri: str, data: bytes, **kwargs: Any) -> None: ...
            async def mkdir(self, uri: str) -> None: ...
            async def delete(self, uri: str, *, recursive: bool = False) -> None: ...
            async def move(self, source: str, destination: str, **kwargs: Any) -> None: ...
            async def copy(self, source: str, destination: str, **kwargs: Any) -> None: ...

        assert is_writable(Embedders())


class TestAncestorChain:
    """The chain above the root exists, and discloses almost nothing.

    A jail that refuses everything above its root is airtight and unusable.
    VS Code's directory picker validates a typed path by stat-ing the PARENT
    and the target inside one `try` (`simpleFileDialog.ts:914-918`). The parent
    is stat-ed first, so refusing it throws before the target is looked at,
    both stats are lost to the same `catch`, and the dialog reports "Please
    enter a path that exists" about a directory this host resolved
    successfully one call earlier.

    So ancestors resolve, and do nothing else.
    """

    @pytest.fixture
    def rooted(self, tmp_path: Path) -> RootedFilesystemResourceProvider:
        root = tmp_path / "outer" / "inner" / "served"
        root.mkdir(parents=True)
        (root / "kept.txt").write_text("in the jail")
        (tmp_path / "outer" / "secret.txt").write_text("NOT in the jail")
        (tmp_path / "outer" / "sibling").mkdir()
        return RootedFilesystemResourceProvider(root)

    async def test_a_strict_ancestor_resolves(
        self, rooted: RootedFilesystemResourceProvider, tmp_path: Path
    ) -> None:
        info = await rooted.resolve((tmp_path / "outer" / "inner").as_uri())
        assert info.type == "directory"

    async def test_the_ancestor_carries_no_metadata(
        self, rooted: RootedFilesystemResourceProvider, tmp_path: Path
    ) -> None:
        """It exists to be walked THROUGH, not observed."""
        info = await rooted.resolve((tmp_path / "outer").as_uri())
        assert info.to_wire() == {"uri": (tmp_path / "outer").as_uri(), "type": "directory"}

    async def test_listing_an_ancestor_reveals_only_the_way_down(
        self, rooted: RootedFilesystemResourceProvider, tmp_path: Path
    ) -> None:
        """Not the real listing -- `secret.txt` and `sibling` stay invisible."""
        entries = await rooted.list_dir((tmp_path / "outer").as_uri())
        assert [(e.name, e.type) for e in entries] == [("inner", "directory")]

    async def test_an_ancestor_is_still_unreadable(
        self, rooted: RootedFilesystemResourceProvider, tmp_path: Path
    ) -> None:
        with pytest.raises(AhpError) as caught:
            await rooted.read((tmp_path / "outer" / "secret.txt").as_uri())
        assert caught.value.code == -32009

    async def test_a_sibling_of_the_root_is_not_an_ancestor(
        self, rooted: RootedFilesystemResourceProvider, tmp_path: Path
    ) -> None:
        """Walkable is not the same as browsable. Only the chain opens."""
        for target in ("outer/sibling", "outer/secret.txt"):
            with pytest.raises(AhpError) as caught:
                await rooted.resolve((tmp_path / target).as_uri())
            assert caught.value.code == -32009, target

    async def test_the_root_itself_is_real_not_synthetic(
        self, rooted: RootedFilesystemResourceProvider
    ) -> None:
        """The root is served for real: it lists its actual contents."""
        entries = await rooted.list_dir(rooted.root.as_uri())
        assert [e.name for e in entries] == ["kept.txt"]

    async def test_dotdot_is_not_a_way_into_the_ancestor_path(
        self, rooted: RootedFilesystemResourceProvider
    ) -> None:
        with pytest.raises(AhpError) as caught:
            await rooted.resolve(f"{rooted.root.as_uri()}/../..")
        assert caught.value.code == -32009
