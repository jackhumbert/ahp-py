# Agent guide

For AI agents and humans building this package. Assume the reader starts with
zero context; the repository's [`AGENTS.md`](../../AGENTS.md) covers the whole
monorepo.

## Keep it generic

This repository is public. It is a library for anyone to embed or run against
their own deployment, so nothing tracked may name or depend on one particular
setup: no hostnames, machine names, domains, home-directory paths, IP
addresses, tokens, employers or internal projects. Use placeholders
(`example.com`, `my-mac-mini`, `/Users/me`) in code, tests, docs *and* commit
messages — a commit message is as public as the code.

Deployment glue (service files, reverse-proxy config, container files, one
fleet's layout) belongs in the deployment, not here.

Anything an agent needs to know about the local setup lives in
`AGENTS.local.md`, gitignored by `*.local.*`. Read it if it exists; never copy
from it into a tracked file.

## What this package is

A remote MCP server whose tools drive AHP sessions. It is an AHP **client**,
nothing more: to a gateway it is one more surface, to a lone host one more
client. The README lists the tools.

```
 ahp-protocol ◀── ahp-client ◀── ahp-mcp ──▶ mcp (the MCP Python SDK)
```

## Layering (import-linter, `pyproject.toml`)

- **`fleet`** speaks AHP and knows nothing of MCP. Every tool's behaviour is a
  `Fleet` method taking and returning plain data.
- **`auth`** is OAuth and knows nothing of AHP. It answers "who is this
  bearer", and nothing about what they may do on a machine: that is the
  endpoint's admission (the gateway's registry) and the machine's own policy.
- **`server`** joins them: tool names, schemas and descriptions, and the HTTP app.
- Nothing here imports `ahp_host` or `ahp_gateway`. They are dev dependencies,
  for the tests' in-process fleet only. Reading session state the way the
  gateway's orchestrator does is copied into `fleet`, not imported.

## Invariants

1. **No tool answers an approval.** A question a session raises waits for a
   person in an AHP client. Adding a tool that approves would hand a chat
   model the one decision the fleet keeps for people.
2. **The model cannot widen a session.** Its config is the server's
   `session_config`, set by the operator.
3. **Secrets never travel as arguments**, are never logged, and tokens are
   stored only as digests.
4. **Nothing is issued without the owner's passphrase.** Registration is open
   because the MCP flow needs it; authorization is not.

## Conventions

- Conventional commits, scoped `…(ahp-mcp): …`. `CHANGELOG.md` from the first
  release.
- Every claim about behaviour is backed by a test; the tests use real peers
  (stock hosts behind a real gateway), not fakes of AHP.
- Docstrings say why, not what.
