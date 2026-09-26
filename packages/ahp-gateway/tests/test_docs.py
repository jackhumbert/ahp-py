"""Prose that contradicts a checkable fact must fail.

The README and AGENTS.md make claims about the dependency set and the
layering. This module is the second half of the pair: when the code moves and
the prose does not, a test fails rather than a reader.
"""

import pathlib
import re

import ahp_gateway

DOCS = pathlib.Path(__file__).parent.parent / "docs"
README = pathlib.Path(__file__).parent.parent / "README.md"


def test_readme_states_the_three_siblings() -> None:
    text = README.read_text(encoding="utf-8")
    for sibling in ("ahp-protocol", "ahp-host", "ahp-client"):
        assert sibling in text, f"README does not name {sibling}"


def test_version_in_docs_matches_the_package() -> None:
    # Any doc that pins the version in prose (the install snippet) must agree
    # with the one place the version is written.
    for path in (README, DOCS / "plan.md"):
        if not path.exists():
            continue
        for match in re.findall(r"ahp-gateway==([\d.]+)", path.read_text(encoding="utf-8")):
            assert match == ahp_gateway.__version__.split(".dev")[0], (
                f"{path.name} pins {match}, package says {ahp_gateway.__version__}"
            )
