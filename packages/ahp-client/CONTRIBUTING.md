# Contributing

Thanks for looking at this. Two documents outrank this one: for AI agents and
humans alike, [`AGENTS.md`](AGENTS.md) is the working guide (invariants,
layering, testing honesty), and [`docs/plan.md`](docs/plan.md) is the design it
points into. This file is the short version for getting a change landed.

## The one rule that surprises people

AHP is an external specification owned by Microsoft. **We implement it; we do
not design it.** A PR that changes a wire type, an action shape, a state field
or an error code to anything other than what the vendored pin says will be
declined however sensible it is — protocol questions go upstream, not into a
private divergence. Where this client deliberately diverges from a *reference
client* (not the spec), the divergence is recorded as an ADR in
[`docs/decisions/`](docs/decisions/); a new divergence needs a new ADR in the
same PR.

## Setup

This family of packages installs from GitHub, deliberately not from PyPI; for
development, the shared protocol layer is the sibling package
`../ahp-protocol`. From the repository root, `uv sync --all-packages
--all-extras` sets up every package at once; with pip, the install order
matters (its `~=` pin resolves against what is installed):

```bash
python -m venv .venv
.venv/bin/pip install -e ../ahp-protocol
.venv/bin/pip install -e '.[ws]'
.venv/bin/pip install --group dev
```

Optional, for the interop tests: `.venv/bin/pip install -e ../ahp-host`.
Without it those modules skip; with `AHP_INTEROP_REQUIRED=1` a missing sibling
is a hard error instead (that is what CI's host job sets).

## The gate

Everything CI runs, runnable locally:

```bash
.venv/bin/python -m pytest
.venv/bin/ruff check . && .venv/bin/ruff format --check .
.venv/bin/mypy
.venv/bin/lint-imports
```

The suite is offline (no model, no credentials, no network) and warnings are
errors. `mypy` is strict. The three import-linter contracts are architecture,
not style — read the layering section of `AGENTS.md` before adding an import
across packages.

## What a finished change includes

From `AGENTS.md`, abbreviated — a change is not done until:

- **A test pins it.** Every protocol claim is backed by a test; a bug fix
  carries the regression test that would have caught it.
- **The docs still tell the truth, in the same PR.** README claims,
  `docs/plan.md`, and a `CHANGELOG.md` entry under `[Unreleased]`.
  `docs/parity.md` is generated — run `scripts/generate_parity.py` if you
  touched a command, notification or reverse method; `tests/docs/` fails if
  prose and code disagree.
- **Comments say why, not what.** Non-obvious lines carry the evidence that
  made them non-obvious (a spec sentence, a wire frame, an offset in the
  reference client).

Conventional commits (`fix:`, `feat:`, `docs:` …), small reviewable PRs.

## Reporting problems

- A host behaving unexpectedly? `ahp_client.doctor.diagnose()` produces
  a report where each failed check names the MUST or SHOULD it comes from —
  paste that into the issue.
- A security concern? See [`SECURITY.md`](SECURITY.md) — please do not open a
  public issue first.
