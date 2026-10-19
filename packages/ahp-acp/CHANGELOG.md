# Changelog

## [Unreleased]

### Added

- First release: `AcpConnection` (JSON-RPC over an agent's stdio, with exact
  environments, process groups, `user`/`group`/`umask`, oversize-line policy and
  a stderr callback), `AcpClient` (initialize, session methods gated by the
  agent's capabilities, permission and elicitation routing, `cancel` settling
  pending permissions), and `parse_update` (typed `session/update`, unknown kinds
  passed through). Checked against the ACP `schema-v1.26.0` schema in CI.
