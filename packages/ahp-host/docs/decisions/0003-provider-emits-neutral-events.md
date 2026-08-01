# ADR 0003 — The provider sink takes neutral events, not AHP actions

**Status:** accepted · **Date:** 2026-08-01

## Context

The provider interface is the extension point third parties implement. The only
prior art, `@wyrd-company/ahp-provider-kit`, defines its sink as:

```ts
export interface AgentTurnSink {
  emit(action: StateAction): void;
  fail(error: Error): void;
}
```

— the provider constructs AHP protocol actions itself. We considered adopting
that shape unchanged, since it is the one design the ecosystem has.

## Decision

**The sink accepts neutral provider events describing what the agent did. The
host maps them to AHP actions.**

```python
sink.text_delta(text)
sink.reasoning_delta(text)
sink.tool_call_started(call_id, name, tool_input)
sink.tool_call_completed(call_id, result)
sink.turn_failed(error)
```

The host owns the mapping to `chat/*` actions, `serverSeq` assignment, reducer
application, replay-log append and fan-out.

## Rationale

**1. It stops every adapter from being a protocol implementation.** To emit one
sentence of text under the prior-art design, an adapter must know that
`chat/responsePart{kind:'markdown', id}` has to precede any
`chat/delta{partId}`; that `chat/toolCallStart` creates its own response part,
so emitting an extra `chat/responsePart` for a tool call duplicates it; and that
turn ids thread through all of it. That is protocol knowledge replicated into
every adapter, and the ordering rule is pinned by conformance fixture
`161-chat-turn-lifecycle-on-chat.json` — not by anything an adapter author would
naturally read.

**2. It is the difference between surviving a spec bump and not.** This is not
hypothetical. `ahp-provider-kit` emits `session/delta`, `session/responsePart`
and `session/turnComplete`. Spec 0.4.0 relocated all of that to the chat channel.
**Every adapter written against that kit is now wire-dead** — not because any
agent runtime changed, but because the protocol did. With neutral events, the
host's mapper absorbs the relocation and adapters do not notice. Given a spec
that ships breaking changes in MINOR bumps (five releases in eight weeks), this
is the difference between an adapter ecosystem that survives and one that
rots.

**3. Sequencing authority belongs to the host.** `serverSeq` is assigned in
exactly one critical section (ADR 0004 / `AGENTS.md`). A provider emitting
actions produces values that look authoritative but are not, and the host must
re-validate them anyway.

**4. It matches upstream's own description of the boundary.**
`docs/guide/ahp-and-acp.md` puts an "agent event mapper" *inside* the host:
the agent streams `session/update`, and "the agent event mapper converts
ACP-specific events into agent-agnostic AHP actions".

## Consequences

- A `MarkdownTurn` helper in the host guarantees the fixture-161 ordering, so no
  adapter can get it wrong.
- Adapters are testable without any AHP knowledge: assert the events they emit.
- **Cost:** an adapter cannot emit an action the mapper does not model. The event
  vocabulary becomes a thing we have to evolve deliberately.
- If a genuine escape hatch is needed later we can add a raw-action channel, but
  it stays closed by default — opening it by default is what made the prior art
  brittle.
