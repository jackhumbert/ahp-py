"""``Chat.flow_view`` / ``Session.flow_view``: the mirror folded by ``run_view``."""

from __future__ import annotations

from typing import Any, cast

import ahp_protocol.ext.flow as flow
from ahp_protocol.ext.flow import EMPTY_RUN, run_view

from ahp_client.api.client import Chat, Client, Session


class _Mirror:
    def __init__(self, states: dict[str, Any]) -> None:
        self._states = states

    def state(self, uri: str) -> Any:
        return self._states.get(uri)


class _Stub:
    def __init__(self, states: dict[str, Any]) -> None:
        self.mirror = _Mirror(states)


def _note(descriptor: dict[str, Any]) -> dict[str, Any]:
    return {"kind": "systemNotification", "content": "text", "_meta": flow.meta(descriptor)}


def _run_state() -> dict[str, Any]:
    started = flow.step_started(
        ordinal=0, label="s0", phase=None, chat="chat:/w0", model="m", provider="echo"
    )
    finished = flow.step_finished(status="ok", result="x", input_tokens=1, output_tokens=2)
    parts = [
        _note(flow.run_started("r1", "demo")),
        {
            "kind": "toolCall",
            "toolCall": {
                "toolCallId": "c0",
                "status": "completed",
                "_meta": flow.meta(flow.step(started, finished)),
            },
        },
        _note(flow.run_finished("ok")),
    ]
    return {"turns": [{"id": "t1", "state": "complete", "responseParts": parts}]}


def _session(states: dict[str, Any]) -> Session:
    return Session(cast(Client, _Stub(states)), "s:/1", "flow", owned=False)


def test_chat_flow_view_folds_the_mirror() -> None:
    state = _run_state()
    chat = Chat(cast(Client, _Stub({"chat:/run": state})), cast(Session, None), "chat:/run")
    view = chat.flow_view()
    assert view == run_view(state)
    assert view.status == "ok"
    assert view.steps[0].result == "x"


def test_chat_flow_view_without_a_run_is_empty() -> None:
    chat = Chat(cast(Client, _Stub({"chat:/run": {}})), cast(Session, None), "chat:/run")
    assert chat.flow_view() == EMPTY_RUN


def test_session_flow_view_uses_the_default_chat() -> None:
    state = _run_state()
    session = _session({"s:/1": {"defaultChat": "chat:/run"}, "chat:/run": state})
    assert session.flow_view() == run_view(state)
    bare = _session({"s:/1": {}})
    assert bare.flow_view() == EMPTY_RUN


def test_session_flow_view_names_a_chat() -> None:
    state = _run_state()
    session = _session({"s:/1": {"defaultChat": "chat:/other"}, "chat:/run": state})
    assert session.flow_view("chat:/run") == run_view(state)
    assert session.flow_view() == EMPTY_RUN
