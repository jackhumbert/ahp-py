"""Claude Code workflow scripts run as AHP sessions; see docs/plan.md."""

__version__ = "0.1.0"

import importlib
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ahp_host_flow.config import ConfigError, FlowConfig, load_config
    from ahp_host_flow.dispatch import ProviderDispatcher
    from ahp_host_flow.library import FlowRun, run
    from ahp_host_flow.provider import FlowProvider, FlowSession

__all__ = [
    "ConfigError",
    "FlowConfig",
    "FlowProvider",
    "FlowRun",
    "FlowSession",
    "ProviderDispatcher",
    "__version__",
    "load_config",
    "run",
]

# Public names load on first use so that importing the package (and so the
# stdlib-only Python runner) does not pull in ahp_host or third-party code.
_LAZY: dict[str, str] = {
    "ConfigError": "ahp_host_flow.config",
    "FlowConfig": "ahp_host_flow.config",
    "load_config": "ahp_host_flow.config",
    "ProviderDispatcher": "ahp_host_flow.dispatch",
    "FlowProvider": "ahp_host_flow.provider",
    "FlowSession": "ahp_host_flow.provider",
    "FlowRun": "ahp_host_flow.library",
    "run": "ahp_host_flow.library",
}


def __getattr__(name: str) -> Any:
    module = _LAZY.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(importlib.import_module(module), name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(__all__)
