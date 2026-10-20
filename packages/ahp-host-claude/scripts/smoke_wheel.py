#!/usr/bin/env python3
"""Check an *installed* wheel of ahp-host-claude, from outside the source tree.

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
if (cwd / "pyproject.toml").is_file() and (cwd / "src" / "ahp_host_claude").is_dir():
    sys.exit(f"run this from outside the repository; cwd is {cwd}")

try:
    import ahp_host_claude
    from ahp_host_claude import __version__
except Exception as exc:  # pragma: no cover - the failure is the output
    sys.exit(f"FAIL  import ahp_host_claude: {exc!r}")

installed = Path(ahp_host_claude.__file__).resolve().parent
if "site-packages" not in installed.parts:
    sys.exit(f"not an installed copy -- imported from {installed}")
check(f"imported from {installed} ({__version__})")

# 2. Everything `__all__` promises must resolve.
missing = [n for n in ahp_host_claude.__all__ if not hasattr(ahp_host_claude, n)]
if missing:
    fail("__all__ resolves", f"unbound: {missing}")
else:
    check(f"__all__ resolves ({len(ahp_host_claude.__all__)} names)")

# 3. py.typed, or every downstream type-checker silently treats us as Any.
if (installed / "py.typed").is_file():
    check("py.typed is packaged")
else:
    fail("py.typed is packaged", "absent")

# 4. The public surface and the entry point.
try:
    from ahp_host_claude import ClaudeProvider, ClaudeSession, from_config  # noqa: F401
    from ahp_host_claude.__main__ import main  # noqa: F401

    check("ClaudeProvider, ClaudeSession, from_config and the entry point import")
except Exception as exc:
    fail("the public surface imports", repr(exc))

# 5. The dependencies the wheel installs.
try:
    import claude_agent_sdk  # noqa: F401

    check("claude_agent_sdk resolved")
except Exception as exc:
    fail("claude_agent_sdk resolved", repr(exc))

try:
    import ahp_host

    host_dir = Path(ahp_host.__file__).resolve().parent
    if "site-packages" in host_dir.parts:
        check(f"ahp_host resolves outside our own tree ({host_dir})")
    else:
        fail("ahp_host resolves outside our own tree", str(host_dir))
except Exception as exc:
    fail("ahp_host resolved", repr(exc))

if failures:
    print(f"\n{len(failures)} failure(s)")
    sys.exit(1)
print("\nthe wheel is installable and works")
