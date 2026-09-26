# ADR 0006 — The reverse direction is a first-class layer

**Status:** accepted · **Date:** 2026-08-02

## Context

AHP is symmetric. `ServerCommandMap` has **10** methods — the nine `resource*`
calls plus `createResourceWatch` — that a *host* issues against a *client*. On
top of that, a client owns tool execution when a tool call is marked
`contributor: {kind: "client"}`, owns answering elicitation and tool
confirmation, and owns serving `virtual://` plugin content it published.

No reference client finishes this. TypeScript ships a typed handler registry
with zero implementations. `ahpx` implements two of ten, read-only.

The consequence is not theoretical: publishing a `virtual://` plugin makes the
host call `resourceList`/`resourceRead` straight back at you, and a
forward-only client silently publishes plugins whose children never render.

## Decision

**`serve/` is a layer of equal weight to the forward direction**, not an
optional extra, and it is in 0.1.0.

- `ResourceRouter` — longest-prefix mount on `params["uri"]`; `-32601` for
  anything outside `ServerCommandMap`; `-32602` for a frame with no `uri`;
  `-32008` for an unmounted prefix. It answers both `handle(method, params)`
  and `__call__`, so it composes with `connect(resources=…)` and with
  `set_server_request_handler` alike.
- `FileResourceServer(roots, writable=False)` — `realpath` **then** re-check
  containment, so a symlink swapped between check and open cannot escape.
  `writable` is a second, separate opt-in, mirroring the host's posture:
  reading discloses, writing destroys, and the two should not be granted by the
  same gesture.
- `VirtualResourceServer` — in-memory, for client-published plugins.
- `ResourceWatchServer` — the client as a watch *server*. There is no dispose
  command; release when the last subscriber unsubscribes or the connection
  drops.
- `ClientToolHost` — client-owned tool execution.
- `InputResponder` — elicitation, tool confirmation, and
  `chat/toolCallResultConfirmed`, whose absence in `ahpx` hangs any turn using
  `requiresResultConfirmation` forever.

**With no handler installed, an inbound request answers `-32601`** rather than
being ignored, so the host never leaks a pending request.

## Risk

This surface is designed entirely from the protocol types, because there is no
implementation anywhere to check against. `resourceWrite`'s mode/position byte
semantics, `resourceRequest`'s permission negotiation, and the watch release
rule are prose. Expect them to be wrong in ways only a real host reveals, and
do not claim otherwise in the README.

**It was wrong, and a real host revealed it.** Driving the sibling Python host
against `serve/` found nine defects in one pass, every one a field name or a
semantic read straight off `commands.schema.json`: `content` where the schema
says `data` on both `resourceRead` and `resourceWrite` — so a conformant
caller's write **emptied the file and answered success** — `kind` where it says
`type` on `resourceList` and `resourceResolve`, epoch millis where it says ISO
8601, `resourceMkdir` creating the parent, `failIfExists` ignored on
copy/move, `resourceRequest` answering a successful `{"granted": false}` where
`PermissionDenied` is mandated, and a `ResourceRouter` that `connect(resources=…)`
could not install at all. The corrective is not "be more careful": it is to walk
the schema method by method, params and result, and pin each one with a test —
which is what `tests/client/test_serve.py` now does.

---

# ADR 0006b — Reconnect pending policy defaults to `VSCODE`

The spec, VS Code and Swift disagree about `pendingActions` across a reconnect,
and picking one silently would bury a real interoperability question.

| Source | Behaviour |
|---|---|
| `docs/guide/reconciliation.md:81` | clear pending in **both** the replay and snapshot arms |
| VS Code | re-send survivors on the replay arm |
| Swift | re-send always |

**`PendingPolicy.VSCODE` is the default:** on the **replay** arm, drop pending
entries the replay already acknowledged and re-send the survivors; on the
**snapshot** arm, clear pending entirely — they were predicated on pre-disconnect
state, and re-sending after a snapshot risks a duplicate `chat/turnStarted`.
`SPEC` clears in both arms. `RESEND_ALL` matches Swift.

The default is **judgement, not evidence.** A host that expects the spec's
behaviour will see a duplicate `chat/turnStarted` from our replay-arm re-send.
That is the trade, it is switchable in one field, and it is written down here so
the next person does not have to rediscover that the three references disagree.

**Keeping is re-sending, and the first implementation did only half of it.**
`StateMirror.on_reconnect` now *returns* every entry its policy kept, and
`HostRuntime._connect_once` puts each back on the wire through
`AhpClient.redispatch` — original `clientSeq`, ordered as dispatched, before the
state flips to `connected`. Keeping the survivors and sending nothing is the one
outcome none of the three references describes and the only indefensible one: a
`dispatchAction` whose frame died inside the write loop then sits in `pending`
forever, and `optimistic` — the state this library tells you to render — shows a
turn the host has never heard of, with no echo that could ever retire it.
Re-sending risks a duplicate; not re-sending guarantees a phantom.

`redispatch` exists rather than a second call to `dispatch` because the mirror
already holds the pending entry, and recording another would replay the action
twice into `optimistic`. It advances the fresh client's `clientSeq` counter past
what it re-sent: that counter restarts at 1 on every connection, so without it a
re-sent 3 and the caller's next new dispatch both claim 3 and the first echo
retires the wrong entry.
