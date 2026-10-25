"""Inner-provider fakes for the flow tests; not a test module.

Loaded by import path (`tests.flow_fakes:<Name>`), as a flow.toml would.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar

from ahp_host.provider.base import (
    HOST_CLIENT_ID,
    AgentInfo,
    AgentSession,
    AgentSessionContext,
    AuthChallenge,
    ClientToolCall,
    InputOutcome,
    InputQuestion,
    InputRequest,
    ModelInfo,
    ToolConfirmation,
    ToolConfirmationOutcome,
    TurnSink,
    UserMessage,
)
from ahp_host.provider.echo import EchoProvider

from ahp_host_flow.catalog import directory_of
from tests.preamble import stripped


@dataclass
class RecordingSession:
    context: AgentSessionContext
    output_tokens: int = 5
    messages: list[UserMessage] = field(default_factory=list)
    # Every message exactly as sent.
    received: list[UserMessage] = field(default_factory=list)
    overlays: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    config_changes: list[dict[str, Any]] = field(default_factory=list)
    cancelled: list[str | None] = field(default_factory=list)
    closed: bool = False
    overlay_seen: asyncio.Event = field(default_factory=asyncio.Event)
    released: asyncio.Event = field(default_factory=asyncio.Event)

    async def send_user_message(self, message: UserMessage, sink: TurnSink) -> None:
        self.received.append(message)
        message = stripped(message)
        self.messages.append(message)
        if message.text.startswith("wait-overlay"):
            await self.overlay_seen.wait()
        if message.text.startswith("hold"):
            await self.released.wait()
        overlay = self.overlays[-1][1] if self.overlays else {}
        suffix = f" overlay={json.dumps(overlay, sort_keys=True)}" if overlay else ""
        await sink.text_delta(f"rec: {message.text}{suffix}")
        await sink.usage(input_tokens=1, output_tokens=self.output_tokens)

    async def chat_config_changed(self, chat: str, values: Any) -> None:
        self.overlays.append((chat, dict(values)))
        self.overlay_seen.set()

    async def config_changed(self, values: Any) -> None:
        self.config_changes.append(dict(values))

    async def cancel(self, reason: str | None = None) -> None:
        self.cancelled.append(reason)

    async def aclose(self) -> None:
        self.closed = True


class RecordingProvider:
    """Answers `rec: <text>` and remembers every session, message and config change."""

    instances: ClassVar[list[RecordingProvider]] = []

    def __init__(
        self, *, provider_id: str = "rec", output_tokens: int = 5, config: Path | None = None
    ) -> None:
        self.provider_id = provider_id
        self.output_tokens = output_tokens
        self.config_path = config
        self.sessions: list[RecordingSession] = []
        RecordingProvider.instances.append(self)

    @property
    def agent(self) -> AgentInfo:
        return AgentInfo(
            provider=self.provider_id,
            display_name="Recorder",
            description="Records what it is given.",
            models=(
                ModelInfo(id="rec-large", name="Rec Large", vendor="Acme"),
                ModelInfo(id="rec-small", name="Rec Small"),
            ),
        )

    async def create_session(self, context: AgentSessionContext) -> RecordingSession:
        session = RecordingSession(context, self.output_tokens)
        self.sessions.append(session)
        return session


class GatedProvider:
    """Holds `create_session` until `release` is set; sessions are `RecordingSession`s."""

    instances: ClassVar[list[GatedProvider]] = []

    def __init__(self, *, provider_id: str = "gate") -> None:
        self.provider_id = provider_id
        self.sessions: list[RecordingSession] = []
        self.creating = asyncio.Event()
        self.release = asyncio.Event()
        GatedProvider.instances.append(self)

    @property
    def agent(self) -> AgentInfo:
        return AgentInfo(
            provider=self.provider_id,
            display_name="Gate",
            description="Makes sessions only when released.",
            models=(),
        )

    async def create_session(self, context: AgentSessionContext) -> RecordingSession:
        self.creating.set()
        await self.release.wait()
        session = RecordingSession(context)
        self.sessions.append(session)
        return session


@dataclass
class SteerableSession:
    """Waits for a steering message, then answers with what it was steered to."""

    context: AgentSessionContext
    texts: list[str] = field(default_factory=list)
    steered: asyncio.Event = field(default_factory=asyncio.Event)
    cancelled: list[str | None] = field(default_factory=list)
    closed: bool = False

    async def send_user_message(self, message: UserMessage, sink: TurnSink) -> None:
        await asyncio.wait_for(self.steered.wait(), 10)
        await sink.text_delta("steered: " + "; ".join(self.texts))
        await sink.usage(input_tokens=1, output_tokens=1)

    async def steer(self, chat_uri: str, message: UserMessage) -> bool:
        self.texts.append(message.text)
        self.steered.set()
        return True

    async def cancel(self, reason: str | None = None) -> None:
        self.cancelled.append(reason)

    async def aclose(self) -> None:
        self.closed = True


class SteerableProvider:
    """Sessions that take steering mid-turn."""

    instances: ClassVar[list[SteerableProvider]] = []

    def __init__(self, *, provider_id: str = "steer") -> None:
        self.provider_id = provider_id
        self.sessions: list[SteerableSession] = []
        SteerableProvider.instances.append(self)

    @property
    def agent(self) -> AgentInfo:
        return AgentInfo(
            provider=self.provider_id,
            display_name="Steerable",
            description="Answers once steered.",
            models=(),
        )

    async def create_session(self, context: AgentSessionContext) -> SteerableSession:
        session = SteerableSession(context)
        self.sessions.append(session)
        return session


@dataclass
class HangingSession:
    context: AgentSessionContext
    started: asyncio.Event = field(default_factory=asyncio.Event)
    cancelled: list[str | None] = field(default_factory=list)
    closed: bool = False
    _release: asyncio.Event = field(default_factory=asyncio.Event)

    async def send_user_message(self, message: UserMessage, sink: TurnSink) -> None:
        self.started.set()
        await self._release.wait()
        await sink.text_delta("released")

    async def cancel(self, reason: str | None = None) -> None:
        self.cancelled.append(reason)
        self._release.set()

    async def aclose(self) -> None:
        self.closed = True


class HangingProvider:
    """A turn that never ends until the session is cancelled."""

    instances: ClassVar[list[HangingProvider]] = []

    def __init__(self, *, provider_id: str = "hang") -> None:
        self.provider_id = provider_id
        self.sessions: list[HangingSession] = []
        HangingProvider.instances.append(self)

    @property
    def agent(self) -> AgentInfo:
        return AgentInfo(
            provider=self.provider_id,
            display_name="Hanger",
            description="Never answers until cancelled.",
            models=(),
        )

    async def create_session(self, context: AgentSessionContext) -> HangingSession:
        session = HangingSession(context)
        self.sessions.append(session)
        return session


@dataclass
class RestartHoldSession:
    context: AgentSessionContext

    async def send_user_message(self, message: UserMessage, sink: TurnSink) -> None:
        message = stripped(message)
        RestartHoldProvider.prompts.append(message.text)
        if message.text.startswith("hold") and RestartHoldProvider.holding:
            await asyncio.Event().wait()
        await sink.text_delta(f"rec: {message.text}")
        await sink.usage(input_tokens=1, output_tokens=5)

    async def cancel(self, reason: str | None = None) -> None:
        pass

    async def aclose(self) -> None:
        pass


class RestartHoldProvider:
    """Answers at once, except a prompt starting `hold` waits while `holding` (a restart test)."""

    prompts: ClassVar[list[str]] = []
    holding: ClassVar[bool] = True
    sessions_created: ClassVar[int] = 0

    def __init__(self, *, provider_id: str = "hold") -> None:
        self.provider_id = provider_id

    @property
    def agent(self) -> AgentInfo:
        return AgentInfo(
            provider=self.provider_id,
            display_name="Restart hold",
            description="Holds `hold...` prompts until the host restarts.",
            models=(),
        )

    async def create_session(self, context: AgentSessionContext) -> RestartHoldSession:
        RestartHoldProvider.sessions_created += 1
        return RestartHoldSession(context)


@dataclass
class TurnlessHoldSession:
    """Remembers its turns (`memory`); a prompt starting `hold` waits while `holding`.

    Resumes as a session (its provider is resumable) but not a turn: no `resume_turn`.
    """

    context: AgentSessionContext
    memory: list[str] = field(default_factory=list)

    async def send_user_message(self, message: UserMessage, sink: TurnSink) -> None:
        text = stripped(message).text
        ResumableHoldProvider.prompts.append(text)
        if text.startswith("hold") and ResumableHoldProvider.holding:
            await asyncio.Event().wait()
        self.memory.append(text)
        await sink.text_delta("mem: " + " | ".join(self.memory))
        await sink.usage(input_tokens=1, output_tokens=5)

    async def cancel(self, reason: str | None = None) -> None:
        pass

    async def aclose(self) -> None:
        pass


#: What `ResumableHoldSession.resume_turn` answers, so a test tells a turn resumed in place from
#: one sent again (`mem: ...`).
RESUMED_IN_PLACE = "turn resumed in place"


@dataclass
class ResumableHoldSession(TurnlessHoldSession):
    """A `TurnlessHoldSession` that also resumes a cut-off turn in place (`ResumesTurns`)."""

    async def resume_turn(self, chat_uri: str, turn_id: str, sink: TurnSink) -> None:
        ResumableHoldProvider.turn_resumes.append((chat_uri, turn_id))
        await sink.text_delta(RESUMED_IN_PLACE)


class ResumableHoldProvider:
    """`RestartHoldProvider` whose sessions resume after a restart with their memory.

    The class flags make the negative cases: `resumes_turns` False hands back sessions without
    `resume_turn`, `records_state` False makes `resume_state_of` say None, and `fail_resume`
    makes `resume_session` raise. `reset_fakes` restores them.
    """

    prompts: ClassVar[list[str]] = []
    holding: ClassVar[bool] = True
    sessions_created: ClassVar[int] = 0
    sessions_resumed: ClassVar[int] = 0
    turn_resumes: ClassVar[list[tuple[str, str]]] = []
    resumes_turns: ClassVar[bool] = True
    records_state: ClassVar[bool] = True
    fail_resume: ClassVar[bool] = False

    def __init__(self, *, provider_id: str = "rh") -> None:
        self.provider_id = provider_id

    @property
    def agent(self) -> AgentInfo:
        return AgentInfo(
            provider=self.provider_id,
            display_name="Resumable hold",
            description="Holds `hold...` prompts; its sessions resume after a restart.",
            models=(),
        )

    @staticmethod
    def _session(context: AgentSessionContext, memory: list[str]) -> TurnlessHoldSession:
        kind = ResumableHoldSession if ResumableHoldProvider.resumes_turns else TurnlessHoldSession
        return kind(context, memory=memory)

    async def create_session(self, context: AgentSessionContext) -> TurnlessHoldSession:
        ResumableHoldProvider.sessions_created += 1
        return self._session(context, [])

    async def resume_session(self, context: AgentSessionContext) -> TurnlessHoldSession:
        if ResumableHoldProvider.fail_resume:
            raise RuntimeError("the old session is gone")
        ResumableHoldProvider.sessions_resumed += 1
        state = context.resume_state or {}
        return self._session(context, list(state.get("memory", [])))

    async def resume_state_of(self, session: AgentSession) -> dict[str, Any] | None:
        if not ResumableHoldProvider.records_state or not isinstance(session, TurnlessHoldSession):
            return None
        return {"memory": list(session.memory)}


AWS = "AKIA" + "TESTONLY" * 2
GH = "ghp_" + "x1" * 18
USER = "sekrit-" + "12345678"
SECRETS = (AWS, GH, USER)
BEARER = "Bq7" + "x9" * 10
KV = "s3cr3t" + "value1"
PASSWORD = "hunter2" + "hunter2"


@dataclass
class SecretSession:
    context: AgentSessionContext
    messages: list[UserMessage] = field(default_factory=list)
    closed: bool = False

    async def send_user_message(self, message: UserMessage, sink: TurnSink) -> None:
        message = stripped(message)
        self.messages.append(message)
        if message.text.startswith("fail"):
            await sink.turn_failed(f"boom {AWS}")
            return
        await sink.text_delta("key " + AWS[:10])
        await sink.text_delta(AWS[10:] + " and " + GH + "\n")
        await sink.tool_call_started("s1", "shell", {"cmd": "echo " + GH})
        await sink.tool_call_output("s1", [{"type": "text", "text": "token=" + USER}])
        await sink.tool_call_completed("s1", {"content": [{"type": "text", "text": AWS}]})
        await sink.system_notification("printed " + GH)
        await sink.text_delta("done " + USER)
        await sink.usage(input_tokens=1, output_tokens=2)

    async def cancel(self, reason: str | None = None) -> None:
        pass

    async def aclose(self) -> None:
        self.closed = True


class SecretProvider:
    """Prints credentials in its text, tool calls and notifications, and fails with one."""

    instances: ClassVar[list[SecretProvider]] = []

    def __init__(self, *, provider_id: str = "secret") -> None:
        self.provider_id = provider_id
        self.sessions: list[SecretSession] = []
        SecretProvider.instances.append(self)

    @property
    def agent(self) -> AgentInfo:
        return AgentInfo(
            provider=self.provider_id,
            display_name="Secrets",
            description="Prints credentials.",
            models=(),
        )

    async def create_session(self, context: AgentSessionContext) -> SecretSession:
        session = SecretSession(context)
        self.sessions.append(session)
        return session


@dataclass
class FileWriterSession:
    context: AgentSessionContext
    closed: bool = False

    async def send_user_message(self, message: UserMessage, sink: TurnSink) -> None:
        message = stripped(message)
        cwd = directory_of(self.context.working_directories[0])
        assert cwd is not None
        if message.text.startswith("write "):
            (cwd / message.text.removeprefix("write ").strip()).write_text("x", encoding="utf-8")
        if message.text.startswith("write-empty-first "):
            # Writes the file, and the first time any session is sent this replies nothing.
            (cwd / message.text.split(" ", 1)[1].strip()).write_text("x", encoding="utf-8")
            seen = FileWriterProvider.sends.get(message.text, 0)
            FileWriterProvider.sends[message.text] = seen + 1
            if seen == 0:
                await sink.usage(input_tokens=1, output_tokens=1)
                return
        if message.text.strip() == "commit-empty":
            # HEAD moves but the tree does not: kept as changed, yet nothing to review.
            subprocess.run(
                [
                    "git",
                    "-c",
                    "user.name=flow-test",
                    "-c",
                    "user.email=flow-test@example.com",
                    "commit",
                    "-q",
                    "--allow-empty",
                    "-m",
                    "empty",
                ],
                cwd=cwd,
                check=True,
                capture_output=True,
            )
        await sink.text_delta(str(cwd))
        await sink.usage(input_tokens=1, output_tokens=1)

    async def cancel(self, reason: str | None = None) -> None:
        pass

    async def aclose(self) -> None:
        self.closed = True


class FileWriterProvider:
    """Replies with its working directory; `write <name>` first writes that file there, and
    `write-empty-first <name>` too but replies nothing the first time (`sends` counts)."""

    instances: ClassVar[list[FileWriterProvider]] = []
    sends: ClassVar[dict[str, int]] = {}

    def __init__(self, *, provider_id: str = "fw") -> None:
        self.provider_id = provider_id
        self.sessions: list[FileWriterSession] = []
        FileWriterProvider.instances.append(self)

    @property
    def agent(self) -> AgentInfo:
        return AgentInfo(
            provider=self.provider_id,
            display_name="File writer",
            description="Writes files in its working directory.",
            models=(),
        )

    async def create_session(self, context: AgentSessionContext) -> FileWriterSession:
        session = FileWriterSession(context)
        self.sessions.append(session)
        return session


@dataclass
class StructuredSecretSession:
    """Answers a structured turn with credentials in its result: through the tool or the reply."""

    context: AgentSessionContext
    capable: bool = True
    messages: list[UserMessage] = field(default_factory=list)
    tool_results: list[Any] = field(default_factory=list)
    closed: bool = False

    def offers_host_tools(self) -> bool:
        return self.capable

    async def send_user_message(self, message: UserMessage, sink: TurnSink) -> None:
        message = stripped(message)
        self.messages.append(message)
        result = {"k": "x " + AWS, "password": PASSWORD}
        if message.host_tools:
            self.tool_results.append(
                await sink.run_client_tool(
                    ClientToolCall(
                        call_id="so1",
                        name="StructuredOutput",
                        client_id=HOST_CLIENT_ID,
                        tool_input=result,
                        display_name="Structured output",
                    )
                )
            )
        else:
            await sink.text_delta("```json\n" + json.dumps(result) + "\n```\n")
        await sink.usage(input_tokens=1, output_tokens=1)

    async def cancel(self, reason: str | None = None) -> None:
        pass

    async def aclose(self) -> None:
        self.closed = True


class StructuredSecretProvider:
    """A structured agent whose result holds credentials; `capable=false` takes the fallback."""

    instances: ClassVar[list[StructuredSecretProvider]] = []

    def __init__(self, *, provider_id: str = "ssecret", capable: bool = True) -> None:
        self.provider_id = provider_id
        self.capable = capable
        self.sessions: list[StructuredSecretSession] = []
        StructuredSecretProvider.instances.append(self)

    @property
    def agent(self) -> AgentInfo:
        return AgentInfo(
            provider=self.provider_id,
            display_name="Structured secrets",
            description="Returns credentials as a structured result.",
            models=(),
        )

    async def create_session(self, context: AgentSessionContext) -> StructuredSecretSession:
        session = StructuredSecretSession(context, self.capable)
        self.sessions.append(session)
        return session


CONFIRM_COMMAND = f"curl -H 'Authorization: Bearer {BEARER}' https://example.com"
INPUT_QUESTION = f"Use api_key={KV} for the upload?"


@dataclass
class AskingSession:
    context: AgentSessionContext
    confirmation: ToolConfirmation | None = None
    request: InputRequest | None = None
    outcome: ToolConfirmationOutcome | None = None
    answer: InputOutcome | None = None
    closed: bool = False

    async def send_user_message(self, message: UserMessage, sink: TurnSink) -> None:
        self.confirmation = ToolConfirmation(
            call_id="c1",
            name="shell",
            invocation_message=f"Run {CONFIRM_COMMAND}",
            tool_input={"command": CONFIRM_COMMAND},
            confirmation_title=f"Allow {GH}?",
        )
        await sink.tool_call_started("c1", "shell", {"command": CONFIRM_COMMAND})
        self.outcome = await sink.confirm_tool_call(self.confirmation)
        self.request = InputRequest(
            message=f"url https://example.com/?token={KV}",
            questions=[InputQuestion(id="q", kind="text", message=INPUT_QUESTION)],
        )
        self.answer = await sink.request_input(self.request)
        await sink.text_delta(f"approved={self.outcome.approved} answer={self.answer.response}")
        await sink.usage(input_tokens=1, output_tokens=1)

    async def cancel(self, reason: str | None = None) -> None:
        pass

    async def aclose(self) -> None:
        self.closed = True


class AskingProvider:
    """Asks to confirm a command and answers an input question, both holding credentials."""

    instances: ClassVar[list[AskingProvider]] = []

    def __init__(self, *, provider_id: str = "asking") -> None:
        self.provider_id = provider_id
        self.sessions: list[AskingSession] = []
        AskingProvider.instances.append(self)

    @property
    def agent(self) -> AgentInfo:
        return AgentInfo(
            provider=self.provider_id,
            display_name="Asking",
            description="Asks for approval and input with credentials in the prompts.",
            models=(),
        )

    async def create_session(self, context: AgentSessionContext) -> AskingSession:
        session = AskingSession(context)
        self.sessions.append(session)
        return session


@dataclass
class ReasonSession:
    context: AgentSessionContext

    async def send_user_message(self, message: UserMessage, sink: TurnSink) -> None:
        await sink.tool_call_started("r1", "edit", {"path": "b"})
        outcome = await sink.confirm_tool_call(
            ToolConfirmation(
                call_id="r1", name="edit", invocation_message="Edit b", tool_input={"path": "b"}
            )
        )
        await sink.text_delta(
            "approved" if outcome.approved else f"denied: {outcome.reason_message}"
        )

    async def cancel(self, reason: str | None = None) -> None:
        pass

    async def aclose(self) -> None:
        pass


class ReasonEchoProvider:
    """Asks to confirm an edit and replies with the decision and the denial's reason."""

    def __init__(self, *, provider_id: str = "claude") -> None:
        self.provider_id = provider_id

    @property
    def agent(self) -> AgentInfo:
        return AgentInfo(
            provider=self.provider_id,
            display_name="Reason echo",
            description="Asks for approval and echoes the decision.",
            models=(),
        )

    async def create_session(self, context: AgentSessionContext) -> ReasonSession:
        return ReasonSession(context)


@dataclass
class ModalSession:
    context: AgentSessionContext
    mode: str = "default"
    overlays: list[dict[str, Any]] = field(default_factory=list)
    changed: asyncio.Event = field(default_factory=asyncio.Event)

    def __post_init__(self) -> None:
        self.mode = str(self.context.config.get("permissionMode", "default"))

    async def chat_config_changed(self, chat: str, values: Any) -> None:
        self.overlays.append(dict(values))
        if "permissionMode" in values:
            self.mode = str(values["permissionMode"])
        self.changed.set()

    async def send_user_message(self, message: UserMessage, sink: TurnSink) -> None:
        asked = 0
        for i in (1, 2):
            call_id = f"m{i}"
            await sink.tool_call_started(call_id, "write", {"n": i})
            if self.mode == "auto":
                continue
            asked += 1
            outcome = await sink.confirm_tool_call(
                ToolConfirmation(
                    call_id=call_id,
                    name="write",
                    invocation_message=f"write {i}",
                    tool_input={"n": i},
                )
            )
            if not outcome.approved:
                break
        await sink.text_delta(f"asked={asked} mode={self.mode}")

    async def cancel(self, reason: str | None = None) -> None:
        pass

    async def aclose(self) -> None:
        pass


class ModalProvider:
    """Two tool calls that ask only while the approval mode is not `auto`; takes overlays."""

    instances: ClassVar[list[ModalProvider]] = []

    def __init__(self, *, provider_id: str = "modal") -> None:
        self.provider_id = provider_id
        self.sessions: list[ModalSession] = []
        ModalProvider.instances.append(self)

    @property
    def agent(self) -> AgentInfo:
        return AgentInfo(
            provider=self.provider_id,
            display_name="Modal",
            description="Asks for approval until the mode is auto.",
            models=(),
        )

    async def create_session(self, context: AgentSessionContext) -> ModalSession:
        session = ModalSession(context)
        self.sessions.append(session)
        return session


@dataclass
class HintedSession:
    context: AgentSessionContext

    async def send_user_message(self, message: UserMessage, sink: TurnSink) -> None:
        calls = json.loads(stripped(message).text)
        replies: list[str] = []
        for i, item in enumerate(calls):
            call_id = f"h{i}"
            name = item["name"]
            await sink.tool_call_started(call_id, name, item["input"])
            outcome = await sink.confirm_tool_call(
                ToolConfirmation(
                    call_id,
                    name,
                    f"Run {name}",
                    tool_input=item["input"],
                    read_only=item["read_only"],
                )
            )
            replies.append(
                f"{name}=yes" if outcome.approved else f"{name}=no({outcome.reason_message})"
            )
        await sink.text_delta(" ".join(replies))

    async def cancel(self, reason: str | None = None) -> None:
        pass

    async def aclose(self) -> None:
        pass


class HintedProvider:
    """Asks to confirm each tool call in the prompt (a JSON list) and replies with the answers."""

    def __init__(self, *, provider_id: str = "claude") -> None:
        self.provider_id = provider_id

    @property
    def agent(self) -> AgentInfo:
        return AgentInfo(
            provider=self.provider_id,
            display_name="Hinted",
            description="Asks for approval of the calls in its prompt.",
            models=(),
        )

    async def create_session(self, context: AgentSessionContext) -> HintedSession:
        return HintedSession(context)


@dataclass
class AuthSession:
    context: AgentSessionContext

    async def send_user_message(self, message: UserMessage, sink: TurnSink) -> None:
        await sink.tool_call_started("auth-1", "mcp_lookup", {})
        await sink.request_authentication(
            "auth-1", AuthChallenge(resource={"resource": "https://mcp.example.com"})
        )
        await sink.text_delta("authed")

    async def cancel(self, reason: str | None = None) -> None:
        pass

    async def aclose(self) -> None:
        pass


class AuthProvider:
    """A turn whose tool call needs an MCP server's authentication."""

    instances: ClassVar[list[AuthProvider]] = []

    def __init__(self, *, provider_id: str = "auth") -> None:
        self.provider_id = provider_id
        self.sessions: list[AuthSession] = []
        AuthProvider.instances.append(self)

    @property
    def agent(self) -> AgentInfo:
        return AgentInfo(
            provider=self.provider_id,
            display_name="Auth",
            description="Asks for an MCP server's authentication mid-tool-call.",
            models=(),
        )

    async def create_session(self, context: AgentSessionContext) -> AuthSession:
        session = AuthSession(context)
        self.sessions.append(session)
        return session


class StrippingSession:
    """Wraps an agent session so it is sent the prompt without the subagent preamble.

    Every other attribute is the wrapped session's, so a capability check sees exactly the
    methods the wrapped session has. A runtime-checkable protocol check looks attributes up
    statically, past `__getattr__`, so the wrapped session's public methods are also set on
    the instance.
    """

    def __init__(self, session: AgentSession) -> None:
        self._session = session
        for name in dir(session):
            member = getattr(session, name, None)
            if name != "send_user_message" and not name.startswith("_") and callable(member):
                setattr(self, name, member)

    async def send_user_message(self, message: UserMessage, sink: TurnSink) -> None:
        await self._session.send_user_message(stripped(message), sink)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._session, name)


class StrippedEchoProvider(EchoProvider):
    """The echo provider, its sessions sent the prompt without the subagent preamble."""

    async def create_session(self, context: AgentSessionContext) -> Any:
        return StrippingSession(await super().create_session(context))

    async def resume_session(self, context: AgentSessionContext) -> Any:
        return StrippingSession(await super().resume_session(context))


class EmptyFirstSession:
    """Thinks out loud about its prompt and replies nothing the first time any session is
    sent that prompt (an empty reply, so `retry_empty` dispatches it again); afterwards it
    echoes it. `EmptyFirstProvider.sends` counts per prompt; `reset_fakes` clears it."""

    def __init__(self, context: AgentSessionContext) -> None:
        self.context = context

    async def send_user_message(self, message: UserMessage, sink: TurnSink) -> None:
        prompt = stripped(message).text
        seen = EmptyFirstProvider.sends.get(prompt, 0)
        EmptyFirstProvider.sends[prompt] = seen + 1
        if seen == 0:
            await sink.reasoning_delta(f"thinking about {prompt}")
            return
        await sink.text_delta(f"You said: {prompt}")

    async def cancel(self, reason: str | None = None) -> None:
        pass

    async def aclose(self) -> None:
        pass


class EmptyFirstProvider:
    """Sessions whose first reply to a prompt is empty (see `EmptyFirstSession`)."""

    sends: ClassVar[dict[str, int]] = {}

    @property
    def agent(self) -> AgentInfo:
        return AgentInfo(
            provider="empty-first",
            display_name="Empty first",
            description="Replies nothing the first time, then echoes.",
            models=(),
        )

    async def create_session(self, context: AgentSessionContext) -> EmptyFirstSession:
        return EmptyFirstSession(context)


def not_a_provider() -> object:
    return object()


class ClientToolSession:
    """Calls a client's tool on request: `list`, `call:<name>` or `wait:<name>`."""

    def __init__(self, context: AgentSessionContext) -> None:
        self.context = context
        self.clients: list[dict[str, Any]] = (
            [{"clientId": context.active_client_id, "tools": list(context.client_tools)}]
            if context.active_client_id
            else []
        )
        self.changed = asyncio.Event()

    async def active_clients_changed(self, clients: Any) -> None:
        self.clients = [dict(c) for c in clients]
        self.changed.set()

    def _owner(self, name: str) -> str | None:
        for client in self.clients:
            if any(t.get("name") == name for t in client.get("tools") or ()):
                return str(client["clientId"])
        return None

    async def send_user_message(self, message: UserMessage, sink: TurnSink) -> None:
        text = stripped(message).text
        if text == "list":
            names = sorted(str(t["name"]) for t in self.context.client_tools)
            await sink.text_delta(json.dumps(names))
            return
        verb, _, name = text.partition(":")
        owner = self._owner(name)
        if verb == "wait":
            while owner is None:
                self.changed.clear()
                try:
                    await asyncio.wait_for(self.changed.wait(), 5.0)
                except TimeoutError:
                    break
                owner = self._owner(name)
        if owner is None:
            await sink.text_delta(f"no client offers {name}")
            return
        result = await sink.run_client_tool(
            ClientToolCall(
                call_id=f"ct-{uuid.uuid4().hex[:8]}",
                name=name,
                client_id=owner,
                tool_input={"q": "x"},
                display_name=name,
            )
        )
        if result.accepted:
            first = result.value["content"][0]["text"]
            await sink.text_delta(f"{name}: {first}")
        else:
            await sink.text_delta(f"{name} refused: {result.reason}")

    async def cancel(self, reason: str | None = None) -> None:
        pass

    async def aclose(self) -> None:
        pass


class ClientToolProvider:
    """Sessions that call the tools of the clients they are told about."""

    instances: ClassVar[list[ClientToolSession]] = []

    def __init__(self, *, provider_id: str = "ctool") -> None:
        self.provider_id = provider_id

    @property
    def agent(self) -> AgentInfo:
        return AgentInfo(
            provider=self.provider_id,
            display_name="Client tools",
            description="Calls the clients' tools.",
            models=(),
        )

    async def create_session(self, context: AgentSessionContext) -> ClientToolSession:
        session = ClientToolSession(context)
        ClientToolProvider.instances.append(session)
        return session


def reset_fakes() -> None:
    ClientToolProvider.instances.clear()
    RecordingProvider.instances.clear()
    HangingProvider.instances.clear()
    GatedProvider.instances.clear()
    SteerableProvider.instances.clear()
    SecretProvider.instances.clear()
    StructuredSecretProvider.instances.clear()
    AskingProvider.instances.clear()
    AuthProvider.instances.clear()
    ModalProvider.instances.clear()
    FileWriterProvider.instances.clear()
    FileWriterProvider.sends.clear()
    EmptyFirstProvider.sends.clear()
    RestartHoldProvider.prompts.clear()
    RestartHoldProvider.holding = True
    RestartHoldProvider.sessions_created = 0
    ResumableHoldProvider.prompts.clear()
    ResumableHoldProvider.holding = True
    ResumableHoldProvider.sessions_created = 0
    ResumableHoldProvider.sessions_resumed = 0
    ResumableHoldProvider.turn_resumes.clear()
    ResumableHoldProvider.resumes_turns = True
    ResumableHoldProvider.records_state = True
    ResumableHoldProvider.fail_resume = False


class BrokenSessionProvider:
    """A provider whose `create_session` raises."""

    def __init__(self, *, provider_id: str = "broken") -> None:
        self.provider_id = provider_id

    @property
    def agent(self) -> AgentInfo:
        return AgentInfo(
            provider=self.provider_id,
            display_name="Broken",
            description="Cannot make a session.",
            models=(),
        )

    async def create_session(self, context: AgentSessionContext) -> HangingSession:
        raise RuntimeError("no session for you")


class RootedProvider:
    """Records the `roots` and `state_dir` a loader injected (raw, whatever they are)."""

    def __init__(
        self, *, roots: Any = None, state_dir: Any = None, provider_id: str = "rooted"
    ) -> None:
        self.roots = roots
        self.state_dir = state_dir
        self.provider_id = provider_id

    @property
    def agent(self) -> AgentInfo:
        return AgentInfo(
            provider=self.provider_id,
            display_name="Rooted",
            description="Records the roots and state dir it is given.",
            models=(ModelInfo(id="rooted-model", name="Rooted"),),
        )

    async def create_session(self, context: AgentSessionContext) -> RecordingSession:
        return RecordingSession(context)
