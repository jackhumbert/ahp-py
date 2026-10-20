#!/usr/bin/env python3
"""Check an *installed* wheel of ahp-host-acp, from outside the source tree.

Publication defects are invisible from a checkout and obvious from an install: a
name in `__all__` that was never bound, a subpackage or data file the build backend
never collected, a missing `py.typed` that turns every downstream annotation into
`Any`. None of them can fail in the suite, because the suite imports the source tree.

So nothing here may be resolved relative to this file, and the script must run with
a working directory that is not the repository -- CI does that with `cd /tmp`.

    python -m build --wheel --outdir dist
    python -m venv /tmp/fresh && /tmp/fresh/bin/pip install dist/*.whl
    cd /tmp && /tmp/fresh/bin/python .../scripts/smoke_wheel.py

Exits non-zero with the failures listed. Deliberately not a pytest file: pytest
is not installed in the clean venv, and adding it would put the source tree
back within reach.
"""

from __future__ import annotations

import asyncio
import importlib
import sys
import tempfile
from pathlib import Path

failures: list[str] = []


def check(name: str) -> None:
    print(f"  ok  {name}")


def fail(name: str, detail: object) -> None:
    failures.append(f"{name}: {detail}")
    print(f"FAIL  {name}: {detail}")


# 1. Not the repository. An editable install or a stray package directory in
#    the cwd would let every check below pass while proving nothing.
cwd = Path.cwd().resolve()
if (cwd / "pyproject.toml").is_file() and (cwd / "src" / "ahp_host_acp").is_dir():
    sys.exit(f"run this from outside the repository; cwd is {cwd}")

try:
    import ahp_host_acp
    from ahp_host_acp import __version__
except Exception as exc:  # pragma: no cover - the failure is the output
    sys.exit(f"FAIL  import ahp_host_acp: {exc!r}")

installed = Path(ahp_host_acp.__file__).resolve().parent
if "site-packages" not in installed.parts:
    sys.exit(f"not an installed copy -- imported from {installed}")
check(f"imported from {installed} ({__version__})")

# 2. Everything `__all__` promises must resolve.
missing = [n for n in ahp_host_acp.__all__ if not hasattr(ahp_host_acp, n)]
if missing:
    fail("__all__ resolves", f"unbound: {missing}")
else:
    check(f"__all__ resolves ({len(ahp_host_acp.__all__)} names)")

# 3. py.typed, or every downstream type-checker silently treats us as Any.
if (installed / "py.typed").is_file():
    check("py.typed is packaged")
else:
    fail("py.typed is packaged", "absent")

# 4. The modules the entry points and the child process run.
for name in (
    "ahp_host_acp.relay",
    "ahp_host_acp.reference.__main__",
    "ahp_host_acp.reference.stub",
):
    try:
        importlib.import_module(name)
        check(f"{name} is packaged")
    except Exception as exc:
        fail(f"{name} is packaged", repr(exc))
try:
    from ahp_host_acp.__main__ import main  # noqa: F401

    check("ahp_host_acp.__main__.main imports")
except Exception as exc:
    fail("ahp_host_acp.__main__.main imports", repr(exc))


# 5. THE ONE THAT MATTERS. A real prompt through the packaged reference agent,
#    against the packaged stub endpoint, driven by the ACP client.
async def _prompt() -> tuple[dict[str, object], str]:
    import ahp_acp

    from ahp_host_acp import reference
    from ahp_host_acp.reference.stub import StubServer, say

    texts: list[str] = []

    async def on_update(update: ahp_acp.SessionUpdate) -> None:
        if isinstance(update, ahp_acp.AgentMessageChunk):
            texts.append(str(update.content.get("text", "")))

    async def on_permission(params: dict[str, object]) -> dict[str, object]:
        return ahp_acp.reject_once(params["options"])  # type: ignore[arg-type]

    with StubServer([say("smoke ok")]) as stub:
        async with ahp_acp.AcpClient(
            reference.command(),
            spawn_options={"env": stub.agent_env()},
            client_info={"name": "smoke", "version": "0"},
            on_update=on_update,
            on_permission=on_permission,
        ) as client:
            await client.initialize()
            session = await client.new_session(tempfile.mkdtemp())
            result = await client.prompt(session["sessionId"], [{"type": "text", "text": "hi"}])
    return result, "".join(texts)


try:
    result, text = asyncio.run(asyncio.wait_for(_prompt(), 60))
    stop = result.get("stopReason")
    if stop == "end_turn" and text == "smoke ok":
        check("a prompt through the reference agent ends the turn with its text")
    else:
        fail("a prompt through the reference agent", f"stopReason={stop!r} text={text!r}")
except Exception as exc:
    fail("a prompt through the reference agent runs", repr(exc))

if failures:
    print(f"\n{len(failures)} failure(s)")
    sys.exit(1)
print("\nthe wheel is installable and works")
