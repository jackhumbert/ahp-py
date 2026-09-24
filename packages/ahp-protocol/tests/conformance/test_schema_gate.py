"""The shipped schema gate itself: coverage decisions, not schema content.

The corpus tests prove the schemas validate real traffic; these prove the
GATE's own tables match the pin -- a channel kind the map omits crashes the
caller with `KeyError` instead of reporting shape problems, and a method
wrongly listed as resultless silently skips a result the schema declares.
"""

from __future__ import annotations

import pytest

from tests.conformance.schemas import assert_valid_result, assert_valid_state


class TestStateGateCoversEverySnapshotKind:
    def test_resource_watch_state_validates(self) -> None:
        """`resourceWatch` is the seventh channel kind and the pin defines
        `ResourceWatchState`; the first cut of the map stopped at six, so
        gating a resource-watch snapshot raised `KeyError('resourceWatch')`."""
        assert_valid_state("resourceWatch", {"root": "file:///tmp/w", "recursive": True})

    def test_the_automation_kinds_resolve_to_their_definitions(self) -> None:
        """0.9.0 added two channels; a snapshot of either must be gated, not
        `KeyError`."""
        from agent_host_protocol.conformance.corpus import reducer_fixtures

        initial = {f.id: f.initial for f in reducer_fixtures()}
        assert_valid_state("automation", initial["264-automation-set-replaces"])
        assert_valid_state("automationRun", initial["265-automation-run-session-lifecycle"])

    def test_resource_watch_state_reports_shape_problems(self) -> None:
        with pytest.raises(AssertionError, match="ResourceWatchState"):
            # `recursive` is required and `root` must be a URI string.
            assert_valid_state("resourceWatch", {"root": 5})

    def test_every_reducer_kind_has_a_definition(self) -> None:
        """The gate's map and `REDUCERS` must not drift apart: a kind present
        in one and not the other is a `KeyError` at the first real snapshot."""
        from agent_host_protocol.reducers import REDUCERS

        for kind in REDUCERS:
            with pytest.raises(AssertionError):
                # Not-an-object fails every state shape; reaching the assert
                # proves the kind resolved to a definition instead of raising
                # KeyError.
                assert_valid_state(kind, "not-an-object")


class TestResultGate:
    def test_authenticate_result_is_validated_not_skipped(self) -> None:
        """The pin declares `AuthenticateResult` ("an empty object on
        success", `ts/commands.ts`), so `authenticate` must not sit in
        `_RESULTLESS` -- there it would let a host answer with a string and
        pass the gate unchecked."""
        assert assert_valid_result("authenticate", {}) is True

    def test_authenticate_result_rejects_a_non_object(self) -> None:
        with pytest.raises(AssertionError, match="AuthenticateResult"):
            assert_valid_result("authenticate", "ok")

    def test_genuinely_resultless_methods_stay_skipped(self) -> None:
        """These declare no `<Method>Result` in the pin at all; the gate says
        so by returning False rather than passing or failing."""
        for method in ("dispatchAction", "unsubscribe", "createSession"):
            assert assert_valid_result(method, {"anything": 1}) is False
