"""The README's claims, checked against the code that would falsify them.

Prose contradicting itself is not catchable. Prose contradicting the reducer
table, the version constants or the corpus counts is -- and every check here is
*derived* from the code rather than kept in a list, because a list never
contains the thing someone just added.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from agent_host_protocol import (
    DEFAULT_SUPPORTED_VERSIONS,
    REDUCERS,
    UPSTREAM_PROTOCOL_VERSION,
)
from agent_host_protocol.conformance.corpus import reducer_fixtures, round_trip_fixtures

ROOT = Path(__file__).resolve().parents[2]
README = (ROOT / "README.md").read_text(encoding="utf-8")


def _python_blocks(markdown: str) -> list[str]:
    return re.findall(r"```python\n(.*?)```", markdown, flags=re.DOTALL)


def test_readme_has_executable_examples() -> None:
    """Guard the guard: a regex that silently matches nothing proves nothing."""
    assert _python_blocks(README), "no ```python blocks found -- did the fence style change?"


@pytest.mark.parametrize("index", range(len(_python_blocks(README))))
def test_readme_examples_execute(index: int) -> None:
    """Every example runs, with its own assertions, in a shared namespace."""
    block = _python_blocks(README)[index]
    exec(compile(block, f"README.md[python block {index}]", "exec"), {})


def test_readme_names_every_reducer_it_claims() -> None:
    """'all seven' has to keep meaning seven."""
    assert len(REDUCERS) == 7
    assert "seven" in README


def test_readme_fixture_counts_match_the_vendored_corpus() -> None:
    reducers = sum(1 for _ in reducer_fixtures())
    round_trips = sum(1 for _ in round_trip_fixtures())
    assert reducers == 247
    assert round_trips == 39
    assert f"**{reducers}**" in README
    assert f"**{round_trips}**" in README


def test_readme_quotes_the_real_default_versions() -> None:
    """The README tells a reader which list to offer; it must be the real one."""
    quoted = ", ".join(f"`{v}`" for v in DEFAULT_SUPPORTED_VERSIONS)
    assert quoted in README, f"README should say {quoted}"


def test_upstream_md_pins_what_the_tables_were_generated_from() -> None:
    upstream = (ROOT / "UPSTREAM.md").read_text(encoding="utf-8")
    assert UPSTREAM_PROTOCOL_VERSION in upstream


def test_readme_does_not_advertise_a_uri_reducer_lookup() -> None:
    """`reducer_name_for` was deleted on purpose (scheme routing binds nothing).

    If it ever comes back, the README section explaining why it must not exist
    is the thing most likely to be left behind.
    """
    import agent_host_protocol.channels as channels

    assert not hasattr(channels, "reducer_name_for")
    assert "reducer_name_for" in README, "the README should still explain the absence"
