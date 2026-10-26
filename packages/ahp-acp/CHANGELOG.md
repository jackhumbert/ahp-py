# Changelog

## [Unreleased]

### Fixed — review fixes

- stderr keeps draining past a line longer than the stream limit; one long line used to stop
  the drain and stall the agent.
- `spawn(user=...)` runs the child in the user's primary group unless `group` names another,
  and sets `HOME`, `USER` and `LOGNAME` for that user.
- `spawn(user=...)` no longer hands this process's environment to the other user by default:
  the child gets `PATH`, the locale (`LANG`, `LANGUAGE`, `LC_*`, `TZ`), its own identity and
  `env`. `inherit_env` now defaults to `None` (inherit unless `user` is set); pass
  `inherit_env=True` to pass everything on.
- `parse_update` never raises on a `usage_update`'s counts.

### Added — review fixes

- `kill_tree` and `signal_group` (process-group helpers), and
  `AcpClient.settle_permissions`.

## [0.1.0] - unreleased

### Added — release preparation (M18)

- `scripts/smoke_wheel.py` checks an installed wheel from outside the checkout (CI's wheels job and release.yml run it).
- `RELEASING.md`: the release procedure and the release order.

### Added

- First release: `AcpConnection` (JSON-RPC over an agent's stdio, with exact
  environments, process groups, `user`/`group`/`umask`/`extra_groups`, oversize-line policy and
  a stderr callback), `AcpClient` (initialize, session methods gated by the
  agent's capabilities, permission and elicitation routing, `cancel` settling
  pending permissions), and `parse_update` (typed `session/update`, unknown kinds
  passed through). Checked against the ACP `schema-v1.26.0` schema in CI.
