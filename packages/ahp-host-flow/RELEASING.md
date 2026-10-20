# Releasing

The release itself is one action: pushing an `ahp-host-flow/v*` tag. Everything after the tag
is the repository's [`release.yml`](../../.github/workflows/release.yml): rebuild, re-check, smoke
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
pip install "ahp-host @ git+https://github.com/jackhumbert/ahp-py#subdirectory=packages/ahp-host"
pip install "ahp-client @ git+https://github.com/jackhumbert/ahp-py#subdirectory=packages/ahp-client"
pip install "ahp-host-flow @ git+https://github.com/jackhumbert/ahp-py#subdirectory=packages/ahp-host-flow"
pip install "ahp-host-claude @ git+https://github.com/jackhumbert/ahp-py#subdirectory=packages/ahp-host-claude"
pip install "ahp-host-acp @ git+https://github.com/jackhumbert/ahp-py#subdirectory=packages/ahp-host-acp"
```

Pinned by tag (`...@ahp-host-flow/v0.1.0#subdirectory=...`) when they want a release
rather than `main`.

The `[claude]` and `[acp]` extras (`ahp-host-flow[claude]`, `ahp-host-flow[acp]`) pull
`ahp-host-claude` and `ahp-host-acp`; they are separate packages in the same repository,
so install their lines too (the last two above) when you use an extra.

Order matters: the `~=` pins on the sibling packages resolve against what is already
installed, and pip cannot fetch those names from an index that does not carry them.

## Cutting a release

1. **Date the changelog section when tagging; this is done by the maintainer.** The release
   section reads `## [X.Y.Z] - unreleased` until then; set it to the tag's day,
   `## [X.Y.Z] - YYYY-MM-DD`. The release notes are extracted from the `## [X.Y.Z]` heading;
   the workflow fails if the section is missing or empty. For 0.1.0 the section and
   `__version__` are already in place. For a later release, first rename `## [Unreleased]` to
   `## [X.Y.Z] - unreleased`, start a fresh empty `[Unreleased]` above it, and set `__version__`
   in [`src/ahp_host_flow/__init__.py`](src/ahp_host_flow/__init__.py), the only place it is written (drop
   the `.devN` suffix; the workflow refuses a tag that disagrees with it).
2. **First release only, a maintainer decision:** flip the `Development Status :: 2 - Pre-Alpha` classifier in `pyproject.toml` and update the README's pre-alpha status line, if the release is to claim more than pre-alpha.
3. **Run the gate locally**, from the repository root: `scripts/check.sh ahp-host-flow`.
4. **Build and smoke from outside the checkout.**
   ```bash
   uv build --package ahp-host-flow --out-dir dist
   python -m venv <venv> && <venv>/bin/pip install dist/ahp_host_flow-X.Y.Z-py3-none-any.whl
   cd /tmp && <venv>/bin/python "$OLDPWD/packages/ahp-host-flow/scripts/smoke_wheel.py"
   ```
5. **Tag and push; this is done by the maintainer.**
   `git tag ahp-host-flow/vX.Y.Z && git push origin ahp-host-flow/vX.Y.Z`.
6. **Watch the workflow, then verify the thing users get**: the install lines
   above, pinned to the new tag, in a scratch venv; then
   `python -c "import ahp_host_flow; print(ahp_host_flow.__version__)"`.
7. **Open the next cycle.** Bump `__version__` to `X.Y.Z+1.dev0` in a
   follow-up commit so a stray build from `main` can never impersonate a release.

## If a release goes wrong after the tag

Delete the GitHub release, delete the tag, fix, re-tag; nothing is spent
forever, which is one of the reasons the release stops at GitHub. Prefer a
patch bump anyway once anyone may plausibly have installed the tag: a moved
tag is a lie to every environment that already resolved it.

## Versioning

SemVer, independent of the protocol's versioning. Each tag gets a row in the
root [`COMPATIBILITY.md`](../../COMPATIBILITY.md).
