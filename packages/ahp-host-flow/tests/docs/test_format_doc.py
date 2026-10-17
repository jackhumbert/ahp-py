from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_format_doc_names_every_conformance_script() -> None:
    text = (ROOT / "docs" / "format.md").read_text(encoding="utf-8")
    stems = sorted(p.stem for p in (ROOT / "conformance").glob("*.js"))
    assert stems
    missing = [s for s in stems if f"conformance/{s}.js" not in text]
    assert not missing


def test_format_doc_plan_status() -> None:
    assert "M2 done" in (ROOT / "docs" / "plan.md").read_text(encoding="utf-8")
