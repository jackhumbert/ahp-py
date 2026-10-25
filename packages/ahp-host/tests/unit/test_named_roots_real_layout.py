"""Named roots served at their real paths: the sparse real tree."""

from __future__ import annotations

from pathlib import Path

import pytest
from ahp_protocol import errors

from ahp_host.node.roots import NamedRootsResourceProvider, Roots

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def tree(tmp_path: Path) -> tuple[Path, Path, Path]:
    base = tmp_path.resolve() / "x"
    proj = base / "home" / "dev" / "proj"
    data = base / "srv" / "data"
    for folder in (proj / "src", data, base / "home" / "decoy", base / "home" / "dev" / "decoy"):
        folder.mkdir(parents=True)
    (proj / "src" / "a.txt").write_text("hello")
    (base / "home" / "dev" / "secret.txt").write_text("nope")
    return base, proj, data


def _provider(proj: Path, data: Path, layout: str = "real") -> NamedRootsResourceProvider:
    return NamedRootsResourceProvider(Roots.named({"proj": proj, "data": data}, layout))


def test_default_directory_is_the_primary_real_uri(tree: tuple[Path, Path, Path]) -> None:
    _, proj, data = tree
    roots = Roots.named({"proj": proj, "data": data}, "real")
    assert roots.default_directory() == proj.as_uri()
    assert Roots.named({"proj": proj}).default_directory() == "file:///"


def test_an_unknown_layout_is_refused(tree: tuple[Path, Path, Path]) -> None:
    with pytest.raises(ValueError, match="layout"):
        Roots.named({"proj": tree[1]}, "sideways")


async def test_the_top_lists_only_the_spine(tree: tuple[Path, Path, Path]) -> None:
    base, proj, data = tree
    provider = _provider(proj, data)
    top = [e.name for e in await provider.list_dir("file:///")]
    assert top == sorted({p.parts[1] for p in (proj, data)})
    assert all(e.type == "directory" for e in await provider.list_dir("file:///"))
    assert [e.name for e in await provider.list_dir(base.as_uri())] == ["home", "srv"]
    assert [e.name for e in await provider.list_dir((base / "home").as_uri())] == ["dev"]


async def test_a_spine_listing_never_shows_siblings(tree: tuple[Path, Path, Path]) -> None:
    base, proj, data = tree
    provider = _provider(proj, data)
    dev = [e.name for e in await provider.list_dir((base / "home" / "dev").as_uri())]
    assert dev == ["proj"]
    home = [e.name for e in await provider.list_dir((base / "home").as_uri())]
    assert "decoy" not in home


async def test_the_spine_resolves_as_a_directory(tree: tuple[Path, Path, Path]) -> None:
    base, proj, data = tree
    provider = _provider(proj, data)
    info = await provider.resolve((base / "home").as_uri())
    assert info.type == "directory"
    assert info.uri == (base / "home").as_uri()
    assert (await provider.resolve("file:///")).uri == "file:///"


async def test_reading_the_spine_or_outside_is_refused(tree: tuple[Path, Path, Path]) -> None:
    base, proj, data = tree
    provider = _provider(proj, data)
    for target in (
        base / "home" / "dev" / "secret.txt",
        base / "home" / "decoy",
        base / "elsewhere.txt",
    ):
        with pytest.raises(errors.AhpError):
            await provider.read(target.as_uri())
    with pytest.raises(errors.AhpError):
        await provider.read((base / "home").as_uri())
    with pytest.raises(errors.AhpError):
        await provider.list_dir((base / "home" / "decoy").as_uri())
    assert not provider.serves((base / "home").as_uri())
    assert not provider.serves("file:///")
    assert not provider.serves((base / "home" / "dev" / "x" / ".." / "secret.txt").as_uri())


async def test_reading_inside_a_root_works(tree: tuple[Path, Path, Path]) -> None:
    _, proj, data = tree
    provider = _provider(proj, data)
    uri = (proj / "src" / "a.txt").as_uri()
    assert provider.serves(uri)
    assert (await provider.read(uri)).data == b"hello"
    assert [e.name for e in await provider.list_dir(proj.as_uri())] == ["src"]
    assert (await provider.resolve(uri)).uri == uri
    assert provider.serves(data.as_uri())


async def test_the_old_tree_spelling_still_resolves(tree: tuple[Path, Path, Path]) -> None:
    _, proj, data = tree
    provider = _provider(proj, data)
    assert (await provider.read("file:///proj/src/a.txt")).data == b"hello"
    assert provider.serves("file:///data")
    assert provider.roots.real_path("file:///proj/src/a.txt") == proj / "src" / "a.txt"


async def test_a_real_path_wins_over_a_same_named_first_segment(tmp_path: Path) -> None:
    base = tmp_path.resolve()
    shadow = base / "shadow"
    other = base / "other"
    (shadow / "inner").mkdir(parents=True)
    other.mkdir()
    (other / "f.txt").write_text("other")
    # A root named like the first segment of a real path to another root.
    first = base.parts[1]
    provider = NamedRootsResourceProvider(Roots.named({first: shadow, "o": other}, "real"))
    assert (await provider.read((other / "f.txt").as_uri())).data == b"other"
    assert [e.name for e in await provider.list_dir(base.as_uri())] == ["other", "shadow"]


async def test_the_tree_layout_is_unchanged(tree: tuple[Path, Path, Path]) -> None:
    base, proj, data = tree
    provider = _provider(proj, data, "tree")
    assert [e.name for e in await provider.list_dir("file:///")] == ["proj", "data"]
    assert (await provider.read("file:///proj/src/a.txt")).data == b"hello"
    assert not provider.serves(base.as_uri())
    assert provider.roots.spine_children(base.as_uri()) is None


@pytest.mark.skipif(not Path("/tmp").is_dir(), reason="needs a real top-level /tmp")
async def test_a_real_path_outside_every_root_does_not_resolve_as_a_tree_spelling(
    tmp_path: Path,
) -> None:
    base = tmp_path.resolve() / "x"
    srvdata = base / "srvdata"
    proj = base / "home" / "dev" / "proj"
    (srvdata).mkdir(parents=True)
    proj.mkdir(parents=True)
    (srvdata / "other").write_text("served")
    provider = NamedRootsResourceProvider(Roots.named({"tmp": srvdata, "proj": proj}, "real"))
    # `/tmp` is a real directory here: this is that path, not the root named `tmp`.
    assert provider.roots.real_path("file:///tmp/other") is None
    assert not provider.serves("file:///tmp/other")
    with pytest.raises(errors.AhpError):
        await provider.read("file:///tmp/other")
    # The root itself is still reachable at its real URI.
    assert (await provider.read((srvdata / "other").as_uri())).data == b"served"


def test_layout_of_prefers_the_flag_then_the_file_then_tree() -> None:
    from ahp_host.node.roots import layout_of

    assert layout_of("real", "tree") == "real"
    assert layout_of(None, "real") == "real"
    assert layout_of(None) == "tree"
    for bad in ("flat", 3, None):
        if bad is not None:
            with pytest.raises(ValueError, match="roots_layout"):
                layout_of(None, bad)
