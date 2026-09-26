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


async def test_an_explicitly_supplied_client_id_is_still_written_back() -> None:
    """Plan section 6.3: resolution is explicit -> stored -> uuid4(), and the
    resolved value is ALWAYS written back. A process that passes the id once
    and later relies on the store would otherwise load a stale or fresh id and
    silently lose its reconnect identity."""
    store = InMemoryClientIdStore()
    factory = _Factory()
    runtime = HostRuntime(HostConfig(factory, label="h", client_id="chosen", client_id_store=store))
    await runtime.start()
    assert runtime.client_id == "chosen"
    assert await store.load("h") == "chosen"
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
    assert runtime.protocol_version == "0.9.0"
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


async def test_a_permanent_refusal_raises_from_start_rather_than_hanging() -> None:
    """The classification half already worked -- `should_retry` declines -32005
    and the supervisor reaches `failed`. This is the release half: `wait=True`
    is the default and what `connect()` uses, so without it every caller hitting
    a version disagreement blocks with no exception and nothing to observe."""
    from agent_host_client.client.errors import UnsupportedProtocolVersion
    from agent_host_client.testing import FakeRpcError

    def refuse(_params: Any) -> Any:
        raise FakeRpcError({"code": -32005, "message": "no mutually supported version"})

    hosts: list[FakeHost] = []

    async def factory() -> Transport:
        host = echo_host()
        host.on("initialize", refuse)
        await host.start()
        hosts.append(host)
        return host.transport()

    runtime = HostRuntime(HostConfig(factory, label="h"))
    with pytest.raises(UnsupportedProtocolVersion):
        await asyncio.wait_for(runtime.start(), 5)
    assert runtime.state.status == "failed"
    await runtime.shutdown()
    for host in hosts:
        await host.stop()


async def test_an_exhausted_budget_raises_from_the_default_wait() -> None:
    """The second way to reach terminal. Both must release `start(wait=True)`,
    or one of them silently becomes a hang the next time somebody edits the
    supervisor."""
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
    with pytest.raises(OSError, match="connection refused"):
        await asyncio.wait_for(runtime.start(), 5)
    await runtime.shutdown()


async def test_a_refusal_the_policy_retries_keeps_waiting() -> None:
    """The other half of the same rule. A -32009 with an unlimited budget is
    still trying, so `start(wait=True)` blocking is correct -- releasing on
    every failed *attempt* would turn a reconnect into an error."""
    from agent_host_client.testing import FakeRpcError

    def refuse(_params: Any) -> Any:
        raise FakeRpcError({"code": -32009, "message": "not permitted"})

    hosts: list[FakeHost] = []

    async def factory() -> Transport:
        host = echo_host()
        host.on("initialize", refuse)
        await host.start()
        hosts.append(host)
        return host.transport()

    runtime = HostRuntime(
        HostConfig(factory, label="h", reconnect_policy=immediate_forever_policy())
    )
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(runtime.start(), 0.2)
    assert runtime.state.status == "reconnecting"
    await runtime.shutdown()
    for host in hosts:
        await host.stop()


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


async def test_cancelling_the_supervisor_without_shutdown_still_stops_it() -> None:
    """Regression: an un-closed `Client` whose owning coroutine raises or
    returns leaves the supervisor task running when `asyncio.run()` cancels
    every still-pending task on the way out (`_cancel_all_tasks`). `_drain`
    used to catch `asyncio.CancelledError` unconditionally and `return`,
    because `race()` raises that same exception both for a genuine external
    cancellation and for its own `_shutdown`/`_manual` signal firing --
    swallowing the former let `_supervise`'s loop read the return as an
    ordinary disconnect and reconnect, so the task `asyncio.run()` was
    waiting on never finished and the whole process hung.

    This never calls `shutdown()` -- that already worked, because it sets
    `_shutdown` *before* cancelling. It cancels the supervisor the way
    `asyncio.run()` does: from outside, with nothing set first."""
    factory = _Factory()
    runtime = HostRuntime(HostConfig(factory, label="h"))
    await runtime.start()
    assert runtime.state.status == "connected"

    task = runtime._supervisor
    assert task is not None
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        # A bounded wait: if the regression is back, this raises
        # `TimeoutError` instead of `CancelledError`, failing loudly rather
        # than hanging the suite the way the real bug hung a process.
        await asyncio.wait_for(task, 2)

    await factory.stop()


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


async def test_request_ids_continue_across_a_transport_swap() -> None:
    """Plan section 6.1: ids and clientSeq never reset across transport swaps
    (VS Code's first frame on a fresh socket carried id 66). One `AhpClient` is
    one transport, so continuity is the supervisor's to keep -- it seeds each
    successor from where the predecessor stopped."""
    factory = _Factory()
    runtime = HostRuntime(
        HostConfig(factory, label="h", reconnect_policy=immediate_forever_policy())
    )
    await runtime.start()

    first = factory.hosts[0]
    first_ids = [m["id"] for m in first.received if "id" in m]
    await first.stop()
    for _ in range(200):
        await asyncio.sleep(0.01)
        if len(factory.hosts) > 1 and runtime.state.status == "connected":
            break
    second = factory.hosts[1]
    second_ids = [m["id"] for m in second.received if "id" in m]
    assert first_ids
    assert second_ids
    assert min(second_ids) > max(first_ids)
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


async def test_a_disposed_session_stops_being_tracked_and_mirrored() -> None:
    """`reconnect.missing` only covers the reconnect path. On a live connection
    `root/sessionRemoved` is the whole mechanism -- there is no
    `session/disposed` action -- so a runtime that only pops the summary keeps
    mirroring a session the host has forgotten, still reporting
    `lifecycle: "ready"`, and resubscribes to it on the next reconnect."""
    session = "echo:/s1"
    chat = "ahp-chat:/c1"
    state = {"lifecycle": "ready", "chats": [{"resource": chat}], "defaultChat": chat}

    factory = _Factory()
    runtime = HostRuntime(HostConfig(factory, label="h"))
    await runtime.start()
    host = factory.hosts[0]
    host.on(
        "subscribe",
        lambda p: {
            "snapshot": {
                "resource": p["channel"],
                "state": state if p["channel"] == session else {},
                "fromSeq": host._server_seq,
            }
        },
    )
    await runtime.subscribe(session, "session")
    await runtime.subscribe(chat, "chat")
    runtime.session_summaries[session] = {"resource": session}

    await host.notify("root/sessionRemoved", {"channel": ROOT_URI, "session": session})
    for _ in range(200):
        await asyncio.sleep(0.01)
        if session not in runtime._subscriptions:
            break

    assert sorted(runtime._subscriptions) == [ROOT_URI]
    assert sorted(runtime.mirror.channels) == [ROOT_URI]
    assert runtime.mirror.state(session) is None
    assert runtime.session_summaries == {}
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


class _ScriptedFactory:
    """Hands out pre-built hosts in order.

    `_Factory` builds its host inside `__call__`, so a test can only program the
    reconnect target *after* the supervisor already has it -- and then races the
    request it is trying to answer. These are built up front.
    """

    def __init__(self, *hosts: FakeHost) -> None:
        self._queue = list(hosts)
        self.handed: list[FakeHost] = []

    async def __call__(self) -> Transport:
        host = self._queue.pop(0)
        await host.start()
        self.handed.append(host)
        return host.transport()

    async def stop(self) -> None:
        for host in self.handed:
            await host.stop()


async def _spin(predicate: Any, ticks: int = 300) -> None:
    for _ in range(ticks):
        await asyncio.sleep(0.01)
        if predicate():
            return


def _bump(count: int) -> dict[str, Any]:
    return {"type": "root/activeSessionsChanged", "activeSessions": count}


async def _drain(reader: Any) -> list[Any]:
    """Everything already published, without blocking on the next one."""
    seen: list[Any] = []
    while True:
        try:
            seen.append(await asyncio.wait_for(reader.__anext__(), 0.05))
        except (TimeoutError, StopAsyncIteration):
            return seen


async def test_a_refused_subscribe_leaves_nothing_behind_in_either_half() -> None:
    """`AhpClient.subscribe` rolls its own queue back on a refusal; the runtime
    did not, so a -32009 left a bound, snapshot-less channel in the mirror and a
    subscription re-requested on every reconnect, where it can only be declined
    again."""
    from agent_host_client.client.errors import RpcError
    from agent_host_client.testing import FakeRpcError

    chat = "ahp-chat://c/refused"
    factory = _Factory()
    runtime = HostRuntime(HostConfig(factory, label="h"))
    await runtime.start()
    factory.hosts[0].on(
        "subscribe",
        lambda p: (_ for _ in ()).throw(
            FakeRpcError({"code": -32009, "message": f"Not permitted to observe {p['channel']}"})
        ),
    )

    with pytest.raises(RpcError):
        await runtime.subscribe(chat, "chat")

    assert chat not in runtime._subscriptions
    assert chat not in runtime.mirror.channels
    await runtime.shutdown()
    await factory.stop()


async def test_subscribing_while_disconnected_still_records_the_intent() -> None:
    """The other half of the rollback rule, and the reason it keys on
    `RpcError`: no host answered, so the next successful connect must still
    thread this into the handshake."""
    chat = "ahp-chat://c/later"
    factory = _Factory()
    factory.fail_first = 99
    runtime = HostRuntime(HostConfig(factory, label="h", reconnect_policy=disabled_policy()))
    await runtime.start(wait=False)

    with pytest.raises(HostNotConnected):
        await runtime.subscribe(chat, "chat")

    assert chat in runtime._subscriptions
    await runtime.shutdown()


async def test_replay_does_not_republish_what_the_mirror_discards_as_stale() -> None:
    """`lastSeenServerSeq` is one scalar over a host-global counter, so it
    cannot say "and this channel is already at 9"; the host replays from the
    scalar and is right to. The per-channel `Snapshot.fromSeq` baseline is the
    designed defence, and publishing what it discarded hands a consumer a
    duplicate of an event it already has."""
    chat = "ahp-chat://c/s"
    first, second = echo_host(), echo_host()
    first.on(
        "subscribe",
        lambda p: {
            "snapshot": {
                "resource": p["channel"],
                "state": {"turns": [], "status": 1},
                "fromSeq": 9,
            }
        },
    )
    second.on(
        "reconnect",
        lambda _p: {
            "type": "replay",
            "missing": [],
            "actions": [
                {
                    "channel": chat,
                    "action": {"type": "chat/titleChanged", "title": "already in the snapshot"},
                    "serverSeq": 6,
                },
                {
                    "channel": chat,
                    "action": {"type": "chat/titleChanged", "title": "genuine catch-up"},
                    "serverSeq": 11,
                },
            ],
        },
    )
    factory = _ScriptedFactory(first, second)
    runtime = HostRuntime(
        HostConfig(factory, label="h", reconnect_policy=immediate_forever_policy())
    )
    await runtime.start()
    await runtime.subscribe(chat, "chat")
    runtime._server_seq = 4  # a cursor below the channel's baseline, as the host sees it
    events = runtime.events()

    await first.stop()
    await _spin(lambda: runtime.generation == 2)
    assert runtime.generation == 2

    titles = [
        e.event.action.get("title")
        for e in await _drain(events)
        if e.event.__class__.__name__ == "ActionEvent"
    ]
    assert "already in the snapshot" not in titles
    assert "genuine catch-up" in titles
    await runtime.shutdown()
    await factory.stop()


async def test_a_pending_action_the_policy_keeps_is_put_back_on_the_wire() -> None:
    """ADR 0006b's replay arm is "drop what the replay acknowledged and re-send
    the survivors". Only the first half existed, so a `dispatchAction` whose
    frame died in the write loop stayed in `pending` forever -- `optimistic`
    rendering a turn the host has never heard of, with no echo that could ever
    retire it."""
    action = {"type": "root/activeSessionsChanged", "activeSessions": 7}
    first, second = echo_host(), echo_host()
    second.on("reconnect", lambda _p: {"type": "replay", "missing": [], "actions": []})
    factory = _ScriptedFactory(first, second)
    runtime = HostRuntime(
        HostConfig(factory, label="h", reconnect_policy=immediate_forever_policy())
    )
    await runtime.start()
    runtime.client().dispatch(ROOT_URI, action)
    runtime._server_seq = 4  # so the supervisor resumes rather than re-initialising
    assert [p.client_seq for p in runtime.mirror.pending(ROOT_URI)] == [1]

    await first.stop()
    await _spin(lambda: runtime.generation == 2)
    assert runtime.generation == 2

    resent = [m for m in second.received if m.get("method") == "dispatchAction"]
    assert [m["params"]["clientSeq"] for m in resent] == [1]
    assert resent[0]["params"]["action"] == action
    # Still pending: the re-sent frame has not been echoed, so the optimistic
    # effect must survive until it is.
    assert [p.client_seq for p in runtime.mirror.pending(ROOT_URI)] == [1]
    await runtime.shutdown()
    await factory.stop()


async def test_a_resent_client_seq_cannot_collide_with_the_next_new_dispatch() -> None:
    """The reconnect builds a *fresh* client, which starts counting at 1. Re-send
    3 and then let the caller dispatch, and both claim 3 -- the first echo then
    retires the wrong pending entry."""
    first, second = echo_host(), echo_host()
    second.on("reconnect", lambda _p: {"type": "replay", "missing": [], "actions": []})
    factory = _ScriptedFactory(first, second)
    runtime = HostRuntime(
        HostConfig(factory, label="h", reconnect_policy=immediate_forever_policy())
    )
    await runtime.start()

    def _sent() -> list[int]:
        return [
            m["params"]["clientSeq"] for m in second.received if m.get("method") == "dispatchAction"
        ]

    for count in (1, 2, 3):
        runtime.client().dispatch(ROOT_URI, _bump(count))
    runtime._server_seq = 4

    await first.stop()
    await _spin(lambda: runtime.generation == 2)
    runtime.client().dispatch(ROOT_URI, _bump(9))
    await _spin(lambda: len(_sent()) == 4)

    assert _sent() == [1, 2, 3, 4]
    await runtime.shutdown()
    await factory.stop()


async def test_a_non_root_initial_subscription_survives_a_second_connection() -> None:
    """The regression: a bare non-root URI in `initial_subscriptions` was
    recorded with reducer name `""`, which `apply_snapshot` reads as a real
    name -- only `None` engages the shape-sniffing fallback -- so `bind()`
    raised KeyError on every reconnect that took a snapshot path, until the
    policy exhausted into `failed`. First connect hid it because the snapshots
    loop used to run before the dict was populated."""
    chat = "ahp-chat://c/pinned"

    def _snapshots(from_seq: int) -> list[dict[str, Any]]:
        return [
            {
                "resource": ROOT_URI,
                "state": {"agents": [], "activeSessions": 0},
                "fromSeq": from_seq,
            },
            {"resource": chat, "state": {"turns": [], "status": 1}, "fromSeq": from_seq},
        ]

    first, second = echo_host(server_seq=3), echo_host()
    first.on(
        "initialize",
        lambda _p: {
            "protocolVersion": "0.7.0",
            "serverSeq": 3,
            "serverInfo": {"name": "FakeHost", "version": "0"},
            "snapshots": _snapshots(3),
        },
    )
    second.on("reconnect", lambda _p: {"type": "snapshot", "snapshots": _snapshots(5)})
    factory = _ScriptedFactory(first, second)
    runtime = HostRuntime(
        HostConfig(
            factory,
            label="h",
            initial_subscriptions=(ROOT_URI, chat),
            reconnect_policy=immediate_forever_policy(),
        )
    )
    await runtime.start()
    assert runtime.mirror.channels[chat].reducer_name == "chat"  # sniffed from the shape

    await first.stop()
    await _spin(lambda: runtime.generation == 2)
    assert runtime.generation == 2
    assert runtime.state.status == "connected"
    assert runtime.mirror.channels[chat].reducer_name == "chat"
    assert runtime.mirror.state(chat) == {"turns": [], "status": 1}
    await runtime.shutdown()
    await factory.stop()


async def test_a_stated_channel_kind_binds_a_snapshot_sniffing_would_decline() -> None:
    """The `(uri, reducer_name)` form of `initial_subscriptions` is the caller
    stating the kind it already knows -- invariant 1's preferred source. An
    empty snapshot is genuinely ambiguous and the fallback declines it, so only
    the stated kind can bind this channel at all."""
    chat = "ahp-chat://c/empty"
    host = echo_host()
    host.on(
        "initialize",
        lambda _p: {
            "protocolVersion": "0.7.0",
            "serverSeq": 0,
            "serverInfo": {"name": "FakeHost", "version": "0"},
            "snapshots": [{"resource": chat, "state": {}, "fromSeq": 0}],
        },
    )
    factory = _ScriptedFactory(host)
    runtime = HostRuntime(HostConfig(factory, label="h", initial_subscriptions=((chat, "chat"),)))
    await runtime.start()
    assert runtime.mirror.channels[chat].reducer_name == "chat"
    assert runtime.mirror.confirmed(chat) == {}
    await runtime.shutdown()
    await factory.stop()


async def test_a_subscription_made_while_the_reconnect_was_in_flight_survives_it() -> None:
    """Plan section 6.3: the snapshot arm keeps a URI iff surviving *or not
    prior*. One subscribed while the reconnect RPC was in flight cannot be in
    the host's answer, and pruning it would silently discard the recorded
    intent `subscribe()` promises the next handshake will carry. Only what the
    host was actually asked about is the host's to decline -- and that half
    still prunes."""
    doomed = "ahp-chat://c/declined"
    added = "ahp-chat://c/in-flight"
    first, second = echo_host(), echo_host()
    first.on(
        "subscribe",
        lambda p: {
            "snapshot": {
                "resource": p["channel"],
                "state": {"turns": [], "status": 1},
                "fromSeq": 1,
            }
        },
    )
    reconnect_started = asyncio.Event()
    release = asyncio.Event()

    async def held_reconnect(_p: Any) -> Any:
        reconnect_started.set()
        await release.wait()
        # The host resumes root only: `doomed` was asked about and declined.
        return {
            "type": "snapshot",
            "snapshots": [
                {"resource": ROOT_URI, "state": {"agents": [], "activeSessions": 0}, "fromSeq": 9}
            ],
        }

    second.on("reconnect", held_reconnect)
    factory = _ScriptedFactory(first, second)
    runtime = HostRuntime(
        HostConfig(factory, label="h", reconnect_policy=immediate_forever_policy())
    )
    await runtime.start()
    await runtime.subscribe(doomed, "chat")
    runtime._server_seq = 4  # history to resume

    await first.stop()
    await asyncio.wait_for(reconnect_started.wait(), 5)
    with pytest.raises(HostNotConnected):
        await runtime.subscribe(added, "chat")  # records intent, then raises
    release.set()

    await _spin(lambda: runtime.generation == 2)
    assert runtime.generation == 2
    assert runtime.subscribed(added)
    assert added in runtime.mirror.channels  # bound, its snapshot rides the next handshake
    assert not runtime.subscribed(doomed)
    assert doomed not in runtime.mirror.channels
    await runtime.shutdown()
    await factory.stop()


# ── the auth re-check ────────────────────────────────────────────────────────


async def test_the_auth_check_reruns_after_every_reconnect_before_connected() -> None:
    """Plan section 6.3: "Reconnect must re-check authentication."
    `auth/required` is ephemeral and never replayed (spec authentication.md,
    Auth Expiry), and only the supervisor knows a reconnect happened -- so the
    hook runs against every fresh client, before the state flips to connected,
    and a caller released by `start(wait=True)` is never handed a connection
    nobody re-verified."""
    statuses: list[str] = []

    async def check(_client: Any) -> None:
        statuses.append(runtime.state.status)

    factory = _Factory()
    runtime = HostRuntime(
        HostConfig(
            factory, label="h", auth_check=check, reconnect_policy=immediate_forever_policy()
        )
    )
    await runtime.start()
    assert statuses == ["connecting"]  # ran, and before `connected`

    await factory.hosts[0].stop()
    await _spin(lambda: runtime.generation == 2)
    assert runtime.generation == 2
    # Re-ran against the fresh client, and still before the flip: the observed
    # status is whatever the attempt is labelled, never `connected`.
    assert len(statuses) == 2
    assert "connected" not in statuses
    await runtime.shutdown()
    await factory.stop()


async def test_a_failing_auth_check_fails_the_attempt_like_any_other_refusal() -> None:
    """A failure here is classified by the reconnect policy, not special-cased:
    the attempt dies before `connected` flips, and the supervisor dials again
    or reaches `failed` exactly as it would for a refused handshake."""
    calls = 0

    async def check(_client: Any) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("token expired")

    factory = _Factory()
    runtime = HostRuntime(
        HostConfig(
            factory, label="h", auth_check=check, reconnect_policy=immediate_forever_policy()
        )
    )
    await runtime.start()
    assert calls == 2
    assert runtime.state.status == "connected"
    await runtime.shutdown()
    await factory.stop()


# ── aborting an attempt ──────────────────────────────────────────────────────


async def test_reconnect_now_aborts_a_hung_dial() -> None:
    """Plan section 6.3's opening step: the link comes FIRST, and every await
    in the attempt races it. Without that, `reconnect_now()` against a
    black-holed host only takes effect once the dial resolves on its own --
    which is never."""
    dialing = asyncio.Event()
    black_hole = asyncio.Event()  # never set
    attempts = 0
    hosts: list[FakeHost] = []

    async def factory() -> Transport:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            dialing.set()
            await black_hole.wait()
        host = echo_host()
        await host.start()
        hosts.append(host)
        return host.transport()

    runtime = HostRuntime(
        HostConfig(factory, label="h", reconnect_policy=immediate_forever_policy())
    )
    starting = asyncio.ensure_future(runtime.start())
    await asyncio.wait_for(dialing.wait(), 5)
    await runtime.reconnect_now()
    await asyncio.wait_for(starting, 5)

    assert runtime.state.status == "connected"
    assert attempts == 2
    await runtime.shutdown()
    for host in hosts:
        await host.stop()


# ── the cached session catalog ───────────────────────────────────────────────


def _summary() -> dict[str, Any]:
    return {
        "resource": "echo:/s1",
        "provider": "echo",
        "title": "New Session",
        "status": 1,
        "createdAt": "2026-08-02T18:00:00.000Z",
        "modifiedAt": "2026-08-02T18:00:00.000Z",
    }


async def test_the_cached_session_list_follows_root_session_summary_changed() -> None:
    """`session_summaries` is seeded from `listSessions` and is exactly the cache
    this notification exists to keep current -- it is what lets a client stay in
    sync "without having to subscribe to every session URI individually". The one
    consumer of the feature was the one place it was dropped."""
    session = "echo:/s1"
    host = echo_host()
    host.on("listSessions", lambda _p: {"items": [_summary()]})
    factory = _ScriptedFactory(host)
    runtime = HostRuntime(HostConfig(factory, label="h"))
    await runtime.start()
    assert runtime.session_summaries[session]["title"] == "New Session"

    await host.notify(
        "root/sessionSummaryChanged",
        {
            "channel": ROOT_URI,
            "session": session,
            "changes": {
                "title": "please run the tool",
                "modifiedAt": "2026-08-02T18:50:12.258Z",
                # Identity fields "MUST be omitted by senders; receivers SHOULD
                # ignore them if present" -- a sender that sends them anyway is
                # the sender whose values are wrong.
                "provider": "not-echo",
            },
        },
    )
    await _spin(lambda: runtime.session_summaries[session]["title"] == "please run the tool")

    cached = runtime.session_summaries[session]
    assert cached["title"] == "please run the tool"
    assert cached["modifiedAt"] == "2026-08-02T18:50:12.258Z"
    assert cached["provider"] == "echo"
    assert cached["status"] == 1  # merged, not replaced
    await runtime.shutdown()
    await factory.stop()


async def test_a_bare_partial_change_does_not_clobber_the_rest_of_the_summary() -> None:
    """The host sends genuine partials -- a lone `{"status": 8}` while a turn
    runs -- so this is a merge, not a replace."""
    session = "echo:/s1"
    host = echo_host()
    host.on("listSessions", lambda _p: {"items": [_summary()]})
    factory = _ScriptedFactory(host)
    runtime = HostRuntime(HostConfig(factory, label="h"))
    await runtime.start()

    await host.notify(
        "root/sessionSummaryChanged",
        {"channel": ROOT_URI, "session": session, "changes": {"status": 8}},
    )
    await _spin(lambda: runtime.session_summaries[session]["status"] == 8)

    assert runtime.session_summaries[session] == _summary() | {"status": 8}
    await runtime.shutdown()
    await factory.stop()


async def test_a_summary_change_for_an_unknown_session_is_ignored() -> None:
    """ "Clients that have no cached entry for `session` MAY ignore the
    notification; it is not a substitute for `root/sessionAdded`." Inventing an
    entry from a partial publishes a summary with no `resource` or `provider`."""
    host = echo_host()
    factory = _ScriptedFactory(host)
    runtime = HostRuntime(HostConfig(factory, label="h"))
    await runtime.start()

    await host.notify(
        "root/sessionSummaryChanged",
        {"channel": ROOT_URI, "session": "echo:/never-listed", "changes": {"title": "ghost"}},
    )
    await _spin(lambda: bool(runtime.session_summaries), ticks=20)

    assert runtime.session_summaries == {}
    await runtime.shutdown()
    await factory.stop()
