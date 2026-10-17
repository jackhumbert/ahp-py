# ADR 0006 — Open input requests across a restart

**Status:** accepted · **Date:** 2026-10-17 · **Builds on:** graceful restarts
(`Host.drain`, README "Restarts drain first")

## Context

Graceful restarts settled what happens to a **turn** the host stops in the
middle of: restore fails it with `errorType` `host.restart`, its partial
response kept, resumable when the agent can resume turns. A turn a drain-restart
cut off (`interruptedByRestart`) is resumed once, automatically, through the
standard `chat/turnResume` path (`ResumesTurns.resume_turn`); a turn lost to a
crash waits for the user to resume it. That model stands, and this ADR does not
add a second one.

What it left open is what such a turn had **asked**. The pending registry is in
memory ([ADR 0005](0005-suspending-provider-requests.md)), so an approval or a
question dies with the process, while its `session/inputNeeded` entry is part of
the stored session state and comes back. Restored as it was, the session reads
"input needed" for good, and a client answers a request nothing listens to.

An earlier draft of this ADR kept the turn open at restore and let the agent
re-bind requests under their original ids, through a separate provider
capability. It was dropped for the graceful-restart model: one resume path, the
one the spec already defines, rather than two.

## Decision

**Persist the open requests.** `StoredSession.input_requests` (JSON
`inputRequests`) records each open request from the pending registry: `id`,
`kind` (`confirm`/`input`/`clienttool`/`auth`), `chat`, `turnId` and, where it
has one, the registry `key`. Beside the session's own `inputNeeded`, which is
what clients were shown.

**Withdraw every one on restore, and say so.** Nothing in the new process can
answer a stored request: the turn that asked is failed, and a resumed turn runs
with a fresh sink, so it asks again under new ids if it still needs to. So
`restore()` removes each request's `inputNeeded` entry and adds one
`systemNotification` naming them to the interrupted turn, just before its
`host.restart` error. The chat reducer's failure already cancels a tool call
still awaiting confirmation, and the restart's failure drops an unanswered
question part, so the turn holds no live request either. A request that was
never stored as a record (a file from before `inputRequests`) is found from its
`inputNeeded` entry alone and withdrawn the same way; one whose turn was no
longer active has no turn to hold a notice and is removed with a log line. An
`mcp:` entry is the session's MCP server asking for a credential, not a turn's
request, and is left for the resumed agent to report again.

**No re-binding.** A request is never re-registered under its old id. Fitting
it into the resume path would need the provider to re-raise "the same" request
through a sink that mints ids itself (ids never come from a provider), and a
client that answered the old id would still be answering a prompt the new
agent never made.

**`_meta['ahp-py/restart']` instead of a new wire field.** The notice carries
`{"event": "inputWithdrawn", "requests": [<ids>]}`. A client that does not know
the key still shows the text. A standard shape is requested upstream
(`docs/requests.md` entry 11).

**Shutdown persists first, then freezes the store.** `aclose()` writes every
session -- the interrupted list, the active turns, the open requests, and the
resume state while the agents can still report it -- before it cancels
anything, and `_persist` writes nothing after. Cancelling a turn retracts its
requests; persisted after that, the store would hold what shutdown left, not
what was open.

**The store format version is unchanged.** `inputRequests` and
`interruptedByRestart` are additive; files without either load.

## Consequences

- The user always learns what became of an approval they had not given yet:
  withdrawn, by name, in the turn that asked. A resumed turn that still needs
  it asks again.
- A client that answers after the restart under the old id is refused like any
  answer to an unknown request.
- A hard kill (a crash) loses at most the store debounce (1 s by default): the
  requests and turns recorded are the last ones persisted.
- Clients learn what happened only through the notice convention until upstream
  standardises one.
