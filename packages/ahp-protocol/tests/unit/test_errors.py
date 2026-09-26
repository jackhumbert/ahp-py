"""The error taxonomy, in both directions.

`to_json` is the host's exit; `from_json` is the client's entry. They are tested
together because a taxonomy that only round-trips in one direction is how two
peers end up with two vocabularies for the same wire codes.
"""

from __future__ import annotations

from typing import Any

import pytest

from agent_host_protocol.errors import (
    AhpError,
    from_json,
    method_not_found,
    unsupported_protocol_version,
)
from agent_host_protocol.types import AHP_ERROR_CODES, JSON_RPC_ERROR_CODES


def test_round_trip_preserves_code_message_and_data() -> None:
    original = AhpError(-32007, "auth required", {"resources": [{"resource": "https://x"}]})
    rebuilt = from_json(original.to_json())
    assert (rebuilt.code, rebuilt.message, rebuilt.data) == (
        original.code,
        original.message,
        original.data,
    )


def test_absent_data_stays_absent() -> None:
    """`to_json` omits the key rather than writing null, and `from_json` agrees.

    Python's `json.dumps` emits `null` where `JSON.stringify` drops the key, so
    every omission has to be explicit or the wire grows fields the reference
    implementations never send.
    """
    assert "data" not in AhpError(-32001, "gone").to_json()
    assert from_json({"code": -32001, "message": "gone"}).data is None


def test_unknown_codes_are_accepted_verbatim() -> None:
    """Third-party hosts ship their own maps.

    `@wyrd-company/ahp-server` answers a missing session with -32008 NotFound and
    does not define -32001 at all, and upstream's own errors.schema.json omits
    -32011 Conflict. A client that rejects an unrecognised code turns someone
    else's extension into a crash.
    """
    err = from_json({"code": -32099, "message": "vendor specific"})
    assert err.code == -32099
    assert err.message == "vendor specific"


def test_conflict_is_known_even_though_the_published_schema_omits_it() -> None:
    assert AHP_ERROR_CODES["Conflict"] == -32011


@pytest.mark.parametrize(
    "member",
    [
        {},
        {"message": "no code"},
        {"code": "-32001", "message": "code as a string"},
        {"code": None},
        # `bool` is an `int` in Python; JSON `true` is not an error code.
        {"code": True, "message": "boolean code"},
    ],
)
def test_malformed_members_become_internal_error_rather_than_raising(member: Any) -> None:
    """There is no honest attribution for a malformed member, and dropping it
    would leave the caller waiting on a request that has already failed."""
    err = from_json(member)
    assert err.code == JSON_RPC_ERROR_CODES["InternalError"]


def test_missing_message_does_not_produce_the_string_none() -> None:
    assert from_json({"code": -32001}).message == ""


def test_method_not_found_uses_the_json_rpc_code() -> None:
    assert method_not_found("resourceRead").code == -32601


def test_unsupported_protocol_version_data_uses_the_declared_field_name() -> None:
    """The -32005 data field is `supportedVersions` -- the single member of
    `UnsupportedProtocolVersionErrorData` (`ts/errors.ts`, required by
    `errors.schema.json`). The first cut emitted `supportedProtocolVersions`,
    which a conformant client reads as absent, so it could not tell the user
    which host versions would have worked."""
    err = unsupported_protocol_version(("0.7.0", "0.6.0"))
    assert err.code == AHP_ERROR_CODES["UnsupportedProtocolVersion"]
    assert err.data == {"supportedVersions": ["0.7.0", "0.6.0"]}


def test_unsupported_protocol_version_data_validates_against_the_pinned_schema() -> None:
    """Field-name drift from the vendored schema must fail here, not in a peer."""
    pytest.importorskip("jsonschema")
    from agent_host_protocol.conformance.schemas import validate_against

    err = unsupported_protocol_version(("0.7.0",))
    assert validate_against("errors", "UnsupportedProtocolVersionErrorData", err.data) == []
