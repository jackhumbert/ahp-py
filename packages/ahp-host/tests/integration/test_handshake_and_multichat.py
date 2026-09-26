"""The handshake and multi-chat defects the Python client's own probes found.

Every one is reachable only from a peer that sends something the *typed* client
cannot construct -- a duplicated subscription list, a `limit` that is a JSON
boolean, a `chat/turnCancelled` whose `turnId` names nothing, a chat source with
no `turnId` -- which is exactly why none of them had a test. They are all
schema-valid JSON, and a host on a socket has no say in who dials it.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from agent_host_protocol.channels import ROOT_URI
from agent_host_protocol.transport import memory_pair

from agent_host_server.core import Host, LoopbackSingleUserPolicy
from agent_host_server.provider import EchoProvider
from tests.conformance.schemas import validate_against

from .test_host_end_to_end import FakeClient

pytestmark = pytest.mark.anyio

_FORKABLE = {"multipleChats": {"fork": True, "sideChat": True}}
_TELEMETRY = {"logs": "ahp-otlp://logs"}


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
async def host() -> AsyncIterator[Host]:
    made = Host(EchoProvider(capabilities=_FORKABLE, delay=0.4), LoopbackSingleUserPolicy())
    try:
        yield made
    finally:
        await made.aclose()


async def _connect(host: Host, client_id: str = "c1") -> FakeClient:
    """A connection that has finished `initialize`."""
    client_transport, server_transport = memory_pair()
    task = asyncio.create_task(host.serve(server_transport))
    client = FakeClient(client_transport)
    client.serve_task = task  # type: ignore[attr-defined]
    await client.request(
        "initialize",
        {
            "channel": ROOT_URI,
            "clientId": client_id,
            "protocolVersions": ["0.7.0"],
            "initialSubscriptions": [ROOT_URI],
        },
    )
    return client


async def _session(host: Host, client: FakeClient, uri: str) -> str:
    """Create a session and subscribe to it and its default chat."""
    await client.request("createSession", {"channel": uri, "provider": "echo"})
    await client.collect_until(
        lambda: bool((host.sequencer.state_of(uri) or {}).get("defaultChat")), timeout=10.0
    )
    state = (await client.request("subscribe", {"channel": uri}))["result"]["snapshot"]["state"]
    chat: str = state["defaultChat"]
    await client.request("subscribe", {"channel": chat})
    return chat


async def _turn(client: FakeClient, chat: str, *, text: str, turn: str, seq: int = 1) -> None:
    await client.notify(
        "dispatchAction",
        {
            "channel": chat,
            "clientSeq": seq,
            "action": {
                "type": "chat/turnStarted",
                "turnId": turn,
                "startedAt": "1970-01-01T00:00:01.000Z",
                "message": {"text": text, "origin": {"kind": "user"}},
            },
        },
    )


def _state(host: Host, uri: str) -> dict[str, Any]:
    state = host.sequencer.state_of(uri)
    assert isinstance(state, dict)
    return state


def _rejections(client: FakeClient) -> list[str]:
    return [
        note["params"]["rejectionReason"]
        for note in client.notifications
        if note.get("method") == "action" and note["params"].get("rejectionReason")
    ]


def _turn_state(host: Host, chat: str, turn: str) -> str | None:
    """How *turn* ended, or ``None`` while it has not.

    A turn is moved out of `activeTurn` into `turns` only by `_end_turn`, and
    the record it writes there carries the final state -- so this answering at
    all means the turn is over, and what it answers is how it went.
    """
    for record in (host.sequencer.state_of(chat) or {}).get("turns", []):
        if record.get("id") == turn:
            state: str | None = record.get("state")
            return state
    return None


async def _started(
    host: Host, client: FakeClient, chat: str, turn: str, *, timeout: float = 10.0
) -> None:
    """Wait for *turn* to be the chat's ACTIVE turn.

    Load-bearing rather than decorative: a cancel that arrives before the turn
    is active is refused as "no active turn to cancel", which is not the
    rejection these tests are about, so the fixed wait this replaces was a race
    whose failure mode was the right assertion answered by the wrong reason.
    """
    await client.collect_until(
        lambda: ((host.sequencer.state_of(chat) or {}).get("activeTurn") or {}).get("id") == turn,
        timeout=timeout,
    )


async def _settled(
    host: Host, client: FakeClient, chat: str, turn: str, *, timeout: float = 10.0
) -> None:
    """Wait for *turn* to have ended, however it ended.

    `createChat` refuses a source turn that has not landed, so the fixed waits
    this replaces were races whose failure mode was an error response the
    caller then asserted past.
    """
    await client.collect_until(lambda: _turn_state(host, chat, turn) is not None, timeout=timeout)


def _echoed(client: FakeClient, chat: str, action_type: str) -> bool:
    """Whether the echo of a dispatched *action_type* is in the client's hands."""
    return any(action["action"].get("type") == action_type for action in client.actions(chat))


async def _cancel_answered(
    host: Host, client: FakeClient, chat: str, turn: str, *, timeout: float = 10.0
) -> None:
    """Wait for the cancel's echo to be in hand AND for *turn* to have ended.

    Both halves matter. `rejectionReason` rides on the echo, which the
    sequencer enqueues at dispatch and long before the turn finishes -- so a
    wait that watched only the host's own state could return with the echo
    still sitting in the transport, and an assertion about rejections would
    then be answered by an empty list rather than by what the host said.
    """
    await client.collect_until(
        lambda: (
            _echoed(client, chat, "chat/turnCancelled")
            and _turn_state(host, chat, turn) is not None
        ),
        timeout=timeout,
    )


class TestReconnectSubscriptionsAreDeduplicated:
    """`ReconnectParams.subscriptions` is `array of URI`, not a set. Repeating
    one URI N times returned N copies of every missed envelope -- same
    `serverSeq` each time -- so a conformant mirror folded each delta N times
    and a 50 KB request was answered with 62 MB."""

    async def test_a_repeated_uri_replays_each_envelope_once(self, host: Host) -> None:
        client = await _connect(host)
        chat = await _session(host, client, "echo:/dup-1")
        await _turn(client, chat, text="hello", turn="t1")
        await _settled(host, client, chat, "t1")

        result = (
            await client.request(
                "reconnect",
                {
                    "channel": ROOT_URI,
                    "clientId": "c1",
                    "lastSeenServerSeq": 0,
                    "subscriptions": [chat] * 6,
                },
            )
        )["result"]

        assert result["type"] == "replay"
        seqs = [envelope["serverSeq"] for envelope in result["actions"]]
        assert seqs == sorted(set(seqs)), "an envelope was replayed more than once"

    async def test_a_repeated_uri_is_reported_missing_once(self, host: Host) -> None:
        client = await _connect(host)
        result = (
            await client.request(
                "reconnect",
                {
                    "channel": ROOT_URI,
                    "clientId": "c1",
                    "lastSeenServerSeq": 0,
                    "subscriptions": ["echo:/gone", "echo:/gone"],
                },
            )
        )["result"]
        assert result["missing"] == ["echo:/gone"]


class TestAChannelReportedMissingIsNotDelivered:
    """The host was saying "drop this" and staying subscribed. Session and chat
    URIs are CLIENT-chosen, so the URI it disowned can be created later -- by
    someone else -- and the stale subscriber then receives a different client's
    traffic on a channel it was told did not exist."""

    async def test_the_uri_is_not_delivered_when_it_is_later_created(self, host: Host) -> None:
        owner = await _connect(host, "owner")
        stale = await _connect(host, "stale")
        uri = "echo:/later"

        result = (
            await stale.request(
                "reconnect",
                {
                    "channel": ROOT_URI,
                    "clientId": "stale",
                    "lastSeenServerSeq": 0,
                    "subscriptions": [uri],
                },
            )
        )["result"]
        assert result["missing"] == [uri]

        await owner.request("createSession", {"channel": uri, "provider": "echo"})
        # Fixed on purpose: the assertion is that nothing arrives, and a
        # condition wait would return the instant it found nothing -- which is
        # immediately, and proves nothing.
        await stale.collect(seconds=0.4)
        assert not stale.actions(uri)

    async def test_a_live_telemetry_channel_still_resumes(self) -> None:
        """The carve-out. A telemetry channel carries no state, so `replay`
        cannot resume it -- but it is not gone, and `reconnect` returns no
        `telemetry` map for a client to re-read, so calling it missing would
        strand it for the life of the connection."""
        host = Host(EchoProvider(), LoopbackSingleUserPolicy(), telemetry=_TELEMETRY)
        try:
            client = await _connect(host, "otlp")
            result = (
                await client.request(
                    "reconnect",
                    {
                        "channel": ROOT_URI,
                        "clientId": "otlp",
                        "lastSeenServerSeq": 0,
                        "subscriptions": ["ahp-otlp://logs"],
                    },
                )
            )["result"]
            assert result["missing"] == []

            await host.emit_telemetry("logs", {"resourceLogs": []})
            await client.collect_until(
                lambda: any(n.get("method") == "otlp/exportLogs" for n in client.notifications),
                timeout=10.0,
            )
            assert [n for n in client.notifications if n.get("method") == "otlp/exportLogs"]
        finally:
            await host.aclose()


class TestProtocolVersionsIsValidated:
    async def test_a_non_string_entry_is_invalid_params(self, host: Host) -> None:
        """`negotiate` hands every entry to `re.match`, so one integer in a
        peer-controlled array became -32603 with a raw Python `TypeError` --
        the host blaming itself for a schema-invalid request."""
        client_transport, server_transport = memory_pair()
        serve = asyncio.create_task(host.serve(server_transport))
        try:
            client = FakeClient(client_transport)
            response = await client.request(
                "initialize",
                {"channel": ROOT_URI, "clientId": "c", "protocolVersions": [1, "0.7.0"]},
            )
        finally:
            serve.cancel()
        assert response["error"]["code"] == -32602
        assert "TypeError" not in response["error"]["message"]


class TestListSessionsLimit:
    """`PaginatedParams.limit` is typed `number`, not `integer`, and
    `isinstance(limit, int)` got both halves of that wrong: `3.0` -- an ordinary
    JSON number -- was ignored, and `true` was honoured as a page size of 1."""

    @pytest.fixture
    async def catalogue(self, host: Host) -> FakeClient:
        client = await _connect(host)
        for index in range(4):
            await client.request(
                "createSession", {"channel": f"echo:/page-{index}", "provider": "echo"}
            )
        return client

    async def test_a_whole_float_is_honoured(self, catalogue: FakeClient) -> None:
        result = (await catalogue.request("listSessions", {"channel": ROOT_URI, "limit": 2.0}))[
            "result"
        ]
        assert len(result["items"]) == 2
        assert "nextCursor" in result

    async def test_a_boolean_is_not_a_page_size(self, catalogue: FakeClient) -> None:
        response = await catalogue.request("listSessions", {"channel": ROOT_URI, "limit": True})
        assert response["error"]["code"] == -32602

    async def test_a_string_is_refused_rather_than_ignored(self, catalogue: FakeClient) -> None:
        response = await catalogue.request("listSessions", {"channel": ROOT_URI, "limit": "2"})
        assert response["error"]["code"] == -32602

    async def test_a_negative_limit_is_refused(self, catalogue: FakeClient) -> None:
        """Silently ignoring it meant an unbounded page; honouring it literally
        would slice entries off the END."""
        response = await catalogue.request("listSessions", {"channel": ROOT_URI, "limit": -1})
        assert response["error"]["code"] == -32602

    async def test_an_oversized_limit_is_capped(self, catalogue: FakeClient) -> None:
        """ "The server ... MAY impose its own upper cap." """
        result = (await catalogue.request("listSessions", {"channel": ROOT_URI, "limit": 10_000}))[
            "result"
        ]
        assert len(result["items"]) == 4


class TestCancelMustNameTheActiveTurn:
    """`_end_turn` no-ops unless `turnId` matches, while `_react` killed the
    task unconditionally. So a late cancel -- or the `turnId`-less one the
    Python client sends -- aborted the turn and left the host's own state
    saying it was still running, which no later action could clear."""

    async def test_a_turn_id_naming_nothing_is_rejected(self, host: Host) -> None:
        client = await _connect(host)
        chat = await _session(host, client, "echo:/cancel-a")
        await _turn(client, chat, text="hello", turn="real")
        await _started(host, client, chat, "real")
        client.notifications.clear()

        await client.notify(
            "dispatchAction",
            {
                "channel": chat,
                "clientSeq": 2,
                "action": {"type": "chat/turnCancelled", "turnId": "ghost", "duration": 1},
            },
        )
        await _cancel_answered(host, client, chat, "real")

        assert _rejections(client) == ["turnId does not name the active turn"]
        assert _state(host, chat).get("activeTurn") is None, "the turn was killed anyway"
        assert [t["state"] for t in _state(host, chat)["turns"]] == ["complete"]

    async def test_a_cancel_with_no_turn_id_is_rejected(self, host: Host) -> None:
        """`turnId` is required by the schema; the client omits it."""
        client = await _connect(host)
        chat = await _session(host, client, "echo:/cancel-b")
        await _turn(client, chat, text="hello", turn="real")
        await _started(host, client, chat, "real")
        client.notifications.clear()

        await client.notify(
            "dispatchAction",
            {
                "channel": chat,
                "clientSeq": 2,
                "action": {"type": "chat/turnCancelled", "duration": 1},
            },
        )
        await _cancel_answered(host, client, chat, "real")

        assert _rejections(client) == ["turnId does not name the active turn"]
        assert _state(host, chat).get("activeTurn") is None

    async def test_the_real_cancel_still_works(self, host: Host) -> None:
        """The gate must not be so tight that the legitimate cancel bounces."""
        client = await _connect(host)
        chat = await _session(host, client, "echo:/cancel-c")
        await _turn(client, chat, text="hello", turn="real")
        await _started(host, client, chat, "real")
        client.notifications.clear()

        await client.notify(
            "dispatchAction",
            {
                "channel": chat,
                "clientSeq": 2,
                "action": {"type": "chat/turnCancelled", "turnId": "real", "duration": 1},
            },
        )
        await _cancel_answered(host, client, chat, "real")

        assert not _rejections(client)
        assert _state(host, chat).get("activeTurn") is None
        assert [t["state"] for t in _state(host, chat)["turns"]] == ["cancelled"]


class TestChatSourceIsValidated:
    """`ForkChatSource` and `SideChatSource` both require `turnId`, and so do
    the two `ChatOrigin` variants they produce. Accepting a source without one
    published a `ChatOrigin` that fails the state schema -- which fails the
    whole `Snapshot.state` union for every client that validates."""

    async def _forked(self, host: Host, client: FakeClient, source: dict[str, Any]) -> Any:
        chat = f"ahp-chat:/{uuid.uuid4()}"
        response = await client.request(
            "createChat", {"channel": "echo:/src", "chat": chat, "source": source}
        )
        return chat, response

    async def test_a_fork_with_no_turn_id_is_refused(self, host: Host) -> None:
        client = await _connect(host)
        source_chat = await _session(host, client, "echo:/src")
        _, response = await self._forked(host, client, {"kind": "fork", "chat": source_chat})
        assert response["error"]["code"] == -32602

    async def test_an_accepted_origin_matches_the_schema(self, host: Host) -> None:
        client = await _connect(host)
        source_chat = await _session(host, client, "echo:/src")
        await _turn(client, source_chat, text="hello", turn="t1")
        await _settled(host, client, source_chat, "t1")

        chat, response = await self._forked(
            host, client, {"kind": "fork", "chat": source_chat, "turnId": "t1"}
        )
        assert "error" not in response
        assert not validate_against("state", "ChatOrigin", _state(host, chat)["origin"])

    async def test_a_side_chat_selection_with_empty_text_is_refused(self, host: Host) -> None:
        """`SideChatSelection.text` is required and "MUST be non-empty" -- and
        the host promises to preserve it exactly, so an unvalidated one is
        permanent."""
        client = await _connect(host)
        source_chat = await _session(host, client, "echo:/src")
        await _turn(client, source_chat, text="hello", turn="t1")
        await _settled(host, client, source_chat, "t1")

        _, response = await self._forked(
            host,
            client,
            {
                "kind": "sideChat",
                "chat": source_chat,
                "turnId": "t1",
                "selection": {"text": ""},
            },
        )
        assert response["error"]["code"] == -32602

    async def test_a_non_string_selection_is_refused(self, host: Host) -> None:
        client = await _connect(host)
        source_chat = await _session(host, client, "echo:/src")
        await _turn(client, source_chat, text="hello", turn="t1")
        await _settled(host, client, source_chat, "t1")

        _, response = await self._forked(
            host,
            client,
            {
                "kind": "sideChat",
                "chat": source_chat,
                "turnId": "t1",
                "selection": {"text": 123},
            },
        )
        assert response["error"]["code"] == -32602

    async def test_a_real_selection_is_preserved_exactly(self, host: Host) -> None:
        client = await _connect(host)
        source_chat = await _session(host, client, "echo:/src")
        await _turn(client, source_chat, text="hello", turn="t1")
        await _settled(host, client, source_chat, "t1")

        chat, response = await self._forked(
            host,
            client,
            {
                "kind": "sideChat",
                "chat": source_chat,
                "turnId": "t1",
                "selection": {"text": "the selected words", "responsePartId": "p1"},
            },
        )
        assert "error" not in response
        origin = _state(host, chat)["origin"]
        assert origin["selection"] == {"text": "the selected words", "responsePartId": "p1"}
        assert not validate_against("state", "ChatOrigin", origin)


class TestDisposeChatDuringATurn:
    """The channel was dropped out from under a running turn: the agent kept
    working on a chat nobody could see, and anything it was parked on stayed
    advertised in `session/inputNeeded` -- pinning the SESSION at InputNeeded
    with a request that can never be answered, because the only channel it may
    be answered on no longer exists."""

    @pytest.fixture
    async def asking(self) -> AsyncIterator[Host]:
        made = Host(
            EchoProvider(capabilities=_FORKABLE, elicit=True, delay=0.2),
            LoopbackSingleUserPolicy(),
        )
        try:
            yield made
        finally:
            await made.aclose()

    async def _second_chat(self, host: Host, client: FakeClient, uri: str) -> str:
        await _session(host, client, uri)
        second = f"ahp-chat:/{uuid.uuid4()}"
        await client.request("createChat", {"channel": uri, "chat": second})
        await client.request("subscribe", {"channel": second})
        return second

    async def test_the_turn_is_cancelled(self, host: Host) -> None:
        client = await _connect(host)
        second = await self._second_chat(host, client, "echo:/dispose-a")
        await _turn(client, second, text="hello", turn="doomed")
        session = host._sessions["echo:/dispose-a"]
        await client.collect_until(lambda: bool(session.running(second)), timeout=10.0)

        assert session.running(second), "the turn never started"
        await client.request("disposeChat", {"channel": second})
        assert not session.running(second)

    async def test_its_input_request_is_retracted(self, asking: Host) -> None:
        client = await _connect(asking)
        second = await self._second_chat(asking, client, "echo:/dispose-b")
        await _turn(client, second, text="hello", turn="doomed")
        await client.collect_until(
            lambda: bool(_state(asking, "echo:/dispose-b").get("inputNeeded")), timeout=10.0
        )
        assert _state(asking, "echo:/dispose-b").get("inputNeeded"), "the provider never parked"

        await client.request("disposeChat", {"channel": second})
        # Not a negative assertion: the request was advertised a moment ago, so
        # this waits for the RETRACTION -- a transition to empty, which cannot
        # be satisfied by looking too early.
        await client.collect_until(
            lambda: _state(asking, "echo:/dispose-b").get("inputNeeded", []) == [], timeout=10.0
        )
        assert _state(asking, "echo:/dispose-b").get("inputNeeded", []) == []
