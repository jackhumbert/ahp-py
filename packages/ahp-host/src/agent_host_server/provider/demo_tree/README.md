# Demo plugin

Real files behind the `--customizations` demo tree.

They exist because **a customization is only as real as the resource behind
it**: a client expands a host-published plugin by reading its `uri` back
through the `resource*` family, so a plugin pointing at a path that does not
exist renders as an empty container. That is not hypothetical — it is what this
host did until these files were added, and the wire log showed a client
patiently asking 215 times and being told `NotFound` every time.

## The layout is not a guess

It is what VS Code's own tests read
(`chat/test/browser/actions/createPluginAction.test.ts`):

| Directory | Holds | Note |
|---|---|---|
| `agents/` | `*.md` | |
| `commands/` | `*.md` | **Prompts live here** — not in `prompts/` |
| `rules/` | `*.instructions.md`, `*.mdc` | Instructions, not `instructions/` |
| `skills/<name>/` | `SKILL.md` + helpers | A skill is a **directory**, not a file |
| `hooks/` | `*.json` | Manifests are **JSON**, and surface as a `directory` container of `contents: "hook"` — never as a plugin child with a markdown body. Scanned from the **primary working directory only** |

Two of those four were wrong when guessed from the spec alone, which is why the
wire log is the source of truth here and the prose is not.
