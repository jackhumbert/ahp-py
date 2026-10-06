"""Completion trigger characters a provider declares itself.

`completionTriggerCharacters` is the only thing that makes a client call
`completions`, and it could be set only as `Host(completion_trigger_characters=)`.
So a provider could not say that its slash commands start with ``/``, and a host
built without the argument -- `ahp-node`, which fronts whatever agents its
config names -- advertised nothing: no provider's completions were reachable.
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from ahp_host.core import Host, LoopbackSingleUserPolicy
from ahp_host.node import runner
from ahp_host.node.config import load
from ahp_host.provider import EchoProvider
from ahp_host.provider.base import DeclaresCompletionTriggers

from .hosting import connect, shut

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


async def _advertised(host: Host) -> list[str] | None:
    """`completionTriggerCharacters` as a client is told it, or ``None``."""
    wire, serving = await connect(host)
    try:
        value = wire.initialized.get("completionTriggerCharacters")
        return list(value) if value is not None else None
    finally:
        await shut(host, wire, serving)


class NoCompletions:
    """Declares triggers but cannot complete: a promise it could not keep."""

    completion_trigger_characters: Sequence[str] = ("@",)

    def __init__(self) -> None:
        self._echo = EchoProvider(provider_id="mute")

    @property
    def agent(self) -> Any:
        return self._echo.agent

    async def create_session(self, context: Any) -> Any:  # pragma: no cover - not used
        raise AssertionError


def test_the_declaration_is_feature_detected() -> None:
    assert isinstance(EchoProvider(), DeclaresCompletionTriggers)
    assert isinstance(NoCompletions(), DeclaresCompletionTriggers)


async def test_with_no_host_argument_the_provider_declares() -> None:
    host = Host(EchoProvider(completion_trigger_characters=("/",)), LoopbackSingleUserPolicy())
    assert host.completion_triggers() == ("/",)
    assert await _advertised(host) == ["/"]


async def test_an_explicit_host_argument_wins_outright() -> None:
    provider = EchoProvider(completion_trigger_characters=("/",))
    assert Host(
        provider, LoopbackSingleUserPolicy(), completion_trigger_characters=("#",)
    ).completion_triggers() == ("#",)
    # `()` is an answer too -- "advertise none" -- not "no answer".
    host = Host(provider, LoopbackSingleUserPolicy(), completion_trigger_characters=())
    assert await _advertised(host) is None


def test_several_agents_merge_in_order_each_character_once() -> None:
    host = Host(
        [
            EchoProvider(provider_id="a", completion_trigger_characters=("/", "@")),
            EchoProvider(provider_id="b", completion_trigger_characters=("@", "#")),
            NoCompletions(),
        ],
        LoopbackSingleUserPolicy(),
    )
    # NoCompletions' "@" would not count even if nobody else declared it.
    assert host.completion_triggers() == ("/", "@", "#")


def test_a_provider_that_cannot_complete_contributes_nothing() -> None:
    host = Host(NoCompletions(), LoopbackSingleUserPolicy())
    assert host.completion_triggers() == ()


async def test_ahp_node_advertises_its_agents_triggers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The node builds its Host without the argument, on purpose: only its
    agents know what opens their pickers. Its echo agent answers ``#``."""
    (tmp_path / "work").mkdir()
    config = tmp_path / "node.toml"
    config.write_text(
        f"root = '{tmp_path / 'work'}'\nstate_dir = '{tmp_path / 'state'}'\n"
        '[[agents]]\ntype = "echo"\n'
    )
    settings = load(
        argparse.Namespace(
            config=config,
            root=None,
            port=None,
            bind=None,
            token_file=None,
            state_dir=None,
            log_file=None,
            verbose=None,
        )
    )
    built: list[Host] = []

    class StopError(Exception):
        pass

    class Capturing(Host):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            assert "completion_trigger_characters" not in kwargs
            super().__init__(*args, **kwargs)
            built.append(self)
            raise StopError

    monkeypatch.setattr(runner, "Host", Capturing)
    with pytest.raises(StopError):
        await runner.run(settings)
    [host] = built
    assert host.completion_triggers() == ("#",)
