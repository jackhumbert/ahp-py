"""Inner providers by import path, never statically (plan §2 Layering)."""

from __future__ import annotations

import importlib
import inspect
import logging
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any, Final

from ahp_host.node import Roots
from ahp_host.provider.base import AgentProvider

from ahp_host_flow.config import ConfigError, FlowConfig, ProviderConfig

_log = logging.getLogger(__name__)

#: The name of the runs folder among named roots (a taken name gets `-2`, `-3`, ...).
RUNS_ROOT_NAME: Final = "flow-runs"

_ACCEPTED_KINDS = (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY)


def _import_factory(spec: ProviderConfig) -> Any:
    assert spec.load is not None
    module_name, _, attr_path = spec.load.partition(":")
    try:
        obj: Any = importlib.import_module(module_name)
        for attr in attr_path.split("."):
            obj = getattr(obj, attr)
    except (ImportError, AttributeError) as exc:
        raise ConfigError(
            f"providers.{spec.name}.load: cannot import {spec.load!r}: {exc}"
        ) from exc
    return obj


def provider_roots(host_roots: Roots | None, state_dir: Path) -> Roots:
    """The roots an inner provider is given: the host's, plus the runs folder.

    A worktree's cwd (`<state_dir>/runs/<run>/worktrees/<n>`) is then inside them.
    """
    runs = state_dir / "runs"
    runs.mkdir(parents=True, exist_ok=True)
    if host_roots is None:
        return Roots((runs.resolve(),))
    if not host_roots.is_named:
        return Roots((*host_roots.paths, runs.resolve()))
    name, n = RUNS_ROOT_NAME, 1
    while name in host_roots.names:
        n += 1
        name = f"{RUNS_ROOT_NAME}-{n}"
    named = dict(zip(host_roots.names, host_roots.paths, strict=True))
    return Roots.named({**named, name: runs})


def _accepts(factory: Any, name: str) -> bool:
    try:
        parameter = inspect.signature(factory).parameters.get(name)
    except (TypeError, ValueError):
        return False
    return parameter is not None and parameter.kind in _ACCEPTED_KINDS


def _extras(
    spec: ProviderConfig, factory: Any, host_roots: Roots | None, state_dir: Path
) -> dict[str, Any]:
    """`roots` and `state_dir` for a factory that takes them and whose table does not set them."""
    table: Mapping[str, Any] = spec.config if isinstance(spec.config, Mapping) else {}
    extras: dict[str, Any] = {}
    if _accepts(factory, "roots") and "roots" not in table:
        extras["roots"] = provider_roots(host_roots, state_dir)
    if _accepts(factory, "state_dir") and "state_dir" not in table:
        own = state_dir / "providers" / spec.name
        own.mkdir(parents=True, exist_ok=True, mode=0o700)
        extras["state_dir"] = own
    return extras


def load_provider(
    spec: ProviderConfig, *, host_roots: Roots | None = None, state_dir: Path | None = None
) -> AgentProvider:
    """Import and construct one provider.

    No config calls the factory bare, a table passes its keys as keyword arguments, and a path
    is passed as `config=`. With a *state_dir*, a factory that takes `roots` and/or `state_dir`
    (named parameters, not `**kwargs`) is given them unless its table sets them: the host's
    roots plus the runs folder, and `<state_dir>/providers/<name>`.
    """
    if spec.load is None:
        raise ConfigError(f"providers.{spec.name} is not an in-process provider")
    factory = _import_factory(spec)
    try:
        extras = _extras(spec, factory, host_roots, state_dir) if state_dir is not None else {}
        if spec.config is None:
            made = factory(**extras)
        elif isinstance(spec.config, Mapping):
            made = factory(**spec.config, **extras)
        else:
            made = factory(config=spec.config, **extras)
    except Exception as exc:
        raise ConfigError(f"providers.{spec.name}: {exc}") from exc
    if not isinstance(made, AgentProvider):
        raise ConfigError(f"providers.{spec.name}: {spec.load!r} did not make an AgentProvider")
    return made


def load_providers(config: FlowConfig, *, roots: Roots | None = None) -> dict[str, AgentProvider]:
    """Every in-process provider, loaded now; the first that fails raises `ConfigError`."""
    return {
        name: load_provider(spec, host_roots=roots, state_dir=config.state_dir)
        for name, spec in config.providers.items()
        if spec.kind == "inprocess"
    }


class LazyProviders(Mapping[str, AgentProvider]):
    """The configured providers, each imported and constructed on first use.

    A provider that fails to load fails only the lookups of its own name, with
    the same `ConfigError` every time (it is not retried), so one broken or
    missing provider cannot stop the others from running. Fleet and remote
    providers are not in-process: they are not listed and their lookups raise
    `KeyError`.
    """

    def __init__(self, config: FlowConfig, *, roots: Roots | None = None) -> None:
        self._config = config
        self._roots = roots
        self._loaded: dict[str, AgentProvider] = {}
        self._failed: dict[str, ConfigError] = {}

    def __getitem__(self, name: str) -> AgentProvider:
        if name in self._loaded:
            return self._loaded[name]
        if name in self._failed:
            raise self._failed[name]
        spec = self._config.providers[name]
        if spec.kind != "inprocess":
            raise KeyError(name)
        try:
            provider = load_provider(spec, host_roots=self._roots, state_dir=self._config.state_dir)
        except ConfigError as exc:
            _log.warning("provider %r failed to load: %s", name, exc)
            self._failed[name] = exc
            raise
        self._loaded[name] = provider
        return provider

    def __iter__(self) -> Iterator[str]:
        return iter(self._names())

    def __len__(self) -> int:
        return len(self._names())

    def _names(self) -> list[str]:
        return [n for n, spec in self._config.providers.items() if spec.kind == "inprocess"]
