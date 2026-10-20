"""Inner providers by import path, never statically (plan §2 Layering)."""

from __future__ import annotations

import importlib
import inspect
import logging
from collections.abc import Callable, Iterator, Mapping
from pathlib import Path
from typing import Any, Final

from ahp_host.node import Roots
from ahp_host.provider.base import AgentProvider
from ahp_host.provider.policy import (
    Decision,
    PolicyError,
    ToolPolicy,
    ToolRequest,
    combine,
    load_check,
)

from ahp_host_flow.config import ConfigError, FlowConfig, ProviderConfig
from ahp_host_flow.engine import AgentCall, VerbCall, VerbHandler
from ahp_host_flow.engine.remote import Launcher
from ahp_host_flow.routing import resolved_route

_log = logging.getLogger(__name__)

#: The name of the runs folder among named roots (a taken name gets `-2`, `-3`, ...).
RUNS_ROOT_NAME: Final = "flow-runs"

_ACCEPTED_KINDS = (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY)


def import_object(path: str, where: str) -> Any:
    """The object 'module:attr.path' names; a `ConfigError` led by *where* if it is missing."""
    module_name, _, attr_path = path.partition(":")
    try:
        obj: Any = importlib.import_module(module_name)
        for attr in attr_path.split("."):
            obj = getattr(obj, attr)
    except (ImportError, AttributeError) as exc:
        raise ConfigError(f"{where}: cannot import {path!r}: {exc}") from exc
    return obj


def _import_factory(spec: ProviderConfig) -> Any:
    assert spec.load is not None
    return import_object(spec.load, f"providers.{spec.name}.load")


def load_launcher(spec: ProviderConfig) -> Launcher:
    """The async launcher factory a remote provider's `launch` names."""
    assert spec.launch is not None
    launcher = import_object(spec.launch, f"providers.{spec.name}.launch")
    if not callable(launcher):
        raise ConfigError(f"providers.{spec.name}.launch: {spec.launch!r} is not callable")
    return launcher  # type: ignore[no-any-return]


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


def _policy_check(
    flow: ToolPolicy, own: Callable[[ToolRequest], Decision | None] | None
) -> Callable[[ToolRequest], Decision | None]:
    """A `ToolPolicy.check` consulting the flow's policy and the provider table's own check.

    `combine` decides: the most restrictive verdict wins (a deny from either is a deny), None
    when both allow, and a check that raises is a deny (`ToolPolicy._run_check`).
    """
    table_check = ToolPolicy(allow_paths_outside_roots=True, check=own) if own else None
    policy = combine(flow, table_check)
    assert policy is not None

    def check(request: ToolRequest) -> Decision | None:
        verdict = policy.decide(request)
        return None if verdict.action == "allow" else verdict

    return check


def _with_flow_policy(
    spec: ProviderConfig, table: Mapping[str, Any], flow: ToolPolicy
) -> dict[str, Any]:
    """The provider table's `policy` with the flow's `[policy]` folded into its check.

    Security decision (plan §8 M15 item 2): the flow policy is enforced by the inner
    provider's own policy hook, which runs in every approval mode, so an approve-all that
    stops the provider asking cannot also stop the flow's denies applying. The table's other
    keys (its rules, a provider-specific key such as ACP's `host_tools`) are kept as they are.
    """
    own = table.get("policy")
    if own is None:
        # No rules of the table's own, so no roots check of its own either: the flow
        # policy's `allow_paths_outside_roots` is the one that decides.
        merged: dict[str, Any] = {"allow_paths_outside_roots": True}
    elif isinstance(own, Mapping):
        merged = dict(own)
    else:
        raise ConfigError(f"providers.{spec.name}.policy must be a table")
    own_check: Callable[[ToolRequest], Decision | None] | None = None
    if "check" in merged:
        value = merged["check"]
        try:
            own_check = value if callable(value) else load_check(value)
        except PolicyError as exc:
            raise ConfigError(f"providers.{spec.name}: {exc}") from exc
    merged["check"] = _policy_check(flow, own_check)
    return merged


def load_provider(
    spec: ProviderConfig,
    *,
    host_roots: Roots | None = None,
    state_dir: Path | None = None,
    policy: ToolPolicy | None = None,
) -> AgentProvider:
    """Import and construct one provider.

    No config calls the factory bare, a table passes its keys as keyword arguments, and a path
    is passed as `config=`. With a *state_dir*, a factory that takes `roots` and/or `state_dir`
    (named parameters, not `**kwargs`) is given them unless its table sets them: the host's
    roots plus the runs folder, and `<state_dir>/providers/<name>`.

    The flow's *policy* is handed to a factory that takes a named `policy` keyword, combined
    with the table's own `policy` (see `_with_flow_policy`). A factory configured by a file
    path gets the flow policy alone and must combine it with its file's `[policy]` (the
    Claude and ACP `from_config` do); a factory without the keyword is enforced only at the
    flow's own confirmation hook.
    """
    if spec.load is None:
        raise ConfigError(f"providers.{spec.name} is not an in-process provider")
    factory = _import_factory(spec)
    table: Mapping[str, Any] = spec.config if isinstance(spec.config, Mapping) else {}
    composed: dict[str, Any] = {}
    if policy is not None and _accepts(factory, "policy"):
        composed["policy"] = _with_flow_policy(spec, table, policy)
    try:
        extras = _extras(spec, factory, host_roots, state_dir) if state_dir is not None else {}
        if spec.config is None:
            made = factory(**extras, **composed)
        elif isinstance(spec.config, Mapping):
            made = factory(**{**spec.config, **composed}, **extras)
        else:
            made = factory(config=spec.config, **extras, **composed)
    except Exception as exc:
        raise ConfigError(f"providers.{spec.name}: {exc}") from exc
    if not isinstance(made, AgentProvider):
        raise ConfigError(f"providers.{spec.name}: {spec.load!r} did not make an AgentProvider")
    return made


def load_providers(config: FlowConfig, *, roots: Roots | None = None) -> dict[str, AgentProvider]:
    """Every in-process provider, loaded now; the first that fails raises `ConfigError`."""
    return {
        name: load_provider(
            spec, host_roots=roots, state_dir=config.state_dir, policy=config.policy
        )
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
            provider = load_provider(
                spec,
                host_roots=self._roots,
                state_dir=self._config.state_dir,
                policy=self._config.policy,
            )
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


class LazyVerb:
    """A custom verb handler imported on its first call, never at config load.

    A failed import, or an object that is not callable, is cached and raised on every call
    (it is not retried), so one broken verb fails only its own calls.
    """

    def __init__(self, name: str, path: str) -> None:
        self._name = name
        self._path = path
        self._handler: VerbHandler | None = None
        self._failed: ConfigError | None = None

    def _load(self) -> VerbHandler:
        if self._handler is not None:
            return self._handler
        if self._failed is not None:
            raise self._failed
        try:
            obj = import_object(self._path, f"verbs.{self._name}")
            if not callable(obj):
                raise ConfigError(f"verbs.{self._name}: {self._path!r} is not callable")
        except ConfigError as exc:
            _log.warning("verb %r failed to load: %s", self._name, exc)
            self._failed = exc
            raise
        handler: VerbHandler = obj
        self._handler = handler
        return handler

    async def __call__(self, call: VerbCall) -> Any:
        result = self._load()(call)
        if inspect.isawaitable(result):
            result = await result
        return result


def lazy_verbs(config: FlowConfig) -> dict[str, VerbHandler]:
    """A `LazyVerb` for each `[verbs]` entry; nothing is imported until a verb is called."""
    return {name: LazyVerb(name, path) for name, path in config.verbs.items()}


def resume_key_hook(
    config: FlowConfig, run_model: str | None
) -> tuple[Callable[[AgentCall], Any] | None, str | None]:
    """The `[resume] key` hook and its journal mode name: `(None, None)` for 'default'.

    'model' keys each call by its resolved provider and model; an import path is called with the
    call and that route. A path that is missing or not callable is a `ConfigError`.
    """
    key = config.resume.key
    if key == "default":
        return None, None
    if key == "model":
        return (lambda call: resolved_route(config, call, run_model)), "model"
    fn = import_object(key, "resume.key")
    if not callable(fn):
        raise ConfigError(f"resume.key: {key!r} is not callable")
    return (lambda call: fn(call, resolved_route(config, call, run_model))), key
