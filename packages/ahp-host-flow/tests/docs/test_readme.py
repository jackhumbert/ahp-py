import re
import tomllib
from pathlib import Path

from ahp_host_flow.config import parse_config

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
