# Releasing

The release itself is one action: pushing an `ahp-client/v*` tag. Everything after the tag
is the repository's [`release.yml`](../../.github/workflows/release.yml) — rebuild, re-check, smoke
the wheel along the documented install path, then a GitHub release whose notes
are the changelog section verbatim, with the built wheel and sdist attached.
**The GitHub release is the whole release**: this family of packages is public
on GitHub and deliberately not on PyPI. Nothing in the pipeline needs a
secret, an environment, or any index-side setup.

## Release order

Release in dependency order: ahp-protocol, ahp-host, ahp-client, ahp-acp, then ahp-gateway,
ahp-host-claude, ahp-host-acp and ahp-host-flow. A package's `~=` pins resolve against siblings
already tagged. Tagging and pushing tags is done by the maintainer, not by a build run.

## How people install it

```bash
pip install "ahp-protocol @ git+https://github.com/jackhumbert/ahp-py#subdirectory=packages/ahp-protocol"
pip install "ahp-client[ws] @ git+https://github.com/jackhumbert/ahp-py#subdirectory=packages/ahp-client"
```

Pinned by tag (`…/ahp-py@ahp-client/v0.1.0#subdirectory=…`) when they want a
release rather than `main` — though today no package has been tagged: **`v0.1.0` is the
pending first release for the whole family**, so both lines currently install
`main`. The order matters and is the closest thing to a cross-package constraint
left: the
`~=` pin on `ahp-protocol` resolves against what is already installed,
and pip cannot fetch that name from an index that does not carry it — skipping
the first line fails with `No matching distribution found for
ahp-protocol`. The release workflow smokes exactly this two-line path,
so a release cut against an incompatible protocol package fails before the
release exists.

## Cutting a release

1. **Date the changelog section when tagging; this is done by the maintainer.** The release
   section reads `## [X.Y.Z] - unreleased` until then; set it to the tag's day,
   `## [X.Y.Z] - YYYY-MM-DD`. The release notes are extracted from the `## [X.Y.Z]` heading;
   the workflow fails if the section is missing or empty. For 0.1.0 the section and
   `__version__` are already in place. For a later release, first rename `## [Unreleased]` to
   `## [X.Y.Z] - unreleased`, start a fresh empty `[Unreleased]` above it, and set `__version__`
   in [`src/ahp_client/__init__.py`](src/ahp_client/__init__.py), the only place it is written
   (drop the `.devN` suffix; the workflow refuses a tag that disagrees with it).
2. **First release only, a maintainer decision:** flip the `Development Status :: 2 - Pre-Alpha`
   classifier in `pyproject.toml` (to `3 - Alpha`) and update the README's status banner —
   it currently says the API is unstable and nothing is tagged, and shipping a
   release that still says so contradicts `AGENTS.md`'s documentation rule.
3. **Run the gate locally** — the same commands CI runs, from the repository
   root: `scripts/check.sh ahp-client`. Then build and smoke the wheel from outside the checkout: `uv build --package ahp-client --out-dir dist`, a fresh venv, and `cd /tmp && <venv>/bin/python "$OLDPWD/packages/ahp-client/scripts/smoke_wheel.py"`.
4. **Commit, tag, push; this is done by the maintainer.** Conventional commit (`release(ahp-client): v0.1.0`), then
   `git tag ahp-client/v0.1.0 && git push origin main ahp-client/v0.1.0`.
5. **Watch the workflow, then verify the thing users get** — the two install
   lines above, pinned to the new tag, in a scratch venv; then
   `python -c "import ahp_client; print(ahp_client.__version__)"`.
6. **Open the next cycle.** Bump `__version__` to the next `X.Y.Z.dev0` in a
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
