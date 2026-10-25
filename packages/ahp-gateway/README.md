# ahp-gateway

A federated gateway for the [Agent Host Protocol](https://github.com/jackhumbert/ahp-py/tree/main/packages/ahp-protocol)
(AHP): one endpoint and one login behind which a developer sees a single flat
"my sessions everywhere" view across every box that runs an agent for them.

It is an AHP **host** facing the surfaces (web, Windows, CLI) and an AHP
**client** facing each node. Nodes implement the AHP server only; surfaces
implement the AHP client only; the gateway implements both, and is the only
component that does. The data plane is AHP, unmodified - a stock AHP client
still works against a bare node with no fleet in sight.

It is the newest member of the family:

```
                ahp-protocol              ← the shared layer
                  ▲        ▲         ▲
                  │        │         │
        ahp-host │ ahp-client
                  ▲       │       ▲        ← the gateway is BOTH
                  └───────┴───────┘
                    ahp-gateway       ← this repo
```

Status: pre-alpha. The multiplexer (build-order unit 1) works end to end:
stock clients through the gateway to stock hosts, in-process and over
WebSocket. A surface sees one session list, one agent list (a shared agent
offering only what every machine running it can do), one automation catalogue
and one telemetry stream per OTel signal, and every node's files, chats and
diff content route back to the machine they live on.
[`docs/plan.md`](docs/plan.md) is the design; §9 records what is built and
what is not.

## Refusing client-local folders

By default a new session whose folder is a `file:` URI naming no connected node
(`file:///typo/srv/repo`, bare `file:///`) is passed through as a client-local
path and routed by provider, so it opens at the default root of the first node
offering the agent. `Gateway(client_files="refuse")` answers InvalidParams
"no node 'typo' is available to this connection" instead, the same refusal as
for an `ahp-file` URI naming an unknown node. A `vscode-agent-host:` folder is
treated as `file:`, and both schemes match in any case (`FILE:///`). The
session-config commands are checked too, before routing, so even a one-node
fleet refuses. A folder naming a connected node (`file:///<node>/...` or
`ahp-file:///<node>/...`) routes as before.

## Flat places

An editor labels a folder, in a working-directory chip or a folder picker, by the
last segment of its URI and ignores host-provided display names. With the default
nested layout (`ahp-file:///<node>/<root>/...`) picking node `node-a` then root
`proj` shows just `proj`. Pass `Gateway(..., flat_places=True)` (default off) to
fold the node into that segment: `file:///<root>/<rest>` on node `node-a` is
`ahp-file:///node-a: proj/<rest>`, and the top of the tree lists one
`node-a: proj` entry per root each node serves (read from the node's own
top-level listing). A node with no named roots stays a bare `ahp-file:///<node>`,
and its files keep the nested `ahp-file:///<node>/<rest>` spelling both ways
(whether it serves named roots is read once, when the node is dialed).
Inbound, a first segment without `": "` keeps its nested meaning, so older URIs
still route. Node ids may not contain `": "`; the gateway refuses to admit one.

`flat_places` may also be a callable taking the authenticated `Principal` and
returning a bool. It is resolved once per connection, right after authentication,
so an embedder can choose per person: a connection narrowed to a single node has
no need to fold the node into the folder name, and can get plain root names.

## Passthrough

When an embedder narrows a connection to one file-serving node, translating that
node's paths into the aggregated namespace is pointless. Pass
`Gateway(..., passthrough_node="node-a")` (or a callable taking the authenticated
`Principal` and returning a node id or `None`; default `None`) and on that
connection `file:` URIs to and from that node cross the gateway verbatim: the
client sees the node's real absolute paths, the root `defaultDirectory` is the
node's own, and `client_files="refuse"` does not apply (every `file:` URI is that
node's). `ahp-file` URIs still route as before, so an older client URI keeps
working, and other nodes on the connection are reachable only by explicit `ahp-file`
URIs (the client's `file:///<node>/...` alias is not read there: a plain `file:` path is
always the passthrough node's, so a request or action carrying one that is bound
for another node is refused: InvalidParams, or the action echoed rejected). It is
resolved once per connection after authentication; an id the principal is not
admitted to is ignored. `flat_places` does not affect the passthrough node.

## Fleet tools: the orchestrator and flow sessions

With an `OrchestratorConfig` (`Gateway(orchestrator=...)`), the gateway joins
two kinds of session as an active client offering **fleet tools**, and runs
them over supervised links of its own, one per (principal, node), so they keep
working after the surface that started the session goes away
([`docs/plan.md`](docs/plan.md) §10):

- **Orchestrator sessions** (the `orchestrator` agent it adds to the merged
  list) get tools for a model: list machines and folders, start, message, read,
  wait for and stop sessions.
- **Flow sessions** (agents named in `flow_agents`, default `["flow"]`, such as
  `ahp-host-flow`'s workflow runs) get four tools for a workflow engine rather
  than a model: `start_session`, `wait_for_sessions`, `read_session` and
  `stop_session`, each taking and returning plain data.

What holds for a flow session, whatever the engine asks:

- **The principal who started it.** Every session its tools start is started
  as that principal, only on nodes the registry admits them to. A run can only
  wait on, read or stop the sessions it started. Another principal can neither
  prompt the flow session nor read or drive (prompt, cancel, answer) a session
  its run started: they are listed like any session, and refused.
- **Delegation.** `start_session` takes optional `originChat` and
  `originToolCall`; with both, the node session is created with a delegation
  `origin` (`{kind: "ahp-py/delegation", session: <the flow session>, chat,
  toolCallId}`, plain AHP `_meta` on `createSession`), so a client can nest it
  under the call that started it.
- **Config.** A started session gets `child_config` (by default
  `{"permissionMode": "auto"}`). A requested `permissionMode` may only be
  stricter (`plan` < `default` < `acceptEdits` < `auto` < `bypassPermissions`);
  a looser one fails the call. With `child_config` holding no `permissionMode`,
  only `plan` and `default` pass. A requested `effort` (`low` to `max`) is
  passed on unless `child_config` sets `effort`, in which case the request is
  dropped silently and `child_config`'s applies. No other key is accepted.
- **Limits.** At most `max_running` of one flow session's sessions run at
  once, counted per flow session (all its runs), as an orchestrator session's
  are, not per principal. A `start_session` over the limit waits for a slot, up
  to `slot_wait` seconds (default 600), then fails. The count is read from the
  nodes, so it holds across a gateway restart. `OrchestratorConfig.
  max_running_per_principal` (default none) also bounds what one principal runs
  across all its flow sessions: a flow `start_session` waits, up to `slot_wait`,
  until both counts are under their limits, then fails with "N sessions of yours
  are still running across your runs"; one principal's starts are serialised, so
  two of its flow sessions cannot both pass. The orchestrator's own start path is
  unchanged. Nothing answers an approval: a
  prompt raised in a started session waits for a person, in any client.
- **Restarts.** Flow sessions and what they started are kept in `state_path`;
  a restarted gateway rejoins each flow session with its tools, and a link that
  drops rejoins too.

## Install

The family is distributed from GitHub, not from an index. The protocol package
is the floor and installs first:

```bash
pip install "ahp-protocol @ git+https://github.com/jackhumbert/ahp-py#subdirectory=packages/ahp-protocol"
pip install "ahp-host @ git+https://github.com/jackhumbert/ahp-py#subdirectory=packages/ahp-host"
pip install "ahp-client @ git+https://github.com/jackhumbert/ahp-py#subdirectory=packages/ahp-client"
pip install "ahp-gateway @ git+https://github.com/jackhumbert/ahp-py#subdirectory=packages/ahp-gateway"
```

## Development

```bash
pip install -e '.[ws]'
pip install --group dev
pytest
mypy
ruff check . && ruff format --check .
lint-imports
```

## License

MIT - see [LICENSE](LICENSE).
