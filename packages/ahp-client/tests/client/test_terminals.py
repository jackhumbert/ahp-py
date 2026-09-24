"""The terminal surface: the claim, the stream, the exit, and the ``!`` prefix.

The terminal channel is the one place the state is *contested*, and the interop
run found the shape of that: a rejected `terminal/claimed` being applied by every
non-originating subscriber, after which each of them believed the wrong peer
owned the terminal with nothing that could ever correct it. So the assertions
here are mostly about what does **not** happen -- an un-echoed claim that confers
no rights, a refusal that changes no state, an input echo that appends nothing.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from typing import Any

import pytest
from agent_host_protocol.conformance.corpus import CORPUS_ROOT
from agent_host_protocol.types import ACTION_TYPES

from agent_host_client.api import connect
from agent_host_client.api.terminals import (
    _BY_TYPE,
    _NOT_MODELLED,
    ClientClaim,
    SessionClaim,
    TerminalClaimed,
    TerminalCommandExecuted,
    TerminalCommandFinished,
    TerminalExited,
    TerminalNotHeld,
    TerminalOutput,
    TerminalRefused,
    claim_from_wire,
    split_terminal_command,
    terminal_uris,
)
from agent_host_client.client.errors import AhpClientError, InvalidArgument, RequestTimeout
from agent_host_client.testing import FakeHost
from agent_host_client.testing.fake_host import FakeRpcError

from ._sibling import requires_sibling_pty

#: A VS Code-shaped URI, deliberately: the scheme the shared layer's display-only
#: `classify()` does NOT recognise. Every test here that applies an action proves
#: the reducer was bound from the kind we passed, not sniffed from this string.
TERMINAL = "agenthost-terminal:/1"

SCHEMA = json.loads((CORPUS_ROOT / "schema" / "commands.schema.json").read_text(encoding="utf-8"))


def _terminal_host(state: dict[str, Any] | None = None, **kwargs: Any) -> FakeHost:
    host = FakeHost(agents=[{"provider": "echo", "displayName": "Echo"}], **kwargs)
    body = (
        state
        if state is not None
        else {
            "title": "bash",
            "content": [],
            "claim": {"kind": "client", "clientId": "me"},
            "isPty": True,
        }
    )

    def subscribe(params: dict[str, Any]) -> dict[str, Any]:
        channel = params["channel"]
        return {
            "snapshot": {
                "resource": channel,
                "state": host.root_state if channel == "ahp-root://" else body,
                "fromSeq": host._server_seq,
            }
        }

    host.on("initialize", lambda params: _initialize(host, params))
    host.on("ping", lambda _p: {})
    host.on("listSessions", lambda _p: {"items": []})
    host.on("subscribe", subscribe)
    host.on("createTerminal", lambda _p: {})
    host.on("disposeTerminal", lambda _p: {})
    return host


def _initialize(host: FakeHost, params: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {
        "protocolVersion": host.protocol_version,
        "serverSeq": host._server_seq,
        "snapshots": [
            {"resource": uri, "state": host.root_state, "fromSeq": host._server_seq}
            for uri in params.get("initialSubscriptions") or []
            if uri == "ahp-root://"
        ],
    }
    if host.terminal_command_prefix:
        result["terminalCommandPrefix"] = host.terminal_command_prefix
    return result


def _sent(host: FakeHost, method: str) -> list[dict[str, Any]]:
    return [m["params"] for m in host.received if m.get("method") == method]


def _dispatched(host: FakeHost, action_type: str) -> list[dict[str, Any]]:
    return [
        m["params"]["action"]
        for m in host.received
        if m.get("method") == "dispatchAction" and m["params"]["action"]["type"] == action_type
    ]


async def _settle(predicate: Any, timeout: float = 2.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition never became true")


# ── createTerminal ───────────────────────────────────────────────────────────


async def test_the_claim_is_required_and_defaults_to_this_client() -> None:
    """`CreateTerminalParams` requires `claim`, and the only claim that lets the
    caller type is its own: a session claim is held by no client at all."""
    required = SCHEMA["$defs"]["CreateTerminalParams"]["required"]
    assert "claim" in required
    assert "channel" in required

    host = _terminal_host()
    await host.start()
    async with connect(transport=host.transport(), client_id="me") as client:
        terminal = await client.create_terminal(name="bash", cols=100, rows=30)
        params = _sent(host, "createTerminal")[0]
        assert params["claim"] == {"kind": "client", "clientId": "me"}
        assert params["channel"] == terminal.uri
        assert (params["cols"], params["rows"], params["name"]) == (100, 30, "bash")
        assert terminal.held_by_us
    await host.stop()


async def test_absent_optionals_are_omitted_rather_than_sent_as_null() -> None:
    """`json.dumps` writes `null` where `JSON.stringify` drops the key, and a
    host reading `cwd: null` is reading a different document to one with no cwd."""
    host = _terminal_host()
    await host.start()
    async with connect(transport=host.transport(), client_id="me") as client:
        await client.create_terminal()
        params = _sent(host, "createTerminal")[0]
        assert set(params) == {"channel", "claim"}
    await host.stop()


async def test_the_reducer_is_bound_by_kind_and_never_from_the_scheme() -> None:
    """VS Code mints three `agenthost-terminal:` forms and the spec's examples a
    fourth. A scheme lookup binds no reducer, and the state then freezes silently
    while output keeps arriving -- so this asserts output actually lands."""
    host = _terminal_host()
    await host.start()
    async with connect(transport=host.transport(), client_id="me") as client:
        terminal = await client.open_terminal(TERMINAL)
        await host.push(TERMINAL, {"type": "terminal/data", "data": "hello"})
        await _settle(lambda: terminal.output() == "hello")
    await host.stop()


# ── the claim ────────────────────────────────────────────────────────────────


async def test_input_is_refused_locally_when_the_terminal_is_held_elsewhere() -> None:
    """`dispatchAction` is a notification, so a host refusing the keystrokes
    answers on an envelope stream nobody has to be reading. The symptom is
    "typing does nothing"; the exception names the holder instead."""
    host = _terminal_host(
        {"title": "t", "content": [], "claim": {"kind": "session", "session": "echo:/s"}}
    )
    await host.start()
    async with connect(transport=host.transport(), client_id="me") as client:
        terminal = await client.open_terminal(TERMINAL)
        assert not terminal.held_by_us
        with pytest.raises(TerminalNotHeld) as caught:
            terminal.write("ls\r")
        assert "session 'echo:/s'" in str(caught.value)
        assert _dispatched(host, "terminal/input") == []

        # The gate is the sibling host's default policy rather than an upstream
        # MUST, so there has to be a way past it.
        terminal.write("ls\r", force=True)
        await _settle(lambda: _dispatched(host, "terminal/input") != [])
        assert _dispatched(host, "terminal/input") == [{"type": "terminal/input", "data": "ls\r"}]
    await host.stop()


async def test_an_unechoed_claim_confers_no_rights() -> None:
    """`terminal/claimed` is an ARBITRATION. Reading the claim from optimistic
    state answers "you won" to a question the host has not decided yet -- and
    then the keystrokes it authorises are the ones that vanish."""
    host = _terminal_host(
        {"title": "t", "content": [], "claim": {"kind": "client", "clientId": "someone-else"}}
    )
    await host.start()
    async with connect(transport=host.transport(), client_id="me") as client:
        terminal = await client.open_terminal(TERMINAL)
        terminal.take()
        # Optimistic state already says we hold it; confirmed does not.
        assert terminal.state["claim"] == {"kind": "client", "clientId": "me"}
        assert not terminal.held_by_us
        with pytest.raises(TerminalNotHeld):
            terminal.write("x")

        await host.push(
            TERMINAL,
            {"type": "terminal/claimed", "claim": {"kind": "client", "clientId": "me"}},
            origin={"clientId": "me", "clientSeq": 1},
        )
        await _settle(lambda: terminal.held_by_us)
    await host.stop()


async def test_a_refused_claim_changes_no_state_and_reaches_the_stream() -> None:
    """The interop finding, both halves.

    The host fans a refusal out to every subscriber with its own state untouched,
    so no peer may apply it -- ours included. And the refusal has to be
    *reportable*, because for our own action it is the only answer to "why can I
    still not type".
    """
    host = _terminal_host(
        {"title": "t", "content": [], "claim": {"kind": "client", "clientId": "owner"}}
    )
    await host.start()
    async with connect(transport=host.transport(), client_id="me") as client:
        terminal = await client.open_terminal(TERMINAL)
        stream = terminal.events()
        async with stream:
            terminal.take()
            await host.push(
                TERMINAL,
                {"type": "terminal/claimed", "claim": {"kind": "client", "clientId": "me"}},
                origin={"clientId": "me", "clientSeq": 1},
                rejection="terminal/claimed from a peer that does not hold the terminal",
            )
            event = await asyncio.wait_for(stream.__anext__(), 2)
        assert isinstance(event, TerminalRefused)
        assert event.mine is True
        assert "does not hold" in event.reason
        # The optimistic effect is reverted and the owner is unchanged.
        assert terminal.claim == ClientClaim("owner")
        assert terminal.state["claim"] == {"kind": "client", "clientId": "owner"}
    await host.stop()


async def test_another_peers_refused_claim_is_news_not_state() -> None:
    """A non-originating subscriber applying the refusal is exactly the defect
    the run found: nothing later corrects it, so its owner is wrong forever."""
    host = _terminal_host(
        {"title": "t", "content": [], "claim": {"kind": "client", "clientId": "me"}}
    )
    await host.start()
    async with connect(transport=host.transport(), client_id="me") as client:
        terminal = await client.open_terminal(TERMINAL)
        stream = terminal.events()
        async with stream:
            await host.push(
                TERMINAL,
                {"type": "terminal/claimed", "claim": {"kind": "client", "clientId": "intruder"}},
                origin={"clientId": "intruder", "clientSeq": 4},
                rejection="terminal/claimed from a peer that does not hold the terminal",
            )
            event = await asyncio.wait_for(stream.__anext__(), 2)
        assert isinstance(event, TerminalRefused)
        assert event.mine is False
        assert terminal.held_by_us
    await host.stop()


async def test_handing_a_terminal_over_is_never_refused_locally() -> None:
    """Upstream disagrees with itself: `actions.ts` says a server SHOULD reject a
    claim from a peer that does not hold it, while the guide's detach flow has a
    client narrowing a *session's* claim it certainly never held. Enforcing the
    first here would break the second."""
    host = _terminal_host(
        {
            "title": "t",
            "content": [],
            "claim": {"kind": "session", "session": "echo:/s", "turnId": "t1", "toolCallId": "c1"},
        }
    )
    await host.start()
    async with connect(transport=host.transport(), client_id="me") as client:
        terminal = await client.open_terminal(TERMINAL)
        assert not terminal.held_by_us
        terminal.hand_to(SessionClaim("echo:/s"))
        await _settle(lambda: _dispatched(host, "terminal/claimed") != [])
        assert _dispatched(host, "terminal/claimed") == [
            {"type": "terminal/claimed", "claim": {"kind": "session", "session": "echo:/s"}}
        ]
    await host.stop()


def test_a_session_claim_omits_the_scope_it_does_not_have() -> None:
    """Absent, not null: `turnId` and `toolCallId` are optional, and an explicit
    null is a different document -- one an unconditional JS spread writes
    through, so the narrowed claim would not narrow anything."""
    assert SessionClaim("echo:/s").to_wire() == {"kind": "session", "session": "echo:/s"}
    assert SessionClaim("echo:/s", "t1").to_wire() == {
        "kind": "session",
        "session": "echo:/s",
        "turnId": "t1",
    }


def test_a_claim_with_the_wrong_types_is_not_a_claim() -> None:
    """`terminal/claimed` is client-dispatchable, so this payload is arbitrary
    peer JSON -- and a coerced `clientId` of 123 would become the claim of a
    client named "123", which is the comparison that guards somebody's shell."""
    assert claim_from_wire({"kind": "client", "clientId": 123}) is None
    assert claim_from_wire({"kind": "session"}) is None
    assert claim_from_wire({"kind": "other", "clientId": "me"}) is None
    assert claim_from_wire(None) is None
    assert claim_from_wire({"kind": "client", "clientId": "me"}) == ClientClaim("me")


# ── output, commands, exit ───────────────────────────────────────────────────


async def test_input_is_never_echoed_into_the_buffer() -> None:
    """`terminal/input` is side-effect-only and the reducer no-ops it, because
    the pty sends the same bytes back as `terminal/data`. A surface that also
    appended them would double every character on screen."""
    host = _terminal_host()
    await host.start()
    async with connect(transport=host.transport(), client_id="me") as client:
        terminal = await client.open_terminal(TERMINAL)
        terminal.write("ls\r")
        await host.push(TERMINAL, {"type": "terminal/data", "data": "ls\r\n"})
        await _settle(lambda: terminal.output() == "ls\r\n")
    await host.stop()


async def test_output_is_reconstructed_across_typed_content_parts() -> None:
    """The schema's own reconstruction: `p.type === 'command' ? p.output : p.value`.

    Including for a part type from a newer spec, which contributes its `value`
    exactly as the JavaScript does. The one difference is the part carrying
    neither field, where JS splices the literal "undefined" into the VT stream.
    """
    host = _terminal_host(
        {
            "title": "t",
            "claim": {"kind": "client", "clientId": "me"},
            "content": [
                {"type": "unclassified", "value": "$ "},
                {
                    "type": "command",
                    "commandId": "c1",
                    "commandLine": "ls",
                    "output": "a\nb\n",
                    "timestamp": 1,
                    "isComplete": True,
                },
                {"type": "somethingNewer", "value": "!"},
                {"type": "somethingNewerStill"},
            ],
        }
    )
    await host.start()
    async with connect(transport=host.transport(), client_id="me") as client:
        terminal = await client.open_terminal(TERMINAL)
        assert terminal.output() == "$ a\nb\n!"
        assert [c.id for c in terminal.commands()] == ["c1"]
    await host.stop()


async def test_command_detection_is_a_flag_and_not_the_presence_of_a_part() -> None:
    """ "Clients MUST check this flag before relying on command detection. Do NOT
    use the presence of a `command` part as a feature flag" -- parts are absent
    in the ordinary idle state, so the obvious test reports no support for a
    shell that simply has not run anything yet."""
    host = _terminal_host()
    await host.start()
    async with connect(transport=host.transport(), client_id="me") as client:
        terminal = await client.open_terminal(TERMINAL)
        assert not terminal.supports_command_detection
        assert terminal.commands() == []
        await host.push(TERMINAL, {"type": "terminal/commandDetectionAvailable"})
        await _settle(lambda: terminal.supports_command_detection)
        assert terminal.commands() == []
    await host.stop()


async def test_a_command_reports_its_exit_code_and_duration() -> None:
    """The difference between a terminal widget and a byte pipe."""
    host = _terminal_host()
    await host.start()
    async with connect(transport=host.transport(), client_id="me") as client:
        terminal = await client.open_terminal(TERMINAL)
        events: list[Any] = []
        stream = terminal.events()
        async with stream:
            await host.push(
                TERMINAL,
                {
                    "type": "terminal/commandExecuted",
                    "commandId": "c1",
                    "commandLine": "ls -l",
                    "timestamp": 1730000000000,
                },
            )
            await host.push(TERMINAL, {"type": "terminal/data", "data": "total 0\n"})
            await host.push(
                TERMINAL,
                {
                    "type": "terminal/commandFinished",
                    "commandId": "c1",
                    "exitCode": 2,
                    "durationMs": 41,
                },
            )
            for _ in range(3):
                events.append(await asyncio.wait_for(stream.__anext__(), 2))

    executed, output, finished = events
    assert isinstance(executed, TerminalCommandExecuted)
    assert (executed.command_id, executed.command_line) == ("c1", "ls -l")
    assert executed.timestamp == 1730000000000
    assert isinstance(output, TerminalOutput)
    assert output.data == "total 0\n"
    assert isinstance(finished, TerminalCommandFinished)
    assert (finished.exit_code, finished.duration_ms) == (2, 41.0)
    await host.stop()


def test_a_missing_duration_is_not_a_zero_duration() -> None:
    """The client renders `finish(exitCode, durationMs)` with a `?? 0` fallback,
    so a host that reports nothing made every command look instantaneous. `None`
    keeps "not reported" and "took no time" apart at this end too."""
    finished = TerminalCommandFinished({"action": {"type": "terminal/commandFinished"}})
    assert finished.duration_ms is None
    assert finished.exit_code is None


async def test_a_codeless_exit_is_in_the_state_since_0_9() -> None:
    """Before 0.9.0 this was a protocol hole: `terminal/exited` spread an
    omitted `exitCode` onto the state, `undefined` deleted the key, and a
    codeless exit left the state identical to a running terminal. 0.9.0 made
    the exit a `lifecycle`, so it is readable with or without a code."""
    host = _terminal_host()
    await host.start()
    async with connect(transport=host.transport(), client_id="me") as client:
        terminal = await client.open_terminal(TERMINAL)
        assert not terminal.exit_reported

        stream = terminal.events()
        async with stream:
            await host.push(TERMINAL, {"type": "terminal/exited"})
            event = await asyncio.wait_for(stream.__anext__(), 2)
        assert isinstance(event, TerminalExited)
        assert event.exit_code is None
        await _settle(lambda: terminal.exit_reported)
        assert terminal.state["lifecycle"] == {"status": "exited"}
        assert terminal.exit_code is None

        await host.push(TERMINAL, {"type": "terminal/exited", "exitCode": 0})
        await _settle(lambda: terminal.exit_code == 0)
    await host.stop()


async def test_wait_for_exit_returns_the_code_and_times_out_loudly() -> None:
    host = _terminal_host()
    await host.start()
    async with connect(transport=host.transport(), client_id="me") as client:
        terminal = await client.open_terminal(TERMINAL)
        with pytest.raises(RequestTimeout):
            await terminal.wait_for_exit(timeout=0.05)

        async def exit_soon() -> None:
            await asyncio.sleep(0.02)
            await host.push(TERMINAL, {"type": "terminal/exited", "exitCode": 7})

        task = asyncio.create_task(exit_soon())
        assert await asyncio.wait_for(terminal.wait_for_exit(timeout=2), 3) == 7
        await task
    await host.stop()


async def test_an_exit_that_lands_during_setup_is_not_missed() -> None:
    """The stream is attached before the state is read, so an exit arriving in
    the gap is caught by the reader rather than lost between the two."""
    host = _terminal_host()
    await host.start()
    async with connect(transport=host.transport(), client_id="me") as client:
        terminal = await client.open_terminal(TERMINAL)
        await host.push(TERMINAL, {"type": "terminal/exited", "exitCode": 3})
        await _settle(lambda: terminal.exit_reported)
        assert await asyncio.wait_for(terminal.wait_for_exit(timeout=1), 2) == 3
    await host.stop()


# ── disposal ─────────────────────────────────────────────────────────────────


async def test_a_terminal_we_created_is_disposed_on_exit_and_one_we_opened_is_not() -> None:
    """Disposal kills the process. Doing that to another client's terminal on a
    `with` exit is the kind of surprise that loses trust -- and the host does not
    stop us, because `disposeTerminal` is deliberately not claim-gated."""
    host = _terminal_host()
    await host.start()
    async with connect(transport=host.transport(), client_id="me") as client:
        async with await client.create_terminal() as mine:
            pass
        async with await client.open_terminal(TERMINAL):
            pass
        disposed = [p["channel"] for p in _sent(host, "disposeTerminal")]
        assert disposed == [mine.uri]
    await host.stop()


async def test_disposal_drops_the_subscription_too() -> None:
    """A channel the host has torn down still in our subscription set is one the
    supervisor re-requests on every reconnect, where it can only be refused."""
    host = _terminal_host()
    await host.start()
    async with connect(transport=host.transport(), client_id="me") as client:
        terminal = await client.create_terminal()
        await terminal.dispose()
        assert client.mirror.state(terminal.uri) is None
    await host.stop()


# ── the catalogue ────────────────────────────────────────────────────────────


async def test_the_root_catalogue_is_readable_without_subscribing() -> None:
    """`TerminalInfo` carries the claim, which is what a client reads to decide
    whether to offer an input box -- so a tab strip renders without subscribing
    to every terminal."""
    host = _terminal_host()
    await host.start()
    async with connect(transport=host.transport(), client_id="me") as client:
        await host.push(
            "ahp-root://",
            {
                "type": "root/terminalsChanged",
                "terminals": [
                    {
                        "resource": TERMINAL,
                        "title": "bash",
                        "claim": {"kind": "client", "clientId": "me"},
                    }
                ],
            },
        )
        await _settle(lambda: len(client.terminals()) == 1)
        assert client.terminals()[0].title == "bash"
        assert client.terminals()[0].held_by_us is True
    await host.stop()


# ── the `!` shorthand ────────────────────────────────────────────────────────


async def test_the_bang_shorthand_follows_the_negotiated_prefix() -> None:
    """ "Absence means the host does not support command prefixes." A client that
    hardcodes `!` offers the shortcut to hosts that never claimed it."""
    host = _terminal_host(terminal_command_prefix="!")
    await host.start()
    async with connect(transport=host.transport(), client_id="me") as client:
        assert client.terminal_command_prefix == "!"
        assert client.terminal_command("!ls -l") == "ls -l"
        assert client.terminal_command("ls -l") is None
    await host.stop()

    silent = _terminal_host()
    await silent.start()
    async with connect(transport=silent.transport(), client_id="me") as client:
        assert client.terminal_command_prefix is None
        assert client.terminal_command("!ls") is None
    await silent.stop()


def test_a_bare_prefix_is_a_message_not_a_command() -> None:
    """The sibling host strips the remainder and treats an empty one as an
    ordinary message, so `!` alone reaches the agent. Reporting it as a command
    puts a "will run" badge on something a model is about to answer."""
    assert split_terminal_command("!", "!") is None
    assert split_terminal_command("!   ", "!") is None
    assert split_terminal_command("!  ls  ", "!") == "ls"
    assert split_terminal_command("!ls", None) is None
    assert split_terminal_command(">>build", ">>") == "build"


# ── the event map ────────────────────────────────────────────────────────────


def test_every_terminal_action_is_modelled_or_deliberately_omitted() -> None:
    """An invented key fails exactly the way a missing one does -- silently, as
    `UnknownTerminalEvent` -- so the map is held against the generated table
    rather than against a reading of it."""
    upstream = {t for t in ACTION_TYPES if t.startswith("terminal/")}
    assert upstream, "the generated action table carries no terminal actions"
    assert set(_BY_TYPE) | _NOT_MODELLED == upstream


def test_a_rejected_envelope_never_decodes_as_the_action_it_names() -> None:
    """Decoded by type first, a refused `terminal/claimed` becomes a
    `TerminalClaimed` -- telling the caller the terminal changed hands when the
    host's own state says it did not."""
    from agent_host_client.api.terminals import terminal_event_for

    envelope = {
        "channel": TERMINAL,
        "action": {"type": "terminal/claimed", "claim": {"kind": "client", "clientId": "me"}},
        "origin": {"clientId": "me", "clientSeq": 1},
        "rejectionReason": "nope",
    }
    event = terminal_event_for(envelope, client_id="me")
    assert isinstance(event, TerminalRefused)
    assert not isinstance(event, TerminalClaimed)


# ── against the sibling host, with a real pty ────────────────────────────────


@requires_sibling_pty
async def test_a_real_terminal_against_the_sibling_host() -> None:
    """**Not independent evidence** -- both peers share the reducers -- but the
    claim is arbitrated by the host, and nothing else exercises that.

    Runs a real shell. Every path here disposes it, and every wait is bounded: a
    leaked pty outlives the test process, reparents to init, and keeps a shell
    running that nothing can name.
    """
    from agent_host_protocol.transport import memory_pair
    from agent_host_server.core import Host, LoopbackSingleUserPolicy
    from agent_host_server.core.pty_backend import PtyTerminalBackend
    from agent_host_server.provider import EchoProvider

    host = Host(
        EchoProvider(),
        LoopbackSingleUserPolicy(),
        terminals=PtyTerminalBackend(shell="/bin/sh", default_cwd="/tmp"),
    )
    client_side, host_side = memory_pair()
    served = asyncio.get_running_loop().create_task(host.serve(host_side))
    try:
        async with connect(transport=client_side, client_id="probe") as client:
            # Advertised only behind a real backend, which is the whole reason
            # the prefix is negotiated rather than assumed.
            assert client.terminal_command_prefix == "!"
            assert client.terminal_command("!echo hi") == "echo hi"

            terminal = await client.create_terminal(name="probe", cols=80, rows=24)
            try:
                assert terminal.held_by_us
                assert terminal.is_pty
                terminal.write("echo marker-97\n")
                await _settle(lambda: "marker-97" in terminal.output(), timeout=10)

                stream = terminal.events()
                async with stream:
                    terminal.hand_to(
                        SessionClaim("echo:/probe", "turn-1", "call-1", chat="ahp-chat:/probe")
                    )
                    await _settle(lambda: not terminal.held_by_us, timeout=10)
                    with pytest.raises(TerminalNotHeld):
                        terminal.write("whoami\n")

                    # The arbitration itself: having given the terminal to a
                    # session, this client cannot take it back -- and learns so
                    # from the stream rather than from silence.
                    terminal.take()
                    refusal = await asyncio.wait_for(_first_refusal(stream), 10)
                assert refusal.mine is True
                assert terminal.claim == SessionClaim(
                    "echo:/probe", "turn-1", "call-1", chat="ahp-chat:/probe"
                )
            finally:
                # Not claim-gated, deliberately: this is the disposal that a gate
                # would have made impossible, and the shell would outlive us.
                await asyncio.wait_for(terminal.dispose(), 10)
            assert client.terminals() == []

            exiter = await client.create_terminal(name="exiter")
            try:
                exiter.write("exit 7\n")
                assert await exiter.wait_for_exit(timeout=15) == 7
            finally:
                await asyncio.wait_for(exiter.dispose(), 10)
    finally:
        served.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await served
        await host.aclose()


async def _first_refusal(stream: Any) -> Any:
    async for event in stream:
        if isinstance(event, TerminalRefused):
            return event
    raise AssertionError("the stream ended before the refusal arrived")


@requires_sibling_pty
async def test_the_bang_shorthand_runs_on_the_host_and_not_on_the_agent() -> None:
    """The affordance the surface is for, end to end.

    The host never hands the message to the provider: it runs the remainder and
    reports it back as a tool call named `terminal`, which is why the client
    needs no second code path for a `!` turn -- only a way to know in advance.
    """
    from agent_host_protocol.transport import memory_pair
    from agent_host_server.core import Host, LoopbackSingleUserPolicy
    from agent_host_server.core.pty_backend import PtyTerminalBackend
    from agent_host_server.provider import EchoProvider

    from agent_host_client.api.events import ToolCallStarted

    host = Host(
        EchoProvider(),
        LoopbackSingleUserPolicy(),
        terminals=PtyTerminalBackend(shell="/bin/sh", default_cwd="/tmp"),
    )
    client_side, host_side = memory_pair()
    served = asyncio.get_running_loop().create_task(host.serve(host_side))
    try:
        async with (
            connect(transport=client_side, client_id="probe") as client,
            await client.create_session(provider="echo", cwd="/tmp") as session,
        ):
            command = client.terminal_command("!echo bang-works")
            assert command == "echo bang-works"
            names = [
                event.tool_name
                async for event in session.prompt("!echo bang-works", idle_timeout=20.0)
                if isinstance(event, ToolCallStarted)
            ]
            assert names == ["terminal"]
    finally:
        served.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await served
        await host.aclose()


# ── what the reviews caught ──────────────────────────────────────────────────


async def test_a_refused_input_reaches_the_stream() -> None:
    """The headline case, and it was the broken one.

    `terminal/input` is one of the two actions the host gates on the claim, and
    the *only* one whose refusal has no other channel: `dispatchAction` is a
    notification with no reply. Filtering the stream by action type ran before
    the refusal check and destroyed it -- so the surface named after "typing does
    nothing" delivered nothing when typing did nothing.
    """
    host = _terminal_host()
    await host.start()
    async with connect(transport=host.transport(), client_id="me") as client:
        terminal = await client.open_terminal(TERMINAL)
        stream = terminal.events()
        async with stream:
            await host.push(
                TERMINAL,
                {"type": "terminal/input", "data": "whoami\n"},
                origin={"clientId": "me", "clientSeq": 1},
                rejection="terminal/input requires holding the terminal's claim",
            )
            event = await asyncio.wait_for(stream.__anext__(), 2)
        assert isinstance(event, TerminalRefused)
        assert event.mine is True
        assert "requires holding" in event.reason
    await host.stop()


async def test_an_accepted_input_echo_is_still_silent() -> None:
    """The other half of the same filter: an accepted echo is our own keystrokes
    and the pty sends the same bytes back as `terminal/data`, so surfacing it
    would double every character a caller renders."""
    host = _terminal_host()
    await host.start()
    async with connect(transport=host.transport(), client_id="me") as client:
        terminal = await client.open_terminal(TERMINAL)
        stream = terminal.events()
        async with stream:
            await host.push(TERMINAL, {"type": "terminal/input", "data": "x"})
            await host.push(TERMINAL, {"type": "terminal/data", "data": "x"})
            event = await asyncio.wait_for(stream.__anext__(), 2)
        assert isinstance(event, TerminalOutput)
    await host.stop()


async def test_json_numbers_are_not_narrowed_to_python_ints() -> None:
    """`cols`, `rows` and `exitCode` are all declared `"type": "number"`, so a
    host serialising `30` as `30.0` is conformant. Reading them with
    `isinstance(raw, int)` made a legal size read as `(0, 0)` and a clean
    `exit 7` read as "killed without a code"."""
    host = _terminal_host()
    await host.start()
    async with connect(transport=host.transport(), client_id="me") as client:
        terminal = await client.open_terminal(TERMINAL)
        await host.push(TERMINAL, {"type": "terminal/resized", "cols": 100.0, "rows": 30.0})
        await _settle(lambda: terminal.size == (100, 30))
        await host.push(TERMINAL, {"type": "terminal/exited", "exitCode": 7.0})
        await _settle(lambda: terminal.exit_reported)
        assert terminal.exit_code == 7
    await host.stop()


async def test_outbound_dimensions_must_be_whole_cells() -> None:
    """The client is a producer of these too. `960 / 12` is a float, is
    schema-legal, and the sibling host drops it -- so the pty silently runs at
    the backend default with nothing raised and nothing on the stream."""
    host = _terminal_host()
    await host.start()
    async with connect(transport=host.transport(), client_id="me") as client:
        terminal = await client.open_terminal(TERMINAL)
        terminal.resize(960 / 12, 48 / 2)
        await _settle(lambda: _dispatched(host, "terminal/resized"))
        assert _dispatched(host, "terminal/resized")[-1] == {
            "type": "terminal/resized",
            "cols": 80,
            "rows": 24,
        }
        with pytest.raises(AhpClientError, match="whole number"):
            terminal.resize(80.5, 24)
        with pytest.raises(AhpClientError, match="positive"):
            terminal.resize(0, 24)
    await host.stop()


async def test_mine_asks_one_question_on_every_arm_of_the_union() -> None:
    """A single field name on a union must ask a single question. `mine` meant
    "I originated it" on a refusal and "I am the new holder" on a claim, so
    `case ...(mine=True)` over `TerminalEvent` changed meaning per arm."""
    host = _terminal_host()
    await host.start()
    async with connect(transport=host.transport(), client_id="me") as client:
        terminal = await client.open_terminal(TERMINAL)
        stream = terminal.events()
        async with stream:
            await host.push(
                TERMINAL,
                {"type": "terminal/claimed", "claim": {"kind": "client", "clientId": "me"}},
                origin={"clientId": "someone-else", "clientSeq": 1},
            )
            event = await asyncio.wait_for(stream.__anext__(), 2)
        assert isinstance(event, TerminalClaimed)
        assert event.mine is False  # a peer dispatched it
        assert event.held_by_us is True  # and handed it to us
    await host.stop()


def test_an_empty_claim_is_refused_before_it_strands_a_terminal() -> None:
    """`terminal/claimed` is itself claim-gated, so a terminal handed to a
    `clientId` no connection has can never be taken back by any peer: disposal
    is the only operation left. One unset config value reaches it."""
    with pytest.raises(InvalidArgument, match="clientId"):
        ClientClaim("")
    with pytest.raises(InvalidArgument, match="session URI"):
        SessionClaim("")
    # And the same value arriving from a peer is unreadable rather than a claim
    # that compares equal to a real connection.
    assert claim_from_wire({"kind": "client", "clientId": ""}) is None


async def test_a_blank_rename_is_refused() -> None:
    """`TerminalState.title` is required and `""` is schema-valid, so it reaches
    every subscriber's catalogue as a nameless tab -- which is the case the
    sibling host guards on *creation* with `name or default`. Renaming walked
    around that guard."""
    host = _terminal_host()
    await host.start()
    async with connect(transport=host.transport(), client_id="me") as client:
        terminal = await client.open_terminal(TERMINAL)
        with pytest.raises(InvalidArgument, match="may not be empty"):
            terminal.rename("")
        assert _dispatched(host, "terminal/titleChanged") == []
    await host.stop()


async def test_a_disposed_terminal_names_its_state_instead_of_guessing() -> None:
    """`TerminalState.claim` is required, so a state with no claim is never a
    malformed claim -- it is a channel this client is not receiving. Collapsing
    the two reported "held by an unreadable claim" for the most ordinary
    lifecycle event there is, and kept accepting `write()` into the dropped
    channel."""
    host = _terminal_host()
    await host.start()
    async with connect(transport=host.transport(), client_id="me") as client:
        terminal = await client.create_terminal(name="bash")
        assert terminal.alive is True
        await terminal.dispose()
        assert terminal.alive is False
        assert "not subscribed" in terminal.holder
        with pytest.raises(AhpClientError, match="not subscribed"):
            terminal.write("ls\n")
    await host.stop()


async def test_a_second_handle_survives_the_first_ones_release() -> None:
    """Two handles on one channel is the ordinary case -- `open_terminal` mints a
    fresh object per call and does not memoise -- and an unrefcounted
    unsubscribe blinded the survivor with no error anywhere."""
    host = _terminal_host()
    await host.start()
    async with connect(transport=host.transport(), client_id="me") as client:
        first = await client.open_terminal(TERMINAL)
        second = await client.open_terminal(TERMINAL)
        async with second:
            pass
        assert first.alive is True
        assert first.title == "bash"
    await host.stop()


async def test_wait_for_exit_ends_when_a_peer_disposes_the_terminal() -> None:
    """A disposal publishes no `terminal/exited` -- the channel simply stops --
    so a wait with the old `timeout=None` default blocked forever on an event
    that could no longer arrive."""
    host = _terminal_host()
    await host.start()
    async with connect(transport=host.transport(), client_id="me") as client:
        terminal = await client.open_terminal(TERMINAL)
        await host.push(
            "ahp-root://",
            {
                "type": "root/terminalsChanged",
                "terminals": [
                    {
                        "resource": TERMINAL,
                        "title": "bash",
                        "claim": {"kind": "client", "clientId": "me"},
                    }
                ],
            },
        )
        await _settle(lambda: terminal.exists)

        async def peer_disposes() -> None:
            await asyncio.sleep(0.02)
            await host.push("ahp-root://", {"type": "root/terminalsChanged", "terminals": []})

        task = asyncio.create_task(peer_disposes())
        with pytest.raises(AhpClientError, match="disposed by another peer"):
            await asyncio.wait_for(terminal.wait_for_exit(timeout=5), 3)
        await task
    await host.stop()


async def test_create_terminal_disposes_what_it_cannot_subscribe() -> None:
    """The URI is minted inside `create_terminal`, so a caller who did not pass
    one cannot name the shell it just started. Leaving it behind runs it until
    the host stops -- the immortal-terminal failure disposal is ungated to
    avoid."""
    host = _terminal_host()

    def refuse(_params: dict[str, Any]) -> dict[str, Any]:
        raise FakeRpcError({"code": -32009, "message": "no"})

    await host.start()
    async with connect(transport=host.transport(), client_id="me") as client:
        host.on("subscribe", refuse)
        with pytest.raises(Exception, match="no"):
            await client.create_terminal(name="orphan")
        disposed = [
            m["params"]["channel"] for m in host.received if m.get("method") == "disposeTerminal"
        ]
        created = [
            m["params"]["channel"] for m in host.received if m.get("method") == "createTerminal"
        ]
        assert disposed == created
    await host.stop()


async def test_a_context_manager_raises_a_disposal_it_could_not_perform() -> None:
    """`__aexit__` suppressed every exception unconditionally, with no exception
    in flight to protect: the `with` block promised to end the shell, the host
    refused, and nothing anywhere said so."""
    host = _terminal_host()

    def refuse(_params: dict[str, Any]) -> dict[str, Any]:
        raise FakeRpcError({"code": -32009, "message": "not yours"})

    await host.start()
    async with connect(transport=host.transport(), client_id="me") as client:
        terminal = await client.create_terminal(name="bash")
        host.on("disposeTerminal", refuse)
        with pytest.raises(Exception, match="not yours"):
            async with terminal:
                pass
        # The subscription still goes, so a failed disposal does not also leave a
        # channel the runtime re-requests on every reconnect.
        assert terminal.alive is False
    await host.stop()


async def test_is_pty_defaults_to_the_recoverable_answer() -> None:
    """`isPty` is optional. Running a VT parser over plain text is a no-op;
    printing an unparsed pty stream shows the user literal `ESC[0m`."""
    host = _terminal_host(
        {"title": "t", "content": [], "claim": {"kind": "client", "clientId": "me"}}
    )
    await host.start()
    async with connect(transport=host.transport(), client_id="me") as client:
        terminal = await client.open_terminal(TERMINAL)
        assert terminal.is_pty is True
        await host.push(TERMINAL, {"type": "terminal/data", "data": "x"})
    await host.stop()


def test_terminal_uris_finds_the_receiver_assigned_channel() -> None:
    """`ToolResultTerminalContent.resource` is the one terminal URI the *host*
    assigns -- "clients can subscribe to the terminal's URI to stream its output
    in real time" -- and it arrives inside a tool call's content parts."""
    content = [
        {"type": "text", "value": "running"},
        {"type": "terminal", "resource": "agenthost-terminal:/tool-1"},
        {"type": "terminal"},
    ]
    assert terminal_uris(content) == ["agenthost-terminal:/tool-1"]


async def test_the_fan_in_tap_reports_what_it_drops() -> None:
    """The runtime's `events()` queue is bounded and had no `on_drop`, so a
    reader slower than a flooding pty was fast-forwarded past its own
    `TerminalRefused` with nothing anywhere saying so. `AhpClient` wires the
    identical queue to a diagnostic; this one did not."""
    host = _terminal_host()
    await host.start()
    async with connect(transport=host.transport(), client_id="me") as client:
        seen: list[Any] = []
        reader = client._runtime.diagnostics()
        queue = client._runtime._events
        reading = asyncio.create_task(_collect(reader, seen))
        await asyncio.sleep(0)
        events = client._runtime.events()  # a cursor to fall behind
        for _ in range(4200):
            queue.publish(None)  # type: ignore[arg-type]
        await _settle(lambda: any(getattr(d, "stream", "") == "host-events" for d in seen))
        await events.aclose()
        reading.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await reading
        await reader.aclose()
    await host.stop()


async def _collect(reader: Any, sink: list[Any]) -> None:
    async for item in reader:
        sink.append(item)


def test_the_whole_terminal_event_family_is_importable_from_the_top_level() -> None:
    """A caller writing `case TerminalResized()` must not need a different
    import path than the siblings the README example uses -- the terminal
    event family is one union, exported as one."""
    import agent_host_client as pkg

    for name in (
        "TerminalClaimed",
        "TerminalCleared",
        "TerminalCommandDetected",
        "TerminalCommandExecuted",
        "TerminalCommandFinished",
        "TerminalCwdChanged",
        "TerminalExited",
        "TerminalNotHeld",
        "TerminalOutput",
        "TerminalRefused",
        "TerminalResized",
        "TerminalTitleChanged",
        "UnknownTerminalEvent",
    ):
        assert hasattr(pkg, name), name
        assert name in pkg.__all__, name
