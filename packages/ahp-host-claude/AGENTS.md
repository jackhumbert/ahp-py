# Agent guide

An `agent-host-server` provider adapter, living in its own distribution as the
server's AGENTS.md requires ("Adding a provider adapter").

- `provider.py` translates the Claude Agent SDK's message stream into the
  host's neutral `TurnSink` events. Never emit AHP actions directly; the host
  owns ordering and the wire.
- `permissions.py` is the approval policy. Changing what runs without asking is
  a security decision: say so in the commit, and test it.
- Tests use a fake SDK client (`tests/fakes.py`); no network, no subprocess.
- Conventional commits; `CHANGELOG.md` under `[Unreleased]`.
