# Demo changes

Real files, so the `--changes` demo diffs against something that exists.

The host **never writes here.** A changeset is a *proposal*: `before` is what
is on disk, `after` is what the agent suggests, and both are carried as content
the host serves back through `resourceRead`. Nothing on disk moves unless an
embedder makes it move.

That is also why the demo's operations — the buttons on the Changes view — are
deliberately inert. They demonstrate the invoke round trip (idle -> running ->
idle, with the status reaching the client) and touch no files. An operation
named `discard-changes` that actually destroyed work would be a poor thing to
put behind a demo flag.
