# Changelog

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
versioning is [SemVer](https://semver.org/), independent of the protocol's.
Every release states the protocol versions it speaks.

## [Unreleased]

Under construction. `docs/plan.md` is the design and its §7 is the build order.

### Added

- **Scaffold.** Packaging (hatchling, single-sourced `__version__`), the
  layer skeleton (`core` / `registry` / `ws` / `relay`), `mypy --strict`,
  ruff, two import-linter contracts, and the family's CI shape (sibling
  checkouts supplying the GitHub-distributed dependencies).
- **The design**, in `docs/plan.md`: two planes (AHP data plane unmodified,
  control plane beside it), the reachability split (dial-in vs. dial-out
  relay), the trust model (per-user node accounts; identity gates the session,
  the OS gates the filesystem), and the build order.
