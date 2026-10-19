"""Shared helpers for the tests."""

from __future__ import annotations

import json
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from ahp_acp import SessionUpdate

FAKE_AGENT = Path(__file__).with_name("fake_agent.py")
INIT: dict[str, Any] = {"protocolVersion": 1, "agentCapabilities": {}, "authMethods": []}


def argv_for(script: Mapping[str, Any]) -> list[str]:
    return [sys.executable, str(FAKE_AGENT), json.dumps(script)]


class Recorder:
    """Collects what the client delivers."""

    def __init__(self) -> None:
        self.updates: list[SessionUpdate] = []
        self.permissions: list[dict[str, Any]] = []

    async def on_update(self, update: SessionUpdate) -> None:
        self.updates.append(update)

    async def on_permission(self, params: dict[str, Any]) -> dict[str, Any]:
        self.permissions.append(params)
        return {"outcome": "selected", "optionId": "allow"}
