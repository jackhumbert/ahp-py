# Demo plugin

Real files behind the `--customizations` demo tree.

They exist because a customization is only as real as the resource behind it:
a client expands a host-published plugin by **reading its `uri` back through
the `resource*` family**, so a plugin pointing at a path that does not exist
renders as an empty container. That is not a hypothetical — it is what this
host did until these files were added, and the wire log showed 215
`NotFound` answers to a client patiently asking for them.

The layout is the ordinary one: a child's type comes from its **directory**
(`agents/`, `skills/`, `prompts/`, `instructions/`, `hooks/`), not from a
filename suffix.
