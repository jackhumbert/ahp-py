"""Settings: a TOML config file, with command-line flags taking precedence.

    # ~/.config/agent-host/openclaw.toml
    agent_name = "OpenClaw"
    provider_id = "openclaw"
    port = 4322
    token_file = "~/.config/agent-host/openclaw.token"
    command = ["openclaw", "acp"]
    model_command = "/model {model} -s"

    [env]
    OPENCLAW_HIDE_BANNER = "1"

    [config_options]              # ACP session config options, set on each session
    thought_level = "low"

    [[models]]
    id = "ollama/glm-5.3-flash:cloud"
    name = "GLM 5.3 Flash"
    context_window = 1048576

    [roots]
    llm = 'G:\\llm'

The first model is each new session's default. Use single-quoted (literal)
TOML strings for Windows paths so backslashes are kept as written. `~` is
expanded everywhere a path is expected.
"""

from __future__ import annotations

import argparse
import shlex
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agent_host_server.provider.base import ModelInfo

from agent_host_server_acp.roots import Roots, parse_root_arg

DEFAULT_STATE = Path.home() / ".local/state/agent-host-server-acp"
DEFAULT_PORT = 4322
DEFAULT_BIND = "127.0.0.1"
DEFAULT_AGENT_NAME = "ACP agent"
DEFAULT_PROVIDER_ID = "acp"

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
        "description",
        "command",
        "env",
        "models",
        "model_command",
        "config_options",
        "verbose",
    }
)
_MODEL_KEYS = frozenset({"id", "name", "context_window", "vision"})


class ConfigError(ValueError):
    """A config file or flag the host cannot start with."""


@dataclass(frozen=True)
class Settings:
    roots: Roots
    command: tuple[str, ...]
    port: int = DEFAULT_PORT
    bind: str = DEFAULT_BIND
    token_file: Path | None = None
    state_dir: Path = DEFAULT_STATE
    agent_name: str = DEFAULT_AGENT_NAME
    provider_id: str = DEFAULT_PROVIDER_ID
    description: str | None = None
    env: Mapping[str, str] = field(default_factory=dict)
    models: tuple[ModelInfo, ...] = ()
    model_command: str | None = None
    config_options: Mapping[str, str] = field(default_factory=dict)
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


def _command(flag: str | None, data: Mapping[str, Any]) -> tuple[str, ...]:
    command: Any = shlex.split(flag) if flag is not None else data.get("command")
    if isinstance(command, str):
        command = shlex.split(command)
    if (
        not isinstance(command, list)
        or not command
        or not all(isinstance(part, str) and part for part in command)
    ):
        raise ConfigError('no agent to run: give --command, or command = ["prog", "arg"]')
    return tuple(command)


def _strings(data: Mapping[str, Any], key: str) -> dict[str, str]:
    table = data.get(key, {})
    if not isinstance(table, Mapping) or not all(isinstance(v, str) for v in table.values()):
        raise ConfigError(f'[{key}] must be a table of name = "value" strings')
    return dict(table)


def _models(flags: list[str] | None, data: Mapping[str, Any]) -> tuple[ModelInfo, ...]:
    if flags:
        return tuple(ModelInfo(id=model, name=model) for model in flags)
    entries = data.get("models", [])
    if not isinstance(entries, list):
        raise ConfigError("models must be an array of tables: [[models]]")
    models = []
    for index, entry in enumerate(entries):
        where = f"models[{index}]"
        if not isinstance(entry, Mapping):
            raise ConfigError(f"{where} must be a table")
        unknown = sorted(set(entry) - _MODEL_KEYS)
        if unknown:
            raise ConfigError(f"{where}: unknown setting(s): {', '.join(unknown)}")
        model_id = entry.get("id")
        if not isinstance(model_id, str) or not model_id:
            raise ConfigError(f"{where} needs an id")
        window = entry.get("context_window")
        if window is not None and (not isinstance(window, int) or isinstance(window, bool)):
            raise ConfigError(f"{where}.context_window must be a number")
        vision = entry.get("vision")
        if vision is not None and not isinstance(vision, bool):
            raise ConfigError(f"{where}.vision must be true or false")
        models.append(
            ModelInfo(
                id=model_id,
                name=str(entry.get("name", model_id)),
                max_context_window=window,
                max_prompt_tokens=window,
                supports_vision=vision,
            )
        )
    ids = [model.id for model in models]
    if len(set(ids)) != len(ids):
        raise ConfigError("models: duplicate ids")
    return tuple(models)


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
    port = pick(args.port, "port", DEFAULT_PORT)
    if not isinstance(port, int) or isinstance(port, bool):
        raise ConfigError("port must be a number")
    model_command = pick(args.model_command, "model_command", None)
    if model_command is not None and (
        not isinstance(model_command, str) or "{model}" not in model_command
    ):
        raise ConfigError("model_command must be a string containing {model}")
    description = pick(None, "description", None)
    return Settings(
        roots=roots,
        command=_command(args.command, data),
        port=port,
        bind=str(pick(args.bind, "bind", DEFAULT_BIND)),
        token_file=token_file,
        state_dir=state_dir.expanduser(),
        agent_name=str(pick(args.agent_name, "agent_name", DEFAULT_AGENT_NAME)),
        provider_id=str(pick(args.provider_id, "provider_id", DEFAULT_PROVIDER_ID)),
        description=str(description) if description is not None else None,
        env=_strings(data, "env"),
        config_options=_strings(data, "config_options"),
        models=_models(args.model, data),
        model_command=model_command,
        verbose=bool(args.verbose or data.get("verbose", False)),
    )
