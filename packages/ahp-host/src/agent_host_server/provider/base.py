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

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

__all__ = [
    "AgentInfo",
    "AgentProvider",
    "AgentSession",
    "AgentSessionContext",
    "AuthChallenge",
    "ClientToolCall",
    "Completes",
    "CompletionItem",
    "CompletionRequest",
    "ConfigRequest",
    "ConfigResolution",
    "ConfigValue",
    "ConfiguresSessions",
    "DescribesSession",
    "ForkedFrom",
    "HandlesCustomizations",
    "InputOutcome",
    "InputQuestion",
    "InputRequest",
    "ManagesMcpServers",
    "ModelInfo",
    "ResumableAgentProvider",
    "SessionDescription",
    "SessionPublisher",
    "ToolConfirmation",
    "ToolConfirmationOutcome",
    "ToolResult",
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
class UserMessage:
    """One user turn request, in provider terms."""

    text: str
    raw: Mapping[str, Any] = field(default_factory=dict)


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
    """What a client reported back. ``value`` is the raw wire result."""

    value: Any = None


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
class HandlesCustomizations(Protocol):
    """An agent session that reacts to a client toggling a customization.

    The reducer already flips `enabled` in state, so a client's toggle is
    visible without this. What it cannot do is make the *agent* stop using a
    disabled skill -- only the provider can, and only if it is told.
    """

    async def customization_toggled(self, customization_id: str, enabled: bool) -> None: ...


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
    ) -> None:
        """Announce a call. *display_name* is what the user sees; it defaults
        to *name* because the wire field is required and a blank row is worse
        than a technical one."""
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

    async def turn_failed(self, message: str) -> None: ...

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
