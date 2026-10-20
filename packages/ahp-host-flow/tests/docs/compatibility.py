"""Readers for the family's version pins: pyproject files and `__version__` lines.

Plain files only (tomllib, re, pathlib): nothing here imports the packages.
"""

import ast
import json
import re
import tomllib
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[4]

# The release order (docs/plan.md §8 M18): each package after the ones it pins.
RELEASE_ORDER = (
    "ahp-protocol",
    "ahp-host",
    "ahp-client",
    "ahp-acp",
    "ahp-gateway",
    "ahp-host-claude",
    "ahp-host-acp",
    "ahp-host-flow",
)

_REQUIREMENT = re.compile(r"^\s*([A-Za-z0-9_.-]+)\s*(\[[^\]]*\])?\s*(.*?)\s*$")
_VERSION = re.compile(r'^__version__ = "([^"]+)"$', re.MULTILINE)


def package_dirs() -> list[str]:
    return sorted(p.parent.name for p in (REPO / "packages").glob("*/pyproject.toml"))


def pyproject(name: str) -> dict[str, Any]:
    return tomllib.loads((REPO / "packages" / name / "pyproject.toml").read_text(encoding="utf-8"))


def version(name: str) -> str:
    path = pyproject(name)["tool"]["hatch"]["version"]["path"]
    text = (REPO / "packages" / name / path).read_text(encoding="utf-8")
    found = _VERSION.search(text)
    assert found, f"{name}: no __version__ line in {path}"
    return found.group(1)


def requirements(name: str) -> list[tuple[str, str, str]]:
    data = pyproject(name)
    groups: list[tuple[str, list[str]]] = [("runtime", data["project"].get("dependencies", []))]
    for extra, entries in data["project"].get("optional-dependencies", {}).items():
        groups.append((f"extra:{extra}", entries))
    groups.append(("dev", data.get("dependency-groups", {}).get("dev", [])))
    found = []
    for group, entries in groups:
        for entry in entries:
            match = _REQUIREMENT.match(entry.split(";")[0])
            assert match, f"{name}: unreadable requirement {entry!r}"
            found.append((group, match.group(1), match.group(3)))
    return found


def sibling_requirements(name: str) -> list[tuple[str, str, str]]:
    return [r for r in requirements(name) if r[1] in RELEASE_ORDER]


HEADER = (
    "| Tag | Package | Version | ahp-protocol | AHP versions offered | Spec commit | Tested with |"
)
SEPARATOR = "|---|---|---|---|---|---|---|"


def supported_versions() -> tuple[str, ...]:
    path = REPO / "packages" / "ahp-protocol" / "src" / "ahp_protocol" / "versions.py"
    for node in ast.parse(path.read_text(encoding="utf-8")).body:
        if isinstance(node, ast.Assign):
            names = [t.id for t in node.targets if isinstance(t, ast.Name)]
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names = [node.target.id]
        else:
            continue
        if "DEFAULT_SUPPORTED_VERSIONS" in names and node.value is not None:
            return tuple(ast.literal_eval(node.value))
    raise AssertionError("no DEFAULT_SUPPORTED_VERSIONS in versions.py")


def spec_commit() -> str:
    pin = REPO / "packages" / "ahp-protocol" / "vendor" / "upstream" / "PIN.json"
    return str(json.loads(pin.read_text(encoding="utf-8"))["specCommit"])[:8]


def _runtime_siblings(name: str) -> list[str]:
    return [dist for group, dist, _spec in sibling_requirements(name) if group == "runtime"]


def reaches_protocol(name: str) -> bool:
    return name == "ahp-protocol" or any(reaches_protocol(d) for d in _runtime_siblings(name))


def _bare_range(name: str) -> str:
    """The ahp-protocol specifier `name` accepts, directly or through its first path."""
    for group, dist, spec in sibling_requirements(name):
        if group == "runtime" and dist == "ahp-protocol":
            return spec
    for dist in _runtime_siblings(name):
        if reaches_protocol(dist):
            return _bare_range(dist)
    return "—"


def protocol_range(name: str) -> str:
    if name == "ahp-protocol":
        return "—"
    for group, dist, spec in sibling_requirements(name):
        if group == "runtime" and dist == "ahp-protocol":
            return f"`{spec}`"
    for dist in _runtime_siblings(name):
        if reaches_protocol(dist):
            return f"`{_bare_range(dist)}` (via {dist})"
    return "—"


def render_rows() -> list[str]:
    rows = []
    for name in RELEASE_ORDER:
        ver = version(name)
        reaches = reaches_protocol(name)
        offered = ", ".join(supported_versions()) if reaches else "—"
        commit = f"`{spec_commit()}`" if reaches else "—"
        siblings = {dist for _group, dist, _spec in sibling_requirements(name)}
        tested = ", ".join(f"{d} {version(d)}" for d in RELEASE_ORDER if d in siblings) or "—"
        cells = [f"`{name}/v{ver}`", name, ver, protocol_range(name), offered, commit, tested]
        rows.append("| " + " | ".join(cells) + " |")
    return rows


def render_table() -> str:
    return "\n".join([HEADER, SEPARATOR, *render_rows()])


if __name__ == "__main__":
    print(render_table())
