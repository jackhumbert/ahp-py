"""`child_env`: the environment allowlist for spawned agents (plan M14 item 2).

Every test passes `base=` so the real environment is never read.
"""

from __future__ import annotations

import sys

import pytest

from ahp_host.provider.spawn import child_env, is_secret_name


def test_childenv_keeps_only_allowed() -> None:
    base = {"PATH": "/bin", "HOME": "/h", "M14_DROP": "1"}
    assert child_env(["PATH", "HOME"], base=base) == {"PATH": "/bin", "HOME": "/h"}


def test_childenv_globs_skip_secrets() -> None:
    base = {"LC_ALL": "C", "LC_TOKEN": "s", "M14_KEEP_ME": "1", "M14_KEEP_API_KEY": "k"}
    assert child_env(["LC_*", "M14_KEEP_*"], base=base) == {
        "LC_ALL": "C",
        "M14_KEEP_ME": "1",
    }


def test_childenv_exact_secret_kept() -> None:
    base = {"GITHUB_TOKEN": "t"}
    assert child_env(["GITHUB_TOKEN"], base=base) == {"GITHUB_TOKEN": "t"}
    assert child_env(["GITHUB_*"], base=base) == {}


def test_childenv_extra_wins() -> None:
    out = child_env(["PATH"], {"PATH": "/x", "MY_TOKEN": "y"}, base={"PATH": "/bin"})
    assert out == {"PATH": "/x", "MY_TOKEN": "y"}


@pytest.mark.parametrize(
    ("name", "secret"),
    [
        ("ANTHROPIC_API_KEY", True),
        ("AWS_SECRET_ACCESS_KEY", True),
        ("GH_TOKEN", True),
        ("DB_PASSWORD", True),
        ("SSH_AUTH_SOCK", True),
        ("npm_config__authToken", True),
        ("MY_PRIVATE_KEY", True),
        ("PATH", False),
        ("HOME", False),
        ("LANG", False),
        ("TERM", False),
        ("XDG_SESSION_TYPE", False),
    ],
)
def test_childenv_secret_names(name: str, secret: bool) -> None:
    assert is_secret_name(name) is secret


@pytest.mark.skipif(sys.platform != "win32", reason="Windows names are case-insensitive")
def test_childenv_case_on_windows() -> None:
    assert child_env(["PATH"], base={"Path": "C:\\x"}) == {"Path": "C:\\x"}


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX names are case-sensitive")
def test_childenv_case_on_posix() -> None:
    assert child_env(["PATH"], base={"Path": "C:\\x"}) == {}


def test_childenv_does_not_mutate_base() -> None:
    base = {"PATH": "/bin", "M14_DROP": "1"}
    out = child_env(["PATH"], {"A": "b"}, base=base)
    out["PATH"] = "changed"
    assert base == {"PATH": "/bin", "M14_DROP": "1"}
