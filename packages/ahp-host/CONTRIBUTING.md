# Contributing

Thanks for your interest. Two things make this project unusual, and both shape
how contributions work.

## 1. This project targets an external specification

The [Agent Host Protocol][ahp] is owned and designed by Microsoft. We implement
it; we do not design it.

**Protocol changes come from upstream, not from contributors' preferences.** A
PR that changes a wire type, an action shape, a state field or an error code to
something other than what the pinned upstream revision specifies will be
declined, however sensible the change. If the spec is wrong or underspecified:

1. Open an issue here so the question is recorded.
2. Raise it upstream at [microsoft/agent-host-protocol][repo].
3. We record the open question in `docs/research.md` §11 and implement the
   spec's current behaviour in the meantime.

A private divergence would defeat the entire purpose of the project — other
implementations must be able to trust that we behave exactly as the spec says.

Which revision we target, and how it is bumped, is in [UPSTREAM.md](UPSTREAM.md).

## 2. Every protocol claim needs a test

"Conformant" without a conformance test is a lie. Reducer changes must keep
upstream's 247-fixture corpus green, and behaviour the corpus does not pin needs
a hand-written test — see the hazards list in `docs/research.md` §2f, which
exists because JavaScript and Python disagree about things like whether `[]` is
truthy.

## Getting started

```bash
uv sync --all-extras     # or: pip install -e '.[dev]'
pytest
ruff check . && mypy --strict src
```

The whole suite runs offline with no model, no credentials and no network. The
one exception is the interop job, which drives the real Microsoft TypeScript
client and needs Node.

Read [AGENTS.md](AGENTS.md) before your first change — it lists the invariants
that must not break, and most of them fail silently in *clients* rather than
loudly in our tests.

## Conventions

- [Conventional commits](https://www.conventionalcommits.org/).
- Small, reviewable PRs.
- `docs/research.md` and `docs/plan.md` are living documents. If your change
  makes either inaccurate, update it in the same PR.
- Adapters for specific agent runtimes go in their own distribution, never in
  the core — the core stays vendor-neutral and fully testable with no adapter
  installed.

## Code of conduct

Be decent. Assume good faith. Harassment or personal attacks are not welcome and
will be moderated.

## Licence

By contributing you agree that your contributions are licensed under the MIT
licence, matching [LICENSE](LICENSE) and upstream.

[ahp]: https://microsoft.github.io/agent-host-protocol/
[repo]: https://github.com/microsoft/agent-host-protocol
