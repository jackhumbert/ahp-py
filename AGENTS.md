# Agent guide

For AI agents and humans maintaining this repository. Assume the reader starts
with zero context.

This is a monorepo of Python packages for the Agent Host Protocol; see
[`README.md`](README.md) for the list and how they layer. Each package under
`packages/` keeps its own `AGENTS.md`, which governs work inside it — read the
one for every package you touch.

## Working across packages

- A spec bump or a change to a shared surface lands in **one commit across
  every package it touches**, not package by package.
- Layering only points down: `ahp-protocol` depends on nothing here; the host,
  client and gateway depend on it; the providers depend on the host. Never
  add an upward or sideways import.
- Each package keeps its own `pyproject.toml`, `CHANGELOG.md` and version.
- Tags are namespaced by package: `ahp-host/v0.1.0`, not `v0.1.0`.
- Conventional commits, scoped by package where it helps: `fix(ahp-host): …`.

## Keep it generic

This repository is public. It is a library for anyone to embed or run against
their own deployment, so nothing tracked may name or depend on one particular
setup: no hostnames, machine names, domains, home-directory paths, IP
addresses, tokens, employers or internal projects. Use placeholders
(`example.com`, `my-mac-mini`, `/Users/me`) in code, tests, docs *and* commit
messages — a commit message is as public as the code.

Deployment glue (service files, reverse-proxy config, one fleet's layout)
belongs in the deployment, not here. A feature one setup needs is generalised
into an option or left out.

Anything an agent needs to know about the local setup lives in
`AGENTS.local.md`, gitignored by `*.local.*`. Read it if it exists; never copy
from it into a tracked file.

