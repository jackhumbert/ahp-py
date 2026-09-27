# Security

## Reporting a vulnerability

Report privately through [GitHub's advisory
form](https://github.com/jackhumbert/ahp-py/security/advisories/new), for any
package in this repository. Please do not open a public issue for a
vulnerability.

Include which package, what you did, what happened, and what you expected. A
wire log or a minimal script that reproduces it is worth more than a
description, but read the package's own policy on what its logs contain before
attaching one.

## What each package promises

Scope, threat model and what is logged are per package:

- [`ahp-host`](packages/ahp-host/SECURITY.md): the host, its filesystem jails,
  authentication and policies. This covers the providers as well, since they
  run inside it (`ahp-host-claude`, `ahp-host-acp`).
- [`ahp-client`](packages/ahp-client/SECURITY.md): the client.
- [`ahp-protocol`](packages/ahp-protocol/SECURITY.md): the wire types and
  reducers, which parse whatever a peer sends.

`ahp-gateway` is pre-alpha and has no policy of its own yet; report against it
the same way.
