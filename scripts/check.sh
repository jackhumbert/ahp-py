#!/usr/bin/env bash
# The gate CI runs, runnable locally: lint, format, types, import contracts and
# tests for each package named (default: all of them).
#
#     scripts/check.sh                 # every package
#     scripts/check.sh ahp-host ahp-client
#
# Assumes `uv sync --all-packages --all-extras --all-groups` has been run.
set -euo pipefail

root="$(cd "$(dirname "$0")/.." && pwd)"
if [ "$#" -eq 0 ]; then
  set -- $(cd "$root/packages" && ls -d */ | tr -d /)
fi

for package in "$@"; do
  echo "::group::$package"
  cd "$root/packages/$package"
  uv run --no-sync ruff check .
  uv run --no-sync ruff format --check .
  uv run --no-sync mypy
  if grep -q '^\[tool\.importlinter\]' pyproject.toml; then
    uv run --no-sync lint-imports
  fi
  uv run --no-sync pytest
  echo "::endgroup::"
done
