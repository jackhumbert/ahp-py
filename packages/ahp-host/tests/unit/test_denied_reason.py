"""A policy can supply the message a person actually reads.

Without this every refusal wears the host's generic string, and for terminals that
string is rendered by VS Code as "The terminal process failed to launch: ..." --
i.e. as a crash in the host rather than as a host that does not offer terminals.
"""

from __future__ import annotations

import pytest

from agent_host_server import Denied
from agent_host_server.core.policy import LoopbackSingleUserPolicy, reason_or


def test_denied_is_falsy_so_existing_policies_are_unaffected() -> None:
    """The whole design rests on this: `if not verdict` must still take the
    refusal branch, so a policy returning plain False needs no change."""
    assert not Denied("nope")
    assert not bool(Denied("nope"))
    assert not False


def test_reason_or_prefers_the_policys_own_message() -> None:
    assert reason_or(Denied("this host does not provide terminals"), "fallback") == (
        "this host does not provide terminals"
    )


def test_reason_or_falls_back_for_a_plain_bool() -> None:
    """A policy that knows nothing about `Denied` keeps the host's message."""
    assert reason_or(False, "Not permitted to create a terminal") == (
        "Not permitted to create a terminal"
    )
    assert reason_or(True, "unused") == "unused"


@pytest.mark.parametrize("hook", ["may_create_terminal", "may_create_session"])
def test_the_shipped_permissive_policy_still_returns_a_plain_bool(hook: str) -> None:
    """`Denied` is additive: nothing in the library started returning it."""
    policy = LoopbackSingleUserPolicy()
    from agent_host_server.core.policy import ConnectionInfo

    verdict = getattr(policy, hook)(ConnectionInfo(client_id="c"), {})
    assert verdict is True


# ── an inert backend must not advertise `!command` ───────────────────────────


def test_a_backend_that_runs_nothing_does_not_advertise_the_command_prefix() -> None:
    """The class check alone was not enough.

    A host may install a backend that deliberately executes nothing -- to satisfy a
    client that opens a terminal unconditionally and explain itself in the panel
    rather than refusing and producing an error toast on every focus. Advertising
    `!` for such a backend turns a working input into a dead end, which is the same
    reason the refusing default does not advertise it.
    """
    from agent_host_server import Host, LoopbackSingleUserPolicy
    from agent_host_server.provider import EchoProvider

    class Inert:
        runs_commands = False

        async def create(self, request: object, output: object) -> object:  # pragma: no cover
            raise AssertionError("not called")

    class Executes:
        async def create(self, request: object, output: object) -> object:  # pragma: no cover
            raise AssertionError("not called")

    default = Host(EchoProvider(), LoopbackSingleUserPolicy())
    assert default._advertised_prefix() == "", "the refusing default must stay silent"

    inert = Host(EchoProvider(), LoopbackSingleUserPolicy(), terminals=Inert())  # type: ignore[arg-type]
    assert inert._advertised_prefix() == "", "runs_commands=False must not advertise"

    # Absent attribute defaults to True, so every pre-existing backend is unaffected.
    real = Host(EchoProvider(), LoopbackSingleUserPolicy(), terminals=Executes())  # type: ignore[arg-type]
    assert real._advertised_prefix() == "!"
