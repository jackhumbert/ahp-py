# Releasing

The release itself is one action: pushing a `v*` tag. Everything after the tag
is [`release.yml`](.github/workflows/release.yml) — rebuild, re-check, smoke
the wheel against the real index, publish to PyPI via trusted publishing, then
cut a GitHub release whose notes are the changelog section verbatim. Nothing in
the pipeline needs a secret; there is no API token to rotate or leak.

## Ordering, or why a release here can fail on purpose

This package pins `agent-host-protocol ~= 0.1.0`. The release workflow installs
the built wheel **from PyPI's point of view** — real resolution, no sibling
checkouts — so while the protocol package is unpublished, the release fails at
the install step, before anything irreversible. That is the intended sequence,
not an accident to work around:

1. `agent-host-protocol` publishes first.
2. `agent-host-server` and this client release against it, in either order.

Do not "fix" a failing release by teaching `release.yml` about sibling
checkouts; a wheel that cannot install from the index alone is not releasable.

## One-time setup (before the first tag)

1. On PyPI, create the `agent-host-client` project (or reserve it at first
   publish) and add a **trusted publisher**: owner `jackhumbert`, repository
   `agent-host-client-py`, workflow `release.yml`, environment `pypi`.
2. In the GitHub repository settings, create the `pypi` environment. Optional
   but worth it: require a reviewer on it, which turns "publish" into a
   two-person action without touching the workflow.

## Cutting a release

1. **Roll the changelog.** Rename `## [Unreleased]` to `## [X.Y.Z] - YYYY-MM-DD`
   and start a fresh empty `[Unreleased]` above it. The release notes are
   extracted from exactly the `## [X.Y.Z]` heading — the workflow fails if the
   section is missing or empty.
2. **Set the version.** `__version__` in
   [`src/agent_host_client/__init__.py`](src/agent_host_client/__init__.py) is
   the only place it is written; drop the `.devN` suffix. The workflow refuses
   a tag that disagrees with `__version__`.
3. **First release only:** flip the `Development Status` classifier in
   `pyproject.toml` (Pre-Alpha → Alpha) and replace the README's "not
   published" status banner with an install section — both say "not on PyPI"
   today because it is true today, and shipping a release that still says so
   contradicts `AGENTS.md`'s documentation rule.
4. **Run the gate locally** — the same commands CI runs:
   `pytest && ruff check . && ruff format --check . && mypy && lint-imports`.
5. **Commit, tag, push.** Conventional commit (`release: v0.1.0`), then
   `git tag v0.1.0 && git push origin main v0.1.0`.
6. **Watch the workflow.** Build → `pypi` environment (approval, if you
   configured a reviewer) → publish → GitHub release. Then prove the result the
   way a user will: `pip install agent-host-client[ws]` into a scratch venv.
7. **Open the next cycle.** Bump `__version__` to the next `X.Y.Z.dev0` in a
   follow-up commit so a stray build from `main` can never impersonate a
   release.

## Versioning

SemVer, independent of the protocol's versioning; every release states the
protocol versions it speaks in the changelog's `Notes` section
(`DEFAULT_SUPPORTED_VERSIONS` is the source of truth, and `tests/docs/`
asserts the two agree).
