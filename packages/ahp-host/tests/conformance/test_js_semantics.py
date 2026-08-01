"""The corpus's blind spot, closed.

`reduced_equal` normalises `null` away on both sides -- it has to, because the
upstream fixtures write an absent optional as JSON `null`. That makes the
247-fixture corpus **structurally incapable** of catching the single most
common porting defect in this project: confusing JavaScript's `undefined` with
its `null`.

It is not a hypothetical gap. An adversarial audit of the four reducers ported
in v0.2 found the confusion in all four *and* in `session.py`, which had shipped
with it, on sites the corpus exercises and passes.

So these cases go the other way round. Each was run through the **real pinned
TypeScript reducers** under `node --experimental-transform-types`, and the
`expected` state is that run's output after `JSON.stringify` -- the operation
that actually drops `undefined`-valued keys. An absent key here means the
reference genuinely produced `undefined`; an explicit `null` means it genuinely
produced `null`.

Comparison is **verbatim**. No `drop_none`, no normalisation: that is the point.

Regenerate with `scripts/regenerate_js_semantics.sh` after an upstream pin bump.
The test itself is offline and needs no Node.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from agent_host_server.reducers import REDUCERS
from agent_host_server.reducers.clock import frozen_clock

_FIXTURE = Path(__file__).parent / "fixtures" / "js-semantics.json"
_CASES: list[dict[str, Any]] = json.loads(_FIXTURE.read_text(encoding="utf-8"))["cases"]


def test_the_oracle_covers_every_reducer_with_a_lookup_or_an_assignment() -> None:
    """A reducer that indexes by a client-chosen id, or spreads an optional
    field, needs coverage here -- those are the two shapes that go wrong."""
    covered = {case["reducer"] for case in _CASES}
    assert {"terminal", "changeset", "annotations", "session"} <= covered


@pytest.mark.parametrize("case", _CASES, ids=[c["name"] for c in _CASES])
def test_matches_the_reference_verbatim(case: dict[str, Any]) -> None:
    with frozen_clock():
        state = case["initial"]
        for action in case["actions"]:
            state = REDUCERS[case["reducer"]](state, action)

    # Round-trip through JSON so the comparison is of documents that could
    # actually go on the wire, exactly as the oracle's JSON.stringify did.
    got = json.loads(json.dumps(state))
    assert got == case["expected"], (
        f"{case['name']}\n  got      {json.dumps(got)}\n  expected {json.dumps(case['expected'])}"
    )
