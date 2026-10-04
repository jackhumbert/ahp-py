#!/usr/bin/env python3
"""Check an *installed* wheel of this package, from outside the source tree.

This package has one publication risk above all others: **the vendored corpus
resolves in a source checkout and is absent from the wheel.** That defect
shipped once already, in the repository this code was extracted from, where
`CORPUS_ROOT` walked `parents[3]` to a `vendor/` directory that the build
backend never packaged. From a checkout everything passed; from an install the
conformance gate had a loader and no data.

So the rule here is that nothing may be imported or resolved relative to this
file, and the script must run with a working directory that is not the
repository — CI does that with `cd /tmp`.

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
if (cwd / "pyproject.toml").is_file() and (cwd / "src" / "ahp_protocol").is_dir():
    sys.exit(f"run this from outside the repository; cwd is {cwd}")

try:
    import ahp_protocol
    from ahp_protocol import __version__
except Exception as exc:  # pragma: no cover - the failure is the output
    sys.exit(f"FAIL  import ahp_protocol: {exc!r}")

installed = Path(ahp_protocol.__file__).resolve().parent
if "site-packages" not in installed.parts:
    sys.exit(f"not an installed copy -- imported from {installed}")
check(f"imported from {installed} ({__version__})")

# 2. Everything `__all__` promises must resolve. A name in the list that is not
#    bound is a wheel-only failure: `from x import *` raises, plain imports do
#    not, and nothing in the suite does the former.
missing = [n for n in ahp_protocol.__all__ if not hasattr(ahp_protocol, n)]
if missing:
    fail("__all__ resolves", f"unbound: {missing}")
else:
    check(f"__all__ resolves ({len(ahp_protocol.__all__)} names)")

# 3. py.typed, or every downstream type-checker silently treats us as Any. Both
#    consumers of this package are strict-mypy codebases.
if (installed / "py.typed").is_file():
    check("py.typed is packaged")
else:
    fail("py.typed is packaged", "absent")

# 4. THE ONE THAT MATTERS. The corpus has to be inside the wheel, and the
#    loader has to find it there rather than by walking up from a source file.
try:
    from ahp_protocol.conformance.corpus import (
        CORPUS_ROOT,
        pin,
        reducer_fixtures,
        round_trip_fixtures,
    )

    if not str(CORPUS_ROOT).startswith(str(installed)):
        fail("the corpus is inside the wheel", f"CORPUS_ROOT escaped to {CORPUS_ROOT}")
    else:
        check(f"corpus resolves inside the package ({CORPUS_ROOT.name})")

    reducers = list(reducer_fixtures())
    trips = list(round_trip_fixtures())
    # Exact counts, not "more than zero": a loader that silently finds a
    # partial tree is the failure mode a truthy check would miss.
    if len(reducers) != 308:
        fail("308 reducer fixtures", f"found {len(reducers)}")
    else:
        check("308 reducer fixtures")
    if len(trips) != 67:
        fail("67 round-trip fixtures", f"found {len(trips)}")
    else:
        check("67 round-trip fixtures")

    spec = pin().get("specTag")
    check(f"PIN.json readable (spec {spec})")

    schemas = sorted(p.name for p in (CORPUS_ROOT / "schema").glob("*.schema.json"))
    if len(schemas) < 5:
        fail("the JSON schemas are packaged", f"found {schemas}")
    else:
        check(f"{len(schemas)} JSON schemas packaged")
except Exception as exc:
    fail("the corpus is packaged", repr(exc))

# 5. The reducers actually run, against the packaged corpus. Importable is not
#    the same as working, and this is cheap.
try:
    from ahp_protocol.reducers import REDUCERS

    fixture = next(f for f in reducer_fixtures() if f.reducer == "chat")
    state = fixture.initial
    for action in fixture.actions:
        state = REDUCERS[fixture.reducer](state, action)
    check(f"a reducer runs against the packaged corpus ({len(REDUCERS)} reducers)")
except Exception as exc:
    fail("a reducer runs", repr(exc))

# 6. The generated authorisation table. A wheel that shipped an empty one would
#    make every host trusting it refuse every client action.
try:
    from ahp_protocol.types import ACTION_TYPES, IS_CLIENT_DISPATCHABLE

    dispatchable = sum(1 for v in IS_CLIENT_DISPATCHABLE.values() if v)
    if len(ACTION_TYPES) < 80 or dispatchable < 30:
        fail(
            "the generated tables are populated",
            f"{len(ACTION_TYPES)} actions, {dispatchable} dispatchable",
        )
    else:
        check(f"{len(ACTION_TYPES)} action types, {dispatchable} client-dispatchable")
except Exception as exc:
    fail("the generated tables", repr(exc))

# 7. The transports, which both peers meet over.
try:
    from ahp_protocol.transport import memory_pair

    a, b = memory_pair()
    check("memory_pair() constructs")
except Exception as exc:
    fail("transports", repr(exc))

if failures:
    print(f"\n{len(failures)} failure(s)")
    sys.exit(1)
print("\nthe wheel is installable and carries its corpus")
