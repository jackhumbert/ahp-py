# Deploying a host

Read this before a host is reachable by anything you do not control.

Everything here is executed by the test suite
(`tests/docs/test_guide_examples.py`), including the assertions about defaults.
If a default changes, this page fails rather than quietly becoming wrong.

## The protocol has no authentication

AHP defines none. There is no login, no session token in the spec, no notion of
a user. A peer that completes `initialize` is, as far as the protocol is
concerned, entitled to everything the host exposes.

That is not a gap this library can close on its own, so it does two things
instead: it makes every dangerous surface **off by default**, and it puts the
one decision it cannot make — *who is this peer* — in a `Policy` you write.

## Everything dangerous is off by default

Verified here rather than asserted:

```python
from agent_host_server import Host, LoopbackSingleUserPolicy
from agent_host_server.core.resources import NullResourceProvider
from agent_host_server.core.terminals import RefusingTerminalBackend
from agent_host_server.provider import EchoProvider

host = Host(EchoProvider(), LoopbackSingleUserPolicy())

# No filesystem. `NotFound` to everything, not `PermissionDenied` — a host with
# no resource provider has no resources, and "denied" would tell a peer that
# something is there.
assert isinstance(host.resources, NullResourceProvider)

# No command execution. Declines with a reason rather than an AttributeError.
assert isinstance(host.terminals, RefusingTerminalBackend)

# No resource watching, no durable session store path, no root config schema.
assert host.watcher is None
assert host.root_config is None
```

A host does not acquire any of these by being upgraded. Each is an argument you
pass by name.

## The three you have to opt into

| Surface | How you enable it | What it grants |
|---|---|---|
| Reading files | `resources=RootedFilesystemResourceProvider(path)` | Everything under `path`, to any admitted peer |
| Writing files | the same, plus `writable=True` | Creating, overwriting, moving and **deleting** under `path` |
| Running commands | `terminals=PtyTerminalBackend()` | Arbitrary command execution as the host user |

Reading and writing are deliberately two separate acts. Reading discloses;
writing destroys. Granting both with one gesture is how a review misses the
second.

The pty backend is the largest of the three: every other surface reads, writes
or describes, and that one executes. In the demo it is behind its own
`--terminal` flag for that reason.

### The filesystem jail

`RootedFilesystemResourceProvider` walks a path one component at a time with
`openat` and `O_NOFOLLOW`, resolving symlinks itself and re-checking each
against the root. It is deliberately **not** realpath-then-open: that has a
window in which a component can be swapped for a symlink after the check and
before the open, which is the classic jail escape.

Strict ancestors of the root resolve — a bare `{type: "directory"}` with no
metadata — and list only the next component toward the root. That is not a
weakening: a client's directory picker stats the *parent* of a typed path, and a
jail that refuses everything above its root makes every path look nonexistent.
Reads, writes and watches on ancestors stay refused.

## The Policy is the part only you can write

`Policy` has eleven decision points, not one, so implement it by narrowing a
base rather than from scratch — otherwise you have to answer questions you have
no opinion about, and the ones you forget are the ones that matter.

```python
from agent_host_server import ConnectionInfo, LoopbackSingleUserPolicy, Policy


class OnlyAlice(LoopbackSingleUserPolicy):
    """Refuse anyone whose bearer token is not Alice's."""

    def authorize_connection(self, info: ConnectionInfo) -> bool:
        return info.headers.get("authorization") == "Bearer alice-secret"


assert isinstance(OnlyAlice(), Policy)  # structural, no registration
assert not OnlyAlice().authorize_connection(
    ConnectionInfo(client_id="c", peer="1.2.3.4", token=None, headers={})
)
assert OnlyAlice().authorize_connection(
    ConnectionInfo(
        client_id="c",
        peer="1.2.3.4",
        token=None,
        headers={"authorization": "Bearer alice-secret"},
    )
)
```

The other ten — `may_see_channel`, `may_create_session`, `may_access_resource`,
`may_create_terminal`, `may_dispatch`, `may_grant_working_directory`,
`may_invoke_operation`, `may_push_token`, `may_restore_session`,
`may_set_root_config` — are each a place a real deployment has an opinion.

`LoopbackSingleUserPolicy` permits everything, and is safe **only** because the
demo binds to loopback. It is not a starting point for a deployment; it is the
absence of one.

## The demo is a demo

`python -m agent_host_server` uses `LoopbackSingleUserPolicy`, binds to
`127.0.0.1`, and refuses to bind elsewhere without `--allow-remote`. The
connection token it prints is a convenience against other local processes, not
an authentication scheme — it is a shared secret in a URL, visible in process
listings.

## What is logged

The wire log (`--wire-log`) records every frame, and it redacts bearer tokens.
The audit sink records *decisions* — who connected, what was refused, who
approved a tool call — and by construction carries no conversation content.
Those are different files with different sensitivities; treat the wire log as
containing everything the user said.
