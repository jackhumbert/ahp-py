"""The upstream wire round-trip corpus.

Per ADR 0001 our decode/encode is `json.loads`/`json.dumps`, so asserting the
round trip alone would be a tautology. The gate that makes this corpus real is
`test_input_satisfies_its_declared_spec`: every fixture is validated against the
`Spec` for its declared `type`, which is what catches a misspelled key, a wrongly
required optional, or a mis-modelled nesting in our hand-written declarations.

Upstream records that TypeScript "does not verify generated-type correctness"
because its types are erased at runtime. Ours are not erased -- this is where we
do better than the reference implementation.
"""

from __future__ import annotations

import json

import pytest

from agent_host_protocol.conformance.corpus import RoundTripFixture, round_trip_fixtures
from agent_host_protocol.types import SPECS, wire_equal

FIXTURES = list(round_trip_fixtures())


def _ids(fixtures: list[RoundTripFixture]) -> list[str]:
    return [f.id for f in fixtures]


def test_corpus_is_present() -> None:
    assert len(FIXTURES) == 44, "vendored round-trip corpus changed; re-check the pin"


@pytest.mark.parametrize("fixture", FIXTURES, ids=_ids(FIXTURES))
def test_round_trip_produces_the_expected_form(fixture: RoundTripFixture) -> None:
    """decode -> encode must reproduce the form our strategy commits to.

    Group A: every implementation agrees on `acceptableOutputs[0]`.
    Group B: we preserve unknown keys, so we assert `preservedOutput`.
    """
    decoded = json.loads(json.dumps(fixture.input))
    reencoded = json.loads(json.dumps(decoded))
    assert wire_equal(reencoded, fixture.expected), (
        f"{fixture.id} ({fixture.type_name}, group {fixture.group}): "
        f"got {json.dumps(reencoded, sort_keys=True)} "
        f"expected {json.dumps(fixture.expected, sort_keys=True)}"
    )


@pytest.mark.parametrize("fixture", FIXTURES, ids=_ids(FIXTURES))
def test_input_satisfies_its_declared_spec(fixture: RoundTripFixture) -> None:
    """Our hand-written type declaration must accept every real payload."""
    spec = SPECS.get(fixture.type_name)
    if spec is None:
        pytest.skip(f"no spec yet for {fixture.type_name}")
    problems = spec.validate(fixture.input)
    assert not problems, f"{fixture.id} ({fixture.type_name}): {problems}"


@pytest.mark.parametrize("fixture", FIXTURES, ids=_ids(FIXTURES))
def test_expected_output_also_satisfies_the_spec(fixture: RoundTripFixture) -> None:
    """The canonical output form must validate too, not just the input."""
    spec = SPECS.get(fixture.type_name)
    if spec is None:
        pytest.skip(f"no spec yet for {fixture.type_name}")
    problems = spec.validate(fixture.expected)
    assert not problems, f"{fixture.id} ({fixture.type_name}) expected form: {problems}"


def test_every_declared_type_has_a_spec() -> None:
    """No fixture type may be silently skipped -- a skip looks like a pass."""
    declared = {f.type_name for f in FIXTURES}
    missing = sorted(declared - set(SPECS))
    assert not missing, f"round-trip corpus declares types with no spec: {missing}"


def test_group_b_fixtures_carry_both_forms() -> None:
    """Group B is the documented preserve-vs-drop fork; both forms must be present."""
    group_b = [f for f in FIXTURES if f.group == "B"]
    assert group_b, "expected at least one Group B fixture"
    for fixture in group_b:
        assert fixture.preserved_output is not None, (
            f"{fixture.id} is Group B but has no preservedOutput"
        )
        # Our strategy preserves, so the two forms must genuinely differ --
        # otherwise the fixture is not exercising the fork at all.
        assert not wire_equal(fixture.preserved_output, fixture.acceptable_outputs[0])
