"""`FlowProvider`: a workflow run as an AHP session (plan §4)."""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Final

from ahp_host import TurnSink
from ahp_host.provider.base import (
    AgentInfo,
    AgentProvider,
    AgentSession,
    AgentSessionContext,
    ConfigRequest,
    ConfigResolution,
    ConfigValue,
    ModelInfo,
    ReconfiguresChats,
    ReconfiguresSessions,
    SessionDescription,
    UserMessage,
)

from ahp_host_flow import __version__, catalog
from ahp_host_flow.commands import (
    BadRequest,
    Help,
    help_text,
    parse_message,
    unknown_text,
)
from ahp_host_flow.config import (
    APPROVAL_KEY,
    APPROVAL_MODES,
    EFFORT_KEY,
    EFFORT_LEVELS,
    ConfigError,
    FlowConfig,
)
from ahp_host_flow.dispatch import ProviderDispatcher
from ahp_host_flow.engine import AbortSignal, JournalError, RunOptions
from ahp_host_flow.loading import LazyProviders
from ahp_host_flow.sandbox import (
    NodeNotFoundError,
    SandboxError,
    ScriptError,
    find_node,
    run_script,
)

PROVIDER_ID: Final = "flow"

APPROVAL_PROPERTY: Final[Mapping[str, Any]] = {
    "type": "string",
    "title": "Approvals",
    "description": (
        "How each agent() call's tool use is approved. "
        "Unset: the routed provider's approval_mode from flow.toml."
    ),
    "enum": list(APPROVAL_MODES),
    "enumLabels": ["Ask", "Accept edits", "Auto", "Plan"],
    "sessionMutable": True,
    "chatMutable": True,
}

EFFORT_PROPERTY: Final[Mapping[str, Any]] = {
    "type": "string",
    "title": "Effort",
    "enum": ["default", *EFFORT_LEVELS],
    "enumLabels": ["Default", "Low", "Medium", "High", "Extra high", "Max"],
    "default": "default",
    "sessionMutable": True,
    "chatMutable": True,
}

_PROPERTIES: Final[Mapping[str, Mapping[str, Any]]] = {
    APPROVAL_KEY: APPROVAL_PROPERTY,
    EFFORT_KEY: EFFORT_PROPERTY,
}


def _own(values: Mapping[str, Any]) -> dict[str, Any]:
    """The config keys this provider owns."""
    return {key: values[key] for key in _PROPERTIES if key in values}


def _model_info(model: ModelInfo | Mapping[str, Any]) -> ModelInfo | None:
    if isinstance(model, ModelInfo):
        return ModelInfo(
            id=model.id,
            name=model.name,
            vendor=model.vendor,
            max_context_window=model.max_context_window,
            max_prompt_tokens=model.max_prompt_tokens,
            max_output_tokens=model.max_output_tokens,
            supports_vision=model.supports_vision,
        )
    model_id = model.get("id")
    if not isinstance(model_id, str):
        return None
    return ModelInfo(
        id=model_id,
        name=str(model.get("name") or model_id),
        vendor=model.get("vendor"),
        max_context_window=model.get("maxContextWindow"),
        max_prompt_tokens=model.get("maxPromptTokens"),
        max_output_tokens=model.get("maxOutputTokens"),
        supports_vision=model.get("supportsVision"),
    )


def _aggregate_models(
    config: FlowConfig, providers: Mapping[str, AgentProvider]
) -> list[ModelInfo]:
    """Every configured provider's models (first id wins), then aliased ids not yet listed."""
    models: list[ModelInfo] = []
    seen: set[str] = set()
    for name in config.providers:
        try:
            provider = providers.get(name)
        except ConfigError:
            # Already logged when it failed to load; its calls fail on their own.
            continue
        if provider is None:
            continue
        for model in provider.agent.models:
            info = _model_info(model)
            if info is not None and info.id not in seen:
                seen.add(info.id)
                models.append(info)
    for spec in config.providers.values():
        for alias, model_id in spec.models.items():
            if model_id not in seen:
                seen.add(model_id)
                models.append(ModelInfo(id=model_id, name=alias))
    return models


class FlowProvider:
    """Runs Claude Code workflow scripts; each `agent()` call is a worker chat."""

    def __init__(
        self,
        config: FlowConfig,
        *,
        providers: Mapping[str, AgentProvider] | None = None,
        user_workflows_dir: Path | None = None,
    ) -> None:
        self.config = config
        self.providers: Mapping[str, AgentProvider] = (
            providers if providers is not None else LazyProviders(config)
        )
        self.user_workflows_dir = (
            user_workflows_dir
            if user_workflows_dir is not None
            else Path.home() / ".claude" / "workflows"
        )

    @property
    def agent(self) -> AgentInfo:
        return AgentInfo(
            provider=PROVIDER_ID,
            display_name="Workflows",
            description="Runs Claude Code workflow scripts; each agent() call is a worker chat.",
            models=_aggregate_models(self.config, self.providers),
        )

    async def resolve_config(self, request: ConfigRequest) -> ConfigResolution:
        # No defaults are filled in, so an absent value means "use flow.toml".
        values: dict[str, Any] = {}
        for key, prop in _PROPERTIES.items():
            value = request.values.get(key)
            if isinstance(value, str) and value in prop["enum"]:
                values[key] = value
        return ConfigResolution(properties=dict(_PROPERTIES), values=values)

    async def complete_config(self, request: ConfigRequest) -> Sequence[ConfigValue]:
        return ()

    async def create_session(self, context: AgentSessionContext) -> FlowSession:
        return FlowSession(self, context)


class FlowSession:
    """One flow session: each message that starts a run is one run."""

    def __init__(self, provider: FlowProvider, context: AgentSessionContext) -> None:
        self.provider = provider
        self.context = context
        self._values: dict[str, Any] = _own(context.config)
        self._chat_overlays: dict[str, dict[str, Any]] = {}
        self._workers: dict[str, AgentSession] = {}
        self._aborts: dict[str, AbortSignal] = {}

    def _dirs(self) -> list[catalog.SourceDir]:
        config = self.provider.config
        return catalog.source_dirs(
            working_directories=[
                p for d in self.context.working_directories if (p := catalog.directory_of(d))
            ],
            user_dir=self.provider.user_workflows_dir,
            host_dirs=config.workflows.dirs,
            sources=config.workflows.sources,
        )

    def _node(self) -> str | None:
        return find_node(self.provider.config.node)

    async def describe(self) -> SessionDescription:
        dirs = self._dirs()
        entries = await catalog.discover(dirs, node=self._node())
        return SessionDescription(
            customizations=catalog.customizations(entries, dirs, version=__version__)
        )

    async def send_user_message(self, message: UserMessage, sink: TurnSink) -> None:
        context = self.context
        chat = message.chat_uri or context.chat_uri
        parsed = parse_message(message.text)
        if isinstance(parsed, BadRequest):
            await sink.text_delta(parsed.message)
            return
        dirs = self._dirs()
        node = self._node()
        entries = await catalog.discover(dirs, node=node)
        if isinstance(parsed, Help):
            await sink.text_delta(help_text(catalog.visible_names(entries)))
            return
        if parsed.script is not None:
            source, filename = parsed.script, "message.js"
        else:
            ref = f"{parsed.source}:{parsed.name}" if parsed.source else str(parsed.name)
            entry = catalog.resolve(entries, ref)
            if entry is None:
                await sink.text_delta(unknown_text(ref, catalog.visible_names(entries)))
                return
            source, filename = entry.path.read_text(encoding="utf-8"), str(entry.path)
        if node is None:
            await sink.turn_failed(str(NodeNotFoundError()), "flow.node")
            return
        if context.publisher is None:
            await sink.turn_failed("no session publisher", "flow.run")
            return

        config = self.provider.config
        workspace = next(
            (p for d in context.working_directories if (p := catalog.directory_of(d))), None
        )
        try:
            options = RunOptions(
                state_dir=config.state_dir,
                workflow_dirs=tuple(d.path for d in dirs if d.path.is_dir()),
                run_root=workspace,
            )
        except ValueError as exc:
            await sink.turn_failed(str(exc), "flow.run")
            return
        abort = AbortSignal()
        self._aborts[chat] = abort
        dispatcher = ProviderDispatcher(
            config=config,
            providers=self.provider.providers,
            sink=sink,
            publisher=context.publisher,
            chat=chat,
            session_uri=context.session_uri,
            working_directories=context.working_directories,
            run_values=lambda: {**self._values, **self._chat_overlays.get(chat, {})},
            run_model=message.model.id if message.model else None,
            workers=self._workers,
        )

        notes: asyncio.Queue[str | None] = asyncio.Queue()

        async def drain() -> None:
            while (text := await notes.get()) is not None:
                await sink.system_notification(text)

        drainer = asyncio.create_task(drain())

        async def finish() -> None:
            notes.put_nowait(None)
            await drainer

        try:
            try:
                value = await run_script(
                    source,
                    node=node,
                    dispatcher=dispatcher,
                    args=parsed.args,
                    filename=filename,
                    options=options,
                    abort=abort,
                    on_log=notes.put_nowait,
                    on_phase=lambda title: notes.put_nowait(f"Phase: {title}"),
                    on_run_start=lambda run_id: notes.put_nowait(f"Run {run_id}"),
                )
            except ScriptError as exc:
                await finish()
                await sink.turn_failed(str(exc), "flow.script")
                return
            except (SandboxError, JournalError, ValueError) as exc:
                await finish()
                await sink.turn_failed(str(exc), "flow.run")
                return
            await finish()
            body = json.dumps(value, indent=2, ensure_ascii=False)
            await sink.text_delta(f"```json\n{body}\n```")
        finally:
            # Also reached on cancellation: the run is over, so stop the notifier.
            self._aborts.pop(chat, None)
            if not drainer.done():
                drainer.cancel()

    async def config_changed(self, values: Mapping[str, Any]) -> None:
        self._values.update(_own(values))

    async def chat_config_changed(self, chat: str, values: Mapping[str, Any]) -> None:
        mapped = _own(values)
        inner = self._workers.get(chat)
        if inner is None:
            # Applies to the calls a run on this chat makes from now on.
            self._chat_overlays[chat] = mapped
        elif isinstance(inner, ReconfiguresChats):
            await inner.chat_config_changed(chat, mapped)
        elif isinstance(inner, ReconfiguresSessions):
            await inner.config_changed(mapped)

    async def cancel_chat(self, chat_uri: str, reason: str | None = None) -> None:
        abort = self._aborts.get(chat_uri)
        if abort is not None:
            abort.abort()

    async def cancel(self, reason: str | None = None) -> None:
        for abort in list(self._aborts.values()):
            abort.abort()

    async def aclose(self) -> None:
        for inner in list(self._workers.values()):
            with contextlib.suppress(Exception):
                await inner.aclose()
