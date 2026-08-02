# Security

## Reporting a vulnerability

Report privately through [GitHub's advisory
form](https://github.com/jackhumbert/agent-host-server-py/security/advisories/new).
Please do not open a public issue for a vulnerability.

Include what you did, what happened, and what you expected. A wire log
(`--wire-log`) or a minimal script that reproduces it is worth more than a
description — but read [what is logged](#what-the-logs-contain) before attaching
one.

## What this library is, in security terms

It implements a protocol that **defines no authentication**. A peer that
completes `initialize` is, as far as AHP is concerned, entitled to everything
the host exposes. This library cannot close that on your behalf, so instead it
does two things:

- every dangerous surface is **off by default**, and
- the one decision it cannot make — *who is this peer* — is a `Policy` you write.

[`docs/guide/deploying.md`](docs/guide/deploying.md) is the long version, and
its examples are executed as tests so they cannot rot.

## The three surfaces that matter

| Surface | Off by default | What it grants when on |
|---|---|---|
| Reading files | `NullResourceProvider` answers NotFound to everything | Everything under one root, to any admitted peer |
| Writing files | second, separate opt-in (`writable=True`) | Creating, overwriting, moving and **deleting** under that root |
| Running commands | `RefusingTerminalBackend` declines with a reason | **Arbitrary command execution as the host user** |

Reading and writing are deliberately two acts. Reading discloses; writing
destroys. Granting both with one gesture is how a review misses the second.

The pty backend is the largest: every other surface reads, writes or describes,
and that one executes. It is constructed by name and never arrives by upgrading.

### The filesystem jail

`RootedFilesystemResourceProvider` walks a path one component at a time with
`openat` and `O_NOFOLLOW`, resolving symlinks itself and re-checking each
against the root. It is deliberately **not** realpath-then-open: that has a
window in which a component can be swapped for a symlink after the check and
before the open, which is the classic jail escape. There is a test for that
race.

If you enable writes, the jail is the **entire** boundary between a peer and
your filesystem.

## Scope

In scope, and treated as vulnerabilities:

- Escaping the resource jail, in either direction (read or write).
- Reaching a channel a `Policy` refused — including by guessing or deriving a
  URI. Channel URIs are client-chosen and opaque; nothing may be inferred from
  their shape.
- Causing unbounded memory or descriptor growth from a peer's behaviour.
- Executing anything without a terminal backend installed.
- Crashing or wedging a host with malformed peer input. Every field arriving
  over the wire is untrusted, including what a previous run wrote to a store.

Out of scope, because they are the documented posture rather than defects:

- A peer doing anything the `Policy` allows. `LoopbackSingleUserPolicy` permits
  everything, and is safe **only** because the demo binds to loopback.
- A host that installed a pty backend running commands. That is the feature.
- The demo's connection token being guessable, shared, or visible in a process
  listing. It is a convenience against other local processes, not an
  authentication scheme, and it says so.
- Anything reachable only by an embedder's own code. The library does not defend
  against the process it is running inside.

## What the logs contain

Two files with very different sensitivities:

- **The wire log** (`--wire-log`) records every frame. Treat it as containing
  everything the user said and everything the agent answered. Bearer tokens are
  redacted; nothing else is.
- **The audit sink** records *decisions* — who connected, what was refused, who
  approved a tool call — and carries no conversation content by construction.

Attach the second to a report freely. Read the first before you attach it.

## Supported versions

Pre-1.0: the latest minor is supported. The protocol this implements moves
weekly and lands breaking changes in MINOR bumps, so
[`UPSTREAM.md`](UPSTREAM.md) records which protocol versions each release
speaks.
