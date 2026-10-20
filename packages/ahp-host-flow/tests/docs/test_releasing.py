import re
import shutil
import subprocess

import pytest

from .compatibility import RELEASE_ORDER, REPO, supported_versions, version

# A release section's heading: the date is the tag's day, set by the maintainer
# when tagging, so until then it reads `unreleased`.
_RELEASE_HEADING = r"^## \[{v}\] - (unreleased|\d{{4}}-\d{{2}}-\d{{2}})$"


def _release_awk() -> str:
    """The awk program release.yml runs to extract a section's notes."""
    workflow = (REPO / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
    found = re.search(r"awk -v v=\"## \[\$VERSION\]\" '\n(.*?)\n\s*' ", workflow, re.DOTALL)
    assert found, "release.yml: the CHANGELOG extractor moved"
    return found.group(1)


def _extract_like_release_yml(text: str, ver: str) -> str:
    """release.yml's awk, line for line: from the first line starting with
    `## [<ver>]` (exclusive) up to the next `## [` heading."""
    out: list[str] = []
    inside = False
    for line in text.splitlines():
        if not inside and line.startswith(f"## [{ver}]"):
            inside = True
            continue
        if inside and line.startswith("## ["):
            break
        if inside:
            out.append(line)
    return "\n".join(out)


def _changelog(name: str) -> str:
    return (REPO / "packages" / name / "CHANGELOG.md").read_text(encoding="utf-8")


def test_releasing_smoke_scripts_exist() -> None:
    for name in RELEASE_ORDER:
        path = REPO / "packages" / name / "scripts" / "smoke_wheel.py"
        assert path.is_file(), f"{name}: no scripts/smoke_wheel.py"
        text = path.read_text(encoding="utf-8")
        compile(text, str(path), "exec")
        assert '"site-packages"' in text, f"{name}: does not check for an installed copy"
        assert f"import {name.replace('-', '_')}" in text, f"{name}: does not import itself"
        assert "import pytest" not in text, f"{name}: the smoke script must not need pytest"


def test_releasing_smoke_ci_runs_every_script() -> None:
    ci = (REPO / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert "packages/*/scripts/smoke_wheel.py" in ci
    lines = [line for line in ci.splitlines() if 'python -c "import ' in line]
    for name in RELEASE_ORDER:
        module = name.replace("-", "_")
        assert any(module in line for line in lines), f"{name}: not in the import line"


def test_releasing_docs_every_package() -> None:
    for name in RELEASE_ORDER:
        path = REPO / "packages" / name / "RELEASING.md"
        assert path.is_file(), f"{name}: no RELEASING.md"
        text = path.read_text(encoding="utf-8")
        assert f"{name}/v" in text, f"{name}: no tag form"
        assert "scripts/smoke_wheel.py" in text, f"{name}: no smoke step"
        assert "done by the maintainer" in text, f"{name}: no maintainer sentence"


def test_releasing_docs_release_plan() -> None:
    text = (REPO / "packages" / "ahp-host" / "docs" / "release-plan.md").read_text(encoding="utf-8")
    assert "PyPI trusted publishing" not in text
    assert "dependency order" in text


def test_releasing_changelog_section_matches_version() -> None:
    for name in RELEASE_ORDER:
        text = _changelog(name)
        headings = re.findall(r"^## \[.*$", text, re.MULTILINE)
        assert headings, name
        assert headings[0] == "## [Unreleased]", name
        ver = version(name)
        heading = re.compile(_RELEASE_HEADING.format(v=re.escape(ver)))
        mine = [h for h in headings if h.startswith(f"## [{ver}]")]
        assert len(mine) == 1, f"{name}: {len(mine)} {ver} headings"
        assert heading.match(mine[0]), f"{name}: {mine[0]!r} is not `## [{ver}] - unreleased|date`"


def test_releasing_extractor_finds_each_section() -> None:
    awk = _release_awk()
    # The Python port above is what the other checks trust; pin it to the workflow's text.
    assert "index($0, v) == 1 { inside = 1; next }" in awk
    assert r"inside && /^## \[/ { exit }" in awk
    for name in RELEASE_ORDER:
        notes = _extract_like_release_yml(_changelog(name), version(name))
        assert notes.strip(), f"{name}: release.yml would find an empty {version(name)} section"


def test_releasing_extractor_runs_under_awk() -> None:
    awk = shutil.which("awk")
    if awk is None:
        pytest.skip("no awk on this machine")
    program = _release_awk()
    for name in RELEASE_ORDER:
        ver = version(name)
        path = REPO / "packages" / name / "CHANGELOG.md"
        done = subprocess.run(
            [awk, "-v", f"v=## [{ver}]", program, str(path)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=True,
        )
        assert done.stdout.strip(), f"{name}: awk found no {ver} section"
        assert done.stdout.strip() == _extract_like_release_yml(_changelog(name), ver).strip()


def test_releasing_host_changelog_names_offered_versions() -> None:
    text = _changelog("ahp-host")
    section = _extract_like_release_yml(text, version("ahp-host"))
    for offered in supported_versions():
        assert offered in section, f"ahp-host {version('ahp-host')}: does not name {offered}"
    assert "no `v0.1.0` tag exists yet" not in section
