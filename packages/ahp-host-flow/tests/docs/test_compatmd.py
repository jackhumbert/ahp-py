import re
import tomllib
from typing import Any

from .compatibility import (
    HEADER,
    RELEASE_ORDER,
    REPO,
    SEPARATOR,
    package_dirs,
    render_rows,
    sibling_requirements,
    version,
)


def test_compatmd_sibling_pins_cover_every_package() -> None:
    assert set(package_dirs()) == set(RELEASE_ORDER)


def test_compatmd_sibling_pins_admit_the_siblings() -> None:
    for name in RELEASE_ORDER:
        assert re.fullmatch(r"\d+\.\d+\.\d+", version(name)), f"{name}: {version(name)}"
    for name in RELEASE_ORDER:
        for _group, dist, spec in sibling_requirements(name):
            match = re.fullmatch(r"~=(\d+)\.(\d+)\.(\d+)", spec)
            assert match, f"{name}: {dist}{spec} is not a ~=X.Y.Z pin"
            major, minor, patch = (int(g) for g in match.groups())
            have = [int(p) for p in version(dist).split(".")]
            assert (major, minor) == (have[0], have[1]), f"{name}: {dist}{spec} vs {have}"
            assert patch <= have[2], f"{name}: {dist}{spec} exceeds {version(dist)}"


def test_compatmd_sibling_pins_flow() -> None:
    pins = sibling_requirements("ahp-host-flow")
    for expected in (
        ("runtime", "ahp-client", "~=0.1.0"),
        ("extra:claude", "ahp-host-claude", "~=0.1.0"),
        ("extra:acp", "ahp-host-acp", "~=0.1.0"),
        ("dev", "ahp-gateway", "~=0.1.0"),
    ):
        assert expected in pins


def test_compatmd_sibling_pins_uv_sources() -> None:
    sources = pyproject_root()["tool"]["uv"]["sources"]
    assert set(sources) == set(RELEASE_ORDER)
    assert all(value == {"workspace": True} for value in sources.values())


def pyproject_root() -> dict[str, Any]:
    return tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))


def test_compatmd_table_matches() -> None:
    lines = (REPO / "COMPATIBILITY.md").read_text(encoding="utf-8").splitlines()
    assert HEADER in lines
    assert SEPARATOR in lines
    rows = render_rows()
    for row in rows:
        assert row in lines, f"stale or missing row: {row}"
    tags = [row.split("|")[1].strip() for row in rows]
    for line in lines:
        if line.startswith("| `") and line not in rows:
            assert line.split("|")[1].strip() not in tags, f"stale current row: {line}"


def test_compatmd_table_reads_the_sources() -> None:
    from ahp_protocol.versions import DEFAULT_SUPPORTED_VERSIONS

    cells = {
        row.split("|")[2].strip(): [c.strip() for c in row.split("|")[1:-1]]
        for row in render_rows()
    }
    assert cells["ahp-protocol"][4] == ", ".join(DEFAULT_SUPPORTED_VERSIONS)
    assert cells["ahp-acp"][3:6] == ["—", "—", "—"]
