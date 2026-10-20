import re

from ahp_host.provider.base import (
    FollowsActiveClients,
    ResumesTurns,
    ReturnsStructuredOutput,
    SteersTurns,
    TruncatesHistory,
)

from .support_matrix import REPO, ROWS, detected, render_matrix, resumes_sessions


def test_supportmatrix_readme_matches() -> None:
    text = (REPO / "README.md").read_text(encoding="utf-8")
    assert render_matrix() in text


# The detected cells, pinned by hand: a class that gains or loses a capability fails here, and
# the README row is then updated on purpose rather than following the code silently.
EXPECTED_DETECTED = {
    "echo": ("yes", "native", "no", "no", "yes", "yes"),
    "ACP (any agent)": ("no", "native", "yes", "no", "yes", "no"),
    "ACP (reference agent)": ("no", "native", "yes", "no", "yes", "no"),
    "Claude": ("yes", "native", "yes", "no", "yes", "no"),
    "flow": ("yes", "prompted", "yes", "yes", "yes", "no"),
}
DETECTED_COLUMNS = (
    "Steering",
    "Structured output",
    "Client tools",
    "Turn resume",
    "Session resume",
    "History truncation",
)


def test_supportmatrix_detected_columns_match_the_pinned_table() -> None:
    assert {name for name, _s, _p in ROWS} == set(EXPECTED_DETECTED)
    for name, session, provider in ROWS:
        found = detected(session, provider)
        assert tuple(found[c] for c in DETECTED_COLUMNS) == EXPECTED_DETECTED[name], name


def test_supportmatrix_detection_reads_the_protocols() -> None:
    for _name, session, provider in ROWS:
        found = detected(session, provider)
        assert found["Steering"] == ("yes" if issubclass(session, SteersTurns) else "no")
        assert found["Structured output"] == (
            "native" if issubclass(session, ReturnsStructuredOutput) else "prompted"
        )
        assert found["Client tools"] == (
            "yes" if issubclass(session, FollowsActiveClients) else "no"
        )
        assert found["Turn resume"] == ("yes" if issubclass(session, ResumesTurns) else "no")
        assert found["Session resume"] == ("yes" if resumes_sessions(provider) else "no")
        assert found["History truncation"] == (
            "yes" if issubclass(session, TruncatesHistory) else "no"
        )


def test_supportmatrix_reference_session_resume_is_host_side() -> None:
    row = next(line for line in render_matrix().splitlines() if "ACP (reference agent)" in line)
    assert "host-side only" in row


def test_supportmatrix_acp_agents_listed() -> None:
    text = (REPO / "README.md").read_text(encoding="utf-8")
    section = text[text.index("### ACP agents known to work") :]
    for name in ("opencode", "OpenClaw", "reference agent"):
        assert name in section
    assert re.search(r"as of \d{4}-\d{2}-\d{2}", section)
