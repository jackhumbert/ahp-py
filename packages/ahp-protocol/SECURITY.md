# Security

## Reporting a vulnerability

Report privately through [GitHub's advisory
form](https://github.com/jackhumbert/agent-host-protocol-py/security/advisories/new).
Please do not open a public issue for a vulnerability.

## What this package is, in security terms

**It performs no I/O and holds no credentials.** No sockets, no filesystem, no
subprocesses, no network. There is an `import-linter` contract that machine-
proves it: `types` may not import `asyncio`, `socket`, `pathlib`, `json`, `os`
or `websockets`, and the whole package has zero runtime dependencies,
permanently.

So the attack surface is not "what can this reach" — it is nothing — but
**what it computes for the peers that trust it**. This is the floor a host and
a client both stand on. A reducer that produces the wrong state produces it
identically on both sides of a connection, which is precisely the class of
defect that no amount of interop testing between two consumers of this package
will catch.

### The three things that matter

| Surface | Why it is security-relevant |
|---|---|
| The seven reducers | Both peers derive their view of who said what from these. A reducer that mis-applies an action mis-applies it everywhere. |
| `IS_CLIENT_DISPATCHABLE` | The generated table a host uses to decide whether a peer may dispatch an action at all. An entry wrongly set to `true` is an authorisation hole in every host that trusts it. |
| The wire types and `js.py` | Parsing peer-supplied JSON. Every value here arrives from someone else. |

`IS_CLIENT_DISPATCHABLE` is **generated** from the vendored upstream
TypeScript rather than hand-kept, and CI fails if the checked-in copy differs
from what the generator produces. That is deliberate: a hand-maintained
authorisation table drifts, and drift here is silent.

## Scope

In scope, and treated as vulnerabilities:

- A reducer that disagrees with upstream's on any input the corpus covers, or
  that mutates the state it was given (it must not — every fixture asserts
  non-mutation, which the JSON fixtures themselves cannot express).
- `IS_CLIENT_DISPATCHABLE` or `ACTION_INTRODUCED_IN` diverging from the pinned
  upstream source.
- Unbounded memory or CPU from peer-supplied input: a deeply nested value, a
  huge array, a pathological string. Everything here takes untrusted JSON.
- A crash on malformed input. A reducer must skip what it cannot read, not
  raise — a host applying replayed state cannot afford an exception.
- Anything in `js.py` that diverges from JavaScript semantics in a way that
  changes a reducer's output. The two opposing null-comparison rules are load-
  bearing and are pinned by a generated oracle (`test_js_semantics.py`).

Out of scope:

- What a consumer does with the result. This package decides what the state
  *is*; it has no opinion on who may see it. Access control belongs to the
  host — see [`agent-host-server-py`](https://github.com/jackhumbert/agent-host-server-py)'s
  own `SECURITY.md`.
- Upstream's protocol design. AHP defines no authentication and says so; that
  is a property of the specification, not of this implementation of it. Report
  it to [upstream](https://github.com/microsoft/agent-host-protocol).
- The vendored corpora under `vendor/upstream/`. They are data copied verbatim
  from a pinned tag; a defect in them is upstream's.

## Supported versions

Pre-1.0: the latest minor is supported. This package's version is independent
of the protocol's, but its MINOR moves whenever the vendored spec tag's MINOR
does — see [`UPSTREAM.md`](UPSTREAM.md) for the current pin.
