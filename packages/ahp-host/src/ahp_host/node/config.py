"""A node's settings: one TOML file for the machine, with an `[[agents]]` list.

    # ~/.config/ahp/node.toml
    token_file = "~/.config/ahp/node.token"
    port = 4321
    log_file = "~/.local/state/ahp-node/node.log"   # rotated; default stderr
    # kept running beside the node by `ahp-node supervise` (optional)
    tunnel = ["ssh", "-N", "-R", "127.0.0.1:4402:127.0.0.1:4321", "ahp-tunnel@gateway.example"]

    [roots]                       # named folders: file:///llm/..., file:///projects/...
    llm = 'G:\\llm'
    projects = 'C:\\Users\\me\\projects'
    # or, instead of [roots], one unnamed folder served as itself:
    # root = "~/Github"

    [[agents]]
    type = "claude"               # which installed agent package serves it
    [[agents]]
    type = "acp"
    provider_id = "opencode"
    command = ["opencode", "acp"]

Everything in an `[[agents]]` table except `type` is that agent's own, handed
to its package as given; the node checks only that `provider_id`s differ. The
first agent is the default: it serves a new session that names no agent, and a
restored session whose agent has since been renamed.

Use single-quoted (literal) TOML strings for Windows paths so backslashes are
kept as written. `~` is expanded everywhere a path is expected.
"""

from __future__ import annotations

import argparse
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ahp_host.node.roots import Roots, parse_root_arg

DEFAULT_STATE = Path.home() / ".local/state/ahp-node"
DEFAULT_PORT = 4321
DEFAULT_BIND = "127.0.0.1"

_KEYS = frozenset(
    {
        "root",
        "roots",
        "port",
        "bind",
        "token_file",
        "state_dir",
        "log_file",
        "tunnel",
        "verbose",
        "agents",
    }
)


class ConfigError(ValueError):
    """A config file or flag the node cannot start with."""


@dataclass(frozen=True)
class AgentSpec:
    """One `[[agents]]` table: the package that serves it, and its options."""

    type: str
    options: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class NodeSettings:
    roots: Roots
    agents: tuple[AgentSpec, ...]
    port: int = DEFAULT_PORT
    bind: str = DEFAULT_BIND
    token_file: Path | None = None
    state_dir: Path = DEFAULT_STATE
    #: Where the node logs, rotated; None logs to stderr.
    log_file: Path | None = None
    #: A command `supervise` keeps running beside the node (an `ssh -N -R` to
    #: a gateway, say); empty for none.
    tunnel: tuple[str, ...] = ()
    verbose: bool = False


def _path(value: Any, key: str) -> Path:
    if not isinstance(value, str) or not value:
        raise ConfigError(f"{key} must be a path string")
    return Path(value).expanduser()


def _named(roots: Mapping[str, Path]) -> Roots:
    try:
        return Roots.named({name: path.expanduser() for name, path in roots.items()})
    except ValueError as exc:
        raise ConfigError(str(exc)) from exc


def roots_from_flags(values: list[str]) -> Roots:
    parsed = [parse_root_arg(value) for value in values]
    named = [(name, path) for name, path in parsed if name is not None]
    if not named:
        if len(parsed) > 1:
            raise ConfigError("several --root flags need names: --root NAME=PATH")
        return Roots.single(parsed[0][1].expanduser())
    if len(named) != len(parsed):
        raise ConfigError("mix of named and unnamed --root flags: name them all")
    return _named(dict(named))


def roots_from_file(data: Mapping[str, Any]) -> Roots | None:
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


def read_toml(path: Path) -> dict[str, Any]:
    config_path = path.expanduser()
    try:
        # utf-8-sig: Notepad and Windows PowerShell write a byte-order mark.
        return tomllib.loads(config_path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError as exc:
        raise ConfigError(f"no config file at {config_path}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{config_path}: {exc}") from exc


def _agents(data: Mapping[str, Any]) -> tuple[AgentSpec, ...]:
    tables = data.get("agents")
    if not isinstance(tables, list) or not tables:
        raise ConfigError("no agents: add at least one [[agents]] table with a type")
    specs: list[AgentSpec] = []
    seen: set[str] = set()
    for index, table in enumerate(tables):
        if not isinstance(table, Mapping):
            raise ConfigError(f"agents[{index}] must be a table")
        kind = table.get("type")
        if not isinstance(kind, str) or not kind:
            raise ConfigError(f"agents[{index}] needs a type (the agent package, e.g. 'claude')")
        options = {key: value for key, value in table.items() if key != "type"}
        provider_id = options.get("provider_id", kind)
        if provider_id in seen:
            raise ConfigError(f"two agents share the provider_id {provider_id!r}")
        seen.add(str(provider_id))
        specs.append(AgentSpec(kind, options))
    return tuple(specs)


def load(args: argparse.Namespace) -> NodeSettings:
    """Merge the config file and the flags; flags win."""
    data = read_toml(Path(args.config))
    unknown = sorted(set(data) - _KEYS)
    if unknown:
        raise ConfigError(f"{args.config}: unknown setting(s): {', '.join(unknown)}")

    roots = roots_from_flags(args.root) if args.root else roots_from_file(data)
    if roots is None:
        raise ConfigError("no folder to serve: give --root, or root / [roots] in the config")
    for path in roots.paths:
        if not path.is_dir():
            raise ConfigError(f"{path} is not a directory")

    token_file = args.token_file or (
        _path(data["token_file"], "token_file") if "token_file" in data else None
    )
    state_dir = args.state_dir or (
        _path(data["state_dir"], "state_dir") if "state_dir" in data else DEFAULT_STATE
    )
    log_file = getattr(args, "log_file", None) or (
        _path(data["log_file"], "log_file") if "log_file" in data else None
    )
    tunnel = data.get("tunnel", [])
    if not isinstance(tunnel, list) or not all(isinstance(part, str) for part in tunnel):
        raise ConfigError("tunnel must be a command: a list of strings")
    port = args.port if args.port is not None else data.get("port", DEFAULT_PORT)
    if not isinstance(port, int) or isinstance(port, bool):
        raise ConfigError("port must be a number")
    return NodeSettings(
        roots=roots,
        agents=_agents(data),
        port=port,
        bind=str(args.bind if args.bind is not None else data.get("bind", DEFAULT_BIND)),
        token_file=token_file,
        state_dir=state_dir.expanduser(),
        log_file=log_file.expanduser() if log_file else None,
        tunnel=tuple(tunnel),
        verbose=bool(args.verbose or data.get("verbose", False)),
    )
