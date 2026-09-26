"""The injectable clock's public surface.

`now_iso` is the function the chat reducer imports for all six `modifiedAt`
stamps -- the same six wall-clock sites the module docstring enumerates from
`types/channels-chat/reducer.ts` -- so it belongs in `__all__`: a star-import
or `__all__`-driven doc tooling that misses the primary entry point presents
`to_iso`/`now_ms` as the API and hides the one function reducers actually
stamp with.
"""

from __future__ import annotations

from agent_host_protocol.reducers import clock
from agent_host_protocol.reducers.clock import MOCK_NOW_MS, frozen_clock, now_iso, to_iso


def test_now_iso_is_public() -> None:
    assert "now_iso" in clock.__all__


def test_now_iso_stamps_what_the_fixture_corpus_asserts() -> None:
    """`Date.now() === 9999` stamps as the literal 28 fixtures carry."""
    with frozen_clock():
        assert now_iso() == to_iso(MOCK_NOW_MS) == "1970-01-01T00:00:09.999Z"
