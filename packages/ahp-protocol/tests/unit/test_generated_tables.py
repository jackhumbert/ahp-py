"""The generated upstream data tables, and that the vendored pin is honest."""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

from ahp_protocol.conformance.corpus import CORPUS_ROOT, pin, reducer_fixtures
from ahp_protocol.types import (
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
    assert len(ACTION_TYPES) == 107
    assert set(IS_CLIENT_DISPATCHABLE) == set(ACTION_TYPES)
    assert set(ACTION_INTRODUCED_IN) == set(ACTION_TYPES)


def test_client_dispatchable_count() -> None:
    """48 of 107. Upstream has more `@clientDispatchable` JSDoc annotations, but
    its own generator only counts declarations carrying `type: ActionType.X`, so
    the generated map -- which `isClientDispatchable` reads -- is authoritative.
    1.1.0 added one: `chat/configChanged`."""
    assert sum(IS_CLIENT_DISPATCHABLE.values()) == 48
    assert IS_CLIENT_DISPATCHABLE["chat/configChanged"] is True


def test_unknown_actions_are_not_client_dispatchable() -> None:
    """The gate must default to closed for anything it does not recognise."""
    assert IS_CLIENT_DISPATCHABLE.get("future/madeUpAction", False) is False


def test_notification_versions_match_the_pinned_registry() -> None:
    """The 14-method table from `registry.ts`, pinned verbatim so a pin bump
    that adds a notification cannot land without its version -- upstream's
    `isNotificationKnownToVersion` filter is unimplementable otherwise.

    1.1.0 is the first pin where the filter bites: windowed delivery's
    `channel/*` and the TCP channel's `tcp/*` are newer than every baseline,
    so a 1.0.0 connection must never see one. The table also lists the
    client->server methods among them (`channel/credit`, `tcp/data`, ...),
    exactly as upstream's registry does."""
    assert NOTIFICATION_INTRODUCED_IN == {
        "root/sessionAdded": "0.1.0",
        "root/sessionRemoved": "0.1.0",
        "root/sessionSummaryChanged": "0.1.0",
        "root/progress": "0.5.0",
        "auth/required": "0.1.0",
        "otlp/exportLogs": "0.2.0",
        "otlp/exportTraces": "0.2.0",
        "otlp/exportMetrics": "0.2.0",
        "channel/frame": "1.1.0",
        "channel/credit": "1.1.0",
        "channel/snapshot": "1.1.0",
        "tcp/data": "1.1.0",
        "tcp/eof": "1.1.0",
        "tcp/reset": "1.1.0",
    }

    def parts(version: str) -> tuple[int, ...]:
        return tuple(int(p) for p in version.split("."))

    newest_baseline = max(parts(v) for v in UPSTREAM_SUPPORTED_PROTOCOL_VERSIONS)
    newer = sorted(m for m, v in NOTIFICATION_INTRODUCED_IN.items() if parts(v) > newest_baseline)
    assert newer == [
        "channel/credit",
        "channel/frame",
        "channel/snapshot",
        "tcp/data",
        "tcp/eof",
        "tcp/reset",
    ]


def test_error_codes_match_the_spec() -> None:
    """Use the spec's codes; never invent parallel ones."""
    assert JSON_RPC_ERROR_CODES["MethodNotFound"] == -32601
    assert AHP_ERROR_CODES["UnsupportedProtocolVersion"] == -32005
    assert AHP_ERROR_CODES["AuthRequired"] == -32007
    # -32011 Conflict is absent from upstream's published errors.schema.json
    # (the generator hardcodes the enum); we read types/common/errors.ts, which
    # has it. See UPSTREAM.md.
    assert AHP_ERROR_CODES["Conflict"] == -32011
    # 0.9.0 removed `ContentNotFound`; -32006 "remains reserved and unassigned".
    assert "ContentNotFound" not in AHP_ERROR_CODES
    assert -32006 not in AHP_ERROR_CODES.values()
    # 1.1.0: the TCP channel's one failure of its own.
    assert AHP_ERROR_CODES["TcpConnectionOpenFailed"] == -32012
    assert len(AHP_ERROR_CODES) == 11


def test_upstream_version_constants() -> None:
    # The pin is a commit on upstream `main`, whose source tree declares the
    # version under development; 1.1.0 is not released.
    assert UPSTREAM_PROTOCOL_VERSION == "1.1.0"
    # "Released compatibility baselines, independent of the current development
    # PROTOCOL_VERSION" (registry.ts) -- and at this pin, no longer led by it.
    assert UPSTREAM_SUPPORTED_PROTOCOL_VERSIONS == ("1.0.0", "0.9.0")


def test_vendored_pin_matches_upstream_md() -> None:
    """The committed corpus and the documented pin must not drift apart."""
    vendored = pin()
    doc = (ROOT / "UPSTREAM.md").read_text(encoding="utf-8")
    assert vendored["specRef"] in doc, "UPSTREAM.md does not mention the vendored spec ref"
    assert vendored["specCommit"] in doc, "UPSTREAM.md does not mention the vendored commit"


def test_generated_file_is_reproducible() -> None:
    """CI guard: the checked-in table must match what the generator emits today."""
    target = ROOT / "src" / "ahp_protocol" / "types" / "_generated.py"
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
    assert counts["session"] == 30
    assert counts["chat"] == 38
    assert counts["root"] + counts["session"] + counts["chat"] == 72


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


def test_empty_state_schema_admits_every_object() -> None:
    """1.1.0's `EmptyState` schema lacks `maxProperties: 0`, so inside
    `Snapshot.state`'s `oneOf` every non-empty state matches twice and fails.
    `conformance.schemas.load` closes it; this pins the raw defect so the day
    upstream fixes it, the workaround is noticed and can go."""
    schema = json.loads((CORPUS_ROOT / "schema" / "state.schema.json").read_text(encoding="utf-8"))
    empty = schema["$defs"]["EmptyState"]
    fixed = "EmptyState is closed upstream now -- drop the patch in conformance/schemas.py"
    assert "maxProperties" not in empty, fixed
    assert "additionalProperties" not in empty, fixed
    refs = [v.get("$ref") for v in schema["$defs"]["Snapshot"]["properties"]["state"]["oneOf"]]
    assert "#/$defs/EmptyState" in refs


def test_generator_parses_the_vendored_source_not_a_snapshot() -> None:
    """The generator must read vendor/upstream/ts, so a pin bump flows through."""
    source = (ROOT / "scripts" / "generate_tables.py").read_text(encoding="utf-8")
    assert re.search(r'VENDOR\s*=\s*ROOT\s*/\s*"vendor"\s*/\s*"upstream"\s*/\s*"ts"', source)
