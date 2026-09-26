"""Protocol version negotiation.

Correctness here is entirely the host's: the reference client does not verify
the version it is given and will proceed on one it never offered
(docs/experiments.md E3). So these are the tests nothing else will catch.
"""

from __future__ import annotations

from agent_host_protocol.versions import is_compatible, negotiate, parse_version


class TestParse:
    def test_wellformed(self) -> None:
        assert parse_version("0.7.0") == (0, 7, 0)

    def test_rejects_prerelease_and_build_metadata(self) -> None:
        assert parse_version("0.7.0-beta") is None
        assert parse_version("0.7.0+build1") is None

    def test_rejects_junk(self) -> None:
        assert parse_version("") is None
        assert parse_version("0.7") is None
        assert parse_version("v0.7.0") is None


class TestCompatibility:
    """Pre-1.0, every MINOR bump is breaking."""

    def test_same_minor_is_compatible(self) -> None:
        assert is_compatible("0.7.0", "0.7.0")

    def test_different_minor_is_not(self) -> None:
        assert not is_compatible("0.6.0", "0.7.0")
        assert not is_compatible("0.7.0", "0.6.0")

    def test_different_major_is_not(self) -> None:
        assert not is_compatible("1.7.0", "0.7.0")

    def test_offered_may_not_exceed_ours(self) -> None:
        """A host that knows 0.7.0 cannot pretend to speak 0.7.5."""
        assert not is_compatible("0.7.5", "0.7.0")
        assert is_compatible("0.7.0", "0.7.5")

    def test_junk_is_never_compatible(self) -> None:
        assert not is_compatible("nonsense", "0.7.0")


class TestNegotiate:
    def test_vscode_gets_its_preferred_version(self) -> None:
        """VS Code offers the full list, most-preferred first."""
        assert negotiate(["0.7.0", "0.6.0", "0.5.2", "0.5.1"]) == "0.7.0"

    def test_npm_client_negotiates_down_to_0_6_0(self) -> None:
        """The installable @microsoft/agent-host-protocol@0.6.0 client."""
        assert negotiate(["0.6.0", "0.5.2", "0.5.1"]) == "0.6.0"

    def test_multi_host_client_offers_a_single_version(self) -> None:
        """MultiHostClient sends [PROTOCOL_VERSION] only, never the full list."""
        assert negotiate(["0.6.0"]) == "0.6.0"

    def test_ahpx_is_refused(self) -> None:
        """ahpx offers a single 0.5.x, which we deliberately do not speak."""
        assert negotiate(["0.5.0"]) is None
        assert negotiate(["0.5.2"]) is None

    def test_highest_wins_regardless_of_client_preference_order(self) -> None:
        assert negotiate(["0.6.0", "0.7.0"]) == "0.7.0"

    def test_no_overlap_refuses(self) -> None:
        assert negotiate(["0.10.0"]) is None
        assert negotiate([]) is None

    def test_unparseable_entries_are_skipped_not_fatal(self) -> None:
        assert negotiate(["garbage", "0.7.0"]) == "0.7.0"

    def test_a_single_minor_host_rejects_other_minors(self) -> None:
        """The reference host's behaviour, which we generalise past."""
        assert negotiate(["0.6.0"], supported=["0.7.0"]) is None
        assert negotiate(["0.6.0"], supported=["0.7.0", "0.6.0"]) == "0.6.0"
