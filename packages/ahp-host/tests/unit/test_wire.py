"""Wire semantics, and the JavaScript/Python divergences the corpora do NOT pin.

Every test here exists because a natural Python translation of the upstream
TypeScript would be silently wrong. See docs/research.md §2f.
"""

from __future__ import annotations

from agent_host_server.types import coalesce, drop_none, reduced_equal, wire_equal
from agent_host_server.types.protocol import SessionStatus, session_status_flags
from agent_host_server.types.wire import discriminator, json_equal


class TestCoalesce:
    """`??` falls through only on null. Python's `or` also falls through on
    falsy values, which would silently change reducer behaviour."""

    def test_none_falls_through(self) -> None:
        assert coalesce(None, "fallback") == "fallback"

    def test_empty_string_does_not_fall_through(self) -> None:
        assert coalesce("", "fallback") == ""

    def test_zero_does_not_fall_through(self) -> None:
        assert coalesce(0, 7) == 0

    def test_false_does_not_fall_through(self) -> None:
        assert coalesce(False, True) is False

    def test_empty_list_does_not_fall_through(self) -> None:
        """The empty-collection trap: `[]` is truthy in JS, falsy in Python."""
        assert coalesce([], ["fallback"]) == []


class TestJsonEqual:
    """Plain `==` produces false passes in two ways the corpora would hit."""

    def test_true_is_not_one(self) -> None:
        assert not json_equal({"reviewed": True}, {"reviewed": 1})
        assert {"reviewed": True} == {"reviewed": 1}  # the trap being avoided

    def test_false_is_not_zero(self) -> None:
        assert not json_equal({"x": False}, {"x": 0})

    def test_int_and_float_compare_by_value(self) -> None:
        """The corpora carry real floats: safety 0.0, 0.25, 1.5."""
        assert json_equal({"safety": 0}, {"safety": 0.0})

    def test_nested_and_ordering_insensitive(self) -> None:
        assert json_equal({"a": 1, "b": {"c": [1, 2]}}, {"b": {"c": [1, 2]}, "a": 1})

    def test_length_mismatch(self) -> None:
        assert not json_equal([1, 2], [1, 2, 3])

    def test_large_integers_survive(self) -> None:
        """serverSeq routinely exceeds int32 (fixture 016: 2148131814)."""
        assert json_equal({"serverSeq": 2148131814}, {"serverSeq": 2148131814})


class TestNullSemantics:
    """The two corpora have OPPOSITE rules over the same payloads."""

    def test_reducer_corpus_treats_null_as_absent(self) -> None:
        assert reduced_equal({"usage": None, "id": "t1"}, {"id": "t1"})

    def test_wire_corpus_keeps_null_and_absent_distinct(self) -> None:
        """Upstream: "an absent `origin` re-encoding as `"origin": null` is a
        failure, not a pass"."""
        assert not wire_equal({"origin": None, "serverSeq": 1}, {"serverSeq": 1})

    def test_drop_none_is_recursive_and_spares_lists(self) -> None:
        assert drop_none({"a": {"b": None, "c": 1}, "d": [{"e": None}]}) == {
            "a": {"c": 1},
            "d": [{}],
        }

    def test_drop_none_keeps_falsy_values(self) -> None:
        assert drop_none({"a": 0, "b": "", "c": False, "d": []}) == {
            "a": 0,
            "b": "",
            "c": False,
            "d": [],
        }


class TestDiscriminator:
    """Reading a union tag must never raise -- unknown variants are legal."""

    def test_known(self) -> None:
        assert discriminator({"type": "chat/delta"}) == "chat/delta"

    def test_missing(self) -> None:
        assert discriminator({"foo": 1}) is None

    def test_non_string(self) -> None:
        assert discriminator({"type": 7}) is None

    def test_non_mapping(self) -> None:
        assert discriminator("not an object") is None
        assert discriminator(None) is None

    def test_alternate_key(self) -> None:
        assert discriminator({"kind": "sideChat"}, "kind") == "sideChat"


class TestSessionStatus:
    """A bitset, not an enum -- and JS coerces bitwise operands to SIGNED int32
    while every runtime client uses unsigned. We mask to u32, agreeing with four
    of the five clients."""

    def test_known_flags(self) -> None:
        assert SessionStatus.IN_PROGRESS == 1
        assert SessionStatus.INPUT_NEEDED == 64

    def test_unknown_high_bit_survives(self) -> None:
        """Fixture 005 carries 2147483720, which has bit 31 set."""
        assert session_status_flags(2147483720) == 2147483720

    def test_combination_outside_the_published_enum(self) -> None:
        """72 = 8 | 64. The published schema's enum does not contain it."""
        assert session_status_flags(72) == 72

    def test_negative_signed_value_normalises_to_unsigned(self) -> None:
        """What a JS `status & ~flag` would emit for a high-bit status."""
        assert session_status_flags(-2147483584) == 2147483712
