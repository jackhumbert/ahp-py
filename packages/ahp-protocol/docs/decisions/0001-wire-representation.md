# ADR 0001 — Wire values are plain dicts with TypedDict views

**Status:** accepted · **Date:** 2026-08-01

## Context

The protocol imposes an unusual set of simultaneous requirements on a type
layer (evidence in the sibling host repo's [`docs/research.md`](https://github.com/jackhumbert/agent-host-server-py/blob/main/docs/research.md) §2f, §5, where this decision was originally taken):

1. **Unknown fields must survive verbatim.** No schema uses
   `additionalProperties: false`, and the reducer corpus tests preservation
   directly (fixture `103` keeps an unknown `ResponsePart.kind` across a
   reduction).
2. **Unknown union variants must survive verbatim** — fixture
   `002-state-action-unknown-variant-preserved`.
3. **`null` and absent are distinct** on the wire (round-trip corpus), while the
   *reducer* corpus treats fixture `null` as absent. Two different rules over the
   same payloads.
4. **Unknown enum values must not raise.** Upstream issue #366 records that every
   generated client hard-fails here today; we should not reproduce that.
5. **Integers exceed int32** (`serverSeq: 2147483720`) and `SessionStatus` is a
   bitset whose unknown high bits must survive.
6. A host is **authoritative for state it replays to other clients**, including
   clients newer than itself.

Requirement 6 is the decisive one. A host that parses state into closed models
and drops what it does not recognise will silently corrupt state for any client
that understands more of the protocol than it does.

## Decision

**Wire values are plain Python `dict`/`list`/scalars. Static typing is provided
by `TypedDict` views over them. There is no parse-into-objects step.**

- `decode` is `json.loads`; `encode` is `json.dumps`. Unknown keys, unknown
  variants, unknown enum values and large integers all survive by construction.
- `TypedDict` + `Literal` discriminators give `mypy --strict` coverage with zero
  runtime representation change.
- Validation is **targeted and explicit**, not a by-product of parsing: a
  `TypeSpec` per protocol type checks required keys and value shapes, and is
  applied where the protocol says a host MUST validate (inbound command params,
  client-dispatched actions). It never rewrites or drops.
- Union access goes through guard functions (`is_markdown_part`, …) that return
  `False` for unknown discriminators rather than raising.

## Consequence for the round-trip corpus

Upstream's corpus explicitly models two legitimate behaviours for its two
Group B fixtures (`KNOWN-FIDELITY-GAPS.md`):

- runtime-decoder clients (Go, Rust, Swift, Kotlin) **drop** unknown keys and
  assert `acceptableOutputs[0]`;
- TypeScript has no runtime decoder, **preserves** them, and asserts
  `preservedOutput`.

We preserve, so **we assert `preservedOutput` for Group B** and
`acceptableOutputs[0]` for Group A. This is the documented structural exception,
not a divergence — the corpus ships both forms precisely so either
implementation strategy can be asserted, never skipped.

## Making the corpus a real gate, not a tautology

If encode/decode were identity and nothing else ran, the round-trip test would
prove nothing. So each fixture is also run through the `TypeSpec` for its
declared `type`, asserting that every required field is present and
well-shaped and that discriminated unions resolve (or fall back to `Unknown`).

That is what catches a misspelled key, a wrongly-required optional, or a
mis-modelled nesting — exactly the gap upstream records for TypeScript, which
"does not verify generated-type correctness" because its types are erased.

## Confirmed in practice

The decision to keep unknown data verbatim paid off immediately: VS Code's
`createSession` carries an `activeClient.tools` array of its own contributed
tool definitions, and its session state carries fields v0.1 does not model. A
host that parsed into closed models would have dropped them on the floor while
remaining authoritative for that state.

## Alternatives rejected

- **Pydantic v2 models.** Idiomatic and the ecosystem norm, but `extra='allow'`
  plus preserving `null`-vs-absent plus open enums plus `Unknown` arms on ~30
  unions fights the library the whole way, and every model is a place state can
  be silently dropped. Adds a hard dependency to a core that otherwise needs
  none.
- **Generating models from the published JSON Schemas.** Ruled out on evidence:
  the schemas are a derived artifact, `actions.schema.json` has shipped a
  malformed `{"$ref": "#/$defs/"}` since `spec/v0.5.0`, and `SessionStatus` is a
  bitset published as a closed enum that rejects upstream's own fixtures
  (sibling `docs/research.md` §5).
- **Dataclasses with an `extra: dict` escape hatch.** Preserves data, but
  round-tripping key order and null-vs-absent through two representations is
  more machinery than dicts, for no gain a host needs.

## Cost, stated plainly

No runtime type errors on field access — a typo in host code surfaces as a
`KeyError` at runtime rather than a validation error at the boundary.
`mypy --strict` over `TypedDict` is the mitigation, and it is why the strict
gate is non-negotiable in CI.
