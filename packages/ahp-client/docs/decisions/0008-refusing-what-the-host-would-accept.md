# 0008 — Refusing what the host would accept

**Status:** accepted
**Date:** 2026-08-02
**Supersedes:** nothing. **Relates to:** §7.4 and §7.5 of `docs/plan.md`.

## Context

The typed changeset and terminal surfaces raise on calls the sibling host
answers happily. That is unusual here. §1.3's whole argument is that "a wrapper
is a place to be wrong", and this repository has an explicit rule against
ceilings: *a library refusing a call the host would accept* is the reason
`Changeset.invoke` does **not** gate on operation `status`, and the reason
`Terminal.hand_to`, `resize`, `rename` and `clear` are not claim-gated.

Two reviewers asked, from opposite directions, why the same reasoning does not
delete the gates that remain. It needs to be written down once, because the next
person to add a surface will re-litigate it.

## Decision

A local refusal is justified only when **all four** of the following hold. Where
any one fails, the call goes out.

1. **The wire accepts it.** If the schema forbids the value, the host rejects it
   and the error is legible; nothing is gained.
2. **The host does not check it either** — or checks it in a way the caller
   cannot see. `dispatchAction` is a notification: a refused `terminal/input`
   comes back as a `rejectionReason` on a stream nobody is obliged to read.
3. **The failure is silent, not loud.** The library's own bar. An action that
   matches no reducer is numbered, broadcast and applied to nothing; a `side` no
   handler recognises is read as neither side.
4. **The caller could not have known.** The check uses something the caller does
   not hold: the changeset's live `capabilities`, the terminal's confirmed
   claim, the reducer's `new Set(action.files)` semantics.

The gates that clear all four, with the fact each one turns on:

| Gate | The fact the caller does not hold |
|---|---|
| `invoke(confirmed=…)` on a `confirmation` | It is a client **MUST** ("only invoke the operation after the user accepts"), the host cannot enforce it, and the sibling's `ahs-revert` really does `git checkout HEAD --`. |
| `mark_reviewed` on `capabilities.review` | The flag is on the *catalogue entry*, not in `ChangesetState`, and it moves mid-session. |
| `mark_reviewed` on unknown file ids | "If none match, the action is a no-op." The parameter is `ChangesetFile.id` and the file list is in hand. |
| `invoke(side=…)` against `before`/`after` | A closed enum on both target branches that **nothing** in the stack validates — grep the host for `"side"`. |
| `expand()` on an unexpandable template | An RFC 6570 operator or unknown name expands to a channel string no host registered. Subscribing it succeeds and then publishes nothing, forever. |
| `write()` on the confirmed claim | See (2). The reported symptom is "typing does nothing". |
| `ClientClaim("")` / `SessionClaim("")` | `terminal/claimed` is itself claim-gated, so a terminal handed to a `clientId` no connection has can never be taken back **by anyone**. |
| `rename("")` | `TerminalState.title` is required, `""` is valid, and it renders as a nameless tab in every subscriber's catalogue. |
| `resize()` on fractional cells | The host takes `cols if isinstance(cols, int) else None`, so `960 / 12` is dropped and the pty silently runs at the backend default. |

And the ones that fail the test, kept as documented non-gates:

- **Operation `status`.** The host *does* refuse a disabled operation while a
  turn is active — the earlier claim that it does not was wrong and is corrected
  in §7.4 — but the status is host-pushed and can be stale, so refusing on it
  would block a legitimate retry. Fails (4): a stale `running` is not knowledge.
- **`invoke(resource=…)` membership.** `ChangesetFile.id` is "typically
  `after.uri`", a rename's legitimate target may be `before.uri`, and that is
  what `side` exists for. Fails (4) in the other direction: the check would
  produce false refusals on exactly the case it appears to help with.
- **`hand_to` / `resize` / `rename` / `clear` on the claim.** The guide's detach
  flow has a client narrowing a *session's* claim and resizing a terminal it
  never held. Fails (1) and (2): it is a documented interaction and the host is
  the one entitled to apply the rule.
- **A session URI `hand_to` does not recognise.** Tempting, because the mistake
  is unrecoverable — but a session claim may name a session this connection
  never opened, so the client's own list is not authority. Fails (4).

## Consequences

- Every gate above names its evidence at the call site and has a test that fails
  without it. A gate with no such test is not a gate, it is a guess.
- `force=True` exists on `write()` and `confirmed=True` on `invoke()`: where the
  rule is a *host policy* rather than a protocol MUST, the escape hatch keeps
  the refusal from becoming a ceiling.
- Argument refusals raise `InvalidArgument`, which is both an `AhpClientError`
  and a `ValueError`. The documented handler for the review gate is
  `except AhpClientError`; before this it caught the capability refusal and
  missed the empty-batch refusal one line away.
- **Resource watches get no wrapper**, and this is the same test applied to a
  whole surface: `createResourceWatch` already returns the channel to subscribe
  to, nothing about it is silent, and a `watch()` helper that hid the
  receiver-assigned channel would make the spec's own advice — do not derive it,
  subscribe to what comes back — unfollowable. §1.3's reasoning stands unchanged.
