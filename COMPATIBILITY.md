# Compatibility

One row per release tag. A tag listed here may not exist yet: tags are pushed by the maintainer (each package's `RELEASING.md`). `ahp-protocol` is the range a package accepts, directly or through the sibling named. "AHP versions offered" is `ahp_protocol`'s `DEFAULT_SUPPORTED_VERSIONS`. "Spec commit" is the upstream commit the protocol package is pinned to (`packages/ahp-protocol/UPSTREAM.md`). "Tested with" lists the sibling versions at the commit that holds the tag. `packages/ahp-host-flow/tests/docs/test_compatmd.py` rebuilds the current rows from each `pyproject.toml`, `versions.py` and the pin, and fails on drift; older rows are history and stay as written.

| Tag | Package | Version | ahp-protocol | AHP versions offered | Spec commit | Tested with |
|---|---|---|---|---|---|---|
| `ahp-protocol/v0.1.0` | ahp-protocol | 0.1.0 | — | 1.0.0, 0.9.0, 0.8.0, 0.7.0, 0.6.0 | `c5628cd4` | — |
| `ahp-host/v0.1.0` | ahp-host | 0.1.0 | `~=0.1.0` | 1.0.0, 0.9.0, 0.8.0, 0.7.0, 0.6.0 | `c5628cd4` | ahp-protocol 0.1.0 |
| `ahp-client/v0.1.0` | ahp-client | 0.1.0 | `~=0.1.0` | 1.0.0, 0.9.0, 0.8.0, 0.7.0, 0.6.0 | `c5628cd4` | ahp-protocol 0.1.0 |
| `ahp-acp/v0.1.0` | ahp-acp | 0.1.0 | — | — | — | — |
| `ahp-gateway/v0.1.0` | ahp-gateway | 0.1.0 | `~=0.1.0` | 1.0.0, 0.9.0, 0.8.0, 0.7.0, 0.6.0 | `c5628cd4` | ahp-protocol 0.1.0, ahp-host 0.1.0, ahp-client 0.1.0 |
| `ahp-host-claude/v0.1.0` | ahp-host-claude | 0.1.0 | `~=0.1.0` (via ahp-host) | 1.0.0, 0.9.0, 0.8.0, 0.7.0, 0.6.0 | `c5628cd4` | ahp-host 0.1.0 |
| `ahp-host-acp/v0.1.0` | ahp-host-acp | 0.1.0 | `~=0.1.0` (via ahp-host) | 1.0.0, 0.9.0, 0.8.0, 0.7.0, 0.6.0 | `c5628cd4` | ahp-host 0.1.0, ahp-acp 0.1.0 |
| `ahp-host-flow/v0.1.0` | ahp-host-flow | 0.1.0 | `~=0.1.0` (via ahp-host) | 1.0.0, 0.9.0, 0.8.0, 0.7.0, 0.6.0 | `c5628cd4` | ahp-host 0.1.0, ahp-client 0.1.0, ahp-gateway 0.1.0, ahp-host-claude 0.1.0, ahp-host-acp 0.1.0 |
