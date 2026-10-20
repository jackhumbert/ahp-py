"""Launchers for the per-call host tests; not a test module.

Loaded by import path (`tests.launch_fakes:launch`), as a flow.toml's `launch` would.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from ahp_host import Host, LoopbackSingleUserPolicy
from ahp_host.provider.base import (
    AgentInfo,
    AgentSessionContext,
    TurnSink,
    UserMessage,
)
from ahp_protocol.transport import Transport, memory_pair

from ahp_host_flow.engine.remote import LaunchRequest
from tests.preamble import stripped


@dataclass
class Launched:
    request: LaunchRequest
    host: Host
    task: asyncio.Task[None]
    closed: bool = False


LAUNCHES: list[Launched] = []


def reset() -> None:
    LAUNCHES.clear()


class ModeSession:
    """`fail` fails the turn, `hang...` never ends, anything else is echoed with the context."""

    def __init__(self, context: AgentSessionContext) -> None:
        self.context = context

    async def send_user_message(self, message: UserMessage, sink: TurnSink) -> None:
        text = stripped(message).text
        if text == "fail":
            await sink.turn_failed("boom", "test.fail")
            return
        if text.startswith("hang"):
            await asyncio.Event().wait()
        folders = self.context.working_directories
        cwd = str(folders[0]) if folders else "-"
        names = sorted(str(t["name"]) for t in self.context.client_tools)
        await sink.text_delta(f"echo: {text} cwd={cwd} tools={names}")

    async def cancel(self, reason: str | None = None) -> None:
        pass

    async def aclose(self) -> None:
        pass


class ModeProvider:
    def __init__(self, *, provider_id: str = "claude") -> None:
        self.provider_id = provider_id

    @property
    def agent(self) -> AgentInfo:
        return AgentInfo(
            provider=self.provider_id,
            display_name="Mode",
            description="Echoes, fails or hangs by prompt.",
            models=(),
        )

    async def create_session(self, context: AgentSessionContext) -> ModeSession:
        return ModeSession(context)


async def launch(request: LaunchRequest) -> tuple[Transport, Callable[[], Awaitable[None]]]:
    host = Host(ModeProvider(), LoopbackSingleUserPolicy())
    client_end, server_end = memory_pair()
    task = asyncio.create_task(host.serve(server_end))
    launched = Launched(request, host, task)
    LAUNCHES.append(launched)

    async def close() -> None:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        await host.aclose()
        launched.closed = True

    return client_end, close


async def broken_launch(request: LaunchRequest) -> Any:
    raise OSError("no container runtime")
