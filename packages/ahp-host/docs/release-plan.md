# Release plan — everything between here and `0.1.0`

Three sources feed this: round two of [`requests.md`](requests.md) (what an
embedder deploying this cannot do), a parity diff against `agentHostMain.js`
(VS Code's own in-process AHP host, which is the reference implementation of
exactly what this project builds), and the packaging work a first publish needs.

The tiers are not priority labels, they are different **kinds** of blocker:

- **Tier 0** stops someone using the library at all. An embedder installs the
  package, follows our own documented example, and cannot proceed.
- **Tier 1** is what "published" means. Without it there is nothing to install.
- **Tier 2** is how well we drive one specific client. Real, visible, and not a
  reason to withhold a release that says what it does and does not do.

Ordering within a tier is by user-visible value per unit of effort, with
correctness ahead of features: a wrong shape outranks an absent one, because
wrong shapes fail silently.

---

## Tier 0 — an embedder cannot ship without these

### 0.1 Session lifecycle must be knowable by the Policy — **blocking**

`OwnedSessionPolicy` keeps a channel→principal map and **nothing populates it**.
`claim()` has no caller in the library; its only call site is a test that builds
sessions in-process and reads `session.chat_uri` off the object it just made. A
deployment driven by clients never holds that object:

- `createSession` arrives over the wire;
- `may_create_session` is consulted **before** the host mints the chat and
  annotations channels, so at decision time those URIs do not exist;
- nothing informs the policy afterwards.

The prefix walk in `owner_of` does not close it either — a chat URI is
`ahp-chat://<chatId>/<base64 session uri>`, so the session URI is *inside* it,
not its parent.

Fail-closed, at least: the symptom is "a user cannot see their own chat", not a
leak. It is still a shipped example that cannot do the thing it is an example
of.

**Do:** a session-lifecycle hook on `Policy` — *this session, with these
channels, was created on this connection* — delivered before the first
`subscribe` can arrive, plus the matching disposal. Nothing parses a URI
(invariant 15) and nothing reaches into private state.

The alternative worth weighing first: let `Policy` see **channel registration**
directly. That is the moment ownership becomes expressible, it covers channels a
session creates later (terminals, changesets, watches) with no extra hook, and
it is one call site instead of two.

**Honest test:** drive it over the wire with two connections and two
principals — not by constructing sessions in-process, which is what made the
existing test pass while the feature was unusable.

### 0.2 `StoredSession` needs embedder metadata

`may_restore_session` refuses by default and its docstring is right about why: a
restored session has no owner, and `may_see_channel` refuses unowned channels,
so restoring one produces a session nobody can reach. That is a correct default
for a **missing capability**, not a design position — so durability and
partitioning, the two headline features of a multi-user deployment, cannot both
be on.

**Do:** an embedder-owned `metadata` mapping on `StoredSession`, round-tripped
verbatim and never interpreted — the same treatment `ProviderResumeState`
already gets. Ownership then travels with the session it describes.

**Test:** store → restart → restore → the principal still owns it, asserted
through `may_see_channel` rather than by reading the mapping back.

### 0.3 Decide the outbox bound

`Connection._outbox` is an unbounded `asyncio.Queue`. A peer that stops
reading — a suspended laptop, a wedged renderer, a stalled proxy — accumulates
frames in host memory for the life of that connection, with no backpressure and
no drop policy. Nothing on loopback; a slow leak with an ordinary trigger on a
host serving several people.

The request explicitly does **not** prescribe a fix, because the tradeoff
belongs to whoever owns invariant 10. Dropping frames breaks replay
expectations; closing a slow connection is a policy decision. A bound with
documented overflow behaviour is probably the answer, and the limit belongs in
[`guide/deploying.md`](guide/deploying.md).

**This is the one Tier 0 item that is a decision before it is a change.**

### 0.4 A worked liveness example

`Host.counters()` is the right API and deliberately not an endpoint. Every
deployment still writes the same twenty lines: a loopback listener returning
those counters and a readiness answer. Readiness in particular is not guessable
from outside — "restore finished, transport bound" is a fact only the host has.

**Do:** put it in `guide/deploying.md`, where the examples are executed as
tests, so the shown pattern cannot rot.

---

## Tier 1 — what "published" means

1. **A version.** `0.0.0` in two places (`pyproject.toml`, `__init__.py`) —
   single-source it and pick `0.1.0`.
2. **A populated CHANGELOG.** `[Unreleased]` is empty after a very large amount
   of work.
3. **A publish workflow** using PyPI trusted publishing (OIDC), not a
   long-lived token. Tag-triggered, building the same wheel CI already tests.
4. **`SECURITY.md`.** This ships a filesystem jail and a pty backend. It needs
   a disclosure path.
5. **Interop in CI.** `tests/interop` drives the real TypeScript client and runs
   only locally today. It is one of two genuinely independent checks we have.
6. **A wheel smoke test in CI** — install the built wheel into a clean venv and
   import from it. Three publication defects were invisible from a checkout and
   obvious from an install: missing exports, a demo tree resolving outside the
   package, and a missing `py.typed`. All three are now fixed; nothing stops
   the fourth.

Already done: public exports, packaged demo tree, `py.typed`, the embedder
guide, executable doc tests, README accuracy, and the wire-schema conformance
harness.

---

## Tier 2 — parity with VS Code's own host

The full diff found no **command** gap at all: all 30 `CommandMap` methods are
answered, plus the reverse server→client `resource*` direction, and our
client-dispatchable action table is byte-identical to the reference's, all 85
entries. Our action rejection is stricter than the reference's and correctly so.
What is missing is *what we publish*, not *what we implement*.

The ranked gaps cluster, and the cluster is exactly what has been reported by
hand this week.

### 2A. Changesets — ranked 1, 2, 5, 7 (and 6, 11, 20, 23)

**The picker never renders** because the session summary carries no
`workingDirectories`; the client gates the changeset dropdown on it. So only the
first changeset is ever visible, which is why switching to "Session changes"
appeared to do nothing even after the `changeKind` fix.

**Four operations collapse into one button.** The client groups by the
`group` field and renders one primary; we set three different groups and get one
button labelled "Stage all". Commit, Revert and Mark-all-reviewed appear not to
exist.

**`reviewed` is wiped by the next republish** — and this one is mine. Ticking
Viewed then pressing any button clears every tick, because the republish I added
so the buttons would visibly do something replaces the file list wholesale,
carrying no `reviewed` flags. The fix is to carry them across a republish.

**A failed operation is visually identical to one that did nothing.** This is
the literal answer to "why does a button look like it did nothing": on failure
the client shows no error anywhere.

Then: operations are not disabled during a turn (so a commit can race the
agent's own writes), the list is static rather than derived from live git state
(Commit is offered with nothing staged), the changeset never returns to
`computing` so there is no refresh progress, and there is no `turn` changeset so
"Last Turn Changes" never appears.

### 2B. The session list looks dead — ranked 8, 12, 13, 21 — **done**

All four landed. Titles are seeded from the user's first message (the default
chat only, and never over a client's own rename); `session/activityChanged`
carries the running tool's display name and is cleared when it finishes,
including when the turn is cancelled mid-tool; the host clears
`session/isReadChanged` when the agent answers a session nobody is looking at;
and `InProgress` promotes from a side chat like `InputNeeded` and `Error`
already did — by **rank** rather than by iteration order, since the non-default
chats are walked in URI order and first-wins would hand the activity string to
whichever sorted earliest.

Two things fell out of building it. The mirror seam: activity only renders in
the session list, which is fed by `root/sessionSummaryChanged`, so publishing to
the session channel alone would have changed nothing a user can see — the sink
takes a callback for exactly that, and the turn's ordinary output still is not
mirrored per action. And `Idle` is now cleared when anything promotes, because
a session whose default chat is idle and whose side chat is working reported
`Idle | InProgress` and a client testing either bit was right either way.

The original finding, for the record:

Every session is called **"New Session"**, so with more than one open they are
indistinguishable. The reference generates a title with a small model, which we
cannot do — but it degrades to *seeding from the user's first message*, and that
is reachable.

`session/activityChanged` is never published, so a working session shows the
client's literal fallback **"Working..."** instead of "Editing core.py".
`session/isReadChanged` is never cleared by the host, so once you have opened a
session the unread dot never returns however many times the agent answers — it
is a two-party protocol and we only implement the client's half. And a streaming
side chat does not promote the session summary, so `--multi-chat` work is
invisible in the list.

### 2C. Turn fidelity — ranked 3, 4, 15, 16, 19, 24 — **done**

The queued-message drain was confirmed rather than assumed, and it is not a
VS Code nicety: the schema states it. "If the chat is idle when a queued message
is set, the server SHOULD immediately consume it and start a new turn", and
`chat/pendingMessageRemoved` is "dispatched ... by the server when it consumes a
message". Both ends are implemented — on queue and on turn end — and the
synthesized `chat/turnStarted` is **published** before it is run, which a
client-dispatched turn gets for free and a host-started one does not. Steering
messages are deliberately left alone: they belong in the running turn, which
only a provider can do.

Response parts are now segmented by kind, so prose after a tool call renders
below it. A run of one kind still shares a part — a part per delta would be a
part per token.

`chat/usage`, `chat/toolCallDelta` and `chat/toolCallContentChanged` are all on
the sink, and the demo emits all three. The `_meta` well-known key is
**`ptyTerminal`**, with `{input, output}` — not `toolKind`, which this document
previously guessed; the schema names it exactly. The library carries `_meta`
through and there is a test for it, but the demo does not claim a terminal it
never touched.

The one item that turned out to be already fixed: `toolCallReady.toolInput` is
encoded on **both** publication sites. There is now a test asserting every
action carrying a `toolInput` carries a string, so a fourth site cannot regress
quietly.

The original findings:

**Queued messages are never drained.** Type a follow-up while the agent works
and it sits in the chip forever. (The verifier partly refuted this for remote
hosts — VS Code drains it client-side — so confirm before building.)

**`toolCallReady.toolInput` is still raw JSON on one render path**, so the
client's `JSON.parse` throws and shows an opaque blob. Related to the confirm-path
fix already landed; one site remains.

One markdown part per turn means prose written *after* a tool call renders
*above* it. No `_meta.toolKind`, so a shell command does not get the terminal
widget. No `chat/usage`, so the context gauge never appears — the client's rule
is explicitly "no usage, no gauge". And no sink method for streaming tool
arguments or progressive output, so a long command shows a static row then dumps
everything at once.

### 2D. Chat tabs and truncation — ranked 9, 10, 17 — **done**

The `session/chatUpdated` finding was bigger than "renaming is a dead button".
The action is server-only, and the catalogue entry was written once at
`session/chatAdded` and never touched again — so `SessionState.chats[]`, which
is what a client renders its chat tabs from, showed every chat idle, unnamed and
stamped with the moment it was created however much work happened inside it.
`ChatState` "inlines (denormalizes) every field" the entry carries, so the two
can disagree and only the host can stop them. `_mirror_chats` is the chat-level
counterpart of `_mirror_summary`, and it runs *before* the summary's early
return: a chat's own title can move without the session summary moving.

The default chat is now called "New Chat" like every other chat. It was given
`session.title` — so its tab read "New Session", the name of the thing that
contains it. `ChatSummary.title` is REQUIRED, so it could not simply be omitted.

Truncation is refused for a provider that cannot forget. The reducer drops the
turns whatever the provider does, so edit-and-resend looks right while the agent
goes on remembering — the one gap in this list where the user is actively
misinformed rather than merely underserved. `TruncatesHistory` is the opt-in;
the host also cancels a running turn ("if there is an active turn it is silently
dropped"), and reads `turnId` exactly as the reducer does, because an absent key
and an explicit null mean different things and collapsing them would produce
this same defect with the sides swapped.

The original findings:

`session/chatUpdated` is never emitted, so renaming a chat is a dead button. The
default chat is seeded with a non-empty title, which pins its tab. And
`chat/truncated` has no side effect, so edit-and-resend rewinds the transcript
visually while the agent still remembers everything — the most *dangerous* of
these, because the user is told a thing that is not true.

### 2E. Small correctness — ranked 25, 26, 27

A tool call that goes Start → Complete with no Ready is silently swallowed
(latent: our providers always emit a Ready). A `dispatchAction` to an unknown
channel is dropped with no echo, pinning the client's optimistic overlay
forever — narrow, but it is the failure mode that produces "the UI is stuck".
And `root/activeSessionsChanged` counts live sessions rather than sessions with
an active turn.

---

## Not doing, with reasons

**Cannot match — needs the editor's own credentials or in-process access:**
model-generated session titles and commit messages, Copilot billing metadata on
`chat/usage`, the context-attribution tooltip, GitHub PR operations, registering
terminals into the editor's own dropdown, and workspace-trust enforcement. The
Models and MCP Servers config sections are `hiddenSections` policy keyed on
remote-ness — no amount of publishing makes them render for us.

**Not host-drivable at all:** checkpoints and plan review are internal to
VS Code's own host process, with no channel, command, action or state field in
the protocol. `SessionState.serverTools` is rendered by nothing in any build.

**Permanently out:** `pickle`/`eval`/any `__reduce__`-capable store format. The
danger is the format, not the feature, so there is no version of it that is safe
behind a flag.

**Advertised and unimplemented, which is worse than absent:**
`terminalCommandPrefix: "!"` makes the input box promise a terminal command that
never runs. Either implement it or stop advertising it — currently we only
advertise it behind a real terminal backend, which is the right shape, but the
demo now ships one.

---

## Sequencing

1. **Tier 0**, in order: 0.3 is a decision, so start it; 0.1 then 0.2 unblock the
   partitioning example; 0.4 is documentation with a test.
2. **2A**, because it is where every hand-reported bug has landed and the top
   two are small.
3. **Tier 1**, and cut `0.1.0`.
4. **2B, 2C, 2D, 2E** after the release, as `0.2.0`.

The wire-schema harness gates all of it: every new frame added below is
validated against the vendored schemas as a matter of course, which is how the
last three shape defects were found rather than shipped.
