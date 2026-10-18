"""Where there is no POSIX pty, the terminal backend says so and declines.

Runs on every platform: a missing `fcntl` is simulated rather than waited for,
so the refusal Windows users would see is checked on the Linux gate too.
"""

from __future__ import annotations

import argparse
import sys

import pytest

from ahp_host.__main__ import _terminal_backend


@pytest.fixture
def no_pty(monkeypatch: pytest.MonkeyPatch) -> None:
    # `None` in sys.modules makes `import fcntl` raise, as it does on Windows.
    monkeypatch.setitem(sys.modules, "fcntl", None)
    monkeypatch.setitem(sys.modules, "termios", None)
    monkeypatch.delitem(sys.modules, "ahp_host.core.pty_backend", raising=False)


def test_importing_the_backend_explains_itself(no_pty: None) -> None:
    with pytest.raises(ImportError, match=r"needs a POSIX pty .*fcntl and termios"):
        import ahp_host.core.pty_backend  # noqa: F401


def test_the_demo_declines_terminal_by_name_instead_of_crashing(no_pty: None) -> None:
    with pytest.raises(SystemExit, match=r"--terminal: .*needs a POSIX pty"):
        _terminal_backend(argparse.Namespace(serve_directory=None))
