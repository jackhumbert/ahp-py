# Plan

The design of the broker, captured from a design conversation about running
agent fleets across many machines.

## 1. What this is

A Python implementation of the one new component in a federated AHP fleet:
the **broker**. It implements **both** AHP edges:

- an AHP **host** facing the surfaces (web, Windows, CLI), so every surface
  speaks plain AHP to one endpoint behind one login, and
- an AHP **client** facing each node, because AHP is strictly
  client-dials-host and the agent loop, filesystem and terminal are all
  host-side.

Nodes implement the AHP server only. Surfaces implement the AHP client only.
The broker implements both, and is the only thing in the family that does.

```
web / windows / cli  ──AHP client──▶  agent-host-broker  ──AHP client──▶  dial-in node (server room / VM)
                                        │  SSO auth                      ──AHP client──▶  another reachable node
                                        │  node registry + per-node authz
                                        │  session→node routing / aggregation
                                        └──relay──◀──outbound registrar──  laptop / roaming VM node
```

## 2. Two planes

Keep AHP as the unmodified **data plane** so any stock AHP client (VS Code)
still works against a bare node with no fleet in sight. All fleet logic lives
in this **control plane**:

| Layer | Concern | New protocol? |
| --- | --- | --- |
| Below AHP (transport) | relay / reverse-tunnel for NAT'd nodes | No - a tunnel carrying AHP frames verbatim (yamux / HTTP-CONNECT / `ssh -R` / wireguard) |
| AHP itself (data plane) | turns, approvals, resources, terminal | **Unchanged** - broker↔node is just AHP |
| Beside AHP (control plane) | registry, SSO authz, health, routing/aggregation | Not a wire protocol - a directory + REST the broker owns |

The only new wire contract this project owns is the **dial-out node's
registration + heartbeat handshake** ("I'm `<node>`, here's my
identity/roots, keep this tunnel alive"). Everything session-level rides AHP
untouched.

Federation stays **out of the AHP packages** even though the family is owned
by the same people: a lone node must stay speakable by a stock AHP client,
and coupling "talk to one host" to "talk to the fleet" would force every
client to carry the extensions.

## 3. Reachability splits the nodes

- **Datacenter / VM nodes** - dial-in. The broker connects to them as a
  client over the network, behind a reverse proxy; a PKI wildcard certificate
  covers the fleet's domain. Server-only on the node.
- **Laptops / roaming VMs** - NAT'd, cannot be dialed. The node reaches
  *out*: an outbound registrar dials the broker and the broker tunnels the
  AHP WS back through that pipe. The only place a node runs client-shaped
  code, and it is the relay, not AHP.

Open fork (§6): relay-everything vs. direct-for-reachable. Starting lean:
relay-everything for uniformity, direct-connect as an optimization once
latency/load justifies it.

## 4. Trust model

Run each node's AHP host **as the signed-in developer's own OS account**,
not a shared service user. Then filesystem + terminal reach is that user's
real OS permissions, which sidesteps the reason a shared-service deployment
must ship read-only stubs: running as a service account and rooting the
provider at a shared directory would leak that directory's secrets to every
session.

Division of authz: **the OS** gates files/shell on a node; **the broker's
identity check** gates whether you may start a session on that node at all.
This also retires a static bearer token as the fleet's gate: per-node authz
keys on identity-provider groups/app-roles, the same mechanism that gates
the surfaces.

## 5. Package layout

```
src/agent_host_broker/
  core/       interfaces: node connection, session route, aggregated namespace
  registry/   node records + per-node admission (stays offline)
  ws/         concrete WebSocket transports for both edges
  relay/      dial-out registrar endpoint + tunnel
```

Layering is enforced by import-linter (see pyproject.toml): `core` is the
floor its edges plug into; `registry` answers "may user X start on node Y"
without ever importing a transport.

## 6. Open forks (decide with evidence, not in advance)

1. **Relay-everything vs. direct-for-reachable** (§3). Lean: relay-first.
2. **Transparent per-node proxy vs. single aggregated AHP namespace.** The
   aggregated namespace is what gives the "one flat session list" UX; it
   needs the node encoded in session URIs (or the working-directory
   authority) and a merged `listSessions`. Lean: aggregated namespace.
3. **Web client: JS AHP client vs. a thinner normalized WS/HTTP API behind
   the broker.** Not this repo's decision alone - the JS client is its own
   deliverable.

## 7. Build order (candidate)

Each unit below is independently shippable:

1. `agent-host-broker` multiplexer - host edge + client edge + routing.
2. Node registry + per-node authz backed by the identity provider (a
   declarative node inventory).
3. Real resource/terminal backends + per-user host launch on a node.
4. Dial-out registrar + relay - the only new wire contract.
5. Web AHP client (the Python client covers the CLI; VS Code ships its own).

This repo is (1), with (2)'s interfaces stubbed in `core/` and `registry/`.

## 8. What a milestone must prove

- A stock AHP client can still speak to a bare node; nothing in this repo
  leaks into the AHP packages.
- The surfaces never learn federation exists: one endpoint, one login, one
  flat session list.
- Broker↔node traffic is indistinguishable from any AHP client against any
  AHP host - conformance evidence comes from the sibling suites, not from
  new fixtures.

## 9. Milestone 1: the multiplexer (as built)

`agent_host_broker.core.Broker` is §7 unit (1). What it does, and the
decisions it rests on:

**The host edge is a frame router.** `agent-host-server`'s `Host` owns
sessions, turns, terminals and resources itself, and its only plug-in point
is the agent loop. A broker built on it would re-host every session and
bridge the node's approvals, terminals and resources through that one seam.
So `Broker.serve(transport, ...)` is its own AHP endpoint that relays JSON-RPC
frames. It keeps `Host.serve`'s signature, so `agent-host-server`'s WebSocket
server can serve it (`agent_host_broker.ws.serve_broker`).

**One node link per surface connection.** At `initialize`, the broker
authenticates the peer, asks the registry which nodes admit the principal,
and opens one `AhpClient` link to each. The handshake uses the surface's own
`clientId` and the protocol version negotiated with the surface. Nodes the
principal is not admitted to are never dialed. A node that cannot be reached
is left out; the connection still succeeds.

**Routing (fork 2 settled: aggregated namespace).** Channel URIs are opaque
and client-chosen, so they are never rewritten. The broker learns which node
owns each one from the node's own payloads. File URIs are each node's own path
space, so the surfaces never see them as `file:` at all - `file:` would promise
a file on the client's machine, and a client's genuine `file:` URI (a local
attachment) must never be mistaken for a node's. They see the broker's own
scheme instead, translated back to the node's `file:///` on the way in:

- `ahp-file:///<node>/<rel>` - under the node's root, the `defaultDirectory`
  it advertised. `ahp-file:///` is a directory the broker answers itself, one
  entry per connected node, and `ahp-file:///<node>` is that node's root, so a
  folder picker walks from "which machine" straight into its projects. It is
  the surfaces' `defaultDirectory` whenever more than one node is connected
  (with one, it is that node's root). `..` is refused.
- `ahp-file://<node>/<absolute path>` - anything on the node outside its root.
- VS Code keeps only the *path* of a host's `defaultDirectory` and browses it
  as `file:`, so it sends the tree as `file:///<node>/<rel>` (and `file:///` for
  the list of nodes). Inbound, a `file:` URI whose first segment is a node id
  is read as that node's `ahp-file` URI. A client's genuine local path that
  starts with `/<node id>/` would be misread; node ids make that unlikely.

A request routes by, in order:

1. a channel the broker knows the owner of;
2. the node an `ahp-file` URI names (`workingDirectories`, `workingDirectory`,
   `uri`, `root`, `cwd`);
3. the one node offering the named provider;
4. the only node, if there is one.

Where several nodes run the same agent (the same provider id on two
machines), the surface sees one agent - the first node's entry with every
node's models - and the folder picks the machine. A request that names no
folder yet - a plain chat's `createSession`, or the `resolveSessionConfig` /
`sessionConfigCompletions` a client asks before a folder is chosen - goes to
the first connected node offering it, in inventory (node id) order. Anything
else is refused as ambiguous rather than guessed.

**One `serverSeq`.** Every node action is restamped from the broker's own
counter, and every snapshot's `fromSeq` is translated to the stamp of that
link's last action at or before it. Actions that reach the broker for a
channel before its snapshot are held, and are released after the reply.

**The root channel is merged**, not relayed: agents are the union (the first
node wins a shared provider id), `activeSessions` is the sum, and `terminals`
is the concatenation. `config` is never advertised. The handshake extras
`completionTriggerCharacters` and `terminalCommandPrefix` are advertised only
when every node agrees on them (invariant 4).

**`listSessions`** fans out and merges newest-first. The cursor records each
node's own cursor and offset, so it is stateless and exact across page
boundaries, including when a node returns short pages.

**Reconnect: always the snapshot arm.** The broker keeps no replay log and
needs none. A `reconnect` is authenticated and admitted exactly like an
`initialize`, links are opened to every admitted node, and every channel in
`subscriptions` is re-read from its node. A channel no node has is left out,
which is how the snapshot arm reports it gone. The new connection's counter
starts at the surface's `lastSeenServerSeq`, because the reply carries no
`serverSeq` of its own. A reconnecting connection has never seen which node
owns which channel, so a channel no node has named yet is found by asking
each node for it: a node that does not have a channel answers `subscribe`
with no snapshot, which is plain AHP. The broker process therefore keeps no
state, and a surface can reconnect to a different broker instance.

**Node recovery: redial, then bounce.** A node whose link drops, or that was
unreachable at the handshake, is redialed in the background with doubling
backoff (`Broker(redial_backoff=(first, ceiling))`). While it is gone its
agents leave the merged root and requests for its channels are refused as
"not connected", never rerouted to another node. When it answers again, the
broker closes the surface's connection on purpose. The surface's own
reconnect then re-reads every channel, that node's included, from fresh
snapshots. AHP has no server-pushed re-snapshot, so this is the only way to
repair a surface's state without inventing wire semantics. The cost is a
reconnect on every node for that surface, and any request in flight at that
moment fails. The stock client prunes any subscription a snapshot reply
leaves out, which is why the bounce waits for the node to come back instead
of happening at the moment of loss.

**Not yet:**

- `authenticate` and the other root-level commands when more than one node
  could answer them: refused as ambiguous.
- Clients that never send `reconnect` (a surface that only ever
  `initialize`s) resync after a bounce through `initialize` instead; nothing
  is lost, but that client re-subscribes on its own.

**Gaps that belong in the siblings:**

- `AhpClient` forwards only the notification methods it models, so a new
  upstream method would be dropped at the node edge.
- `AhpClient`'s `events()` tap is bounded. A drop closes the link rather than
  leave a surface on a silently wrong mirror.
- `WebSocketServer` is typed to take a concrete `Host`. `serve_broker` casts
  around it; typing that parameter as a protocol with a `serve` method would
  remove the cast.
