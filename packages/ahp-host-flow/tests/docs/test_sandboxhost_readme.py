import ast
import re
import tomllib
from pathlib import Path

import pytest

from ahp_host_flow.config import parse_config

ROOT = Path(__file__).resolve().parents[2]


def _section() -> str:
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    start = text.index("## Running on sandboxed hosts")
    end = text.find("\n## ", start + 1)
    return text[start : end if end != -1 else len(text)]


def _launcher() -> ast.Module:
    py_block = re.search(r"```python\n(.*?)```", _section(), re.DOTALL)
    assert py_block is not None
    return ast.parse(py_block.group(1))


def test_sandboxhost_readme_example_parses(tmp_path: Path) -> None:
    """The README's sandboxed-host flow.toml parses and its launcher is valid Python."""
    section = _section()
    toml_block = re.search(r"```toml\n(.*?)```", section, re.DOTALL)
    assert toml_block is not None
    config = parse_config(tomllib.loads(toml_block.group(1)), base=tmp_path)
    provider = config.providers["sandbox"]
    assert provider.kind == "remote"
    assert provider.launch == "my_launchers.container:launch"
    assert provider.per_call is True
    tree = _launcher()
    assert any(isinstance(n, ast.AsyncFunctionDef) and n.name == "launch" for n in tree.body)
    for word in ("env_allow", "worktree", "opts.x.cwd"):
        assert word in section


def test_sandboxhost_readme_launcher_imports_resolve() -> None:
    """Every import the launcher makes is a real module and name."""
    imports: list[ast.stmt] = [
        n for n in _launcher().body if isinstance(n, ast.Import | ast.ImportFrom)
    ]
    assert imports
    exec(compile(ast.Module(body=imports, type_ignores=[]), "<README launcher>", "exec"), {})


def _container_command() -> list[ast.expr]:
    """The arguments of the launcher's `create_subprocess_exec` call."""
    for node in ast.walk(_launcher()):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "create_subprocess_exec"
        ):
            return list(node.args)
    raise AssertionError("the launcher starts no subprocess")


def test_sandboxhost_readme_container_flags_exist() -> None:
    """The ACP host's flags the launcher passes are real, and the port stays on loopback."""
    acp_main = pytest.importorskip("ahp_host_acp.__main__")
    args = _container_command()
    words = [a.value if isinstance(a, ast.Constant) else None for a in args]
    assert "--network" not in words
    publish = args[words.index("-p") + 1]
    # f"127.0.0.1:{port}:{INNER_PORT}": published on the outside's loopback only.
    assert isinstance(publish, ast.JoinedStr)
    first = publish.values[0]
    assert isinstance(first, ast.Constant)
    assert str(first.value).startswith("127.0.0.1:")
    start = words.index("ahp_host_acp") + 1
    # A computed value (the port) stands in as a number; everything else is as written.
    argv = [str(w) if w is not None else "4322" for w in words[start:]]
    assert {"--bind", "--token-file", "--root", "--command", "--port"} <= set(argv)
    parsed = acp_main._parse_args(argv)
    assert parsed.bind == "0.0.0.0"
    assert parsed.token_file == Path("/run/secrets/ahp-token")
