# Writing a provider

A **provider** is the only thing you have to write. It is the seam between this
library — which speaks the protocol, sequences state, and talks to clients — and
your agent, which does the thinking.

Everything in this file is executed by the test suite. If an example here stops
working, `tests/docs/test_guide_examples.py` fails. Documentation that can drift
does drift, so none of this is allowed to.

## The smallest host that works

```python
from agent_host_server import AgentProvider, Host, LoopbackSingleUserPolicy
from agent_host_server.provider.base import (
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

from agent_host_server.transport import memory_pair


async def main() -> None:
    client, server = memory_pair()
    serve = asyncio.create_task(host.serve(server))
    # ... drive `client` ...
    serve.cancel()
    await host.aclose()
```

Over a real socket, use the `ws` extra:

```
pip install "agent-host-server[ws]"
```

```python
from agent_host_server.ws import serve_websocket  # noqa: F401
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
| `tool_call_started(id, name, input, *, display_name=...)` | Announce a call. `display_name` is what the user reads. |
| `tool_call_completed(id, result, *, success=, past_tense_message=)` | Finish it. Both keyword fields are **required by the protocol**. |
| `turn_failed(message, error_type=, duration_ms=)` | End in error. `error_type` is required — omitting it renders `Error: (undefined) …`. |
| `request_input(request)` | **Suspends.** Ask a human and wait. |
| `confirm_tool_call(confirmation)` | **Suspends.** Ask before running a tool. |
| `run_client_tool(call)` | **Suspends.** Ask the *client* to run one of its own tools. |

The three suspending methods are the interesting ones — see
[ADR 0005](../decisions/0005-suspending-provider-requests.md). They park the
turn until a human answers, and they raise `asyncio.CancelledError` if the turn
ends first, which is the same way ordinary cancellation reaches you. An adapter
that already handles cancellation needs no new code for them.

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
from agent_host_server import AhpError

error = AhpError(-32009, "Not permitted")
assert error.code == -32009
```

The codes are the spec's own: `-32001 SessionNotFound`, `-32002
ProviderNotFound`, `-32003 SessionAlreadyExists`, `-32007 AuthRequired`,
`-32008 NotFound`, `-32009 PermissionDenied`, `-32010 AlreadyExists`,
`-32011 Conflict`.

```python
from agent_host_server.types import AHP_ERROR_CODES

assert AHP_ERROR_CODES["PermissionDenied"] == -32009
assert AHP_ERROR_CODES["NotFound"] == -32008
```

## Where to look next

- [deploying.md](deploying.md) — the security posture, and what to do before
  exposing a host.
- `src/agent_host_server/provider/echo.py` — the reference adapter. It is the
  smallest complete example of every optional surface, and it is what the demo
  runs.
- [ADR 0003](../decisions/0003-provider-emits-neutral-events.md) — why a
  provider emits neutral events rather than wire actions.
