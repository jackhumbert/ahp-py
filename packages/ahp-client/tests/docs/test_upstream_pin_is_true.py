"""The two version claims the docs make about the dependency, asserted.

`AGENTS.md` says to pin `agent-host-protocol ~= 0.1.0` and assert
`UPSTREAM_PROTOCOL_VERSION` here, so a dependency bump that moves the spec
under us fails loudly; invariant 12 says to ship the offered-list subset test.
Both claims described tests that did not exist — this file is them.
"""

from __future__ import annotations

from agent_host_protocol import (
    DEFAULT_SUPPORTED_VERSIONS,
    UPSTREAM_PROTOCOL_VERSION,
    UPSTREAM_SUPPORTED_PROTOCOL_VERSIONS,
)

from agent_host_client.client.client import ClientConfig


def test_the_pin_is_the_spec_revision_this_client_was_written_against() -> None:
    """Every wire shape here was read out of `spec/v0.7.0` (plan §2.3). A
    dependency that vendors a different tag invalidates that evidence, and the
    place it must fail is a test rather than a field report."""
    assert UPSTREAM_PROTOCOL_VERSION == "0.7.0"


def test_the_default_offer_is_the_pins_list_not_upstreams() -> None:
    """Invariant 12: default the offered versions to the pin's
    `DEFAULT_SUPPORTED_VERSIONS`, never upstream's constant. The first is a
    claim about which tables are vendored; the second is a fact about upstream.
    Offering a version whose tables are not vendored means negotiating a
    protocol we cannot reduce."""
    assert ClientConfig().protocol_versions == DEFAULT_SUPPORTED_VERSIONS


def test_every_offered_version_is_covered_by_the_pin() -> None:
    """The subset test invariant 12 says to ship: whatever the default offer
    becomes, each entry must be one the pin's vendored tables cover — and the
    pin's own list must stay within what upstream declares, so a widened
    `DEFAULT_SUPPORTED_VERSIONS` is a vendoring decision, not a typo."""
    assert set(ClientConfig().protocol_versions) <= set(DEFAULT_SUPPORTED_VERSIONS)
    assert set(DEFAULT_SUPPORTED_VERSIONS) <= set(UPSTREAM_SUPPORTED_PROTOCOL_VERSIONS)
