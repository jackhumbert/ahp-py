# ADR 0004 — Port every reducer branch for in-scope channels, not just what v0.1 emits

**Status:** accepted · **Date:** 2026-08-01

## Context

v0.1's echo provider exercises roughly six actions: the four-action minimal turn
plus session bring-up. But root, session and chat together define **60 actions**
and **1,323 lines** of upstream reducer logic.

The obvious economy is to implement only the branches the provider drives and
add the rest as they are needed.

## Decision

**Port every reducer branch for root, session and chat in v0.1, and gate on the
whole fixture corpus.**

## Rationale

**1. The corpus is all-or-nothing per reducer.** The 247 fixtures are keyed by
`reducer`, not by action. Implementing a subset means skipping fixtures — 123
chat fixtures and 70 session fixtures would have to be filtered by inspecting
each one's action list. At that point the conformance claim is gone, and the
project's entire value proposition with it. "Conformant" is a binary property of
a reducer.

**2. A host does not choose which actions it reduces.** It reduces what it is
*sent*. Any connected client may dispatch any of the 40 client-dispatchable
actions, and the host must apply, validate or reject each one and broadcast the
echo. An unimplemented branch is not "unused" — it is a silent state divergence
between the host and every other client the moment a client uses it.

**3. The cost is front-loaded, not additive.** The reducers are the one part of
this project whose correctness is fully determined externally. Porting 1,323
lines against 247 executable fixtures is mechanical and self-checking. Doing it
incrementally means repeatedly re-reading the same TypeScript for context and
re-deriving the same JS-semantics hazards, which is more total work and more
opportunity for the hazards in `docs/research.md` §2f to slip through.

**4. It is the cheapest possible way to be trustworthy.** Passing all 247
fixtures is a claim other implementations can check. Passing 30 of them is not a
claim at all.

## Consequences

- Step 2 of the build order is larger than it looks and should not be split
  across many PRs — but each of the seven reducers is independently gateable, so
  root → session → chat is a natural three-PR sequence.
- Terminals, changesets, annotations and resource-watch stay **out** (their 25
  actions and 360 reducer lines add four more state vocabularies for no v0.1
  client benefit). This ADR is about completeness *within* the chosen channels,
  not about widening them.
- The hazard tests in `docs/research.md` §2f are part of this work, not a
  follow-up: the corpus does not pin JavaScript truthiness, `??` semantics, or
  int32 bitwise coercion, so passing it is necessary and not sufficient.
