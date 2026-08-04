# Security

## Reporting

Report vulnerabilities privately via GitHub Security Advisories
("Security" → "Report a vulnerability" on this repository). Please do not open
a public issue for anything you believe is exploitable. You will get an
acknowledgement within a week; coordinated disclosure once a fix exists is the
default.

## What counts as a vulnerability here

This library sits between an application and a host it connects to over a
socket, so the trust boundaries are concrete:

- **The host is untrusted input.** Anything a malicious or broken peer can send
  that crashes the client process, hangs it, exhausts memory beyond the
  documented queue bounds, or corrupts unrelated sessions' state is in scope.
  (The malformed-frame budget and bounded close exist for exactly this class.)
- **Credential handling.** A connection token appearing in an exception,
  a log line, or a wire log is in scope — redaction is deliberately not
  optional (`wirelog/` has no opt-out; the transport redacts the token from
  `websockets`' own error text).
- **The reverse direction.** `serve/` exposes client files to the host:
  a path or symlink escape past a mount root, a write the read-only flag should
  have refused, or a resource served outside the granted scope is in scope.
- **Approval gating.** A tool call executing without the confirmation the
  policy requires, or an approval answered on the caller's behalf, is in scope.

Out of scope: vulnerabilities in a host implementation (report those to the
host project), and the protocol's own design (that goes upstream to the AHP
specification).

## Supported versions

Pre-1.0: only the latest release gets fixes.
