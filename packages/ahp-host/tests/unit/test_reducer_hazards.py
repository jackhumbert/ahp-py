"""Divergences from the reference reducers that the fixture corpus does not pin.

Every case here came out of a differential audit that drove ~1.2M single-step
cases through both our port and the real TypeScript reducers under
`node --experimental-transform-types`. The corpus is necessary but not
sufficient: it exercises well-formed inputs, and these are the shapes an
untrusted or merely different peer actually sends.

The recurring root cause is that JavaScript distinguishes `undefined` from
`null` and Python does not: `x === undefined` is false for a JSON `null`, while
`x is None` is true for both an explicit null and an absent key.
"""

from __future__ import annotations

from typing import Any

import pytest

from agent_host_server.reducers import chat_reducer, session_reducer
from agent_host_server.reducers.clock import frozen_clock


def _chat(**overrides: object) -> dict[str, object]:
    state: dict[str, object] = {
        "resource": "ahp-chat:/c1",
        "title": "Chat",
        "status": 1,
        "modifiedAt": "1970-01-01T00:00:01.000Z",
        "turns": [],
    }
    state.update(overrides)
    return state


def _session(**overrides: object) -> dict[str, object]:
    state: dict[str, object] = {
        "provider": "echo",
        "title": "Session",
        "status": 1,
        "lifecycle": "ready",
        "activeClients": [],
        "chats": [],
    }
    state.update(overrides)
    return state


class TestTruncatedNullTurnId:
    """`chat/truncated` clears everything only when `turnId` is ABSENT.

    The reference tests `action.turnId === undefined`; a JSON `null` fails that
    and falls through to an id search that finds nothing, i.e. a no-op. Treating
    `null` as absent destroys the entire transcript -- and `chat/truncated` is
    client-dispatchable, so any peer whose serializer writes `None`/`nil` as
    `null` rather than omitting the key wipes the chat.
    """

    def test_absent_turn_id_clears_everything(self) -> None:
        state = _chat(
            turns=[{"id": "turn-1", "message": {}, "responseParts": [], "state": "complete"}],
            turnsNextCursor="cur-1",
        )
        with frozen_clock():
            out = chat_reducer(state, {"type": "chat/truncated"})
        assert out["turns"] == []
        assert "turnsNextCursor" not in out or out["turnsNextCursor"] is None

    def test_explicit_null_turn_id_is_a_no_op(self) -> None:
        state = _chat(
            turns=[{"id": "turn-1", "message": {}, "responseParts": [], "state": "complete"}],
            turnsNextCursor="cur-1",
        )
        with frozen_clock():
            out = chat_reducer(state, {"type": "chat/truncated", "turnId": None})
        assert out["turns"] == state["turns"], "a null turnId must not clear history"
        assert out["turnsNextCursor"] == "cur-1"


class TestUnhashableWireValues:
    """Membership and set/dict keys must tolerate any JSON value.

    The reference compares with `===` and keys with `Map`/`Set`, both total over
    any value. Python `x in frozenset` and `{...}` raise `TypeError` on a dict or
    list. Every path below is reachable from a client-dispatchable action, and
    `chat/toolCallComplete` spreads the client's `result` onto the tool call, so
    a client picks the value.
    """

    def test_dict_tool_call_status_does_not_raise(self) -> None:
        state = _chat(
            activeTurn={
                "id": "t1",
                "startedAt": "x",
                "message": {},
                "responseParts": [
                    {"kind": "toolCall", "toolCall": {"toolCallId": "tc1", "status": "running"}}
                ],
            }
        )
        with frozen_clock():
            chat_reducer(
                state,
                {
                    "type": "chat/toolCallComplete",
                    "turnId": "t1",
                    "toolCallId": "tc1",
                    "result": {"status": {"evil": 1}},
                },
            )

    def test_list_tool_call_status_does_not_raise(self) -> None:
        state = _chat(
            activeTurn={
                "id": "t1",
                "startedAt": "x",
                "message": {},
                "responseParts": [
                    {"kind": "toolCall", "toolCall": {"toolCallId": "tc1", "status": "running"}}
                ],
            }
        )
        with frozen_clock():
            chat_reducer(
                state,
                {
                    "type": "chat/toolCallComplete",
                    "turnId": "t1",
                    "toolCallId": "tc1",
                    "result": {"status": ["a"]},
                },
            )

    def test_unhashable_pending_message_id_survives_reorder(self) -> None:
        with frozen_clock():
            state = chat_reducer(
                _chat(),
                {
                    "type": "chat/pendingMessageSet",
                    "kind": "queued",
                    "id": {"a": 1},
                    "message": {"text": "hi"},
                },
            )
            chat_reducer(state, {"type": "chat/queuedMessagesReordered", "ids": []})

    def test_unhashable_turn_id_survives_turns_loaded(self) -> None:
        state = _chat(turns=[{"id": {"a": 1}, "message": {}, "responseParts": []}])
        with frozen_clock():
            chat_reducer(state, {"type": "chat/turnsLoaded", "turns": []})


class TestSessionActionsWithMissingFields:
    """The reference returns a state where we were raising `KeyError`.

    Nine of these are client-dispatchable. TypeScript reads a missing property
    as `undefined` and carries on; `action["x"]` raises, and before the
    sequencer was hardened that both burnt a `serverSeq` and dropped the
    connection.
    """

    @pytest.mark.parametrize(
        "action",
        [
            {"type": "session/titleChanged"},
            {"type": "session/activeClientSet"},
            {"type": "session/activeClientSet", "activeClient": "not-a-mapping"},
            {"type": "session/activeClientRemoved"},
            {"type": "session/workingDirectorySet"},
            {"type": "session/workingDirectoryRemoved"},
            {"type": "session/customizationToggled"},
            {"type": "session/mcpServerStartRequested"},
            {"type": "session/mcpServerStopRequested"},
            {"type": "session/configChanged"},
            {"type": "session/activityChanged"},
            {"type": "session/isReadChanged"},
            {"type": "session/isArchivedChanged"},
        ],
        ids=lambda a: str(a["type"]).split("/")[-1] + ("-badtype" if len(a) > 1 else ""),
    )
    def test_does_not_raise(self, action: dict[str, object]) -> None:
        session_reducer(_session(config={"values": {}}), action)

    def test_customization_toggled_with_a_matching_id_and_no_enabled(self) -> None:
        state = _session(
            customizations=[{"type": "plugin", "id": "p1", "enabled": True, "children": []}]
        )
        session_reducer(state, {"type": "session/customizationToggled", "id": "p1"})


class TestNullClearsRatherThanIgnored:
    """`x !== undefined` accepts an explicit null, so null CLEARS the field."""

    def test_meta_null_clears_on_tool_call_content_changed(self) -> None:
        state = _chat(
            activeTurn={
                "id": "t1",
                "startedAt": "x",
                "message": {},
                "responseParts": [
                    {
                        "kind": "toolCall",
                        "toolCall": {
                            "toolCallId": "tc1",
                            "status": "running",
                            "_meta": {"keep": 1},
                        },
                    }
                ],
            }
        )
        with frozen_clock():
            out = chat_reducer(
                state,
                {
                    "type": "chat/toolCallContentChanged",
                    "turnId": "t1",
                    "toolCallId": "tc1",
                    "content": [],
                    "_meta": None,
                },
            )
        call = out["activeTurn"]["responseParts"][0]["toolCall"]
        assert call.get("_meta") in (None, {}), "an explicit null _meta must clear it"


class TestOpenInputRequest:
    """`part.response === undefined` means open; an explicit null means resolved.

    This host can produce the offending state itself, so the divergence is not
    hypothetical: writing `"response": None` for an action that omitted the key
    serialises to `"response": null`, which a reference client reads as resolved
    while we still consider it open. The two then compute different `status`.
    """

    def test_input_completed_without_a_response_key_does_not_store_null(self) -> None:
        with frozen_clock():
            state = chat_reducer(
                _chat(
                    activeTurn={
                        "id": "t1",
                        "startedAt": "x",
                        "message": {},
                        "responseParts": [
                            {
                                "kind": "inputRequest",
                                "id": "ir1",
                                "request": {"requestId": "r1", "questions": []},
                            }
                        ],
                    }
                ),
                {"type": "chat/inputCompleted", "turnId": "t1", "requestId": "r1"},
            )
        part = state["activeTurn"]["responseParts"][0]
        assert "response" not in part or part["response"] is not None, (
            "an omitted response must not be stored as null -- it serialises to "
            '"response": null, which a reference client reads as resolved'
        )


class TestInputAnswerChanged:
    """The elicitation block, found by the verify pass after the first six fixes.

    All four are reachable from `chat/inputAnswerChanged`, which is
    client-dispatchable, and none is covered by any fixture.
    """

    @staticmethod
    def _open_request(answers: object = None) -> dict[str, object]:
        # The lookup is on `part.request.id` -- not `requestId`.
        request: dict[str, object] = {"id": "r1", "questions": []}
        if answers is not None:
            request["answers"] = answers
        return _chat(
            activeTurn={
                "id": "t1",
                "startedAt": "x",
                "message": {},
                "responseParts": [{"kind": "inputRequest", "id": "ir1", "request": request}],
            }
        )

    @staticmethod
    def _answers(state: Any) -> Any:
        return state["activeTurn"]["responseParts"][0]["request"].get("answers")

    def test_an_explicit_null_answer_is_stored_not_deleted(self) -> None:
        """The reference tests `action.answer === undefined`, so a null answer
        takes the *else* branch and is stored as a value."""
        with frozen_clock():
            out = chat_reducer(
                self._open_request({"q1": "old"}),
                {
                    "type": "chat/inputAnswerChanged",
                    "requestId": "r1",
                    "questionId": "q1",
                    "answer": None,
                },
            )
        assert self._answers(out) == {"q1": None}, "a null answer must be stored, not deleted"

    def test_an_omitted_answer_deletes(self) -> None:
        with frozen_clock():
            out = chat_reducer(
                self._open_request({"q1": "old", "q2": "keep"}),
                {"type": "chat/inputAnswerChanged", "requestId": "r1", "questionId": "q1"},
            )
        assert self._answers(out) == {"q2": "keep"}

    def test_an_unhashable_question_id_does_not_raise(self) -> None:
        """`answers` is a plain JS object, so any key is coerced to a string
        rather than raising -- Python would `TypeError` on a dict key."""
        with frozen_clock():
            out = chat_reducer(
                self._open_request({"q1": "old"}),
                {
                    "type": "chat/inputAnswerChanged",
                    "requestId": "r1",
                    "questionId": {"a": 1},
                    "answer": "x",
                },
            )
        assert self._answers(out) == {"q1": "old", "[object Object]": "x"}

    def test_array_valued_answers_are_spread_by_index_not_corrupted(self) -> None:
        """`{...["ab", "cd"]}` is `{"0": "ab", "1": "cd"}` in JS.

        `dict(["ab", "cd"])` would silently produce `{'a': 'b', 'c': 'd'}`, and
        `dict(["p", "q"])` would raise outright.
        """
        with frozen_clock():
            out = chat_reducer(
                self._open_request(["ab", "cd"]),
                {
                    "type": "chat/inputAnswerChanged",
                    "requestId": "r1",
                    "questionId": "k",
                    "answer": "v",
                },
            )
        assert self._answers(out) == {"0": "ab", "1": "cd", "k": "v"}

    def test_a_two_character_array_answer_does_not_raise(self) -> None:
        with frozen_clock():
            chat_reducer(
                self._open_request(["p", "q"]),
                {
                    "type": "chat/inputAnswerChanged",
                    "requestId": "r1",
                    "questionId": "k",
                    "answer": "v",
                },
            )
