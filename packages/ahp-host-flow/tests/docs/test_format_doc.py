import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_format_doc_names_every_conformance_script() -> None:
    text = (ROOT / "docs" / "format.md").read_text(encoding="utf-8")
    stems = sorted(p.stem for p in (ROOT / "conformance").glob("*.js"))
    assert stems
    missing = [s for s in stems if f"conformance/{s}.js" not in text]
    assert not missing


def test_format_doc_plan_status() -> None:
    # The status line names what is done and what is next; it must exist and
    # name a next milestone that the milestone table lists.
    text = (ROOT / "docs" / "plan.md").read_text(encoding="utf-8")
    status = re.search(r"^Status: (.*)$", text, re.MULTILINE)
    assert status is not None
    nxt = re.search(r"\b(M\d+)\b[^.]*\bis next\b", status.group(1))
    assert nxt is not None
    assert f"| {nxt.group(1)} |" in text
