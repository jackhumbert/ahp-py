"""`FlowProvider`: a workflow run as an AHP session (plan §4)."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import weakref
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Final

from ahp_host import TurnSink
from ahp_host.core.watches import PollingResourceWatcher, WatchRequest
from ahp_host.node import Roots
from ahp_host.provider.base import (
    AgentInfo,
    AgentProvider,
    AgentSession,
    AgentSessionContext,
    ConfigRequest,
    ConfigResolution,
    ConfigValue,
    IdentifiesTurn,
    ModelInfo,
    ObservesSessionState,
    PublishesChatActivity,
    ReadsClientPlugins,
    SessionDescription,
    SteersTurns,
    UserMessage,
)
from ahp_host.provider.changes import OperationContext, OperationHandler
from ahp_protocol.ext import flow
from ahp_protocol.transport import Transport

from ahp_host_flow import __version__, catalog
from ahp_host_flow.commands import (
    BadRequest,
    Help,
    StartRun,
    help_text,
    parse_message,
    parse_ref,
    unknown_text,
)
from ahp_host_flow.config import (
    APPROVAL_KEY,
    APPROVAL_MODES,
    EFFORT_KEY,
    EFFORT_LEVELS,
    ConfigError,
    FlowConfig,
    ProviderConfig,
    make_redactor,
)
from ahp_host_flow.dispatch import (
    FlowAgentError,
    ProviderDispatcher,
    RemoteWorkerChats,
    RoutingDispatcher,
    forward_overlay,
)
from ahp_host_flow.engine import (
    IDLE_TIMEOUT,
    QUOTA_FILE,
    RUN_ID,
    AbortSignal,
    AgentCall,
    ClientDispatcher,
    ClientWorkflow,
    Dispatcher,
    JournalError,
    QuotaLedger,
    RemoteSpec,
    RunOptions,
    VerbHandler,
    WorkflowDir,
    args_error,
    load_journal,
    preamble_text,
    run_dir,
)
from ahp_host_flow.fleet import FleetClients, FleetDispatcher
from ahp_host_flow.gates import MainChatGates, MainChatVerbs
from ahp_host_flow.limits import ProviderSlotsRegistry, probe
from ahp_host_flow.loading import LazyProviders, lazy_verbs, resume_key_hook
from ahp_host_flow.sandbox import (
    NO_ARGS,
    NodeNotFoundError,
    SandboxError,
    ScriptError,
    find_node,
    language_of,
    run_script,
)
from ahp_host_flow.worktree import (
    APPLY_OPERATION,
    COMMIT_OPERATION,
    DISCARD_OPERATION,
    WorktreeChangesets,
    WorktreeError,
)

_log = logging.getLogger(__name__)

PROVIDER_ID: Final = "flow"

#: The version of the resume state a session persists (`{"flow": 1, "runs": [...]}`).
RESUME_STATE_VERSION: Final = 1

#: Why `resume_turn` declines a chat with no run of this session to continue.
NOTHING_TO_RESUME: Final = (
    "nothing to resume on this chat: a flow run is resumed on its main chat, "
    "and a call cut off in a worker chat is dispatched again by its run"
)

#: Why a steering message to a worker chat is refused (plan §4.5).
STEER_READ_ONLY: Final = (
    "worker chats of provider {name!r} are read-only; "
    "set interactive = true under [providers.{name}] in flow.toml to steer them"
)
STEER_NOT_RUNNING: Final = "this worker chat's agent() call is not running"
STEER_UNSUPPORTED: Final = "provider {name!r} cannot take steering mid-turn"

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


@dataclass(frozen=True)
class RunEntry:
    """A run that was live: its main chat, the turn it runs in and its journal.

    The rest is what resuming the turn needs and cannot read back from it
    (`resume_turn` is handed only the chat and the turn id): the run's model,
    how many times the turn was resumed already (the next continuation's
    tool-call ids are `agent-r<k>`), and how many `phaseStarted` /
    `phaseFinished` notices the turn holds, so a continued run does not open a
    phase twice.
    """

    chat: str
    turn_id: str
    run_id: str
    workflow: str | None
    model: str | None = None
    resumes: int = 0
    phases_started: int = 0
    phases_finished: int = 0
    # Who the run is for: the turn's principal only (None for a queued, steered or host-started
    # turn); never the session creator's.
    principal: str | None = None

    def to_json(self) -> dict[str, Any]:
        entry: dict[str, Any] = {
            "chat": self.chat,
            "turnId": self.turn_id,
            "runId": self.run_id,
            "workflow": self.workflow,
            "model": self.model,
            "resumes": self.resumes,
            "phasesStarted": self.phases_started,
            "phasesFinished": self.phases_finished,
        }
        if self.principal is not None:
            entry["principal"] = self.principal
        return entry


def _count(item: Mapping[str, Any], key: str) -> int | None:
    """A non-negative count, 0 when absent, None when malformed."""
    value = item.get(key, 0)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _parse_runs(state: Mapping[str, Any] | None) -> dict[str, RunEntry]:
    """The runs a persisted resume state names, by main chat; malformed entries are skipped."""
    if state is None:
        return {}
    runs = state.get("runs")
    if state.get("flow") != RESUME_STATE_VERSION or not isinstance(runs, list):
        _log.warning("ignoring flow resume state of an unknown shape")
        return {}
    found: dict[str, RunEntry] = {}
    for item in runs:
        if isinstance(item, Mapping):
            chat, turn_id, run_id = item.get("chat"), item.get("turnId"), item.get("runId")
            workflow, model = item.get("workflow"), item.get("model")
            principal = item.get("principal")
            counts = [_count(item, k) for k in ("resumes", "phasesStarted", "phasesFinished")]
            if (
                isinstance(chat, str)
                and isinstance(turn_id, str)
                and isinstance(run_id, str)
                and RUN_ID.fullmatch(run_id)
                and (workflow is None or isinstance(workflow, str))
                and (model is None or isinstance(model, str))
                and (principal is None or isinstance(principal, str))
                and None not in counts
            ):
                resumes, started, finished = (int(c or 0) for c in counts)
                found[chat] = RunEntry(
                    chat, turn_id, run_id, workflow, model, resumes, started, finished, principal
                )
                continue
        _log.warning("ignoring a malformed run entry in the flow resume state")
    return found


def _parse_worktrees(state: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    """The kept worktrees a persisted resume state names; non-mapping items are dropped."""
    if state is None or state.get("flow") != RESUME_STATE_VERSION:
        return []
    items = state.get("worktrees")
    if not isinstance(items, list):
        return []
    found: list[dict[str, Any]] = []
    for item in items:
        if isinstance(item, Mapping):
            found.append(dict(item))
        else:
            _log.warning("ignoring a malformed kept worktree entry in the flow resume state")
    return found


class FlowProvider:
    """Runs Claude Code workflow scripts; each `agent()` call is a worker chat."""

    def __init__(
        self,
        config: FlowConfig,
        *,
        providers: Mapping[str, AgentProvider] | None = None,
        roots: Roots | None = None,
        user_workflows_dir: Path | None = None,
        fleet_client_wait: float = 10.0,
        remote_dialer: Callable[[ProviderConfig], Callable[[], Awaitable[Transport]]] | None = None,
    ) -> None:
        self.config = config
        #: The folders the host serves; inner providers get them plus the runs folder.
        self.roots = roots
        #: Dials a remote provider's host; tests inject an in-memory one, production dials `url`.
        self.remote_dialer = remote_dialer
        #: Seconds a continued run waits for the gateway to rejoin before a fleet call fails
        #: (the gateway's redial backoff, 0.5 s doubling, reaches 4 redials in 7.5 s), and a
        #: fleet tool call whose gateway left waits for it to come back to try once more.
        self.fleet_client_wait = fleet_client_wait
        self.redactor = make_redactor(config.redact)
        #: What each principal used today, shared by every run; None without `[quotas]`.
        self.quotas: QuotaLedger | None = (
            QuotaLedger(config.state_dir / QUOTA_FILE, config.quotas) if config.quotas.any else None
        )
        #: Per-provider concurrency (`providers.<name>.concurrency`), shared by every session and
        #: run of this provider.
        self.provider_slots = ProviderSlotsRegistry(
            config, on_log=_log.info, prober=self._probe_provider
        )
        #: Numbers the throwaway sessions of a probe.
        self._probe_count = 0
        #: Kept worktrees published as changesets; the provider declares their operations.
        self.worktree_changesets = WorktreeChangesets(
            branch=config.changesets.branch,
            trailer=config.changesets.trailer,
            mask=self.redactor.mask,
        )
        self.providers: Mapping[str, AgentProvider] = (
            providers if providers is not None else LazyProviders(config, roots=roots)
        )
        #: One `LazyVerb` per `[verbs]` name, shared by every session: a handler module
        #: is imported once.
        self.verbs: Mapping[str, VerbHandler] = lazy_verbs(config)
        self.user_workflows_dir = (
            user_workflows_dir
            if user_workflows_dir is not None
            else Path.home() / ".claude" / "workflows"
        )
        #: Every session this provider created or resumed, until it is collected.
        self._sessions: weakref.WeakSet[FlowSession] = weakref.WeakSet()
        #: Client-chosen run ids claimed by a run that has not yet created its journal.
        self._reserved_run_ids: set[str] = set()

    async def _probe_provider(self, name: str) -> int | None:
        """Runs the concurrency probe on in-process provider *name* (two throwaway sessions)."""
        try:
            provider = self.providers[name]
        except Exception as exc:
            _log.warning("provider %s: probe cannot load it (%s); no limit", name, exc)
            return None

        def context() -> AgentSessionContext:
            self._probe_count += 1
            uri = f"flow-probe:/{name}/{self._probe_count}"
            return AgentSessionContext(
                session_uri=uri,
                chat_uri=f"{uri}/chat",
                provider_id=provider.agent.provider,
                publisher=None,
            )

        return await probe(provider, context, self.provider_slots.on_log, name=name)

    def live_run_ids(self) -> frozenset[str]:
        """The run id of every run live now (not the ones a restart left to resume)."""
        return frozenset(
            entry.run_id for session in list(self._sessions) for entry in session._runs.values()
        ) | frozenset(self._reserved_run_ids)

    def changeset_operations(self) -> Mapping[str, OperationHandler]:
        """Apply, Commit and Discard for kept worktrees (`ProvidesOperations`); called once."""
        changesets = self.worktree_changesets
        changesets.operations_registered = True

        async def apply(
            uri: str, _op: str, _target: Mapping[str, Any] | None, _context: OperationContext
        ) -> None:
            await changesets.apply(uri)

        async def commit(
            uri: str, _op: str, _target: Mapping[str, Any] | None, context: OperationContext
        ) -> Mapping[str, Any]:
            return await changesets.commit(uri, params=context.params, principal=context.principal)

        async def discard(
            uri: str, _op: str, _target: Mapping[str, Any] | None, context: OperationContext
        ) -> None:
            reason = context.params.get("reason")
            if reason is not None and not isinstance(reason, str):
                raise WorktreeError("params.reason must be a string")
            await changesets.discard(uri, reason=(reason or "").strip() or None)

        return {APPLY_OPERATION: apply, COMMIT_OPERATION: commit, DISCARD_OPERATION: discard}

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
        session = FlowSession(self, context)
        self._sessions.add(session)
        await session.start_watching()
        return session

    async def resume_session(self, context: AgentSessionContext) -> FlowSession:
        session = FlowSession(self, context)
        self._sessions.add(session)
        await session.start_watching()
        await session.restore_worktrees()
        return session

    def resume_on_restore(self, resume_state: Mapping[str, Any]) -> bool:
        """`ResumesOnRestore`: only a session holding kept worktrees must resume eagerly."""
        return bool(_parse_worktrees(resume_state))

    async def resume_state_of(self, session: AgentSession) -> Mapping[str, Any] | None:
        return session.resume_state() if isinstance(session, FlowSession) else None


class FlowSession:
    """One flow session: each message that starts a run is one run."""

    def __init__(self, provider: FlowProvider, context: AgentSessionContext) -> None:
        self.provider = provider
        self.context = context
        self._values: dict[str, Any] = _own(context.config)
        self._chat_overlays: dict[str, dict[str, Any]] = {}
        self._workers: dict[str, AgentSession] = {}
        #: The provider name of each live worker chat (shared with the dispatcher).
        self._worker_providers: dict[str, str] = {}
        self._aborts: dict[str, AbortSignal] = {}
        #: Live runs by main chat.
        self._runs: dict[str, RunEntry] = {}
        #: Runs the last host process left active, until their turn is resumed
        #: (`resume_turn`) or the chat moves on to another turn.
        self._restored: dict[str, RunEntry] = _parse_runs(context.resume_state)
        #: Kept worktrees the last host process held, until `restore_worktrees` republishes them.
        self._restored_worktrees: list[dict[str, Any]] = _parse_worktrees(context.resume_state)
        #: The customizations last published (or described), to see when a source changes.
        self._published: list[dict[str, Any]] | None = None
        #: Client plugins' workflows already read, by (plugin id, uri, nonce): a plugin
        #: republished with a new nonce is read again.
        self._client_cache: dict[tuple[str, str, str | None], list[catalog.WorkflowEntry]] = {}
        #: The client plugins the last read saw (`describe` lists from these, never reads).
        self._client_plugins_seen: list[catalog.ClientPlugin] = []
        self._refresh: asyncio.Task[None] | None = None
        self._refresh_again = False
        #: A pending refresh that must read the clients (a watch-triggered one never does).
        self._refresh_reads_clients = False
        self._watcher: PollingResourceWatcher | None = None
        self._watched: list[WatchRequest] = []
        #: The active clients that offer the fleet tools (a gateway joined to this session).
        self._fleet = FleetClients(
            [{"clientId": context.active_client_id, "tools": list(context.client_tools)}]
            if context.active_client_id
            else []
        )

    async def active_clients_changed(self, clients: Sequence[Mapping[str, Any]]) -> None:
        self._fleet.update(clients)
        if "client" in self.provider.config.workflows.sources:
            # Never read inline: this runs in the publishing client's notification handler,
            # and a read back to that client would deadlock.
            self._schedule_refresh(read_clients=True)

    def _schedule_refresh(self, read_clients: bool) -> None:
        """Coalesce a refresh; one that reads the clients is never downgraded."""
        self._refresh_reads_clients = self._refresh_reads_clients or read_clients
        if self._refresh is not None and not self._refresh.done():
            self._refresh_again = True
        else:
            self._refresh = asyncio.create_task(self._refresh_loop())

    async def _refresh_loop(self) -> None:
        self._refresh_again = True
        while self._refresh_again:
            self._refresh_again = False
            read_clients = self._refresh_reads_clients
            self._refresh_reads_clients = False
            await self._republish(read_clients=read_clients)

    async def start_watching(self) -> None:
        """Watch the local source dirs, so a change republishes the customizations."""
        interval = self.provider.config.workflows.watch_interval
        if interval <= 0:
            return
        self._watcher = PollingResourceWatcher(interval=interval)
        paths = {s.path.resolve() for s in self._dirs()}
        # A missing dir scans as empty and reports `added` when it appears.
        for path in sorted(paths):
            request = WatchRequest(root=path.as_uri(), recursive=False, includes=("*.js", "*.py"))
            self._watched.append(request)
            await self._watcher.start(request, self._on_source_change)

    def _on_source_change(self, changes: Sequence[Any]) -> None:
        # Local sources only: never read a client from here.
        self._schedule_refresh(read_clients=False)

    async def _republish(self, *, read_clients: bool) -> None:
        """Re-read the sources and publish the customizations if they changed."""
        try:
            _, current = await self._catalog(self._node(), read_clients=read_clients)
            publisher = self.context.publisher
            if current != self._published and publisher is not None:
                await publisher.customizations_changed(current)
                self._published = current
        except Exception:
            _log.exception("cannot re-publish the workflow customizations")

    async def _client_workflows(
        self, node: str | None
    ) -> tuple[list[catalog.ClientPlugin], list[catalog.WorkflowEntry]]:
        """The client plugins published into this session and their workflows (read or cached)."""
        publisher = self.context.publisher
        if "client" not in self.provider.config.workflows.sources or not isinstance(
            publisher, ReadsClientPlugins
        ):
            return [], []
        plugins = catalog.client_plugins(publisher.session_plugins())
        keys = {(p.plugin_id, p.uri, p.nonce) for p in plugins}
        for key in [k for k in self._client_cache if k not in keys]:
            del self._client_cache[key]
        entries: list[catalog.WorkflowEntry] = []
        for plugin in plugins:
            key = (plugin.plugin_id, plugin.uri, plugin.nonce)
            # A live plugin without a nonce cannot tell when it changed; a captured copy
            # (no clientId) never changes.
            cached = key in self._client_cache and (
                plugin.nonce is not None or plugin.client_id is None
            )
            if not cached:
                self._client_cache[key] = await catalog.read_client_workflows(
                    plugin, publisher, node=node
                )
            entries.extend(self._client_cache[key])
        self._client_plugins_seen = plugins
        return plugins, entries

    async def _catalog(
        self, node: str | None, *, read_clients: bool
    ) -> tuple[list[catalog.WorkflowEntry], list[dict[str, Any]]]:
        """Every workflow entry and the customizations that list them."""
        dirs = self._dirs()
        if read_clients:
            plugins, client_entries = await self._client_workflows(node)
        else:
            plugins = self._client_plugins_seen
            client_entries = [
                e
                for p in plugins
                for e in self._client_cache.get((p.plugin_id, p.uri, p.nonce), [])
            ]
        entries = await catalog.discover(
            dirs,
            node=node,
            python_sources=self.provider.config.workflows.python_sources,
            client=client_entries,
        )
        return entries, catalog.customizations(entries, dirs, version=__version__, plugins=plugins)

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

    def resume_state(self) -> dict[str, Any]:
        """The runs to carry across a restart; restored ones not yet resumed are kept."""
        entries = {**self._restored, **self._runs}
        state: dict[str, Any] = {
            "flow": RESUME_STATE_VERSION,
            "runs": [e.to_json() for _, e in sorted(entries.items())],
        }
        worktrees = self.provider.worktree_changesets.kept_for(self.context.session_uri)
        if worktrees:
            state["worktrees"] = worktrees
        return state

    async def restore_worktrees(self) -> None:
        """Republish the kept worktrees the last host process held (once)."""
        publisher = self.context.publisher
        entries, self._restored_worktrees = self._restored_worktrees, []
        if publisher is None:
            return
        await self.provider.worktree_changesets.restore(
            entries, publisher, session=self.context.session_uri
        )

    async def resume_turn(self, chat_uri: str, turn_id: str, sink: TurnSink) -> None:
        """Resume a run's main turn that a host restart failed: relaunch it from its journal.

        `ResumesTurns`. The host calls it once, by itself, for a turn a
        drain-restart cut off, and on a client's `chat/turnResume` for one a
        crash lost. Finished `agent()` calls replay by key; one that started and
        never finished is dispatched again live. Any other chat -- a worker chat,
        a turn that is not the run's, a run whose journal is gone -- is declined
        by failing the turn again, not resumable, so a client is not offered the
        same dead end twice.
        """
        entry = self._restored.pop(chat_uri, None)
        if entry is None:
            await sink.turn_failed(NOTHING_TO_RESUME, "flow.run")
            return
        if entry.turn_id != turn_id:
            _log.warning(
                "dropping run %s: it ran in turn %s, not %s", entry.run_id, entry.turn_id, turn_id
            )
            await sink.turn_failed(NOTHING_TO_RESUME, "flow.run")
            return
        try:
            header = load_journal(self.provider.config.state_dir, entry.run_id).header
        except (JournalError, ValueError) as exc:
            # Dropped, not kept: an entry nothing can continue would otherwise
            # ride along in the resume state forever.
            _log.warning("cannot continue run %s, dropping it: %s", entry.run_id, exc)
            await sink.turn_failed(self.provider.redactor.mask(str(exc)), "flow.run")
            return
        node = self._node()
        if node is None and language_of(header.filename) == "js":
            # Kept, and resumable: the run can go on once Node is found.
            self._restored[chat_uri] = entry
            await sink.turn_failed(str(NodeNotFoundError()), "flow.node", resumable=True)
            return
        resumed = replace(entry, resumes=entry.resumes + 1)
        # Live from here, before the first await: the host persists the resume
        # state at any time (a quick second shutdown, a debounced save), and the
        # entry has just left `_restored` while `on_run_start` has not yet put
        # the continued run in `_runs`. Without this the run was in neither.
        self._runs[chat_uri] = resumed
        try:
            # The cached client workflows: a resume never reads a client.
            entries, _ = await self._catalog(node, read_clients=False)
            # The calls the restart cut off were closed (cancelled) when the
            # host failed the turn; the re-dispatched ones get ids of their own.
            await self._run_workflow(
                chat=chat_uri,
                sink=sink,
                source=None,
                filename=header.filename,
                args=NO_ARGS,
                workflow=resumed.workflow,
                model=resumed.model,
                node=node,
                dirs=self._dirs(),
                client_entries=[e for e in entries if e.source == "client"],
                resume_from=resumed.run_id,
                resumed=resumed,
                principal=resumed.principal,
            )
        finally:
            self._runs.pop(chat_uri, None)

    async def describe(self) -> SessionDescription:
        # Describe runs during session bring-up: never read back from a client here.
        _, self._published = await self._catalog(self._node(), read_clients=False)
        return SessionDescription(customizations=self._published)

    async def send_user_message(self, message: UserMessage, sink: TurnSink) -> None:
        context = self.context
        chat = message.chat_uri or context.chat_uri
        # A new turn on the chat: the restored run's turn is no longer the latest,
        # so `chat/turnResume` can never reach it again.
        self._restored.pop(chat, None)
        redactor = self.provider.redactor
        try:
            start = flow.parse_workflow_start(message.raw.get("_meta"))
        except ValueError as exc:
            await sink.turn_failed(
                redactor.mask(f"bad workflow start: {exc}"), flow.BAD_ARGS_ERROR_TYPE
            )
            return
        run_id: str | None = None
        parsed: Help | BadRequest | StartRun
        if start is not None:
            # A typed start wins over the text (which stays `/name {json}` for stock hosts).
            ref_parts = parse_ref(start.workflow)
            if isinstance(ref_parts, BadRequest):
                await sink.turn_failed(
                    redactor.mask("bad workflow start: " + ref_parts.message),
                    flow.BAD_ARGS_ERROR_TYPE,
                )
                return
            parsed = StartRun(
                ref_parts[1], ref_parts[0], None, start.args if start.has_args else NO_ARGS
            )
            run_id = start.run_id
        else:
            parsed = parse_message(message.text)
        if isinstance(parsed, BadRequest):
            await sink.text_delta(parsed.message)
            return
        dirs = self._dirs()
        node = self._node()
        entries, current = await self._catalog(node, read_clients=True)
        if current != self._published and context.publisher is not None:
            await context.publisher.customizations_changed(current)
            self._published = current
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
            if parsed.args is not NO_ARGS:
                schema, _ = catalog.args_schema_of(entry.meta)
                if schema is not None and (err := args_error(schema, parsed.args)) is not None:
                    await sink.turn_failed(
                        redactor.mask(f"bad args for /{ref}: {err}"), flow.BAD_ARGS_ERROR_TYPE
                    )
                    return
            source, filename = entry.source_text(), entry.filename()
        if node is None and language_of(filename) == "js":
            await sink.turn_failed(str(NodeNotFoundError()), "flow.node")
            return
        if context.publisher is None:
            await sink.turn_failed("no session publisher", "flow.run")
            return

        # The turn's principal only: never the session creator's, so a turn with none (queued,
        # steered, host-started, or no policy hook) cannot act as whoever created the session.
        principal = message.principal
        await self._run_workflow(
            chat=chat,
            sink=sink,
            source=source,
            filename=filename,
            args=parsed.args,
            workflow=parsed.name,
            model=message.model.id if message.model else None,
            node=node,
            dirs=dirs,
            client_entries=[e for e in entries if e.source == "client"],
            principal=principal,
            run_id=run_id,
        )

    async def _run_workflow(
        self,
        *,
        chat: str,
        sink: TurnSink,
        source: str | None,
        filename: str,
        args: object,
        workflow: str | None,
        model: str | None,
        node: str | None,
        dirs: list[catalog.SourceDir],
        client_entries: Sequence[catalog.WorkflowEntry] = (),
        resume_from: str | None = None,
        resumed: RunEntry | None = None,
        principal: str | None = None,
        run_id: str | None = None,
    ) -> None:
        """Run one workflow as the turn *sink* publishes; *resume_from* continues that run.

        *resumed* is, for a continued run, its entry: how often the turn was
        resumed (the call ids' `agent-r<k>` prefix) and how many `phaseStarted`
        and `phaseFinished` notices the turn already holds -- the script
        replays its `phase()` calls, and a phase the transcript has opened must
        not be opened a second time.

        *run_id* is the id a client asked for (never with *resume_from*): used when free,
        else the run gets a fresh one and says so.
        """
        context = self.context
        config = self.provider.config
        publisher = context.publisher
        if publisher is None:
            await sink.turn_failed("no session publisher", "flow.run")
            return
        redactor = self.provider.redactor
        workspace = next(
            (p for d in context.working_directories if (p := catalog.directory_of(d))), None
        )
        # Where workflow() may load a .py from: the allowed sources' workflow directories only
        # (for the workspace, its `.claude/workflows`, not the whole working directory), and
        # never a source that `[workflows] sources` leaves out. Always set on a provider run.
        python_dirs = catalog.python_dirs(dirs, config.workflows.python_sources)
        taken_run_id: str | None = None
        if run_id is not None and not (
            RUN_ID.fullmatch(run_id)
            and run_id not in self.provider.live_run_ids()
            and not run_dir(config.state_dir, run_id).exists()
        ):
            taken_run_id, run_id = run_id, None
        # Claimed now, with no await since the check above, so a second start asking for the
        # same id sees it taken until this run's journal exists (its directory then says so).
        reserved = run_id
        if reserved is not None:
            self.provider._reserved_run_ids.add(reserved)
        try:
            key_extra, key_mode = resume_key_hook(config, model)
            limits = config.limits
            # 'mean' is RunOptions' None; an absent reserve keeps its default.
            reserve: dict[str, Any] = {}
            if limits.reserve is not None:
                reserve["reserve"] = None if limits.reserve == "mean" else limits.reserve
            options = RunOptions(
                state_dir=config.state_dir,
                workflow_dirs=tuple(WorkflowDir(d.source, d.path) for d in dirs if d.path.is_dir()),
                client_workflows=tuple(
                    ClientWorkflow(e.name, e.source_text(), e.filename())
                    for e in client_entries
                    if e.source == "client" and e.language == "js"
                ),
                python_dirs=python_dirs,
                run_root=workspace,
                redact=redactor.mask_value,
                verbs=self.provider.verbs,
                key_extra=key_extra,
                key_mode=key_mode,
                resume_from=resume_from,
                continue_run=resume_from is not None,
                run_id=run_id,
                budget_total=limits.budget,
                input_budget=limits.input_budget,
                cost_budget=limits.cost_budget,
                wall_budget=limits.wall_budget,
                timeout=limits.timeout,
                concurrency=limits.concurrency,
                count_cache=limits.count_cache,
                quota=None
                if self.provider.quotas is None
                else self.provider.quotas.for_run(principal),
                **reserve,
            )
        except ValueError as exc:
            if reserved is not None:
                self.provider._reserved_run_ids.discard(reserved)
            await sink.turn_failed(redactor.mask(str(exc)), "flow.run")
            return
        abort = AbortSignal()
        self._aborts[chat] = abort
        notes: asyncio.Queue[tuple[str, Mapping[str, Any] | None] | None] = asyncio.Queue()
        current_phase: str | None = None
        #: Phases are announced in script order and finish in the same order, so
        #: the first `started_count` phase() calls and the first `finished_count`
        #: finishes are already in the transcript (non-zero only when continuing).
        started_count = resumed.phases_started if resumed is not None else 0
        finished_count = resumed.phases_finished if resumed is not None else 0
        phase_calls = 0
        #: The phase notices the turn holds, counted as each is published (so a
        #: persisted resume state never claims one the transcript lacks).
        published = [started_count, finished_count]
        resumes = resumed.resumes if resumed is not None else 0

        def remember_phases() -> None:
            entry = self._runs.get(chat)
            if entry is not None:
                self._runs[chat] = replace(
                    entry, phases_started=published[0], phases_finished=published[1]
                )

        async def drain() -> None:
            while (note := await notes.get()) is not None:
                text, note_meta = note
                if note_meta is not None:
                    await sink.system_notification(text, meta=note_meta)
                    noted = flow.descriptor_of(note_meta) or {}
                    if "phaseStarted" in noted or "phaseFinished" in noted:
                        published[0] += "phaseStarted" in noted
                        published[1] += "phaseFinished" in noted
                        remember_phases()
                else:
                    await sink.system_notification(text)

        drainer = asyncio.create_task(drain())

        def on_log(text: str) -> None:
            notes.put_nowait((redactor.mask(text), None))

        if taken_run_id is not None:
            on_log(f"run id {taken_run_id!r} is not available; this run gets a fresh one")

        # A continued run's calls get ids of their own: the cut-off ones stay in the turn.
        call_id_prefix = f"agent-r{resumes}" if resumes else "agent"

        gates = MainChatGates(
            sink=sink,
            publisher=publisher,
            chat=chat,
            redactor=redactor,
            on_log=on_log,
            call_id_prefix=f"gate-r{resumes}" if resumes else "gate",
            working_directories=context.working_directories,
        )

        # A continued run's verb calls get ids of their own too; replayed verbs are not shown.
        verb_host = MainChatVerbs(
            sink=sink,
            publisher=publisher,
            chat=chat,
            session_uri=context.session_uri,
            redactor=redactor,
            on_log=on_log,
            call_id_prefix=f"verb-r{resumes}" if resumes else "verb",
            working_directories=context.working_directories,
            principal=principal,
        )

        view = publisher.session_state if isinstance(publisher, ObservesSessionState) else None
        remote_workers = RemoteWorkerChats(
            config=config,
            sink=sink,
            publisher=publisher,
            chat=chat,
            run_values=lambda: {**self._values, **self._chat_overlays.get(chat, {})},
            redactor=redactor,
            on_log=on_log,
            call_id_prefix=call_id_prefix,
            session_view=view,
        )

        def remote_for(name: str, spec: ProviderConfig) -> Dispatcher:
            token: str | None = None
            if spec.token_file is not None:
                try:
                    token = spec.token_file.read_text(encoding="utf-8").strip()
                except OSError as exc:
                    raise FlowAgentError(
                        f"provider {name!r}: cannot read token_file: {exc}"
                    ) from exc
            dialer = self.provider.remote_dialer
            return ClientDispatcher(
                RemoteSpec(
                    name,
                    str(spec.agent),
                    spec.url,
                    token,
                    transport_factory=dialer(spec) if dialer is not None else None,
                    models=spec.models,
                    run_model=model,
                    idle_timeout=(
                        spec.idle_timeout if spec.idle_timeout is not None else IDLE_TIMEOUT
                    ),
                    approval_timeout=spec.approval_timeout,
                    preamble=preamble_text(spec.preamble),
                ),
                workers=remote_workers,
                on_log=on_log,
            )

        dispatcher = RoutingDispatcher(
            config,
            slots=self.provider.provider_slots,
            inprocess=ProviderDispatcher(
                config=config,
                providers=self.provider.providers,
                sink=sink,
                publisher=publisher,
                chat=chat,
                session_uri=context.session_uri,
                working_directories=context.working_directories,
                run_values=lambda: {**self._values, **self._chat_overlays.get(chat, {})},
                run_model=model,
                workers=self._workers,
                pending_overlays=self._chat_overlays,
                redactor=redactor,
                on_log=on_log,
                call_id_prefix=call_id_prefix,
                changesets=self.provider.worktree_changesets,
                worker_providers=self._worker_providers,
                session_view=view,
                principal=principal,
            ),
            fleet=FleetDispatcher(
                config=config,
                sink=sink,
                publisher=publisher,
                chat=chat,
                clients=self._fleet,
                client_wait=self.provider.fleet_client_wait if resume_from is not None else 0.0,
                rejoin_wait=self.provider.fleet_client_wait,
                run_values=lambda: {**self._values, **self._chat_overlays.get(chat, {})},
                run_model=model,
                aborted=lambda: abort.aborted,
                redactor=redactor,
                on_log=on_log,
                call_id_prefix=call_id_prefix,
            ),
            remote=remote_for,
        )

        # Liveness for a client's idle timeout: a tick on the main chat while a step is in flight.
        running = 0
        beat_task: asyncio.Task[None] | None = None
        beating = False

        def on_agent_start(_call: AgentCall) -> None:
            nonlocal running
            running += 1

        def on_agent_end(_call: AgentCall, _value: Any) -> None:
            nonlocal running
            running -= 1

        async def beat(chat_activity: PublishesChatActivity) -> None:
            nonlocal beating
            tick = 0
            while True:
                await asyncio.sleep(limits.heartbeat)
                k = running
                if k < 1:
                    continue
                tick += 1
                beating = True
                try:
                    await chat_activity.chat_activity_changed(
                        chat,
                        f"Running {k} step" + ("s" if k != 1 else ""),
                        meta=flow.meta(flow.heartbeat(running=k, tick=tick)),
                    )
                except Exception:
                    _log.debug("heartbeat for %s failed", chat, exc_info=True)

        def on_run_start(run_id: str) -> None:
            dispatcher.run_id = run_id
            if isinstance(sink, IdentifiesTurn):
                # The continued run journals under a new id, and a later resume
                # continues from that journal.
                self._runs[chat] = RunEntry(
                    chat,
                    sink.turn_id,
                    run_id,
                    workflow,
                    model,
                    resumes,
                    published[0],
                    published[1],
                    principal,
                )
            if resume_from is None:
                notes.put_nowait((f"Run {run_id}", flow.meta(flow.run_started(run_id, workflow))))
            else:
                # No descriptor: the transcript keeps its one runStarted, and run_view
                # keeps the original run id.
                notes.put_nowait(
                    (f"Run {resume_from} resumed after a host restart as run {run_id}", None)
                )

        def on_phase(title: str) -> None:
            nonlocal current_phase, phase_calls
            end_phase()
            if phase_calls >= started_count:
                notes.put_nowait((f"Phase: {title}", flow.meta(flow.phase_started(title))))
            phase_calls += 1
            current_phase = title

        def end_phase() -> None:
            nonlocal finished_count
            if current_phase is not None and phase_calls > finished_count:
                finished_count = phase_calls
                notes.put_nowait(
                    (
                        f"Phase finished: {current_phase}",
                        flow.meta(flow.phase_finished(current_phase)),
                    )
                )

        async def finish() -> None:
            notes.put_nowait(None)
            await drainer

        async def fail(exc: Exception, reason: str) -> None:
            end_phase()
            message = redactor.mask(str(exc))
            notes.put_nowait(
                (f"Workflow failed: {message}", flow.meta(flow.run_finished("error", message)))
            )
            await finish()
            await sink.turn_failed(message, reason)

        if isinstance(publisher, PublishesChatActivity):
            beat_task = asyncio.create_task(beat(publisher))
        try:
            try:
                value = await run_script(
                    source,
                    node=node,
                    dispatcher=dispatcher,
                    # NO_ARGS (the default) leaves a continued run on its journal's args.
                    args=args,
                    filename=filename,
                    options=options,
                    abort=abort,
                    on_log=on_log,
                    on_phase=on_phase,
                    on_agent_start=on_agent_start,
                    on_agent_end=on_agent_end,
                    on_run_start=on_run_start,
                    gates=gates,
                    verb_host=verb_host,
                )
            except ScriptError as exc:
                await fail(exc, "flow.script")
                return
            except (SandboxError, JournalError, ValueError) as exc:
                await fail(exc, "flow.run")
                return
            end_phase()
            notes.put_nowait(("Workflow finished", flow.meta(flow.run_finished("ok"))))
            await finish()
            body = json.dumps(redactor.mask_value(value), indent=2, ensure_ascii=False)
            await sink.text_delta(f"```json\n{body}\n```")
        finally:
            # Also reached on cancellation: the run is over, so stop the notifier.
            if beat_task is not None:
                beat_task.cancel()
                await asyncio.gather(beat_task, return_exceptions=True)
                if beating and isinstance(publisher, PublishesChatActivity):
                    with contextlib.suppress(Exception):
                        await publisher.chat_activity_changed(chat, None)
            if reserved is not None:
                self.provider._reserved_run_ids.discard(reserved)
            self._aborts.pop(chat, None)
            self._runs.pop(chat, None)
            if not drainer.done():
                drainer.cancel()
            with contextlib.suppress(Exception):
                await dispatcher.aclose()

    async def config_changed(self, values: Mapping[str, Any]) -> None:
        """Take the session's new config; an Approvals change also reaches every live worker.

        "Approve all for this run" applies at once: the latest change wins for running
        workers, and new calls still resolve their mode through `inner_config` as before.
        Effort is not fanned out.
        """
        self._values.update(_own(values))
        mode = values.get(APPROVAL_KEY)
        if isinstance(mode, str) and mode in APPROVAL_MODES:
            for chat, inner in list(self._workers.items()):
                # One worker failing never stops the others.
                with contextlib.suppress(Exception):
                    await forward_overlay(inner, chat, {APPROVAL_KEY: mode})

    async def chat_config_changed(self, chat: str, values: Mapping[str, Any]) -> None:
        mapped = _own(values)
        inner = self._workers.get(chat)
        if inner is None:
            # Applies to the calls a run on this chat makes from now on.
            self._chat_overlays[chat] = mapped
        else:
            await forward_overlay(inner, chat, mapped)

    def steering_rejection(self, chat_uri: str) -> str | None:
        """Why steering *chat_uri* is refused; None for the main chat and a steerable worker."""
        name = self._worker_providers.get(chat_uri)
        if name is None:
            return None
        spec = self.provider.config.providers.get(name)
        if spec is None or not spec.interactive:
            return STEER_READ_ONLY.format(name=name)
        inner = self._workers.get(chat_uri)
        if inner is None:
            return STEER_NOT_RUNNING
        if not isinstance(inner, SteersTurns):
            return STEER_UNSUPPORTED.format(name=name)
        return None

    async def steer(self, chat_uri: str, message: UserMessage) -> bool:
        """Forward a steering message for a worker chat to its inner session."""
        if chat_uri not in self._worker_providers or self.steering_rejection(chat_uri) is not None:
            return False
        inner = self._workers.get(chat_uri)
        if not isinstance(inner, SteersTurns):
            return False
        try:
            return await inner.steer(chat_uri, message)
        except Exception:
            _log.warning("steering worker chat %s failed", chat_uri, exc_info=True)
            return False

    async def cancel_chat(self, chat_uri: str, reason: str | None = None) -> None:
        abort = self._aborts.get(chat_uri)
        if abort is not None:
            abort.abort()

    async def cancel(self, reason: str | None = None) -> None:
        for abort in list(self._aborts.values()):
            abort.abort()

    async def aclose(self) -> None:
        if self._watcher is not None:
            for request in self._watched:
                await self._watcher.stop(request)
        if self._refresh is not None and not self._refresh.done():
            self._refresh.cancel()
        for inner in list(self._workers.values()):
            with contextlib.suppress(Exception):
                await inner.aclose()
