"""A worker chat resolves the turn it works for, so a per-turn policy can reach it.

An embedding host's `Policy` may bind something to each client turn (a credential,
say) when `chat/turnStarted` passes `may_dispatch`. A worker chat's own turns are
host-internal and never pass it, so an inner provider asks
`ResolvesOriginTurn.origin_turn` for the turn the run serves and looks the
binding up under that.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from pathlib import Path
from typing import Any, ClassVar

import pytest
from ahp_host import (
    AgentInfo,
    AgentSessionContext,
    ConnectionInfo,
    LoopbackSingleUserPolicy,
    TurnSink,
    UserMessage,
)
from ahp_host.provider.base import OriginTurn, ResolvesOriginTurn

from tests.flow_fakes import reset_fakes
from tests.flow_hosting import (
    SESSION,
    FlowFixture,
    boot_flow,
    final_json,
    restart_flow,
    store_for,
)
from tests.hosting import open_session, start_turn, wait_turn, worker_chats
from tests.preamble import stripped

pytestmark = pytest.mark.node

TOML = (
    '[providers.cred]\nload = "tests.integration.test_origin_turn:CredProvider"\n'
    '\n[routing]\ndefault = "cred"\n'
)
PARALLEL = "```js\nconst r = await Promise.all([agent('one'), agent('two')])\nreturn r\n```"
HOLD = "```js\nconst a = await agent('one')\nconst b = await agent('hold two')\nreturn [a, b]\n```"


class BindingPolicy(LoopbackSingleUserPolicy):
    """Binds a credential to every client turn it lets start or resume, as an embedder would."""

    def __init__(self) -> None:
        self.bound: dict[tuple[str, str], str] = {}
        self.resumed: list[tuple[str, str]] = []

    def may_dispatch(self, info: ConnectionInfo, channel: str, action: Mapping[str, Any]) -> bool:
        kind = action.get("type")
        if kind in ("chat/turnStarted", "chat/turnResume"):
            key = (channel, str(action["turnId"]))
            self.bound[key] = f"cred:{key[1]}"
            if kind == "chat/turnResume":
                self.resumed.append(key)
        return True

    def credential_for_turn(self, chat_uri: str, turn_id: str) -> str | None:
        return self.bound.get((chat_uri, turn_id))


class CredSession:
    def __init__(self, context: AgentSessionContext) -> None:
        self.context = context

    async def send_user_message(self, message: UserMessage, sink: TurnSink) -> None:
        text = stripped(message).text
        publisher = self.context.publisher
        assert isinstance(publisher, ResolvesOriginTurn)
        origin = publisher.origin_turn()
        CredProvider.seen.append((text, self.context.chat_uri, origin))
        if text.startswith("hold") and CredProvider.holding:
            await asyncio.Event().wait()
        policy = CredProvider.policy
        credential = policy.credential_for_turn(origin.chat, origin.turn_id) if origin else None
        await sink.text_delta(f"{text}={credential}")

    async def cancel(self, reason: str | None = None) -> None:
        pass

    async def aclose(self) -> None:
        pass


class CredProvider:
    seen: ClassVar[list[tuple[str, str, OriginTurn | None]]] = []
    holding: ClassVar[bool] = False
    policy: ClassVar[BindingPolicy]

    @property
    def agent(self) -> AgentInfo:
        return AgentInfo(provider="cred", display_name="Cred", description="d", models=())

    async def create_session(self, context: AgentSessionContext) -> CredSession:
        return CredSession(context)


def fresh() -> BindingPolicy:
    reset_fakes()
    CredProvider.seen.clear()
    CredProvider.holding = False
    CredProvider.policy = BindingPolicy()
    return CredProvider.policy


async def test_parallel_agents_resolve_the_originating_turn(tmp_path: Path, node_path: str) -> None:
    policy = fresh()
    fx = await boot_flow(tmp_path, node_path, TOML, policy=policy)
    try:
        await start_turn(fx.wire, fx.chat, "t1", PARALLEL)
        turn = await wait_turn(fx.wire, fx.chat, "t1")
        assert final_json(turn) == ["one=cred:t1", "two=cred:t1"]
        assert [origin for _, _, origin in CredProvider.seen] == [OriginTurn(fx.chat, "t1")] * 2
        workers = {str(c["resource"]) for c in worker_chats(fx.host, SESSION)}
        assert {chat for _, chat, _ in CredProvider.seen} == workers
        # The workers' own turns never reached the policy: that is the gap this closes.
        assert set(policy.bound) == {(fx.chat, "t1")}
        # The host answers the same, for an embedder that holds it rather than a publisher.
        assert {fx.host.origin_turn(w) for w in workers} == {OriginTurn(fx.chat, "t1")}
        assert fx.host.origin_turn(fx.chat) is None
    finally:
        await fx.shut()


async def test_concurrent_runs_of_one_owner_each_resolve_their_own_turn(
    tmp_path: Path, node_path: str
) -> None:
    policy = fresh()
    fx = await boot_flow(tmp_path, node_path, TOML, policy=policy)
    try:
        other = await open_session(
            fx.wire, "flow:/s2", "flow", working_directories=[fx.workspace.as_uri()]
        )
        await start_turn(fx.wire, fx.chat, "ta", PARALLEL)
        await start_turn(fx.wire, other, "tb", PARALLEL)
        a = await wait_turn(fx.wire, fx.chat, "ta")
        b = await wait_turn(fx.wire, other, "tb")
        assert final_json(a) == ["one=cred:ta", "two=cred:ta"]
        assert final_json(b) == ["one=cred:tb", "two=cred:tb"]
        origins = {origin for _, _, origin in CredProvider.seen}
        assert origins == {OriginTurn(fx.chat, "ta"), OriginTurn(other, "tb")}
    finally:
        await fx.shut()


async def test_resumed_run_resolves_the_resuming_turn(tmp_path: Path, node_path: str) -> None:
    policy = fresh()
    CredProvider.holding = True
    fx = await boot_flow(tmp_path, node_path, TOML, store=store_for(tmp_path), policy=policy)
    await start_turn(fx.wire, fx.chat, "t1", HOLD)
    assert await fx.wire.until(lambda: any(t.startswith("hold") for t, _, _ in CredProvider.seen))
    await fx.shut()

    CredProvider.holding = False
    CredProvider.seen.clear()
    policy2 = BindingPolicy()
    CredProvider.policy = policy2
    fx2: FlowFixture = await restart_flow(tmp_path, node_path, TOML, policy=policy2)
    try:
        await fx2.wire.dispatch(fx2.chat, {"type": "chat/turnResume", "turnId": "t1"})
        assert await fx2.wire.until(lambda: bool(CredProvider.seen), timeout=20.0)
        turn = await wait_resumed_turn(fx2)
        assert policy2.resumed == [(fx2.chat, "t1")]
        assert ("hold two=cred:t1") in str(final_json(turn))
        assert {origin for _, _, origin in CredProvider.seen} == {OriginTurn(fx2.chat, "t1")}
    finally:
        await fx2.shut()


async def wait_resumed_turn(fx: FlowFixture) -> dict[str, Any]:
    from tests.integration.test_flow_durability import wait_resumed

    return await wait_resumed(fx)
