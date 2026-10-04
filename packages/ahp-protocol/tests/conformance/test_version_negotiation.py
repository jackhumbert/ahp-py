"""Upstream's version-negotiation corpus (spec 1.0.0).

Run against upstream's own baselines, not ours: the corpus pins what a host
advertising exactly ``1.0.0`` and ``0.9.0`` selects. Our wider default set is
tested separately in ``tests/unit/test_versions.py``, and must agree with this
corpus everywhere the two sets overlap.
"""

from __future__ import annotations

import pytest

from ahp_protocol.conformance.corpus import NegotiationCase, version_negotiation_cases
from ahp_protocol.types import UPSTREAM_SUPPORTED_PROTOCOL_VERSIONS
from ahp_protocol.versions import InvalidProtocolVersionError, negotiate

CASES = list(version_negotiation_cases())


def test_corpus_is_present() -> None:
    assert len(CASES) == 22, "vendored negotiation corpus changed; re-check the pin"
    assert sum(case.invalid for case in CASES) == 11


@pytest.mark.parametrize("case", CASES, ids=[repr(c.offered) for c in CASES])
def test_negotiation_matches_upstream(case: NegotiationCase) -> None:
    if case.invalid:
        with pytest.raises(InvalidProtocolVersionError):
            negotiate(case.offered, UPSTREAM_SUPPORTED_PROTOCOL_VERSIONS)
    else:
        assert negotiate(case.offered, UPSTREAM_SUPPORTED_PROTOCOL_VERSIONS) == case.expected
