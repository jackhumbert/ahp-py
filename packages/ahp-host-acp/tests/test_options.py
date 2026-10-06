"""ACP options and modes <-> AHP session config, commands, plans, the catalogue."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ahp_host.provider.base import CompletionRequest

from ahp_host_acp import commands, plan
from ahp_host_acp.catalogue import Catalogue
from ahp_host_acp.options import MODE_PROPERTY, AgentOptions

SELECT = {
    "id": "mode",
    "name": "Mode",
    "category": "mode",
    "type": "select",
    "currentValue": "ask",
    "options": [
        {
            "group": "g",
            "name": "G",
            "options": [{"value": "ask", "name": "Ask", "description": "d"}],
        },
        {"value": "code", "name": "Code"},
    ],
}
MODEL = {
    "id": "model",
    "name": "Model",
    "category": "model",
    "type": "select",
    "currentValue": "b",
    "options": [{"value": "a", "name": "A"}, {"value": "b", "name": "B"}],
}
TOGGLE = {"id": "yolo", "name": "Auto-approve", "type": "boolean", "currentValue": False}
MODES = {
    "currentModeId": "ask",
    "availableModes": [{"id": "ask", "name": "Ask"}, {"id": "code", "name": "Code"}],
}


def test_select_and_boolean_options_become_mutable_properties() -> None:
    options = AgentOptions([SELECT, MODEL, TOGGLE])
    props = options.properties(with_model=True, pinned={})
    assert list(props) == ["mode", "model", "yolo"]  # the agent's order
    assert props["mode"] == {
        "title": "Mode",
        "type": "string",
        "enum": ["ask", "code"],  # groups flattened
        "enumLabels": ["Ask", "Code"],
        "enumDescriptions": ["d", ""],
        "default": "ask",
        "sessionMutable": True,
    }
    assert props["yolo"] == {
        "title": "Auto-approve",
        "type": "boolean",
        "default": False,
        "sessionMutable": True,
    }


def test_the_model_option_is_left_to_a_model_picker() -> None:
    options = AgentOptions([SELECT, MODEL])
    assert "model" not in options.properties(with_model=False, pinned={})
    assert options.model_option is not None
    assert options.model_option.id == "model"


def test_a_pinned_option_is_read_only_with_the_configured_default() -> None:
    props = AgentOptions([SELECT, TOGGLE]).properties(
        with_model=True, pinned={"mode": "code", "yolo": "true"}
    )
    assert props["mode"]["default"] == "code"
    assert props["mode"]["readOnly"] is True
    assert "sessionMutable" not in props["mode"]
    assert props["yolo"]["default"] is True  # "true" from a config file, as a boolean


def test_legacy_modes_are_used_only_without_config_options() -> None:
    assert [o.id for o in AgentOptions(None, MODES).options] == [MODE_PROPERTY]
    # "Clients that support config options SHOULD use configOptions exclusively"
    assert [o.id for o in AgentOptions([TOGGLE], MODES).options] == ["yolo"]
    # ...but an empty list offers nothing to use exclusively.
    assert [o.id for o in AgentOptions([], MODES).options] == [MODE_PROPERTY]


def test_requests_match_the_acp_schema() -> None:
    options = AgentOptions([SELECT, TOGGLE])
    assert options.request_for("mode", "code") == (
        "session/set_config_option",
        {"configId": "mode", "value": "code"},
    )
    assert options.request_for("yolo", "true") == (
        "session/set_config_option",
        {"configId": "yolo", "type": "boolean", "value": True},
    )
    # An option the agent never reported (a config-file value) is sent as given.
    assert options.request_for("thought_level", "low")[1] == {
        "configId": "thought_level",
        "value": "low",
    }
    legacy = AgentOptions(None, MODES)
    assert legacy.request_for(MODE_PROPERTY, "code") == ("session/set_mode", {"modeId": "code"})


def test_updates_replace_the_options_and_track_the_mode() -> None:
    options = AgentOptions([SELECT])
    changed = dict(SELECT, currentValue="code")
    assert options.note_update(
        {"sessionUpdate": "config_option_update", "configOptions": [changed]}
    )
    assert options.value_of("mode") == "code"
    legacy = AgentOptions(None, MODES)
    assert legacy.note_update({"sessionUpdate": "current_mode_update", "currentModeId": "code"})
    assert legacy.value_of(MODE_PROPERTY) == "code"
    assert not legacy.note_update({"sessionUpdate": "current_mode_update", "currentModeId": "code"})


def _completion(text: str, offset: int | None = None) -> CompletionRequest:
    """At the end of *text* by default -- in UTF-16 code units, as a client counts."""
    end = len(text.encode("utf-16-le")) // 2
    return CompletionRequest(
        kind="userMessage", chat="c", text=text, offset=end if offset is None else offset
    )


def test_slash_commands_complete_at_the_start_of_the_message() -> None:
    known = commands.parse_commands(
        [
            {"name": "web", "description": "Search", "input": {"hint": "query"}},
            {"name": "help", "description": "Help"},
            {"description": "no name"},
        ]
    )
    assert [c.name for c in known] == ["web", "help"]
    assert known[0].hint == "query"
    items = commands.complete(known, _completion("  /we"))
    assert [i.to_wire() for i in items] == [
        {
            "insertText": "/web ",
            "attachment": {
                "type": "simple",
                "label": "/web",
                "displayKind": "command",
                "_meta": {"acpCommand": "web"},
            },
            "rangeStart": 2,
            "rangeEnd": 5,
        }
    ]
    assert len(commands.complete(known, _completion("/"))) == 2
    assert commands.complete(known, _completion("ask /web")) == []  # not at the start
    assert commands.complete(known, _completion("/web foo")) == []  # writing the input


def test_completion_ranges_are_utf16() -> None:
    known = commands.parse_commands([{"name": "\U0001f600go", "description": ""}])
    # An emoji is two UTF-16 code units, and one Python character.
    (item,) = commands.complete(known, _completion("/\U0001f600"))
    assert (item.range_start, item.range_end) == (0, 3)
    # An ideographic space is whitespace, and one unit.
    (item,) = commands.complete(known, _completion("\u3000/"))
    assert (item.range_start, item.range_end) == (1, 2)
    # The cursor, not the end of the text, ends the range.
    (item,) = commands.complete(known, _completion("/\U0001f600go", offset=3))
    assert item.range_end == 3


def test_plan_reads_as_a_task_list() -> None:
    entries = plan.parse_plan(
        [
            {"content": "Read", "priority": "high", "status": "completed"},
            {"content": "Fix", "priority": "low", "status": "in_progress"},
            {"content": "Ship", "priority": "medium", "status": "pending"},
            {"priority": "low"},
        ]
    )
    assert plan.markdown(entries) == (
        "- [x] Read (high priority)\n- [ ] **Fix** (in progress)\n- [ ] Ship"
    )
    assert plan.progress(entries) == "1 of 3 done"
    assert plan.current(entries) == "Fix"
    assert plan.markdown(()) == "The plan is empty."


def test_catalogue_keeps_what_a_new_session_reported(tmp_path: Path) -> None:
    path = tmp_path / "agent.json"
    catalogue = Catalogue(path)
    catalogue.remember_new_session({"sessionId": "s", "configOptions": [SELECT, MODEL]})
    catalogue.remember_commands([{"name": "web", "description": "Search"}])
    catalogue.remember_context_window("b", 200_000)
    again = Catalogue(path)
    assert [o.id for o in again.options.options] == ["mode", "model"]
    assert [c.name for c in again.commands] == ["web"]
    assert again.context_windows == {"b": 200_000}
    # The agent's starting model first, so a picker's default does not switch it.
    assert again.models() == [("b", "B"), ("a", "A")]


def test_catalogue_takes_the_removed_session_model_api(tmp_path: Path) -> None:
    catalogue = Catalogue()
    catalogue.remember_new_session(
        {
            "sessionId": "s",
            "models": {
                "currentModelId": "y",
                "availableModels": [{"modelId": "x", "name": "X"}, {"modelId": "y"}],
            },
        }
    )
    assert catalogue.models() == [("y", "y"), ("x", "X")]


def test_a_broken_catalogue_file_is_ignored(tmp_path: Path) -> None:
    path = tmp_path / "agent.json"
    path.write_text("{not json", encoding="utf-8")
    assert Catalogue(path).options.options == ()
    path.write_text(json.dumps({"version": 99, "configOptions": [SELECT]}), encoding="utf-8")
    assert Catalogue(path).options.options == ()


def test_catalogue_writes_only_on_change(tmp_path: Path) -> None:
    path = tmp_path / "agent.json"
    catalogue = Catalogue(path)
    result: dict[str, Any] = {"sessionId": "s", "configOptions": [TOGGLE]}
    catalogue.remember_new_session(result)
    assert json.loads(path.read_text(encoding="utf-8"))["configOptions"] == [TOGGLE]
    path.unlink()
    catalogue.remember_new_session(result)
    catalogue.remember_commands([])
    assert not path.exists()  # nothing new to say
