"""The agent-provider extension point.

Per ADR 0003 a provider emits **neutral events describing what the agent did**,
never AHP actions. The host maps those to `chat/*` actions, assigns `serverSeq`,
applies the reducer and fans out.

That boundary is the difference between an adapter that survives a spec bump and
one that does not. The only prior-art provider kit has adapters emit
`session/delta` / `session/responsePart` / `session/turnComplete` directly --
spec 0.4.0 relocated all three to the chat channel, and every adapter written
against it is wire-dead today. Nothing about the agent runtimes changed.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from ahp_protocol.reducers.clock import now_iso

__all__ = [
    "AgentInfo",
    "AgentProvider",
    "AgentSession",
    "AgentSessionContext",
    "ArchivesSessions",
    "AuthChallenge",
    "BackgroundWork",
    "BackgroundsMcpServers",
    "Canvas",
    "ClientToolCall",
    "Completes",
    "CompletionItem",
    "CompletionRequest",
    "ConfigRequest",
    "ConfigResolution",
    "ConfigValue",
    "ConfiguresSessions",
    "DescribesSession",
    "DisposesSessions",
    "FollowsActiveClients",
    "FollowsWorkingDirectories",
    "ForkedFrom",
    "HandlesCustomizations",
    "InputOutcome",
    "InputQuestion",
    "InputRequest",
    "ManagesMcpServers",
    "ModelInfo",
    "ModelSelection",
    "OpensSessions",
    "ProviderTerminal",
    "ResumableAgentProvider",
    "SessionDescription",
    "SessionDirectory",
    "SessionPublisher",
    "ToolConfirmation",
    "ToolConfirmationOutcome",
    "ToolResult",
    "TransfersChats",
    "TurnSink",
    "UserMessage",
]


@dataclass(frozen=True)
class ModelInfo:
    """`SessionModelInfo`. A model in the client's picker.

    Typed rather than a bare mapping because the mapping accepted ``{"id",
    "name"}`` in silence, which is what this project shipped and what every
    adapter copied from the demo would ship too. `provider` is declared
    non-optional by the spec and was the field most often missing.

    Each optional field below has a confirmed reader in the shipping client:

    * ``provider`` -- picks the picker's section, which otherwise falls back
      to a generic vendor bucket
    * ``max_prompt_tokens`` -- ``maxInputTokens``, the "Max context" row and
      the denominator of the usage meter; without it the meter cannot render
    * ``max_output_tokens`` / ``max_context_window`` -- the rest of that row
    * ``supports_vision`` -- gates image attachments; absent means refused
    * ``policy_state`` -- ``"disabled"`` removes the model from the picker,
      which is the only way to list a model while keeping it unselectable
    """

    id: str
    name: str
    #: Non-optional in the spec. Defaults to the owning agent's provider at
    #: serialisation time rather than being required here, so the common case
    #: stays a two-argument construction.
    provider: str | None = None
    max_context_window: int | None = None
    max_prompt_tokens: int | None = None
    max_output_tokens: int | None = None
    supports_vision: bool | None = None
    #: `"enabled"` | `"disabled"` | `"unconfigured"`.
    policy_state: str | None = None
    config_schema: Mapping[str, Any] | None = None
    meta: Mapping[str, Any] | None = None

    def to_wire(self, provider: str) -> dict[str, Any]:
        wire: dict[str, Any] = {
            "id": self.id,
            "name": self.name,
            "provider": self.provider or provider,
        }
        for key, value in (
            ("maxContextWindow", self.max_context_window),
            ("maxPromptTokens", self.max_prompt_tokens),
            ("maxOutputTokens", self.max_output_tokens),
            ("supportsVision", self.supports_vision),
            ("policyState", self.policy_state),
            ("configSchema", self.config_schema),
            ("_meta", self.meta),
        ):
            if value is not None:
                wire[key] = value
        return wire


@dataclass(frozen=True)
class AgentInfo:
    """What the host publishes about this provider on the root channel.

    Mirrors the protocol's ``AgentInfo``. ``protected_resources`` is left out:
    v0.1 implements no authentication, and declaring none is fully conformant --
    every discovery field is optional.
    """

    provider: str
    display_name: str
    description: str
    #: Accepts :class:`ModelInfo` or a raw mapping. A raw mapping is passed
    #: through untouched apart from having `provider` filled in when absent --
    #: an embedder that already builds wire dicts is not forced to migrate,
    #: but is also not left publishing a model the picker cannot group.
    models: Sequence[ModelInfo | Mapping[str, Any]] = field(default_factory=tuple)
    capabilities: Mapping[str, Any] = field(default_factory=dict)
    #: `AgentInfo.protectedResources` -- upstream services the AGENT talks to
    #: that need a credential. Plain wire dicts rather than
    #: `core.auth.ProtectedResource`, because `provider/` sits below `core/` in
    #: the import layering; build them with `ProtectedResource.to_wire()`.
    #:
    #: Declaring none is fully conformant. It does not mean "no auth needed" --
    #: it means this agent does not front anything that asks for one.
    protected_resources: Sequence[Mapping[str, Any]] = field(default_factory=tuple)

    def _model_wire(self, model: ModelInfo | Mapping[str, Any]) -> dict[str, Any]:
        if isinstance(model, ModelInfo):
            return model.to_wire(self.provider)
        # A raw mapping still gets `provider`, which the spec declares
        # non-optional and which decides the picker's section header.
        return {"provider": self.provider, **dict(model)}

    def to_wire(self) -> dict[str, Any]:
        wire: dict[str, Any] = {
            "provider": self.provider,
            "displayName": self.display_name,
            "description": self.description,
            "models": [self._model_wire(m) for m in self.models],
        }
        if self.capabilities:
            wire["capabilities"] = dict(self.capabilities)
        if self.protected_resources:
            wire["protectedResources"] = [dict(r) for r in self.protected_resources]
        return wire


@dataclass(frozen=True)
class ModelSelection:
    """`ModelSelection` -- the model the USER picked for this message.

    Distinct from :class:`ModelInfo`, which is a model the host OFFERS. This is
    the answer to the picker.
    """

    id: str
    #: Model-specific values from the model's own `configSchema`. JSON
    #: primitives: mostly strings, sometimes numbers or booleans, carried
    #: through as-is.
    config: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def from_wire(cls, value: Any) -> ModelSelection | None:
        if not isinstance(value, Mapping):
            return None
        identifier = value.get("id")
        if not isinstance(identifier, str):
            return None
        config = value.get("config")
        return cls(id=identifier, config=dict(config) if isinstance(config, Mapping) else {})


@dataclass(frozen=True)
class UserMessage:
    """One user turn request, in provider terms."""

    text: str
    raw: Mapping[str, Any] = field(default_factory=dict)
    #: The model the user picked, if any. **Carried, not obeyed.** This host is
    #: a courier: it hands the selection to the provider and never decides what
    #: to do with it. An adapter that fronts several models reads this; one
    #: that fronts a single model ignores it. Absent means "the host's default
    #: applies", which is the spec's own wording.
    #:
    #: It arrived on every turn and was dropped on the floor, surviving only as
    #: an unnamed key inside `raw` -- so changing the model in the picker
    #: appeared to work, because the state round-tripped, and did nothing.
    model: ModelSelection | None = None
    #: `AgentSelection` -- the custom agent the user picked, as a URI matching
    #: an `AgentCustomization.uri`. Same courier rule.
    agent_uri: str | None = None


@dataclass(frozen=True)
class InputQuestion:
    """One question in an :class:`InputRequest`.

    ``kind`` is the protocol's own vocabulary -- ``text``, ``number``,
    ``integer``, ``boolean``, ``single-select``, ``multi-select`` -- because
    inventing a parallel one would only have to be mapped back. ``options`` is
    required by the two select kinds and ignored otherwise.
    """

    id: str
    kind: str
    message: str
    options: Sequence[Mapping[str, Any]] = ()
    extra: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class InputRequest:
    """The agent needs a human before it can continue.

    Either ``questions``, or a ``url`` for the user to review, or both.
    """

    message: str | None = None
    url: str | None = None
    questions: Sequence[InputQuestion] = ()


@dataclass(frozen=True)
class InputOutcome:
    """How the user answered. Neutral by ADR 0003 -- not a wire action.

    ``response`` is ``"accept"``, ``"decline"`` or ``"cancel"``. ``answers`` maps
    a question id to its final answer, and is empty for anything but an accept.
    """

    response: str
    answers: Mapping[str, Any] = field(default_factory=dict)

    @property
    def accepted(self) -> bool:
        return self.response == "accept"


@dataclass(frozen=True)
class ToolConfirmation:
    """The agent wants to run a tool and is asking first.

    ``invocation_message`` is what a client renders in the prompt.
    ``editable`` lets a client change ``tool_input`` before approving, in which
    case the edited input comes back on the outcome.
    """

    call_id: str
    name: str
    invocation_message: str
    display_name: str | None = None
    tool_input: Any = None
    confirmation_title: str | None = None
    editable: bool = False


@dataclass(frozen=True)
class ToolConfirmationOutcome:
    approved: bool
    #: The input the client actually approved. Identical to what was proposed
    #: unless the call was `editable` and a client changed it -- in which case
    #: running the original would execute something nobody agreed to.
    tool_input: Any = None


@dataclass(frozen=True)
class ClientToolCall:
    """A tool the *client* owns, which the host asks it to run.

    The agent gets the editor's own tools -- file reads, searches, whatever the
    client published on `activeClient.tools` -- with **no filesystem API on the
    host at all**. The client executes in its own process, under its own
    permissions, and reports the result.
    """

    call_id: str
    name: str
    client_id: str
    tool_input: Any = None
    display_name: str | None = None
    #: Shown while the client runs it. `chat/toolCallReady.invocationMessage`
    #: is required, so the host defaults one rather than omitting the frame.
    invocation_message: str | None = None


@dataclass(frozen=True)
class ToolResult:
    """What a client reported back, or why it did not.

    ``response`` carries ADR 0003's neutral vocabulary, like
    :class:`InputOutcome`: ``"accept"`` when the client ran the tool and
    ``value`` holds its raw wire result, ``"decline"`` when the owning client
    refused -- which the spec REQUIRES it to do "if it does not recognize the
    tool or cannot execute it" (`ChatToolCallDeniedAction`) -- and ``"cancel"``
    when the host gave up on the call because its owner left the session.

    It exists because a refusal used to arrive as ``ToolResult(value={})``,
    indistinguishable from a tool that ran and returned nothing: an agent told
    "the editor will not do that" reported an empty success and carried on.
    ``value`` is meaningful only when :attr:`accepted`; ``reason`` only when it
    is not.
    """

    value: Any = None
    response: str = "accept"
    reason: str | None = None

    @property
    def accepted(self) -> bool:
        return self.response == "accept"


@dataclass(frozen=True)
class ForkedFrom:
    """The session this one was forked from, and the transcript it inherited.

    A provider needs this because the host publishes the copied turns as the
    new session's history: without it the agent is asked to continue a
    conversation it has never seen, and answers the next message with no
    context while the client shows a full transcript above it. State and agent
    would silently disagree.

    ``turns`` is the host's published shape, deliberately not translated into
    provider terms -- the adapter knows what its model wants; the host does not.
    """

    session_uri: str
    turns: Sequence[Mapping[str, Any]] = ()


@dataclass(frozen=True)
class AgentSessionContext:
    session_uri: str
    chat_uri: str
    provider_id: str
    #: Plural since 0.7.0; a host without the multipleWorkingDirectories
    #: capability keeps only the first entry.
    working_directories: Sequence[str] = ()
    model: str | None = None
    config: Mapping[str, Any] = field(default_factory=dict)
    resume_state: Mapping[str, Any] | None = None
    #: Out-of-turn updates -- customizations, activity, bring-up progress. The
    #: host supplies it; a provider may hold it for the life of the session.
    publisher: SessionPublisher | None = None
    #: The client that created the session, if it published itself via
    #: `createSession.activeClient`. Only a starting point: clients come and go
    #: over the life of a session, and the authoritative list is
    #: `SessionState.activeClients`.
    active_client_id: str | None = None
    #: Tools that client offered to execute on the agent's behalf. These run in
    #: the client's process, so they are the one tool surface that needs no
    #: filesystem API on the host at all.
    client_tools: Sequence[Mapping[str, Any]] = ()
    #: Set when this session was created with `createSession.fork`. The turns
    #: it carries are already published as this session's history.
    fork: ForkedFrom | None = None


@runtime_checkable
class SessionPublisher(Protocol):
    """Out-of-turn updates: things true of the *session*, not of one turn.

    :class:`TurnSink` covers what an agent does while answering. This covers
    what changes when it is not: a plugin was installed, an MCP server came up,
    bring-up is still cloning a repository. A provider gets one on its
    :class:`AgentSessionContext` and may call it at any time, including before
    the first turn and after the last.
    """

    async def customizations_changed(
        self,
        customizations: Sequence[Mapping[str, Any]],
        server_tools: Sequence[Mapping[str, Any]] | None = None,
    ) -> None:
        """Republish the session's customization tree. Full replacement."""
        ...

    async def activity_changed(self, activity: str | None) -> None:
        """ "Human-readable description of what the session is currently doing."\""""
        ...

    async def changes_published(
        self, changeset: Any, changes: Sequence[Any], *, chat: str | None = None
    ) -> str:
        """Publish (or refresh) a changeset. Returns its channel URI.

        With *chat*, the changeset belongs to that chat (1.0.0): it is listed
        in `ChatState.changesets` and rolls up into the chat's
        `ChatSummary.changes`. Scope its contents to the chat's effective
        working directories. A refresh keeps the original scope.

        The provider is the only thing that knows what the agent changed, and
        until this existed it had no way to say so: `Host.publish_changeset`
        was public but reachable only by an embedder holding the Host, so the
        whole changeset feature was unreachable from inside a turn.

        `changeset` is a :class:`~ahp_host.core.changesets.Changeset`
        and `changes` are
        :class:`~ahp_host.core.changesets.FileChange`. Typed as `Any`
        here only because `provider/` sits below `core/` in the import
        layering -- the objects are the real ones.
        """
        ...

    async def mcp_server_changed(
        self, customization_id: str, state: Mapping[str, Any], channel: str | None = None
    ) -> None:
        """Report an MCP server's lifecycle.

        `state` is the protocol's discriminated union on `kind` --
        ``starting``, ``ready``, ``stopped``, ``authRequired``, ``error``.
        Full replacement of both runtime fields: omitting `channel` clears an
        existing one, which is what the reducer does with an absent value.
        """
        ...

    async def progress(
        self, progress: float, total: float | None = None, message: str | None = None
    ) -> None:
        """Report progress against the client's `createSession.progressToken`.

        Ephemeral and never replayed. A no-op when the client supplied no token,
        which is most of them -- so a provider can call it unconditionally.
        """
        ...

    async def title_changed(self, title: str) -> None:
        """Rename the session, for an agent whose sessions are named elsewhere."""
        ...

    async def config_changed(self, values: Mapping[str, Any]) -> None:
        """Change session config values, for an agent reconfigured elsewhere.

        The provider-side twin of a client's `session/configChanged`: the
        agent's own setting moved (a mode switched on another device), and
        clients should show what is actually in force. Merged, like a client's;
        saved, so a restart resumes with it. The provider is not called back
        with its own change.
        """
        ...

    async def background_work_set(self, work: BackgroundWork, *, chat: str | None = None) -> None:
        """Report work running in the background for a chat (1.0.0).

        Upsert by ``work.id``: call again with the same id to change the label.
        *chat* defaults to the session's default chat. The entry stays until
        :meth:`background_work_removed` -- turns ending, being cancelled or
        truncated say nothing about whether the work stopped, so the host never
        removes one on its own.
        """
        ...

    async def background_work_removed(self, work_id: str, *, chat: str | None = None) -> None:
        """The background work *work_id* finished or is no longer tracked."""
        ...

    async def canvas_set(self, canvas: Canvas, *, chat: str | None = None) -> str:
        """Expose or update a live canvas on a chat. Returns its channel URI.

        The first call for an ``instance_id`` adds an `ahp-canvas:` channel to
        the chat's `ChatState.canvases`; later calls replace its state.
        Experimental upstream ("1 - Experimental"), like the channel itself.
        """
        ...

    async def canvas_removed(self, instance_id: str, *, chat: str | None = None) -> None:
        """Withdraw a canvas: its reference leaves the chat and its channel goes."""
        ...

    async def open_terminal(
        self,
        title: str,
        *,
        chat: str | None = None,
        cwd: str | None = None,
        turn_id: str | None = None,
        tool_call_id: str | None = None,
    ) -> ProviderTerminal:
        """Open a read-only terminal channel for output the agent produces.

        For a command the agent runs: point a tool result's
        ``{"type": "terminal", "resource": terminal.resource}`` content, or a
        shell's :class:`BackgroundWork` ``terminal``, at it. The terminal is
        claimed by the session and *chat* (the default chat unless named), so
        no client can type into it; *turn_id* and *tool_call_id* narrow the
        claim to the call using it.

        Retained after :meth:`ProviderTerminal.exited` and across a host
        restart, as 1.0.0 requires of a terminal a tool result references --
        until its chat or session is disposed. Not listed in the root terminal
        catalogue: that is for interactive shells a client re-attaches to.
        """
        ...

    async def external_turn(self, text: str, run: Callable[[TurnSink], Awaitable[None]]) -> bool:
        """Start a turn on the default chat that no client asked for.

        For an agent that is also driven from somewhere else -- a message typed
        on another device -- so the conversation here does not silently skip
        the turns that happened there. The host publishes `chat/turnStarted`
        carrying `text` as the user's message, then runs `run(sink)` exactly as
        it runs `send_user_message`: a client can cancel it, and it ends as a
        client's turn does. Returns once the turn has started, not when it
        ends; ``False`` if the chat already has a turn running.
        """
        ...


@dataclass(frozen=True)
class Canvas:
    """A live canvas a chat exposes (`CanvasState`, 1.0.0, experimental).

    ``instance_id`` is stable and yours: publish again with the same one to
    update the canvas. ``url`` is its current absolute HTTP(S) source; leave it
    ``None`` while the source is unavailable -- the spec requires clearing it
    then. It is never persisted and is redacted from wire logs.
    """

    instance_id: str
    extension_id: str
    canvas_id: str
    extension_name: str | None = None
    title: str | None = None
    status: str | None = None
    url: str | None = None

    def to_wire(self) -> dict[str, Any]:
        if self.url is not None and not self.url.startswith(("https://", "http://")):
            raise ValueError("a canvas url must be an absolute HTTP(S) URL")
        wire: dict[str, Any] = {
            "instanceId": self.instance_id,
            "extensionId": self.extension_id,
            "canvasId": self.canvas_id,
        }
        for key, value in (
            ("extensionName", self.extension_name),
            ("title", self.title),
            ("status", self.status),
            ("url", self.url),
        ):
            if value is not None:
                wire[key] = value
        return wire


class ProviderTerminal(Protocol):
    """The writing end of a terminal from :meth:`SessionPublisher.open_terminal`."""

    @property
    def resource(self) -> str:
        """The terminal's channel URI."""
        ...

    async def write(self, data: str) -> None:
        """Append output. Plain text; ANSI escapes pass through to clients."""
        ...

    async def exited(self, exit_code: int | None = None) -> None:
        """The command ended. Further writes are ignored."""
        ...


@dataclass(frozen=True)
class BackgroundWork:
    """One entry of `ChatState.backgroundWork` (1.0.0): unfinished work that
    outlives the turn that started it.

    ``kind`` is ``"shell"`` or ``"subagent"``, and the set is non-exhaustive,
    so another string passes through for clients to render from the common
    fields. A shell needs ``command`` and may name the ``terminal`` carrying
    its output; a subagent needs ``chat``, the subagent's own chat. Whether a
    shell is attached to the agent's lifetime, and anything else
    provider-specific, goes in ``meta``.

    ``id`` is opaque to clients and unique within the chat across kinds --
    derive it from the runtime's own task id.
    """

    id: str
    kind: str
    label: str
    #: ISO 8601. Defaults to when the value was built.
    started_at: str = field(default_factory=now_iso)
    command: str | None = None
    terminal: str | None = None
    chat: str | None = None
    meta: Mapping[str, Any] | None = None

    def to_wire(self) -> dict[str, Any]:
        """The `BackgroundWork` union member. Raises on a missing required field.

        Raised rather than published: a shell without its command or a subagent
        without its chat is a schema violation every client would receive.
        """
        if self.kind == "shell" and self.command is None:
            raise ValueError("a shell's background work needs its command")
        if self.kind == "subagent" and self.chat is None:
            raise ValueError("a subagent's background work needs its chat")
        wire: dict[str, Any] = {
            "id": self.id,
            "kind": self.kind,
            "label": self.label,
            "startedAt": self.started_at,
        }
        for key, value in (
            ("command", self.command),
            ("terminal", self.terminal),
            ("chat", self.chat),
        ):
            if value is not None:
                wire[key] = value
        if self.meta is not None:
            wire["_meta"] = dict(self.meta)
        return wire


@dataclass(frozen=True)
class AuthChallenge:
    """An upstream service refused the agent, mid-tool-call.

    `reason` is the protocol's own vocabulary -- ``unauthorized`` when there is
    no token at all, ``insufficientScope`` when there is one and it does not
    reach far enough. `required_scopes` is authoritative for the next
    authorization request: clients "MUST NOT assume any subset/superset
    relationship" to what the resource advertises.
    """

    resource: Mapping[str, Any]
    reason: str = "unauthorized"
    required_scopes: Sequence[str] = ()
    oauth_client: Mapping[str, Any] | None = None

    def to_wire(self) -> dict[str, Any]:
        wire: dict[str, Any] = {"reason": self.reason, "resource": dict(self.resource)}
        if self.required_scopes:
            wire["requiredScopes"] = list(self.required_scopes)
        if self.oauth_client is not None:
            wire["oauthClient"] = dict(self.oauth_client)
        return wire


@runtime_checkable
class ManagesMcpServers(Protocol):
    """A provider that fronts MCP servers and can start and stop them.

    **The provider owns the runtime, not the host.** Spawning a process,
    speaking stdio or HTTP, `tools/list`, restart-on-crash -- all of that lives
    in the agent harness the host wraps, and upstream's own doctrine puts it
    there ("the agent host's job is to normalize whatever the harness
    exposes"). What the host owns is the *state*: publishing the server, its
    lifecycle, and routing a client's start/stop request to whoever can honour
    it.

    That split is why this library spawns nothing. A host-side MCP client would
    be process execution smuggled in behind a customization.
    """

    async def start_mcp_server(self, customization_id: str) -> None: ...

    async def stop_mcp_server(self, customization_id: str) -> None: ...


@runtime_checkable
class BackgroundsMcpServers(Protocol):
    """A provider that can stop holding messages back on a starting MCP server.

    A provider that waits for a server's tools before it processes the next
    message publishes ``{"kind": "starting", "blocking": True}`` through
    :meth:`SessionPublisher.mcp_server_changed` (1.0.0). A client may then
    dispatch `session/mcpServerBackgroundRequested`, and the host calls this.

    Return ``True`` once the startup no longer blocks -- the server keeps
    starting in the background and later reports `ready` as usual. Return
    ``False`` to refuse; the host then republishes ``blocking: True``, which is
    the spec's way for a host to stay authoritative over the optimistic
    reducer. A provider without this protocol is treated as refusing.

    Separate from :class:`ManagesMcpServers` so a provider written before 1.0.0
    still satisfies that protocol's runtime check.
    """

    async def background_mcp_server(self, customization_id: str) -> bool: ...


@runtime_checkable
class TransfersChats(Protocol):
    """A provider that can follow a chat moving to another session (`moveChat`).

    Implemented on the **provider**, not the session: either side's agent
    session may not be running (restored sessions resume lazily). The host asks
    before it commits a cross-session move, with every chat that moves -- the
    requested chat and the side and tool chats under it -- and the source and
    destination session URIs. Return ``False`` to refuse; the move then fails
    and nothing changes.

    A provider without this cannot have its chats moved between sessions, only
    reordered within one: a turn on a moved chat runs on the destination's
    agent, and only the provider knows whether that agent can carry the
    conversation on.
    """

    async def chats_transferred(
        self, chats: Sequence[str], source_session: str, destination_session: str
    ) -> bool: ...


@runtime_checkable
class HandlesCustomizations(Protocol):
    """An agent session that reacts to a client toggling a customization.

    The reducer already applies the client's `enablement` decisions in state,
    so a toggle is visible without this. What it cannot do is make the *agent*
    stop using a disabled skill -- only the provider can, and only if it is
    told. `enabled` is the effective value the host derives from those
    decisions: the most specific one, or `True` when there are none.
    """

    async def customization_toggled(self, customization_id: str, enabled: bool) -> None: ...


@runtime_checkable
class SteersTurns(Protocol):
    """An agent session that can take a message into a turn already running.

    A client "steers" by setting a chat's steering message
    (`chat/pendingMessageSet` with kind `steering`) while a turn is active.
    The host offers it here; returning ``True`` means the agent took it and
    will answer it within the current turn, and the host then removes it from
    the chat and notes it in the transcript. ``False`` (not mid-turn, not
    possible right now) leaves it pending, and the host runs it as the next
    turn once the chat is idle.
    """

    async def steer(self, chat_uri: str, message: UserMessage) -> bool: ...


@runtime_checkable
class ArchivesSessions(Protocol):
    """An agent session that follows a client archiving or unarchiving it.

    `session/isArchivedChanged` is a flag in state: the session stays, filed
    away. For most agents that is all it is. One whose session also lives
    somewhere else (Claude Code's on claude.ai) can file it away there too,
    and let go of what it keeps running for it; unarchiving brings it back.
    Called after the reducer has applied the change, then the session is
    saved, so `resume_state_of` can record it. Raising is logged; it does not
    undo the change.
    """

    async def archived_changed(self, is_archived: bool) -> None: ...


@runtime_checkable
class FollowsWorkingDirectories(Protocol):
    """An agent session that follows its folders changing mid-session.

    `session/workingDirectorySet`, `...Removed` and `...Replaced` are
    client-dispatchable once the agent advertises
    `multipleWorkingDirectories`, and between them they decide what the agent
    may touch. The context's `working_directories` is only the set at
    creation; without this a folder added to a running session reached state
    and every client, and never the agent. Called after the reducer has
    applied the change (and the host's capability and policy checks have
    passed), with the whole set as it now stands, then the session is saved.
    Raising is logged; it does not undo the change.
    """

    async def working_directories_changed(self, directories: Sequence[str]) -> None: ...


@runtime_checkable
class FollowsActiveClients(Protocol):
    """An agent session that follows which clients can run tools for it.

    The context's `active_client_id` and `client_tools` are only the creator,
    at creation: a restored session has neither, and a client that reconnects,
    joins later or republishes its tools ("re-dispatch with the full, updated
    entry") never reached the agent. An adapter that offered the creator's
    tools for the life of the session would ask a departed client to run them
    and fail every call.

    Called with the whole `SessionState.activeClients` list as it now stands
    (each entry a `SessionActiveClient`: `clientId`, `tools`, ...) after
    `session/activeClientSet`, `session/activeClientRemoved`, a client
    disconnecting, and a restored session getting its agent back. Raising is
    logged; it does not undo the change.
    """

    async def active_clients_changed(self, clients: Sequence[Mapping[str, Any]]) -> None: ...


@runtime_checkable
class DisposesSessions(Protocol):
    """An agent session that should know it is being deleted, not just closed.

    `aclose` runs both when a session is disposed and when the host shuts
    down, and a provider cannot tell which. That matters for an agent whose
    session also exists somewhere else (Claude Code's on claude.ai): a
    shutdown should leave it there to come back to, a deletion should end it.
    Called before `aclose` when the session is disposed (`disposeSession`, or
    `Host.close_session`), never at shutdown. Raising is logged; `aclose`
    still runs.
    """

    async def disposed(self) -> None: ...


@runtime_checkable
class SessionDirectory(Protocol):
    """A provider's own view of the host's session list.

    For an agent whose conversations are started somewhere else and should be
    listed here too (Claude Code sessions on claude.ai): it opens one when it
    appears there and closes it when it ends there. Every call is scoped to
    the provider that was given the directory.
    """

    async def open(
        self,
        uri: str,
        *,
        title: str,
        resume_state: Mapping[str, Any],
        working_directories: Sequence[str] = (),
    ) -> bool:
        """`Host.open_session` for this provider: the agent comes from
        `resume_session` with *resume_state*. ``False`` if it was already
        listed (its agent is then made sure to be running)."""
        ...

    async def close(self, uri: str) -> bool:
        """`Host.close_session`: dispose it, as a client deleting it would."""
        ...

    def uris(self) -> Sequence[str]:
        """This provider's sessions, including restored ones not yet running."""
        ...


@runtime_checkable
class OpensSessions(Protocol):
    """A provider that lists sessions of its own accord.

    Given its `SessionDirectory` once, after the host has restored what it
    saved - so `uris()` already includes sessions from the last run. The
    provider typically starts watching wherever its sessions live and returns;
    it must not block serving.
    """

    async def attach_directory(self, directory: SessionDirectory) -> None: ...


@runtime_checkable
class ReconfiguresSessions(Protocol):
    """An agent session that follows its config changing after creation.

    A client may change a property the provider marked ``sessionMutable``
    (`session/configChanged`); the host validates it, the reducer applies it to
    state, and then this is called with the properties that changed. Without
    it the state would say one thing and the agent do another, so a provider
    should only mark a property ``sessionMutable`` if it implements this.

    Raising is reported and logged; it does not undo the state change, so a
    provider should apply the safer half of a change first (e.g. tighten a
    permission gate before loosening anything).
    """

    async def config_changed(self, values: Mapping[str, Any]) -> None: ...


@runtime_checkable
class TruncatesHistory(Protocol):
    """An agent session that can forget part of its own conversation.

    The reducer drops the turns from the state every client can see, so
    edit-and-resend *looks* right without this. What it cannot do is make the
    agent forget them -- and an agent that still remembers a transcript the user
    was shown being rewound is the most dangerous of these gaps, because the
    user was told something untrue and will act on it.

    A provider that cannot truncate should not implement this. The host then
    leaves the state alone rather than pretending: see
    `guide/writing-a-provider.md`.
    """

    async def history_truncated(self, chat: str, turn_id: str | None) -> None:
        """Forget everything after *turn_id*, or everything if it is None."""
        ...


@runtime_checkable
class TurnSink(Protocol):
    """What a provider may report while a turn runs.

    Deliberately *not* AHP actions. The host owns ordering -- including the rule
    that a `chat/responsePart` must precede any `chat/delta` for it, which is
    pinned by conformance fixture 161 and is the easiest thing for an adapter
    author to get wrong.
    """

    async def text_delta(self, text: str) -> None: ...

    async def reasoning_delta(self, text: str) -> None: ...

    async def tool_call_started(
        self,
        call_id: str,
        name: str,
        tool_input: Any = None,
        *,
        display_name: str | None = None,
        intention: str | None = None,
        meta: Mapping[str, Any] | None = None,
    ) -> None:
        """Announce a call. *display_name* is what the user sees; it defaults
        to *name* because the wire field is required and a blank row is worse
        than a technical one.

        *tool_input* is held rather than published here: `chat/toolCallStart`
        declares no input field, so the host carries it on the
        `chat/toolCallReady` that ends the streaming phase, which is where the
        protocol puts the final input.

        *meta* is the protocol's `_meta`, where the well-known keys live -- in
        particular `ptyTerminal: {"input": ..., "output": ...}`, which is what
        makes a client render a shell command as a terminal rather than a row.
        """
        ...

    async def tool_call_delta(
        self,
        call_id: str,
        content: str | None = None,
        *,
        invocation_message: str | None = None,
        meta: Mapping[str, Any] | None = None,
    ) -> None:
        """Stream a call's parameters, or update the line under its name.

        Optional, and only interesting for a call that takes long enough to
        watch. Without it such a call is one static row and then everything at
        once.

        Call it whenever you like: the host publishes whichever action the
        call's current state accepts, because `chat/toolCallDelta` reaches a
        call that is still `streaming` and nothing else. *content* is
        parameters, so it only means anything before the call is ready;
        *invocation_message* is progress, and keeps working afterwards.
        """
        ...

    async def tool_call_output(
        self,
        call_id: str,
        content: Sequence[Mapping[str, Any]],
        *,
        meta: Mapping[str, Any] | None = None,
    ) -> None:
        """Show what a still-running call has produced so far.

        REPLACES the running call's content each time rather than appending, so
        pass everything so far. Optional, like `tool_call_delta`.

        A call that produces output is running, so the host moves it there for
        you if it has not moved already -- the state this content attaches to
        exists only from `running` onwards.
        """
        ...

    async def usage(
        self,
        *,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        cache_read_tokens: int | None = None,
        model: str | None = None,
        meta: Mapping[str, Any] | None = None,
    ) -> None:
        """Report the turn's token usage.

        The client's rule is "no usage, no gauge" -- it renders nothing rather
        than a zero -- so a provider that never calls this has a context gauge
        its users cannot see at all.
        """
        ...

    async def tool_call_completed(
        self,
        call_id: str,
        result: Any = None,
        *,
        success: bool = True,
        past_tense_message: str | None = None,
    ) -> None:
        """Finish a call. `success` and `pastTenseMessage` are REQUIRED by the
        protocol, so they are keyword arguments with defaults rather than
        something an adapter can forget."""
        ...

    async def turn_failed(
        self, message: str, error_type: str = "agent.turn", duration_ms: int = 0
    ) -> None:
        """End the turn in error. `errorType` is REQUIRED by the protocol.

        Omitting it rendered every failure as `Error: (undefined) <message>`.
        Any non-empty string renders -- the schema declares `errorType: string`
        with no enum -- so the default matches the reference host's dotted
        vocabulary rather than inventing one.
        """
        ...

    async def request_input(self, request: InputRequest) -> InputOutcome:
        """Ask a human, and **wait**. See ADR 0005.

        The only method here that suspends. The answer arrives on whichever
        connection the user happened to use -- not the one that started the turn
        -- so it cannot be a return value from anything the caller controls.

        Raises ``asyncio.CancelledError`` if the turn ends first, which is the
        same way ordinary turn cancellation reaches a provider. An adapter that
        already handles cancellation needs no new code for this.

        A provider that awaits input nobody is watching will block its own turn
        until it is cancelled. That is a real failure mode; it is bounded by the
        turn, and `session/inputNeeded` makes it visible while it happens.
        """
        ...

    async def confirm_tool_call(self, call: ToolConfirmation) -> ToolConfirmationOutcome:
        """Ask before running a tool, and wait. Suspends; see ADR 0005.

        **Use the returned `tool_input`, not the one you proposed.** A client
        may edit the parameters before approving when `editable` is set, and
        running the original would execute something nobody agreed to.
        """
        ...

    async def tool_call_confirmed(
        self, call_id: str, *, approved: bool, reason_message: str | None = None
    ) -> None:
        """Report that a confirmation was answered outside this host.

        For an agent that is also driven from somewhere else: it asked both
        places, the other one answered, and it cancelled its own
        `confirm_tool_call`. This withdraws the prompt from every client and
        records the answer. A no-op if a client here answered first.
        """
        ...

    async def request_authentication(self, call_id: str, challenge: AuthChallenge) -> None:
        """Pause a running tool call until a client pushes a credential. Suspends.

        The 0.6.0 step-up flow: the agent got partway through a tool call, the
        upstream service said "not with that token", and the turn stays open
        while a human goes and gets one. Same primitive as everything else that
        waits (ADR 0005), so a cancelled turn frees it.

        Returns when the token has arrived and been recorded. The provider then
        retries whatever it was doing -- the host does not retry on its behalf,
        because only the provider knows what the call was.
        """
        ...

    async def run_client_tool(self, call: ClientToolCall) -> ToolResult:
        """Have a *client* execute one of its own tools, and wait. Suspends.

        `call.client_id` must name a client in the session's `activeClients`;
        the host refuses otherwise rather than parking a request no one will
        ever answer.
        """
        ...


@runtime_checkable
class AgentSession(Protocol):
    """One live conversation with an agent runtime."""

    async def send_user_message(self, message: UserMessage, sink: TurnSink) -> None:
        """Run one turn, reporting progress through *sink*.

        Cancellation is two-level: the host cancels the asyncio task for a
        cooperative unwind, and calls :meth:`cancel` for anything the runtime
        needs told explicitly.
        """
        ...

    async def cancel(self, reason: str | None = None) -> None: ...

    async def aclose(self) -> None: ...


@dataclass(frozen=True)
class SessionDescription:
    """What a provider contributes to a session's own state, beyond its turns.

    These are session *state*, not turn events, so they do not go through the
    :class:`TurnSink` -- the host publishes them as `session/*` actions during
    bring-up and whenever the provider reports a change.

    `customizations` is the two-level tree the protocol defines: the top level
    holds only `plugin`, `directory` and `mcpServer` entries, and agents,
    skills, prompts, rules and hooks are **children** of a container.
    `server_tools` is a separate field (`SessionState.serverTools`), not a
    customization.
    """

    customizations: Sequence[Mapping[str, Any]] = ()
    server_tools: Sequence[Mapping[str, Any]] = ()


@dataclass(frozen=True)
class ConfigRequest:
    """What a client has chosen so far, while it is still setting a session up.

    Sent repeatedly as the user changes things -- pick a directory, toggle a
    property -- and each answer is the **full** current property set, not a
    delta, contextual to what has been chosen.
    """

    provider: str | None = None
    working_directory: str | None = None
    values: Mapping[str, Any] = field(default_factory=dict)
    #: Set only for a completion query: the property whose values are wanted,
    #: and what the user has typed so far.
    property: str | None = None
    query: str = ""


@dataclass(frozen=True)
class ConfigResolution:
    """The properties a session can be created with, given the current context.

    `properties` is `SessionConfigSchema.properties`. `values` is echoed back to
    the client with any server-resolved defaults applied, and is what the client
    then passes to `createSession`.
    """

    properties: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    values: Mapping[str, Any] = field(default_factory=dict)
    required: Sequence[str] = ()


@dataclass(frozen=True)
class ConfigValue:
    """One option for a property whose schema set ``enumDynamic``."""

    value: str
    label: str
    description: str | None = None


@dataclass(frozen=True)
class CompletionRequest:
    """What the user has typed, and where the cursor is.

    ``offset`` is in **UTF-16 code units**, which is the protocol's unit and not
    Python's. For anything outside the BMP -- an emoji, most CJK extension
    characters -- a Python string index is a different number, and slicing by
    the wrong one silently completes against the wrong prefix.
    :func:`text_before_cursor` does the conversion.
    """

    kind: str
    chat: str
    text: str
    offset: int

    def text_before_cursor(self) -> str:
        """The prefix the user has typed, converting the offset correctly."""
        encoded = self.text.encode("utf-16-le")
        return encoded[: max(0, self.offset) * 2].decode("utf-16-le", errors="ignore")


@dataclass(frozen=True)
class CompletionItem:
    """One suggestion. `range` is a half-open interval in the CURRENT input."""

    insert_text: str
    #: REQUIRED by the spec, not optional: "Associate the item's `attachment`
    #: with the resulting Message" is the whole point of a completion. An item
    #: without one inserts text that references nothing.
    attachment: Mapping[str, Any]
    range_start: int | None = None
    range_end: int | None = None

    def to_wire(self) -> dict[str, Any]:
        # `label` and `detail` used to be emitted here. They are NOT spec
        # fields on CompletionItem (channels-session/commands.ts:266-297) and
        # no client reads them -- the display name comes from
        # `attachment.label`, which the spec makes required on every
        # attachment (channels-chat/state.ts:679-684). Sending our own two
        # produced items that rendered blank.
        wire: dict[str, Any] = {
            "insertText": self.insert_text,
            "attachment": dict(self.attachment),
        }
        for key, value in (("rangeStart", self.range_start), ("rangeEnd", self.range_end)):
            if value is not None:
                wire[key] = value
        return wire


@runtime_checkable
class Completes(Protocol):
    """A provider that suggests attachments as the user types.

    Optional and feature-detected. A provider without it gets an empty list,
    which is what "nothing to suggest" looks like -- a refusal would make an
    empty picker indistinguishable from a broken host.
    """

    async def complete(self, request: CompletionRequest) -> Sequence[CompletionItem]: ...


@runtime_checkable
class ConfiguresSessions(Protocol):
    """A provider that a session can be configured *before* it exists.

    Optional, and feature-detected with ``isinstance`` like the other extension
    points. A provider without it gets an empty schema, which is the honest
    answer for an agent with nothing to configure -- not a refusal, because a
    client cannot tell "no configuration" from "broken host" if it gets one.
    """

    async def resolve_config(self, request: ConfigRequest) -> ConfigResolution: ...

    async def complete_config(self, request: ConfigRequest) -> Sequence[ConfigValue]:
        """Values for a property whose schema set ``enumDynamic``.

        Only reached for such a property, so a provider that declares none never
        needs to implement it meaningfully.
        """
        ...


@runtime_checkable
class DescribesSession(Protocol):
    """An agent session that contributes customizations or tools.

    Optional, and feature-detected with ``isinstance`` rather than a capability
    flag -- the same shape as :class:`ResumableAgentProvider`.
    """

    async def describe(self) -> SessionDescription: ...


@runtime_checkable
class AgentProvider(Protocol):
    """One host, one provider. A decision, not an omission.

    `RootState.agents` is plural and this host publishes a single entry. That is
    deliberate: nothing else in the protocol is keyed by agent. `createSession.provider`
    selects one, but tools, customizations, config and capabilities all hang off
    the *session*, so a multi-provider host would have to invent a per-provider
    view of each of those and then decide what a client sees when they disagree.

    Running one host process per agent costs a process and leaves every one of
    those surfaces unambiguous. See `docs/roadmap.md` section 4a; if upstream
    later keys those surfaces by provider, this is worth revisiting.
    """

    @property
    def agent(self) -> AgentInfo: ...

    async def create_session(self, context: AgentSessionContext) -> AgentSession: ...


@runtime_checkable
class ResumableAgentProvider(AgentProvider, Protocol):
    """A provider whose sessions survive a host restart.

    Split from :class:`AgentProvider` so the host can feature-detect with
    ``isinstance`` rather than inventing a capability flag. The host persists the
    opaque resume state; only the provider interprets it.
    """

    async def resume_session(self, context: AgentSessionContext) -> AgentSession: ...

    async def resume_state_of(self, session: AgentSession) -> Mapping[str, Any] | None: ...
