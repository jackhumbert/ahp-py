"""`type_matches`: the admission check on a config value (`core/config.py`)."""

from __future__ import annotations

from ahp_host.core.config import RootConfig, type_matches

_TAGS = {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 2}


class TestArrayCardinality:
    """`minItems` / `maxItems` (1.0.0)."""

    def test_within_bounds(self) -> None:
        assert type_matches(_TAGS, ["a"])
        assert type_matches(_TAGS, ["a", "b"])

    def test_too_few(self) -> None:
        assert not type_matches(_TAGS, [])

    def test_too_many(self) -> None:
        assert not type_matches(_TAGS, ["a", "b", "c"])

    def test_an_unbounded_array_takes_any_length(self) -> None:
        assert type_matches({"type": "array"}, [])
        assert type_matches({"type": "array"}, list(range(100)))

    def test_a_malformed_bound_is_ignored_rather_than_refusing_everything(self) -> None:
        assert type_matches({"type": "array", "minItems": True}, [])
        assert type_matches({"type": "array", "maxItems": -1}, [1])
        assert type_matches({"type": "array", "maxItems": "2"}, [1, 2, 3])


class TestArrayItems:
    def test_every_element_matches_items(self) -> None:
        assert not type_matches(_TAGS, ["a", 1])

    def test_items_nest(self) -> None:
        schema = {"type": "array", "items": {"type": "array", "items": {"type": "number"}}}
        assert type_matches(schema, [[1, 2], [3.5]])
        assert not type_matches(schema, [[1, "2"]])


def test_root_config_rejects_an_out_of_bounds_array() -> None:
    config = RootConfig(properties={"tags": _TAGS})
    assert config.rejection("tags", ["a"]) is None
    assert config.rejection("tags", []) == "'tags' does not accept that value"
