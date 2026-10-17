"""Parsing the message that starts a run."""

from __future__ import annotations

from ahp_host_flow.commands import (
    BadRequest,
    Help,
    StartRun,
    help_text,
    parse_message,
    unknown_text,
)
from ahp_host_flow.sandbox import NO_ARGS


def test_slash_command_with_args() -> None:
    assert parse_message('/greet {"who": "bob"}') == StartRun("greet", None, None, {"who": "bob"})


def test_slash_command_without_args() -> None:
    result = parse_message("/greet")
    assert result == StartRun("greet", None, None, NO_ARGS)
    assert isinstance(result, StartRun)
    assert result.args is NO_ARGS


def test_source_prefix() -> None:
    assert parse_message("/user:greet [1]") == StartRun("greet", "user", None, [1])


def test_workflow_command() -> None:
    assert parse_message("/workflow greet 3") == StartRun("greet", None, None, 3)


def test_workflow_named_workflow_parses() -> None:
    assert parse_message("/workflow workflow") == StartRun("workflow", None, None, NO_ARGS)
    assert parse_message('/workflow workflow {"a": 1}') == StartRun(
        "workflow", None, None, {"a": 1}
    )


def test_workflow_command_needs_a_name() -> None:
    assert parse_message("/workflow") == BadRequest("Usage: /workflow <name> [json args]")


def test_bad_names() -> None:
    assert parse_message("/client:greet") == BadRequest("Not a workflow name: 'client:greet'")
    assert parse_message("/a.b") == BadRequest("Not a workflow name: 'a.b'")


def test_bad_json_args() -> None:
    result = parse_message("/greet {bad")
    assert isinstance(result, BadRequest)
    assert result.message.startswith("Arguments must be JSON")


def test_fenced_script() -> None:
    assert parse_message("```js\nreturn 1\n```") == StartRun(None, None, "return 1", NO_ARGS)


def test_fenced_script_with_args() -> None:
    assert parse_message('```js\nreturn 1\n```\n{"n": 2}') == StartRun(
        None, None, "return 1", {"n": 2}
    )


def test_javascript_tag() -> None:
    assert parse_message("```javascript\nreturn 1\n```") == StartRun(
        None, None, "return 1", NO_ARGS
    )


def test_anything_else_is_help() -> None:
    assert parse_message("hello") == Help()


def test_help_text() -> None:
    assert help_text([]).endswith("No workflows found.")
    assert help_text(["a", "b"]).endswith("Workflows: /a, /b")


def test_unknown_text() -> None:
    assert unknown_text("x", ["a"]).startswith("Unknown workflow: /x.")
    assert unknown_text("x", ["a"]).endswith("Workflows: /a")
