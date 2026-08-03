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
    assert not False  # noqa: PT018 - the pre-existing shape, stated for contrast


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
