"""`UPSTREAM.md`'s vendored-file claims, checked against `vendor/upstream/`.

`AGENTS.md` makes "`UPSTREAM.md` still describes what is actually vendored" a
commit gate because the table has drifted in both directions: it once listed
`registry-snapshot.json`, which the vendoring script has never fetched, and it
later listed five `ts/` files while the directory held thirteen. A hand-kept
table never contains the thing someone just vendored, so derive the truth from
the directory and fail when the prose disagrees.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
UPSTREAM = (ROOT / "UPSTREAM.md").read_text(encoding="utf-8")
VENDOR = ROOT / "vendor" / "upstream"


def _vendored_section() -> str:
    match = re.search(r"## What is vendored.*?\n##", UPSTREAM, flags=re.DOTALL)
    assert match is not None, "the 'What is vendored' section is missing from UPSTREAM.md"
    return match.group(0)


def _expand_braces(token: str) -> list[str]:
    """`commands-{root,session}.ts` → both filenames; a plain name passes through."""
    match = re.fullmatch(r"(.*)\{([^}]*)\}(.*)", token)
    if match is None:
        return [token]
    head, alternatives, tail = match.groups()
    return [f"{head}{alt}{tail}" for alt in alternatives.split(",")]


def test_the_ts_table_rows_name_exactly_the_vendored_ts_files() -> None:
    """The table's `ts/` rows must equal `ls vendor/upstream/ts` — no more, no
    less. Listing a file that is not vendored sends a reader hunting for codegen
    input that does not exist; omitting one hides an input the peers' parity
    and scoping gates actually read."""
    documented: set[str] = set()
    for token in re.findall(r"`([^`]+\.ts)`", _vendored_section()):
        documented.update(_expand_braces(token))
    actual = {path.name for path in (VENDOR / "ts").glob("*.ts")}
    assert actual, "vendor/upstream/ts is empty -- did the layout move?"
    assert documented == actual, (
        f"undocumented: {sorted(actual - documented)}; phantom: {sorted(documented - actual)}"
    )


def test_the_tables_counts_match_the_vendored_trees() -> None:
    """The parenthesised counts in the vendored table, recounted from disk."""
    section = _vendored_section()
    # The table names upstream's `types/test-cases/**` paths; the vendoring
    # script flattens them to `vendor/upstream/test-cases/` locally.
    reducers = sum(1 for _ in (VENDOR / "test-cases" / "reducers").glob("**/*.json"))
    round_trips = sum(1 for _ in (VENDOR / "test-cases" / "round-trips").glob("**/*.json"))
    schemas = sum(1 for _ in (VENDOR / "schema").glob("*.schema.json"))
    assert f"({reducers} fixtures)" in section
    assert f"({round_trips} fixtures)" in section
    assert f"({schemas} files)" in section


def test_the_table_does_not_resurrect_the_registry_snapshot() -> None:
    """`registry-snapshot.json` was listed as vendored for months while the
    script never fetched it — the exact failure this suite exists to stop."""
    assert "registry-snapshot" not in _vendored_section()
