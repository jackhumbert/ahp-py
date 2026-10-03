"""Settings: a TOML config file, with command-line flags taking precedence.

    # ~/.config/ahp/node.toml
    agent_name = "Claude"
    token_file = "~/.config/ahp/node.token"

    [roots]                       # named folders: file:///llm/..., file:///projects/...
    llm = 'G:\\llm'
    projects = 'C:\\Users\\me\\projects'

    # or, instead of [roots], one unnamed folder served as itself:
    # root = "~/Github"

Use single-quoted (literal) TOML strings for Windows paths so backslashes are
kept as written. `~` is expanded everywhere a path is expected.
"""

from __future__ import annotations

import argparse
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ahp_host_claude.claude_ai import scope_of
from ahp_host_claude.roots import Roots, parse_root_arg

DEFAULT_STATE = Path.home() / ".local/state/ahp-host-claude"
DEFAULT_PORT = 4321
DEFAULT_BIND = "127.0.0.1"
DEFAULT_AGENT_NAME = "Claude"
DEFAULT_PROVIDER_ID = "claude"

_KEYS = frozenset(
    {
        "root",
        "roots",
        "port",
        "bind",
        "token_file",
        "state_dir",
        "agent_name",
        "provider_id",
        "remote_control",
        "claude_ai_sessions",
        "chat_tools",
        "verbose",
    }
)


class ConfigError(ValueError):
    """A config file or flag the host cannot start with."""


@dataclass(frozen=True)
class Settings:
    roots: Roots
    port: int = DEFAULT_PORT
    bind: str = DEFAULT_BIND
    token_file: Path | None = None
    state_dir: Path = DEFAULT_STATE
    agent_name: str = DEFAULT_AGENT_NAME
    provider_id: str = DEFAULT_PROVIDER_ID
    #: Put new sessions on claude.ai. None: whatever Claude Code itself does
    #: (the user's `remoteControlAtStartup`, then org policy).
    remote_control: bool | None = None
    #: Also list the account's other Remote Control sessions, through
    #: claude.ai. On one machine only: each would otherwise list them all.
    #: None (off), `"local"` (this machine's) or `"all"`: see `claude_ai.scope_of`.
    claude_ai_sessions: str | None = None
    #: What a session with no folder may use; None keeps the default
    #: (`provider.CHAT_TOOLS`). May only narrow it.
    chat_tools: tuple[str, ...] | None = None
    verbose: bool = False


def _path(value: Any, key: str) -> Path:
    if not isinstance(value, str) or not value:
        raise ConfigError(f"{key} must be a path string")
    return Path(value).expanduser()


def _roots_from_flags(values: list[str]) -> Roots:
    parsed = [parse_root_arg(value) for value in values]
    named = [(name, path) for name, path in parsed if name is not None]
    if not named:
        if len(parsed) > 1:
            raise ConfigError("several --root flags need names: --root NAME=PATH")
        return Roots.single(parsed[0][1].expanduser())
    if len(named) != len(parsed):
        raise ConfigError("mix of named and unnamed --root flags: name them all")
    return _named(dict(named))


def _named(roots: Mapping[str, Path]) -> Roots:
    try:
        return Roots.named({name: path.expanduser() for name, path in roots.items()})
    except ValueError as exc:
        raise ConfigError(str(exc)) from exc


def _roots_from_file(data: Mapping[str, Any]) -> Roots | None:
    if "root" in data and "roots" in data:
        raise ConfigError("use either root or [roots], not both")
    if "root" in data:
        return Roots.single(_path(data["root"], "root"))
    if "roots" in data:
        table = data["roots"]
        if not isinstance(table, Mapping) or not table:
            raise ConfigError("[roots] must be a table of name = path")
        return _named({name: _path(path, f"roots.{name}") for name, path in table.items()})
    return None


def _chat_tools(flag: str | None, value: Any) -> tuple[str, ...] | None:
    """`--chat-tools A,B` (empty for none) or `chat_tools = ["A"]`; the flag wins."""
    from ahp_host_claude.provider import chat_tools_subset

    if flag is not None:
        value = [name.strip() for name in flag.split(",") if name.strip()]
    elif value is None:
        return None
    elif not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ConfigError("chat_tools must be a list of tool names")
    try:
        return chat_tools_subset(value)
    except ValueError as exc:
        raise ConfigError(str(exc)) from exc


def load(args: argparse.Namespace) -> Settings:
    """Merge the config file (if any) and the flags; flags win."""
    data: dict[str, Any] = {}
    if args.config is not None:
        config_path = Path(args.config).expanduser()
        try:
            # utf-8-sig: Notepad and Windows PowerShell write a byte-order mark.
            data = tomllib.loads(config_path.read_text(encoding="utf-8-sig"))
        except FileNotFoundError as exc:
            raise ConfigError(f"no config file at {config_path}") from exc
        except tomllib.TOMLDecodeError as exc:
            raise ConfigError(f"{config_path}: {exc}") from exc
        unknown = sorted(set(data) - _KEYS)
        if unknown:
            raise ConfigError(f"{config_path}: unknown setting(s): {', '.join(unknown)}")

    roots = _roots_from_flags(args.root) if args.root else _roots_from_file(data)
    if roots is None:
        raise ConfigError("no folder to serve: give --root, or root / [roots] in a config file")
    for path in roots.paths:
        if not path.is_dir():
            raise ConfigError(f"{path} is not a directory")

    def pick(flag: Any, key: str, default: Any) -> Any:
        return flag if flag is not None else data.get(key, default)

    token_file = args.token_file or (
        _path(data["token_file"], "token_file") if "token_file" in data else None
    )
    state_dir = args.state_dir or (
        _path(data["state_dir"], "state_dir") if "state_dir" in data else DEFAULT_STATE
    )
    remote_control = pick(args.remote_control, "remote_control", None)
    if remote_control is not None and not isinstance(remote_control, bool):
        raise ConfigError("remote_control must be true or false")
    try:
        chosen = pick(args.claude_ai_sessions, "claude_ai_sessions", False)
        claude_ai_sessions = scope_of(False if chosen == "off" else chosen)
    except ValueError as exc:
        raise ConfigError(str(exc)) from exc
    chat_tools = _chat_tools(args.chat_tools, data.get("chat_tools"))
    port = pick(args.port, "port", DEFAULT_PORT)
    if not isinstance(port, int) or isinstance(port, bool):
        raise ConfigError("port must be a number")
    return Settings(
        roots=roots,
        port=port,
        bind=str(pick(args.bind, "bind", DEFAULT_BIND)),
        token_file=token_file,
        state_dir=state_dir.expanduser(),
        agent_name=str(pick(args.agent_name, "agent_name", DEFAULT_AGENT_NAME)),
        provider_id=str(pick(args.provider_id, "provider_id", DEFAULT_PROVIDER_ID)),
        remote_control=remote_control,
        claude_ai_sessions=claude_ai_sessions,
        chat_tools=chat_tools,
        verbose=bool(args.verbose or data.get("verbose", False)),
    )
