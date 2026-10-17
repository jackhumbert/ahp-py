"""A raw JSON-RPC peer for `python -I -m ahp_host_flow.sandbox.pyrunner` (a test helper)."""

from __future__ import annotations

import asyncio
import json
import sys
from typing import Any

from ahp_host_flow.sandbox import protocol


class PyRunnerWire:
    def __init__(self) -> None:
        self.proc: asyncio.subprocess.Process | None = None
        self.raw_lines: list[bytes] = []

    async def start(self) -> None:
        self.proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-I",
            "-m",
            protocol.PYRUNNER_MODULE,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=protocol.MAX_LINE_BYTES,
        )

    async def send(self, msg: dict[str, Any]) -> None:
        assert self.proc is not None
        assert self.proc.stdin is not None
        line = json.dumps(msg, ensure_ascii=True, separators=(",", ":"))
        self.proc.stdin.write(line.encode("ascii") + b"\n")
        await self.proc.stdin.drain()

    async def recv(self, timeout: float = 10.0) -> dict[str, Any]:
        assert self.proc is not None
        assert self.proc.stdout is not None
        raw = await asyncio.wait_for(self.proc.stdout.readline(), timeout)
        self.raw_lines.append(raw)
        msg: dict[str, Any] = json.loads(raw)
        return msg

    async def close(self) -> int | None:
        assert self.proc is not None
        assert self.proc.stdin is not None
        self.proc.stdin.close()
        try:
            await asyncio.wait_for(self.proc.wait(), 10.0)
        except TimeoutError:
            self.proc.kill()
            await self.proc.wait()
        return self.proc.returncode

    async def stderr_text(self) -> str:
        assert self.proc is not None
        assert self.proc.stderr is not None
        return (await self.proc.stderr.read()).decode("utf-8", "replace")
