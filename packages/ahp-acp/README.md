# ahp-acp

A plain [Agent Client Protocol](https://agentclientprotocol.com/) (ACP) client
for Python: spawn an agent, speak newline-delimited JSON-RPC 2.0 over its stdio.
Standard library only, and it imports nothing from the other packages in this
repository.

```python
import asyncio
from ahp_acp import AcpClient, AgentMessageChunk, SessionUpdate, allow_once


async def on_update(update: SessionUpdate) -> None:
    if isinstance(update, AgentMessageChunk):
        print(update.content.get("text", ""), end="")


async def on_permission(params: dict) -> dict:
    return allow_once(params["options"])


async def main() -> None:
    async with AcpClient(
        ["my-acp-agent"],
        client_info={"name": "example", "version": "0.1"},
        on_update=on_update,
        on_permission=on_permission,
    ) as client:
        await client.initialize()
        session = await client.new_session("/path/to/project")
        await client.prompt(session["sessionId"], [{"type": "text", "text": "hello"}])


asyncio.run(main())
```

## What it gives you

- **`AcpConnection`**: the transport. Notifications are handled in order before
  the next line is read; requests from the agent run on their own tasks.
  `spawn` can start the agent with an exact environment (`inherit_env=False`),
  in its own process group (shutdown is close stdin, SIGTERM the group, SIGKILL
  the group, each after a grace period), as another `user`/`group`/`umask`
  (POSIX), and with an over-long line either closing the connection or skipped.
- **`AcpClient`**: `initialize`, the session methods (`new_session`,
  `load_session`, `resume_session`, `fork_session`, `close_session`,
  `list_sessions`, `set_mode`, `set_config_option`, `prompt`, `cancel`) and
  routing for what the agent sends back. A method the agent did not advertise
  raises `UnsupportedError` before anything is sent. Every method takes `meta`
  (sent as `_meta`) and `**extra` top-level params; `request` and `notify` reach
  extension methods.
- **`parse_update`**: `session/update` as frozen dataclasses. A kind it does not
  know arrives as `UnknownUpdate` with the raw object, never an error, so a new
  protocol revision shows up immediately.

`session/fork` and its capability are on the schema's unstable track.

## Schema drift

`tests/fixtures/schema/` holds the upstream `schema.json` (stable and unstable
tracks) from tag `schema-v1.26.0`. `tests/test_updates.py` fails when that schema
has a `sessionUpdate` kind that is neither typed nor in `DELIBERATELY_UNTYPED`.
To bump: copy the new files in, run the tests, decide each new kind.

## Develop

```bash
uv sync --all-packages --all-extras --all-groups
scripts/check.sh ahp-acp
```
