# ahp-host-flow

Run Claude Code workflow scripts (`agent()`, `parallel()`, `pipeline()`,
`phase()`, …) as AHP sessions. Each `agent()` call is a child chat driven by
any provider the host can load: Claude through `ahp-host-claude`, or any ACP
agent through `ahp-host-acp`.

Status: pre-alpha. M1 is in: `parallel`, `pipeline`, caps, `budget`, `workflow()` and timeouts run on an echo agent: `flow run script.js --args '{"who": "world"}' [--concurrency N] [--budget TOKENS] [--timeout SECONDS] [--workflows DIR]` (Node >= 20, or set `AHP_FLOW_NODE`). See [docs/plan.md](docs/plan.md) and [docs/format.md](docs/format.md).
