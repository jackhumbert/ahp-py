# Demo changes

`--changes` makes the demo agent perform real edits, so the Changes view has
something truthful to show.

## A changeset is a RECORD, not a proposal

That is the thing this directory exists to get right. The guide's own examples
are "uncommitted working-tree edits, the diff between two turns, the cumulative
changes for the whole session, the staged index" — every one a view of changes
that have **already happened**. `FileEdit.before` is even documented as absent
"for in-place file edits", which only makes sense if `after` is the file on
disk.

So a client opens `after.uri` and expects to find a file there. Publishing a
changeset for a file that does not exist gets you *"The editor could not be
opened because the file was not found."*

## What actually happens

- `baseline/` is committed. It is the "before" state, and the demo never
  modifies it.
- `scratch/` is gitignored. On every turn the demo resets it from `baseline/`,
  then really does the work: edits two files, deletes one, creates one.
- The changeset reports what it did, with `before` read from the baseline and
  `after` being the file now on disk.

Nothing outside `scratch/` is ever written.
