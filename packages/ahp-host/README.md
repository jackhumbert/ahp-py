# ahp-host

[![CI](https://github.com/jackhumbert/ahp-py/actions/workflows/ci.yml/badge.svg)](https://github.com/jackhumbert/ahp-py/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](https://github.com/jackhumbert/ahp-py/blob/main/packages/ahp-host/LICENSE)

A Python **host/server** library for the [Agent Host Protocol][ahp] (AHP) —
Microsoft's protocol for synchronized multi-client state over AI agent sessions.

> **"AHP" here means the Agent Host Protocol, not the Analytic Hierarchy
> Process.** The PyPI names `ahp` and `pyahp` belong to packages for the latter,
> a decision-making method with no relationship to this project — which is why
> this one is spelled out. If you want pairwise-comparison matrices and
> consistency ratios, you want one of those instead.

> ### ⚠️ Status: working, but pre-alpha.
>
> A real client can connect, create a session, and run a turn. **All ten
> reducers pass upstream's whole 327-fixture corpus**, the v0.1 command set plus
> `fetchTurns` is implemented, and a WebSocket transport is in place.
> Distributed from this repository — deliberately not on PyPI. API not stable,
> single-trust-domain only.
> [`docs/roadmap.md`](https://github.com/jackhumbert/ahp-py/blob/main/packages/ahp-host/docs/roadmap.md) scopes everything that remains;
> [`docs/decisions/`](https://github.com/jackhumbert/ahp-py/tree/main/packages/ahp-host/docs/decisions) records the decisions taken.

## Try it

The wire types and the ten reducers live in a sibling package,
[`ahp-protocol`][protocol], which a client stands on too. This family
of packages installs from GitHub, not from PyPI, and the protocol package must
go in first — on its own, the second line fails with
`No matching distribution found for ahp-protocol`, because pip asks an
index that does not carry it.

```bash
pip install "ahp-protocol @ git+https://github.com/jackhumbert/ahp-py#subdirectory=packages/ahp-protocol"
pip install "ahp-host[ws] @ git+https://github.com/jackhumbert/ahp-py#subdirectory=packages/ahp-host"
python -m ahp_host
```

No release is tagged yet — **`ahp-host/v0.1.0` is pending** — so these lines install
`main`. Once the first tag lands, pin it by appending the tag
(`…ahp-host@v0.1.0`), and each
[GitHub release](https://github.com/jackhumbert/ahp-py/releases)
will carry the built wheel and sdist. For development, editable installs from
sibling checkouts work the same way: `pip install -e ../ahp-protocol`
then `pip install -e '.[ws]'`.

That serves the offline echo provider on loopback and prints the VS Code
settings to paste — add them to `settings.json`, then open the **Agent Sessions**
view and pick **Echo**:

```json
{
  "chat.remoteAgentHostsEnabled": true,
  "chat.remoteAgentHosts": [
    { "address": "127.0.0.1:4321", "name": "Echo" }
  ]
}
```

Add `"connectionToken": "…"` to the entry if you started the host with
`--token`. `address` and `name` are both required, and VS Code silently drops an
entry that is missing either. The address is scheme-less on purpose — VS Code's
transport prepends `ws://`, and only `wss://` is preserved.

Connecting a third-party host is a supported, extension-free VS Code feature
(1.131+). Verified working against **VS Code Stable 1.131.0**: handshake,
session creation, and a full turn. The details of what it sends — and the three
host bugs that finding out uncovered — are in
[`docs/experiments.md`](https://github.com/jackhumbert/ahp-py/blob/main/packages/ahp-host/docs/experiments.md) §E12.

### Demo flags

The bare command is deliberately minimal. Each flag turns on one surface, and
the ones that execute or destroy are separate from the ones that only read:

| Flag | What it turns on |
|---|---|
| `--customizations` | Publish a demo plugin tree: agents, skills, prompts, rules, hooks, an MCP server |
| `--configurable` | Both config schemas — the session one, and `RootState.config` so the client's own pushes stop being dropped |
| `--elicit` | The agent stops mid-turn and asks a question (ADR 0005) |
| `--confirm-tools` | The agent asks before running its tool, and honours edits to the input |
| `--client-tools` | The agent delegates to a tool the **client** owns |
| `--multi-chat` | Advertise `capabilities.multipleChats` — chat tabs, fork, and side chats |
| `--serve-directory PATH` | Expose `PATH` over the `resource*` commands, jailed to that root |
| `--writable` | **Also allow writes** under `--serve-directory`. A second opt-in, on purpose |
| `--terminal` | Install a real pty backend. **This runs commands** |
| `--changes` | The agent makes real git edits in `--serve-directory` and publishes changesets with working stage/commit/revert |
| `--token [VALUE]` | Require a connection token on the upgrade; omit the value to generate one |
| `--allow-remote` | Bind off-loopback. Read the security section first |
| `--wire-log PATH` | Append every frame as ahp-inspector JSONL |
| `--sequence-file PATH` | Persist `serverSeq` so it keeps increasing across a restart |
| `--port PORT` | Listen here instead of 4321 |
| `--bind ADDR` | Bind address. Loopback unless `--allow-remote` |
| `--delay SECONDS` | Pause between echo deltas, so streaming is visible |
| `--agent-name NAME` | `AgentInfo.displayName` — how clients label the agent |
| `--model-name NAME` | The model name shown in the client's model picker |
| `-v`, `--verbose` | Debug logging |

`--elicit`, `--confirm-tools` and `--client-tools` are mutually exclusive: the
demo provider takes the first one enabled and returns.

A full exploration, against a scratch git repo you do not mind being edited:

```bash
python -m ahp_host --customizations --configurable --multi-chat   --serve-directory /tmp/scratch --writable --terminal --changes
```

To see the wire:

```bash
python -m ahp_host --token --wire-log /tmp/agent-host-demo.jsonl
```

which writes [ahp-inspector](https://github.com/roblourens/ahp-inspector) JSONL —
`npx ahp-inspector` picks up an `agent-host-*.jsonl` file with no arguments.

## Starting agents with a filtered environment

A provider that spawns an agent process can build the child's environment with
`child_env` instead of inheriting the host's:

```python
from ahp_host.provider.spawn import child_env

env = child_env(["PATH", "HOME", "LANG", "LC_*"], {"MY_AGENT_MODE": "x"})
```

Only variables matching the allowlist pass, then the second argument (the
agent's own configured env) is merged on top. A pattern with `*`, `?` or `[` is
a glob and never passes a secret-shaped name (one containing `TOKEN`, `SECRET`,
`PASSWORD`, `AUTH`, `COOKIE`, `API_KEY`, or ending in `_KEY`, among others); a
plain name is exact and passes even when secret-shaped, so a secret reaches the
child only when named exactly. On Windows names match case-insensitively. The
`ahp-host` and `ahp-host-claude` CLIs keep inheriting the full environment by
default; this applies only when a provider's `env_allow` is set.

## Tool policy

`ahp_host.provider.policy` is a pure policy a provider asks before it runs a
tool call. Describe the call as a `ToolRequest` (`name`, `kind` one of
`"command"`, `"read"`, `"write"`, `"tool"`, `input`, `cwd`, `roots`, and the
tool's own `raw` input) and `ToolPolicy.decide` returns a `Decision` whose
`action` is `"allow"`, `"deny"` or `"ask"`, with a `reason`. Build a policy
from a `[policy]` table with `policy_from_table`:

```toml
[policy]
deny_commands = ["git push*", "git remote set-url*", "curl * | sh"]
deny_paths = ["**/.env", "**/id_*", "~/.ssh/**"]
ask_paths = ["**/secrets/*"]
allow_paths_outside_roots = false
check = "mypkg.policy:check"   # optional: fn(ToolRequest) -> Decision | None
```

In brief: a command pattern matches when the command begins with it. Options
and their arguments are skipped, and an option the matcher does not know is
tried both with and without an argument, so `git -C x push`, `git -c a=b push`
and `git --no-pager push` all match `git push*`. Leading `NAME=value` words,
wrappers (`env`, `sudo`, `doas`, `command`, `exec`, `nohup`, `time`, `nice`,
`timeout`, `xargs`, `stdbuf`, `setsid`, with their options) and shell keywords
and grouping (`if`, `then`, `do`, `while`, `{`, `!`, `( )`) are matched both as
written and stripped. The program compares by basename without `.exe`;
`bash -c`, `cmd /c`, `eval` and `env -S` scripts and the bodies of `$()`,
backticks and `<()` are parsed again as commands; `;`, `&&`, `|` and newlines
split a command into segments, so a pattern with `|` matches a pipeline.

A path is checked both as written and after resolving symlinks and `..`; `*`
stays within one folder, `**` crosses folders, a relative pattern matches at
any depth, `~` expands, and matching ignores case on every platform. A path
outside the session's folders (`roots`) is denied unless
`allow_paths_outside_roots` is set. `deny_paths` covers reads, writes and
commands; `ask_paths` asks before a read, a write or a command that names a
matching path. A command's words that are not options (and an option's
`=value`, and redirection targets) are judged as paths too, resolved against
`cwd`, so `cat .env`, `cat ~/.ssh/id_rsa` and `cp .env /tmp/x` are denied by
`**/.env` and `~/.ssh/**`; the roots check does not apply to them. Commands that
cannot be parsed are denied; commands with `$()`, backticks or `<()` ask.

Known limits (best effort over shell text, not a sandbox): paths built at run
time (variables, `$()` output, an earlier `cd` in the same command), globs the
shell expands (`cat .en*`), and files a program opens on its own are not seen. A
word that merely looks like a denied path (`grep id_ file` under `**/id_*`) is
denied too.

A deny is final. `decide` combines every verdict and the most restrictive wins
(deny over ask over allow), and a provider must never downgrade a deny in any
approval mode.

## What this is for

AHP turns "an agent session" from a thing trapped inside one application into a
shared resource several clients can attach to at once — an editor, a browser
tab, a phone, a CLI — all seeing the same live session, any of them able to
answer a tool-approval prompt. The protocol handles ordering, reconnection and
conflict resolution so each front-end doesn't reinvent a worse version of it.

Microsoft publishes AHP **clients** for Rust, TypeScript, Kotlin, Swift and Go.
It publishes **no host library in any language** — the only first-party host is
embedded in the VS Code source tree. Upstream's own
`docs/guide/hosts.md` is a seven-line redirect stub, and its list of server
implementations has exactly one entry.

Python is where a large share of agent infrastructure already lives — harnesses,
orchestrators, eval platforms, internal agent services. Those systems hand-roll a
bespoke SSE or WebSocket stream per front-end today. A conformant Python host
lets them expose a standard protocol instead, and be driven by editor clients
that already speak it.

## Scope

This is a **coordination and state-synchronization layer**. It is not an agent.
Model calls, tool loops, context management and prompting live behind a
pluggable provider interface. Upstream's doctrine is explicit that the protocol
must not enshrine an agent loop, tool schema, model provider or storage backend,
and this project holds that line: the core is vendor-neutral and fully testable
with no adapter installed.

### Implemented

Protocol **1.0.0, 0.9.0, 0.8.0, 0.7.0 and 0.6.0** on the wire, and **1.1.0** — upstream's
unreleased next version, inside `^1.0.0`, whose reducers this host runs · **every channel** — root,
session, chat, annotations, terminal, changeset, resource-watch, automation,
automation-run and canvas, each detailed below · **all ten reducers**, gated on upstream's whole 327-fixture corpus
· host-global sequencing with per-channel replay budgets · a pluggable agent
provider with an offline echo implementation · WebSocket transport behind a
transport abstraction.

All 34 commands, and none of them a stub:

`initialize` · `ping` · `subscribe` · `unsubscribe` · `reconnect` ·
`listSessions` (paginated) · `createSession` (with `fork`) · `disposeSession` ·
`dispatchAction` · `fetchTurns` · `createChat` · `disposeChat` ·
`moveChat` (reorder, or move to another session with the agent's consent) ·
`resolveSessionConfig` · `sessionConfigCompletions` · `completions` ·
`authenticate` · `createTerminal` · `disposeTerminal` · `createResourceWatch` ·
`invokeChangesetOperation` · `resourceResolve` · `resourceRead` ·
`resourceList` · `resourceRequest` · `resourceWrite` · `resourceMkdir` ·
`resourceDelete` · `resourceMove` · `resourceCopy` ·
`listAutomationTriggerDefinitions` · `runAutomation` · `fetchAutomationRuns` ·
`createTcpConnection` (experimental, and declined: answered `PermissionDenied`,
since this host never advertises `tcpConnections`)

A provider can also **stop and wait for a human** — elicitation, tool-call
confirmation, and handing a tool to a client to execute — all on one primitive
([ADR 0005](https://github.com/jackhumbert/ahp-py/blob/main/packages/ahp-host/docs/decisions/0005-suspending-provider-requests.md)). That last one
is worth calling out: the host marks a tool call `contributor: {kind: "client"}`,
the client runs it in its own process, and the client reports the result. **The
agent gets the editor's own tools with no filesystem API on the host at all.**

Try any of them against the demo host:

```bash
python -m ahp_host --elicit
```

`--confirm-tools` asks before running a tool and honours a client's edits to the
input; `--client-tools` delegates the work to a tool the client owns;
`--configurable` publishes a session config schema.

A confirmation can offer **richer choices** (`ConfirmationOption`) and a **diff
preview** of the edits it will make, and its answer reaches the provider
whole — the option picked, and for a denial the user's reason and suggestion.
A question answered on another device can be **withdrawn here**
(`ResolvesInput.input_resolved`, keyed by `InputRequest.key`), as an approval
can (`tool_call_confirmed`). A provider republishing its customization tree no
longer wipes a client's plugins or the user's on/off switches: the host merges
the replacement. A tool result can **be a diff** (`TurnSink.file_edit`, `fileEdit` content); a
turn can carry a **system notification**; and a failed turn can be **resumed**
(`chat/turnResume`) by an agent that is `ResumesTurns`, accepted only when the
spec's preconditions hold. A **structured turn** (an ahp-py extension) returns a
value matching a JSON Schema the client puts in
`Message._meta["ahp-py/outputSchema"]`; the host validates and retries it, and a
turn with no valid result fails with `errorType` `ahp-py/structuredOutput` (see
the "Structured turns" section of the provider guide).

**One agent session serves every chat**, so every message names its chat
(`UserMessage.chat_uri`), and an agent that keeps a conversation per chat can
follow them: `HostsChats` hears each chat open (a refusal fails `createChat`
with nothing created) and close, replayed after a restart; `CancelsChats` stops
one chat's turn without the others; `FollowsChatWorkingDirectories` tracks a
chat's folder subset, which the host keeps inside the session's set — a
`chat/workingDirectorySet` naming a folder the session lacks is rejected, as
1.0.0 requires. A **chat attachment** (`MessageChatAttachment`) has its
`endTurn` pinned when the message is accepted, is refused when it names an
unknown chat or turn, and reaches the provider resolved
(`UserMessage.attached_chats`).

**The agent can change after start-up**: an `UpdatesAgentInfo` provider that
finds its models late republishes them (`root/agentsChanged`, only when the
list differs; `Host.refresh_agents()` for an embedder), `AgentInfo.customizations`
is published, and a `Completes` provider declares its own completion trigger
characters (`DeclaresCompletionTriggers`) — used when the embedder passes none,
which is how `ahp-node` reaches its agents' completions. A `ProvidesOperations`
provider declares its changeset operations, which the host registers when it is
built; a handler may take a fourth `OperationContext(params, principal)` argument
(chosen by arity, so three-argument handlers keep working) and return
`{message, followUp}` for the result. A `ResumesOnRestore` provider names the
restored sessions that need their agent at once instead of on the first turn.
The session publisher is also `ObservesSessionState` (`session_state()`, the
session channel's reduced state), `ObservesChatState` (`chat_state(chat)`, one of
its chats as reduced, turns and their messages included, also after a restart), `ReopensToolChats` (`worker_chat(resource)`,
a worker chat opened earlier, also after a restart) and `PublishesChatActivity`
(`chat_activity_changed(chat, activity, meta=)`, a `chat/activityChanged` on one of
the session's own chats with an optional `_meta`, e.g. a liveness tick), and a `ToolConfirmation`
carries hints for automatic answerers (`read_only`, `read_only_from_agent`,
`policy_asked`, `locations`) that are never published. All of it is in
[the provider guide](https://github.com/jackhumbert/ahp-py/blob/main/packages/ahp-host/docs/guide/writing-a-provider.md).

**Params are validated where the schema is specific, and the refusal is the
schema's code.** A `protocolVersions` array with a non-string entry, a
`listSessions` `limit` that is a JSON boolean or a string, a chat `source` with
no `turnId`, a `SideChatSelection` whose `text` is empty: each is `-32602`,
rather than a Python exception rendered as `-32603`, or a field quietly ignored
so that it means the opposite of what was asked. `limit` is typed `number`, so
`3.0` is honoured and `true` is not. The `-32005` refusal carries
`supportedVersions` — the name `errors.schema.json` declares, and the one frame
whose whole job is telling a user which host version to install.

**`reconnect` de-duplicates its subscription list and registers a connection
only for the channels it actually resumes.** The list is peer-supplied and the
schema does not forbid repeats: N copies of one URI returned N copies of every
missed envelope, each carrying the same `serverSeq`, so a conformant mirror
folded every delta N times and a 50 KB request was answered with 62 MB. And a
channel the reply reports in `missing` is no longer left subscribed — the host
was saying "drop this" while continuing to deliver it, and because session and
chat URIs are *client-chosen*, the URI it disowned can be created later by
somebody else. A telemetry channel is the one exception, carved out explicitly:
it is stateless but live, and `reconnect` returns no `telemetry` map for a
client to re-read.

**A `chat/turnCancelled` must name the turn that is running**, not merely arrive
while one is. The reducer no-ops unless `turnId` matches, so accepting a
mismatch aborted the provider while the host's own state went on claiming a turn
was in flight — and nothing could then clear an `activeTurn` whose id nobody
knew. **`disposeChat` cancels that chat's turn before dropping the channel**,
which is also what retracts any confirmation it was parked on; otherwise the
session stayed at `InputNeeded` holding a request that could only be answered on
a channel that no longer existed.

**Configuration** is `resolveSessionConfig` / `sessionConfigCompletions` plus
`RootState.config`, and it comes with its gate rather than after it. A host
publishes no schema by default, and **the schema is the gate**: an unknown key,
a read-only property or a wrong-typed value is refused whatever policy the
embedder supplied. That matters because `root/configChanged` is
client-dispatchable, VS Code sends it about ten times per connect, and a
permissive policy is the norm for a loopback host.

**Terminals** run real commands on a POSIX pty behind `--terminal` (there is no
Windows backend: importing `PtyTerminalBackend` there raises an `ImportError`
that says so, and `--terminal` exits with that message), with shell
integration parsed (never injected), process-group teardown on hangup, and
`terminal/exited` announced so a client can close the tab. A `!command` turn is
a one-shot: its child dies when the turn is cancelled and again when the host
stops, because a shell that outlives what asked for it is nobody's idea of a
closed terminal. `terminal/input` and `terminal/claimed` are refused from a peer
that does not hold the claim; **`disposeTerminal` deliberately is not** — a
session-claimed terminal is held by no client, so gating disposal on the claim
would make every handed-over terminal unkillable — and it is gated where the
other commands are, by `Policy`. **Resource watches** poll and coalesce.

**Automations** are a saved prompt plus a session template, run by hand
(`runAutomation`) or on a cron schedule the host evaluates in a named time
zone. Pass `automations=FileAutomationStore(path)` to turn them on;
`ahp-node` does, under its state directory. Each run is an ordinary
session whose `origin` points back at the run, with the saved message as its
first turn, and the run's lifecycle follows that turn: `completed`, `failed`,
or `cancelled` through `automationRun/cancelRequested`. A turn parked on a tool
confirmation leaves the run `running` until someone answers. Missed occurrences
follow `misfirePolicy` — one catch-up run, or none — so a host that was down
overnight neither forgets the schedule nor fires it eight times. A run that was
in flight when the host stopped comes back `failed`, not forever `running`.
`runAutomation`'s `requestId` makes a retry return the same run. Definitions
are validated before they are saved, with the refusal echoed as
`rejectionReason`: an unparseable cron field, an unknown zone, a duplicate
trigger id, a message whose origin is not `automation`, an unknown provider.
**Event triggers are refused** — their types are host-defined, and this host
defines none, so `listAutomationTriggerDefinitions` answers an empty list.
**A client can name the session it started a session for.** `createSession`
`_meta["ahp-py/origin"]` with `{kind: "ahp-py/delegation", session, chat,
toolCallId}` (`ahp_protocol.ext.delegation`) is seeded into the session's
immutable `origin`, so it is in `listSessions` and persisted. It is the client's
claim and is not verified; only this kind is accepted, `automation` stays
host-only and any other kind is ignored.
**Client plugins travel with the automation** (`session.customizations`,
advertised as `automations.customizations`): when a definition is saved the
host copies each new or changed plugin from the client that saved it, serves
the copy under its own `ahp-plugin-copy:` URI, and gives it to every run
session — so a run at 3 a.m. has the skills and prompts the user saved,
with no client connected. The copy is bounded (500 files, 8 MiB); a capture
that fails rejects the whole action.
**Disable conditions** (`afterRuns`, `afterDate`) stop scheduling on their own:
`AutomationEntry.runCount` counts each scheduled run when it is admitted, a met
condition sets `enabled` to false and leaves the conditions in place, and
manual runs neither spend the allowance nor are blocked by it.
**Automations are owned.** With a policy that defines `principal_of`, the creating
connection's principal is stamped on the automation (host-private: persisted on
its record, never in an `AutomationEntry` or any wire type; a record written
before this loads as ownerless). The catalogue `ahp-automations://` is then
filtered per subscriber — snapshot, replay and live actions — to that principal's
own automations, and so are the run channels. Updating, removing, running,
`fetchAutomationRuns` and cancelling a run are refused for anyone else: a
dispatched action echoes `rejectionReason: "rejected by policy"` to its sender
only, a method answers `-32009`. A run, and the session it starts, belong to the
automation's owner, scheduled or manual. With no principal (a single-user policy)
everyone sees and manages everything, as before. `OwnedSessionPolicy` does all of
this; a custom policy may define `may_manage_automation` (below).

**Changesets** are driven from a real git working tree: two changesets
(`uncommitted` and `session`), per-file review flags, and operations wired to
`git add`, `git commit` and a scoped revert. A changeset can also belong to one
chat (`publish_changeset(..., chat=)`): it is then listed in that chat's
`ChatState.changesets`, and its roll-up is the chat's `ChatSummary.changes`.
A refresh reports `recomputing`. Four rules the schema states and a host is on
the hook for:

- **An operation is invocable only while the changeset declares it.** A
  registered handler is not an invitation — the demo drops Commit from the list
  when nothing is staged — and the check comes first, so an undeclared id is
  refused rather than reaching a handler with its scope and target unchecked.
  `target` is validated against the declared `scopes`, and a `range` target
  without a `range` is `-32602` rather than whatever the handler raises.
- **The `disabled` gate is re-evaluated at both ends of every turn**, not
  sampled when the changeset is published. A provider can only publish from
  inside its own turn, so a gate read once is a gate stuck at "busy" for the
  life of the session. `running` and `error` are left alone: they belong to an
  invocation, not to this gate.
- **Review survives a republish whichever side set it.** `filesReviewChanged`
  is the one client-dispatchable `changeset/*` action and the server MAY
  originate it too; the ticks are reconciled from the channel's own state, so
  the host's own tick is remembered exactly like a reviewer's.
- **The catalogue entry is re-emitted when it changes** — label, description,
  `changeKind`, `capabilities.review` — and not when it does not. The entry is
  the only copy a client has, and the review gate reads the current one.

`SessionSummary.changes` rides on `root/sessionSummaryChanged`. It is not
written into `SessionState`, which declares no such key.

Changeset diffs, confirmation edit previews and `fileEdit` results share one
content store per session, served by `resourceRead` under that session's
visibility. It is a bounded cache — `Host(max_content_bytes=...)`, 128 MiB by
default, least recently used out first — and it does not survive a restart, so
an old diff may no longer open.

**The `resource*` family** is implemented, and exposes nothing by default. A
host does not acquire a filesystem by being upgraded: install
`RootedFilesystemResourceProvider` to serve one directory, and `writable=True`
is a **second, separate opt-in** on top of that — reading discloses, writing
destroys, and the two should not be granted by the same gesture.

The jail walks a path one component at a time with `openat` and `O_NOFOLLOW`,
resolving any symlink itself and re-checking the result against the root. That
is deliberately not `realpath`-then-open: between the check and the open, a
component can be swapped for a symlink and the open follows it — the check
passed, the read escaped. There is a test for exactly that race. Reads follow
symlinks; a write whose final component is one is refused rather than followed,
because a write's policy check runs against the name the peer sent.

On Windows the same class builds a separate, **read-only** jail over NT handles
(relative `NtCreateFile` opens with `FILE_OPEN_REPARSE_POINT`, no
`FILE_SHARE_DELETE`), and `writable=True` is refused there. Its guarantees and
limits are in [`SECURITY.md`](SECURITY.md).

Reads are bounded — 16 MiB by default, `Host(max_read_bytes=…)` to raise it and
`None` to remove it. The protocol has no partial read (`resourceRead` takes no
offset or length), so a file above the bound is refused rather than truncated:
unbounded, one 64 MiB file took host memory from 29 MB to 970 MB.

```bash
python -m ahp_host --serve-directory ./workspace
```

**Sessions can survive a restart.** Install a `FileSessionStore` and call
`await host.restore()` before serving. JSON only — never `pickle`, never
`eval` — because a stored session is attacker-influenced data: titles, chat
content and tool results all come off the wire. `serverSeq` survives too, which
matters more than it sounds: the reference client records it with a *maximum*,
so a counter that restarts at zero can never replay again.

A turn that was running when the host stopped is restored as a failed turn
(`errorType` `host.restart`) with its partial response intact, and is resumed
through the ordinary `chat/turnResume` path (`ResumesTurns.resume_turn`):
automatically, once, after a deliberate restart, or by the user after a crash
(see **Restarts drain first** below). Open approvals and questions are persisted
as `inputRequests` beside the session's `inputNeeded`. Nothing in the next
process can answer one -- the turn that asked has failed, and a resumed turn runs
with a fresh sink and raises fresh requests of its own -- so on restore every one
is withdrawn: its `inputNeeded` entry is removed and a `systemNotification`
naming it is added to the interrupted turn, just before the error. Nothing
vanishes silently, and the session is not left showing "input needed" with
nobody listening. The notice's `_meta['ahp-py/restart']` is
`{"event": "inputWithdrawn", "requests": [<ids>]}`; a client that does not know
the key still shows the text. A restored session lists no active clients:
every connection ended with the previous process, so none of them can run a
tool, and each one that comes back joins again (`session/activeClientSet`) as
after a disconnect; what they published stays in `customizations`. Shutdown
persists every session before cancelling
its turns, so the store keeps what was open; a hard kill loses at most the store
debounce (1 s by default). See
[ADR 0006](docs/decisions/0006-restoring-interrupted-turns.md).

**Client-published plugins are expanded.** `resource*` is symmetrical, and the
reverse direction exists so a host can fetch content only the client has — a
`virtual://my-client/...` plugin lives in the client's memory and no filesystem
here will find it. The host reads it back and publishes the children, which is
what makes a plugin's skills, prompts and instructions render rather than
appearing as an empty container.

**Named roots can be served at their real paths.** By default `[roots]` (or
`--root NAME=PATH`) is shown as a small virtual tree: `file:///` lists the names and
`file:///<name>/<rel>` is `<rel>` under that root. Set `roots_layout = "real"` (a top-level key, placed before `[roots]`; or
`--roots-layout real`) and each root is served at its real absolute `file:` URI instead.
A folder picker can walk down from `/` through the directories on the way to a root;
those list only the children that are roots or lead to one, and they are a listing and
nothing else (no read, write or watch). Anything else is refused as before, and the
old `file:///<name>/...` spelling still works unless the URI is itself a real path
inside a root or on the way to one.

**`ahp-node` keeps itself running.** `ahp-node supervise` restarts the node,
and the config's `tunnel` command, whenever they exit; `ahp-node install`
registers that supervisor as the current user — a Scheduled Task at logon on
Windows, a launchd agent on macOS, and on Linux a systemd user unit
(`~/.config/systemd/user/ahp-node.service`) with lingering turned on, so it
starts at boot with no one logged in. The unit's `PATH` puts `~/.local/bin`
first, where agent CLIs usually live. `ahp-node uninstall` removes it.

```bash
ahp-node install --config ~/.config/ahp/node.toml
```

**Restarts drain first.** A stop or restart does not cut running turns off at
once. The node stops *starting* work and keeps serving its clients while the
turns already running finish, for up to `drain_timeout` seconds (a top-level
setting in the node's TOML config; default `300`). Only then does it close its
agents.
What happens to each kind of work:

- **A running turn** finishes normally if it ends within the timeout. A turn
  still running at the deadline is cut off, and its chat is recorded as
  interrupted by the restart.
- **A new turn from a client** is not refused. The host rejects the client's
  optimistic action and publishes the message back as a queued pending message,
  so the user sees it waiting. It does not start until the restarted node runs it.
- **A queued message** is not started while the node drains, and is started
  after the restart once its chat is idle.
- **A cut-off turn, after the restart,** is restored as a failed turn. When the
  agent can resume turns (the Claude provider can), the error is resumable, and
  the chat is resumed automatically, once, because the restart is what stopped
  it. An agent that cannot resume turns leaves a failed turn that is not
  resumed. A turn lost to a crash, which the drain never got to see, is resumable
  but is *not* resumed on its own; a client resumes it with `chat/turnResume`.
- **An automation run** cut off at the deadline is marked failed on the next
  start; its turn is not resumed, since no run would be attached to it.
- **A scheduled automation** is paused during the drain: nothing new is started,
  and no cursor moves, so a run that came due is still due after the restart.

Three ways to ask a running node to restart or stop:

```bash
ahp-node restart --config ~/.config/ahp/node.toml --timeout 120
```

`ahp-node restart` writes a `restart-request` file in the node's state
directory; the node notices it within about a second, drains, and exits with
code `75`, which the supervisor reads as "start me again now". `--timeout`
overrides `drain_timeout` for this one restart. The same request can be made by
sending `SIGHUP` on POSIX. A `stop-request` file, or `SIGTERM` / `SIGINT`, drains
and exits `0` instead, so the supervisor stops rather than restarts. On Windows
a stop from the supervisor is written as a `stop-request` file first, then the
node is killed if it has not exited within the drain wait.

Service managers are given enough time to let a drain finish. The launchd agent
gets `ExitTimeOut`, and the systemd unit gets `TimeoutStopSec` and
`KillMode=mixed`, which sends `SIGTERM` to the supervisor alone so the `claude`
children are not hit directly. Both are `drain_timeout` plus 30 seconds.
These limits are written into the service file at install time, so run
`ahp-node install` again after changing `drain_timeout`. (The supervisor's own
wait follows the node's current setting without that.)

**Stopping the Windows Scheduled Task is a hard kill.** Task Scheduler has no
graceful stop, so ending the task cuts running turns off. They resume on the
next start, as after a crash, but are not resumed automatically. Use
`ahp-node restart` for a graceful restart on Windows.

### Not implemented

**No command answers `MethodNotFound`.** That used to be how this host declined
a feature, since AHP has no server capability object. A refusal is specific
now: `PermissionDenied` for something the host will not do, `NotFound` for
something it does not have, `ProviderNotFound` for an agent that does not
exist. Each tells a client more than "stop asking".

What is genuinely absent, and why:

- **An MCP client runtime.** The lifecycle commands are answered and the
  customization is published; nothing spawns or connects a server.
- **Model routing.** `AgentInfo.models` is published for the client's picker
  and `UserMessage.model` carries back what the user chose — the host is a
  courier and never selects.
- **Checkpoints and plan review.** Not host-drivable: they are internal to
  VS Code's own in-process host, with no channel, command, action or state
  field in the protocol.
- **`pickle`, `eval`, or any `__reduce__`-capable store format**, permanently.
  JSON only. See [`docs/roadmap.md`](https://github.com/jackhumbert/ahp-py/blob/main/packages/ahp-host/docs/roadmap.md) §10.
- **Retracting a session-summary field over `root/sessionSummaryChanged`** —
  the wire cannot say it. "Only fields present in `changes` have new values;
  omitted fields are unchanged", and every property of `changes` is typed as
  its own non-null type, so there is no token for "this field is gone". A
  session that reported an `activity` and then went idle keeps advertising the
  last one to a client rendering from that incremental cache, until it
  re-fetches. `listSessions` and every fresh subscriber always see the truth,
  and the chat catalogue does not have the problem — `session/chatAdded` is a
  documented upsert and this host retracts through it. Open question 11 in
  [`docs/research.md`](https://github.com/jackhumbert/ahp-py/blob/main/packages/ahp-host/docs/research.md); this host will not invent an
  encoding for it unilaterally.

Session state is in-memory by default: sessions do not survive a host restart
unless the embedder installs a `SessionStore`. `serverSeq` survives one with
`--sequence-file`; without it a reconnecting client is correctly told to take
fresh snapshots, but it is told that on **every** reconnect thereafter, because
the reference client records the sequence with a maximum and stays permanently
ahead of a counter that restarted at zero.

## Redaction after publishing

AHP cannot take back what it has published: history is append-only, and a
delta, a tool call's output and a reasoning block all sit in clients' mirrors
and in the replay log. This host adds an **ahp-py extension** for the case where
something must be removed afterwards (a pasted secret, personal data).
`Host.redact(channel, turn_id, part_id, reason, *, actor, keep_meta, now)`
(`actor` is required: the audit's "who" is never null) replaces one response part; `Host.redact_message(channel, turn_id, reason, ...)`
does the same for a turn's user message. Both raise `RedactionError` for a
target that is not there. Redaction is **host-initiated**: it is the embedder's
operator action, and a client that dispatches the extension action is refused
and echoed like any other client-sent server action (invariant 12).

**What can be targeted.** `markdown`, `reasoning` and `toolCall` parts. A part
with no id, and an `inputRequest` part, cannot be targeted.

**The marker.** The part keeps its kind and id and its text becomes
`[redacted]`, with `_meta['ahp-py/redaction'] = {reason, redactedAt}`. A tool
call keeps its id, name and status; its text fields become `[redacted]`, its
content becomes a single text item and structured content is dropped. Every
stock client renders it, because it is an ordinary part of an ordinary kind.

**Delivery.** A client that sends `_meta: {'ahp-py/redaction': {}}` in
`initialize` (or in a first-request `reconnect`) receives
`ahp-py/chat/partRedacted` / `ahp-py/chat/messageRedacted` live, as a sequenced
action on the channel. Every other client is **never sent it**: an unknown action
must not reach a stock client. Its live view keeps the original until it
resubscribes or reconnects, and then it gets a snapshot (the host forces one on
reconnect even when its `lastSeenServerSeq` is already past the redaction). A
stock client may see a `serverSeq` gap; that is legal, since it is traffic it
did not see.

**Replay cannot return the original.** The channel's replay log is truncated at
the redaction, so a reconnect that would have replayed the original is answered
with a snapshot instead ([ADR 0007](docs/decisions/0007-redaction-truncates-replay.md)).
The stored session is rewritten immediately, not on the debounce, and content-store
blobs the part referenced are discarded. The audit event `chat.redacted` records
who, when, which target and why, and never the content. It also records
`persisted` (and `error` when false): the entry is written whenever the redaction
was *published*, even if the store write then failed -- in which case
`RedactionError` is raised telling the caller the stored session may still hold
the original, and redacting again once the store works rewrites it. A redaction
during `aclose` is refused with `RedactionError` before anything is published. A
target that vanishes between the check and the sequencer's lock raises `target not
found` and consumes no `serverSeq`.

**What it does not do.**

- **The wire log is not rewritten.** Frames written to `--wire-log` before the
  redaction still hold the original. Rotate or delete those files; the host
  cannot edit them.
- **The agent's own context and provider state are not redacted**, only the
  shared view. A model that has seen the content still has it.
- A session title seeded from a first message is not changed; rename the session
  with `session/titleChanged`.
- An `ahp-gateway` in front of the host does not advertise the capability, so
  its clients get the redacted state on their next snapshot.

## Embedding it behind a proxy

The library ships one `Policy` that permits everything
(`LoopbackSingleUserPolicy`) and one that partitions sessions between users
(`OwnedSessionPolicy`). The second is an **example**, not a core concept — but
it comes with the negative tests that matter, because every deployment writes
the same four and they are the ones that fail loudly when a change routes around
a hook: peer B cannot see A's session, cannot subscribe to its channels, cannot
dispatch into it, and cannot resume A's connection by asserting A's `clientId`.

`Host.serve` takes `headers=` and `token=`, and the WebSocket server forwards
both from the upgrade. **The library assigns meaning to neither.** Which header
carries a principal, and whether to believe it, is the embedder's decision — and
a forwarded header is evidence only if the socket cannot be reached except
through the proxy that set it.

A `Policy` may also define `principal_of(info: ConnectionInfo) -> str | None`, an
optional hook looked up like the others. When present, the host stamps the creating
connection's principal on `AgentSessionContext.principal` and each
`chat/turnStarted` sender's on `UserMessage.principal`. It also reports who answered
an input or confirmation request as `answered_by` on `InputOutcome` and
`ToolConfirmationOutcome` (a `clientId` is never an identity). The principal is
persisted under `metadata["ahp-py/principal"]`. Without the hook every one is
`None`. A policy may also define `may_manage_automation(info, uri, owner) -> bool`,
optional in the same way: whether a peer may see or act on the automation or run
`uri`, whose creator's principal is `owner` (persisted on the automation record).
Absent, the default is an owner match when both principals are known and allow
when either is `None`; a raise refuses. `OwnedSessionPolicy` implements it and
claims the automation, run and run-session channels for their owner through
`claim`, which the host calls for what it creates with no connection. For the socket, `bearer_token_validator(token)` checks an
`Authorization: Bearer` header, and `serve_websocket` takes `ping_interval` (so a
proxy's idle timeout does not cut a quiet connection) and `max_size` (the frame
limit; keep a proxy's at or above it).

`Host.counters()` reports connections, sessions, active turns, pending requests,
watches, channels and `serverSeq`. `AuditSink` records decisions — admissions,
refusals, session creation, tool-call resolutions — with **no conversation
content by construction**. Both are absent by default.

One host runs **one provider**. `RootState.agents` is plural and this publishes
one entry, deliberately: see [`docs/roadmap.md`](https://github.com/jackhumbert/ahp-py/blob/main/packages/ahp-host/docs/roadmap.md) §4a.

## ⚠️ Security: read this before exposing a host

**AHP defines no security model, and says so.** Connection admission is
explicitly outside the wire protocol
([`transport.md`][transport]). The `authenticate` command is *not* a login — it
pushes tokens for upstream services the agent talks to, and never gates the AHP
connection itself. There is no server capability object, so a host cannot
negotiate dangerous surfaces away; it can only refuse them.

A host that implements the full protocol literally hands any peer that completes
`initialize`: a read/write/delete filesystem API, arbitrary pty creation with a
client-chosen working directory, enumeration of every session on the machine,
and the ability to approve **any other client's** pending tool call — the
protocol's own validation table conditions tool-call approval on the call's
*status*, never on client identity.

This library's position:

- The core guarantees **protocol** invariants only — sequencing, reducer parity,
  state-transition validity, action-origin stamping, client-dispatch gating.
- Every **trust** decision — who may connect, who may see which channel, who may
  approve — is a **mandatory, no-default, embedder-supplied policy**.
- **There is no `serve()` one-liner that binds a socket.** Constructing a host
  requires a policy object.
- Default bind is loopback. Binding off-loopback without an explicit policy is a
  hard error, not a warning.
- The filesystem family and terminals **are** implemented, and every one of them
  is **off by default**: no filesystem without a resource provider, no writes
  without a second and separate `writable=True`, and no command execution
  without a terminal backend you construct by name. The default backend declines
  every terminal with a reason. Nothing arrives by upgrading.
- The jail **walks** — `openat` with `O_NOFOLLOW`, one component at a time, each
  resolved symlink re-checked against the root — rather than resolving a path
  and then opening it, which has a window in which a component can be swapped.
- `--wire-log` redacts credentials and writes owner-only. An embedder that masks its
  own output can mask the log's text too: `Host(wire_log=..., wire_log_mask=...)` takes a
  `Callable[[str], str]` or an object with a `mask(str)` method, applied to every string
  written (ahp-host-flow passes its `Redactor`). It still contains
  every message of every session, which is a transcript, not a trace.
  `FileSessionStore` and `FileAutomationStore` directories are owner-only too:
  `0o700`/`0o600` on POSIX, and on Windows — where `chmod` only sets the
  read-only attribute — a protected ACL granting the current user alone, which
  files created in the directory inherit (details in `SECURITY.md`).

**This is single-trust-domain.** It is not multi-tenant, and it is not safe to
expose to an untrusted network. Both known existing hosts punt on this too — VS
Code uses a single connection token; the one third-party host states outright
that remote and multi-tenant security are unimplemented — but that is context,
not reassurance.

[`SECURITY.md`](https://github.com/jackhumbert/ahp-py/blob/main/packages/ahp-host/SECURITY.md) has the scope and the disclosure path;
[`docs/guide/deploying.md`](https://github.com/jackhumbert/ahp-py/blob/main/packages/ahp-host/docs/guide/deploying.md) has the long version, with
examples the test suite executes.

## Conformance

"Conformant" without a conformance test is a lie, and this project's entire
value is that other implementations can trust it. So:

The reducers and the wire types now live in
[`ahp-protocol`](https://github.com/jackhumbert/ahp-py/tree/main/packages/ahp-protocol),
a separate package this one depends on, so that a Python *client* can share them
rather than fork them. Its conformance gates are listed here because they are
what this host stands on:

- Reducers are validated against **upstream's own 327-fixture corpus**, the same
  artifact the Rust, Go, Kotlin and Swift clients are gated on, consumed
  unmodified. All 327, not a subset.
- Wire types are validated against upstream's 94-fixture round-trip corpus.
- **The corpus's own blind spot is covered separately.** Its comparator drops
  `null`-valued keys on both sides, so it cannot express the difference between
  an absent key and an explicit `null` — the single most common porting defect
  here, and one an audit found in four reducers at once. So a second corpus is
  generated by running adversarial cases through the **real pinned TypeScript
  reducers** under Node and freezing their output verbatim, nulls and all
  Comparison is byte-for-byte, offline, and it runs in the protocol package.
- Integration tests drive the **real published Microsoft TypeScript client**
  against the host, and feed our live action stream through the **official
  TypeScript reducers**, diffing the result against a fresh snapshot.
- A golden trace captured from a live **VS Code 1.131** session is replayed on
  every run.
- Multi-client tests cover the thing AHP exists for: several clients over one
  session agreeing on order, origin and final state.

That last one matters because the reference client validates almost nothing —
in testing it accepted an unoffered protocol version, a missing required field
and a 94-wide sequence gap without complaint. Host correctness is simply not
observable by pointing a client at it, so it has to be asserted directly.

## Relationship to upstream

This project targets an external specification. Protocol changes come from
upstream, not from contributors' preferences. Where something is underspecified,
the question is filed upstream and recorded in `docs/research.md` — this project
does not fork the spec or diverge privately. See
[`CONTRIBUTING.md`](https://github.com/jackhumbert/ahp-py/blob/main/packages/ahp-host/CONTRIBUTING.md) and [`UPSTREAM.md`](https://github.com/jackhumbert/ahp-py/blob/main/packages/ahp-host/UPSTREAM.md).

Licensed **MIT**, matching upstream — [`ahp-protocol`][protocol], which
this package stands on, vendors upstream's MIT-licensed conformance fixtures and
ports its reducers, so identical terms avoid any compatibility question.

[ahp]: https://microsoft.github.io/ahp-protocol/
[protocol]: https://github.com/jackhumbert/ahp-py/tree/main/packages/ahp-protocol
[transport]: https://microsoft.github.io/ahp-protocol/specification/transport
