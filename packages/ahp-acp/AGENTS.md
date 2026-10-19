# Agent guide

A standalone ACP *client*: it spawns an agent and speaks JSON-RPC over stdio.
It is the floor under any ACP-speaking package here, so it depends on nothing:
standard library only, and an import-linter contract forbids importing any other
`ahp_*` package. Keep it that way.

- `connection.py` is the transport. Notifications are handled in order before the
  next line is read; requests from the agent run on their own tasks. Keep both,
  and never await a *request* inside a notification handler: the read loop is
  waiting on it, so the answer could never be read.
- Lines are read in chunks, not with `readline`, so an over-long line can be
  skipped to its newline. Shutdown order is close stdin, SIGTERM, SIGKILL, each
  after a grace; with a process group the signals go to the group.
- `updates.py` parses `session/update` and must never raise. An unknown kind is
  `UnknownUpdate`. The schema drift test (`tests/test_updates.py`) is the
  guard: a new kind in the vendored schema must be typed or added to
  `DELIBERATELY_UNTYPED` on purpose.
- `client.py` holds no policy: what a permission request gets is the caller's
  decision (`allow_once`/`reject_once` only pick the agent's own option, and
  answer `cancelled` rather than a broader grant when it offers none).
  `cancel()` settles pending permission requests as `cancelled` first.
- Field names come from `tests/fixtures/schema/schema.json`, not from memory.
- Tests run `tests/fake_agent.py` as a real subprocess; no network, no real
  agent. Its script format is in the module docstring.
- Conventional commits; `CHANGELOG.md` under `[Unreleased]`.

## Keep it generic

This repository is public: nothing tracked may name or depend on one particular
setup (hostnames, domains, home paths, tokens, employers, internal projects).
Use placeholders, and `_example/...` for extension method names.
