"""The subagent preamble helpers: what a setting stands for and how it joins a prompt."""

from __future__ import annotations

from ahp_host_flow.engine import DEFAULT_PREAMBLE, preamble_text, with_preamble


def test_preamble_text_settings() -> None:
    assert preamble_text(True) == DEFAULT_PREAMBLE
    assert preamble_text(False) is None
    assert preamble_text("Be terse.") == "Be terse."


def test_with_preamble_joins_with_a_blank_line() -> None:
    assert with_preamble("hi", "P") == "P\n\nhi"
    assert with_preamble("hi", None) == "hi"
    assert with_preamble("hi", "") == "hi"
