"""The adapter: one Claude Agent SDK client per AHP session.

The Agent SDK runs Claude Code as a subprocess and speaks to it over a stream;
this module translates that stream into the host's neutral turn events
(`TurnSink`) and routes Claude Code's permission prompts to the host's
`confirm_tool_call`, which is where a client shows an approval dialog.

Sessions are resumable: the SDK's own session id is the resume state, so a
host restart continues the same Claude conversation instead of starting a
blank one under a transcript the client still shows.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import AsyncIterable, AsyncIterator, Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, Protocol

from agent_host_server.provider.base import (
    AgentInfo,
    AgentSessionContext,
    ConfigRequest,
    ConfigResolution,
    ConfigValue,
    ModelInfo,
    ToolConfirmation,
    TurnSink,
    UserMessage,
)
from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    HookMatcher,
    PermissionResultAllow,
    PermissionResultDeny,
    ResultMessage,
    SystemMessage,
    TextBlock,
    ThinkingBlock,
    ToolPermissionContext,
    ToolResultBlock,
    ToolUseBlock,
)
from claude_agent_sdk import UserMessage as SdkUserMessage
from claude_agent_sdk.types import StreamEvent

from agent_host_server_claude.attachments import prompt_content
from agent_host_server_claude.paths import directory_of
from agent_host_server_claude.permissions import (
    APPROVALS_PROPERTY,
    ASK,
    CONFIG_KEY,
    DISALLOWED_TOOLS,
    EXIT_PLAN_TOOL,
    PERMISSION_MODES,
    approval_mode,
    describe,
    pre_tool_use_decision,
)

log = logging.getLogger(__name__)

PROVIDER_ID = "claude"
_PROVIDER_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")


def is_valid_provider_id(value: str) -> bool:
    """A provider id is how `createSession` names an agent; keep it plain."""
    return _PROVIDER_ID.fullmatch(value) is not None


#: Claude Code's value for "whatever the account's default is".
DEFAULT_MODEL = "default"


class SdkClient(Protocol):
    """The slice of `ClaudeSDKClient` this adapter uses; tests supply a fake."""

    async def connect(self) -> None: ...

    async def query(self, prompt: str | AsyncIterable[dict[str, Any]]) -> None: ...

    def receive_response(self) -> AsyncIterator[Any]: ...

    async def interrupt(self) -> None: ...

    async def set_model(self, model: str | None = None) -> None: ...

    async def disconnect(self) -> None: ...

    async def get_server_info(self) -> dict[str, Any] | None: ...


ClientFactory = Callable[[ClaudeAgentOptions], SdkClient]


def _default_client(options: ClaudeAgentOptions) -> SdkClient:
    return ClaudeSDKClient(options=options)


def models_from_server_info(info: Mapping[str, Any] | None) -> tuple[ModelInfo, ...]:
    """The picker entries Claude Code itself offers this account.

    Claude Code reports them at start-up (`get_server_info()["models"]`), with
    the account's own default first. Taking them from there, rather than from
    a list in this file, is what keeps a new model - or an account without a
    given one - right without a release of this adapter.
    """
    entries = (info or {}).get("models")
    models: list[ModelInfo] = []
    if not isinstance(entries, Sequence):
        return ()
    for entry in entries:
        if not isinstance(entry, Mapping):
            continue
        value, name = entry.get("value"), entry.get("displayName")
        if not isinstance(value, str) or not value:
            continue
        meta = {
            key: entry[key]
            for key in ("description", "resolvedModel", "supportedEffortLevels")
            if key in entry
        }
        models.append(
            ModelInfo(id=value, name=name if isinstance(name, str) else value, meta=meta or None)
        )
    return tuple(models)


async def discover_models(
    root: Path, client_factory: ClientFactory = _default_client
) -> tuple[ModelInfo, ...]:
    """Ask Claude Code which models it offers. Empty (no picker) if it cannot say."""
    client = client_factory(ClaudeAgentOptions(cwd=str(root)))
    try:
        await client.connect()
        info = await client.get_server_info()
    except Exception:
        log.exception("could not read Claude Code's model list; offering no picker")
        return ()
    finally:
        try:
            await client.disconnect()
        except Exception:
            log.debug("disconnecting the model probe failed", exc_info=True)
    return models_from_server_info(info)


def _as_stream(content: list[dict[str, Any]]) -> AsyncIterable[dict[str, Any]]:
    """One user message with content blocks, in the SDK's streaming-input form."""

    async def stream() -> AsyncIterator[dict[str, Any]]:
        yield {
            "type": "user",
            "message": {"role": "user", "content": content},
            "parent_tool_use_id": None,
        }

    return stream()


def _text_of(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    parts = []
    for item in content:
        if isinstance(item, Mapping) and item.get("type") == "text":
            parts.append(str(item.get("text", "")))
    return "\n".join(parts)


class ClaudeSession:
    def __init__(
        self,
        context: AgentSessionContext,
        *,
        root: Path,
        client_factory: ClientFactory,
        claude_session_id: str | None = None,
        approvals: str | None = None,
    ) -> None:
        self.context = context
        #: Fixed for the session's life: chosen at creation (or restored on
        #: resume), because the host tells a provider its config only then.
        self.approvals = approval_mode(
            approvals if approvals is not None else context.config.get(CONFIG_KEY)
        )
        self._root = root.resolve()
        self._client_factory = client_factory
        self._client: SdkClient | None = None
        self._model: str | None = context.model
        #: The SDK's session id: what `resume=` needs after a host restart.
        self.claude_session_id = claude_session_id
        #: The turn in flight, if any. Permission prompts arrive on the SDK's
        #: own task, so they find the sink here rather than as an argument.
        self._sink: TurnSink | None = None
        self._announced: set[str] = set()
        self._streamed_messages: set[str] = set()
        self._lock = asyncio.Lock()

    # -- lifecycle -----------------------------------------------------------

    def working_directory(self) -> Path:
        """The session's directory, which must lie inside the served root."""
        for uri in self.context.working_directories:
            path = directory_of(uri)
            if path is not None:
                resolved = path.resolve()
                if resolved != self._root and self._root not in resolved.parents:
                    raise PermissionError(f"{resolved} is outside this host's root {self._root}")
                return resolved
        return self._root

    def _options(self) -> ClaudeAgentOptions:
        return ClaudeAgentOptions(
            cwd=str(self.working_directory()),
            resume=self.claude_session_id,
            model=None if self._model == DEFAULT_MODEL else self._model,
            permission_mode=PERMISSION_MODES[self.approvals],  # type: ignore[arg-type]
            disallowed_tools=list(DISALLOWED_TOOLS),
            can_use_tool=self._can_use_tool,
            hooks={"PreToolUse": [HookMatcher(matcher=None, hooks=[self._pre_tool_use])]},
            include_partial_messages=True,
            system_prompt={"type": "preset", "preset": "claude_code"},
        )

    async def _ensure_client(self) -> SdkClient:
        if self._client is None:
            client = self._client_factory(self._options())
            await client.connect()
            self._client = client
        return self._client

    async def aclose(self) -> None:
        client, self._client = self._client, None
        if client is not None:
            try:
                await client.disconnect()
            except Exception:  # a dying subprocess must not fail session disposal
                log.exception("disconnecting the Claude client failed")

    async def cancel(self, reason: str | None = None) -> None:
        if self._client is not None:
            try:
                await self._client.interrupt()
            except Exception:
                log.exception("interrupting the Claude client failed")

    # -- permissions ---------------------------------------------------------

    async def _pre_tool_use(self, hook_input: Any, tool_use_id: str | None, context: Any) -> Any:
        return pre_tool_use_decision(str(hook_input.get("tool_name", "")), self.approvals)

    async def _announce(self, call_id: str, name: str, tool_input: Mapping[str, Any]) -> None:
        if call_id in self._announced or self._sink is None:
            return
        self._announced.add(call_id)
        display, _ = describe(name, tool_input)
        await self._sink.tool_call_started(call_id, name, dict(tool_input), display_name=display)

    async def _can_use_tool(
        self, tool_name: str, tool_input: dict[str, Any], context: ToolPermissionContext
    ) -> PermissionResultAllow | PermissionResultDeny:
        sink = self._sink
        call_id = context.tool_use_id
        if sink is None or not call_id:
            return PermissionResultDeny(message="No client is attached to approve this.")
        await self._announce(call_id, tool_name, tool_input)
        plan = tool_input.get("plan") if tool_name == EXIT_PLAN_TOOL else None
        if isinstance(plan, str) and plan.strip():
            # The plan lives in the tool's input, which a client may not render
            # readably; the user must be able to read what they are approving.
            await sink.text_delta(f"\n\n{plan.strip()}\n")
        display, message = describe(tool_name, tool_input)
        outcome = await sink.confirm_tool_call(
            ToolConfirmation(
                call_id=call_id,
                name=tool_name,
                display_name=display,
                invocation_message=context.title or f"{display}: {message}",
                tool_input=tool_input,
                confirmation_title=context.title,
            )
        )
        if not outcome.approved:
            return PermissionResultDeny(message="The user declined this tool call.")
        if tool_name == EXIT_PLAN_TOOL:
            # Leaving plan mode must not leave the session looser than Ask: in
            # Claude Code's own default mode the user's allow rules would apply.
            self.approvals = ASK
        approved = outcome.tool_input if isinstance(outcome.tool_input, dict) else tool_input
        return PermissionResultAllow(updated_input=approved)

    # -- turns ---------------------------------------------------------------

    async def send_user_message(self, message: UserMessage, sink: TurnSink) -> None:
        async with self._lock:
            self._sink = sink
            self._announced.clear()
            try:
                await self._run_turn(message, sink)
            finally:
                self._sink = None

    async def _run_turn(self, message: UserMessage, sink: TurnSink) -> None:
        try:
            client = await self._ensure_client()
        except PermissionError as error:
            await sink.turn_failed(str(error), error_type="agent.workingDirectory")
            return
        picked = message.model.id if message.model is not None else None
        if picked is not None and picked != self._model:
            # "default" is Claude Code's name for the account default; the SDK
            # spells that as no model at all.
            await client.set_model(None if picked == DEFAULT_MODEL else picked)
            self._model = picked
        content = prompt_content(message.text, message.raw, self._root)
        await client.query(content if isinstance(content, str) else _as_stream(content))
        model: str | None = self._model
        async for item in client.receive_response():
            if isinstance(item, StreamEvent):
                await self._on_stream_event(item, sink)
            elif isinstance(item, AssistantMessage):
                model = item.model or model
                await self._on_assistant(item, sink)
            elif isinstance(item, SdkUserMessage):
                await self._on_tool_results(item, sink)
            elif isinstance(item, SystemMessage):
                session_id = item.data.get("session_id")
                if item.subtype == "init" and isinstance(session_id, str):
                    self.claude_session_id = session_id
            elif isinstance(item, ResultMessage):
                self.claude_session_id = item.session_id or self.claude_session_id
                await self._on_result(item, sink, model)

    async def _on_stream_event(self, item: StreamEvent, sink: TurnSink) -> None:
        # A sub-agent's text is its own conversation; its result arrives as the
        # parent's tool result, so streaming it here would say everything twice.
        if item.parent_tool_use_id is not None:
            return
        event = item.event
        if event.get("type") == "message_start":
            message_id = event.get("message", {}).get("id")
            if isinstance(message_id, str):
                self._current_message = message_id
        elif event.get("type") == "content_block_delta":
            delta = event.get("delta", {})
            if delta.get("type") == "text_delta" and delta.get("text"):
                self._mark_streamed()
                await sink.text_delta(delta["text"])
            elif delta.get("type") == "thinking_delta" and delta.get("thinking"):
                self._mark_streamed()
                await sink.reasoning_delta(delta["thinking"])

    _current_message: str | None = None

    def _mark_streamed(self) -> None:
        if self._current_message is not None:
            self._streamed_messages.add(self._current_message)

    async def _on_assistant(self, item: AssistantMessage, sink: TurnSink) -> None:
        # Text normally arrived already as deltas; a message that was never
        # streamed (a replay, or partial messages switched off upstream) is
        # published whole rather than lost.
        streamed = item.message_id is not None and item.message_id in self._streamed_messages
        for block in item.content:
            if isinstance(block, ToolUseBlock):
                await self._announce(block.id, block.name, block.input)
            elif item.parent_tool_use_id is None and not streamed:
                if isinstance(block, TextBlock) and block.text:
                    await sink.text_delta(block.text)
                elif isinstance(block, ThinkingBlock) and block.thinking:
                    await sink.reasoning_delta(block.thinking)

    async def _on_tool_results(self, item: SdkUserMessage, sink: TurnSink) -> None:
        if isinstance(item.content, str):
            return
        for block in item.content:
            if not isinstance(block, ToolResultBlock):
                continue
            if block.tool_use_id not in self._announced:
                continue
            failed = bool(block.is_error)
            text = _text_of(block.content)
            await sink.tool_call_completed(
                block.tool_use_id,
                {"content": [{"type": "text", "text": text}]},
                success=not failed,
                past_tense_message="Failed" if failed else "Done",
            )

    async def _on_result(self, item: ResultMessage, sink: TurnSink, model: str | None) -> None:
        usage = item.usage or {}
        await sink.usage(
            input_tokens=usage.get("input_tokens"),
            output_tokens=usage.get("output_tokens"),
            cache_read_tokens=usage.get("cache_read_input_tokens"),
            model=model,
        )
        if item.terminal_reason in ("aborted_streaming", "aborted_tools"):
            return  # the user pressed stop; the host already knows
        if item.is_error:
            detail = "; ".join(item.errors or []) or item.result or item.subtype
            await sink.turn_failed(detail, error_type=f"claude.{item.subtype}")


class ClaudeProvider:
    """One host, one provider: Claude Code, rooted at one directory."""

    def __init__(
        self,
        root: Path,
        *,
        display_name: str = "Claude",
        client_factory: ClientFactory = _default_client,
        models: Sequence[ModelInfo] = (),
        provider_id: str = PROVIDER_ID,
    ) -> None:
        if not is_valid_provider_id(provider_id):
            raise ValueError(f"invalid provider id: {provider_id!r}")
        self.provider_id = provider_id
        self.root = Path(root).resolve()
        self._display_name = display_name
        self._client_factory = client_factory
        self._models = tuple(models)

    @property
    def agent(self) -> AgentInfo:
        return AgentInfo(
            provider=self.provider_id,
            display_name=self._display_name,
            description="Claude Code, running on this machine as its user.",
            models=self._models,
        )

    async def resolve_config(self, request: ConfigRequest) -> ConfigResolution:
        """The one setting a session takes: how tool calls are approved."""
        return ConfigResolution(
            properties={CONFIG_KEY: APPROVALS_PROPERTY},
            values={CONFIG_KEY: approval_mode(request.values.get(CONFIG_KEY))},
        )

    async def complete_config(self, request: ConfigRequest) -> Sequence[ConfigValue]:
        return ()  # nothing here is `enumDynamic`

    async def create_session(self, context: AgentSessionContext) -> ClaudeSession:
        return ClaudeSession(context, root=self.root, client_factory=self._client_factory)

    async def resume_session(self, context: AgentSessionContext) -> ClaudeSession:
        state = context.resume_state or {}
        session_id = state.get("claudeSessionId")
        return ClaudeSession(
            context,
            root=self.root,
            client_factory=self._client_factory,
            claude_session_id=session_id if isinstance(session_id, str) else None,
            # The host does not replay a resumed session's config to the
            # provider, so the mode travels in the resume state instead. A
            # state from before approvals existed resumes in `ask`.
            approvals=approval_mode(state.get(CONFIG_KEY, state.get("approvals", ASK))),
        )

    async def resume_state_of(self, session: Any) -> Mapping[str, Any] | None:
        if isinstance(session, ClaudeSession) and session.claude_session_id:
            return {"claudeSessionId": session.claude_session_id, CONFIG_KEY: session.approvals}
        return None
