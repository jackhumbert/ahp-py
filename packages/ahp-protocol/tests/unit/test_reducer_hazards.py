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

from agent_host_protocol.reducers import chat_reducer, root_reducer, session_reducer
from agent_host_protocol.reducers.clock import frozen_clock


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
        """`enabled: action.enabled` writes `undefined` upstream, which
        `JSON.stringify` drops -- so the key must GO AWAY, not become null."""
        state = _session(
            customizations=[{"type": "plugin", "id": "p1", "enabled": True, "children": []}]
        )
        out = session_reducer(state, {"type": "session/customizationToggled", "id": "p1"})
        assert "enabled" not in out["customizations"][0]


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

    def test_deleting_the_last_answer_removes_the_key_not_nulls_it(self) -> None:
        """Upstream writes `answers: ... : undefined`, and the explicit
        `undefined` member overwrites the spread copy before `JSON.stringify`
        drops it. `"answers": null` would be schema-invalid and reads as
        present (`!== undefined`) in a reference peer."""
        with frozen_clock():
            out = chat_reducer(
                self._open_request({"q1": "old"}),
                {"type": "chat/inputAnswerChanged", "requestId": "r1", "questionId": "q1"},
            )
        assert "answers" not in out["activeTurn"]["responseParts"][0]["request"]


class TestInputCompleted:
    """`chat/inputCompleted` merges two peer-controlled `answers` blobs.

    The reference merge `{ ...(part.request.answers ?? {}), ...(action.answers
    ?? {}) }` is total over ANY JSON value -- a JS object-spread of a string
    yields index keys, of a number yields `{}`. A bare `{**...}` raises
    `TypeError` on every truthy non-mapping, and both operands arrive from the
    peer (`chat/inputRequested` stores `answers` verbatim; the action is
    client-dispatchable).
    """

    @staticmethod
    def _with_stored_answers(answers: object) -> dict[str, object]:
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
    def _request(state: Any) -> Any:
        return state["activeTurn"]["responseParts"][0]["request"]

    def test_string_and_array_answers_merge_by_index_keys(self) -> None:
        """`{...'ab'}` is `{'0': 'a', '1': 'b'}` and `{...[9]}` is `{'0': 9}`,
        so the merge overlays index keys instead of raising."""
        with frozen_clock():
            out = chat_reducer(
                self._with_stored_answers("ab"),
                {"type": "chat/inputCompleted", "turnId": "t1", "requestId": "r1", "answers": [9]},
            )
        assert self._request(out)["answers"] == {"0": 9, "1": "b"}

    def test_numeric_answers_spread_to_nothing_and_the_key_is_dropped(self) -> None:
        """`{...42}` is `{}` -- and zero surviving answers means NO `answers`
        key (upstream `... : undefined`), never `"answers": null`."""
        with frozen_clock():
            out = chat_reducer(
                self._with_stored_answers(42),
                {"type": "chat/inputCompleted", "turnId": "t1", "requestId": "r1"},
            )
        assert "answers" not in self._request(out)

    @pytest.mark.parametrize("stored", ["abc", [1, 2], 42, True], ids=type)
    def test_truthy_non_mapping_stored_answers_do_not_raise(self, stored: object) -> None:
        with frozen_clock():
            chat_reducer(
                self._with_stored_answers(stored),
                {
                    "type": "chat/inputCompleted",
                    "turnId": "t1",
                    "requestId": "r1",
                    "answers": {"q": 1},
                },
            )


class TestStrictEqualityOnPeerControlledIds:
    """Every upstream id comparison is `===`, where `1 === true` is false and
    objects compare by reference. Python `==` matches `True` against `1` and
    matches structurally, so it selects entries the reference skips -- on data
    a peer controls.
    """

    def test_truncated_with_a_boolean_turn_id_never_matches_a_numeric_one(self) -> None:
        """The reference `findIndex(t => t.id === action.turnId)` misses, so it
        no-ops; `1 == True` would truncate the transcript and stamp
        `modifiedAt` -- a transcript-wipe-vs-no-op fork, and `chat/truncated`
        is client-dispatchable."""
        state = _chat(
            turns=[
                {"id": 1, "message": {}, "responseParts": []},
                {"id": "keep", "message": {}, "responseParts": []},
            ]
        )
        with frozen_clock():
            out = chat_reducer(state, {"type": "chat/truncated", "turnId": True})
        assert out["turns"] == state["turns"]
        assert out["modifiedAt"] == state["modifiedAt"]

    def test_working_directory_removed_true_does_not_remove_one(self) -> None:
        """`indexOf` compares with `===`; `in`/`list.index` would match."""
        state = _chat(workingDirectories=[1, "x"])
        with frozen_clock():
            out = chat_reducer(state, {"type": "chat/workingDirectoryRemoved", "directory": True})
        assert out["workingDirectories"] == [1, "x"]

    def test_working_directory_set_without_a_directory_appends_null(self) -> None:
        """An absent `directory` is `undefined`: `includes(undefined)` misses a
        parsed array's every element (even an explicit null is not `undefined`
        under SameValueZero), so upstream appends -- and `JSON.stringify`
        writes an `undefined` ARRAY ELEMENT as `null`. The oracle pins the
        first append.

        The second assertion pins a DOCUMENTED DIVERGENCE: upstream's array
        still holds the in-memory `undefined`, so a repeat no-ops there, while
        we stored its null image eagerly and append again. Keeping the
        sentinel in state would leak it into every consumer's `json.dumps`;
        the reducer's comment records the trade."""
        state = _chat(workingDirectories=["x"])
        with frozen_clock():
            once = chat_reducer(state, {"type": "chat/workingDirectorySet"})
            twice = chat_reducer(once, {"type": "chat/workingDirectorySet"})
        assert once["workingDirectories"] == ["x", None]
        assert twice["workingDirectories"] == ["x", None, None]

    def test_pending_message_removal_by_a_structurally_equal_object_id_misses(self) -> None:
        """`m.id !== action.id` compares objects by REFERENCE; an id parsed from
        a fresh frame is never the stored object, so the reference keeps the
        message where Python `==` would delete it."""
        state = _chat(queuedMessages=[{"id": {"a": 1}, "message": {}}])
        with frozen_clock():
            out = chat_reducer(
                state, {"type": "chat/pendingMessageRemoved", "kind": "queued", "id": {"a": 1}}
            )
        assert out["queuedMessages"] == state["queuedMessages"]


class TestJsTruthinessGates:
    """`if (x)` in JavaScript sees `{}` and `[]` as TRUTHY; Python's `bool`
    inverts exactly those, silently steering a tool call into a different
    terminal state than the reference on a degenerate-but-legal payload.
    """

    @staticmethod
    def _tool_call(status: str, **fields: object) -> dict[str, object]:
        call: dict[str, object] = {"toolCallId": "tc1", "status": status, **fields}
        return _chat(
            activeTurn={
                "id": "t1",
                "startedAt": "x",
                "message": {},
                "responseParts": [{"kind": "toolCall", "toolCall": call}],
            }
        )

    @staticmethod
    def _call(state: Any) -> Any:
        return state["activeTurn"]["responseParts"][0]["toolCall"]

    def test_an_empty_object_approval_approves(self) -> None:
        """`if (action.approved)` -- `{}` is truthy, so the reference
        transitions to Running; `bool({})` would cancel the call."""
        state = self._tool_call("pending-confirmation", toolInput="{}")
        with frozen_clock():
            out = chat_reducer(
                state,
                {
                    "type": "chat/toolCallConfirmed",
                    "turnId": "t1",
                    "toolCallId": "tc1",
                    "approved": {},
                },
            )
        assert self._call(out)["status"] == "running"

    def test_an_empty_string_contributor_is_ignored_not_stored(self) -> None:
        """`if (!next) return current` -- `''` is falsy, so the reference keeps
        the MCP contributor; `is None` alone would store the empty string and
        disable the `chat/toolCallAuthRequired` path (it requires kind
        `mcp`)."""
        state = self._tool_call("streaming", contributor={"kind": "mcp", "serverId": "srv"})
        with frozen_clock():
            out = chat_reducer(
                state,
                {
                    "type": "chat/toolCallReady",
                    "turnId": "t1",
                    "toolCallId": "tc1",
                    "contributor": "",
                },
            )
        assert self._call(out)["contributor"] == {"kind": "mcp", "serverId": "srv"}

    def test_an_empty_object_success_from_auth_required_still_no_ops(self) -> None:
        """A *successful* completion from `auth-required` is invalid (execution
        never resumed) and upstream ignores it -- with `action.result.success`
        under JS truthiness, so `{}` counts as success there where `bool({})`
        would let the completion through."""
        state = self._tool_call("auth-required")
        with frozen_clock():
            out = chat_reducer(
                state,
                {
                    "type": "chat/toolCallComplete",
                    "turnId": "t1",
                    "toolCallId": "tc1",
                    "result": {"success": {}},
                },
            )
        assert self._call(out)["status"] == "auth-required"


class TestSessionOmitVersusNull:
    """Writes ported from `x: action.x` must DROP the key when the action omits
    it: `undefined` never reaches the wire, while a written-through `None`
    serialises as a schema-invalid `null` that a reference peer reads as
    present. The fixture comparator normalises `null` away, which is why the
    corpus cannot pin any of these.
    """

    def test_mcp_server_state_changed_without_a_channel_clears_it(self) -> None:
        """The actions doc calls `channel` "full-replacement: omit to clear an
        existing channel (typical when leaving Ready)" -- this is the normal
        shutdown path, not an edge case."""
        state = _session(
            customizations=[
                {"type": "mcpServer", "id": "m1", "state": {"kind": "ready"}, "channel": "uri:x"}
            ]
        )
        out = session_reducer(
            state,
            {"type": "session/mcpServerStateChanged", "id": "m1", "state": {"kind": "stopped"}},
        )
        entry = out["customizations"][0]
        assert entry["state"] == {"kind": "stopped"}
        assert "channel" not in entry

    def test_changesets_null_clears_the_catalogue_key(self) -> None:
        """Upstream `action.changesets ? ... : ...` -- null is falsy, so an
        explicit null REMOVES the key (pinned fixture 146's expected state has
        no `changesets`); `assign` would write the null through."""
        state = _session(changesets=[{"resource": "cs:1"}])
        out = session_reducer(state, {"type": "session/changesetsChanged", "changesets": None})
        assert "changesets" not in out

    def test_changesets_empty_array_sets_an_empty_catalogue(self) -> None:
        """`[]` is truthy in JavaScript: it must SET, not clear."""
        state = _session(changesets=[{"resource": "cs:1"}])
        out = session_reducer(state, {"type": "session/changesetsChanged", "changesets": []})
        assert out["changesets"] == []

    def test_creation_failed_without_an_error_stores_no_creation_error(self) -> None:
        out = session_reducer(_session(), {"type": "session/creationFailed"})
        assert out["lifecycle"] == "creationFailed"
        assert "creationError" not in out

    def test_chat_removed_without_a_chat_matches_absence_and_spares_a_null_default(self) -> None:
        """`c.resource === action.chat` with an absent `chat` is `undefined ===
        undefined`: it removes the entry that LACKS `resource` -- and `null ===
        undefined` is false, so an explicit-null `defaultChat` survives.
        Reading the needle with `.get` (None for both) inverts both pairings."""
        state = _session(chats=[{"other": 1}, {"resource": "c1"}], defaultChat=None)
        out = session_reducer(state, {"type": "session/chatRemoved"})
        assert out["chats"] == [{"resource": "c1"}]
        assert "defaultChat" in out, "null defaultChat must survive an absent `chat`"

    def test_customization_removed_without_an_id_spares_a_null_id_entry(self) -> None:
        """`c.id === action.id`: an absent action id (`undefined`) never matches
        an entry carrying an explicit `"id": null`."""
        state = _session(customizations=[{"type": "plugin", "id": None, "children": []}])
        out = session_reducer(state, {"type": "session/customizationRemoved"})
        assert out["customizations"] == state["customizations"]


class TestRootReducerReadsLikeJavaScript:
    """The reference writes `agents: action.agents` (undefined written through,
    dropped at serialization) and spreads `{...action.config}` (undefined and
    null spread to `{}`) -- it never throws. `action["agents"]` raised
    `KeyError`, and `root/configChanged` is client-dispatchable.
    """

    @pytest.mark.parametrize(
        "action",
        [
            {"type": "root/agentsChanged"},
            {"type": "root/activeSessionsChanged"},
            {"type": "root/terminalsChanged"},
            {"type": "root/configChanged"},
        ],
        ids=lambda a: str(a["type"]).split("/")[-1],
    )
    def test_missing_properties_do_not_raise(self, action: dict[str, object]) -> None:
        root_reducer({"agents": [], "config": {"schema": {}, "values": {}}}, action)

    def test_agents_changed_without_agents_clears_the_key(self) -> None:
        out = root_reducer({"agents": [{"id": "a"}]}, {"type": "root/agentsChanged"})
        assert "agents" not in out

    def test_config_changed_without_a_config_merges_nothing(self) -> None:
        """`{...action.config}` on an absent property is `{}`, so the values
        survive unchanged."""
        state = {"agents": [], "config": {"schema": {}, "values": {"k": 1}}}
        out = root_reducer(state, {"type": "root/configChanged"})
        assert out["config"]["values"] == {"k": 1}

    def test_null_values_in_state_do_not_raise(self) -> None:
        """`{...null}` is `{}` in JavaScript where `None.get` is an
        AttributeError and `{**None}` a TypeError."""
        state = {"agents": [], "config": {"schema": {}, "values": None}}
        out = root_reducer(state, {"type": "root/configChanged", "config": {"n": 2}})
        assert out["config"]["values"] == {"n": 2}

    def test_config_changed_with_no_config_in_state_is_a_no_op(self) -> None:
        """Upstream: a host that publishes no config schema is dropped, not
        created."""
        state: dict[str, object] = {"agents": []}
        out = root_reducer(state, {"type": "root/configChanged", "config": {"n": 2}})
        assert out == {"agents": []}
