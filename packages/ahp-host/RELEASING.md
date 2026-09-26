# Releasing

The release workflow (`release.yml`) is tag-triggered and gated: it builds,
checks the tag against the packaged version, smoke-tests the wheel from a clean
venv — installing `agent-host-protocol` from its repository the way a user
would — and drafts a GitHub release from the CHANGELOG section, failing if
there is none. **The GitHub release is the whole release**: this family of
packages is public on GitHub and deliberately not on PyPI. Users install with

```bash
pip install "agent-host-protocol @ git+https://github.com/jackhumbert/agent-host-protocol-py"
pip install "agent-host-server[ws] @ git+https://github.com/jackhumbert/agent-host-server-py"
```

pinned by tag (`…-py@v0.1.0`) when they want a release rather than `main`.
There is no index upload, no trusted publisher, no `pypi` environment — and
nothing here uses a long-lived token. What follows is the part a workflow
cannot do: the order of operations around it.

## One-time, before the first release

**The repositories must be public** — this one and both siblings. Every CI job
checks out `agent-host-protocol-py` (the interop job also
`agent-host-client-py`), users' `pip install git+https://…` lines fetch them
anonymously, and the README and CHANGELOG link into them. There is nothing to
configure on any index.

## Per release

1. **Flip the claims that releasing makes true.** These are guarded by
   `tests/docs/`, so a flip is a test change in the same commit — that is the
   design, not an obstacle. The install block is already in its final form
   (git installs work before and after a tag); what moves is the status
   banner's maturity claim and the tag named in the pinning example.
2. **Set the version and the date.** `__version__` in
   `src/agent_host_server/__init__.py` is the single source (`pyproject.toml`
   reads it dynamically); the tag must be `v<that>` or the workflow refuses.
   The CHANGELOG section for the version currently reads `## [0.1.0] —
   pending`: replace `pending` with the real release date, and point the
   changelog's two link definitions at the `releases/tag/v0.1.0` and
   `compare/v0.1.0...HEAD` URLs — they deliberately point at `commits/main`
   while no tag exists, because both of those URLs 404 until it does.
3. **Verify from a checkout** — everything the release claims, before the tag
   exists:

   ```bash
   ruff check . && ruff format --check . && mypy && lint-imports
   pytest
   AHP_INTEROP_REQUIRED=1 pytest -m interop   # needs Node and the npm client
   rm -rf dist && python -m build && twine check dist/*
   ```

   And the wheel, from outside the source tree, the way a user gets it:

   ```bash
   python -m venv /tmp/fresh
   /tmp/fresh/bin/pip install "agent-host-protocol @ git+https://github.com/jackhumbert/agent-host-protocol-py"
   /tmp/fresh/bin/pip install "$(echo dist/*.whl)[ws]"
   cd /tmp && /tmp/fresh/bin/python "$OLDPWD/scripts/smoke_wheel.py"
   ```

4. **Push, and wait for CI green** — the matrix runs every supported Python,
   which is more than any local machine does.
5. **Tag.**

   ```bash
   git tag v0.1.0 && git push origin v0.1.0
   ```

   The workflow takes it from here: build → tag/version gate → wheel smoke
   (dependency resolved from its repository, which is exactly the documented
   install) → GitHub release with the CHANGELOG section as its notes and the
   built distributions attached.
6. **Verify the thing users get**, not the thing you built:

   ```bash
   python -m venv /tmp/from-github
   /tmp/from-github/bin/pip install "agent-host-protocol @ git+https://github.com/jackhumbert/agent-host-protocol-py@<its latest tag>"
   /tmp/from-github/bin/pip install "agent-host-server[ws] @ git+https://github.com/jackhumbert/agent-host-server-py@v0.1.0"
   /tmp/from-github/bin/agent-host-server --help
   ```

7. **Open the next cycle:** bump `__version__` to the next `.dev0`, and leave
   the fresh `## [Unreleased]` heading in the CHANGELOG to accumulate.

## If a release goes wrong after the tag

All of it is recoverable, which is one of the reasons the release stops at
GitHub: delete the release, delete the tag, fix, re-tag. Prefer a patch bump
anyway once anyone may plausibly have installed the tag — a moved tag is a
lie to every environment that already resolved it — but nothing is spent
forever the way an index upload would be.
