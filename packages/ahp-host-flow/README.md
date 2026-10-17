# ahp-host-flow

Run Claude Code workflow scripts (`agent()`, `parallel()`, `pipeline()`,
`phase()`, …) as AHP sessions. Each `agent()` call is a child chat driven by
any provider the host can load: Claude through `ahp-host-claude`, or any ACP
agent through `ahp-host-acp`.

Status: pre-alpha. M0 is in: `flow run script.js --args '{"who": "world"}'` runs a workflow script with an echo agent and prints its result (Node >= 20, or set `AHP_FLOW_NODE`). See [docs/plan.md](docs/plan.md).
