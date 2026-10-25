"""Parsing the message that starts a run."""

from __future__ import annotations

from ahp_host_flow.commands import (
    BadRequest,
    Help,
    StartRun,
    help_text,
    parse_message,
    parse_ref,
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
    # 'client' is a source since step 4 (see test_client_qualified_name); use an unknown prefix.
    assert parse_message("/nosuch:greet") == BadRequest("Not a workflow name: 'nosuch:greet'")
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


def test_client_qualified_name() -> None:
    assert parse_message('/client:nightly {"a": 1}') == StartRun(
        "nightly", "client", None, {"a": 1}
    )


def test_typedstart_parse_ref() -> None:
    assert parse_ref("host:fanout") == ("host", "fanout")
    assert parse_ref("fanout") == (None, "fanout")
    assert parse_ref("bogus:x") == BadRequest("Not a workflow name: 'bogus:x'")
    assert isinstance(parse_ref("a b"), BadRequest)
    assert isinstance(parse_ref(""), BadRequest)


def test_inline_script_keeps_a_body_line_starting_with_a_fence() -> None:
    body = "const md = `\n```\nnot the end\n```\n`\nreturn md"
    assert parse_message(f"```js\n{body}\n```") == StartRun(None, None, body, NO_ARGS)
    assert parse_message(f"```js\n{body}\n``` [1]") == StartRun(None, None, body, [1])


def test_args_refuse_nan_and_infinity() -> None:
    for text in ("NaN", "Infinity", "-Infinity", '{"x": NaN}', "[1, Infinity]"):
        result = parse_message(f"/greet {text}")
        assert isinstance(result, BadRequest), text
        assert result.message.startswith("Arguments must be JSON")
        assert isinstance(parse_message(f"```js\nreturn 1\n``` {text}"), BadRequest)
