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

Symlinks are followed for *reading* — the walk resolves each one itself and
re-checks the result against the root — but a **write whose final component is a
symlink is refused** with `PermissionDenied`, not followed. `resourceWrite` is
the one command whose policy check runs against the name the peer sent (a file
that does not exist yet has nothing to canonicalise), so following the link
would land the write on a path `may_access_resource` never saw.

#### On Windows

Windows' Python has no `dir_fd` and no `O_NOFOLLOW`, so on Windows
`RootedFilesystemResourceProvider(path)` builds a separate, **read-only** jail
(`core/resources_windows.py`) — the same class name, selected by platform.
`writable=True` raises `ValueError` there rather than serving an unsafe write
path. Read-only is what a client needs to browse for a folder.

It opens each component relative to its parent's *handle* with `NtCreateFile`
and `FILE_OPEN_REPARSE_POINT` — `openat` and `O_NOFOLLOW` in NT terms — so no
path string is ever re-parsed after it was checked. Handles are opened without
`FILE_SHARE_DELETE`, so a component held by the walk cannot be renamed or
swapped, and each one is checked with `GetFinalPathNameByHandleW` to be the
direct child of the one before it. Symlinks and junctions that stay inside the
root are followed by re-walking from the root; one that leaves it is refused.
The served root is re-opened and compared *by file identity* on every walk, so
replacing it with a junction does not move the jail.

Accepted: `file:///C:/Users/me/proj` and VS Code's `file:///c%3A/Users/me/proj`,
case-insensitively. Results always come back as `file:///C:/…` in the stored
case and long (never 8.3) names. Refused: `..`, alternate data streams
(`file.txt:stream`), device names (`CON`, `NUL`, `COM1`…), names ending in a dot
or space, `\\?\` and UNC spellings, and any authority but `localhost`.

Limits, each of which fails closed: roots must be on a local drive letter (not
a network share or mapped network drive); an 8.3 spelling of the *root* is not
recognised; files whose content a filesystem filter supplies (OneDrive
placeholders, dedup, WOF compression) list and resolve but are not read; and
resource watches are POSIX-only.

### Reads are bounded

`resourceRead` has no offset or length in the protocol, so a file that is too
big to send cannot be sent in pieces — it can only be refused. The bound is a
constructor argument, because an embedder serving large assets has to be able to
lift it:

```python
from agent_host_server import Host, LoopbackSingleUserPolicy
from agent_host_server.core.host import DEFAULT_MAX_READ_BYTES
from agent_host_server.provider import EchoProvider

assert DEFAULT_MAX_READ_BYTES == 16 * 1024 * 1024
assert Host(EchoProvider(), LoopbackSingleUserPolicy()).max_read_bytes == DEFAULT_MAX_READ_BYTES

# Raise it for a host that serves video or model weights…
big = Host(EchoProvider(), LoopbackSingleUserPolicy(), max_read_bytes=512 * 1024 * 1024)
assert big.max_read_bytes == 512 * 1024 * 1024

# …or remove it entirely, which is what the host did before anyone measured it:
# one unprivileged read of a 64 MiB file took resident memory from 29 MB to
# 970 MB, because base64 and JSON multiply the file several times over.
unbounded = Host(EchoProvider(), LoopbackSingleUserPolicy(), max_read_bytes=None)
assert unbounded.max_read_bytes is None
```

A read above the bound answers `PermissionDenied` (-32009); the file still
resolves and still lists, because the bound is on the bytes rather than on the
existence of the thing.

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

## Restoring, and knowing when you are ready

`Host.restore()` is **explicit** — nothing in the library calls it. Sessions a
previous run persisted come back only when you ask, before you serve:

```python
import asyncio

from agent_host_server import Host, LoopbackSingleUserPolicy
from agent_host_server.core.store import FileSessionStore
from agent_host_server.provider import EchoProvider


async def start(path: str) -> tuple[Host, int]:
    host = Host(EchoProvider(), LoopbackSingleUserPolicy(), store=FileSessionStore(path))
    restored = await host.restore()  # ← nothing calls this for you
    return host, restored
```

A restored session has no live agent yet: the provider is asked to resume
lazily, on the first turn, so a host with a hundred stored sessions does not
spawn a hundred agent runtimes at startup.

### Liveness and readiness

`Host.counters()` is deliberately **not** a metrics endpoint — what scrapes it
is yours. But every deployment then writes the same twenty lines, so here they
are. Readiness in particular is not guessable from outside: "restore finished
and the transport is bound" is a fact only the host has.

```python
import json


class Readiness:
    """Wrap a host with the two answers a supervisor needs."""

    def __init__(self, host: Host) -> None:
        self.host = host
        self.restored = False
        self.serving = False

    def live(self) -> bool:
        """The process is up. Weak on its own — the interesting failures all
        keep the port open."""
        return True

    def ready(self) -> bool:
        """Restore finished AND the transport is bound. Before both, a client
        that connects sees an empty session list and concludes its work is
        gone."""
        return self.restored and self.serving

    def report(self) -> str:
        counters = self.host.counters()
        return json.dumps({"ready": self.ready(), **counters})


host = Host(EchoProvider(), LoopbackSingleUserPolicy())
probe = Readiness(host)
assert not probe.ready()
probe.restored = probe.serving = True
assert probe.ready()

payload = json.loads(probe.report())
assert payload["ready"] is True
# The one to watch: it only grows when providers are waiting on clients that
# are not answering.
assert "pendingRequests" in payload
assert "outboxOverflows" in payload
```

Serve that on loopback, or on a port your orchestrator can reach and nobody
else — the counters disclose session and connection counts.

## Backpressure: a slow peer is disconnected, not buffered

A connection's outbox is bounded. A peer that stops reading — a suspended
laptop, a wedged renderer, a client behind a stalled proxy — hits the limit and
**the connection is closed**, rather than frames being dropped or the host
blocking.

```python
from agent_host_server.core.connection import DEFAULT_OUTBOX_LIMIT

assert DEFAULT_OUTBOX_LIMIT >= 1024
host = Host(EchoProvider(), LoopbackSingleUserPolicy(), outbox_limit=4096)
assert host.outbox_limit == 4096
```

Closing is the option with a **recovery path**: the protocol already handles
"you missed things" — the peer reconnects, and `reconnect` either replays the
gap or answers with fresh snapshots and a `missing` list. A silently dropped
frame has no such path, and the ordering guarantee is exactly what makes a hole
undetectable. Blocking is not available: `enqueue` runs inside the sequencer's
critical section, so one slow peer would stall every other client.

`counters()["outboxOverflows"]` counts them. A climbing value means a client or
a proxy is not draining, and explains disconnects that otherwise look
mysterious.

The reply to `reconnect` is bounded by the same reasoning. Its `subscriptions`
list is peer-supplied and the schema does not forbid repeats, so a request
naming one URI a thousand times used to be answered with a thousand copies of
every missed envelope — a 50 KB frame in, 62 MB out, from a peer that has only
completed `initialize`. The list is de-duplicated, so the answer is bounded by
the replay budget rather than by the length of the request.

## What is logged

The wire log (`--wire-log`) records every frame, and it redacts bearer tokens.
The audit sink records *decisions* — who connected, what was refused, who
approved a tool call — and by construction carries no conversation content.
Those are different files with different sensitivities; treat the wire log as
containing everything the user said.
