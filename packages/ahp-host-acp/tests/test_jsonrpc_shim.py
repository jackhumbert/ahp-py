"""The deprecated `ahp_host_acp.jsonrpc` re-export warns, and still re-exports."""

from __future__ import annotations

import importlib
import sys

import pytest
from ahp_acp import AcpConnection, AcpError


def test_the_jsonrpc_shim_warns_and_re_exports() -> None:
    sys.modules.pop("ahp_host_acp.jsonrpc", None)
    with pytest.warns(DeprecationWarning, match="ahp_acp"):
        shim = importlib.import_module("ahp_host_acp.jsonrpc")
    assert shim.AcpConnection is AcpConnection
    assert shim.AcpError is AcpError
