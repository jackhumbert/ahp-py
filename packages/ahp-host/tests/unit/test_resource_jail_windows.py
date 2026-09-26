"""The Windows filesystem jail, attacked with real junctions and symlinks.

Windows only: these create reparse points and hold real NT handles. The pure
half (URI mapping, device names, streams, `..`, link-target mapping) is in
`test_resource_jail_windows_paths.py` and runs everywhere.

The escapes worth testing are the Windows-shaped ones:

* a junction (no privilege needed to make one) pointing out of the root;
* a symlink out of the root, absolute or relative;
* the Win32 spellings that name a different file than they appear to -- a
  trailing dot, a device name, an alternate data stream, an 8.3 short name;
* a component swapped for a junction after it was checked, which is what
  beats `realpath`-then-open, both once and under a racing thread;
* the served root itself replaced by a junction.

Symlinks need `SeCreateSymbolicLinkPrivilege` or Developer Mode. Without it
those tests skip -- unless `AHP_WINDOWS_JAIL_REQUIRED=1`, which CI sets, so a
skipped escape test cannot pass for a green one.
"""

from __future__ import annotations

import ctypes
import importlib
import os
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

import pytest
from ahp_protocol.errors import AhpError

from ahp_host.core.resources import RootedFilesystemResourceProvider

pytestmark = [
    pytest.mark.anyio,
    pytest.mark.skipif(sys.platform != "win32", reason="the Windows jail needs Windows"),
]

Jail = tuple[RootedFilesystemResourceProvider, Path, Path]


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _junction(target: Path, link: Path) -> None:
    """A directory junction. Needs no privilege, which is why it matters most."""
    winapi: Any = importlib.import_module("_winapi")
    create = getattr(winapi, "CreateJunction", None)
    if create is not None:
        create(str(target), str(link))
    else:  # pragma: no cover - every CPython on Windows has it today
        subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], check=True)


def _symlink(target: Path | str, link: Path, *, directory: bool = False) -> None:
    try:
        os.symlink(target, link, target_is_directory=directory)
    except OSError as exc:
        if os.environ.get("AHP_WINDOWS_JAIL_REQUIRED") == "1":
            raise
        pytest.skip(f"cannot create symlinks here (no privilege / Developer Mode): {exc}")


def _short_name(path: Path) -> str:
    kernel32: Any = getattr(ctypes, "windll").kernel32  # noqa: B009 - Windows-only attribute
    buffer = ctypes.create_unicode_buffer(1024)
    if not kernel32.GetShortPathNameW(str(path), buffer, 1024):
        return str(path)
    return str(buffer.value)


def _uri(path: Path) -> str:
    return path.as_uri()


async def _denied(call: Any) -> int:
    with pytest.raises(AhpError) as caught:
        await call
    return caught.value.code


@pytest.fixture
def base(tmp_path: Path) -> Path:
    """`tmp_path` in long form. On a CI runner it is `C:\\Users\\RUNNER~1\\...`,
    and an 8.3 spelling of the root is (deliberately) not the root."""
    return Path(os.path.realpath(tmp_path))


@pytest.fixture
def jail(base: Path) -> Jail:
    root = base / "root"
    outside = base / "outside"
    (root / "sub").mkdir(parents=True)
    outside.mkdir()
    (root / "hello.txt").write_bytes(b"inside\n")
    (root / "sub" / "nested.txt").write_bytes(b"nested\n")
    (outside / "secret.txt").write_bytes(b"SECRET\n")
    return RootedFilesystemResourceProvider(root), root, outside


class TestConstruction:
    def test_the_windows_implementation_is_selected(self, jail: Jail) -> None:
        from ahp_host.core.resources_windows import (
            WindowsRootedFilesystemResourceProvider,
        )

        provider, _root, _ = jail
        assert isinstance(provider, WindowsRootedFilesystemResourceProvider)
        assert isinstance(provider, RootedFilesystemResourceProvider)

    def test_writable_is_refused_at_construction(self, base: Path) -> None:
        with pytest.raises(ValueError, match="read-only"):
            RootedFilesystemResourceProvider(base, writable=True)

    async def test_every_mutation_is_refused(self, jail: Jail) -> None:
        provider, root, _ = jail
        target = _uri(root / "new.txt")
        assert await _denied(provider.write(target, b"x")) == -32009
        assert await _denied(provider.mkdir(target)) == -32009
        assert await _denied(provider.delete(_uri(root / "hello.txt"))) == -32009
        assert (root / "hello.txt").read_bytes() == b"inside\n"


class TestReading:
    async def test_a_file_in_the_root_reads(self, jail: Jail) -> None:
        provider, root, _ = jail
        content = await provider.read(_uri(root / "hello.txt"))
        assert content.data == b"inside\n"
        assert content.content_type == "text/plain"

    async def test_a_nested_file_reads(self, jail: Jail) -> None:
        provider, root, _ = jail
        assert (await provider.read(_uri(root / "sub" / "nested.txt"))).data == b"nested\n"

    async def test_resolve_answers_a_windows_file_uri(self, jail: Jail) -> None:
        provider, root, _ = jail
        info = await provider.resolve(_uri(root / "hello.txt"))
        assert info.uri == _uri(root / "hello.txt")
        assert info.uri.startswith("file:///")
        assert info.uri[9] == ":"
        assert (info.type, info.size) == ("file", 7)
        assert info.etag is not None

    async def test_a_root_given_as_a_windows_path_string(self, jail: Jail) -> None:
        _provider, root, _ = jail
        provider = RootedFilesystemResourceProvider(Path(str(root)))
        assert (await provider.resolve(root.as_uri())).uri == root.as_uri()

    async def test_case_and_vs_codes_encoding_are_accepted_and_canonicalised(
        self, jail: Jail
    ) -> None:
        provider, root, _ = jail
        exact = _uri(root / "hello.txt")
        shouted = "file:///" + exact[len("file:///") :].upper()
        # `file:///c%3A/...`: VS Code's lowercase drive and encoded colon.
        encoded = shouted[:8] + shouted[8].lower() + "%3A" + shouted[10:]
        for uri in (shouted, encoded):
            info = await provider.resolve(uri)
            assert info.uri == _uri(root / "hello.txt"), uri

    async def test_listing_reports_entry_types(self, jail: Jail) -> None:
        provider, root, _ = jail
        entries = {e.name: e.type for e in await provider.list_dir(_uri(root))}
        assert entries == {"hello.txt": "file", "sub": "directory"}

    async def test_a_directory_is_not_read(self, jail: Jail) -> None:
        provider, root, _ = jail
        assert await _denied(provider.read(_uri(root / "sub"))) == -32602


class TestWindowsSpellings:
    """Names Win32 would quietly turn into some other file."""

    async def test_a_trailing_dot_is_not_the_file_without_it(self, jail: Jail) -> None:
        provider, root, _ = jail
        for suffix in (".", " ", "..."):
            uri = _uri(root) + "/hello.txt" + suffix.replace(" ", "%20")
            assert await _denied(provider.read(uri)) == -32009, suffix

    async def test_an_alternate_data_stream_is_refused(self, jail: Jail) -> None:
        provider, root, _ = jail
        with open(str(root / "hello.txt") + ":hidden", "wb") as stream:
            stream.write(b"STREAM\n")
        for name in ("hello.txt:hidden", "hello.txt:hidden:$DATA", "hello.txt::$DATA"):
            assert await _denied(provider.read(f"{_uri(root)}/{name}")) == -32009, name
        names = [e.name for e in await provider.list_dir(_uri(root))]
        assert not any(":" in name for name in names)

    @pytest.mark.parametrize("name", ["CON", "nul", "NUL.txt", "COM1", "lpt1.log", "CONIN$"])
    async def test_device_names_are_refused(self, jail: Jail, name: str) -> None:
        provider, root, _ = jail
        assert await _denied(provider.read(f"{_uri(root)}/{name}")) == -32009
        assert await _denied(provider.resolve(f"{_uri(root)}/sub/{name}")) == -32009

    async def test_the_verbatim_prefix_is_not_a_uri(self, jail: Jail) -> None:
        provider, root, _ = jail
        verbatim = "file:///%5C%5C%3F%5C" + str(root / "hello.txt").replace("\\", "%5C")
        assert await _denied(provider.read(verbatim)) == -32602

    async def test_unc_is_refused(self, jail: Jail) -> None:
        provider, _root, _ = jail
        for uri in ("file://localhost2/c$/x", "file:////127.0.0.1/c$/Windows/win.ini"):
            assert await _denied(provider.read(uri)) == -32602

    async def test_an_8_3_name_below_the_root_resolves_to_its_long_name(self, jail: Jail) -> None:
        provider, root, _ = jail
        long_dir = root / "a rather long directory name"
        long_dir.mkdir()
        (long_dir / "f.txt").write_bytes(b"long\n")
        # Only the component below the root: GetShortPathNameW also shortens
        # the root's own ancestors where they have 8.3 names (a CI runner's
        # user profile does), and an 8.3 spelling of the root is refused by
        # design -- see the next test.
        short = root / Path(_short_name(long_dir)).name
        if short == long_dir:
            pytest.skip("8.3 name generation is disabled on this volume")
        short_uri = short.as_uri() + "/f.txt"
        assert (await provider.read(short_uri)).data == b"long\n"
        # The policy is shown the canonical, long spelling -- never the alias.
        assert (await provider.resolve(short_uri)).uri == _uri(long_dir / "f.txt")

    async def test_an_8_3_spelling_of_the_root_is_refused(self, base: Path) -> None:
        root = base / "a rather long root name"
        root.mkdir()
        (root / "f.txt").write_bytes(b"x")
        short = _short_name(root)
        if short == str(root):
            pytest.skip("8.3 name generation is disabled on this volume")
        provider = RootedFilesystemResourceProvider(root)
        assert await _denied(provider.read(Path(short).as_uri() + "/f.txt")) == -32009


class TestEscapes:
    async def test_dot_dot_is_refused(self, jail: Jail) -> None:
        provider, root, _ = jail
        assert await _denied(provider.read(f"{_uri(root)}/../outside/secret.txt")) == -32009
        assert await _denied(provider.read(f"{_uri(root)}/sub%5C..%5C..%5Coutside")) == -32009

    async def test_an_absolute_path_outside_the_root_is_refused(self, jail: Jail) -> None:
        provider, _root, outside = jail
        assert await _denied(provider.read(_uri(outside / "secret.txt"))) == -32009

    async def test_a_sibling_sharing_the_roots_prefix_is_outside(self, jail: Jail) -> None:
        provider, root, _ = jail
        sibling = root.parent / (root.name + "2")
        sibling.mkdir()
        (sibling / "x.txt").write_bytes(b"x")
        assert await _denied(provider.read(_uri(sibling / "x.txt"))) == -32009

    async def test_a_junction_out_of_the_root_is_refused(self, jail: Jail) -> None:
        """The escape that needs no privilege at all."""
        provider, root, outside = jail
        _junction(outside, root / "door")
        target = _uri(root / "door" / "secret.txt")
        assert await _denied(provider.read(target)) == -32009
        assert await _denied(provider.resolve(target)) == -32009
        assert await _denied(provider.list_dir(_uri(root / "door"))) == -32009

    async def test_a_junction_is_listed_without_being_followed(self, jail: Jail) -> None:
        provider, root, outside = jail
        _junction(outside, root / "door")
        entries = {e.name: e.type for e in await provider.list_dir(_uri(root))}
        assert entries["door"] == "directory"

    async def test_a_junction_within_the_root_is_followed(self, jail: Jail) -> None:
        provider, root, _ = jail
        _junction(root / "sub", root / "alias")
        uri = _uri(root / "alias" / "nested.txt")
        assert (await provider.read(uri)).data == b"nested\n"
        # Canonicalised to where it actually lives, for the policy's benefit.
        assert (await provider.resolve(uri)).uri == _uri(root / "sub" / "nested.txt")

    async def test_follow_symlinks_false_refuses_even_an_inside_junction(self, jail: Jail) -> None:
        _provider, root, _ = jail
        provider = RootedFilesystemResourceProvider(root, follow_symlinks=False)
        _junction(root / "sub", root / "alias")
        assert await _denied(provider.read(_uri(root / "alias" / "nested.txt"))) == -32009

    async def test_resolve_without_following_describes_the_link(self, jail: Jail) -> None:
        provider, root, outside = jail
        _junction(outside, root / "door")
        info = await provider.resolve(_uri(root / "door"), follow_symlinks=False)
        assert (info.type, info.uri) == ("symlink", _uri(root / "door"))

    async def test_a_symlink_out_of_the_root_is_refused(self, jail: Jail) -> None:
        provider, root, outside = jail
        _symlink(outside / "secret.txt", root / "escape")
        assert await _denied(provider.read(_uri(root / "escape"))) == -32009

    async def test_a_symlinked_directory_out_of_the_root_is_refused(self, jail: Jail) -> None:
        provider, root, outside = jail
        _symlink(outside, root / "door", directory=True)
        assert await _denied(provider.read(_uri(root / "door" / "secret.txt"))) == -32009

    async def test_dot_dot_inside_a_link_target_is_refused(self, jail: Jail) -> None:
        provider, root, _ = jail
        _symlink("..\\..\\outside\\secret.txt", root / "sub" / "up")
        assert await _denied(provider.read(_uri(root / "sub" / "up"))) == -32009

    async def test_a_symlink_within_the_root_still_works(self, jail: Jail) -> None:
        provider, root, _ = jail
        _symlink(root / "hello.txt", root / "alias")
        assert (await provider.read(_uri(root / "alias"))).data == b"inside\n"

    async def test_a_relative_symlink_within_the_root_still_works(self, jail: Jail) -> None:
        provider, root, _ = jail
        _symlink("..\\hello.txt", root / "sub" / "back")
        assert (await provider.read(_uri(root / "sub" / "back"))).data == b"inside\n"

    async def test_a_link_loop_terminates(self, jail: Jail) -> None:
        provider, root, _ = jail
        (root / "b").mkdir()
        _junction(root / "b", root / "a")
        (root / "b").rmdir()
        _junction(root / "a", root / "b")  # a -> b -> a
        assert await _denied(provider.read(_uri(root / "a" / "x"))) in (-32008, -32009)

    async def test_the_root_replaced_by_a_junction_is_refused(self, jail: Jail) -> None:
        """Checked by identity, not by name: the path still says the same thing."""
        provider, root, outside = jail
        (outside / "hello.txt").write_bytes(b"SECRET\n")
        root.rename(root.parent / "parked")
        _junction(outside, root)
        assert await _denied(provider.read(_uri(root / "hello.txt"))) == -32009


class TestTheSwapRace:
    async def test_a_component_swapped_after_the_check_cannot_escape(self, jail: Jail) -> None:
        """The race that beats `realpath`-then-open, played out once."""
        provider, root, outside = jail
        (outside / "target.txt").write_bytes(b"SECRET\n")
        swappable = root / "swap"
        swappable.mkdir()
        (swappable / "target.txt").write_bytes(b"innocent\n")
        uri = _uri(swappable / "target.txt")
        assert (await provider.read(uri)).data == b"innocent\n"

        swappable.rename(root / "moved")
        _junction(outside, swappable)

        assert await _denied(provider.read(uri)) == -32009

    def test_a_held_component_cannot_be_renamed(self, jail: Jail) -> None:
        """Why the window cannot be reopened mid-walk: no FILE_SHARE_DELETE."""
        provider, root, _ = jail
        walk: Any = getattr(provider, "_win_walk")  # noqa: B009 - reaching into the walk on purpose
        handle, _stats, _resolved = walk(["sub"])
        try:
            with pytest.raises(PermissionError):
                (root / "sub").rename(root / "elsewhere")
        finally:
            getattr(provider, "_w").close(handle)  # noqa: B009
        (root / "sub").rename(root / "elsewhere")  # and once released, it can

    async def test_a_racing_swapper_never_wins(self, jail: Jail) -> None:
        """A thread flips a directory between real and a junction out of the
        root as fast as it can. Whatever interleaving happens, no read may ever
        return the secret -- a refusal is fine, a leak is not."""
        provider, root, outside = jail
        (outside / "target.txt").write_bytes(b"SECRET\n")
        swappable = root / "swap"
        parked = root / "parked"
        swappable.mkdir()
        (swappable / "target.txt").write_bytes(b"innocent\n")
        uri = _uri(swappable / "target.txt")
        stop = threading.Event()

        def flip() -> None:
            while not stop.is_set():
                try:
                    swappable.rename(parked)
                    _junction(outside, swappable)
                    os.rmdir(swappable)  # removes the junction, never the target
                    parked.rename(swappable)
                except OSError:
                    continue

        flipper = threading.Thread(target=flip, daemon=True)
        flipper.start()
        seen: set[bytes] = set()
        try:
            for _ in range(400):
                try:
                    seen.add((await provider.read(uri)).data)
                except AhpError:
                    seen.add(b"<refused>")
        finally:
            stop.set()
            flipper.join(timeout=10)
        assert b"SECRET\n" not in seen, seen


class TestAncestorChain:
    """As on POSIX: ancestors resolve, and do nothing else."""

    @pytest.fixture
    def rooted(self, base: Path) -> RootedFilesystemResourceProvider:
        root = base / "outer" / "inner" / "served"
        root.mkdir(parents=True)
        (root / "kept.txt").write_bytes(b"in the jail")
        (base / "outer" / "secret.txt").write_bytes(b"NOT in the jail")
        (base / "outer" / "sibling").mkdir()
        return RootedFilesystemResourceProvider(root)

    async def test_a_strict_ancestor_resolves_bare(
        self, rooted: RootedFilesystemResourceProvider, base: Path
    ) -> None:
        info = await rooted.resolve((base / "outer").as_uri())
        assert info.to_wire() == {"uri": (base / "outer").as_uri(), "type": "directory"}

    async def test_the_drive_root_is_an_ancestor(
        self, rooted: RootedFilesystemResourceProvider, base: Path
    ) -> None:
        drive = base.drive
        assert (await rooted.resolve(f"file:///{drive}/")).type == "directory"

    async def test_an_ancestor_in_another_case_answers_in_the_roots_spelling(
        self, rooted: RootedFilesystemResourceProvider, base: Path
    ) -> None:
        info = await rooted.resolve((base / "OUTER").as_uri())
        assert info.uri == (base / "outer").as_uri()

    async def test_listing_an_ancestor_reveals_only_the_way_down(
        self, rooted: RootedFilesystemResourceProvider, base: Path
    ) -> None:
        entries = await rooted.list_dir((base / "outer").as_uri())
        assert [(e.name, e.type) for e in entries] == [("inner", "directory")]

    async def test_an_ancestor_is_still_unreadable(
        self, rooted: RootedFilesystemResourceProvider, base: Path
    ) -> None:
        assert await _denied(rooted.read((base / "outer" / "secret.txt").as_uri())) == -32009
        for target in ("outer/sibling", "outer/secret.txt"):
            assert await _denied(rooted.resolve((base / target).as_uri())) == -32009

    async def test_the_root_itself_is_real(self, rooted: RootedFilesystemResourceProvider) -> None:
        entries = await rooted.list_dir(rooted.root.as_uri())
        assert [e.name for e in entries] == ["kept.txt"]
