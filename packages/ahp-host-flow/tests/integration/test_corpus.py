"""The acceptance corpus: four scripts on a scripted fake, and (opt-in) the fan-out one live.

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
from ahp_protocol.ext.flow import RunView, run_view
from ahp_protocol.transport import memory_pair

from ahp_host_flow import FlowProvider, load_config
from ahp_host_flow.config import FlowConfig
from ahp_host_flow.inprocess import FlowRunFailed, _value
from tests.corpus_fakes import ScriptedProvider
from tests.flow_hosting import FlowFixture, boot_flow, final_json
from tests.hosting import notifications, run_turn, state

pytestmark = pytest.mark.node

CORPUS = Path(__file__).resolve().parents[2] / "conformance" / "corpus"
SCRIPT = CORPUS / "fanout.js"
TOPICS = ["alpha", "beta", "gamma"]


async def run_corpus(config: FlowConfig, root: Path) -> tuple[Any, RunView]:
    """Run the fan-out script in a flow session rooted at *root*: its value and run view."""
    host = Host(FlowProvider(config, user_workflows_dir=root / "user"), LoopbackSingleUserPolicy())
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
            async for event in session.prompt("```js\n" + SCRIPT.read_text("utf-8") + "\n```"):
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


@pytest.mark.skipif(
    not os.environ.get("AHP_FLOW_LIVE_CONFIG"),
    reason="set AHP_FLOW_LIVE_CONFIG to a flow.toml to make real model calls",
)
async def test_fanout_corpus_live(tmp_path: Path, node_path: str) -> None:
    config = dataclasses.replace(
        load_config(Path(os.environ["AHP_FLOW_LIVE_CONFIG"])),
        node=node_path,
        state_dir=tmp_path / "state",
    )
    value, view = await asyncio.wait_for(run_corpus(config, tmp_path), 900)
    assert set(value["answers"]) <= set(TOPICS)
    assert len(value["answers"]) == 3
    assert value["best"] in TOPICS
    assert view.status == "ok"
    assert [s.status for s in view.steps] == ["ok"] * 4
