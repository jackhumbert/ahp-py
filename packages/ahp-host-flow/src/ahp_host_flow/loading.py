"""Inner providers by import path, never statically (plan §2 Layering)."""

from __future__ import annotations

import importlib
import logging
from collections.abc import Iterator, Mapping
from typing import Any

from ahp_host.provider.base import AgentProvider

from ahp_host_flow.config import ConfigError, FlowConfig, ProviderConfig

_log = logging.getLogger(__name__)


def _import_factory(spec: ProviderConfig) -> Any:
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


def load_provider(spec: ProviderConfig) -> AgentProvider:
    """Import and construct one provider.

    No config calls the factory bare, a table passes its keys as keyword arguments, and a path
    is passed as `config=`.
    """
    factory = _import_factory(spec)
    try:
        if spec.config is None:
            made = factory()
        elif isinstance(spec.config, Mapping):
            made = factory(**spec.config)
        else:
            made = factory(config=spec.config)
    except Exception as exc:
        raise ConfigError(f"providers.{spec.name}: {exc}") from exc
    if not isinstance(made, AgentProvider):
        raise ConfigError(f"providers.{spec.name}: {spec.load!r} did not make an AgentProvider")
    return made


def load_providers(config: FlowConfig) -> dict[str, AgentProvider]:
    """Every configured provider, loaded now; the first that fails raises `ConfigError`."""
    return {name: load_provider(spec) for name, spec in config.providers.items()}


class LazyProviders(Mapping[str, AgentProvider]):
    """The configured providers, each imported and constructed on first use.

    A provider that fails to load fails only the lookups of its own name, with
    the same `ConfigError` every time (it is not retried), so one broken or
    missing provider cannot stop the others from running.
    """

    def __init__(self, config: FlowConfig) -> None:
        self._config = config
        self._loaded: dict[str, AgentProvider] = {}
        self._failed: dict[str, ConfigError] = {}

    def __getitem__(self, name: str) -> AgentProvider:
        if name in self._loaded:
            return self._loaded[name]
        if name in self._failed:
            raise self._failed[name]
        spec = self._config.providers[name]
        try:
            provider = load_provider(spec)
        except ConfigError as exc:
            _log.warning("provider %r failed to load: %s", name, exc)
            self._failed[name] = exc
            raise
        self._loaded[name] = provider
        return provider

    def __iter__(self) -> Iterator[str]:
        return iter(self._config.providers)

    def __len__(self) -> int:
        return len(self._config.providers)
