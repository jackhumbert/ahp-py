"""The generated upstream data tables, and that the vendored pin is honest."""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

from agent_host_protocol.conformance.corpus import CORPUS_ROOT, pin, reducer_fixtures
from agent_host_protocol.types import (
    ACTION_INTRODUCED_IN,
    ACTION_TYPES,
    AHP_ERROR_CODES,
    IS_CLIENT_DISPATCHABLE,
    JSON_RPC_ERROR_CODES,
    NOTIFICATION_INTRODUCED_IN,
    UPSTREAM_PROTOCOL_VERSION,
    UPSTREAM_SUPPORTED_PROTOCOL_VERSIONS,
)

ROOT = Path(__file__).resolve().parents[2]


def test_action_tables_cover_every_action() -> None:
    """A missing entry would silently make an action non-dispatchable or unversioned."""
    assert len(ACTION_TYPES) == 85
    assert set(IS_CLIENT_DISPATCHABLE) == set(ACTION_TYPES)
    assert set(ACTION_INTRODUCED_IN) == set(ACTION_TYPES)


def test_client_dispatchable_count() -> None:
    """38 of 85. Upstream has 40 `@clientDispatchable` JSDoc annotations, but its
    own generator only counts declarations carrying `type: ActionType.X`, so the
    generated map -- which `isClientDispatchable` reads -- is authoritative."""
    assert sum(IS_CLIENT_DISPATCHABLE.values()) == 38


def test_unknown_actions_are_not_client_dispatchable() -> None:
    """The gate must default to closed for anything it does not recognise."""
    assert IS_CLIENT_DISPATCHABLE.get("future/madeUpAction", False) is False


def test_notification_versions_match_the_pinned_registry() -> None:
    """The 8-method table from `registry.ts` (`ServerNotificationMap` minus
    `action`), pinned verbatim so a pin bump that adds a notification cannot
    land without its version -- upstream's `isNotificationKnownToVersion`
    filter is unimplementable otherwise. Every current method predates the
    oldest negotiable version, so the filter is inert at this pin."""
    assert NOTIFICATION_INTRODUCED_IN == {
        "root/sessionAdded": "0.1.0",
        "root/sessionRemoved": "0.1.0",
        "root/sessionSummaryChanged": "0.1.0",
        "root/progress": "0.5.0",
        "auth/required": "0.1.0",
        "otlp/exportLogs": "0.2.0",
        "otlp/exportTraces": "0.2.0",
        "otlp/exportMetrics": "0.2.0",
    }

    def parts(version: str) -> tuple[int, ...]:
        return tuple(int(p) for p in version.split("."))

    oldest_negotiable = parts(UPSTREAM_SUPPORTED_PROTOCOL_VERSIONS[-1])
    assert all(parts(v) <= oldest_negotiable for v in NOTIFICATION_INTRODUCED_IN.values())


def test_error_codes_match_the_spec() -> None:
    """Use the spec's codes; never invent parallel ones."""
    assert JSON_RPC_ERROR_CODES["MethodNotFound"] == -32601
    assert AHP_ERROR_CODES["UnsupportedProtocolVersion"] == -32005
    assert AHP_ERROR_CODES["AuthRequired"] == -32007
    # -32011 Conflict is absent from upstream's published errors.schema.json
    # (the generator hardcodes the enum); we read types/common/errors.ts, which
    # has it. See UPSTREAM.md.
    assert AHP_ERROR_CODES["Conflict"] == -32011
    assert len(AHP_ERROR_CODES) == 11


def test_upstream_version_constants() -> None:
    assert UPSTREAM_PROTOCOL_VERSION == "0.7.0"
    assert UPSTREAM_SUPPORTED_PROTOCOL_VERSIONS[0] == UPSTREAM_PROTOCOL_VERSION
    assert UPSTREAM_SUPPORTED_PROTOCOL_VERSIONS == ("0.7.0", "0.6.0", "0.5.2", "0.5.1")


def test_vendored_pin_matches_upstream_md() -> None:
    """The committed corpus and the documented pin must not drift apart."""
    vendored = pin()
    doc = (ROOT / "UPSTREAM.md").read_text(encoding="utf-8")
    assert vendored["specTag"] in doc, "UPSTREAM.md does not mention the vendored spec tag"
    assert vendored["specCommit"] in doc, "UPSTREAM.md does not mention the vendored commit"


def test_generated_file_is_reproducible() -> None:
    """CI guard: the checked-in table must match what the generator emits today."""
    target = ROOT / "src" / "agent_host_protocol" / "types" / "_generated.py"
    before = target.read_text(encoding="utf-8")
    subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "generate_tables.py")],
        check=True,
        capture_output=True,
    )
    assert target.read_text(encoding="utf-8") == before, (
        "_generated.py is stale; run `python scripts/generate_tables.py`"
    )


def test_every_action_in_the_reducer_corpus_is_known_or_deliberately_unknown() -> None:
    """The corpus contains six synthetic unknown actions that test forward
    compatibility. Everything else must be a real action we have a table entry
    for -- otherwise our tables are missing something upstream ships."""
    used = {a["type"] for fx in reducer_fixtures() for a in fx.actions}
    unknown = sorted(used - set(ACTION_TYPES))
    assert unknown == [
        "annotations/unknownActionType",
        "changeset/nonExistentAction",
        "resourceWatch/unknownAction",
        "root/nonExistentAction",
        "session/nonExistentAction",
        "terminal/nonExistentAction",
    ], f"unexpected unknown action types in the corpus: {unknown}"


def test_root_session_chat_action_counts() -> None:
    """The v0.1 scope, asserted so a pin bump that widens it is noticed."""
    counts: dict[str, int] = {}
    for action in ACTION_TYPES:
        counts[action.split("/")[0]] = counts.get(action.split("/")[0], 0) + 1
    assert counts["root"] == 4
    assert counts["session"] == 27
    assert counts["chat"] == 29
    assert counts["root"] + counts["session"] + counts["chat"] == 60


def test_schemas_are_vendored_but_not_trusted() -> None:
    """We vendor the schemas for reference, and record why they cannot validate.

    `actions.schema.json` has shipped a malformed empty `$ref` since spec/v0.5.0;
    this test pins that fact so the day it is fixed upstream, we notice.
    """
    schema = json.loads(
        (CORPUS_ROOT / "schema" / "actions.schema.json").read_text(encoding="utf-8")
    )
    one_of = schema["$defs"]["StateAction"]["oneOf"]
    malformed = [v for v in one_of if v.get("$ref", "").rstrip("/") == "#/$defs"]
    assert len(malformed) == 1, (
        "actions.schema.json's malformed empty $ref is gone -- upstream may have "
        "fixed it. Re-evaluate the schema-validation gate in docs/plan.md §10."
    )


def test_session_status_schema_is_a_closed_enum_that_rejects_real_values() -> None:
    """The other reason we do not generate from the schemas. Pinned so a fix is noticed."""
    schema = json.loads((CORPUS_ROOT / "schema" / "state.schema.json").read_text(encoding="utf-8"))
    declared = schema["$defs"]["SessionStatus"]
    assert "enum" in declared, "SessionStatus is no longer a closed enum -- re-evaluate"
    assert "Bitset" in declared.get("description", ""), "description no longer says bitset"
    real_values = {1, 2, 8, 24, 33, 40, 56, 65, 72, 2147483720}
    rejected = sorted(real_values - set(declared["enum"]))
    assert rejected, "the enum now accepts every real value -- re-evaluate"


def test_generator_parses_the_vendored_source_not_a_snapshot() -> None:
    """The generator must read vendor/upstream/ts, so a pin bump flows through."""
    source = (ROOT / "scripts" / "generate_tables.py").read_text(encoding="utf-8")
    assert re.search(r'VENDOR\s*=\s*ROOT\s*/\s*"vendor"\s*/\s*"upstream"\s*/\s*"ts"', source)
