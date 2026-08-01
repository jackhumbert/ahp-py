"""Loaders for the vendored upstream conformance corpora.

The corpora live only in the upstream git repository -- they are not published as
release assets -- so they are vendored at a pinned tag by
``scripts/vendor_upstream.sh`` and committed. Nothing here touches the network;
the suite must pass offline.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

__all__ = [
    "CORPUS_ROOT",
    "ReducerFixture",
    "RoundTripFixture",
    "pin",
    "reducer_fixtures",
    "round_trip_fixtures",
]

CORPUS_ROOT = Path(__file__).resolve().parents[3] / "vendor" / "upstream"

_REDUCER_NAMES = frozenset(
    {"root", "session", "chat", "terminal", "changeset", "annotations", "resourceWatch"}
)


def pin() -> dict[str, Any]:
    """The vendored upstream pin, for asserting it matches ``UPSTREAM.md``."""
    loaded: dict[str, Any] = json.loads((CORPUS_ROOT / "PIN.json").read_text(encoding="utf-8"))
    return loaded


@dataclass(frozen=True)
class ReducerFixture:
    path: Path
    description: str
    reducer: str
    initial: Any
    actions: list[Any]
    expected: Any

    @property
    def id(self) -> str:
        return self.path.stem


@dataclass(frozen=True)
class RoundTripFixture:
    path: Path
    name: str
    group: str
    description: str
    type_name: str
    input: Any
    acceptable_outputs: list[Any]
    preserved_output: Any | None

    @property
    def id(self) -> str:
        return self.path.stem

    @property
    def expected(self) -> Any:
        """The output form OUR implementation must produce.

        Group B fixtures carry a known type plus extra unmodelled keys. Upstream
        ships two legitimate expectations for them: runtime-decoder clients drop
        the unknown keys (``acceptableOutputs[0]``), while TypeScript -- which has
        no runtime decoder -- preserves them (``preservedOutput``). We preserve
        (ADR 0001), so we assert ``preservedOutput`` where it exists. Group A has
        only one form and every implementation agrees on it.
        """
        if self.preserved_output is not None:
            return self.preserved_output
        return self.acceptable_outputs[0]


def _require(raw: dict[str, Any], path: Path, *keys: str) -> None:
    missing = [k for k in keys if k not in raw]
    if missing:
        raise ValueError(f"{path.name}: fixture missing {missing}")


def reducer_fixtures() -> Iterator[ReducerFixture]:
    """The 247-fixture reducer corpus, validated structurally as it loads.

    A malformed upstream fixture must fail loudly rather than be silently
    skipped -- a skipped fixture looks identical to a passing one in a summary.
    """
    directory = CORPUS_ROOT / "test-cases" / "reducers"
    for path in sorted(directory.glob("*.json")):
        raw = json.loads(path.read_text(encoding="utf-8"))
        _require(raw, path, "description", "reducer", "initial", "actions", "expected")
        if raw["reducer"] not in _REDUCER_NAMES:
            raise ValueError(f"{path.name}: unknown reducer {raw['reducer']!r}")
        if not isinstance(raw["actions"], list) or not raw["actions"]:
            raise ValueError(f"{path.name}: 'actions' must be a non-empty list")
        yield ReducerFixture(
            path=path,
            description=raw["description"],
            reducer=raw["reducer"],
            initial=raw["initial"],
            actions=raw["actions"],
            expected=raw["expected"],
        )


def round_trip_fixtures() -> Iterator[RoundTripFixture]:
    """The 39-fixture wire round-trip corpus."""
    directory = CORPUS_ROOT / "test-cases" / "round-trips"
    for path in sorted(directory.glob("*.json")):
        raw = json.loads(path.read_text(encoding="utf-8"))
        _require(raw, path, "name", "description", "type", "input", "acceptableOutputs")
        outputs = raw["acceptableOutputs"]
        if not isinstance(outputs, list) or len(outputs) != 1:
            # Upstream: "acceptableOutputs MUST have exactly one entry --
            # multiple entries would cement observed-but-wrong divergence."
            raise ValueError(f"{path.name}: acceptableOutputs must have exactly one entry")
        yield RoundTripFixture(
            path=path,
            name=raw["name"],
            group=raw.get("group", "A"),
            description=raw["description"],
            type_name=raw["type"],
            input=raw["input"],
            acceptable_outputs=outputs,
            preserved_output=raw.get("preservedOutput"),
        )
