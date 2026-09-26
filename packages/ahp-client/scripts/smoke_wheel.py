#!/usr/bin/env python3
"""Check an *installed* wheel of this package, from outside the source tree.

Three publication defects in this family of repositories were invisible from a
checkout and obvious from an install: a name in `__all__` that was never bound,
a subpackage the build backend never collected, and a missing `py.typed` that
silently turned every downstream annotation into `Any`. None of them can fail
in the suite, because the suite imports the source tree.

So the rule is that nothing here may be resolved relative to this file, and the
script must run with a working directory that is not the repository -- CI does
that with `cd /tmp`.

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
if (cwd / "pyproject.toml").is_file() and (cwd / "src" / "ahp_client").is_dir():
    sys.exit(f"run this from outside the repository; cwd is {cwd}")

try:
    import ahp_client
    from ahp_client import __version__
except Exception as exc:  # pragma: no cover - the failure is the output
    sys.exit(f"FAIL  import ahp_client: {exc!r}")

installed = Path(ahp_client.__file__).resolve().parent
if "site-packages" not in installed.parts:
    sys.exit(f"not an installed copy -- imported from {installed}")
check(f"imported from {installed} ({__version__})")

# 2. Everything `__all__` promises must resolve. A name in the list that is not
#    bound is a wheel-only failure: `from x import *` raises, plain imports do
#    not, and nothing in the suite does the former.
missing = [n for n in ahp_client.__all__ if not hasattr(ahp_client, n)]
if missing:
    fail("__all__ resolves", f"unbound: {missing}")
else:
    check(f"__all__ resolves ({len(ahp_client.__all__)} names)")

# 3. Every subpackage, by name. `packages = ["src/ahp_client"]` collects
#    the tree today; a future move to explicit includes could drop one and only
#    the users of that one surface would notice. `testing` is the likeliest to
#    be excluded as "not production code" -- it is part of the public API.
for sub in ("api", "client", "hosts", "serve", "testing", "wirelog", "ws"):
    try:
        importlib.import_module(f"ahp_client.{sub}")
        check(f"ahp_client.{sub} is packaged")
    except Exception as exc:
        fail(f"ahp_client.{sub} is packaged", repr(exc))

# 4. py.typed, or every downstream type-checker silently treats us as Any.
if (installed / "py.typed").is_file():
    check("py.typed is packaged")
else:
    fail("py.typed is packaged", "absent")

# 5. The floor this package stands on. The dependency is what makes the client
#    and the host agree; a wheel whose pin did not install is unusable.
try:
    from ahp_protocol import __version__ as protocol_version
    from ahp_protocol.transport import memory_pair  # noqa: F401

    check(f"ahp-protocol resolved ({protocol_version})")
except Exception as exc:
    fail("ahp-protocol resolved", repr(exc))


# 6. THE ONE THAT MATTERS. A real handshake and the conformance probe, driven
#    through the installed wheel. Importable is not the same as working.
async def _probe() -> object:
    from ahp_client.doctor import diagnose
    from ahp_client.testing import echo_host

    host = echo_host()
    await host.start()
    try:
        return await diagnose(host.transport())
    finally:
        await host.stop()


try:
    report = asyncio.run(_probe())
    if getattr(report, "ok", False):
        check("the conformance probe passes against the packaged fake host")
    else:
        fail("the conformance probe passes", str(report))
except Exception as exc:
    fail("the conformance probe runs", repr(exc))

if failures:
    print(f"\n{len(failures)} failure(s)")
    sys.exit(1)
print("\nthe wheel is installable and the client works from it")
