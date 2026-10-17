# Changelog

## [Unreleased]

### Added

- Package skeleton and plan (`docs/plan.md`).
- Node discovery for the script sandbox: `AHP_FLOW_NODE`, else `node` on `PATH`; Node >= 20.
- acorn 8.19.0 (MIT), vendored under `sandbox/vendor/` to read `meta` without running a script.
- `engine.Dispatcher` (one `agent()` call in, its result out) and `EchoDispatcher`, which answers each call with its prompt.
- Script sandbox: a Node subprocess speaking line-delimited JSON-RPC; `meta` is read with acorn and must be a pure literal, without running the script.
- Scripts run in a fresh `node:vm` context per run. `agent()`, `log()` and `phase()` cross to Python, `args` passes through verbatim, and the context has a null-prototype global, so no Node API or host-realm object is reachable through constructors. This enforces determinism and is not a security boundary for hostile scripts. Messages to Node are ASCII-only JSON, so U+2028/U+2029 cannot split a frame.
- `Date.now()`, `Math.random()`, `new Date()` and `Date()` throw inside a script; dates built from explicit values still work.
- `flow run <path> [--args JSON] [--node PATH]`: runs a script with an echo agent and prints its return value as JSON.
- Spike: an inner provider session's turn runs in a worker chat (`open_tool_chat`), with its publisher scoped to that chat (integration test over the in-memory host).
- `docs/plan.md` §8 records the M0 spike result.
