import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_format_doc_names_every_conformance_script() -> None:
    text = (ROOT / "docs" / "format.md").read_text(encoding="utf-8")
    stems = sorted(p.stem for p in (ROOT / "conformance").glob("*.js"))
    assert stems
    missing = [s for s in stems if f"conformance/{s}.js" not in text]
    assert not missing
    twins = sorted(p.stem for p in (ROOT / "conformance").glob("*.py"))
    assert twins
    missing = [s for s in twins if f"conformance/{s}.py" not in text]
    assert not missing


def test_format_doc_names_every_extension_script() -> None:
    text = (ROOT / "docs" / "format.md").read_text(encoding="utf-8")
    names = sorted(
        p.name
        for pattern in ("*.js", "*.py")
        for p in (ROOT / "conformance" / "extensions").glob(pattern)
    )
    assert names
    missing = [n for n in names if f"conformance/extensions/{n}" not in text]
    assert not missing
    assert "conformance/shim/dual_target.js" in text


def test_format_doc_plan_status() -> None:
    # The status line names what is done and what is next; it must exist and
    # name a next milestone that the milestone table lists.
    text = (ROOT / "docs" / "plan.md").read_text(encoding="utf-8")
    status = re.search(r"^Status: (.*)$", text, re.MULTILINE)
    assert status is not None
    if "No milestone is next" in status.group(1):
        # At the start of a line, so the status line's own mention does not count.
        assert re.search(r"^\*\*Open after M10\.\*\*", text, re.MULTILINE) is not None
        return
    nxt = re.search(r"\b(M\d+[a-z]?)\b[^.]*\bis next\b", status.group(1))
    assert nxt is not None
    assert f"| {nxt.group(1)} |" in text


def test_format_doc_m14_result() -> None:
    text = (ROOT / "docs" / "plan.md").read_text(encoding="utf-8")
    assert re.search(r"^\*\*M14 result\.\*\*", text, re.MULTILINE)
    repo = ROOT.parents[1]
    start = re.search(r"^\*\*M14 result\.\*\*", text, re.MULTILINE)
    assert start is not None
    rest = text[start.start() :]
    end = re.search(r"^\*\*M15", rest, re.MULTILINE)
    assert end is not None
    paragraph = rest[: end.start()]
    paths = re.findall(r"`(packages/[^`]+)`", paragraph)
    assert len(paths) >= 10
    missing = [p for p in paths if not (repo / p).is_file()]
    assert not missing


def test_format_doc_m15_result() -> None:
    text = (ROOT / "docs" / "plan.md").read_text(encoding="utf-8")
    repo = ROOT.parents[1]
    start = re.search(r"^\*\*M15 result\.\*\*", text, re.MULTILINE)
    assert start is not None
    rest = text[start.start() :]
    end = re.search(r"^\*\*M16", rest, re.MULTILINE)
    assert end is not None
    paragraph = rest[: end.start()]
    paths = re.findall(r"`(packages/[^`]+)`", paragraph)
    assert len(paths) >= 10
    missing = [p for p in paths if not (repo / p).is_file()]
    assert not missing
