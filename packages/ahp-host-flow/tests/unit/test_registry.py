"""Registry and workflow reference resolution (no node)."""

from __future__ import annotations

from pathlib import Path

import pytest

from ahp_host_flow.engine import (
    ClientWorkflow,
    Registry,
    RunOptions,
    WorkflowDir,
    WorkflowRefError,
    resolve_workflow,
)


def _write(path: Path, text: str = "x") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_registry_first_match_wins(tmp_path: Path) -> None:
    _write(tmp_path / "one" / "a.js", "first")
    _write(tmp_path / "two" / "a.js", "second")
    reg = Registry([tmp_path / "one", tmp_path / "two"])
    got = resolve_workflow("a", registry=reg, run_root=None)
    assert got.source == "first"
    assert got.filename.endswith("a.js")
    assert Path(got.filename).parent.name == "one"


def test_registry_names(tmp_path: Path) -> None:
    _write(tmp_path / "one" / "b.js")
    _write(tmp_path / "one" / "a.js")
    _write(tmp_path / "one" / "bad.name.js")
    _write(tmp_path / "two" / "a.js")
    _write(tmp_path / "two" / "c.js")
    reg = Registry([tmp_path / "one", tmp_path / "missing", tmp_path / "two"])
    assert reg.names() == ["a", "b", "c"]


@pytest.mark.parametrize("name", ["", "../x", "a/b", "a\\b", ".hidden", "x.js", "-x"])
def test_registry_invalid_names(tmp_path: Path, name: str) -> None:
    reg = Registry([tmp_path])
    with pytest.raises(WorkflowRefError, match="invalid workflow name"):
        resolve_workflow(name, registry=reg, run_root=None)


def test_registry_unknown_name(tmp_path: Path) -> None:
    with pytest.raises(WorkflowRefError) as info:
        resolve_workflow("nope", registry=Registry([tmp_path]), run_root=None)
    assert str(info.value) == "unknown workflow: 'nope'"


def test_registry_unknown_without_registry() -> None:
    with pytest.raises(WorkflowRefError) as info:
        resolve_workflow("nope", registry=None, run_root=None)
    assert str(info.value) == "unknown workflow: 'nope'"


def test_registry_script_path_relative(tmp_path: Path) -> None:
    _write(tmp_path / "sub" / "w.js", "rel")
    got = resolve_workflow({"scriptPath": "sub/w.js"}, registry=None, run_root=tmp_path)
    assert got.source == "rel"
    assert got.filename == str((tmp_path / "sub" / "w.js").resolve())


def test_registry_script_path_absolute_inside_root(tmp_path: Path) -> None:
    path = _write(tmp_path / "sub" / "w.js", "abs")
    got = resolve_workflow({"scriptPath": str(path)}, registry=None, run_root=tmp_path)
    assert got.source == "abs"


def test_registry_script_path_outside_root(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    _write(tmp_path / "w.js")
    with pytest.raises(WorkflowRefError) as info:
        resolve_workflow({"scriptPath": "../w.js"}, registry=None, run_root=root)
    assert str(info.value) == "workflow path is outside the run root: '../w.js'"


def test_registry_script_path_needs_root() -> None:
    with pytest.raises(WorkflowRefError, match="needs a run root"):
        resolve_workflow({"scriptPath": "w.js"}, registry=None, run_root=None)


def test_registry_unreadable(tmp_path: Path) -> None:
    with pytest.raises(WorkflowRefError, match="cannot read workflow"):
        resolve_workflow({"scriptPath": "missing.js"}, registry=None, run_root=tmp_path)
    (tmp_path / "bin.js").write_bytes(b"\xff\xfe\x00")
    with pytest.raises(WorkflowRefError, match="cannot read workflow"):
        resolve_workflow({"scriptPath": "bin.js"}, registry=None, run_root=tmp_path)


@pytest.mark.parametrize("ref", [3, None, {}, {"scriptPath": 3}, ["a"]])
def test_registry_bad_ref(ref: object) -> None:
    with pytest.raises(WorkflowRefError) as info:
        resolve_workflow(ref, registry=None, run_root=None)
    assert str(info.value) == "workflow() needs a name or {scriptPath}"


def test_registry_finds_py(tmp_path: Path) -> None:
    path = _write(tmp_path / "x.py", "py")
    assert Registry([tmp_path]).find("x") == path
    assert resolve_workflow("x", registry=Registry([tmp_path]), run_root=None).source == "py"


def test_registry_js_wins_tie(tmp_path: Path) -> None:
    _write(tmp_path / "x.py", "py")
    js = _write(tmp_path / "x.js", "js")
    assert Registry([tmp_path]).find("x") == js


def test_registry_earlier_dir_wins_across_languages(tmp_path: Path) -> None:
    py = _write(tmp_path / "d1" / "x.py")
    _write(tmp_path / "d2" / "x.js")
    assert Registry([tmp_path / "d1", tmp_path / "d2"]).find("x") == py


def test_registry_python_dirs_filter(tmp_path: Path) -> None:
    d1, d2 = tmp_path / "d1", tmp_path / "d2"
    _write(d1 / "x.py")
    js = _write(d2 / "x.js")
    assert Registry([d1, d2], python_dirs=(d2,)).find("x") == js
    only = Registry([d1], python_dirs=(d2,))
    assert only.find("x") is None
    with pytest.raises(WorkflowRefError) as info:
        resolve_workflow("x", registry=only, run_root=None)
    assert str(info.value) == "unknown workflow: 'x'"


def test_registry_names_include_py(tmp_path: Path) -> None:
    _write(tmp_path / "a.js")
    _write(tmp_path / "b.py")
    _write(tmp_path / "bad.name.py")
    assert Registry([tmp_path]).names() == ["a", "b"]
    assert Registry([tmp_path], python_dirs=()).names() == ["a"]


def test_registry_script_path_py_refused(tmp_path: Path) -> None:
    _write(tmp_path / "wf" / "x.py")
    with pytest.raises(WorkflowRefError) as info:
        resolve_workflow(
            {"scriptPath": "wf/x.py"}, registry=None, run_root=tmp_path, python_dirs=()
        )
    assert str(info.value) == "Python workflows are not allowed from 'wf/x.py'"


def test_registry_script_path_py_allowed_in_dir(tmp_path: Path) -> None:
    _write(tmp_path / "wf" / "x.py", "py")
    got = resolve_workflow(
        {"scriptPath": "wf/x.py"},
        registry=None,
        run_root=tmp_path,
        python_dirs=(tmp_path / "wf",),
    )
    assert got.source == "py"


def test_run_options_python_dirs_validated(tmp_path: Path) -> None:
    assert RunOptions().python_dirs is None
    assert RunOptions(python_dirs=(tmp_path,)).python_dirs == (tmp_path,)
    for bad in ([tmp_path], (str(tmp_path),)):
        with pytest.raises(ValueError, match="python_dirs must be a tuple of paths or None"):
            RunOptions(python_dirs=bad)  # type: ignore[arg-type]


def _qualified(tmp_path: Path) -> Registry:
    _write(tmp_path / "ws" / "x.js", "workspace")
    _write(tmp_path / "host" / "x.js", "host")
    _write(tmp_path / "plain" / "y.js", "plain")
    return Registry(
        [
            WorkflowDir("workspace", tmp_path / "ws"),
            WorkflowDir("host", tmp_path / "host"),
            tmp_path / "plain",
        ]
    )


def test_registry_qualified_picks_labeled_dir(tmp_path: Path) -> None:
    reg = _qualified(tmp_path)
    assert resolve_workflow("host:x", registry=reg, run_root=None).source == "host"
    assert resolve_workflow("workspace:x", registry=reg, run_root=None).source == "workspace"
    assert resolve_workflow("x", registry=reg, run_root=None).source == "workspace"
    assert reg.dirs == (tmp_path / "ws", tmp_path / "host", tmp_path / "plain")


def test_registry_qualified_unlabeled_dirs_serve_bare_names(tmp_path: Path) -> None:
    reg = _qualified(tmp_path)
    assert reg.find("y") == tmp_path / "plain" / "y.js"
    for source in ("client", "workspace", "user", "host"):
        assert reg.find(f"{source}:y") is None


def _client(name: str, source: str, filename: str | None = None) -> ClientWorkflow:
    return ClientWorkflow(name, source, filename or f"virtual://c/workflows/{name}.js")


def test_registry_qualified_bare_name_prefers_client(tmp_path: Path) -> None:
    _write(tmp_path / "x.js", "ws")
    reg = Registry([WorkflowDir("workspace", tmp_path)], client=(_client("x", "client"),))
    got = resolve_workflow("x", registry=reg, run_root=None)
    assert (got.source, got.filename) == ("client", "virtual://c/workflows/x.js")
    assert resolve_workflow("workspace:x", registry=reg, run_root=None).source == "ws"
    assert reg.names() == ["x"]


def test_registry_qualified_client_reaches_only_client_entries(tmp_path: Path) -> None:
    _write(tmp_path / "x.js", "ws")
    reg = Registry([WorkflowDir("workspace", tmp_path)], client=(_client("c", "client"),))
    assert resolve_workflow("client:c", registry=reg, run_root=None).source == "client"
    with pytest.raises(WorkflowRefError) as info:
        resolve_workflow("client:x", registry=reg, run_root=None)
    assert str(info.value) == "unknown workflow: 'client:x'"
    with pytest.raises(WorkflowRefError):
        resolve_workflow("workspace:c", registry=reg, run_root=None)


def test_registry_qualified_drops_python_client_entry() -> None:
    reg = Registry([], client=(_client("p", "return 1", "virtual://c/workflows/p.py"),))
    assert reg.find_client("p") is None
    assert reg.names() == []


def test_registry_qualified_unknown_source_message(tmp_path: Path) -> None:
    for registry in (Registry([tmp_path]), None):
        with pytest.raises(WorkflowRefError) as info:
            resolve_workflow("bogus:x", registry=registry, run_root=None)
        assert str(info.value) == (
            "unknown workflow source 'bogus' in 'bogus:x': use one of client, workspace, user, host"
        )


def test_registry_qualified_invalid_name(tmp_path: Path) -> None:
    for registry in (Registry([tmp_path]), None):
        with pytest.raises(WorkflowRefError) as info:
            resolve_workflow("host:../x", registry=registry, run_root=None)
        assert str(info.value) == "invalid workflow name: 'host:../x'"
