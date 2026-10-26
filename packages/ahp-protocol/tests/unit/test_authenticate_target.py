"""`AuthenticateParams.target` and the changeset nonce (upstream `c5628cd4`).

Both are optional additions, so the contract worth pinning is: absent is still
valid, a well-formed value is valid, a malformed one is reported, and a `kind`
this package has never heard of is *not* an error -- `AuthenticateTargetKind` is
`@nonexhaustive`, and the host (not the type layer) decides what to do with a
consumer it cannot serve.
"""

from __future__ import annotations

from typing import Any

import pytest

from ahp_protocol.types import SPECS

SESSION = "copilot:/6d0b3bd7"


def _params(**extra: Any) -> dict[str, Any]:
    return {"channel": "ahp-root://", "resource": "https://api.example.com", "token": "t", **extra}


def _problems(name: str, value: Any) -> list[str]:
    return SPECS[name].validate(value)


def test_authenticate_params_without_a_target_is_unchanged() -> None:
    assert _problems("AuthenticateParams", _params()) == []
    assert _problems("AuthenticateParams", _params(expiresIn=60, scopes=["a"])) == []


def test_authenticate_params_requires_its_own_fields() -> None:
    assert _problems("AuthenticateParams", {"channel": "ahp-root://", "token": "t"})
    assert _problems("AuthenticateParams", _params(token=5))


def test_an_mcp_server_target_validates() -> None:
    target = {"kind": "mcpServer", "session": SESSION, "customizationId": "github"}
    assert _problems("AuthenticateParams", _params(target=target)) == []
    assert _problems("AuthenticateTarget", target) == []


@pytest.mark.parametrize(
    "target",
    [
        {"kind": "mcpServer", "customizationId": "github"},
        {"kind": "mcpServer", "session": SESSION},
        {"kind": "mcpServer", "session": 1, "customizationId": "github"},
        {"kind": "mcpServer", "session": SESSION, "customizationId": 7},
    ],
)
def test_an_mcp_server_target_needs_string_session_and_customization_id(
    target: dict[str, Any],
) -> None:
    assert _problems("AuthenticateParams", _params(target=target))


@pytest.mark.parametrize("target", ["mcpServer", ["mcpServer"], 3, {}, {"kind": 1}, {"kind": None}])
def test_a_target_must_be_an_object_with_a_string_kind(target: Any) -> None:
    assert _problems("AuthenticateParams", _params(target=target))


def test_an_unknown_target_kind_validates_and_survives_verbatim() -> None:
    target = {"kind": "hostTool", "session": SESSION, "extra": {"a": 1}}
    params = _params(target=target)
    assert _problems("AuthenticateParams", params) == []
    assert params["target"] == {"kind": "hostTool", "session": SESSION, "extra": {"a": 1}}


def test_an_explicit_null_target_is_treated_as_absent() -> None:
    """Same rule as every other optional field (`ObjectSpec`).

    The shape check only. ahp-host and ahp-gateway refuse a present `target: null`
    on their own: what they would do with a token they cannot scope is broadcast it.
    """
    assert _problems("AuthenticateParams", _params(target=None)) == []


@pytest.mark.parametrize(
    "target",
    [
        {"kind": "resource", "resource": "file:///a.txt", "nonce": "n1"},
        {
            "kind": "range",
            "resource": "file:///a.txt",
            "range": {"start": {"line": 0, "character": 0}, "end": {"line": 1, "character": 0}},
            "nonce": "n1",
        },
    ],
)
def test_changeset_operation_target_checks_the_nonce_type(target: dict[str, Any]) -> None:
    assert _problems("ChangesetOperationTarget", target) == []
    assert _problems("ChangesetOperationTarget", {**target, "nonce": 5})
    legacy = {k: v for k, v in target.items() if k != "nonce"}
    assert _problems("ChangesetOperationTarget", legacy) == []
