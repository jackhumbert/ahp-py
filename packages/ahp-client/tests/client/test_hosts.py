"""The supervisor: reconnect, replay, and the things that leak if you get it wrong."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from agent_host_protocol.channels import ROOT_URI
from agent_host_protocol.transport import Transport

from agent_host_client.client.mirror import PendingPolicy
from agent_host_client.hosts import (
    Backoff,
    FileClientIdStore,
    HostConfig,
    HostNotConnected,
    HostRuntime,
    InMemoryClientIdStore,
    ReconnectPolicy,
    ShutdownSignal,
    disabled_policy,
    immediate_forever_policy,
    link,
)
from agent_host_client.testing import FakeHost, echo_host


class _Factory:
    """Hands out a fresh FakeHost per attempt, like a real transport factory."""

    def __init__(self) -> None:
        self.hosts: list[FakeHost] = []
        self.attempts = 0
        self.fail_first = 0

    async def __call__(self) -> Transport:
        self.attempts += 1
        if self.attempts <= self.fail_first:
            raise OSError("connection refused")
        host = echo_host()
        await host.start()
        self.hosts.append(host)
        return host.transport()

    async def stop(self) -> None:
        for host in self.hosts:
            await host.stop()


# ── backoff ──────────────────────────────────────────────────────────────────


def test_exponential_backoff_is_capped() -> None:
    backoff = Backoff("exponential", initial=1.0, maximum=8.0, multiplier=2.0)
    assert [backoff.delay_for(n) for n in range(1, 6)] == [1.0, 2.0, 4.0, 8.0, 8.0]


def test_jitter_is_deterministic_when_the_sample_is_supplied() -> None:
    policy = ReconnectPolicy(backoff=Backoff("constant", initial=10.0), jitter=0.5)
    assert policy.delay_with_jitter(1, sample=0.0) == 5.0
    assert policy.delay_with_jitter(1, sample=0.5) == 10.0
    assert policy.delay_with_jitter(1, sample=1.0) == 15.0


def test_max_attempts_polarity_matches_typescript_and_rust_not_go() -> None:
    """Go's `maxAttempts == 0` means *unlimited*, inverted from everyone else --
    a policy copied from it retries forever exactly where the author meant
    "do not retry"."""
    assert disabled_policy().max_attempts == 0
    assert disabled_policy().exhausted(1)
    assert immediate_forever_policy().max_attempts is None
    assert not immediate_forever_policy().exhausted(10_000)


# ── client id ────────────────────────────────────────────────────────────────


async def test_a_client_id_is_generated_and_persisted_on_first_use() -> None:
    store = InMemoryClientIdStore()
    factory = _Factory()
    runtime = HostRuntime(HostConfig(factory, label="h", client_id_store=store))
    await runtime.start()
    assert await store.load("h") == runtime.client_id
    await runtime.shutdown()
    await factory.stop()


async def test_a_stored_client_id_is_reused_across_runtimes() -> None:
    """This is the whole point: without it every launch is a new client to the
    host, and every launch takes fresh snapshots."""
    store = InMemoryClientIdStore()
    await store.store("h", "stable-id")
    factory = _Factory()
    runtime = HostRuntime(HostConfig(factory, label="h", client_id_store=store))
    await runtime.start()
    assert runtime.client_id == "stable-id"
    await runtime.shutdown()
    await factory.stop()


async def test_the_file_store_round_trips_and_is_owner_only(tmp_path: Path) -> None:
    store = FileClientIdStore(tmp_path)
    assert await store.load("h") is None
    await store.store("h", "abc-123")
    assert await store.load("h") == "abc-123"
    written = next(tmp_path.glob("*.clientid"))
    assert written.stat().st_mode & 0o777 == 0o600


async def test_the_file_store_percent_encodes_a_hostile_host_id(tmp_path: Path) -> None:
    store = FileClientIdStore(tmp_path)
    await store.store("../../etc/passwd", "x")
    assert await store.load("../../etc/passwd") == "x"
    assert not (tmp_path.parent.parent / "etc").exists()


# ── connect ──────────────────────────────────────────────────────────────────


async def test_a_successful_connect_reaches_connected_and_mirrors_root() -> None:
    factory = _Factory()
    runtime = HostRuntime(HostConfig(factory, label="h"))
    await runtime.start()
    assert runtime.state.status == "connected"
    assert runtime.generation == 1
    assert runtime.protocol_version == "0.7.0"
    assert runtime.mirror.state(ROOT_URI) is not None
    await runtime.shutdown()
    await factory.stop()


async def test_a_failed_attempt_retries_rather_than_raising() -> None:
    factory = _Factory()
    factory.fail_first = 2
    runtime = HostRuntime(
        HostConfig(factory, label="h", reconnect_policy=immediate_forever_policy())
    )
    await runtime.start()
    assert factory.attempts == 3
    assert runtime.state.status == "connected"
    await runtime.shutdown()
    await factory.stop()


async def test_exhausting_the_policy_ends_in_failed_not_a_hang() -> None:
    factory = _Factory()
    factory.fail_first = 99
    runtime = HostRuntime(
        HostConfig(
            factory,
            label="h",
            reconnect_policy=ReconnectPolicy(
                backoff=Backoff("immediate"), jitter=0.0, max_attempts=2
            ),
        )
    )
    await runtime.start(wait=False)
    for _ in range(100):
        await asyncio.sleep(0.01)
        if runtime.state.status == "failed":
            break
    assert runtime.state.status == "failed"
    assert isinstance(runtime.state.error, OSError)
    await runtime.shutdown()


async def test_the_client_is_not_handed_out_while_disconnected() -> None:
    factory = _Factory()
    factory.fail_first = 99
    runtime = HostRuntime(HostConfig(factory, label="h", reconnect_policy=disabled_policy()))
    await runtime.start(wait=False)
    with pytest.raises(HostNotConnected):
        runtime.client()
    await runtime.shutdown()


# ── reconnect ────────────────────────────────────────────────────────────────


async def test_a_dropped_connection_is_reestablished_with_a_new_generation() -> None:
    factory = _Factory()
    runtime = HostRuntime(
        HostConfig(factory, label="h", reconnect_policy=immediate_forever_policy())
    )
    await runtime.start()
    assert runtime.generation == 1

    await factory.hosts[0].stop()  # the host goes away mid-flight
    for _ in range(200):
        await asyncio.sleep(0.01)
        if runtime.generation == 2:
            break
    assert runtime.generation == 2
    assert runtime.state.status == "connected"
    await runtime.shutdown()
    await factory.stop()


async def test_reconnect_is_attempted_only_once_state_exists_to_resume() -> None:
    """`reconnect` on a first connection has nothing to resume from, so the
    supervisor sends `initialize` -- and a host that has never seen this client
    is not asked to remember one."""
    factory = _Factory()
    runtime = HostRuntime(HostConfig(factory, label="h"))
    await runtime.start()
    methods = [m.get("method") for m in factory.hosts[0].received]
    assert "initialize" in methods
    assert "reconnect" not in methods
    await runtime.shutdown()
    await factory.stop()


async def test_a_refused_reconnect_falls_back_to_initialize() -> None:
    """An RPC-level refusal means the host cannot resume us. A transport error
    is a different thing and must reach the retry loop instead."""
    from agent_host_client.testing import FakeRpcError

    factory = _Factory()
    runtime = HostRuntime(
        HostConfig(factory, label="h", reconnect_policy=immediate_forever_policy())
    )
    await runtime.start()
    runtime._server_seq = 42  # pretend we have history to resume

    first = factory.hosts[0]
    await first.stop()
    for _ in range(200):
        await asyncio.sleep(0.01)
        if len(factory.hosts) > 1:
            break
    second = factory.hosts[1]
    second.on(
        "reconnect",
        lambda _p: (_ for _ in ()).throw(FakeRpcError({"code": -32008, "message": "unknown"})),
    )
    for _ in range(200):
        await asyncio.sleep(0.01)
        if any(m.get("method") == "initialize" for m in second.received):
            break
    assert runtime.state.status == "connected"
    await runtime.shutdown()
    await factory.stop()


# ── cancellation ─────────────────────────────────────────────────────────────


async def test_link_detaches_its_waiters() -> None:
    """The leak this prevents grows once per reconnect cycle against a
    long-lived signal, so it is invisible until it is enormous."""
    signal = ShutdownSignal("s")
    captured: list[asyncio.Future[Any]] = []
    for _ in range(50):
        async with link(signal) as waiters:
            captured.extend(waiters)
    assert all(w.done() for w in captured)


async def test_race_awaits_the_loser_so_asyncio_never_reports_an_orphan() -> None:
    from agent_host_client.hosts.runtime import race

    signal = ShutdownSignal("s")
    async with link(signal) as waiters:
        assert await race(asyncio.sleep(0, result="won"), waiters) == "won"
        assert all(not w.done() for w in waiters)
    signal.trigger()
    async with link(signal) as waiters:
        with pytest.raises(asyncio.CancelledError):
            await race(asyncio.sleep(5), waiters)


# ── subscriptions ────────────────────────────────────────────────────────────


async def test_a_subscription_binds_its_reducer_and_applies_the_snapshot() -> None:
    factory = _Factory()
    runtime = HostRuntime(HostConfig(factory, label="h"))
    await runtime.start()
    host = factory.hosts[0]
    chat = "ahp-chat://c/s"
    host.on(
        "subscribe",
        lambda p: {"snapshot": {"resource": p["channel"], "state": {"turns": []}, "fromSeq": 1}},
    )
    await runtime.subscribe(chat, "chat")
    assert runtime.mirror.channels[chat].reducer_name == "chat"
    assert runtime.mirror.state(chat) == {"turns": []}
    await runtime.shutdown()
    await factory.stop()


@pytest.mark.parametrize("policy", list(PendingPolicy))
async def test_every_pending_policy_survives_a_reconnect(policy: PendingPolicy) -> None:
    factory = _Factory()
    runtime = HostRuntime(
        HostConfig(
            factory,
            label="h",
            pending_policy=policy,
            reconnect_policy=immediate_forever_policy(),
        )
    )
    await runtime.start()
    await factory.hosts[0].stop()
    for _ in range(200):
        await asyncio.sleep(0.01)
        if runtime.generation == 2:
            break
    assert runtime.state.status == "connected"
    await runtime.shutdown()
    await factory.stop()
