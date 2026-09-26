"""Test doubles, shipped as public API.

Importing this must never pull a socket stack -- an `import-linter` contract
forbids `ws` and `cli` from here so a downstream suite stays offline.
"""

from __future__ import annotations

from agent_host_client.testing.fake_host import (
    FakeHost,
    FakeRpcError,
    FakeToolCall,
    echo_host,
    tool_call_host,
)

__all__ = ["FakeHost", "FakeRpcError", "FakeToolCall", "echo_host", "tool_call_host"]
