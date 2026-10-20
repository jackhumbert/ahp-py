#!/usr/bin/env python3
"""Check an *installed* wheel of ahp-gateway, from outside the source tree.

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

import importlib
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
if (cwd / "pyproject.toml").is_file() and (cwd / "src" / "ahp_gateway").is_dir():
    sys.exit(f"run this from outside the repository; cwd is {cwd}")

try:
    import ahp_gateway
    from ahp_gateway import __version__
except Exception as exc:  # pragma: no cover - the failure is the output
    sys.exit(f"FAIL  import ahp_gateway: {exc!r}")

installed = Path(ahp_gateway.__file__).resolve().parent
if "site-packages" not in installed.parts:
    sys.exit(f"not an installed copy -- imported from {installed}")
check(f"imported from {installed} ({__version__})")

# 2. Everything `__all__` promises must resolve.
missing = [n for n in ahp_gateway.__all__ if not hasattr(ahp_gateway, n)]
if missing:
    fail("__all__ resolves", f"unbound: {missing}")
else:
    check(f"__all__ resolves ({len(ahp_gateway.__all__)} names)")

# 3. py.typed, or every downstream type-checker silently treats us as Any.
if (installed / "py.typed").is_file():
    check("py.typed is packaged")
else:
    fail("py.typed is packaged", "absent")

# 4. Every subpackage, by name.
for sub in ("core", "registry", "relay", "ws"):
    try:
        importlib.import_module(f"ahp_gateway.{sub}")
        check(f"ahp_gateway.{sub} is packaged")
    except Exception as exc:
        fail(f"ahp_gateway.{sub} is packaged", repr(exc))

# 5. The floor this package stands on: the protocol, host and client it pins.
for dep in ("ahp_protocol", "ahp_host", "ahp_client"):
    try:
        module = importlib.import_module(dep)
        check(f"{dep} resolved ({module.__version__})")
    except Exception as exc:
        fail(f"{dep} resolved", repr(exc))

if failures:
    print(f"\n{len(failures)} failure(s)")
    sys.exit(1)
print("\nthe wheel is installable and works")
