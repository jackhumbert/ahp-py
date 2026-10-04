# Writing a provider

A **provider** is the only thing you have to write. It is the seam between this
library — which speaks the protocol, sequences state, and talks to clients — and
your agent, which does the thinking.

Everything in this file is executed by the test suite. If an example here stops
working, `tests/docs/test_guide_examples.py` fails. Documentation that can drift
does drift, so none of this is allowed to.

## The smallest host that works

```python
from ahp_host import AgentProvider, Host, LoopbackSingleUserPolicy
from ahp_host.provider.base import (
    AgentInfo,
    AgentSession,
    AgentSessionContext,
    ModelInfo,
    TurnSink,
    UserMessage,
)


class ShoutSession:
    """One conversation. Created per session, disposed with it."""

    def __init__(self, context: AgentSessionContext) -> None:
        self.context = context

    async def send_user_message(self, message: UserMessage, sink: TurnSink) -> None:
        # `sink` is how a turn reaches the client. Text arrives as deltas, so a
        # real adapter streams tokens as the model produces them.
        await sink.text_delta(message.text.upper())

    async def cancel(self, reason: str | None = None) -> None:
        """The user pressed stop. Return promptly; do not raise."""

    async def aclose(self) -> None:
        """The session is going away. Release anything you hold."""


class ShoutProvider:
    """The factory. One per host."""

    @property
    def agent(self) -> AgentInfo:
        return AgentInfo(
            provider="shout",
            display_name="Shouty",
            description="Repeats you, louder.",
            models=(ModelInfo(id="shout-1", name="Shout v1"),),
        )

    async def create_session(self, context: AgentSessionContext) -> AgentSession:
        return ShoutSession(context)


host = Host(ShoutProvider(), LoopbackSingleUserPolicy())
assert isinstance(host, Host)
assert isinstance(ShoutProvider(), AgentProvider)  # structural, no subclassing
```

`AgentProvider`, `AgentSession` and `TurnSink` are `Protocol`s. You never
subclass anything: if your object has the methods, it is one.

## Serving it

```python
import asyncio

from ahp_protocol.transport import memory_pair


async def main() -> None:
    client, server = memory_pair()
    serve = asyncio.create_task(host.serve(server))
    # ... drive `client` ...
    serve.cancel()
    await host.aclose()
```

Over a real socket, use the `ws` extra:

```
pip install "ahp-host[ws]"
```

```python
from ahp_host.ws import serve_websocket  # noqa: F401
```

## Starting a session against it (the non-obvious part)

In VS Code's **Agents** window, picking a *local folder* will never offer your
host. That is not a bug and no host-side change fixes it: the window asks every
provider `resolveWorkspace(folder)` and only those that answer contribute
session types, and the two gates are mutually exclusive —

```text
Copilot / Claude   resolveWorkspace(e) { if (e.scheme !== file) return; ... }
a remote AHP host  resolveWorkspace(e) { if (e.scheme === "vscode-agent-host"
                                            && e.authority === <this host>) ... }
```

Instead, start a new session under **your host's own entry** and use the folder
browser it offers. That dialog is opened with
`availableFileSystems: ["vscode-agent-host"]`, so it browses the directory
*your host serves* rather than the local disk, and it opens at whatever you sent
as `defaultDirectory` in the `initialize` result — `"/"` if you sent none, which
looks broken and is the most common first mistake.

The files are the same files on disk. The workspace URI is
`vscode-agent-host://<authority>/<path>` rather than `file://<path>`, and that
is what makes the session yours.

So a host that wants to be usable must:

- install a resource provider (`--serve-directory` in the demo), or the browser
  has nothing to show;
- send `defaultDirectory`, or the browser opens at the filesystem root;
- resolve **strict ancestors** of its served root, because the dialog stats the
  *parent* of a typed path before the path itself. `RootedFilesystemResourceProvider`
  does this already — see [deploying.md](deploying.md).

## The turn sink

`TurnSink` is what a turn may do. The whole surface:

| Method | What it does |
|---|---|
| `text_delta(text)` | Append to the visible answer. Call repeatedly. |
| `reasoning_delta(text)` | Append to the reasoning/thinking part. |
| `tool_call_started(id, name, input, *, display_name=, intention=, meta=)` | Announce a call. `display_name` is what the user reads. |
| `tool_call_delta(id, content=, *, invocation_message=)` | Stream the parameters, or move the progress line under the tool's name. Call it at any point in the call's life; the host picks the action that state accepts. |
| `tool_call_output(id, content, *, meta=)` | What a still-running call has produced. **Replaces**, so pass everything so far. Moves the call to `running` if it is not there yet. |
| `tool_call_completed(id, result, *, success=, past_tense_message=)` | Finish it. Both keyword fields are **required by the protocol**. |
| `turn_failed(message, error_type=, duration_ms=)` | End in error. `error_type` is required — omitting it renders `Error: (undefined) …`. |
| `usage(*, input_tokens=, output_tokens=, cache_read_tokens=, model=)` | Report the turn's tokens. No usage, no context gauge — the client renders nothing rather than a zero. |
| `request_input(request)` | **Suspends.** Ask a human and wait. |
| `confirm_tool_call(confirmation)` | **Suspends.** Ask before running a tool. |
| `run_client_tool(call)` | **Suspends.** Ask the *client* to run one of its own tools. |

Three of those are optional and easy to skip, and each is invisible in a
different way. Without `tool_call_delta`/`tool_call_output`, a call that takes
thirty seconds is one static row and then everything at once. Without `usage`,
the context gauge does not appear at all. And `meta` is where the protocol's
well-known keys go — `ptyTerminal: {"input": …, "output": …}` is what makes a
client render a shell command as a terminal instead of a row.

`run_client_tool` needs a client that is in the session *now*: it raises
`LookupError` for one that is not, rather than waiting on an answer that cannot
come. `AgentSessionContext.active_client_id` and `client_tools` only describe
the client that created the session, and a restored session has neither. To
follow clients that join, leave or republish their tools, implement
`FollowsActiveClients.active_clients_changed(clients)`. The host calls it with
the whole `activeClients` list after every change, and once more when a restored
session gets its agent back.

A failed call returns a `ToolResult` that is not `accepted`. That covers a
client that refused, one that left, and a tool that ran and reported
`success: false`. Its `reason` carries the result's own text, so the agent can
say what went wrong.

Parts are segmented for you: switching between text, reasoning and tool calls
starts a new response part, so prose written *after* a tool call renders below
it rather than being appended to the part that came first. A run of the same
kind still shares one part.

### What the host publishes for you

Some of what a session list shows is derived from the turn rather than asked of
you:

- **The session's title**, from the text of the first message on the default
  chat. Only while the session is still called "New Session" — a client can
  rename a session, and that rename wins from then on.
- **`session/activityChanged`**, from `tool_call_started`: the `display_name`
  you passed becomes what the session list shows the session doing, and it is
  cleared when the call completes or the turn ends. Without it every working
  session reads as the client's own literal fallback, "Working...".
- **The unread flag.** The host marks a session unread when a turn ends on a
  session no client has open, which is the half of `session/isReadChanged` that
  is the server's.
- **Queued messages.** A follow-up typed while you are working is consumed as
  soon as the chat goes idle, and run as its own turn — you do not poll for it.
  Steering messages are *not* consumed: they are meant to be injected into the
  running turn, which only a provider can do.
- **`!command`**, when a terminal backend is installed. A message starting with
  `!` never reaches you: the host runs the rest in a one-shot terminal and
  reports it as a tool call. Without a backend the prefix is not advertised and
  the message is ordinary text, so you still see it.
- **The `chat/toolCallReady` transition.** A call you announce and then complete
  — or that produces output — is moved out of `streaming` for you: the
  validation table refuses a completion or a `contentChanged` from that state,
  so without it your call would be silently dropped and cancelled at the end of
  the turn. That frame carries the input you passed to `tool_call_started` and
  the last progress message you streamed, because the reducer reads both **from
  it** and stores nulls for anything it omits.
- **Progress on a call that is already running.** `chat/toolCallDelta` only
  reaches a `streaming` call, so once yours is confirmed the host publishes your
  `invocation_message` as a second `chat/toolCallReady` instead — carrying the
  existing confirmation forward, so the call stays running rather than asking
  the user to approve a tool that is already executing.

- **The chat catalogue.** `SessionState.chats[]` is kept in step with each
  chat channel, so a client's chat tabs show real titles, statuses and
  timestamps rather than whatever they were created with.

If you want to say something better than the tool's name, call
`context.activity_changed("Editing core.py")` — it is on `AgentSessionContext`
and works outside a turn too. Note the host clears the activity when a tool call
completes and when the turn ends, so a string you set *during* a call is cleared
along with it; set yours after, or between calls.

The three suspending methods are the interesting ones — see
[ADR 0005](../decisions/0005-suspending-provider-requests.md). They park the
turn until a human answers, and they raise `asyncio.CancelledError` if the turn
ends first, which is the same way ordinary cancellation reaches you. An adapter
that already handles cancellation needs no new code for them.

## Out of turn: the session publisher

`AgentSessionContext.publisher` is a `SessionPublisher`, for what changes when
no turn is running. Hold it for the life of the session.

| Method | What it does |
|---|---|
| `customizations_changed(customizations, server_tools=)` | Republish the session's customization tree. |
| `mcp_server_changed(id, state, channel=)` | An MCP server's lifecycle. Publish `{"kind": "starting", "blocking": True}` if its startup holds back the next message, and implement `BackgroundsMcpServers` to let a client stop waiting. |
| `activity_changed(activity)` / `title_changed(title)` / `config_changed(values)` | Session metadata that moved on its own. |
| `changes_published(changeset, changes, chat=)` | Publish or refresh a changeset; a refresh shows as `recomputing`. With `chat`, it belongs to that chat's catalogue and roll-up. |
| `background_work_set(work, chat=)` / `background_work_removed(id, chat=)` | Work running in the background for a chat. |
| `canvas_set(canvas, chat=)` / `canvas_removed(instance_id, chat=)` | A live canvas on a chat (experimental): an `ahp-canvas:` channel the chat references. Its `url` is never persisted and is redacted from wire logs. |
| `open_tool_chat(title, tool_call_id=, chat=, interactivity=)` | A worker chat for a tool call — a subagent's own conversation. Returns a handle whose `run_turn(prompt, run)` runs a turn on it. |
| `open_terminal(title, chat=, cwd=, turn_id=, tool_call_id=)` | A read-only terminal for output the agent produces; returns a handle with `resource`, `write(data)` and `exited(code)`. |
| `external_turn(text, run)` | A turn that happened somewhere else. |
| `progress(progress, total=, message=)` | Report against `createSession.progressToken`. |

**Background work** is a shell left running or a subagent still going: work
that outlives the turn that started it, so it goes through the publisher, not
the turn sink. Each entry is a `BackgroundWork`, upserted by `id`, and stays
listed until you remove it — the host never removes one when a turn ends,
because a turn ending says nothing about whether the work stopped. A restored
chat starts with no inventory, so publish what is actually still running once
the runtime is back.

```python
from ahp_host.provider.base import BackgroundWork

shell = BackgroundWork(
    id="shell:42",
    kind="shell",
    label="Start the dev server",
    started_at="2026-10-04T12:00:00.000Z",
    command="npm run dev",
    meta={"attached": True},
)
assert shell.to_wire() == {
    "id": "shell:42",
    "kind": "shell",
    "label": "Start the dev server",
    "startedAt": "2026-10-04T12:00:00.000Z",
    "command": "npm run dev",
    "_meta": {"attached": True},
}
```

A shell needs its `command`, and a subagent needs its own `chat`; `to_wire`
raises rather than publish an entry every client would reject.

**Worker chats.** `open_tool_chat` opens a chat whose origin is
`{kind: "tool", chat, toolCallId}` — the reverse of a
`{"type": "subagent", "resource": ..., "title": ...}` content on the spawning
call's result — read-only by default, and moved with its parent by `moveChat`.
`run_turn(prompt, run)` starts a turn on it the way `external_turn` does on the
default chat. A client stopping that turn cancels only your `run`: the
session's agent is not interrupted, so stop the worker yourself on
`CancelledError`.

**Terminals for the agent's own output.** `open_terminal` registers a terminal
channel the session holds, so clients can watch it and none can type into it.
Point a tool result's `{"type": "terminal", "resource": terminal.resource,
"title": ...}` content at it, or a background shell's `terminal`. After
`exited(code)` it stays subscribable with its output, across a host restart
too, until its chat or session is disposed — 1.0.0 requires that of a terminal
a tool result references. It is not in the root terminal catalogue, which is
for interactive shells a client re-attaches to.

## Chats that move

A client can move a chat to another session with `moveChat` (1.0.0). Within one
session that only reorders the catalogue and needs nothing from you. Between
sessions, or into a new session, later turns on the chat run on *another*
session's agent, so the host asks first: implement `TransfersChats` on your
provider, and `chats_transferred(chats, source_session, destination_session)`
is called with every chat that moves, its side chats included, before
anything changes. Return `False` and the move is refused. Without the protocol
your chats can still be reordered, but not moved out.

## Truncation, and the one thing you must not fake

`chat/truncated` is how edit-and-resend works: a client drops the turns after a
point and sends a new message. The reducer rewrites the state, so it *looks*
right whatever your provider does — and if your agent still remembers those
turns, the user has been shown a conversation being rewound that was not.

So the host refuses `chat/truncated` unless the session implements
`history_truncated`:

```python
from ahp_host.provider.base import TruncatesHistory


class ForgetfulSession:
    def __init__(self) -> None:
        self.history: list[str] = []

    async def history_truncated(self, chat: str, turn_id: str | None) -> None:
        """Forget everything after `turn_id`, or everything if it is None."""
        if turn_id is None:
            self.history.clear()


assert isinstance(ForgetfulSession(), TruncatesHistory)  # structural, as always
```

The refusal is stricter than the spec, which gates the action on nothing. It is
deliberate: a visible refusal beats a silent lie, and this is the only place in
the protocol where getting it wrong actively misinforms the user rather than
merely underserving them.

If there is a turn running when truncation arrives, the host cancels it — "if
there is an active turn it is silently dropped and the chat status returns to
`idle`" — so your `cancel` is called as usual.

## What the host does not do for you

- **It does not call a model.** There is no model in this library. `AgentInfo.models`
  is a list you publish for a client's picker; `UserMessage.model` is what the
  user chose. Both are carried, neither is obeyed — routing is yours.
- **It does not touch the filesystem** unless you install a resource provider,
  and not for writing unless you pass `writable=True` as a second, separate act.
- **It does not run commands** unless you install a terminal backend. The
  default declines every terminal with a reason.
- **It does not authenticate anyone.** AHP defines no authentication. See
  [deploying.md](deploying.md) before exposing a host to anything.

## Errors

Raise `AhpError` with a protocol code, or let an exception escape a turn — the
host converts that into a `chat/error` with `errorType` `agent.turn` and the
exception's text.

```python
from ahp_host import AhpError

error = AhpError(-32009, "Not permitted")
assert error.code == -32009
```

The codes are the spec's own: `-32001 SessionNotFound`, `-32002
ProviderNotFound`, `-32003 SessionAlreadyExists`, `-32007 AuthRequired`,
`-32008 NotFound`, `-32009 PermissionDenied`, `-32010 AlreadyExists`,
`-32011 Conflict`.

```python
from ahp_protocol.types import AHP_ERROR_CODES

assert AHP_ERROR_CODES["PermissionDenied"] == -32009
assert AHP_ERROR_CODES["NotFound"] == -32008
```

## Where to look next

- [deploying.md](deploying.md) — the security posture, and what to do before
  exposing a host.
- `src/ahp_host/provider/echo.py` — the reference adapter. It is the
  smallest complete example of every optional surface, and it is what the demo
  runs.
- [ADR 0003](../decisions/0003-provider-emits-neutral-events.md) — why a
  provider emits neutral events rather than wire actions.
