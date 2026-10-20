#!/usr/bin/env python3
"""Check an *installed* wheel of ahp-acp, from outside the source tree.

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

import sys
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
if (cwd / "pyproject.toml").is_file() and (cwd / "src" / "ahp_acp").is_dir():
    sys.exit(f"run this from outside the repository; cwd is {cwd}")

try:
    import ahp_acp
    from ahp_acp import __version__
except Exception as exc:  # pragma: no cover - the failure is the output
    sys.exit(f"FAIL  import ahp_acp: {exc!r}")

installed = Path(ahp_acp.__file__).resolve().parent
if "site-packages" not in installed.parts:
    sys.exit(f"not an installed copy -- imported from {installed}")
check(f"imported from {installed} ({__version__})")

# 2. Everything `__all__` promises must resolve.
missing = [n for n in ahp_acp.__all__ if not hasattr(ahp_acp, n)]
if missing:
    fail("__all__ resolves", f"unbound: {missing}")
else:
    check(f"__all__ resolves ({len(ahp_acp.__all__)} names)")

# 3. py.typed, or every downstream type-checker silently treats us as Any.
if (installed / "py.typed").is_file():
    check("py.typed is packaged")
else:
    fail("py.typed is packaged", "absent")

# 4. Standalone: the ACP client side depends on nothing of the family.
FAMILY = (
    "ahp_protocol",
    "ahp_host",
    "ahp_client",
    "ahp_gateway",
    "ahp_host_claude",
    "ahp_host_acp",
    "ahp_host_flow",
)
pulled = sorted(m for m in sys.modules if m.split(".")[0] in FAMILY)
if pulled:
    fail("ahp_acp is standalone", f"imported: {pulled}")
else:
    check("ahp_acp is standalone")

# 5. A real parse through the installed wheel.
try:
    update = ahp_acp.parse_update(
        {
            "sessionId": "s",
            "update": {
                "sessionUpdate": "agent_message_chunk",
                "content": {"type": "text", "text": "hi"},
            },
        }
    )
    if isinstance(update, ahp_acp.AgentMessageChunk):
        check("parse_update returns an AgentMessageChunk")
    else:
        fail("parse_update returns an AgentMessageChunk", repr(update))
except Exception as exc:
    fail("parse_update runs", repr(exc))

if failures:
    print(f"\n{len(failures)} failure(s)")
    sys.exit(1)
print("\nthe wheel is installable and works")
