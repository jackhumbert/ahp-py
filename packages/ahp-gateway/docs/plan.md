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
