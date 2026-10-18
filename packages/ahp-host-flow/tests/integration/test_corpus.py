"""The acceptance corpus: four scripts on a scripted fake, and (opt-in) three run live.

`AHP_FLOW_LIVE_CONFIG` names a flow.toml; the live run makes real model calls.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import os
from pathlib import Path
from typing import Any

import ahp_client
import pytest
from ahp_client import TurnCancelled, TurnCompleted, TurnFailed
from ahp_host import Host, LoopbackSingleUserPolicy
from ahp_host.node import Roots
from ahp_protocol.ext.flow import RunView, run_view
from ahp_protocol.transport import memory_pair

from ahp_host_flow import FlowProvider, load_config
from ahp_host_flow.config import FlowConfig, Routing, RoutingRule
from ahp_host_flow.inprocess import FlowRunFailed, _value
from tests.corpus_fakes import ScriptedProvider
from tests.flow_hosting import FlowFixture, boot_flow, final_json
from tests.hosting import notifications, run_turn, state

pytestmark = pytest.mark.node

CORPUS = Path(__file__).resolve().parents[2] / "conformance" / "corpus"
SCRIPT = CORPUS / "fanout.js"
TOPICS = ["alpha", "beta", "gamma"]


NO_ARGS = object()


async def run_corpus(
    config: FlowConfig, root: Path, script: Path = SCRIPT, args: object = NO_ARGS
) -> tuple[Any, RunView]:
    """Run *script* (with *args* after it) in a flow session rooted at *root*: value and view."""
    # The roots cover the working directory, as `inprocess._run_message` does: a `from_config`
    # inner provider gets only these, and would refuse a session folder outside them.
    provider = FlowProvider(
        config, user_workflows_dir=root / "user", roots=Roots.single(root.resolve())
    )
    host = Host(provider, LoopbackSingleUserPolicy())
    client_t, server_t = memory_pair()
    serving = asyncio.create_task(host.serve(server_t))
    try:
        async with (
            ahp_client.connect(transport=client_t) as client,
            await client.create_session(
                provider="flow", working_directories=[root.as_uri()]
            ) as session,
        ):
            text = ""
            prompt = "```js\n" + script.read_text("utf-8") + "\n```"
            if args is not NO_ARGS:
                prompt += "\n" + json.dumps(args)
            # A run's main chat is quiet while a step runs, so ahp-client's 300 s idle timer
            # would fail any step slower than that; each caller bounds the run itself.
            async for event in session.prompt(prompt, idle_timeout=None):
                if isinstance(event, TurnCompleted):
                    text = event.text
                elif isinstance(event, TurnFailed):
                    raise FlowRunFailed(event.reason)
                elif isinstance(event, TurnCancelled):
                    raise FlowRunFailed("run cancelled")
            value = _value(text)
            await session.chat()
            return value, session.flow_view()
    finally:
        serving.cancel()
        await host.aclose()


BODY = f"""[providers.scripted]
load = "tests.corpus_fakes:ScriptedProvider"

[workflows]
dirs = [{json.dumps(str(CORPUS))}]
"""

REVIEW_VERDICTS = [
    {"step": "add parser", "verdict": "approve", "notes": "looks fine"},
    {"step": "wire cli", "verdict": "approve", "notes": "looks fine"},
]
ANSWERS = [
    {"topic": "alpha", "answer": "alpha is fine", "confidence": 0.4},
    {"topic": "beta", "answer": "beta is fine", "confidence": 0.9},
    {"topic": "gamma", "answer": "gamma is fine", "confidence": 0.6},
]


@pytest.fixture(autouse=True)
def _reset_scripted() -> None:
    ScriptedProvider.reset()


async def run_named(fx: FlowFixture, turn_id: str, command: str) -> tuple[Any, RunView, list[str]]:
    """Run *command* on the booted host: its value, run view and log lines."""
    turn = await run_turn(fx.wire, fx.chat, turn_id, command)
    return final_json(turn), run_view(state(fx.host, fx.chat), turn_id=turn_id), notifications(turn)


def prompts_starting(prefix: str) -> list[str]:
    return [text for text, _ in ScriptedProvider.calls if text.startswith(prefix)]


async def test_build_to_spec_corpus_on_fakes(tmp_path: Path, node_path: str) -> None:
    fx = await boot_flow(tmp_path, node_path, BODY)
    try:
        value, view, notes = await run_named(fx, "t1", '/build_to_spec {"spec": "docs/spec.md"}')
        assert value == {
            "ok": True,
            "steps": ["add parser", "wire cli"],
            "verdicts": ["approve", "approve"],
            "summary": "2 of 2 steps approved",
        }
        assert len(ScriptedProvider.calls) == 6
        assert all(schema is not None for _, schema in ScriptedProvider.calls)
        (final,) = prompts_starting("Review change:")
        assert json.dumps(REVIEW_VERDICTS, separators=(",", ":")) in final
        assert view.status == "ok"
        assert [p.title for p in view.phases] == ["Plan", "Build", "Review"]
        assert "2 steps planned" in notes
        assert "2/2 steps approved" in notes
        assert (fx.workspace / "src/parser.txt").read_text("utf-8") == "built: add parser\n"
        assert (fx.workspace / "src/cli.txt").read_text("utf-8") == "built: wire cli\n"
        assert (fx.workspace / "README.txt").read_text("utf-8") == "built: wire cli\n"
    finally:
        await fx.shut()


async def test_fanout_corpus_on_fakes(tmp_path: Path, node_path: str) -> None:
    fx = await boot_flow(tmp_path, node_path, BODY)
    try:
        value, view, notes = await run_named(fx, "t1", "/fanout")
        assert value == {
            "answers": ["alpha", "beta", "gamma"],
            "best": "beta",
            "summary": "3 answers, best beta",
        }
        assert len(ScriptedProvider.calls) == 4
        (reduce_prompt,) = prompts_starting("Reduce:")
        answers_line = next(
            line for line in reduce_prompt.splitlines() if line.startswith("Answers: ")
        )
        assert json.loads(answers_line.removeprefix("Answers: ")) == ANSWERS
        assert [s.label for s in view.steps] == ["fan-alpha", "fan-beta", "fan-gamma", "reduce"]
        assert [s.status for s in view.steps] == ["ok"] * 4
        assert [p.title for p in view.phases] == ["Fan out", "Reduce"]
        assert "3/3 answers" in notes
    finally:
        await fx.shut()


async def test_triage_corpus_on_fakes(tmp_path: Path, node_path: str) -> None:
    fx = await boot_flow(tmp_path, node_path, BODY)
    try:
        value, view, notes = await run_named(fx, "t1", "/triage")
        assert value == {
            "failures": 3,
            "kinds": ["test", "product", "product"],
            "groups": [
                {"cause": "wrong expected sum", "tests": ["tests/test_math.py::test_add"]},
                {
                    "cause": "file handle left open",
                    "tests": ["tests/test_io.py::test_read", "tests/test_io.py::test_write"],
                },
            ],
        }
        assert len(ScriptedProvider.calls) == 5
        assert [p.title for p in view.phases] == ["Collect", "Diagnose", "Report"]
        assert "3 failing tests" in notes
        value, _, _ = await run_named(
            fx, "t2", '/triage {"failures": ["tests/test_math.py::test_add"]}'
        )
        assert value["failures"] == 1
        assert value["kinds"] == ["test"]
        assert len(ScriptedProvider.calls) == 7
    finally:
        await fx.shut()


async def test_commit_prep_corpus_on_fakes(tmp_path: Path, node_path: str) -> None:
    fx = await boot_flow(tmp_path, node_path, BODY)
    try:
        value, view, notes = await run_named(fx, "t1", "/commit_prep")
        assert value == {
            "clean": False,
            "violations": [
                {
                    "path": "src/app.py",
                    "rule": "no debug prints",
                    "note": "remove the print() on line 3",
                }
            ],
            "message": "fix(app): tidy the pending change\n\nLint found 1 violation(s).",
        }
        assert len(ScriptedProvider.calls) == 3
        assert [s.label for s in view.steps] == ["lint src/app.py", "lint README.md", "message"]
        assert "1 violation(s)" in notes
    finally:
        await fx.shut()


live = pytest.mark.skipif(
    not os.environ.get("AHP_FLOW_LIVE_CONFIG"),
    reason="set AHP_FLOW_LIVE_CONFIG to a flow.toml to make real model calls",
)


def live_config(tmp_path: Path, node_path: str) -> FlowConfig:
    return dataclasses.replace(
        load_config(Path(os.environ["AHP_FLOW_LIVE_CONFIG"])),
        node=node_path,
        state_dir=tmp_path / "state",
    )


@live
async def test_fanout_corpus_live(tmp_path: Path, node_path: str) -> None:
    config = live_config(tmp_path, node_path)
    question = "Is this the first letter of the Greek alphabet?"
    value, view = await asyncio.wait_for(
        run_corpus(config, tmp_path, args={"question": question, "topics": TOPICS}), 900
    )
    assert set(value["answers"]) <= set(TOPICS)
    assert len(value["answers"]) == 3
    # The mechanics, not the model's judgment: the reduce step returned its schema.
    assert isinstance(value["best"], str)
    assert value["best"]
    assert isinstance(value["summary"], str)
    assert view.status == "ok"
    assert [s.status for s in view.steps] == ["ok"] * 4


@live
async def test_triage_corpus_live(tmp_path: Path, node_path: str) -> None:
    config = live_config(tmp_path, node_path)
    # Something to read: with an empty folder the agent reaches for a shell, and under
    # `acceptEdits` that waits on an approval no one in this test answers.
    (tmp_path / "mathlib.py").write_text("def add(a, b):\n    return a - b\n", "utf-8")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_math.py").write_text(
        "from mathlib import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n", "utf-8"
    )
    value, view = await asyncio.wait_for(
        run_corpus(
            config,
            tmp_path,
            CORPUS / "triage.js",
            {"failures": ["tests/test_math.py::test_add"]},
        ),
        900,
    )
    assert value["failures"] == 1
    assert len(value["kinds"]) == 1
    assert value["kinds"][0] in ("product", "test", "flaky")
    assert view.status == "ok"


REVIEW_SCRIPT = """phase('Review')
return await agent('Review this function: def add(a, b): return a - b. Is it correct?', {
  label: 'review',
  schema: {
    type: 'object',
    required: ['verdict', 'notes'],
    properties: {
      verdict: {type: 'string', enum: ['approve', 'request_changes']},
      notes: {type: 'string'},
    },
  },
})
"""


@live
async def test_review_routed_to_glm_live(tmp_path: Path, node_path: str) -> None:
    config = live_config(tmp_path, node_path)
    if "glm" not in config.providers:
        pytest.skip("the live config has no glm provider")
    config = dataclasses.replace(
        config,
        routing=Routing(
            config.routing.default, (RoutingRule("glm", phase="Review"), *config.routing.rules)
        ),
    )
    script = tmp_path / "review.js"
    script.write_text(REVIEW_SCRIPT, "utf-8")
    value, view = await asyncio.wait_for(run_corpus(config, tmp_path, script), 900)
    assert value["verdict"] in ("approve", "request_changes")
    assert isinstance(value["notes"], str)
    assert view.status == "ok"
    (step,) = view.steps
    assert step.provider == "glm"
