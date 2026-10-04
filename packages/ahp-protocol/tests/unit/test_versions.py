"""Protocol version negotiation.

Correctness here is entirely the host's: the reference client does not verify
the version it is given and will proceed on one it never offered
(docs/experiments.md E3). So these are the tests nothing else will catch.
Upstream's own corpus is ``tests/conformance/test_version_negotiation.py``;
these cover what it does not -- our wider default set and the edges of the
caret rule.
"""

from __future__ import annotations

import pytest

from ahp_protocol.types import UPSTREAM_SUPPORTED_PROTOCOL_VERSIONS
from ahp_protocol.versions import (
    DEFAULT_SUPPORTED_VERSIONS,
    InvalidProtocolVersionError,
    is_compatible,
    negotiate,
    parse_version,
)


class TestParse:
    def test_wellformed(self) -> None:
        assert parse_version("0.7.0") == (0, 7, 0)
        assert parse_version("1.10.0") == (1, 10, 0)

    def test_rejects_prerelease_and_build_metadata(self) -> None:
        assert parse_version("0.7.0-beta") is None
        assert parse_version("0.7.0+build1") is None

    def test_rejects_junk(self) -> None:
        assert parse_version("") is None
        assert parse_version("0.7") is None
        assert parse_version("v0.7.0") is None

    def test_rejects_leading_zeros(self) -> None:
        assert parse_version("01.0.0") is None
        assert parse_version("1.00.0") is None

    def test_rejects_a_trailing_newline(self) -> None:
        """`re.match` with `$` accepts this; the old parser did."""
        assert parse_version("1.0.0\n") is None


class TestCompatibility:
    """Caret ranges: ``^0.9.0`` is ``>=0.9.0 <0.10.0``, ``^1.0.0`` is ``<2.0.0``."""

    def test_same_version_is_compatible(self) -> None:
        assert is_compatible("0.7.0", "0.7.0")

    def test_pre_1_0_minor_is_breaking(self) -> None:
        assert not is_compatible("0.6.0", "0.7.0")
        assert not is_compatible("0.7.0", "0.6.0")

    def test_different_major_is_not(self) -> None:
        assert not is_compatible("1.7.0", "0.7.0")
        assert not is_compatible("2.0.0", "1.0.0")

    def test_offered_may_exceed_the_baseline(self) -> None:
        """Reversed by spec 1.0.0: MINOR and PATCH above a baseline are in range."""
        assert is_compatible("0.9.10", "0.9.0")
        assert is_compatible("1.10.0", "1.0.0")

    def test_offered_may_not_fall_below_the_baseline(self) -> None:
        assert not is_compatible("0.7.0", "0.7.5")

    def test_zero_zero_pins_the_patch(self) -> None:
        assert is_compatible("0.0.3", "0.0.3")
        assert not is_compatible("0.0.4", "0.0.3")

    def test_junk_is_never_compatible(self) -> None:
        assert not is_compatible("nonsense", "0.7.0")


class TestDefaults:
    def test_defaults_cover_upstreams_baselines(self) -> None:
        """Wider than upstream is a choice; narrower would refuse its clients."""
        assert set(UPSTREAM_SUPPORTED_PROTOCOL_VERSIONS) <= set(DEFAULT_SUPPORTED_VERSIONS)

    def test_defaults_lead_with_the_newest(self) -> None:
        assert DEFAULT_SUPPORTED_VERSIONS[0] == "1.0.0"


class TestNegotiate:
    def test_npm_1_0_client_gets_1_0_0(self) -> None:
        """@microsoft/agent-host-protocol@1.0.0 offers its two baselines."""
        assert negotiate(["1.0.0", "0.9.0"]) == "1.0.0"

    def test_a_0_9_client_still_negotiates(self) -> None:
        assert negotiate(["0.9.0"]) == "0.9.0"

    def test_older_minors_are_kept_for_one_release(self) -> None:
        """Upstream dropped them in 1.0.0; our default set still speaks them."""
        assert negotiate(["0.7.0", "0.6.0", "0.5.2", "0.5.1"]) == "0.7.0"
        assert negotiate(["0.6.0"]) == "0.6.0"

    def test_ahpx_is_refused(self) -> None:
        """ahpx offers a single 0.5.x, which we deliberately do not speak."""
        assert negotiate(["0.5.0"]) is None
        assert negotiate(["0.5.2"]) is None

    def test_highest_wins_regardless_of_client_preference_order(self) -> None:
        assert negotiate(["0.6.0", "0.9.0", "1.0.0"]) == "1.0.0"

    def test_the_exact_offered_string_is_returned(self) -> None:
        assert negotiate(["1.3.7"]) == "1.3.7"

    def test_no_overlap_refuses(self) -> None:
        assert negotiate(["0.10.0"]) is None
        assert negotiate(["2.0.0"]) is None
        assert negotiate([]) is None

    def test_a_malformed_entry_is_an_error_not_a_skip(self) -> None:
        with pytest.raises(InvalidProtocolVersionError) as raised:
            negotiate(["garbage", "1.0.0"])
        assert raised.value.version == "garbage"
        assert isinstance(raised.value, ValueError)

    def test_a_single_minor_host_rejects_other_minors(self) -> None:
        assert negotiate(["0.6.0"], supported=["0.7.0"]) is None
        assert negotiate(["0.6.0"], supported=["0.7.0", "0.6.0"]) == "0.6.0"
