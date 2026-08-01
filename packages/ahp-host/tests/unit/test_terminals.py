"""Terminal claims, escape-sequence stripping, and the scrollback cap.

None of this is covered by the fixture corpus, and all of it fails in ways that
look like something else.

The parser is fed by a pty, which means chunk boundaries fall wherever the
kernel decided -- routinely inside an escape sequence and inside a codepoint. A
parser that only works on whole sequences passes every hand-written test and then
leaks `\\x1b]633;C` into a client's "did this command produce output" check the
first time a build writes fast enough. So the boundary cases are parametrised
over *every* split point rather than a chosen one, and the adversarial cases --
a sequence that never terminates, an ESC that starts nothing, a body that
contains a byte someone might mistake for a terminator -- get their own tests,
because each one has a failure mode that consumes the rest of the stream
silently.

The claim tests are a matrix rather than prose because the interesting cells are
the ones upstream disagrees about: `actions.ts:74` says only the holder may
re-claim, and the guide has a client narrowing a claim it never held.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

import pytest

import agent_host_server.core.terminals
from agent_host_server.core.errors import AhpError
from agent_host_server.core.terminals import (
    CLAIM_GATED_ACTIONS,
    STRICT_CLAIM_GATED_ACTIONS,
    CommandFinished,
    CommandLine,
    CommandStart,
    CwdReported,
    OutputItem,
    ParsedOutput,
    RefusingTerminalBackend,
    ShellIntegrationParser,
    TerminalBackend,
    TerminalClaim,
    TerminalClientClaim,
    TerminalRequest,
    TerminalSessionClaim,
    claim_from_wire,
    holds_claim,
    same_owner,
    terminal_dispatch_rejection,
    trim_scrollback,
)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


#: One complete VS Code command lifecycle, using both string terminators (BEL
#: and ESC-backslash) because shells emit both.
LIFECYCLE = (
    b"\x1b]633;E;echo\\x20hi\x07"
    b"\x1b]633;C\x07"
    b"hi\r\n"
    b"\x1b]633;D;0\x1b\\"
    b"\x1b]633;P;Cwd=/tmp\x07"
    b"prompt$ "
)

LIFECYCLE_EVENTS = (
    CommandLine("echo hi"),
    CommandStart(),
    CommandFinished(0),
    CwdReported("/tmp"),
)


def _feed(*chunks: bytes, parser: ShellIntegrationParser | None = None) -> ParsedOutput:
    """Feed every chunk to one parser and concatenate what came back."""
    parser = parser if parser is not None else ShellIntegrationParser()
    items: tuple[OutputItem, ...] = ()
    for chunk in chunks:
        items += parser.feed(chunk).items
    items += parser.flush().items
    return ParsedOutput(items)


class TestReadBoundaries:
    """A sequence split across two reads must parse as if it never was."""

    def test_the_whole_stream_at_once_is_the_reference(self) -> None:
        parsed = _feed(LIFECYCLE)
        assert parsed.text == "hi\r\nprompt$ "
        assert parsed.events == LIFECYCLE_EVENTS

    @pytest.mark.parametrize("split", range(len(LIFECYCLE) + 1))
    def test_every_split_point_gives_the_same_result(self, split: int) -> None:
        parsed = _feed(LIFECYCLE[:split], LIFECYCLE[split:])
        assert parsed.text == "hi\r\nprompt$ "
        assert parsed.events == LIFECYCLE_EVENTS

    def test_one_byte_at_a_time_gives_the_same_result(self) -> None:
        parsed = _feed(*(LIFECYCLE[i : i + 1] for i in range(len(LIFECYCLE))))
        assert parsed.text == "hi\r\nprompt$ "
        assert parsed.events == LIFECYCLE_EVENTS

    def test_a_half_read_sequence_emits_nothing_until_it_terminates(self) -> None:
        """The failure this prevents is emitting the body as text and *then*
        emitting the event when the terminator arrives -- output twice."""
        parser = ShellIntegrationParser()
        assert parser.feed(b"before\x1b]633;C").items == ("before",)
        assert parser.feed(b"\x07after").items == (CommandStart(), "after")

    def test_a_read_ending_on_a_bare_escape_holds_it_back(self) -> None:
        parser = ShellIntegrationParser()
        assert parser.feed(b"abc\x1b").text == "abc"
        assert parser.feed(b"]633;C\x07").events == (CommandStart(),)

    def test_flush_releases_an_escape_the_process_never_finished(self) -> None:
        """A pty that exits mid-sequence must not take the last bytes with it."""
        parser = ShellIntegrationParser()
        parser.feed(b"tail\x1b")
        assert parser.flush().text == "\x1b"


class TestUtf8:
    """Stripping is a byte operation; text is not."""

    @pytest.mark.parametrize("split", range(len("héllo 🌍".encode()) + 1))
    def test_a_multibyte_character_split_across_reads_survives(self, split: int) -> None:
        raw = "héllo 🌍".encode()
        assert _feed(raw[:split], raw[split:]).text == "héllo 🌍"

    def test_a_partial_codepoint_is_buffered_not_replaced(self) -> None:
        parser = ShellIntegrationParser()
        assert parser.feed("🌍".encode()[:2]).text == ""
        assert parser.feed("🌍".encode()[2:]).text == "🌍"

    def test_a_continuation_byte_is_not_a_string_terminator(self) -> None:
        """U+015C encodes as `c5 9c`, and `9c` is the C1 string terminator. A
        parser that honours C1 in a UTF-8 stream ends the sequence inside a
        character and spills the remainder as garbage."""
        parsed = _feed("\x1b]0;tŜtle\x07rest".encode())
        assert parsed.text == "\x1b]0;tŜtle\x07rest"
        assert parsed.events == ()

    def test_undecodable_bytes_do_not_raise(self) -> None:
        """`cat` of a binary is a thing users do inside a terminal."""
        assert _feed(b"\xff\xfe ok").text.endswith(" ok")


class TestSequencesThatNeverTerminate:
    """An OSC with no terminator must not eat the rest of the terminal."""

    def test_an_unterminated_sequence_is_abandoned_at_the_cap(self) -> None:
        parser = ShellIntegrationParser(max_osc_body=16)
        parsed = parser.feed(b"\x1b]633;E;" + b"A" * 20)
        assert parsed.text == "\x1b]633;E;" + "A" * 20, "the bytes must not vanish"
        assert parsed.events == ()

    def test_the_stream_still_parses_after_an_abandoned_sequence(self) -> None:
        parser = ShellIntegrationParser(max_osc_body=16)
        parser.feed(b"\x1b]633;E;" + b"A" * 20)
        parsed = parser.feed(b"out\x1b]633;C\x07more")
        assert parsed.text == "outmore"
        assert parsed.events == (CommandStart(),)

    def test_abandonment_is_byte_exact(self) -> None:
        """The pass-through path relies on it: an iTerm2 inline image is far
        larger than the buffer cap and must still reach the client whole."""
        image = b"\x1b]1337;File=" + b"Zm9v" * 64 + b"\x07"
        assert _feed(image, parser=ShellIntegrationParser(max_osc_body=32)).text == image.decode()

    def test_flush_does_not_wait_for_a_terminator(self) -> None:
        parser = ShellIntegrationParser()
        parser.feed(b"\x1b]633;E;partial")
        assert parser.flush().text == "\x1b]633;E;partial"


class TestThingsThatLookLikeEscapes:
    def test_an_escape_inside_the_body_abandons_the_sequence(self) -> None:
        """`ESC` not followed by `\\` is not a string terminator. Treating it as
        one would apply a marker's meaning to a sequence that never closed."""
        parsed = _feed(b"x\x1b]633;C\x1b[0mafter")
        assert parsed.text == "x\x1b]633;C\x1b[0mafter"
        assert parsed.events == ()

    def test_a_near_miss_identifier_is_not_stripped(self) -> None:
        parsed = _feed(b"\x1b]6330;C\x07")
        assert parsed.text == "\x1b]6330;C\x07"
        assert parsed.events == ()

    def test_the_marker_text_without_an_escape_is_just_text(self) -> None:
        assert _feed(b"]633;C echo").text == "]633;C echo"

    def test_an_escape_that_introduces_something_else_passes_through(self) -> None:
        parsed = _feed(b"\x1b[31mred\x1b[0m\x1b(B")
        assert parsed.text == "\x1b[31mred\x1b[0m\x1b(B"
        assert parsed.events == ()

    def test_an_empty_osc_is_somebody_elses_sequence(self) -> None:
        """It has no identifier, so it is not ours to remove."""
        assert _feed(b"\x1b]\x07after").text == "\x1b]\x07after"


class TestCommandDetection:
    def test_vscode_reports_the_command_line_before_execution_starts(self) -> None:
        assert _feed(b"\x1b]633;E;ls\x07\x1b]633;C\x07").events == (
            CommandLine("ls"),
            CommandStart(),
        )

    def test_the_injection_nonce_is_dropped(self) -> None:
        """It authenticates the sequence; it belongs in no state a client reads."""
        assert _feed(b"\x1b]633;E;ls;a-secret-nonce\x07").events == (CommandLine("ls"),)

    @pytest.mark.parametrize(
        ("escaped", "expected"),
        [
            (rb"echo\x20hi", "echo hi"),
            (rb"a\x3bb", "a;b"),
            (rb"C:\\Users", "C:\\Users"),
            (rb"\xc3\xa9", "é"),
            (rb"trailing\\", "trailing\\"),
            (rb"not\zhex", "not\\zhex"),
        ],
    )
    def test_the_command_line_is_unescaped(self, escaped: bytes, expected: str) -> None:
        assert _feed(b"\x1b]633;E;" + escaped + b"\x07").events == (CommandLine(expected),)

    def test_finalterm_marks_are_recognised_too(self) -> None:
        parsed = _feed(b"\x1b]133;A\x07$ \x1b]133;B\x07\x1b]133;C\x07out\x1b]133;D;3\x07")
        assert parsed.text == "$ out"
        assert parsed.events == (CommandStart(), CommandFinished(3))

    def test_finalterm_carries_no_command_line(self) -> None:
        """Recovering it from the echoed keystrokes is emulator work, declined."""
        assert not any(isinstance(e, CommandLine) for e in _feed(b"\x1b]133;C\x07").events)

    @pytest.mark.parametrize(
        ("field", "expected"),
        [
            (b"0", 0),
            (b"127", 127),
            (b"-1", -1),
            (b"", None),
            (b"1_0", None),
            (b"0x10", None),
            (b" 7", None),
            (b"9" * 5000, None),
        ],
    )
    def test_the_exit_code_is_parsed_strictly(self, field: bytes, expected: int | None) -> None:
        """`int()` accepts underscore separators and non-ASCII digits, so a shell
        reporting `1_0` would become exit code 10 on a naive read."""
        assert _feed(b"\x1b]633;D;" + field + b"\x07").events == (CommandFinished(expected),)

    def test_a_non_ascii_digit_is_not_an_exit_code(self) -> None:
        assert _feed("\x1b]633;D;١٢\x07".encode()).events == (CommandFinished(None),)

    def test_a_finish_without_a_code_reports_none(self) -> None:
        assert _feed(b"\x1b]633;D\x07").events == (CommandFinished(None),)

    def test_unknown_subcommands_are_stripped_but_report_nothing(self) -> None:
        parsed = _feed(b"\x1b]633;A\x07\x1b]633;B\x07\x1b]633;SetMark\x07\x1b]633;Whatever\x07")
        assert parsed.text == ""
        assert parsed.events == ()


class TestWorkingDirectory:
    """Stripping a sequence must not destroy what it said."""

    def test_the_cwd_property_survives_being_stripped(self) -> None:
        parsed = _feed(b"\x1b]633;P;Cwd=/home/x\x07")
        assert parsed.text == ""
        assert parsed.events == (CwdReported("/home/x"),)

    def test_other_properties_report_nothing(self) -> None:
        assert _feed(b"\x1b]633;P;IsWindows=True\x07").events == ()


class TestPassThrough:
    """Only shell integration is stripped. Everything else is a client's job."""

    def test_a_title_sequence_survives(self) -> None:
        assert _feed(b"\x1b]0;a title\x07").text == "\x1b]0;a title\x07"

    def test_an_iterm_inline_image_survives(self) -> None:
        assert _feed(b"\x1b]1337;File=abc\x07").text == "\x1b]1337;File=abc\x07"

    def test_an_iterm_cwd_is_stripped_and_reported(self) -> None:
        parsed = _feed(b"\x1b]1337;CurrentDir=/srv\x07")
        assert parsed.text == ""
        assert parsed.events == (CwdReported("/srv"),)

    def test_an_iterm_cwd_is_not_run_through_the_vscode_unescaper(self) -> None:
        """Only VS Code escapes its values. `C:\\x41` is a Windows path, not an
        escape for `CA`."""
        assert _feed(b"\x1b]1337;CurrentDir=C:\\x41\x07").events == (CwdReported("C:\\x41"),)

    def test_an_iterm_shell_integration_banner_is_stripped(self) -> None:
        parsed = _feed(b"\x1b]1337;ShellIntegrationVersion=15;shell=zsh\x07")
        assert parsed.text == ""
        assert parsed.events == ()


class TestStreamOrder:
    """Flattening to "text plus events" appends output to the wrong content part."""

    def test_text_and_events_interleave(self) -> None:
        items = _feed(b"a\x1b]633;D;0\x07b\x1b]633;C\x07c").items
        assert items == ("a", CommandFinished(0), "b", CommandStart(), "c")

    def test_adjacent_text_is_coalesced(self) -> None:
        assert _feed(b"a\x1b]633;A\x07b").items == ("ab",)

    def test_reset_forgets_a_partial_sequence(self) -> None:
        parser = ShellIntegrationParser()
        parser.feed(b"\x1b]633;E;half")
        parser.reset()
        assert parser.feed(b"fresh").text == "fresh"


class TestClaimParsing:
    """`terminal/claimed` is client-dispatchable, so every claim is peer JSON."""

    @pytest.mark.parametrize(
        "claim",
        [
            TerminalClientClaim("client-a"),
            TerminalSessionClaim("ahp-session:/1"),
            TerminalSessionClaim("ahp-session:/1", "turn-2", "tool-3"),
        ],
    )
    def test_a_claim_round_trips_through_the_wire_shape(self, claim: TerminalClaim) -> None:
        assert claim_from_wire(claim.to_wire()) == claim

    def test_an_absent_turn_is_absent_on_the_wire_not_null(self) -> None:
        assert TerminalSessionClaim("s").to_wire() == {"kind": "session", "session": "s"}

    @pytest.mark.parametrize(
        "payload",
        [
            None,
            {},
            "client",
            {"kind": "client"},
            {"kind": "client", "clientId": 123},
            {"kind": "session"},
            {"kind": "session", "session": None},
            {"kind": 1, "clientId": "a"},
            {"kind": "Client", "clientId": "a"},
        ],
    )
    def test_a_malformed_claim_parses_to_none_rather_than_raising(self, payload: Any) -> None:
        assert claim_from_wire(payload) is None

    def test_a_non_string_id_does_not_become_a_client(self) -> None:
        """`clientId: 123` coerced to `"123"` would let a peer hold a terminal
        whose owner it merely resembles."""
        assert claim_from_wire({"kind": "client", "clientId": 123}) is None

    def test_extra_fields_are_ignored(self) -> None:
        assert claim_from_wire({"kind": "client", "clientId": "a", "future": 1}) == (
            TerminalClientClaim("a")
        )


class TestClaimOwnership:
    def test_only_the_named_client_holds_a_client_claim(self) -> None:
        assert holds_claim(TerminalClientClaim("a"), "a")
        assert not holds_claim(TerminalClientClaim("a"), "b")

    def test_no_client_holds_a_session_claim(self) -> None:
        """A session-owned terminal is the agent's, and takes input from nobody."""
        assert not holds_claim(TerminalSessionClaim("ahp-session:/1"), "a")

    def test_an_unparseable_claim_is_held_by_nobody(self) -> None:
        assert not holds_claim(None, "a")

    def test_turn_scope_does_not_change_the_owner(self) -> None:
        assert same_owner(TerminalSessionClaim("s", "turn-1", "tool-1"), TerminalSessionClaim("s"))

    def test_two_kinds_are_never_the_same_owner(self) -> None:
        assert not same_owner(TerminalClientClaim("a"), TerminalSessionClaim("a"))


class TestDispatchGating:
    HOLDER = TerminalClientClaim("client-a")
    OTHER = TerminalClientClaim("client-b")
    SESSION = TerminalSessionClaim("ahp-session:/1", "turn-1", "tool-1")

    @pytest.mark.parametrize(
        ("action_type", "claim", "allowed"),
        [
            ("terminal/input", HOLDER, True),
            ("terminal/input", OTHER, False),
            ("terminal/input", SESSION, False),
            ("terminal/input", None, False),
            # Not gated by default: protocol-legal from any peer.
            ("terminal/cleared", OTHER, True),
            ("terminal/resized", OTHER, True),
            ("terminal/titleChanged", OTHER, True),
            # Server-only, and refused before any claim is consulted.
            ("terminal/data", HOLDER, False),
            ("terminal/exited", HOLDER, False),
            ("terminal/commandExecuted", HOLDER, False),
            ("terminal/nonsense", HOLDER, False),
        ],
    )
    def test_the_default_matrix(
        self, action_type: str, claim: TerminalClaim | None, allowed: bool
    ) -> None:
        rejection = terminal_dispatch_rejection(
            {"type": action_type}, claim=claim, client_id="client-a"
        )
        assert (rejection is None) is allowed, rejection

    def test_an_action_without_a_type_is_rejected(self) -> None:
        assert terminal_dispatch_rejection({}, claim=self.HOLDER, client_id="client-a") is not None

    def test_the_rejection_names_the_action(self) -> None:
        """It is echoed to the client as `rejectionReason`, so it has to read."""
        rejection = terminal_dispatch_rejection(
            {"type": "terminal/input"}, claim=self.OTHER, client_id="client-a"
        )
        assert rejection is not None
        assert "terminal/input" in rejection

    def test_a_non_holder_may_not_take_a_terminal(self) -> None:
        rejection = terminal_dispatch_rejection(
            {"type": "terminal/claimed", "claim": TerminalClientClaim("client-a").to_wire()},
            claim=self.OTHER,
            client_id="client-a",
        )
        assert rejection is not None

    def test_the_holder_may_hand_a_terminal_anywhere(self) -> None:
        assert (
            terminal_dispatch_rejection(
                {"type": "terminal/claimed", "claim": self.SESSION.to_wire()},
                claim=self.HOLDER,
                client_id="client-a",
            )
            is None
        )

    def test_a_client_may_detach_a_session_it_does_not_hold(self) -> None:
        """The guide's "client detaches a terminal" flow: the claim narrows from
        {session, turnId, toolCallId} to {session}, dispatched by a client that
        never held it. Refusing this breaks a documented interaction."""
        assert (
            terminal_dispatch_rejection(
                {
                    "type": "terminal/claimed",
                    "claim": TerminalSessionClaim("ahp-session:/1").to_wire(),
                },
                claim=self.SESSION,
                client_id="client-a",
            )
            is None
        )

    def test_a_claim_that_would_brick_the_terminal_is_refused(self) -> None:
        """The reducer applies the payload verbatim. An unparseable claim in the
        state means every gated action is denied to everyone, forever."""
        rejection = terminal_dispatch_rejection(
            {"type": "terminal/claimed", "claim": {"kind": "client"}},
            claim=self.HOLDER,
            client_id="client-a",
        )
        assert rejection is not None

    def test_the_strict_posture_closes_the_scrollback_hole(self) -> None:
        """`terminal/cleared` is client-dispatchable, so by default any peer that
        can name the channel wipes another peer's scrollback."""
        action = {"type": "terminal/cleared"}
        assert terminal_dispatch_rejection(action, claim=self.OTHER, client_id="client-a") is None
        assert (
            terminal_dispatch_rejection(
                action,
                claim=self.OTHER,
                client_id="client-a",
                gated=STRICT_CLAIM_GATED_ACTIONS,
            )
            is not None
        )

    def test_the_strict_posture_still_admits_the_holder(self) -> None:
        for action_type in STRICT_CLAIM_GATED_ACTIONS - {"terminal/claimed"}:
            assert (
                terminal_dispatch_rejection(
                    {"type": action_type},
                    claim=self.HOLDER,
                    client_id="client-a",
                    gated=STRICT_CLAIM_GATED_ACTIONS,
                )
                is None
            ), action_type

    def test_the_gated_set_is_a_subset_of_the_strict_one(self) -> None:
        assert CLAIM_GATED_ACTIONS <= STRICT_CLAIM_GATED_ACTIONS


def _unclassified(text: str) -> dict[str, Any]:
    return {"type": "unclassified", "value": text}


def _command(command_id: str, output: str, *, complete: bool) -> dict[str, Any]:
    return {
        "type": "command",
        "commandId": command_id,
        "commandLine": "x",
        "output": output,
        "timestamp": 1,
        "isComplete": complete,
    }


class TestScrollback:
    """Trimming is silent by design; what it must never do is corrupt the buffer."""

    def test_content_under_the_cap_is_untouched(self) -> None:
        content = [_unclassified("short")]
        assert trim_scrollback(content, max_chars=100) == content

    def test_the_returned_list_is_not_the_input_list(self) -> None:
        content = [_unclassified("short")]
        assert trim_scrollback(content, max_chars=100) is not content

    def test_trimming_drops_the_oldest_parts_first(self) -> None:
        content = [_unclassified("a" * 100), _unclassified("b" * 100)]
        trimmed = trim_scrollback(content, max_chars=100)
        assert [part["value"] for part in trimmed] == ["b" * 100]

    def test_the_last_part_is_never_dropped(self) -> None:
        """`terminal/data` appends to the tail; losing it restarts the buffer
        with an unclassified run in the middle of a running command."""
        trimmed = trim_scrollback([_command("1", "z" * 100, complete=True)], max_chars=10)
        assert len(trimmed) == 1
        assert trimmed[0]["commandId"] == "1"

    def test_a_running_command_is_truncated_rather_than_dropped(self) -> None:
        """`terminal/commandFinished` matches by `commandId`. Drop the part and
        the finish completes nothing at all."""
        content = [_unclassified("a" * 100), _command("1", "b" * 100, complete=False)]
        trimmed = trim_scrollback(content, max_chars=50)
        assert [part["type"] for part in trimmed] == ["command"]
        assert trimmed[0]["commandId"] == "1"
        assert trimmed[0]["output"] == "b" * 50

    def test_a_finished_command_may_be_dropped(self) -> None:
        content = [_command("1", "a" * 100, complete=True), _unclassified("b" * 10)]
        assert [part["type"] for part in trim_scrollback(content, max_chars=10)] == ["unclassified"]

    def test_a_command_part_with_no_completion_flag_counts_as_running(self) -> None:
        """JavaScript truthiness, deliberately, matching the reducer's tail test."""
        running = {"type": "command", "commandId": "1", "output": "a" * 100}
        content = [running, _unclassified("b" * 10)]
        assert [part["type"] for part in trim_scrollback(content, max_chars=20)] == [
            "command",
            "unclassified",
        ]

    def test_trimming_to_nothing_keeps_nothing(self) -> None:
        """`text[-0:]` is the whole string. A negative slice silently keeps
        everything it was asked to discard."""
        trimmed = trim_scrollback([_unclassified("abc")], max_chars=0)
        assert trimmed[0]["value"] == ""

    def test_the_tail_is_preserved_exactly(self) -> None:
        """Trimming from the head keeps every client's next append aligned; the
        newest bytes are the ones anybody is looking at."""
        content = [_unclassified("old" * 50 + "NEWEST")]
        assert trim_scrollback(content, max_chars=6)[0]["value"] == "NEWEST"

    def test_a_part_type_from_a_newer_spec_is_never_deleted(self) -> None:
        content = [{"type": "somethingNew", "payload": 1}, _unclassified("b" * 100)]
        trimmed = trim_scrollback(content, max_chars=10)
        assert trimmed[0]["type"] == "somethingNew"

    def test_a_malformed_part_does_not_raise(self) -> None:
        assert trim_scrollback([None, "text", 7, _unclassified("a" * 50)], max_chars=10)


class TestRefusingBackend:
    """No backend ships in this distribution, and that has to be a stated answer."""

    def test_it_satisfies_the_backend_protocol(self) -> None:
        assert isinstance(RefusingTerminalBackend(), TerminalBackend)

    @pytest.mark.anyio
    async def test_it_refuses_every_create(self) -> None:
        backend = RefusingTerminalBackend()
        request = TerminalRequest("ahp-terminal:/1", TerminalClientClaim("client-a"))
        with pytest.raises(AhpError) as raised:
            await backend.create(request, lambda data: None)
        assert raised.value.code == -32009

    @pytest.mark.anyio
    async def test_the_refusal_says_why_and_what_to_do(self) -> None:
        """A refusal a reader cannot act on is indistinguishable from a bug."""
        backend = RefusingTerminalBackend()
        request = TerminalRequest("ahp-terminal:/1", TerminalClientClaim("client-a"))
        with pytest.raises(AhpError) as raised:
            await backend.create(request, lambda data: None)
        assert "TerminalBackend" in raised.value.message

    @pytest.mark.anyio
    async def test_the_reason_is_replaceable(self) -> None:
        backend = RefusingTerminalBackend("terminals are disabled on this host")
        request = TerminalRequest("ahp-terminal:/1", TerminalClientClaim("client-a"))
        with pytest.raises(AhpError) as raised:
            await backend.create(request, lambda data: None)
        assert "disabled on this host" in raised.value.message

    def test_a_request_inherits_no_environment(self) -> None:
        """`env=None` means the backend supplies nothing. A backend that read
        `os.environ` instead would hand every host credential to a shell a peer
        asked for."""
        request = TerminalRequest("ahp-terminal:/1", TerminalClientClaim("client-a"))
        assert request.env is None
        assert request.command is None


def test_the_module_imports_nothing_that_can_start_a_process() -> None:
    """The distribution's central promise about terminals: the parser, the claim
    model and the cap are all here, and the ability to run a process is not.

    Asserted against the import graph rather than the prose, because the prose is
    what gets edited when someone adds "just a small default backend". `os` is on
    the list too: a pty needs `os.forkpty`, so a module that never imports `os`
    cannot grow one by accident.
    """
    source = agent_host_server.core.terminals.__file__
    assert source is not None
    tree = ast.parse(Path(source).read_text(encoding="utf-8"))

    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            imported.add(node.module)

    assert not imported & {"os", "pty", "ptyprocess", "shutil", "signal", "subprocess"}
