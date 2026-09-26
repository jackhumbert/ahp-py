#!/usr/bin/env python3
"""Check an *installed* wheel, from outside the source tree.

Every defect this catches was invisible from a checkout and obvious from an
install: the package imported fine with the repository on `sys.path` and failed
for anyone who had only the wheel. So the rule here is that nothing may be
imported relative to this file, and the script must be run with a working
directory that is not the repository -- CI does that with `cd /tmp`.

    python -m build --wheel --outdir dist
    python -m venv /tmp/fresh && /tmp/fresh/bin/pip install dist/*.whl
    cd /tmp && /tmp/fresh/bin/python .../scripts/smoke_wheel.py

Exits non-zero with the failures listed. It is deliberately not a pytest file:
pytest is not installed in the clean venv, and adding it would put the source
tree back within reach.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

failures: list[str] = []


def check(name: str) -> None:
    print(f"  ok  {name}")


def fail(name: str, detail: object) -> None:
    failures.append(f"{name}: {detail}")
    print(f"FAIL  {name}: {detail}")


# 1. The repository must not be what we are testing. If someone runs this from
#    the checkout, `src/` layout usually saves us -- but an editable install or
#    a stray `agent_host_server/` in the cwd would not, and the whole point is
#    to exercise the installed copy.
cwd = Path.cwd().resolve()
if (cwd / "pyproject.toml").is_file() and (cwd / "src" / "agent_host_server").is_dir():
    sys.exit(f"run this from outside the repository; cwd is {cwd}")

# 2. The first line anyone types. This raised ImportError against a real wheel
#    once, because __init__ exported nothing.
try:
    import agent_host_server
    from agent_host_server import (
        AgentProvider,
        AhpError,
        Host,
        LoopbackSingleUserPolicy,
        Policy,
        TurnSink,
        __version__,
    )

    check(f"top-level exports import ({__version__})")
except Exception as exc:  # pragma: no cover - the failure is the output
    sys.exit(f"FAIL  top-level exports: {exc!r}")

#    ...and it must be the *installed* copy. An editable install resolves to
#    the source tree, which would pass every check below while proving nothing
#    about the wheel.
installed = Path(agent_host_server.__file__).resolve().parent
if "site-packages" not in installed.parts:
    sys.exit(f"not an installed copy -- imported from {installed}")
check(f"imported from {installed}")

# 3. Everything __all__ promises must resolve. A name in the list that is not
#    bound is a wheel-only failure: `from x import *` raises, plain imports do
#    not, and nothing in the suite does the former.
missing = [n for n in agent_host_server.__all__ if not hasattr(agent_host_server, n)]
if missing:
    fail("__all__ resolves", f"unbound: {missing}")
else:
    check(f"__all__ resolves ({len(agent_host_server.__all__)} names)")

# 4. The protocol package must be a SEPARATE distribution that arrived on its
#    own, not something vendored back into this wheel by accident. The whole
#    point of the split is one copy of the reducers.
try:
    import agent_host_protocol

    where = Path(agent_host_protocol.__file__).resolve().parent
    if where.parent == installed.parent and where.name in {"types", "reducers"}:
        fail("the protocol package is separate", f"found inside our own tree at {where}")
    else:
        check(f"agent_host_protocol resolves separately ({where.name})")
except Exception as exc:
    fail("agent_host_protocol imports", repr(exc))

# 5. py.typed, or every downstream type-checker silently treats us as Any.
if (installed / "py.typed").is_file():
    check("py.typed is packaged")
else:
    fail("py.typed is packaged", "absent")

# 6. The demo tree. Build backends skip dot-directories, so `.github/` inside it
#    was dropped from the wheel while the checkout looked fine.
demo = installed / "provider" / "demo_tree"
if not demo.is_dir():
    fail("demo tree is packaged", f"{demo} absent")
else:
    expected = {".github/hooks.json", ".github/hooks/pre-tool.json"}
    absent = sorted(p for p in expected if not (demo / p).is_file())
    if absent:
        fail("demo tree keeps its dotfiles", f"absent: {absent}")
    else:
        check(f"demo tree is packaged ({len(list(demo.rglob('*')))} entries)")

# 7. The optional extras have to be reachable by the names the docs give.
try:
    from agent_host_server.ws import serve_websocket  # noqa: F401

    check("agent_host_server.ws (the [ws] extra)")
except Exception as exc:
    fail("agent_host_server.ws", repr(exc))

# 8. The console script the README tells people to run.
try:
    from agent_host_server.__main__ import main  # noqa: F401

    check("console entry point")
except Exception as exc:
    fail("console entry point", repr(exc))

# 9. A real turn, end to end, in-process. Imports proving importable is not the
#    same as the thing working; this is cheap and it exercises the sequencer,
#    the reducers and the transport against the installed code.
try:
    from agent_host_protocol.transport import memory_pair

    from agent_host_server.provider import EchoProvider

    assert isinstance(EchoProvider(), AgentProvider)
    assert isinstance(LoopbackSingleUserPolicy(), Policy)
    assert issubclass(AhpError, Exception)
    assert TurnSink is not None

    from agent_host_server.core import ROOT_URI

    async def _turn() -> str:
        host = Host(EchoProvider(), LoopbackSingleUserPolicy())
        client, server = memory_pair()
        serve = asyncio.create_task(host.serve(server))
        try:
            await client.send(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "channel": ROOT_URI,
                        "clientId": "smoke",
                        "protocolVersions": ["0.7.0", "0.6.0"],
                        "initialSubscriptions": [ROOT_URI],
                    },
                }
            )
            while True:
                frame: Any = await asyncio.wait_for(client.receive(), timeout=10)
                if not isinstance(frame, dict) or frame.get("id") != 1:
                    continue
                if "error" in frame:
                    raise AssertionError(frame["error"])
                result = frame.get("result")
                return str(result.get("protocolVersion") if isinstance(result, dict) else None)
        finally:
            serve.cancel()
            await host.aclose()

    negotiated = asyncio.run(_turn())
    if negotiated in {"0.7.0", "0.6.0"}:
        check(f"a host serves a handshake (negotiated {negotiated})")
    else:
        fail("a host serves a handshake", f"negotiated {negotiated!r}")
except Exception as exc:
    fail("a host serves a handshake", repr(exc))

if failures:
    print(f"\n{len(failures)} failure(s)")
    sys.exit(1)
print("\nthe wheel is installable and works")
