"""Credential masking: the builtin shapes, benign prose, the stream masker, journal values."""

from __future__ import annotations

from pathlib import Path

import pytest

from ahp_host_flow.engine import (
    MASK,
    NO_REDACTION,
    AgentRecord,
    JournalWriter,
    Redactor,
    RunHeader,
    run_dir,
)

# Built at runtime so no literal token sits in the source.
AWS = "AKIA" + "TESTONLY" * 2
GH = "ghp_" + "x1" * 18
GH_PAT = "github_pat_" + "Ab9" * 8
GLPAT = "glpat-" + "a" * 20
SLACK = "xoxb-" + "1234567890-abcdef"
PEM = "-----BEGIN RSA PRIVATE KEY-----\n" + "MII" + "A" * 60 + "\n-----END RSA PRIVATE KEY-----"
BEARER_SECRET = "abcdefgh01234567890"
KV_SECRET = "s3cr3tvalue"

SHAPES = {
    "aws": AWS,
    "github": GH,
    "github-pat": GH_PAT,
    "gitlab": GLPAT,
    "slack": SLACK,
    "pem": PEM,
}
LINES = [
    *SHAPES.values(),
    f"Authorization: Bearer {BEARER_SECRET}",
    f"api_key={KV_SECRET}",
    f'{{"password": "{KV_SECRET}"}}',
    "plain prose line",
]
TEXT = "\n".join(LINES) + "\n"
SECRETS = [AWS, GH, GH_PAT, GLPAT, SLACK, "MII" + "A" * 60, BEARER_SECRET, KV_SECRET]


@pytest.mark.parametrize("secret", list(SHAPES.values()))
def test_masking_builtin_shapes(secret: str) -> None:
    out = Redactor().mask(f"before {secret} after")
    assert out == f"before {MASK} after"
    assert secret not in out


def test_masking_key_value_keeps_key() -> None:
    r = Redactor()
    assert r.mask("api_key=" + "abcd1234") == "api_key=" + MASK
    assert r.mask('"password": "hunter2hunter2"') == '"password": "' + MASK + '"'


def test_masking_bearer_token() -> None:
    assert Redactor().mask("Bearer " + BEARER_SECRET) == "Bearer " + MASK


@pytest.mark.parametrize(
    "text",
    [
        "Use a bearer token in the Authorization header.",
        "The secret: is short",
        "tokens: unknown",
        "outputTokens=5",
        "AKIA is a prefix",
        "password reset link sent",
    ],
)
def test_masking_benign_prose_unchanged(text: str) -> None:
    assert Redactor().mask(text) == text


def test_masking_is_idempotent() -> None:
    r = Redactor()
    for sample in [*SHAPES.values(), *LINES, TEXT]:
        once = r.mask(sample)
        assert r.mask(once) == once


def test_masking_value_recursion() -> None:
    r = Redactor()
    value = {AWS: [GH, 3, {"k": GLPAT}], "n": 5, "t": (SLACK, None)}
    assert r.mask_value(value) == {AWS: [MASK, 3, {"k": MASK}], "n": 5, "t": [MASK, None]}


def test_masking_disabled_leaves_secrets() -> None:
    r = Redactor(builtin=False)
    assert not r.enabled
    assert r.mask(AWS) == AWS
    assert NO_REDACTION.mask(GH) == GH


def test_masking_user_pattern() -> None:
    r = Redactor(["sekrit-[0-9]+"], builtin=False)
    assert r.enabled
    assert r.mask("a sekrit-42 b") == f"a {MASK} b"


def test_masking_empty_pattern_rejected() -> None:
    with pytest.raises(ValueError, match="matches the empty string"):
        Redactor(["a*"])


def test_masking_stream_every_split_matches_whole() -> None:
    r = Redactor()
    want = r.mask(TEXT)
    for secret in SECRETS:
        assert secret not in want
    for i in range(len(TEXT) + 1):
        s = r.stream()
        got = s.feed(TEXT[:i]) + s.feed(TEXT[i:]) + s.flush()
        assert got == want, i


def test_masking_stream_char_by_char() -> None:
    r = Redactor()
    s = r.stream()
    got = "".join(s.feed(c) for c in TEXT) + s.flush()
    assert got == r.mask(TEXT)


def test_masking_stream_holds_unterminated_pem() -> None:
    r = Redactor()
    s = r.stream()
    head = "intro line\n-----BEGIN RSA PRIVATE KEY-----\nMIIAAAA\n"
    assert s.feed(head) == "intro line\n"
    assert s.feed("more key material\n") == ""
    assert s.flush() == MASK
    assert s.flush() == ""


def test_masking_journal_values_and_errors(tmp_path: Path) -> None:
    r = Redactor()
    header = RunHeader("r1", "wf.js", None, False, None, 10, None)
    writer = JournalWriter.create(tmp_path, header, mask=r.mask_value)
    writer.append_agent(AgentRecord(0, "k0", "ok", {"out": f"key {AWS}"}))
    writer.append_agent(AgentRecord(1, "k1", "error", None, error=f"boom {GH}"))
    writer.close()
    raw = (run_dir(tmp_path, "r1") / "journal.jsonl").read_text(encoding="utf-8")
    assert AWS not in raw
    assert GH not in raw
    assert f"key {MASK}" in raw
    assert f"boom {MASK}" in raw


def test_masking_value_under_secret_named_key() -> None:
    value = {
        "password": "hunter2hunter2",
        "api_key": "short",
        "nested": {"client_secret": ["a", {"x": "b"}], "Authorization": "opaque", "author": "Ann"},
        "items": [{"access_key": "abc"}, {"private-key": "k"}, {"credentials": {"u": "p"}}],
        "token": 5,
        "input_tokens": 12,
        "note": "plain",
        "empty_password": "",
    }
    out = Redactor().mask_value(value)
    assert out == {
        "password": MASK,
        "api_key": MASK,
        "nested": {"client_secret": [MASK, {"x": MASK}], "Authorization": MASK, "author": "Ann"},
        "items": [{"access_key": MASK}, {"private-key": MASK}, {"credentials": {"u": MASK}}],
        "token": 5,
        "input_tokens": 12,
        "note": "plain",
        "empty_password": "",
    }
    # The input is not altered, and builtin=False leaves keys alone.
    assert value["password"] == "hunter2hunter2"
    assert Redactor(["zzz"], builtin=False).mask_value(value) == value
