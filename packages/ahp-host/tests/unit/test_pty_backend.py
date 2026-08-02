"""The pty backend, exercised against a real shell.

Mocking a pty proves nothing: every defect this file guards is in the
interaction with the kernel, not in the Python around it. A leaked descriptor,
an orphaned process group and a window size that never reaches the child all
look completely fine in review.

Skipped where there is no POSIX pty to open.
"""

from __future__ import annotations

import asyncio
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from agent_host_server.core.errors import AhpError
from agent_host_server.core.pty_backend import PtyTerminalBackend
from agent_host_server.core.terminals import TerminalRequest, TerminalSessionClaim

pytestmark = [
    pytest.mark.anyio,
    pytest.mark.skipif(sys.platform == "win32", reason="POSIX pty"),
]

_CLAIM = TerminalSessionClaim(session="echo:/s")


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _request(**overrides: object) -> TerminalRequest:
    fields: dict[str, object] = {
        "channel": "ahp-terminal:/t",
        "claim": _CLAIM,
        "cwd": os.getcwd(),
        "cols": 80,
        "rows": 24,
    }
    fields.update(overrides)
    return TerminalRequest(**fields)  # type: ignore[arg-type]


async def _collect(command: list[str]) -> tuple[str, int | None]:
    output = bytearray()
    process = await PtyTerminalBackend().create(_request(command=command), output.extend)
    code = await asyncio.wait_for(process.wait(), timeout=15)
    return output.decode(errors="replace"), code


class TestItIsARealTerminal:
    async def test_the_child_gets_a_controlling_terminal(self) -> None:
        """Without `setsid` there is no controlling tty: job control breaks and
        Ctrl-C reaches nothing."""
        text, _ = await _collect(["/bin/sh", "-c", "tty"])
        assert "/dev/tty" in text

    async def test_the_requested_size_reaches_the_child(self) -> None:
        """TIOCSWINSZ takes ROWS first. Transposing is silent -- the shell just
        wraps at the wrong column."""
        text, _ = await _collect(["/bin/sh", "-c", "stty size"])
        assert re.search(r"\b24 80\b", text), text

    async def test_the_exit_code_survives(self) -> None:
        _, code = await _collect(["/bin/sh", "-c", "exit 7"])
        assert code == 7

    async def test_output_is_not_stripped(self) -> None:
        """Escape sequences reach the sink raw. Parsing them here would break
        any sequence split across two reads."""
        text, _ = await _collect(["/bin/sh", "-c", "printf '\\033[31mred\\033[0m'"])
        assert "\033[31m" in text


class TestLifecycle:
    async def test_input_reaches_the_shell(self) -> None:
        output = bytearray()
        process = await PtyTerminalBackend().create(
            _request(command=["/bin/sh", "-i"]), output.extend
        )
        await asyncio.sleep(0.4)
        await process.write(b"echo MARKER-42\n")
        await asyncio.sleep(0.5)
        await process.write(b"exit\n")
        await asyncio.wait_for(process.wait(), timeout=10)
        assert "MARKER-42" in output.decode(errors="replace")

    async def test_resize_takes_effect_on_a_live_shell(self) -> None:
        output = bytearray()
        process = await PtyTerminalBackend().create(
            _request(command=["/bin/sh", "-i"]), output.extend
        )
        await asyncio.sleep(0.4)
        await process.resize(cols=132, rows=50)
        await asyncio.sleep(0.2)
        output.clear()
        await process.write(b"stty size\n")
        await asyncio.sleep(0.5)
        assert re.search(r"\b50 132\b", output.decode(errors="replace"))
        await process.kill()
        await asyncio.wait_for(process.wait(), timeout=10)

    async def test_kill_takes_the_whole_process_group(self) -> None:
        """Signalling only the leader leaves its children running on a pty
        nobody reads -- a "closed" terminal that keeps burning CPU."""
        output = bytearray()
        process = await PtyTerminalBackend().create(
            _request(command=["/bin/sh", "-c", "sleep 300 & echo PID=$!; wait"]),
            output.extend,
        )
        await asyncio.sleep(0.6)
        matched = re.search(r"PID=(\d+)", output.decode(errors="replace"))
        assert matched, output.decode(errors="replace")
        grandchild = matched.group(1)

        await process.kill()
        await asyncio.wait_for(process.wait(), timeout=10)
        await asyncio.sleep(0.3)

        alive = subprocess.run(["ps", "-p", grandchild], capture_output=True).returncode == 0
        assert not alive, f"pid {grandchild} outlived its terminal"

    async def test_descriptors_are_not_leaked(self) -> None:
        """A host that opens terminals over a long life otherwise runs out --
        and it surfaces hours later as an unrelated command failing to start."""
        before = len(os.listdir("/dev/fd"))
        for _ in range(5):
            await _collect(["/bin/sh", "-c", "true"])
        await asyncio.sleep(0.4)
        assert len(os.listdir("/dev/fd")) <= before + 1


class TestItRefusesRatherThanReturningACorpse:
    async def test_a_missing_program_is_refused(self) -> None:
        with pytest.raises(AhpError) as caught:
            await PtyTerminalBackend().create(
                _request(command=["/definitely/not/a/program"]), lambda _: None
            )
        assert caught.value.code == -32009

    async def test_a_bad_cwd_is_refused(self) -> None:
        with pytest.raises(AhpError):
            await PtyTerminalBackend().create(
                _request(cwd="/no/such/directory", command=["/bin/sh"]), lambda _: None
            )


class TestTheEnvironmentIsNotInherited:
    async def test_a_host_secret_does_not_reach_the_shell(self, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        """A host process typically holds tokens and cloud config. A shell a
        peer asked for must not start life holding them."""
        monkeypatch.setenv("AHS_FAKE_CREDENTIAL", "super-secret-value")
        text, _ = await _collect(["/bin/sh", "-c", "env"])
        assert "super-secret-value" not in text
        # But it is still a usable terminal.
        assert "PATH=" in text
        assert "TERM=" in text


class TestCwdIsAUri:
    """The terminal channel is URIs throughout.

    `CreateTerminalParams.cwd`, `TerminalState.cwd` and the `cwd` on
    `terminal/cwdChanged` are all declared `URI`. Handing one to
    `os.path.isdir` fails, and the failure is user-facing: VS Code renders the
    refusal verbatim, so the user saw "The terminal process failed to launch:
    Terminal refused: file:///Users/... is not a directory."

    Missed because every probe written for this backend passed a plain path.
    """

    async def test_a_file_uri_cwd_is_accepted(self) -> None:
        here = os.getcwd()
        output = bytearray()
        process = await PtyTerminalBackend().create(
            _request(cwd=Path(here).as_uri(), command=["/bin/sh", "-c", "pwd"]),
            output.extend,
        )
        await asyncio.wait_for(process.wait(), timeout=10)
        assert here in output.decode(errors="replace")

    async def test_a_plain_path_still_works(self) -> None:
        """An embedder building a TerminalRequest by hand writes a path."""
        here = os.getcwd()
        output = bytearray()
        process = await PtyTerminalBackend().create(
            _request(cwd=here, command=["/bin/sh", "-c", "pwd"]), output.extend
        )
        await asyncio.wait_for(process.wait(), timeout=10)
        assert here in output.decode(errors="replace")

    async def test_a_bad_uri_is_refused_in_the_users_own_words(self) -> None:
        """The message quotes what the user picked, not what we converted."""
        missing = Path("/no/such/directory").as_uri()
        with pytest.raises(AhpError) as caught:
            await PtyTerminalBackend().create(
                _request(cwd=missing, command=["/bin/sh"]), lambda _: None
            )
        assert missing in str(caught.value)


class TestReportedCwdBecomesAUri:
    """The mirror image: OSC 633 reports a PATH and the action declares a URI."""

    def test_an_absolute_path_is_converted(self) -> None:
        from agent_host_server.core.host import _cwd_uri

        assert _cwd_uri("/Users/someone/work") == "file:///Users/someone/work"

    def test_a_uri_is_left_alone(self) -> None:
        from agent_host_server.core.host import _cwd_uri

        assert _cwd_uri("file:///already/a/uri") == "file:///already/a/uri"

    def test_a_relative_path_is_not_invented_into_a_uri(self) -> None:
        """A shell reporting a relative cwd has told us something we cannot
        convert. Passing it through beats inventing a root to resolve it."""
        from agent_host_server.core.host import _cwd_uri

        assert _cwd_uri("relative/dir") == "relative/dir"
