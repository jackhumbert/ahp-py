# Releasing

The release itself is one action: pushing a `v*` tag. Everything after the tag
is [`release.yml`](.github/workflows/release.yml) — rebuild, re-check, smoke
the wheel along the documented install path, then a GitHub release whose notes
are the changelog section verbatim, with the built wheel and sdist attached.
**The GitHub release is the whole release**: this family of packages is public
on GitHub and deliberately not on PyPI. Nothing in the pipeline needs a
secret, an environment, or any index-side setup.

## How people install it

```bash
pip install "agent-host-protocol @ git+https://github.com/jackhumbert/agent-host-protocol-py"
pip install "agent-host-client[ws] @ git+https://github.com/jackhumbert/agent-host-client-py"
```

Pinned by tag (`…-py@v0.1.0`) when they want a release rather than `main` —
though today no tag exists in any of the three repositories: **`v0.1.0` is the
pending first release for the whole family**, so both lines currently install
`main`. The order matters and is the closest thing to a cross-repo constraint
left: the
`~=` pin on `agent-host-protocol` resolves against what is already installed,
and pip cannot fetch that name from an index that does not carry it — skipping
the first line fails with `No matching distribution found for
agent-host-protocol`. The release workflow smokes exactly this two-line path,
so a release cut while the protocol repository is unreachable or incompatible
fails before the release exists.

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
   `pyproject.toml` (Pre-Alpha → Alpha) and update the README's status banner —
   it currently says the API is unstable and nothing is tagged, and shipping a
   release that still says so contradicts `AGENTS.md`'s documentation rule.
4. **Run the gate locally** — the same commands CI runs:
   `pytest && ruff check . && ruff format --check . && mypy && lint-imports`.
5. **Commit, tag, push.** Conventional commit (`release: v0.1.0`), then
   `git tag v0.1.0 && git push origin main v0.1.0`.
6. **Watch the workflow, then verify the thing users get** — the two install
   lines above, pinned to the new tag, in a scratch venv; then
   `python -c "import agent_host_client; print(agent_host_client.__version__)"`.
7. **Open the next cycle.** Bump `__version__` to the next `X.Y.Z.dev0` in a
   follow-up commit so a stray build from `main` can never impersonate a
   release.

## If a release goes wrong after the tag

Delete the GitHub release, delete the tag, fix, re-tag — nothing is spent
forever, which is one of the reasons the release stops at GitHub. Prefer a
patch bump anyway once anyone may plausibly have installed the tag: a moved
tag is a lie to every environment that already resolved it.

## Versioning

SemVer, independent of the protocol's versioning; every release states the
protocol versions it speaks in the changelog's `Notes` section
(`DEFAULT_SUPPORTED_VERSIONS` is the source of truth, and `tests/docs/`
asserts the two agree).
