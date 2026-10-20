import asyncio
import re
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from ahp_host_flow.config import parse_config
from ahp_host_flow.engine import ConfirmOutcome, VerbCall, VerbContext

ROOT = Path(__file__).resolve().parents[2]


def test_readme_fleet_and_remote_example_parses(tmp_path: Path) -> None:
    """The README's fleet/remote flow.toml is accepted as written (with its `claude` default)."""
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    section = text[text.index("## Fleet and remote providers") :]
    block = re.search(r"```toml\n(.*?)```", section, re.DOTALL)
    assert block is not None
    data = tomllib.loads('[providers.claude]\nload = "m:C"\n' + block.group(1))
    config = parse_config(data, base=tmp_path)
    kinds = {name: spec.kind for name, spec in config.providers.items()}
    assert kinds == {"claude": "inprocess", "build-box": "fleet", "mac": "remote"}
    assert config.providers["build-box"].reply_timeout == 900.0
    assert config.providers["mac"].idle_timeout == 600.0


def test_readme_operations_example_parses(tmp_path: Path) -> None:
    """The README's Operations flow.toml is accepted as written."""
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    section = text[text.index("## Operations") :]
    block = re.search(r"```toml\n(.*?)```", section, re.DOTALL)
    assert block is not None
    config = parse_config(tomllib.loads(block.group(1)), base=tmp_path)
    assert config.workflows.sources == ("client", "workspace", "user", "host")
    assert config.providers["claude"].interactive is True


def test_readme_verbs_and_resume_examples_parse(tmp_path: Path) -> None:
    """The README's Custom verbs and Resume keys blocks parse into the config."""
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    data: dict[str, object] = {}
    for heading in ("## Custom verbs", "## Resume keys"):
        section = text[text.index(heading) :]
        block = re.search(r"```toml\n(.*?)```", section, re.DOTALL)
        assert block is not None
        data.update(tomllib.loads('[providers.claude]\nload = "m:C"\n' + block.group(1)))
    config = parse_config(data, base=tmp_path)
    assert config.verbs == {
        "tool": "my_pkg.verbs:tool",
        "render": "my_pkg.verbs:render",
        "approve": "my_pkg.verbs:approve",
    }
    assert config.resume.key == "model"


def test_approvalrules_readme_example_parses(tmp_path: Path) -> None:
    """The README's Approval rules block parses into two rules and a policy."""
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    section = text[text.index("### Approval rules") :]
    block = re.search(r"```toml\n(.*?)```", section, re.DOTALL)
    assert block is not None
    data = tomllib.loads('[providers.claude]\nload = "m:C"\n' + block.group(1))
    config = parse_config(data, base=tmp_path)
    assert len(config.approvals.rules) == 2
    assert config.policy is not None


def _section(text: str, heading: str) -> str:
    start = text.index(heading)
    end = text.find(chr(10) + "## ", start + 1)
    return text[start : end if end != -1 else len(text)]


def test_readme_client_workflows_line_is_current() -> None:
    """The README says scripts reach client workflows, not that they cannot."""
    text = " ".join((ROOT / "README.md").read_text(encoding="utf-8").split())
    assert "inside a script reaches client workflows too (`.js` only)" in text
    assert "does not reach client workflows" not in text


def test_readme_proxy_section() -> None:
    """The proxy section names the options, the header token and the ahp-host anchor."""
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    section = _section(text, "## Behind a proxy")
    for needle in (
        "--ping-interval",
        "--max-size",
        "Authorization: Bearer",
        "#embedding-it-behind-a-proxy",
    ):
        assert needle in section


def test_readme_typed_starts_section() -> None:
    """The typed-starts section names the error, the schema key and the client call."""
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    section = _section(text, "## Typed workflow starts")
    for needle in ("flow.badArgs", "argsSchema", "start_workflow"):
        assert needle in section


def test_readme_approve_person_example_runs() -> None:
    """The README's approve(person) handler runs: it re-asks until the person answers."""
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    section = text[text.index("An `approve(person)` handler") :]
    block = re.search(r"```python\n(.*?)```", section, re.DOTALL)
    assert block is not None
    scope: dict[str, Any] = {}
    exec(block.group(1), scope)
    approve = scope["approve"]

    def call_with(answers: list[str | None], asked: list[tuple[str, Any]]) -> VerbCall:
        async def confirm(summary: str, options: Mapping[str, Any] | None = None) -> ConfirmOutcome:
            asked.append((summary, options))
            return ConfirmOutcome(True, answers.pop(0))

        ctx = VerbContext(confirm=confirm)
        return VerbCall("approve", ("alice",), 0, "verb-x-0", None, "r", ctx)

    asked: list[tuple[str, Any]] = []
    result = asyncio.run(approve(call_with(["bob", "alice"], asked)))
    assert result == {"approved": True}
    assert len(asked) == 2
    assert all(options is None for _, options in asked)

    with pytest.raises(RuntimeError, match="no principal"):
        asyncio.run(approve(call_with([None], [])))


def test_flowlimits_readme_example_parses(tmp_path: Path) -> None:
    """The README's Limits block parses: all eight [limits] keys and a provider call_timeout."""
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    section = text[text.index("### Limits") :]
    block = re.search(r"```toml\n(.*?)```", section, re.DOTALL)
    assert block is not None
    config = parse_config(tomllib.loads(block.group(1)), base=tmp_path)
    limits = config.limits
    assert (limits.budget, limits.input_budget, limits.cost_budget) == (200000, 2000000, 5.0)
    assert (limits.wall_budget, limits.timeout, limits.concurrency) == (1800.0, 3600.0, 4)
    assert (limits.reserve, limits.count_cache) == ("mean", True)
    assert config.providers["claude"].call_timeout == 600.0


def test_fallchain_readme_example_parses(tmp_path: Path) -> None:
    """The README's Fallback block parses: a provider fallback and a rule fallback."""
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    section = text[text.index("### Fallback") :]
    block = re.search(r"```toml\n(.*?)```", section, re.DOTALL)
    assert block is not None
    config = parse_config(tomllib.loads(block.group(1)), base=tmp_path)
    assert config.providers["claude"].fallback == ("codex",)
    assert config.routing.rules[0].fallback == ("codex", "local")
