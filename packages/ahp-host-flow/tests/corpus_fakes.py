"""A scripted inner provider for the acceptance corpus; not a test module.

Loaded by import path (`tests.corpus_fakes:ScriptedProvider`), as a flow.toml would. It answers
by the prefix of the prompt, so the corpus scripts run unmodified and no model is called.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar

from ahp_host.provider.base import AgentInfo, AgentSessionContext, TurnSink, UserMessage
from ahp_protocol.ext.structured import OUTPUT_SCHEMA_META_KEY

from ahp_host_flow.catalog import directory_of
from tests.preamble import stripped


def _first_line(text: str, prefix: str) -> str:
    """The text after *prefix* up to the next newline, stripped."""
    return text.removeprefix(prefix).split("\n", 1)[0].strip()


def _quoted(text: str) -> str:
    """The text between the first pair of double quotes."""
    return text.split('"', 2)[1]


def _line(text: str, prefix: str) -> str:
    """The rest of the line that starts with *prefix*."""
    for line in text.splitlines():
        if line.startswith(prefix):
            return line.removeprefix(prefix)
    raise ValueError(f"no line starting {prefix!r}")


def _plan(text: str, cwd: Path) -> Any:
    return {
        "steps": [
            {"title": "add parser", "files": ["src/parser.txt"]},
            {"title": "wire cli", "files": ["src/cli.txt", "README.txt"]},
        ]
    }


def _build(text: str, cwd: Path) -> Any:
    title = _quoted(text)
    files = _line(text, "Files: ").split(", ")
    for name in files:
        target = cwd / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f"built: {title}\n", encoding="utf-8")
    return {"step": title, "changed": files}


def _review_step(text: str, cwd: Path) -> Any:
    return {"step": _quoted(text), "verdict": "approve", "notes": "looks fine"}


def _review_change(text: str, cwd: Path) -> Any:
    match = re.match(r"Review change: (\d+)/(\d+)", text)
    assert match is not None
    approved, total = match.groups()
    return {"approved": approved == total, "summary": f"{approved} of {total} steps approved"}


def _answer(text: str, cwd: Path) -> Any:
    topic = _line(text, "Topic: ").strip()
    confidence = {"alpha": 0.4, "beta": 0.9, "gamma": 0.6}.get(topic, 0.5)
    return {"topic": topic, "answer": f"{topic} is fine", "confidence": confidence}


def _reduce(text: str, cwd: Path) -> Any:
    answers = json.loads(_line(text, "Answers: "))
    best = max(answers, key=lambda a: a["confidence"])["topic"]
    return {"best": best, "summary": f"{len(answers)} answers, best {best}"}


def _collect(text: str, cwd: Path) -> Any:
    return {
        "failures": [
            "tests/test_math.py::test_add",
            "tests/test_io.py::test_read",
            "tests/test_io.py::test_write",
        ]
    }


def _diagnose(text: str, cwd: Path) -> Any:
    test = _first_line(text, "Diagnose:")
    if "test_io" in test:
        return {"test": test, "kind": "product", "cause": "file handle left open"}
    return {"test": test, "kind": "test", "cause": "wrong expected sum"}


def _report(text: str, cwd: Path) -> Any:
    diagnoses = json.loads(_line(text, "Diagnoses: "))
    groups: dict[str, list[str]] = {}
    for diagnosis in diagnoses:
        groups.setdefault(diagnosis["cause"], []).append(diagnosis["test"])
    return {"groups": [{"cause": cause, "tests": tests} for cause, tests in groups.items()]}


def _lint(text: str, cwd: Path) -> Any:
    path = _first_line(text, "Lint:")
    violations = (
        [{"rule": "no debug prints", "note": "remove the print() on line 3"}]
        if path.endswith(".py")
        else []
    )
    return {"path": path, "violations": violations}


def _commit_message(text: str, cwd: Path) -> Any:
    count = _line(text, "Violations: ").strip()
    return {
        "subject": "fix(app): tidy the pending change",
        "body": f"Lint found {count} violation(s).",
    }


REPLIES: tuple[tuple[str, Callable[[str, Path], Any]], ...] = (
    ("Plan:", _plan),
    ("Build:", _build),
    ("Review step:", _review_step),
    ("Review change:", _review_change),
    ("Answer:", _answer),
    ("Reduce:", _reduce),
    ("List failing tests:", _collect),
    ("Diagnose:", _diagnose),
    ("Report:", _report),
    ("Lint:", _lint),
    ("Commit message:", _commit_message),
)


@dataclass
class ScriptedSession:
    context: AgentSessionContext

    async def send_user_message(self, message: UserMessage, sink: TurnSink) -> None:
        message = stripped(message)
        raw = message.raw if isinstance(message.raw, Mapping) else {}
        meta = raw.get("_meta")
        schema = meta.get(OUTPUT_SCHEMA_META_KEY) if isinstance(meta, Mapping) else None
        ScriptedProvider.calls.append((message.text, schema))
        cwd = directory_of(self.context.working_directories[0])
        assert cwd is not None
        reply = next((fn for prefix, fn in REPLIES if message.text.startswith(prefix)), None)
        if reply is None:
            await sink.text_delta("no scripted reply")
        else:
            value = reply(message.text, cwd)
            if isinstance(value, dict | list):
                await sink.text_delta("```json\n" + json.dumps(value) + "\n```")
            else:
                await sink.text_delta(str(value))
        await sink.usage(input_tokens=1, output_tokens=1)

    async def cancel(self, reason: str | None = None) -> None:
        pass

    async def aclose(self) -> None:
        pass


class ScriptedProvider:
    """Replies by prompt prefix and records every (prompt, output schema) it is sent."""

    calls: ClassVar[list[tuple[str, Mapping[str, Any] | None]]] = []

    def __init__(self, *, provider_id: str = "scripted") -> None:
        self.provider_id = provider_id

    @classmethod
    def reset(cls) -> None:
        cls.calls.clear()

    @property
    def agent(self) -> AgentInfo:
        return AgentInfo(
            provider=self.provider_id,
            display_name="Scripted",
            description="Replies by prompt prefix.",
            models=(),
        )

    async def create_session(self, context: AgentSessionContext) -> ScriptedSession:
        return ScriptedSession(context)
