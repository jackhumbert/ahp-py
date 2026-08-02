---
name: AHS Instruction Foxtrot (globbed)
applyTo: '**/*.py'
---
A demo instruction scoped to Python files, so the demo shows both the
always-applied and the globbed shape.

It has its own file because a client renders one entry per file. Two
declarations pointing at one file render as one rule, and the second is
invisible with no error anywhere.
