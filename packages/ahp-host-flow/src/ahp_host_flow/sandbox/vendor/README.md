# Vendored acorn

`acorn.mjs` is acorn, the JavaScript parser the sandbox uses to read
`export const meta` without running a script (docs/plan.md §4.1). `LICENSE` is
its licence.

Both files come from `https://unpkg.com/acorn@8.19.0/dist/acorn.mjs` and
`https://unpkg.com/acorn@8.19.0/LICENSE`. acorn is MIT-licensed (see LICENSE).
The files are vendored byte-for-byte, so there is no npm install at run time.

To update, run the same two curl commands against `acorn@8`, then update the
version line below.

Version: 8.19.0
