"""Named folders (a small tree of jailed roots) and the config file."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
from agent_host_protocol.errors import AhpError
from agent_host_server.provider.base import AgentSessionContext, UserMessage

from agent_host_server_claude.__main__ import _parse_args
from agent_host_server_claude.config import ConfigError, load
from agent_host_server_claude.provider import ClaudeProvider
from agent_host_server_claude.roots import (
    NamedRootsResourceProvider,
    Roots,
    parse_root_arg,
)
from tests.fakes import FakeClient, RecordingSink

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="POSIX jail in these tests")


@pytest.fixture
def two_roots(tmp_path: Path) -> Roots:
    (tmp_path / "llm" / "model").mkdir(parents=True)
    (tmp_path / "llm" / "model" / "weights.txt").write_text("w")
    (tmp_path / "work" / "app").mkdir(parents=True)
    (tmp_path / "secret").mkdir()
    (tmp_path / "secret" / "key").write_text("k")
    return Roots.named({"llm": tmp_path / "llm", "work": tmp_path / "work"})


def test_a_named_tree_maps_to_real_paths(two_roots: Roots) -> None:
    llm, work = two_roots.paths
    assert two_roots.default_directory() == "file:///"
    assert two_roots.real_path("file:///llm/model") == llm / "model"
    assert two_roots.real_path("file:///work") == work
    # The top of the tree is where a folderless session starts.
    assert two_roots.real_path("file:///") == llm
    assert two_roots.real_path("file:///nope/x") is None
    assert two_roots.real_path("file:///llm/../secret") is None
    assert two_roots.tree_uri(llm / "model") == "file:///llm/model"
    assert two_roots.tree_uri(work) == "file:///work"
    assert two_roots.tree_uri(llm.parent / "secret") is None


def test_an_unnamed_root_is_served_as_itself(tmp_path: Path) -> None:
    roots = Roots.single(tmp_path)
    assert roots.default_directory() == tmp_path.resolve().as_uri()
    assert roots.real_path((tmp_path / "x").as_uri()) == (tmp_path / "x").resolve()
    assert roots.real_path(tmp_path.parent.as_uri()) is None


@posix_only
async def test_the_tree_lists_names_then_each_jail(two_roots: Roots) -> None:
    provider = NamedRootsResourceProvider(two_roots)
    top = await provider.list_dir("file:///")
    assert [(e.name, e.type) for e in top] == [("llm", "directory"), ("work", "directory")]
    assert [e.name for e in await provider.list_dir("file:///llm/model")] == ["weights.txt"]
    assert (await provider.read("file:///llm/model/weights.txt")).data == b"w"
    info = await provider.resolve("file:///llm/model")
    assert info.uri == "file:///llm/model"
    assert (await provider.resolve("file:///")).type == "directory"
    # The provider never answers with a real path, and never outside a root.
    with pytest.raises(AhpError):
        await provider.list_dir("file:///llm/../secret")
    with pytest.raises(AhpError):
        await provider.read("file:///")
    assert provider.serves("file:///work/app")
    assert not provider.serves("file:///elsewhere")
    assert not hasattr(provider, "root")  # the host must ask `serves`, not compare


@posix_only
async def test_a_symlink_out_of_a_root_is_refused(two_roots: Roots) -> None:
    llm = two_roots.paths[0]
    os.symlink(llm.parent / "secret", llm / "escape")
    provider = NamedRootsResourceProvider(two_roots)
    with pytest.raises(AhpError):
        await provider.read("file:///llm/escape/key")
    assert two_roots.real_path("file:///llm/escape") is None


async def test_a_session_starts_in_the_named_folder_it_was_given(two_roots: Roots) -> None:
    from claude_agent_sdk import ClaudeAgentOptions, ResultMessage

    result = ResultMessage(
        subtype="success",
        duration_ms=1,
        duration_api_ms=1,
        is_error=False,
        num_turns=1,
        session_id="s",
    )
    clients: list[FakeClient] = []

    def factory(options: ClaudeAgentOptions) -> FakeClient:
        clients.append(FakeClient(options, [[result]]))
        return clients[-1]

    provider = ClaudeProvider(two_roots, client_factory=factory)
    session = await provider.create_session(
        AgentSessionContext(
            session_uri="s",
            chat_uri="c",
            provider_id="claude",
            working_directories=("file:///work/app",),
        )
    )
    await session.send_user_message(UserMessage(text="x"), RecordingSink())
    assert clients[0].options.cwd == str(two_roots.paths[1] / "app")


async def test_a_folder_outside_every_root_is_refused(two_roots: Roots) -> None:
    provider = ClaudeProvider(two_roots)
    session = await provider.create_session(
        AgentSessionContext(
            session_uri="s",
            chat_uri="c",
            provider_id="claude",
            working_directories=((two_roots.paths[0].parent / "secret").as_uri(),),
        )
    )
    with pytest.raises(PermissionError):
        session.working_directory()


def test_root_flags() -> None:
    assert parse_root_arg("llm=D:/work") == ("llm", Path("D:/work"))
    assert parse_root_arg("/Users/me/Github") == (None, Path("/Users/me/Github"))
    # A drive letter is a path, not a name.
    assert parse_root_arg("C:=x") == (None, Path("C:=x"))


def _write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


def test_config_file_with_named_roots(two_roots: Roots, tmp_path: Path) -> None:
    llm, work = two_roots.paths
    config = _write(
        tmp_path / "node.toml",
        f"""
agent_name = "Claude"
token_file = "~/.config/agent-host/node.token"
port = 4400

[roots]
llm = '{llm}'
work = '{work}'
""",
    )
    settings = load(_parse_args(["--config", str(config)]))
    assert settings.roots.names == ("llm", "work")
    assert settings.roots.paths == (llm, work)
    assert settings.port == 4400
    assert settings.token_file == Path("~/.config/agent-host/node.token").expanduser()
    assert settings.agent_name == "Claude"


def test_flags_override_the_file(two_roots: Roots, tmp_path: Path) -> None:
    llm, _ = two_roots.paths
    config = _write(tmp_path / "node.toml", f"port = 4400\nroot = '{llm}'\n")
    settings = load(_parse_args(["--config", str(config), "--port", "5000"]))
    assert settings.port == 5000
    assert not settings.roots.is_named
    overridden = load(_parse_args(["--config", str(config), "--root", f"only={llm}"]))
    assert overridden.roots.names == ("only",)


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("prot = 1\nroot = '.'", "unknown setting"),
        ("root = '.'\n[roots]\na = '.'", "either root or"),
        ("[roots]\n'bad name' = '.'", "root name"),
        ("port = 'x'\nroot = '.'", "port must be"),
        ("agent_name = 'x'", "no folder to serve"),
        ("root = '/definitely/not/here'", "is not a directory"),
    ],
)
def test_config_errors_say_what_is_wrong(tmp_path: Path, text: str, message: str) -> None:
    config = _write(tmp_path / "node.toml", text.replace("'.'", repr(str(tmp_path))))
    with pytest.raises(ConfigError, match=message):
        load(_parse_args(["--config", str(config)]))


def test_several_unnamed_roots_are_refused(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="need names"):
        load(_parse_args(["--root", str(tmp_path), "--root", str(tmp_path)]))
