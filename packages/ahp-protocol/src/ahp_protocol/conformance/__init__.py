"""The vendored upstream corpora, and the gates built on them.

`corpus` loads the fixtures and has no dependencies. `schemas` validates a wire
value against the vendored JSON Schemas and needs `jsonschema`, which this
package does NOT require -- so it is imported explicitly by a caller that wants
it, never pulled in by touching this package.
"""

from __future__ import annotations

from ahp_protocol.conformance.corpus import (
    CORPUS_ROOT,
    NegotiationCase,
    ReducerFixture,
    RoundTripFixture,
    pin,
    reducer_fixtures,
    round_trip_fixtures,
    version_negotiation_cases,
)

__all__ = [
    "CORPUS_ROOT",
    "NegotiationCase",
    "ReducerFixture",
    "RoundTripFixture",
    "pin",
    "reducer_fixtures",
    "round_trip_fixtures",
    "version_negotiation_cases",
]
