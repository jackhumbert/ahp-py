"""The Node subprocess that runs workflow scripts, and its JSON-RPC peer."""

import importlib
from typing import TYPE_CHECKING, Any

from ahp_host_flow.sandbox import protocol

if TYPE_CHECKING:
    from ahp_host_flow.sandbox.client import (
        NO_ARGS,
        RUNNER,
        Language,
        MetaError,
        RunState,
        RunTimeoutError,
        Sandbox,
        SandboxError,
        ScriptError,
        ScriptSyntaxError,
        WorkflowRunError,
        extract_meta,
        language_of,
        resolve_for_run,
        run_script,
        run_workflow_child,
        runner_command,
    )
    from ahp_host_flow.sandbox.node import (
        MIN_NODE_MAJOR,
        NODE_ENV,
        NodeNotFoundError,
        find_node,
        node_version,
        parse_node_version,
    )

__all__ = [
    "MIN_NODE_MAJOR",
    "NODE_ENV",
    "NO_ARGS",
    "RUNNER",
    "Language",
    "MetaError",
    "NodeNotFoundError",
    "RunState",
    "RunTimeoutError",
    "Sandbox",
    "SandboxError",
    "ScriptError",
    "ScriptSyntaxError",
    "WorkflowRunError",
    "extract_meta",
    "find_node",
    "language_of",
    "node_version",
    "parse_node_version",
    "protocol",
    "resolve_for_run",
    "run_script",
    "run_workflow_child",
    "runner_command",
]

# Everything but `protocol` (stdlib-only) loads on first use; see ahp_host_flow.
_CLIENT = "ahp_host_flow.sandbox.client"
_NODE = "ahp_host_flow.sandbox.node"
_LAZY: dict[str, str] = {
    "NO_ARGS": _CLIENT,
    "RUNNER": _CLIENT,
    "Language": _CLIENT,
    "MetaError": _CLIENT,
    "RunState": _CLIENT,
    "RunTimeoutError": _CLIENT,
    "Sandbox": _CLIENT,
    "SandboxError": _CLIENT,
    "ScriptError": _CLIENT,
    "ScriptSyntaxError": _CLIENT,
    "WorkflowRunError": _CLIENT,
    "extract_meta": _CLIENT,
    "run_script": _CLIENT,
    "resolve_for_run": _CLIENT,
    "run_workflow_child": _CLIENT,
    "language_of": _CLIENT,
    "runner_command": _CLIENT,
    "MIN_NODE_MAJOR": _NODE,
    "NODE_ENV": _NODE,
    "NodeNotFoundError": _NODE,
    "find_node": _NODE,
    "node_version": _NODE,
    "parse_node_version": _NODE,
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
