"""Inner-provider fakes for the flow tests; not a test module.

Loaded by import path (`tests.flow_fakes:<Name>`), as a flow.toml would.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar

from ahp_host.provider.base import (
    HOST_CLIENT_ID,
    AgentInfo,
    AgentSession,
    AgentSessionContext,
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
    """Replies with its working directory; `write <name>` first writes that file there."""

    instances: ClassVar[list[FileWriterProvider]] = []

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


def not_a_provider() -> object:
    return object()


def reset_fakes() -> None:
    RecordingProvider.instances.clear()
    HangingProvider.instances.clear()
    GatedProvider.instances.clear()
    SteerableProvider.instances.clear()
    SecretProvider.instances.clear()
    StructuredSecretProvider.instances.clear()
    AskingProvider.instances.clear()
    FileWriterProvider.instances.clear()
    RestartHoldProvider.prompts.clear()
    RestartHoldProvider.holding = True
    RestartHoldProvider.sessions_created = 0


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
