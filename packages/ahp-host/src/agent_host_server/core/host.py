"""The host: JSON-RPC dispatch, the command set, and the agent event mapper.

Implements the v0.1 command surface -- `initialize`, `ping`, `subscribe`,
`unsubscribe`, `listSessions`, `createSession`, `dispatchAction`, `reconnect` --
and answers `MethodNotFound` for everything else. There are no silent stubs:
because the protocol has no server capability object, that error *is* how a host
declines a feature.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import contextlib
import copy
import json
import logging
import math
import time
import uuid
from collections.abc import Awaitable, Callable, Collection, Coroutine, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Final

from agent_host_protocol import errors
from agent_host_protocol.channels import ROOT_URI
from agent_host_protocol.reducers import js
from agent_host_protocol.reducers.clock import now_iso
from agent_host_protocol.transport.base import Transport
from agent_host_protocol.types import (
    AHP_ERROR_CODES,
    IS_CLIENT_DISPATCHABLE,
    JSON_RPC_ERROR_CODES,
)
from agent_host_protocol.types.protocol import SessionStatus, session_status_flags
from agent_host_protocol.versions import DEFAULT_SUPPORTED_VERSIONS, negotiate

from agent_host_server.core import policy as policy_mod
from agent_host_server.core.audit import AuditEvent, AuditSink, emit
from agent_host_server.core.auth import (
    AUTH_REQUIRED_METHOD,
    AuthRequiredReason,
    ProtectedResource,
    TokenStore,
    auth_required,
    auth_required_params,
    scopes_satisfied,
)
from agent_host_server.core.changesets import (
    Changeset,
    ContentStore,
    FileChange,
    OperationHandler,
    changes_summary,
    file_entry,
)
from agent_host_server.core.config import RootConfig, type_matches
from agent_host_server.core.connection import DEFAULT_OUTBOX_LIMIT, Connection
from agent_host_server.core.outbound import OutboundRequests
from agent_host_server.core.pending import PendingRequests, RequestOutcome
from agent_host_server.core.policy import Policy, TracksChannels
from agent_host_server.core.resources import (
    NullResourceProvider,
    ResourceInfo,
    ResourceProvider,
    WritableResourceProvider,
    is_writable,
    path_from_file_uri,
    uri_from_path,
)
from agent_host_server.core.seq import FileSequence
from agent_host_server.core.sequencer import Sequencer
from agent_host_server.core.store import InMemorySessionStore, SessionStore, StoredSession
from agent_host_server.core.terminals import (
    CLAIM_GATED_ACTIONS,
    CommandFinished,
    CommandLine,
    CommandStart,
    CwdReported,
    RefusingTerminalBackend,
    ShellIntegrationParser,
    TerminalBackend,
    TerminalProcess,
    TerminalRequest,
    TerminalSessionClaim,
    claim_from_wire,
    terminal_dispatch_rejection,
    trim_scrollback,
)
from agent_host_server.core.turn import ActionTurnSink, TurnRunner, tool_call_dispatch_rejection
from agent_host_server.core.watches import (
    DEFAULT_COALESCE_SECONDS,
    ResourceChange,
    ResourceWatcher,
    WatchRequest,
    new_watch_channel,
)
from agent_host_server.core.wirelog import WireLog
from agent_host_server.provider.base import (
    AgentProvider,
    AgentSession,
    AgentSessionContext,
    ArchivesSessions,
    Completes,
    CompletionRequest,
    ConfigRequest,
    ConfiguresSessions,
    DescribesSession,
    DisposesSessions,
    ForkedFrom,
    HandlesCustomizations,
    ManagesMcpServers,
    OpensSessions,
    ReconfiguresSessions,
    ResumableAgentProvider,
    SessionPublisher,
    SteersTurns,
    TruncatesHistory,
    TurnSink,
    UserMessage,
)

__all__ = ["Host", "HostInfo"]

_log = logging.getLogger(__name__)

#: SessionStatus.Idle -- what a freshly created session reports.
_STATUS_IDLE = SessionStatus.IDLE

#: The mutable half of `SessionSummary` that `SessionState` also carries.
#: `SessionState` "inlines (denormalizes) every SessionMetadata field directly
#: onto itself", so the catalogue summary is a straight projection of the session
#: channel's own state -- which is how the host keeps the two in sync without
#: enumerating action types. `resource`, `provider` and `createdAt` are identity
#: and never appear in a change set.
#:
#: `changes` is NOT here, and that is the point: `SessionSummary` declares it,
#: `SessionState` does not. Projecting it meant writing it into the session
#: channel's state -- an undeclared key served to every later subscriber, with no
#: action to carry it to the ones already subscribed. It is held on `_Session`
#: and injected by `_project_summary` instead.
_SUMMARY_FIELDS: Final = (
    "title",
    "status",
    "activity",
    "project",
    "workingDirectories",
    "annotations",
    "_meta",
)

#: Upper bound on one `listSessions` page. The spec lets a server "impose its
#: own upper cap"; without one, a host with a large catalogue serialises the
#: whole thing into a single response.
_MAX_PAGE = 200

#: What a session is called before anything names it. The reference host's own
#: string, so a client that special-cases it still recognises ours.
_DEFAULT_SESSION_TITLE: Final = "New Session"

#: "Currently the standardized convention is `"!"`". Advertised in `initialize`
#: and acted on in `_start_turn` -- from the same constant, so the host cannot
#: promise a shortcut it does not honour.
TERMINAL_COMMAND_PREFIX: Final = "!"

#: What a terminal is called when `CreateTerminalParams` carried no `name`.
#: `title` is REQUIRED on both `TerminalState` and `TerminalInfo`, so there is
#: no "leave it out" option -- and the fallback is one constant because the two
#: were seeded separately: the catalogue said "Terminal" while the channel's own
#: state omitted the field entirely, so one terminal had two titles, one of them
#: schema-invalid.
_DEFAULT_TERMINAL_TITLE: Final = "Terminal"

#: And what a chat is called. The DEFAULT chat used to be given the session's
#: title instead, which is a different thing: a chat tab reading "New Session"
#: is a tab labelled with the name of the thing that contains it.
_DEFAULT_CHAT_TITLE: Final = "New Chat"

#: The mutable half of a `ChatSummary`. `resource` is identity and "MUST NOT be
#: carried in `changes`"; `origin` never changes after creation.
_CHAT_SUMMARY_FIELDS: Final = (
    "title",
    "status",
    "activity",
    "modifiedAt",
    "interactivity",
    "workingDirectories",
)

#: How long a title seeded from the first message may be. The session list is a
#: narrow column; past this it is truncated by the renderer anyway, and a title
#: cut where we can see the words is better than one cut where we cannot.
_TITLE_LIMIT: Final = 60

#: `TerminalLifecycleState` of a live terminal (0.9.0).
_TERMINAL_RUNNING: Final[Mapping[str, str]] = {"status": "running"}

#: Client-dispatchable, and between them they name the filesystem roots the
#: agent gets tool access to.
_WORKING_DIRECTORY_ACTIONS: Final = frozenset(
    {
        "session/workingDirectorySet",
        "session/workingDirectoryRemoved",
        "session/workingDirectoryReplaced",
    }
)

#: Client-dispatchable, and both name a request the host is suspended on.
#: Upstream states their rejection rules in prose ("servers SHOULD reject...")
#: and the reducers enforce none of them.
_INPUT_ACTIONS: Final = frozenset({"chat/inputAnswerChanged", "chat/inputCompleted"})

#: Client-dispatchable. The reducer moves the customization's state; only the
#: provider can move the actual server, because only the provider has one.
_MCP_LIFECYCLE_ACTIONS: Final = frozenset(
    {"session/mcpServerStartRequested", "session/mcpServerStopRequested"}
)

#: Terminal actions that change a field `TerminalInfo` also carries, so the root
#: catalogue has to be republished after them. `terminal/exited` is not here
#: because the reaper republishes on its own, and nothing else in the terminal
#: channel touches `title`, `claim` or `exitCode`.
_CATALOGUE_TERMINAL_ACTIONS: Final = frozenset({"terminal/titleChanged", "terminal/claimed"})

#: Client-dispatchable, and each resolves a tool call the host is suspended on.
#: The protocol's own validation table conditions `chat/toolCallConfirmed` on
#: the call's STATUS and never on client identity -- so any subscriber may
#: approve any other client's pending SERVER-side call. That is upstream's
#: design; the host still has to check the call is actually pending, which no
#: reducer does. `chat/toolCallComplete` is the exception and says so: "The
#: server SHOULD reject this action if the dispatching client does not match the
#: contributor's `clientId`."
_TOOL_RESOLVING_ACTIONS: Final = frozenset({"chat/toolCallConfirmed", "chat/toolCallComplete"})

#: Both halves of the active-client lifecycle. `Set` expands the plugins a
#: client just published; `Removed` ends the tool calls it can no longer answer.
#: Only the first was routed, so a client that left a session politely stranded
#: every call it owned -- the exact case the disconnect path already handled.
_ACTIVE_CLIENT_ACTIONS: Final = frozenset(
    {"session/activeClientSet", "session/activeClientRemoved"}
)

#: The read half of the `resource*` family, plus the grant request. The write
#: half -- write, mkdir, copy, move, delete -- is a separate opt-in and is not
#: implemented (docs/roadmap.md section 6).
_RESOURCE_METHODS: Final = frozenset(
    {"resourceResolve", "resourceRead", "resourceList", "resourceRequest"}
)

#: The mutating half. Answered `PermissionDenied` unless the installed provider
#: is structurally a `WritableResourceProvider` -- reading discloses, writing
#: destroys, and a host must not acquire the second by installing the first.
_RESOURCE_WRITE_METHODS: Final = frozenset(
    {"resourceWrite", "resourceMkdir", "resourceDelete", "resourceMove", "resourceCopy"}
)

#: What `resourceRead` returns when the bytes are not valid UTF-8. "Binary
#: content MUST use `base64`; text content MAY use `utf-8`."
_BASE64: Final = "base64"
_UTF8: Final = "utf-8"

#: The closed `ResourceWriteMode` enum. Anything else was coerced to the
#: DEFAULT, which is the full overwrite -- the most destructive of the three
#: picked as the fallback for a value the caller got wrong.
_WRITE_MODES: Final = frozenset({"truncate", "append", "insert"})

#: The largest file `resourceRead` will answer with, before base64 and JSON
#: multiply it. There is no partial read in the protocol -- `ResourceReadParams`
#: has no offset or length -- so this is a refusal, not a truncation, and an
#: embedder serving large assets raises it. 16 MiB is roughly a 21 MiB base64
#: frame; the unbounded version turned one 64 MiB file into ~970 MB of RSS.
DEFAULT_MAX_READ_BYTES: Final = 16 * 1024 * 1024

#: One server-to-client notification per OTel signal. The payload is OTLP/JSON
#: verbatim -- "AHP only adds the routing envelope" -- so this host never parses
#: or validates it, which is also why there is no OTLP encoder here.
_OTLP_METHODS: Final = {
    "logs": "otlp/exportLogs",
    "traces": "otlp/exportTraces",
    "metrics": "otlp/exportMetrics",
}


@dataclass
class _Watch:
    request: WatchRequest
    owner: Connection
    started: bool = False
    buffered: list[ResourceChange] = field(default_factory=list)
    flush: asyncio.Task[None] | None = None


def _items(wrapped: Any) -> tuple[str, ...]:
    """Unwrap the `{items: [...]}` forward-compatibility envelope."""
    if not isinstance(wrapped, Mapping):
        return ()
    items = wrapped.get("items")
    if not isinstance(items, list):
        return ()
    return tuple(i for i in items if isinstance(i, str))


def _write_mode(params: Mapping[str, Any]) -> str:
    """`ResourceWriteParams.mode`, or `InvalidParams`.

    The enum is closed -- `truncate | append | insert` -- and this coerced
    everything else, including `7`, `null` and a typo, to the DEFAULT. The
    default is the full overwrite, so the most destructive of the three modes
    was the fallback for a value the caller demonstrably got wrong, and it
    reported success. `InvalidParams`, the same answer malformed `data` gets.

    ABSENT is the only thing that means "default": invariant 18 -- an explicit
    `null` is not a missing key, and it is not a member of the enum either.
    Membership is tested behind an `isinstance` because `{} in frozenset(...)`
    raises `TypeError`, which a peer would see as `InternalError`.
    """
    if "mode" not in params:
        return "truncate"
    mode = params["mode"]
    if not isinstance(mode, str) or mode not in _WRITE_MODES:
        raise errors.invalid_params(f"mode must be one of {sorted(_WRITE_MODES)}, not {mode!r}")
    return mode


def _write_position(params: Mapping[str, Any]) -> int:
    """`ResourceWriteParams.position`, or `InvalidParams`.

    A negative offset is meaningless in all three modes and the provider had no
    reading for one: `append` computed an offset past EOF and silently NUL-padded
    the file, while `insert` and `truncate` reached `os.ftruncate` and leaked
    `[Errno 22] Invalid argument` as `InternalError` -- "the host has a bug"
    for an unambiguous caller mistake.

    The schema types it `number`, not `integer`, so `3.0` is a valid way to say
    3 and is accepted; `3.5` is not a byte offset and is refused rather than
    silently floored. `True` is rejected before either -- it is an `int` in
    Python and JSON `true` is not an offset.
    """
    if "position" not in params:
        return 0
    position = params["position"]
    if isinstance(position, bool) or not isinstance(position, int | float):
        raise errors.invalid_params(f"position must be a number, not {position!r}")
    if isinstance(position, float) and not math.isfinite(position):
        # `json.loads` accepts `Infinity` and `NaN` by default and `int()` raises
        # on both, so without this a peer picks which frames become
        # `InternalError` with a Python exception name attached.
        raise errors.invalid_params(f"position must be finite: {position!r}")
    if position != int(position) or position < 0:
        raise errors.invalid_params(f"position must be a non-negative whole number: {position!r}")
    return int(position)


def _read_result(content: Any, requested: Any) -> dict[str, Any]:
    """`ResourceReadResult`, honouring the requested encoding where possible.

    "The server SHOULD honor the `encoding` requested... If the server cannot
    provide the requested encoding, it MUST fall back to either `base64` or
    `utf-8`." Bytes that are not valid UTF-8 cannot be sent as `utf-8` at all,
    so that is the one case where the fallback is forced.
    """
    result: dict[str, Any] = {}
    if requested != _BASE64:
        try:
            result = {"data": content.data.decode("utf-8"), "encoding": _UTF8}
        except UnicodeDecodeError:
            result = {}
    if not result:
        result = {"data": base64.b64encode(content.data).decode("ascii"), "encoding": _BASE64}
    if content.content_type is not None:
        result["contentType"] = content.content_type
    return result


@dataclass
class _Terminal:
    channel: str
    parser: ShellIntegrationParser
    process: TerminalProcess | None = None
    #: The command line an OSC 633 `E` announced, waiting for the `C` that
    #: starts the command it describes.
    pending_command: str = ""
    command_id: str | None = None
    announced: bool = False
    #: When the running command started, for `commandFinished.durationMs`.
    started_ms: int = 0
    #: Watches the child and publishes `terminal/exited`. Held so it is not
    #: garbage-collected mid-flight.
    reaper: asyncio.Task[None] | None = None
    #: False until the channel is registered and buffered output flushed. A pty
    #: backend arms its reader in the constructor, so the child's first bytes
    #: can arrive while `_create_terminal` is still awaiting registration --
    #: they are held in `early_output` (in arrival order) instead of being fed
    #: to a parser whose publishes would land on a channel that does not exist
    #: yet and vanish.
    ready: bool = False
    early_output: list[bytes] = field(default_factory=list)

    async def close(self) -> None:
        if self.process is not None:
            with contextlib.suppress(Exception):
                await self.process.kill()
        self.parser.reset()


def _terminal_info(state: Any) -> dict[str, Any]:
    """The `TerminalInfo` fields the root catalogue carries.

    `title` and `claim` are REQUIRED alongside `resource` (schema
    `TerminalInfo.required`), and we sent neither reliably -- the catalogue
    went out as `{resource, isPty}`, where `isPty` is not even a TerminalInfo
    field. A client reading a required field that is absent has nothing to
    render the row with.
    """
    if not isinstance(state, Mapping):
        # Still valid: the caller supplies `resource`, and a title beats an
        # entry the client cannot render at all.
        return {"title": _DEFAULT_TERMINAL_TITLE, "claim": {}, "lifecycle": dict(_TERMINAL_RUNNING)}
    lifecycle = state.get("lifecycle")
    # `lifecycle` is required on `TerminalInfo` since 0.9.0 and replaces the
    # old top-level `exitCode`: an exit is `{status: "exited", exitCode?}`.
    return {
        "title": state.get("title")
        if isinstance(state.get("title"), str)
        else _DEFAULT_TERMINAL_TITLE,
        "claim": state.get("claim") or {},
        "lifecycle": dict(lifecycle if isinstance(lifecycle, Mapping) else _TERMINAL_RUNNING),
    }


#: File suffix -> customization type. The explicit convention: a file that says
#: what it is in its own name.
_CHILD_SUFFIXES: Final = {
    ".prompt.md": "prompt",
    "skill.md": "skill",
    ".agent.md": "agent",
    ".instructions.md": "rule",
    ".rule.md": "rule",
    ".hook.md": "hook",
}

#: Containing directory -> customization type, and the one a real client
#: actually uses: `.github/agents/foo.md` carries its type in the directory,
#: not the filename.
#:
#: The names are **not** the obvious ones, and guessing got two of four wrong.
#: They are what VS Code's own tests read
#: (`chat/test/browser/actions/createPluginAction.test.ts`):
#:
#:   agents/    -> agent
#:   commands/  -> prompt   NOT `prompts/`
#:   rules/     -> rule     NOT `instructions/`
#:   skills/    -> skill    a DIRECTORY per skill, holding SKILL.md
#:
#: `prompts` and `instructions` are kept as aliases because they are what a
#: reader of the spec would write, and accepting both costs nothing.
_CHILD_DIRECTORIES: Final = {
    "agents": "agent",
    "commands": "prompt",
    "prompts": "prompt",
    "rules": "rule",
    "instructions": "rule",
    "skills": "skill",
    "hooks": "hook",
}


def _child_customization(
    plugin: Mapping[str, Any], uri: str, name: str, content: bytes
) -> dict[str, Any] | None:
    """One child of a client-published plugin.

    The id is namespaced under the plugin's, because ids are "session-unique"
    and two clients may publish a plugin containing the same file name.
    """
    lowered = name.lower()
    kind = next(
        (value for suffix, value in _CHILD_SUFFIXES.items() if lowered.endswith(suffix)), None
    )
    if kind is None:
        # Fall back to the containing directory. A filename that says nothing
        # is the norm, not the exception -- and a type this host still cannot
        # work out is skipped rather than guessed, because a mislabelled child
        # renders in the wrong section and the client cannot correct that.
        parts = uri.rstrip("/").split("/")
        kind = _CHILD_DIRECTORIES.get(parts[-2].lower()) if len(parts) > 1 else None
    if kind is None:
        return None
    try:
        text = content.decode()
    except UnicodeDecodeError:
        return None
    return {
        "type": kind,
        "id": f"{plugin.get('id')}/{name}",
        "uri": uri,
        "name": _title_of(text) or name,
        "enabled": True,
    }


#: `CustomizationEnablementKind`. Only `workspace` names a `uri`.
_ENABLEMENT_KINDS: Final = frozenset({"global", "workspace", "session"})


def _enablement_rejection(enablement: Any) -> str | None:
    """Why a `session/customizationToggled` decision list is unusable, if it is.

    Since 0.8.0 the action carries `enablement: CustomizationEnablement[]`
    where it used to carry `enabled: boolean`. A toggle in the old shape --
    what a client that negotiated 0.7.0 still sends -- would reduce to a no-op
    (upstream's reducer throws on it) while the client's optimistic state shows
    it applied; rejecting it tells that client to revert.
    """
    if not isinstance(enablement, list):
        return "session/customizationToggled requires an enablement array"
    for decision in enablement:
        if not isinstance(decision, Mapping):
            return "each enablement decision must be an object"
        if decision.get("kind") not in _ENABLEMENT_KINDS:
            return "enablement kind must be global, workspace or session"
        if not isinstance(decision.get("enabled"), bool):
            return "each enablement decision needs a boolean enabled"
        if decision.get("kind") == "workspace" and not isinstance(decision.get("uri"), str):
            return "a workspace enablement decision needs a uri"
    return None


def _effective_enabled(enablement: Any) -> bool:
    """``enablement?.[0]?.enabled ?? true`` -- an empty list is no decision."""
    if isinstance(enablement, list) and enablement and isinstance(enablement[0], Mapping):
        enabled = enablement[0].get("enabled")
        if isinstance(enabled, bool):
            return enabled
    return True


def _title_of(text: str) -> str | None:
    """A markdown child's display name: its front-matter `name`, or its H1.

    Read rather than invented, because the client wrote the file and the name
    in it is the one a user will recognise.
    """
    for line in text.splitlines()[:20]:
        stripped = line.strip()
        if stripped.lower().startswith("name:"):
            return stripped[5:].strip().strip("\"'") or None
        if stripped.startswith("# "):
            return stripped[2:].strip() or None
    return None


def _reducer_for_restored(uri: str, state: Mapping[str, Any], session_uri: str) -> str:
    """Which reducer a stored channel gets back.

    Chosen by SHAPE, not by URI, for the same reason the sequencer binds
    reducers at registration: session and chat URIs are client-chosen and
    opaque, so a scheme test would restore a channel with no reducer at all --
    the failure that froze state in `docs/experiments.md` E12, made permanent by
    being written to disk.
    """
    if uri == f"{session_uri}/annotations" or "annotations" in state:
        return "annotations"
    if "turns" in state:
        return "chat"
    return "session"


def _connection_identity(client_id: Any) -> str:
    """The id a connection is known by, minted when the client supplied none.

    The schema calls `clientId` a "Unique client identifier", and `""` is not
    one -- it is what an unset config value looks like on the wire. Kept
    verbatim it made every peer that sent it the *same* peer: `holds_claim`
    handed them each other's terminals, the active-client gate let one assert
    another's role, and the removal on disconnect skipped them all, leaving the
    session advertising tools nobody could execute.

    So an empty id is treated as the absence it is. A client cannot learn the
    minted value -- `InitializeResult` has no field for it, which was already
    true of the non-string case -- and that is why an `activeClient` claim on
    `""` is then refused by the gate in `_create_session` rather than quietly
    rewritten: the client asked to be an identity the host cannot give it.
    """
    return client_id if isinstance(client_id, str) and client_id else str(uuid.uuid4())


def _active_clients(active_client: Any) -> list[Any]:
    """`createSession.activeClient` as the initial `activeClients` list.

    `tools` is required on `SessionActiveClient` and a client that omits it is
    malformed, but the list is fanned out to every subscriber and a missing key
    would break their iteration -- so it is normalised, not rejected. Everything
    else on the entry survives verbatim (ADR 0001).
    """
    if not isinstance(active_client, Mapping):
        return []
    client_id = active_client.get("clientId")
    if not isinstance(client_id, str) or not client_id:
        # Without a clientId the entry is unaddressable: nothing can update it,
        # remove it, or be told to execute its tools. An empty one is no more
        # addressable than a missing one -- see `_connection_identity`.
        return []
    entry = dict(active_client)
    if not isinstance(entry.get("tools"), list):
        entry["tools"] = []
    return [entry]


def _answers_of(state: Any, request_id: str) -> Mapping[str, Any]:
    """The final answers on an input-request part, read from the reduced state.

    The part is "both the live interaction and its durable record": drafts land
    on it while the request is open and `chat/inputCompleted` overlays its own
    answers onto them. Reading it after the reducer runs is the only way to see
    the merge without re-implementing it.
    """
    if not isinstance(state, Mapping):
        return {}
    active = state.get("activeTurn")
    parts = active.get("responseParts") if isinstance(active, Mapping) else None
    for part in parts if isinstance(parts, list) else ():
        if not isinstance(part, Mapping) or part.get("kind") != "inputRequest":
            continue
        request = part.get("request")
        if not isinstance(request, Mapping) or request.get("id") != request_id:
            continue
        answers = request.get("answers")
        return answers if isinstance(answers, Mapping) else {}
    return {}


def _side_chat_selection(selection: Any) -> dict[str, Any]:
    """`SideChatSelection`, or -32602.

    Two required-field rules and one prose MUST, all on a value the host
    promises to preserve unchanged for the life of the chat: `text` is required
    and typed `string`, and "MUST be non-empty" (`state.schema.json`
    SideChatSelection). `responsePartId` is optional but typed.
    """
    if not isinstance(selection, Mapping):
        raise errors.invalid_params("source.selection must be an object")
    text = selection.get("text")
    if not isinstance(text, str) or not text:
        raise errors.invalid_params("source.selection.text must be a non-empty string")
    snapshot: dict[str, Any] = {"text": text}
    part_id = selection.get("responsePartId")
    if part_id is not None:
        if not isinstance(part_id, str):
            raise errors.invalid_params("source.selection.responsePartId must be a string")
        snapshot["responsePartId"] = part_id
    return snapshot


def _page_size(limit: Any) -> int:
    """`PaginatedParams.limit` -> a page size, or -32602.

    `limit` is schema-typed `number`, not `integer`, so the old
    `isinstance(limit, int)` gate got both halves wrong on peer-controlled
    input: `3.0` is a perfectly ordinary JSON number and was silently ignored
    (an unbounded page for a client that asked for three), while `true` is not
    a number at all and satisfied `isinstance(..., int)` -- so a JSON boolean
    was honoured as a page size of one.

    Out of range is a caller mistake and says so, rather than quietly meaning
    something else: a negative size would slice entries off the END of the page.
    An oversized one is capped instead, because the schema explicitly lets a
    server "impose its own upper cap".
    """
    if limit is None:
        return _MAX_PAGE
    if isinstance(limit, bool) or not isinstance(limit, int | float):
        raise errors.invalid_params("limit must be a number")
    if limit < 1:
        raise errors.invalid_params("limit must be at least 1")
    # Floored, not rounded: `2.7` promises no more than 2.7 entries.
    return min(int(limit), _MAX_PAGE)


def _encode_cursor(summary: Mapping[str, Any]) -> str:
    """A keyset cursor: the sort key of the last entry on the page.

    Keyset rather than an offset because the catalogue mutates between pages --
    an offset silently skips an entry when a session is disposed mid-walk. The
    encoding is opaque by contract ("clients MUST NOT parse, modify, or persist
    them"); base64url is chosen only so it survives a client that logs it.
    """
    payload = json.dumps([summary["modifiedAt"], summary["resource"]], separators=(",", ":"))
    return base64.urlsafe_b64encode(payload.encode()).decode()


def _decode_cursor(cursor: str) -> tuple[str, str]:
    try:
        modified_at, resource = json.loads(base64.urlsafe_b64decode(cursor.encode()))
        return str(modified_at), str(resource)
    except (ValueError, TypeError):
        # "An unrecognised cursor SHOULD be rejected with an `InvalidParams`
        # error" -- rather than silently restarting from the first page, which
        # would make a client's paging loop never terminate.
        raise errors.invalid_params("unrecognised pagination cursor") from None


class _Publisher:
    """The host's `SessionPublisher`: out-of-turn state, mapped to actions.

    Held by the provider for the life of the session, so every method has to
    tolerate the session having been disposed underneath it. `Sequencer.publish`
    already answers `None` for a channel that no longer exists, so this is
    naturally safe -- but `root/progress` goes through `notify`, which does not
    reduce anything, so it is guarded by the token instead.
    """

    def __init__(self, host: Host, session: _Session, progress_token: str | None) -> None:
        self._host = host
        self._session = session
        self._progress_token = progress_token

    async def customizations_changed(
        self,
        customizations: Sequence[Mapping[str, Any]],
        server_tools: Sequence[Mapping[str, Any]] | None = None,
    ) -> None:
        await self._host.sequencer.publish(
            self._session.uri,
            {"type": "session/customizationsChanged", "customizations": list(customizations)},
        )
        if server_tools is not None:
            await self._host.sequencer.publish(
                self._session.uri,
                {"type": "session/serverToolsChanged", "tools": list(server_tools)},
            )

    async def changes_published(self, changeset: Any, changes: Sequence[Any]) -> str:
        return await self._host.publish_changeset(self._session.uri, changeset, changes)

    async def activity_changed(self, activity: str | None) -> None:
        action: dict[str, Any] = {"type": "session/activityChanged"}
        if activity is not None:
            action["activity"] = activity
        await self._host.sequencer.publish(self._session.uri, action)
        await self._host._mirror_summary(self._session)

    async def mcp_server_changed(
        self, customization_id: str, state: Mapping[str, Any], channel: str | None = None
    ) -> None:
        if state.get("kind") == "authRequired":
            # `McpServerAuthRequiredState.resource` is the second of the three
            # advertisement mechanisms `authenticate` MUST honour; recorded
            # before the publish so no client can see a challenge whose
            # resource the host would still refuse a token for. The identifier
            # is the metadata's own `resource` member (RFC 9728).
            metadata = state.get("resource")
            identifier = metadata.get("resource") if isinstance(metadata, Mapping) else None
            if isinstance(identifier, str):
                self._host._advertise_resource(identifier)
        action: dict[str, Any] = {
            "type": "session/mcpServerStateChanged",
            "id": customization_id,
            "state": dict(state),
        }
        if channel is not None:
            action["channel"] = channel
        await self._host.sequencer.publish(self._session.uri, action)
        # An MCP server that needs a credential is something a human has to act
        # on, so it is surfaced at the session level like any other blocked
        # thing -- otherwise the only sign is a customization badge nobody is
        # looking at.
        if state.get("kind") == "authRequired":
            await self._host.sequencer.publish(
                self._session.uri,
                {
                    "type": "session/inputNeededSet",
                    "request": {
                        "kind": "toolAuthentication",
                        "id": f"mcp:{customization_id}",
                        "chat": self._session.chat_uri,
                        "toolCall": {"toolCallId": customization_id, "status": "auth-required"},
                    },
                },
            )
        else:
            await self._host.sequencer.publish(
                self._session.uri,
                {"type": "session/inputNeededRemoved", "id": f"mcp:{customization_id}"},
            )
        await self._host._mirror_summary(self._session)

    async def progress(
        self, progress: float, total: float | None = None, message: str | None = None
    ) -> None:
        # "Echoes the `progressToken` the client supplied on the originating
        # request" -- with no token there is nothing to correlate to, so there is
        # nothing to send. A provider may therefore call this unconditionally.
        if self._progress_token is None:
            return
        params: dict[str, Any] = {
            "channel": ROOT_URI,
            "progressToken": self._progress_token,
            "progress": progress,
        }
        if total is not None:
            params["total"] = total
        if message is not None:
            params["message"] = message
        await self._host.sequencer.notify(ROOT_URI, "root/progress", params)

    async def title_changed(self, title: str) -> None:
        await self._host.sequencer.publish(
            self._session.uri, {"type": "session/titleChanged", "title": title}
        )
        await self._host._mirror_summary(self._session)

    async def config_changed(self, values: Mapping[str, Any]) -> None:
        await self._host.sequencer.publish(
            self._session.uri, {"type": "session/configChanged", "config": dict(values)}
        )
        await self._host._persist(self._session)

    async def external_turn(self, text: str, run: Callable[[TurnSink], Awaitable[None]]) -> bool:
        host, session = self._host, self._session
        channel = session.chat_uri
        state = host.sequencer.state_of(channel)
        if not isinstance(state, Mapping) or state.get("activeTurn") is not None:
            return False
        started = {
            "type": "chat/turnStarted",
            "turnId": f"external-{uuid.uuid4()}",
            "startedAt": now_iso(),
            "message": {"text": text},
        }
        # Published, then run -- the same order as a queued message, for the
        # same reason: there is no client dispatch to have published it.
        await host.sequencer.publish(channel, started)
        await host._start_turn(session, channel, started, _ExternalTurn(run))
        return True


class _Directory:
    """`SessionDirectory`: `open_session` and `close_session`, for one provider."""

    def __init__(self, host: Host, provider_id: str) -> None:
        self._host = host
        self._provider_id = provider_id

    async def open(
        self,
        uri: str,
        *,
        title: str,
        resume_state: Mapping[str, Any],
        working_directories: Sequence[str] = (),
    ) -> bool:
        return await self._host.open_session(
            uri,
            title=title,
            resume_state=resume_state,
            working_directories=working_directories,
            provider_id=self._provider_id,
        )

    async def close(self, uri: str) -> bool:
        session = self._host._sessions.get(uri)
        if session is None or session.provider_id != self._provider_id:
            return False
        return await self._host.close_session(uri)

    def uris(self) -> Sequence[str]:
        return [
            uri
            for uri, session in self._host._sessions.items()
            if session.provider_id == self._provider_id
        ]


class _ExternalTurn:
    """An `AgentSession` whose only turn is one the provider is already running.

    `TurnRunner` hands the message to `send_user_message`; for an external turn
    there is nothing to hand it to, so the provider's callback runs instead.
    Cancelling still reaches the real agent session: `_cancel_turn` calls the
    session's own `cancel`, not this one's.
    """

    def __init__(self, run: Callable[[TurnSink], Awaitable[None]]) -> None:
        self._run = run

    async def send_user_message(self, message: UserMessage, sink: TurnSink) -> None:
        await self._run(sink)

    async def cancel(self, reason: str | None = None) -> None:
        return

    async def aclose(self) -> None:
        return


@dataclass(frozen=True)
class HostInfo:
    """`InitializeResult.serverInfo`. Informational only; never feature-detect on it."""

    name: str = "agent-host-server"
    version: str = "0.0.0"

    def to_wire(self) -> dict[str, Any]:
        return {"name": self.name, "version": self.version}


@dataclass
class _Session:
    uri: str
    #: The default chat. `SessionState.defaultChat`, and the one a session is
    #: created with.
    chat_uri: str
    provider_id: str
    title: str
    created_at: str
    agent_session: AgentSession | None = None
    #: The running turn PER CHAT. One slot for the whole session meant a second
    #: chat's turn overwrote the first's handle, so `chat/turnCancelled` on chat
    #: A cancelled whichever chat had started most recently -- with no terminal
    #: action on the victim, which was then pinned at `activeTurn` forever and
    #: rejected every later turn as "a turn is already active". Bricked, and
    #: silently. Found by driving the Python client against this host.
    turns: dict[str, asyncio.Task[None]] = field(default_factory=dict)
    #: When each of those turns started, on the host's monotonic clock. Only
    #: the turn task itself measures its own `duration`, and a turn ENDED FROM
    #: OUTSIDE -- `disposeSession`, `disposeChat` -- has to state one too:
    #: `duration` is required on every terminal chat action, and the hardcoded
    #: zero it would otherwise carry renders as an instantaneous turn.
    turn_started: dict[str, float] = field(default_factory=dict)
    #: The runner of each chat's turn in flight, for steering into it.
    runners: dict[str, TurnRunner] = field(default_factory=dict)
    meta: dict[str, Any] = field(default_factory=dict)
    #: The summary the root channel was last told about. `root/sessionSummaryChanged`
    #: carries only fields that changed, so the host has to remember what it sent.
    published_summary: dict[str, Any] = field(default_factory=dict)
    #: The same, per chat, for `session/chatUpdated`. Without it the entries in
    #: `SessionState.chats[]` were written once at `session/chatAdded` and never
    #: again, so a client rendering its chat tabs from that list showed every
    #: chat idle, unnamed and stamped with its creation time forever.
    published_chats: dict[str, dict[str, Any]] = field(default_factory=dict)
    #: Handed to the provider, and kept here so the host can publish on the
    #: session's behalf too.
    publisher: SessionPublisher | None = None
    #: Before/after bytes for this session's changesets. Per-session and dies
    #: with it: a diff cache, not a filesystem.
    content: ContentStore = field(default_factory=ContentStore)
    #: Changeset URI -> catalogue entry, for the channels this session owns.
    changesets: dict[str, Changeset] = field(default_factory=dict)
    #: Changeset URI -> the file ids currently marked reviewed. Held here
    #: because `changeset/contentChanged` replaces the file list wholesale, so
    #: a republish has to restate them or the ticks vanish.
    reviewed: dict[str, set[str]] = field(default_factory=dict)
    #: `SessionSummary.changes` -- the roll-up a session list renders. On the
    #: session object rather than in the session channel's state, because
    #: `SessionState` declares no such key: writing it there served an
    #: undeclared field to every later subscriber while the ones already
    #: subscribed never converged, there being no action that carries it.
    changes: dict[str, int] | None = None
    #: Opaque provider state a previous run persisted. Round-tripped, never
    #: interpreted: only the provider knows what it means.
    resume_state: Mapping[str, Any] | None = None
    #: `(plugin id, nonce)` pairs already expanded, so a republication with an
    #: unchanged nonce does not cost a round trip per child file.
    expanded_plugins: set[tuple[Any, Any]] = field(default_factory=set)

    def running(self, chat: str | None = None) -> list[asyncio.Task[None]]:
        """Live turn tasks -- for one chat, or for the whole session.

        Both scopes are real. A changeset commit races the agent's writes on
        ANY chat, so it asks the session; a cancel addresses exactly one.
        """
        if chat is not None:
            task = self.turns.get(chat)
            return [task] if task is not None and not task.done() else []
        return [t for t in self.turns.values() if not t.done()]

    #: Every chat this session owns, default included. A set rather than a
    #: single URI because `createChat` exists -- and because the summary rules
    #: aggregate across all of them, not just the default.
    chat_uris: set[str] = field(default_factory=set)

    @property
    def annotations_uri(self) -> str:
        """ "The channel URI is derived from the session URI by appending
        `/annotations`." One per session, always."""
        return f"{self.uri}/annotations"


def _unix_ms() -> int:
    """Milliseconds since the epoch, as the terminal actions declare."""
    return int(time.time() * 1000)


def _cwd_uri(path: str) -> str:
    """A shell-reported path as a `file:` URI, or unchanged if it is not one.

    A relative path cannot become a URI, and a shell that reports one has told
    us something we cannot convert -- passing it through unchanged is more
    honest than inventing a root to resolve it against.
    """
    if path.startswith("file:"):
        return path
    candidate = Path(path)
    if not candidate.is_absolute():
        return path
    return uri_from_path(candidate)


def _entry_id(change: FileChange) -> str:
    """The id `file_entry` will give this change. Kept in step with it."""
    if change.after is not None:
        return change.renamed_to or change.uri
    return change.uri


def _published_title(state: Any) -> str:
    """`SessionState.title` as the sequencer holds it, or the default."""
    if isinstance(state, Mapping):
        title = state.get("title")
        if isinstance(title, str) and title:
            return title
    return _DEFAULT_SESSION_TITLE


def _first_working_directory(state: Any) -> str | None:
    """The session's first working directory, as a URI, or None."""
    if not isinstance(state, Mapping):
        return None
    directories = state.get("workingDirectories")
    if not isinstance(directories, list):
        return None
    return next((d for d in directories if isinstance(d, str)), None)


def _turn_id_of(state: Any) -> str:
    """The chat's active turn id, or the empty string.

    `chat/toolCallComplete` requires a `turnId` and the reducer matches on it,
    so a synthesised completion has to name the turn the call belongs to.
    """
    if isinstance(state, Mapping):
        active = state.get("activeTurn")
        if isinstance(active, Mapping) and isinstance(active.get("id"), str):
            return str(active["id"])
    return ""


def _promotion_rank(bits: int) -> int:
    """How strongly a chat's activity bits claim the session summary. 0 = not.

    `InputNeeded` shares a bit with `InProgress` -- it is `(1 << 3) | (1 << 4)`
    -- so it is tested as a whole, never by equality, and it has to be tested
    before `InProgress` or every blocked chat reads as merely busy.
    """
    if bits & SessionStatus.INPUT_NEEDED == SessionStatus.INPUT_NEEDED:
        return 3
    if bits & SessionStatus.ERROR:
        return 2
    if bits & SessionStatus.IN_PROGRESS:
        return 1
    return 0


def _title_from(text: str) -> str | None:
    """A session title from the user's first message, or None if there is none.

    Deliberately dumb: the first line, whitespace collapsed, cut at a word
    boundary. A summary is what a model is for; this is a label, and a rough
    label beats three rows all reading "New Session".
    """
    first = next((line.strip() for line in text.splitlines() if line.strip()), "")
    collapsed = " ".join(first.split())
    if not collapsed:
        return None
    if len(collapsed) <= _TITLE_LIMIT:
        return collapsed
    # The ellipsis is a character of the title, so it comes out of the budget --
    # on BOTH branches. Cutting at the limit and then appending overshot by one,
    # and by two when the first word is longer than the limit: `rsplit` finds no
    # space, hands back the whole cut, and the length guard waves it through. A
    # pasted URL yielded 62 characters against a 60-character cap.
    body = _TITLE_LIMIT - 1
    cut = collapsed[: body + 1]
    # Break on the last space so a title never ends mid-word -- unless the
    # first word is itself longer than the limit, where there is no boundary
    # to find and a hard cut is the only option.
    spaced = cut.rsplit(" ", 1)[0] if " " in cut else ""
    return f"{spaced if len(spaced) >= _TITLE_LIMIT // 2 else collapsed[:body]}…"


def _with_origin(summary: dict[str, Any], origin: Mapping[str, Any] | None) -> dict[str, Any]:
    """`ChatSummary.origin` -- where a forked or side chat came from.

    On the SUMMARY, not only on the chat channel's own state. The client builds
    its chat list from `SessionState.chats[]` and reads `origin` from there, so
    a side chat whose origin lives only on its own channel opens correctly and
    is then indistinguishable from an ordinary chat in the UI.
    """
    if origin is not None:
        summary["origin"] = dict(origin)
    return summary


class Host:
    """An AHP host over one or more client connections.

    A :class:`~agent_host_server.core.policy.Policy` is **required**: there is no
    default and no convenience function that binds a socket. Every trust
    decision belongs to the embedding application (ADR/`policy.py`).
    """

    def __init__(
        self,
        provider: AgentProvider | Sequence[AgentProvider],
        policy: Policy,
        *,
        info: HostInfo | None = None,
        supported_versions: Sequence[str] = DEFAULT_SUPPORTED_VERSIONS,
        wire_log: Path | None = None,
        sequence_file: Path | None = None,
        root_config: RootConfig | None = None,
        resources: ResourceProvider | None = None,
        watcher: ResourceWatcher | None = None,
        max_watches_per_connection: int = 32,
        max_read_bytes: int | None = DEFAULT_MAX_READ_BYTES,
        audit: AuditSink | None = None,
        telemetry: Mapping[str, str] | None = None,
        store: SessionStore | None = None,
        terminals: TerminalBackend | None = None,
        default_directory: str | None = None,
        completion_trigger_characters: Sequence[str] | None = None,
        outbox_limit: int = DEFAULT_OUTBOX_LIMIT,
        claim_gated_actions: Collection[str] = CLAIM_GATED_ACTIONS,
    ) -> None:
        if policy is None:  # pragma: no cover - defensive; typing already forbids it
            raise ValueError("a Policy is required; there is no default")
        providers: list[AgentProvider] = (
            list(provider) if isinstance(provider, (list, tuple)) else [provider]  # type: ignore[list-item]
        )
        if not providers:
            raise ValueError("a Host needs at least one provider")
        #: Every agent this host serves, by provider id, in the order given --
        #: the order `RootState.agents` lists them in.
        self.providers: dict[str, AgentProvider] = {}
        for each in providers:
            provider_id = each.agent.provider
            if provider_id in self.providers:
                raise ValueError(f"two providers share the id {provider_id!r}")
            self.providers[provider_id] = each
        #: The default agent: the first given. It serves a `createSession` that
        #: names no provider, and any restored session whose provider this host
        #: no longer serves (an id renamed since it was stored).
        self.provider: AgentProvider = providers[0]
        self.policy = policy
        self.info = info or HostInfo()
        self.supported_versions = tuple(supported_versions)
        # Without `sequence_file` the counter restarts with the process, which
        # is right for a host whose sessions do not outlive it and wrong for one
        # whose do -- see `core/seq.py`. No path is chosen on the embedder's
        # behalf.
        self.sequencer = Sequencer(
            allocator=FileSequence(sequence_file) if sequence_file is not None else None
        )
        #: Every provider request suspended on a client. ADR 0005 -- one
        #: registry, so elicitation, tool confirmation and auth step-up cannot
        #: each grow their own lifetime and cancellation rules.
        self.pending = PendingRequests()
        #: Requests the HOST initiates. `resource*` is symmetrical -- "MAY be
        #: sent in either direction" -- and a client publishes plugin URIs that
        #: only the client can read.
        self.outbound = OutboundRequests()
        self._sessions: dict[str, _Session] = {}
        self._connections: set[Connection] = set()
        self._background: set[asyncio.Task[None]] = set()
        # No default schema, for the same reason there is no default Policy.
        # Without one `RootState.config` stays absent, the reducer's own guard
        # drops every `root/configChanged`, and the host accepts nothing -- the
        # status quo, but now on purpose rather than by accident.
        self.root_config = root_config
        # A host does not acquire a filesystem by being upgraded. The default
        # answers NotFound to everything, and installing something else is an
        # explicit act by the embedder.
        self.resources: ResourceProvider = resources or NullResourceProvider()
        # No default watcher either: a host that exposes no resources has
        # nothing to watch, and starting a poller over a directory the embedder
        # never named would be inventing access it did not grant.
        self.watcher = watcher
        self._max_watches = max_watches_per_connection
        #: The largest `resourceRead` this host will answer. Public because an
        #: embedder serving large assets has to be able to raise it -- and
        #: `None` removes the bound entirely, which is what the host did before
        #: anyone measured what a 64 MiB file costs.
        self.max_read_bytes = max_read_bytes
        self._watches: dict[str, _Watch] = {}
        #: Changeset operations the embedder made invocable. Empty by default,
        #: and nothing in this library ever adds to it.
        self._operations: dict[str, OperationHandler] = {}
        # Decisions, not conversation. Absent by default: a host that records
        # nothing is the honest default for one that cannot say where the
        # record would go.
        self.audit = audit
        # `InitializeResult.telemetry`: signal -> `ahp-otlp:` URI. Absent
        # entirely by default, which is what "a host that emits no telemetry"
        # looks like -- advertising a channel nothing ever publishes to is
        # worse than advertising none.
        self.telemetry = dict(telemetry or {})
        # `InitializeResult.defaultDirectory` -- "suggested default directory
        # for remote filesystem browsing". Without it a client has no idea
        # where this host's workspace is and browses from `/`: the measured VS
        # Code trace asks for `file:///` and `file:///.vscode/settings.json`,
        # which is the filesystem root, not anything anybody meant.
        self.default_directory = default_directory
        # Advertised only when the provider can actually answer. A trigger
        # character is a promise that typing it produces suggestions; promising
        # one a provider ignores gives the user an empty picker on every
        # keystroke, which reads as a broken host rather than as an empty
        # result. Gated again at emit time on `Completes`.
        self.completion_trigger_characters = tuple(completion_trigger_characters or ())
        # Tokens the agent needs for services IT talks to. Host-global, matching
        # the reference implementation -- `authenticate` carries no client
        # identity, so a per-connection store is not observable by a conformant
        # client. The consequence is real and gated: `Policy.may_push_token`.
        self.tokens = TokenStore()
        #: Protected-resource identifiers this host advertised OUTSIDE
        #: `AgentInfo.protectedResources` -- through a live
        #: `ToolCallAuthRequiredState.auth.resource` or an MCP server's
        #: `authRequired` state. "Servers MUST accept any `resource` value they
        #: have themselves advertised through one of these three mechanisms"
        #: (`commands.ts`, authenticate), so `_authenticate`'s gate has to know
        #: about all three, not just the static list. A set that only grows:
        #: the spec keys acceptance on having-been-advertised, not on the
        #: challenge still being open.
        self._dynamic_resources: set[str] = set()
        #: Terminal actions refused from a peer that does not hold the claim.
        #: The default gates what a document names; a multi-trust-domain host
        #: passes `STRICT_CLAIM_GATED_ACTIONS` (see `core/terminals.py`) and
        #: accepts that a viewer can no longer resize the pty to its own
        #: window. A constructor knob because the module docstring promises
        #: one, and `Policy.may_dispatch` can only refuse with the generic
        #: "rejected by policy" instead of the claim-specific reason.
        self._claim_gated: frozenset[str] = frozenset(claim_gated_actions)
        #: Client ids THIS host instance has admitted through `initialize`.
        #: `reconnect` resumes on a client-asserted id alone, so without this
        #: any id is accepted and the client never learns the host has no idea
        #: who it is. Host-scoped rather than connection-scoped: reconnecting
        #: on a new socket is the whole point.
        self._known_clients: set[str] = set()
        #: Connections dropped for not reading. Counted rather than logged only,
        #: because the symptom an operator sees is a reconnect loop.
        self._outbox_overflows = 0
        #: Frames one connection may have outstanding before the host closes it.
        #: The tradeoff is documented on `Connection.enqueue`; the limit is here
        #: because only the embedder knows how far behind a peer may reasonably
        #: fall on their network.
        self.outbox_limit = outbox_limit
        # No path chosen on the embedder's behalf, same as `sequence_file`. The
        # default keeps nothing, which is what a host whose sessions do not
        # outlive it should do.
        self.store: SessionStore = store or InMemorySessionStore()
        self._restored = False
        # Declines every terminal, with a reason. A real backend is arbitrary
        # command execution and belongs in its own distribution -- see
        # `core/terminals.py` and `docs/roadmap.md` section 6.
        self.terminals: TerminalBackend = terminals or RefusingTerminalBackend()
        self._live_terminals: dict[str, _Terminal] = {}
        # Children of a `!command`, keyed by the URI the command minted. These
        # are NOT terminals a client can name: no registered channel, no
        # subscribers, no entry in `RootState.terminals` -- so they cannot live
        # in `_live_terminals`, and for exactly that reason the shutdown sweep
        # never saw them. Cancel a `!sleep 400` turn and the child outlived the
        # turn, `aclose()`, and the host process itself.
        self._oneshot_terminals: dict[str, TerminalProcess] = {}
        self.sequencer.observer = self
        self._root_ready = False
        self.wire_log = WireLog(wire_log) if wire_log is not None else None

    # ─── lifecycle ───────────────────────────────────────────────────────

    async def restore(self) -> int:
        """Bring back sessions a previous run persisted. Returns how many.

        Called once, before serving. Every channel is re-registered under the
        name it was **stored** under, never one derived from the session URI --
        chat and annotations URIs are client-chosen and opaque (invariant 15),
        so a derived name would restore a session whose channels nobody can
        reach.

        A restored session has no live `agent_session`: the provider is asked
        to resume lazily, on the first turn, so a host with a hundred stored
        sessions does not spawn a hundred agent runtimes at startup.
        """
        if self._restored:
            return 0
        self._restored = True
        await self._ensure_root()

        count = 0
        for stored in await self.store.load_all():
            if stored.uri in self._sessions:
                continue
            if not self.policy.may_restore_session(stored.to_json()):
                _log.info("policy declined to restore %s", stored.uri)
                continue
            session = await self._restore_one(stored)
            if session is not None:
                count += 1
        if count:
            await self.sequencer.publish(
                ROOT_URI,
                {"type": "root/activeSessionsChanged", "activeSessions": len(self._sessions)},
            )
        # After restoring, so each directory already lists what was saved.
        for provider_id, provider in self.providers.items():
            if isinstance(provider, OpensSessions):
                try:
                    await provider.attach_directory(_Directory(self, provider_id))
                except Exception:
                    _log.exception("attach_directory failed for %s", provider_id)
        return count

    async def open_session(
        self,
        uri: str,
        *,
        title: str,
        resume_state: Mapping[str, Any],
        working_directories: Sequence[str] = (),
        provider_id: str | None = None,
    ) -> bool:
        """Open a session no client created, for a conversation that exists elsewhere.

        For an embedder whose agent's sessions are started outside this host --
        on another machine, in another app -- and should still be listed and
        driven here. The provider must be a `ResumableAgentProvider`: the
        session's agent comes from `resume_session` with *resume_state*, which
        is how the provider learns which conversation it is. It is persisted
        like any other session, so after a restart `restore()` brings it back
        and calling this again only makes sure its agent is running.

        Returns whether a session was created. Raises `AhpError` if the agent
        could not be started, and `ValueError` if *uri* names some other channel.
        """
        provider_id = provider_id or self.provider.agent.provider
        if provider_id not in self.providers:
            raise ValueError(f"no provider {provider_id!r} on this host")
        if not isinstance(self.providers[provider_id], ResumableAgentProvider):
            raise TypeError("open_session needs a ResumableAgentProvider")
        await self._ensure_root()
        existing = self._sessions.get(uri)
        if existing is not None:
            await self._resume_if_restored(existing)
            return False
        if self.sequencer.has_channel(uri):
            raise ValueError(f"{uri} is already a channel of another kind")

        chat_uri = f"ahp-chat:/{uuid.uuid4()}"
        created_at = now_iso()
        session = _Session(
            uri=uri,
            chat_uri=chat_uri,
            provider_id=provider_id,
            title=title,
            created_at=created_at,
            resume_state=dict(resume_state),
        )
        session.chat_uris.add(chat_uri)
        session.publisher = _Publisher(self, session, None)
        session_state: dict[str, Any] = {
            "provider": session.provider_id,
            "title": title,
            "status": _STATUS_IDLE,
            "lifecycle": "creating",
            "activeClients": [],
            "chats": [],
        }
        if working_directories:
            session_state["workingDirectories"] = list(working_directories)
        await self.sequencer.register_channel(uri, session_state, "session")
        await self.sequencer.register_channel(
            chat_uri,
            {
                "resource": chat_uri,
                "title": _DEFAULT_CHAT_TITLE,
                "status": _STATUS_IDLE,
                "modifiedAt": created_at,
                "turns": [],
            },
            "chat",
        )
        await self.sequencer.register_channel(
            session.annotations_uri, {"annotations": []}, "annotations"
        )
        self._sessions[uri] = session
        await self._resume_if_restored(session)
        if session.agent_session is None:
            await self._teardown(session)
            raise errors.AhpError(-32002, f"the agent for {uri} could not be started")
        await self._announce(session)
        return True

    async def close_session(self, uri: str) -> bool:
        """Dispose a session from the host's side, as `disposeSession` would.

        The counterpart of `open_session`, for when the conversation it mirrors
        has ended elsewhere. Returns whether there was such a session.
        """
        session = self._sessions.get(uri)
        if session is None:
            return False
        await self._teardown(session)
        return True

    def session_uris(self) -> list[str]:
        """Every session this host currently serves."""
        return list(self._sessions)

    async def _restore_one(self, stored: StoredSession) -> _Session | None:
        chat_uri: str | None = None
        for uri, state in stored.channels.items():
            reducer = _reducer_for_restored(uri, state, stored.uri)
            if reducer == "chat" and chat_uri is None:
                chat_uri = uri
        if chat_uri is None:
            _log.warning("stored session %s has no chat channel; skipped", stored.uri)
            return None

        session = _Session(
            uri=stored.uri,
            chat_uri=chat_uri,
            provider_id=stored.provider,
            title=stored.title or "Restored Session",
            created_at=stored.created_at,
            resume_state=stored.resume_state,
        )
        for uri, state in stored.channels.items():
            reducer = _reducer_for_restored(uri, state, stored.uri)
            restored_state = dict(state)
            if reducer == "chat":
                # "In-progress turns SHOULD be considered failed." The partial
                # response is KEPT -- the user should see what the agent had
                # said before the crash -- but it is moved out of `activeTurn`,
                # because a turn nothing is running is not active.
                restored_state.pop("activeTurn", None)
                session.chat_uris.add(uri)
            elif uri == stored.uri:
                await self._refresh_config_schema(stored.provider, restored_state)
            await self.sequencer.register_channel(uri, restored_state, reducer)

        session.publisher = _Publisher(self, session, None)
        self._sessions[stored.uri] = session
        await self.sequencer.notify(
            ROOT_URI,
            "root/sessionAdded",
            {"channel": ROOT_URI, "summary": self._full_summary(session)},
        )
        session.published_summary = self._project_summary(session)
        return session

    async def _refresh_config_schema(self, provider_id: str, state: dict[str, Any]) -> None:
        """Describe a restored session's config as the provider does now.

        The schema is stored with the session, so a provider that has since
        made a property `sessionMutable` (or relabelled it) would otherwise
        never be believed for sessions that existed before. Only properties
        the session already has are updated; the values are kept.
        """
        config = state.get("config")
        provider = self._provider_named(provider_id)
        if not isinstance(provider, ConfiguresSessions) or not isinstance(config, Mapping):
            return
        schema = config.get("schema")
        if not isinstance(schema, Mapping):
            return
        properties = schema.get("properties")
        values = config.get("values")
        if not isinstance(properties, Mapping):
            return
        try:
            fresh = await provider.resolve_config(
                ConfigRequest(
                    provider=provider_id,
                    values=dict(values) if isinstance(values, Mapping) else {},
                )
            )
        except Exception:
            _log.exception("could not refresh the config schema of a restored session")
            return
        updated = {key: dict(fresh.properties.get(key, prop)) for key, prop in properties.items()}
        state["config"] = {**config, "schema": {**schema, "properties": updated}}

    async def _persist(self, session: _Session) -> None:
        """Write a session's channels to the store, debounced.

        A resumable provider is asked for its resume state each time, so what
        is stored is what it would need *now* -- its own session id once the
        first turn has minted one, a mode changed mid-session.
        """
        provider = self._provider_of(session)
        if isinstance(provider, ResumableAgentProvider) and session.agent_session is not None:
            try:
                captured = await provider.resume_state_of(session.agent_session)
            except Exception:
                _log.exception("resume_state_of failed for %s", session.uri)
            else:
                if captured is not None:
                    session.resume_state = dict(captured)
        channels: dict[str, Mapping[str, Any]] = {}
        for uri in [session.uri, session.annotations_uri, *session.chat_uris]:
            state = self.sequencer.state_of(uri)
            if isinstance(state, Mapping):
                channels[uri] = dict(state)
        with contextlib.suppress(Exception):
            await self.store.save_soon(
                StoredSession(
                    uri=session.uri,
                    provider=session.provider_id,
                    created_at=session.created_at,
                    channels=channels,
                    title=session.title,
                    resume_state=session.resume_state,
                    # The embedder's, asked for at write time rather than
                    # cached: a policy that re-keys ownership between the
                    # session starting and this write should not persist a
                    # stale answer.
                    metadata=self._session_metadata(session.uri),
                )
            )

    def _provider_named(self, provider_id: Any) -> AgentProvider:
        """The provider a request names, or the default when it names none.

        A named id no agent here answers to falls back to the default too: the
        commands that take `provider` (config resolution, completions) answer
        for *some* agent rather than failing, which is what a single-agent host
        always did. `createSession`, which must not serve a session with an
        agent the client did not ask for, checks the id itself.
        """
        if isinstance(provider_id, str):
            return self.providers.get(provider_id, self.provider)
        return self.provider

    def _provider_of(self, session: _Session) -> AgentProvider:
        """The agent serving *session*: its own, or the default if gone."""
        return self.providers.get(session.provider_id, self.provider)

    def _provider_of_channel(self, channel: str) -> AgentProvider:
        """The agent serving the session that owns *channel* (session or chat)."""
        session = self._sessions.get(channel)
        if session is None:
            session = next((s for s in self._sessions.values() if channel in s.chat_uris), None)
        return self._provider_of(session) if session is not None else self.provider

    async def _ensure_root(self) -> None:
        if not self._root_ready:
            root_state: dict[str, Any] = {
                "agents": [each.agent.to_wire() for each in self.providers.values()],
                "activeSessions": 0,
            }
            if self.root_config is not None:
                root_state["config"] = self.root_config.to_wire()
            await self.sequencer.register_channel(ROOT_URI, root_state, "root")
            self._root_ready = True

    async def serve(
        self,
        transport: Transport,
        *,
        peer: str | None = None,
        headers: Mapping[str, str] | None = None,
        token: str | None = None,
    ) -> None:
        """Drive one client connection until its transport closes.

        `headers` and `token` are whatever the transport learned while admitting
        the peer, passed through untouched so `Policy.authorize_connection` has
        something to decide with. The usual shape for a non-loopback deployment
        is a reverse proxy that authenticates the person and forwards the
        resulting principal as a header on the upgrade; without this, that
        evidence exists at the handshake and is destroyed one call before the
        only place it is useful.

        The library assigns no meaning to any header. A forwarded header is
        evidence only if the socket cannot be reached except through the proxy
        that set it -- which is the embedder's problem, and saying so is better
        than a hook that quietly implies otherwise.
        """
        await self._ensure_root()
        connection = Connection(
            transport,
            peer=peer,
            headers=headers,
            token=token,
            wire_log=self.wire_log,
            outbox_limit=self.outbox_limit,
        )
        connection.start_writer()
        self._connections.add(connection)
        pending: set[asyncio.Task[None]] = set()
        try:
            while True:
                message = await transport.receive()
                if message is None:
                    return
                if self.wire_log is not None:
                    self.wire_log.record("c2s", message, connection.client_id or "?")
                # `method` FIRST, and the order is not free-form: a request
                # carries BOTH `method` and `id`, so testing `id` first routes
                # every request into the response path -- which now matters,
                # because the loop is full-duplex and responses arrive here too.
                if "method" not in message:
                    # Either a response to something this host asked for, or a
                    # frame nobody can act on. `resolve` completes the future
                    # inline: it does nothing else, and the method that was
                    # waiting resumes on its own task.
                    if not self.outbound.resolve(message, connection):
                        # There is no reply that could carry the problem, and it
                        # must not end the read loop -- invariant 17 covers
                        # responses as much as notifications.
                        _log.debug("dropping unmatched response frame")
                    continue
                if "id" in message:
                    # Requests run as tasks so a slow one cannot block the next
                    # message on this connection.
                    task = asyncio.create_task(self._handle_request(connection, message))
                    pending.add(task)
                    task.add_done_callback(pending.discard)
                    continue
                # Notifications are handled inline, preserving per-channel
                # arrival order for `dispatchAction`. A notification has no
                # response, so a fault has nowhere to go -- and letting it
                # escape would end the read loop, dropping a connection over one
                # bad frame from an untrusted peer.
                try:
                    await self._handle_notification(connection, message)
                except Exception:
                    _log.exception("notification handler failed: %s", message.get("method"))
        finally:
            # Before cancelling the tasks: a waiter that handles the error gets
            # a real `AhpError` rather than a bare `CancelledError`. Also the
            # registry holds a strong reference to the connection until this
            # runs, which is the other reason it belongs here.
            self.outbound.fail_connection(connection, "transport closed")
            for task in list(pending):
                task.cancel()
            await self.sequencer.unsubscribe_all(connection)
            await self._release_watches(connection)
            self._connections.discard(connection)
            if connection.overflowed:
                self._outbox_overflows += 1
            await self._retire_active_client(connection)
            await connection.close()

    # ─── dispatch ────────────────────────────────────────────────────────

    async def _handle_request(self, connection: Connection, message: Mapping[str, Any]) -> None:
        request_id = message["id"]
        method = message["method"]
        params = message.get("params") or {}
        try:
            result = await self._dispatch(connection, method, params)
        except errors.AhpError as exc:
            connection.enqueue({"jsonrpc": "2.0", "id": request_id, "error": exc.to_json()})
            return
        except Exception as exc:
            connection.enqueue(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": errors.internal_error(f"{type(exc).__name__}: {exc}").to_json(),
                }
            )
            return
        connection.enqueue({"jsonrpc": "2.0", "id": request_id, "result": result})

    async def _dispatch(
        self, connection: Connection, method: str, params: Mapping[str, Any]
    ) -> Any:
        if method == "initialize":
            return await self._initialize(connection, params)
        if method == "ping":
            # "the server MUST respond regardless of whether the client has
            # completed `initialize` or holds any subscriptions"
            # (docs/specification/transport.md, Keep-Alive). A ping is how a
            # client keeps an idle-timeout intermediary from closing the socket,
            # so it cannot be gated on anything.
            return None
        if method == "reconnect":
            # A valid FIRST request: it re-establishes a connection that dropped,
            # so there is no prior `initialize` on *this* transport. VS Code
            # opens with `reconnect` whenever it has a remembered serverSeq and
            # subscription set, and does not fall back to `initialize` if we
            # refuse -- it just retries, forever.
            return await self._reconnect(connection, params)
        if not connection.initialized:
            raise errors.invalid_params("initialize must be the first request")
        if method == "subscribe":
            return await self._subscribe(connection, params)
        if method == "listSessions":
            return await self._list_sessions(connection, params)
        if method == "createSession":
            return await self._create_session(connection, params)
        if method == "disposeSession":
            return await self._dispose_session(connection, params)
        if method == "fetchTurns":
            return await self._fetch_turns(connection, params)
        if method == "createTerminal":
            return await self._create_terminal(connection, params)
        if method == "disposeTerminal":
            return await self._dispose_terminal(connection, params)
        if method == "authenticate":
            return await self._authenticate(connection, params)
        if method == "completions":
            return await self._completions(connection, params)
        if method == "createChat":
            return await self._create_chat(connection, params)
        if method == "disposeChat":
            return await self._dispose_chat(connection, params)
        if method == "invokeChangesetOperation":
            return await self._invoke_changeset_operation(connection, params)
        if method == "createResourceWatch":
            return await self._create_resource_watch(connection, params)
        if method in _RESOURCE_METHODS:
            return await self._resource(connection, method, params)
        if method in _RESOURCE_WRITE_METHODS:
            return await self._resource_write(connection, method, params)
        if method == "resolveSessionConfig":
            return await self._resolve_session_config(params)
        if method == "sessionConfigCompletions":
            return await self._session_config_completions(params)
        raise errors.method_not_found(method)

    async def _handle_notification(
        self, connection: Connection, message: Mapping[str, Any]
    ) -> None:
        method = message["method"]
        params = message.get("params") or {}
        if method == "unsubscribe":
            channel = params.get("channel")
            if isinstance(channel, str):
                await self.sequencer.unsubscribe(connection, channel)
        elif method == "dispatchAction":
            await self._dispatch_action(connection, params)
        # Unknown notifications are ignored, per the additive-change guarantee.

    # ─── commands ────────────────────────────────────────────────────────

    async def _initialize(
        self, connection: Connection, params: Mapping[str, Any]
    ) -> dict[str, Any]:
        offered = params.get("protocolVersions")
        # The element check is not pedantry: `negotiate` hands every entry to
        # `re.match`, so one non-string in a peer-controlled array became
        # -32603 "TypeError: expected string or bytes-like object, got 'int'" --
        # the host blaming itself, and leaking a Python type name, for a
        # schema-invalid request. `protocolVersions` is `array of string`.
        if (
            not isinstance(offered, list)
            or not offered
            or not all(isinstance(version, str) for version in offered)
        ):
            raise errors.invalid_params("protocolVersions must be a non-empty array of strings")

        chosen = negotiate(offered, self.supported_versions)
        if chosen is None:
            # MUST refuse rather than proceed. No client verifies this for us.
            # The shared emitter writes the schema's `supportedVersions`; the
            # local workaround this replaced existed only while it did not.
            raise errors.unsupported_protocol_version(self.supported_versions)

        connection.client_id = _connection_identity(params.get("clientId"))
        connection.protocol_version = chosen

        if not self.policy.authorize_connection(connection.info):
            self._audit("connection.refused", connection, allowed=False)
            raise errors.AhpError(-32009, "Connection refused by policy")

        connection.initialized = True
        if connection.client_id:
            # Remembered so a later `reconnect` on this id can be told apart
            # from one asserting an id this host has never seen.
            self._known_clients.add(connection.client_id)
        self._audit("connection.admitted", connection, detail={"protocolVersion": chosen})

        snapshots: list[dict[str, Any]] = []
        for uri in params.get("initialSubscriptions") or []:
            if not isinstance(uri, str) or not self.policy.may_see_channel(connection.info, uri):
                continue
            snapshot = await self.sequencer.subscribe(connection, uri)
            if snapshot is not None:
                snapshots.append(snapshot)

        # `snapshots` is ALWAYS an array. MultiHostClient calls .find() on it
        # with no guard, so omitting it puts the client in an endless reconnect
        # loop rather than surfacing an error.
        result: dict[str, Any] = {
            "protocolVersion": chosen,
            "serverSeq": self.sequencer.server_seq,
            "serverInfo": self.info.to_wire(),
            "snapshots": snapshots,
        }
        if self.telemetry:
            result["telemetry"] = dict(self.telemetry)
        if self.default_directory is not None:
            result["defaultDirectory"] = self.default_directory
        if self.completion_trigger_characters and any(
            isinstance(each, Completes) for each in self.providers.values()
        ):
            # Without this the `completions` command is fully implemented and
            # never called: the client only issues it for a character the host
            # named, so an unadvertised trigger means the picker never opens.
            # Note the client caches these at content-provider registration and
            # does NOT re-read them on reconnect -- changing them needs a
            # window reload, not just a host restart.
            result["completionTriggerCharacters"] = list(self.completion_trigger_characters)
        if self._advertised_prefix():
            # "Absence means the host does not support command prefixes."
            # Advertised only behind a real backend: with the refusing default,
            # `!ls` would render as a terminal request this host then declines,
            # turning a working input into a dead end.
            result["terminalCommandPrefix"] = TERMINAL_COMMAND_PREFIX
        return result

    async def _subscribe(self, connection: Connection, params: Mapping[str, Any]) -> dict[str, Any]:
        channel = params.get("channel")
        if not isinstance(channel, str):
            raise errors.invalid_params("channel is required")
        if not self.policy.may_see_channel(connection.info, channel):
            raise errors.AhpError(-32009, f"Not permitted to observe {channel}")
        snapshot = await self.sequencer.subscribe(connection, channel)
        # A stateless or unknown channel yields `{}` -- the shape the spec gives
        # for channels that carry no snapshot.
        return {"snapshot": snapshot} if snapshot is not None else {}

    # ─── session summaries ───────────────────────────────────────────────
    #
    # The root channel's catalogue and the session channel's state are two views
    # of the same thing -- `SessionState` inlines every `SessionMetadata` field
    # -- and the spec makes keeping them in sync the host's job: "the host keeps
    # the two in sync via `root/sessionSummaryChanged`".
    #
    # So rather than enumerate the actions that mutate a summary field, the host
    # projects the session channel's state after every publish and emits the
    # difference. That is automatically right for actions we do not emit yet, and
    # it cannot drift when a new one is added.

    def _project_summary(self, session: _Session) -> dict[str, Any]:
        """The mutable half of the session's `SessionSummary`, derived from state."""
        state = self.sequencer.state_of(session.uri)
        summary: dict[str, Any] = {}
        if isinstance(state, Mapping):
            summary = {key: state[key] for key in _SUMMARY_FIELDS if key in state}
        if session.changes is not None:
            # Summary-only, and so read from the session rather than from the
            # channel: `SessionSummary` declares `changes`, `SessionState` does
            # not.
            summary["changes"] = session.changes

        # Aggregation across chats, spelled out because upstream states it as
        # producer SHOULDs rather than as a reducer, so nothing enforces it:
        #
        #   status:     activity bits from the DEFAULT chat, but PROMOTE from
        #               ANY chat that needs input, errored, or is working. The
        #               promotion is the whole point -- it is what makes a
        #               worker chat visible in a session list that only ever
        #               renders the default one.
        #   activity:   the default chat's, or the highest-ranked promotion's.
        #   modifiedAt: the max across every chat.
        #
        # Session-scoped flag bits (IsRead, IsArchived) stay with the session
        # and are never overwritten by a chat.
        session_flags = summary.get("status", _STATUS_IDLE)
        flags = session_flags if isinstance(session_flags, int) else _STATUS_IDLE
        activity_bits = 0
        promoted_from: Mapping[str, Any] | None = None
        promoted_rank = 0
        modified = session.created_at

        for chat_uri in [session.chat_uri, *sorted(session.chat_uris - {session.chat_uri})]:
            chat = self.sequencer.state_of(chat_uri)
            if not isinstance(chat, Mapping):
                continue
            status = chat.get("status")
            if isinstance(status, int):
                bits = status & SessionStatus.ACTIVITY_MASK
                if chat_uri == session.chat_uri:
                    activity_bits = bits
                rank = _promotion_rank(bits)
                if rank:
                    activity_bits |= bits
                    # By RANK, not by iteration order: the chats after the
                    # default are walked in URI order, so first-wins would hand
                    # the activity string to whichever chat happened to sort
                    # earliest. A chat waiting on a human outranks one that is
                    # merely busy.
                    if chat_uri != session.chat_uri and rank > promoted_rank:
                        promoted_from, promoted_rank = chat, rank
            when = chat.get("modifiedAt")
            if isinstance(when, str):
                modified = max(modified, when)

        # A promotion means something is happening, so `Idle` cannot also be
        # true. Without this a session whose default chat is idle and whose side
        # chat is working reports `Idle | InProgress`, and a client testing
        # either bit is right either way.
        if activity_bits & ~SessionStatus.IDLE:
            activity_bits &= ~SessionStatus.IDLE

        summary["status"] = session_status_flags(
            (flags & ~SessionStatus.ACTIVITY_MASK) | activity_bits
        )
        summary["modifiedAt"] = modified
        if promoted_from is not None:
            # "mirror the activity string ... of the chat currently driving the
            # promoted status bits when a non-default chat wins".
            borrowed = promoted_from.get("activity")
            if isinstance(borrowed, str):
                summary["activity"] = borrowed

        # `SessionSummary.annotations` lets badge UI render counts "without
        # subscribing to the channel itself", so it is derived here rather than
        # left to a producer to remember. Absent would mean "this session
        # exposes no annotations channel", which is no longer true.
        annotations = self.sequencer.state_of(session.annotations_uri)
        if isinstance(annotations, Mapping):
            entries = annotations.get("annotations")
            entries = entries if isinstance(entries, list) else []
            summary["annotations"] = {
                "resource": session.annotations_uri,
                "annotationCount": len(entries),
                "entryCount": sum(
                    len(e["entries"])
                    for e in entries
                    if isinstance(e, Mapping) and isinstance(e.get("entries"), list)
                ),
            }
        return summary

    def _full_summary(self, session: _Session) -> dict[str, Any]:
        """Identity fields plus the projection. What `listSessions` returns."""
        return {
            "resource": session.uri,
            "provider": session.provider_id,
            "createdAt": session.created_at,
            **self._project_summary(session),
        }

    async def _mirror_summary(self, session: _Session) -> None:
        """Emit `root/sessionSummaryChanged` for whatever actually changed.

        "Only fields present in `changes` have new values; omitted fields are
        unchanged on the client's cached summary." Sending nothing when nothing
        changed matters: the client caches a session list and a no-op
        notification per streamed delta would be a notification per token.

        **A field this notification has set can never be un-set.** The diff runs
        over `current` only, because there is nothing to run it over: omitting a
        key means "unchanged", `SessionSummaryChangedParams.changes` types every
        field as its own non-null type (`"activity": {"type": "string"}`,
        `notifications.schema.json`) so `null` is a schema violation, and unlike
        the chat catalogue's `session/chatAdded` the root channel has no
        documented upsert to restate an entry through. A session whose activity
        is cleared therefore keeps advertising the last tool it ran to any
        client rendering from the incremental cache, until that client
        re-fetches -- which the spec tells it to do on reconnect and never
        otherwise. `listSessions` and every fresh subscriber see the truth.

        Not worked around here. Encoding a retraction as `""`, or re-announcing
        the session with `root/sessionAdded`, would both be this host inventing
        wire semantics no other implementation agrees to. Recorded in
        `docs/research.md` §11 as a question for upstream.
        """
        # BEFORE the early return below. A chat's own title or activity can
        # change without moving anything the session summary projects, and
        # gating the chat catalogue on the session summary would drop exactly
        # those updates.
        await self._mirror_chats(session)
        current = self._project_summary(session)
        changes = {
            key: value
            for key, value in current.items()
            if key not in session.published_summary or session.published_summary[key] != value
        }
        if not changes:
            return
        session.published_summary = current
        session.title = current.get("title", session.title)
        await self._persist(session)
        await self.sequencer.notify(
            ROOT_URI,
            "root/sessionSummaryChanged",
            {"channel": ROOT_URI, "session": session.uri, "changes": changes},
        )

    async def _mirror_chats(self, session: _Session) -> None:
        """Bring `SessionState.chats[]` back in step with the chat channels.

        `ChatState` "inlines (denormalizes) every field" the catalogue entry
        carries, which means the two can disagree and only the host can stop
        them. Nothing did: the entry was written once at `session/chatAdded` and
        never touched again, so a client that renders its chat tabs from the
        catalogue -- which is what the catalogue is for -- showed every chat
        idle, unnamed, and stamped with the moment it was created.

        Partial, like the session summary it mirrors: "only fields present in
        `changes` are written; omitted fields are preserved", and `resource`
        "MUST NOT be carried in `changes`" because it is identity, not data.

        Which is why a RETRACTION cannot go through `changes` at all -- see
        `_republish_chat`.
        """
        for chat_uri in [session.chat_uri, *sorted(session.chat_uris - {session.chat_uri})]:
            state = self.sequencer.state_of(chat_uri)
            if not isinstance(state, Mapping):
                continue
            # `is not None`, not `in`: `chat/activityChanged` reduces as a plain
            # JS spread, so clearing the activity leaves the key present holding
            # `undefined` -- `None` here. A wire frame carrying it would be a
            # schema violation, so it is filtered out and reported as gone.
            current = {
                key: state[key] for key in _CHAT_SUMMARY_FIELDS if state.get(key) is not None
            }
            published = session.published_chats.get(chat_uri, {})
            changes = {k: v for k, v in current.items() if published.get(k) != v}
            retracted = [key for key in published if key not in current]
            if not changes and not retracted:
                continue
            session.published_chats[chat_uri] = current
            if retracted:
                await self._republish_chat(session, chat_uri, current)
                continue
            await self.sequencer.publish(
                session.uri,
                {"type": "session/chatUpdated", "chat": chat_uri, "changes": changes},
            )

    async def _republish_chat(
        self, session: _Session, chat_uri: str, current: Mapping[str, Any]
    ) -> None:
        """Restate one catalogue entry, because a field of it is GONE.

        `session/chatUpdated` merges -- the reference reducer is
        `{...chats[index], ...changes}` -- and every field `changes` declares is
        typed as its own non-null type (`"activity": {"type": "string"}`,
        `actions.schema.json`), so no value it can carry means "this field is
        gone". Omitting the key means "unchanged", which is the opposite. A chat
        that reported an activity and then stopped kept advertising it forever,
        in the host's OWN `SessionState`, against a chat channel that had
        already cleared it.

        The catalogue's one retraction is the upsert: "A chat was added to this
        session's catalog. Upsert semantics: if a chat with the same
        `summary.resource` already exists, the existing entry is replaced."
        Replaced *wholesale*, so the entry is rebuilt from the one already
        published rather than from the projection alone -- `origin` is on the
        catalogue entry and nowhere in `_CHAT_SUMMARY_FIELDS`, and losing it
        would make a side chat indistinguishable from an ordinary one.
        """
        catalogue = self.sequencer.state_of(session.uri)
        entries = catalogue.get("chats") if isinstance(catalogue, Mapping) else None
        entries = entries if isinstance(entries, list) else []
        entry = next(
            (
                e
                for e in entries
                if isinstance(e, Mapping) and js.strict_equal(e.get("resource"), chat_uri)
            ),
            None,
        )
        if entry is None:
            # No entry to replace. `session/chatAdded` would ADD one, which is
            # not what a retraction means, and `session/chatUpdated` no-ops on
            # an unknown chat -- so there is nothing to say.
            return
        summary = {
            **{k: v for k, v in entry.items() if k not in _CHAT_SUMMARY_FIELDS},
            "resource": chat_uri,
            **current,
        }
        await self.sequencer.publish(session.uri, {"type": "session/chatAdded", "summary": summary})

    def _session_for(self, channel: str) -> _Session | None:
        """The session owning a session, chat or annotations channel.

        A reverse lookup over the sessions the host minted, not a parse of the
        URI (invariant 15) -- `<session>/annotations` happens to be derivable,
        but session and chat URIs are client-chosen and opaque.
        """
        session = self._sessions.get(channel)
        if session is not None:
            return session
        return next(
            (
                s
                for s in self._sessions.values()
                if channel in s.chat_uris or channel == s.annotations_uri
            ),
            None,
        )

    async def _list_sessions(
        self, connection: Connection, params: Mapping[str, Any]
    ) -> dict[str, Any]:
        # "The server SHOULD return most-recently-modified entries first, so the
        # first page is the immediately useful one." The URI breaks ties into a
        # total order, without which a cursor could skip or repeat an entry.
        visible = sorted(
            (
                self._full_summary(session)
                for session in self._sessions.values()
                if self.policy.may_see_channel(connection.info, session.uri)
            ),
            key=lambda summary: (summary["modifiedAt"], summary["resource"]),
            reverse=True,
        )

        cursor = params.get("cursor")
        if cursor is not None:
            if not isinstance(cursor, str):
                raise errors.invalid_params("cursor must be a string")
            after = _decode_cursor(cursor)
            visible = [s for s in visible if (s["modifiedAt"], s["resource"]) < after]

        size = _page_size(params.get("limit"))
        page, rest = visible[:size], visible[size:]

        # A *successful* listSessions MUST carry `items`: the client's
        # `for...of summaries.items` sits outside its try/catch, so a malformed
        # success is fatal where an error response would have been tolerated.
        result: dict[str, Any] = {"items": page}
        if rest:
            # "A missing `nextCursor` signals the end of the collection" -- so it
            # is set only when there is genuinely another page, never eagerly.
            result["nextCursor"] = _encode_cursor(page[-1])
        return result

    async def _fetch_turns(
        self, connection: Connection, params: Mapping[str, Any]
    ) -> dict[str, Any]:
        """Load older turns into a chat. There are never any: state is in memory.

        This host keeps every turn of a chat in that chat's state and therefore
        never sets `ChatState.turnsNextCursor`, so there is no older page to
        page in. It is still implemented rather than refused, because the
        alternative reading -- `MethodNotFound` -- is wrong twice over: the
        command *is* supported, and a client cannot distinguish "this host does
        not page" from "this host is broken".

        Two MUSTs are honoured literally:

        * "the host MUST dispatch `chat/turnsLoaded` ... before responding" is
          unconditional, so an empty load still dispatches. With no turns and no
          cursor the reducer's result is the identity, which is the honest
          answer: nothing older exists.
        * "The host MUST reject unrecognised cursors with `InvalidParams`." We
          have never issued one, so every cursor is unrecognised.
        """
        channel = params.get("channel")
        if not isinstance(channel, str):
            raise errors.invalid_params("channel is required")
        if not self.policy.may_see_channel(connection.info, channel):
            raise errors.AhpError(-32009, f"Not permitted to observe {channel}")
        if self.sequencer.reducer_of(channel) != "chat":
            raise errors.invalid_params(f"{channel} is not a chat channel")
        if params.get("cursor") is not None:
            raise errors.invalid_params("unrecognised turns cursor")

        await self.sequencer.publish(channel, {"type": "chat/turnsLoaded", "turns": []})
        return {}

    # ─── resources ───────────────────────────────────────────────────────

    async def _resource(
        self, connection: Connection, method: str, params: Mapping[str, Any]
    ) -> dict[str, Any]:
        """The read half of the `resource*` family.

        Resolution happens **before** the policy is asked, so the policy sees the
        canonical URI rather than the name the peer used -- a peer that reaches a
        file through a symlink must not get a different answer from one that
        names it directly.
        """
        uri = params.get("uri")
        if not isinstance(uri, str):
            raise errors.invalid_params("uri is required")

        if method == "resourceRequest":
            return self._resource_request(connection, params, uri)

        # Host-owned content is answered BEFORE the provider is consulted, so a
        # changeset renders on a host that exposes no filesystem at all. VS
        # Code intercepts its own `git-blob:` scheme the same way.
        owner = self._content_owner(uri)
        if owner is not None:
            if not self.policy.may_see_channel(connection.info, owner.uri):
                raise errors.AhpError(-32009, f"Not permitted to read {uri}")
            if method == "resourceResolve":
                # A client STATS BEFORE IT READS. VS Code's filesystem provider
                # calls `stat()` first, and refusing that meant the diff editor
                # gave up before `resourceRead` was ever tried: "Unable to
                # resolve nonexistent file ...?_ah=..." about content this host
                # was holding and would happily have served.
                content = owner.content.get(uri)
                return ResourceInfo(
                    uri=uri,
                    type="file",
                    size=len(content.data),
                    content_type=content.content_type,
                ).to_wire()
            if method != "resourceRead":
                # `resourceList` on content, which is a file and not a
                # directory. Same answer the filesystem provider gives.
                raise errors.invalid_params(f"{uri} is not a directory")
            return _read_result(owner.content.get(uri), params.get("encoding"))

        operation = {"resourceResolve": "resolve", "resourceRead": "read"}.get(method, "list")
        # `followSymlinks` is declared on `ResourceResolveParams` and on nothing
        # else. Honouring it on read/list let a peer hand the policy the LINK's
        # URI -- `resolve(follow_symlinks=False)` answers with the name it was
        # given -- while `read` went on following the link to its target, so a
        # policy that refuses the target could be walked around by naming a link
        # to it.
        follow = params.get("followSymlinks") if method == "resourceResolve" else None
        info = await self.resources.resolve(uri, follow_symlinks=follow is not False)
        if not self.policy.may_access_resource(connection.info, operation, info.uri):
            raise errors.AhpError(-32009, f"Not permitted to {operation} {uri}")

        if method == "resourceResolve":
            wire = info.to_wire()
            if follow is False:
                # "Canonical URI after symlink resolution. Equal to the requested
                # URI when `followSymlinks` is `false`" (`ResourceResolveResult`
                # `.uri`). The walk canonicalises a symlinked PARENT even when
                # the final component is not a link, so the echo happens here --
                # AFTER the policy has seen the canonical path, never instead of
                # it.
                wire["uri"] = uri
            return wire
        if method == "resourceList":
            return {"entries": [e.to_wire() for e in await self.resources.list_dir(info.uri)]}

        self._within_read_budget(info.size, uri)
        content = await self.resources.read(info.uri)
        # Re-checked against what actually came back: `size` is advisory (a
        # provider may omit it, and a file can grow between the stat and the
        # read), and this is still ahead of the base64/JSON amplification that
        # turned a 64 MiB file into ~970 MB of resident host memory.
        self._within_read_budget(len(content.data), uri)
        return _read_result(content, params.get("encoding"))

    def _within_read_budget(self, size: int | None, uri: str) -> None:
        """Refuse a read this host will not survive.

        `ResourceReadParams` carries `channel`, `uri` and `encoding` and nothing
        else -- **there is no offset or length in the protocol**, so there is no
        partial read to fall back to and a file above the bound cannot be served
        at all. One unprivileged read of a 64 MiB file drove host RSS from 29 MB
        to 970 MB, which makes any served directory holding a video or a disk
        image an OOM lever for a peer that has only completed `initialize`.

        `PermissionDenied` because the spec has no size code (`AhpErrorCode` is
        -32001..-32011) and this is a refusal, not a malformed request or a
        missing file. The bound is `Host(max_read_bytes=...)`: an embedder
        serving large assets raises it, and `None` removes it.
        """
        if self.max_read_bytes is None or size is None or size <= self.max_read_bytes:
            return
        raise errors.AhpError(
            -32009,
            f"{uri} is {size} bytes, above this host's {self.max_read_bytes}-byte read limit",
        )

    def _resource_request(
        self, connection: Connection, params: Mapping[str, Any], uri: str
    ) -> dict[str, Any]:
        """Answer a request for access, honestly.

        This exists so a client stops retrying: without it a denied read becomes
        a deny/retry/deny loop, because the client has no way to ask whether
        asking again would help. The host cannot prompt anybody -- it has no UI --
        so the answer is whatever the standing policy already says, and a refusal
        is a refusal rather than "ask again later".
        """
        wanted = [
            operation
            for operation, requested in (
                ("read", params.get("read")),
                ("write", params.get("write")),
            )
            if requested
        ] or ["read"]
        for operation in wanted:
            if (
                operation == "write" and not is_writable(self.resources)
            ) or not self.policy.may_access_resource(connection.info, operation, uri):
                raise errors.AhpError(-32009, f"Not permitted to {operation} {uri}")
            # The JAIL, as well as the policy. This asked the policy alone, and
            # the default policy permits everything -- so the host granted
            # access to paths the resource provider then refused, one command
            # later. "After a successful `resourceRequest`, the caller MAY use
            # the corresponding `resource*` commands": a grant that does not
            # survive the next call is worse than a refusal, because the client
            # was told asking again would help.
            if not self._inside_the_jail(uri):
                raise errors.AhpError(-32009, f"Not permitted to {operation} {uri}")
        return {}

    async def _resource_write(
        self, connection: Connection, method: str, params: Mapping[str, Any]
    ) -> dict[str, Any]:
        """The mutating half of the `resource*` family.

        Unlike the read half, the policy is asked **before** the provider is
        touched, using the requested URI: there is nothing to canonicalise
        against for a file that does not exist yet, and asking afterwards would
        mean creating it first.
        """
        provider = self.resources
        if not isinstance(provider, WritableResourceProvider) or not is_writable(provider):
            raise errors.AhpError(-32009, "This host does not permit writes")

        pair = method in ("resourceMove", "resourceCopy")
        uri = params.get("source") if pair else params.get("uri")
        destination = params.get("destination")
        if not isinstance(uri, str) or (pair and not isinstance(destination, str)):
            raise errors.invalid_params("uri is required")
        for target in (uri, destination) if pair else (uri,):
            if not self.policy.may_access_resource(connection.info, "write", str(target)):
                raise errors.AhpError(-32009, f"Not permitted to write {target}")

        if method == "resourceWrite":
            encoding = params.get("encoding")
            raw = params.get("data")
            if not isinstance(raw, str):
                raise errors.invalid_params("data is required")
            try:
                data = base64.b64decode(raw, validate=True) if encoding == _BASE64 else raw.encode()
            except (ValueError, binascii.Error) as exc:
                raise errors.invalid_params("data is not valid base64") from exc
            if_match = params.get("ifMatch")
            await provider.write(
                uri,
                data,
                mode=_write_mode(params),
                position=_write_position(params),
                create_only=bool(params.get("createOnly")),
                if_match=if_match if isinstance(if_match, str) else None,
            )
        elif method == "resourceMkdir":
            await provider.mkdir(uri)
        elif method == "resourceDelete":
            await provider.delete(uri, recursive=bool(params.get("recursive")))
        elif method == "resourceMove":
            await provider.move(
                uri, str(destination), fail_if_exists=bool(params.get("failIfExists"))
            )
        else:
            await provider.copy(
                uri, str(destination), fail_if_exists=bool(params.get("failIfExists"))
            )
        return {}

    # ─── the reverse direction ───────────────────────────────────────────

    async def read_client_resource(self, connection: Connection, uri: str) -> bytes:
        """Read a URI only the CLIENT can resolve.

        `resource*` is symmetrical -- "MAY be sent in either direction" -- and
        this is the direction that exists so a host can fetch client-published
        content: a `virtual://my-client/...` plugin lives in the client's
        memory and no filesystem here will ever find it.
        """
        result = await self.outbound.call(
            connection,
            "resourceRead",
            {"channel": ROOT_URI, "uri": uri},
            send=connection.enqueue,
        )
        data = result.get("data") if isinstance(result, Mapping) else None
        if not isinstance(data, str):
            raise errors.internal_error(f"client returned no content for {uri}")
        if (result or {}).get("encoding") == _BASE64:
            try:
                return base64.b64decode(data, validate=True)
            except (ValueError, binascii.Error) as exc:
                raise errors.internal_error(f"client returned invalid base64 for {uri}") from exc
        return data.encode()

    async def list_client_resource(self, connection: Connection, uri: str) -> list[Any]:
        result = await self.outbound.call(
            connection,
            "resourceList",
            {"channel": ROOT_URI, "uri": uri},
            send=connection.enqueue,
        )
        entries = result.get("entries") if isinstance(result, Mapping) else None
        return entries if isinstance(entries, list) else []

    async def expand_client_plugin(
        self, connection: Connection, session_uri: str, plugin: Mapping[str, Any]
    ) -> None:
        """Parse a client-published plugin and surface it with its children.

        This is why the reverse direction exists. A client "MAY synthesize a
        virtual plugin in memory and rely on the host to expand it into concrete
        children" -- and until the host does, the plugin renders as a container
        with nothing in it, which is exactly what a client sees today from a
        host that ignores `activeClient.customizations`.

        Everything the client sent survives verbatim. ADR 0001's decisive
        requirement is that the host is authoritative for state it replays to
        clients NEWER than itself, so a parser that rebuilt the entry through
        closed models would silently drop fields it does not know about -- and
        the client that published them would get them back missing.
        """
        session = self._sessions.get(session_uri)
        if session is None:
            return
        expanded = dict(plugin)
        expanded["children"] = await self._plugin_children(connection, plugin)
        # `customizationUpdated` upserts one container by id and replaces it
        # entirely, children included -- there is no field-level merge and no
        # per-child action, so the whole container goes every time.
        await self.sequencer.publish(
            session_uri,
            {"type": "session/customizationUpdated", "customization": expanded},
        )
        await self._mirror_summary(session)

    async def _plugin_children(
        self, connection: Connection, plugin: Mapping[str, Any]
    ) -> list[Any]:
        """The children of a client-published plugin, read from the client.

        A failure to read one child drops that child, not the plugin: a
        half-expanded plugin is more use than none, and the alternative is that
        one unreadable file hides every skill beside it.
        """
        uri = plugin.get("uri")
        if not isinstance(uri, str):
            return list(plugin.get("children") or [])

        try:
            entries = await self.list_client_resource(connection, uri)
        except errors.AhpError:
            # A plugin whose `uri` is a FILE, not a container. The spec says
            # clients publish "always container-shaped plugins", but the only
            # third-party client in the wild publishes one plugin per agent
            # file and answers ENOTDIR to the list -- so a host that only
            # handles containers renders every one of them empty.
            #
            # Read as a single child instead. That is what the publication
            # plainly means, and being strict here would only make the feature
            # not work.
            return self._single_file_children(plugin, uri)

        children: list[Any] = []
        for entry in entries:
            if not isinstance(entry, Mapping):
                continue
            name = entry.get("name")
            if not isinstance(name, str):
                continue
            if entry.get("type") == "directory":
                # A skill is a directory holding `SKILL.md`, not a file. Only
                # that one shape recurses; a general walk would follow a plugin
                # into whatever it happened to contain.
                nested = await self._skill_in_directory(connection, plugin, uri, name)
                if nested is not None:
                    children.append(nested)
                continue
            child_uri = f"{uri.rstrip('/')}/{name}"
            try:
                content = await self.read_client_resource(connection, child_uri)
            except Exception:
                _log.debug("could not read client plugin child %s", child_uri)
                continue
            child = _child_customization(plugin, child_uri, name, content)
            if child is not None:
                children.append(child)
        return children

    async def _skill_in_directory(
        self, connection: Connection, plugin: Mapping[str, Any], uri: str, name: str
    ) -> dict[str, Any] | None:
        """`skills/<name>/SKILL.md` -- the one child shape that is a directory."""
        skill_uri = f"{uri.rstrip('/')}/{name}/SKILL.md"
        try:
            content = await self.read_client_resource(connection, skill_uri)
        except Exception:
            return None
        child = _child_customization(plugin, skill_uri, "SKILL.md", content)
        if child is not None and not _title_of(content.decode(errors="replace")):
            # No front-matter name: the directory names the skill.
            child["name"] = name
        return child

    def _single_file_children(self, plugin: Mapping[str, Any], uri: str) -> list[Any]:
        """A file-shaped plugin's one child: the file, named for the plugin.

        The name comes from the plugin rather than the filename, because a
        client that published a file as a plugin already told us what to call
        it -- and it is the string a user will recognise.
        """
        name = uri.rsplit("/", 1)[-1]
        child = _child_customization(plugin, uri, name, b"")
        if child is None:
            return []
        published = plugin.get("name")
        if isinstance(published, str) and published:
            child["name"] = published
        return [child]

    # ─── terminals ───────────────────────────────────────────────────────

    async def _create_terminal(self, connection: Connection, params: Mapping[str, Any]) -> None:
        """Open a terminal, if a backend was installed and the policy allows it.

        The default backend declines with `PermissionDenied`, not
        `MethodNotFound` -- once the method is registered it exists, and -32601
        for a request the host parsed and rejected would tell a client to stop
        asking for terminals entirely rather than that this one was refused.

        Returns ``None``: `CommandMap` declares `result: null` for every
        create/dispose lifecycle command, and the reference host serializes
        `result ?? null` -- `{}` failed a strictly-validating peer on every
        terminal it opened, while `createSession` on the same host correctly
        answered `null`.
        """
        channel = params.get("channel")
        if not isinstance(channel, str):
            raise errors.invalid_params("channel is required")
        if self.sequencer.has_channel(channel):
            # `AlreadyExists` (-32010), not `SessionAlreadyExists` (-32003).
            # -32003 is defined as "a session with the given URI already
            # exists", and the shared helper's message says "Session" -- so a
            # client re-creating a terminal it forgot about was told a session
            # collided, on a URI that names no session. -32010 is the general
            # "the target resource already exists and the operation does not
            # allow overwriting", which is exactly this.
            raise errors.AhpError(
                AHP_ERROR_CODES["AlreadyExists"], f"Channel already exists: {channel}"
            )
        claim = claim_from_wire(params.get("claim"))
        if claim is None:
            raise errors.invalid_params("a valid claim is required")
        verdict = self.policy.may_create_terminal(connection.info, params)
        if not verdict:
            self._audit("terminal.refused", connection, channel=channel, allowed=False)
            # A `Denied("...")` from the policy replaces this message. Worth doing
            # here above anywhere else: a client renders a refused terminal as
            # "the terminal process failed to launch", so the default reads as a
            # crash rather than as a host that does not offer terminals.
            raise errors.AhpError(
                -32009, policy_mod.reason_or(verdict, "Not permitted to create a terminal")
            )

        cols, rows = params.get("cols"), params.get("rows")
        # `cwd` is passed through as the CLIENT asked for it, and it is the
        # backend's job to refuse one it should not honour -- the host has no
        # filesystem opinion here, and inventing one would be a second, weaker
        # jail beside the resource provider's.
        request = TerminalRequest(
            channel=channel,
            claim=claim,
            name=params.get("name") if isinstance(params.get("name"), str) else None,
            cwd=params.get("cwd") if isinstance(params.get("cwd"), str) else None,
            cols=cols if isinstance(cols, int) else None,
            rows=rows if isinstance(rows, int) else None,
        )

        terminal = _Terminal(channel=channel, parser=ShellIntegrationParser())
        # In the live map BEFORE the process exists. A pty backend arms its
        # reader inside `create`, so the child's first bytes -- a fast prompt,
        # an immediate error -- can reach `_on_terminal_output` while this
        # method is still awaiting registration below. With no entry they were
        # returned to nobody; with an un-`ready` entry they are buffered and
        # flushed once the channel can carry them.
        self._live_terminals[channel] = terminal
        try:
            process = await self.terminals.create(
                request, lambda chunk: self._on_terminal_output(channel, chunk)
            )
        except BaseException:
            # The refusing default backend raises here; a half-created entry
            # must not make the URI look occupied to the retry.
            self._live_terminals.pop(channel, None)
            raise
        terminal.process = process

        state: dict[str, Any] = {
            "content": [],
            "claim": claim.to_wire(),
            # Required since 0.9.0; `terminal/exited` moves it to `exited`.
            "lifecycle": dict(_TERMINAL_RUNNING),
            "isPty": process.is_pty,
            # `title` is REQUIRED by `TerminalState`, and `name` is OPTIONAL on
            # `CreateTerminalParams`, so the fallback is not a nicety: an
            # unnamed terminal published a state that failed the whole
            # `Snapshot.state` union while the root catalogue substituted
            # "Terminal" -- one terminal with two titles, one of them invalid.
            # `or`, not `is None`: `name: ""` is schema-valid and renders as a
            # blank tab, which is the same unusable row by another route.
            "title": request.name or _DEFAULT_TERMINAL_TITLE,
        }
        for key, value in (
            ("cwd", request.cwd),
            ("cols", request.cols),
            ("rows", request.rows),
        ):
            if value is not None:
                state[key] = value
        # Bound at registration, never routed from the scheme: VS Code uses
        # three `agenthost-terminal:` forms and the spec's examples use a
        # fourth (invariant 15).
        try:
            await self.sequencer.register_channel(channel, state, "terminal")
        except BaseException:
            self._live_terminals.pop(channel, None)
            await terminal.close()
            raise
        self._channel_created(connection, channel)
        # Everything the child wrote during the spawn window, in order, BEFORE
        # `ready` flips: a chunk arriving mid-flush is appended behind the ones
        # being flushed, and nothing runs between the loop's final empty check
        # and the flip, so order is preserved and nothing is dropped.
        while terminal.early_output:
            await self._publish_terminal_output(terminal, terminal.early_output.pop(0))
        terminal.ready = True
        # Nothing published `terminal/exited`, so a shell that ended left the
        # channel looking live forever and the client's tab never closed: the
        # last frame after `exit 7` was the input echo, and then silence.
        terminal.reaper = asyncio.create_task(self._reap_terminal(terminal))
        self._background.add(terminal.reaper)
        terminal.reaper.add_done_callback(self._background.discard)
        await self._publish_terminal_catalogue()
        self._audit("terminal.created", connection, channel=channel)
        return

    async def _reap_terminal(self, terminal: _Terminal) -> None:
        """Wait for the child and announce its exit.

        Ordering matters and is not a nicety: the parser is flushed FIRST, so
        the tail of a burst -- everything the shell wrote between the last read
        and its exit -- reaches the client before `terminal/exited`, rather
        than after a frame that says there is nothing more coming.
        """
        process = terminal.process
        if process is None:
            return
        try:
            exit_code = await process.wait()
        except asyncio.CancelledError:
            raise
        except Exception:
            _log.exception("terminal %s: waiting on the child failed", terminal.channel)
            exit_code = None

        if terminal.channel not in self._live_terminals:
            # Disposed while we were waiting. `disposeTerminal` already dropped
            # the channel, and publishing onto a dropped channel is a no-op --
            # but announcing an exit for a terminal the client has forgotten is
            # noise even when it is harmless.
            return

        # Feeding `b""` would NOT do this: `flush()` returns the held bytes,
        # and re-parsing nothing discards them. Without it the tail of a burst
        # -- everything written between the last read and the exit -- is lost.
        await self._publish_terminal_items(terminal, terminal.parser.flush().items)
        action: dict[str, Any] = {"type": "terminal/exited"}
        if exit_code is not None:
            action["exitCode"] = exit_code
        await self.sequencer.publish(terminal.channel, action)
        await self._publish_terminal_catalogue()
        _log.info("terminal %s exited with %s", terminal.channel, exit_code)

    async def _dispose_terminal(self, connection: Connection, params: Mapping[str, Any]) -> None:
        # `result: null` per the CommandMap, on BOTH paths -- see
        # `_create_terminal`.
        channel = params.get("channel")
        if not isinstance(channel, str):
            raise errors.invalid_params("channel is required")
        # Authorized BEFORE anything is removed. Popping first meant an
        # unauthorized caller still evicted the terminal from the live map on
        # its way to being refused -- the refusal was returned and the damage
        # was already done.
        if not self.policy.may_see_channel(connection.info, channel):
            raise errors.AhpError(-32009, f"Not permitted to dispose {channel}")
        terminal = self._live_terminals.pop(channel, None)
        if terminal is None:
            # Disposal is idempotent. Erroring here made a client that failed
            # to CREATE a terminal fail again trying to clean it up, which is
            # how this host answered every one of VS Code's three disposals
            # with an error after refusing all three creations.
            return
        await terminal.close()
        await self.sequencer.drop_channel(channel)
        self._channel_dropped(channel)
        await self._publish_terminal_catalogue()
        self._audit("terminal.disposed", connection, channel=channel)
        return

    async def _publish_terminal_catalogue(self) -> None:
        """`RootState.terminals`. Full replacement, as the reducer expects."""
        await self.sequencer.publish(
            ROOT_URI,
            {
                "type": "root/terminalsChanged",
                "terminals": [
                    {"resource": uri, **_terminal_info(self.sequencer.state_of(uri))}
                    for uri in self._live_terminals
                ],
            },
        )

    def _on_terminal_output(self, channel: str, chunk: bytes) -> Any:
        terminal = self._live_terminals.get(channel)
        if terminal is None:
            return None
        if not terminal.ready:
            # Output that raced ahead of channel registration. Buffered here,
            # synchronously, so it keeps stream order with everything that
            # follows; `_create_terminal` flushes it once publishing can reach
            # a subscriber.
            terminal.early_output.append(chunk)
            return None
        return self._spawn_result(self._publish_terminal_output(terminal, chunk))

    def _spawn_result(self, coroutine: Coroutine[Any, Any, None]) -> None:
        self._spawn(coroutine)

    async def _publish_terminal_output(self, terminal: _Terminal, chunk: bytes) -> None:
        """Map one read into actions, **in stream order**.

        The order matters: a single read can carry
        `output ESC]633;D ESC]633;C output`, and treating the chunk as
        (all text, then all events) would append the second half of the output
        to the wrong content part.
        """
        await self._publish_terminal_items(terminal, terminal.parser.feed(chunk).items)

    async def _publish_terminal_items(self, terminal: _Terminal, items: Sequence[Any]) -> None:
        """Publish already-parsed output. Shared with the exit path, which has
        a `flush()` result rather than a chunk to feed."""
        channel = terminal.channel
        for item in items:
            if isinstance(item, str):
                if item:
                    await self.sequencer.publish(channel, {"type": "terminal/data", "data": item})
                continue
            if not terminal.announced:
                terminal.announced = True
                await self.sequencer.publish(
                    channel, {"type": "terminal/commandDetectionAvailable"}
                )
            if isinstance(item, CommandLine):
                terminal.pending_command = item.command_line
            elif isinstance(item, CommandStart):
                terminal.command_id = f"cmd-{uuid.uuid4()}"
                await self.sequencer.publish(
                    channel,
                    {
                        "type": "terminal/commandExecuted",
                        "commandId": terminal.command_id,
                        "commandLine": terminal.pending_command,
                        # "Unix timestamp (ms)", declared `number`. We sent an
                        # ISO string, which the client stores and then does
                        # arithmetic on.
                        "timestamp": _unix_ms(),
                    },
                )
                terminal.started_ms = _unix_ms()
                terminal.pending_command = ""
            elif isinstance(item, CommandFinished) and terminal.command_id is not None:
                await self.sequencer.publish(
                    channel,
                    {
                        "type": "terminal/commandFinished",
                        "commandId": terminal.command_id,
                        # OMITTED when the shell reported none, never an
                        # explicit null: the schema declares `exitCode` an
                        # optional number ("`undefined` if the shell did not
                        # report one"), and the reducer writes the value
                        # through into `TerminalCommandPart` -- so a published
                        # null failed the action schema AND every later
                        # snapshot of the channel. Same handling as
                        # `terminal/exited` above.
                        **({"exitCode": item.exit_code} if item.exit_code is not None else {}),
                        # The client renders `finish(exitCode, durationMs)` and
                        # falls back to `??0`, so omitting it made every
                        # command read as instantaneous.
                        **(
                            {"durationMs": max(0, _unix_ms() - terminal.started_ms)}
                            if terminal.started_ms
                            else {}
                        ),
                    },
                )
                terminal.started_ms = 0
                terminal.command_id = None
            elif isinstance(item, CwdReported):
                # OSC 633 reports a PATH; `terminal/cwdChanged.cwd` is a URI,
                # as is `TerminalState.cwd`. Publishing the path raw was the
                # mirror image of the bug that made a client's `file:` URI
                # unusable as a directory -- the same confusion, the other way.
                await self.sequencer.publish(
                    channel, {"type": "terminal/cwdChanged", "cwd": _cwd_uri(item.cwd)}
                )
        self._trim_terminal(channel)

    def _trim_terminal(self, channel: str) -> None:
        """Drop the oldest scrollback, silently.

        The guide licenses either side trimming independently. Publishing a
        server-side `terminal/cleared` -- what the reference host does -- is
        rejected here because the protocol has no partial-trim action: `cleared`
        wipes the whole buffer, so trimming the oldest 10% would blank the
        screen of every subscriber, including ones with memory to spare.
        """
        state = self.sequencer.state_of(channel)
        if not isinstance(state, Mapping):
            return
        content = state.get("content")
        if not isinstance(content, list):
            return
        trimmed = trim_scrollback(content)
        if trimmed is not content and trimmed != content:
            self.sequencer._states[channel] = {**state, "content": trimmed}

    # ─── authentication ──────────────────────────────────────────────────

    def _protected_resources(self) -> list[ProtectedResource]:
        """Every resource any agent here advertises, each once."""
        seen: dict[str, ProtectedResource] = {}
        for each in self.providers.values():
            for wire in each.agent.protected_resources:
                resource = ProtectedResource.from_wire(wire)
                seen.setdefault(resource.resource, resource)
        return list(seen.values())

    async def _authenticate(
        self, connection: Connection, params: Mapping[str, Any]
    ) -> dict[str, Any]:
        """Accept a token for an upstream service the AGENT talks to.

        **This is not a login.** It never gates the AHP connection -- admission
        happened at `initialize`, and `Policy` decided it. What arrives here is a
        credential for something else entirely, and the host is a courier for it.

        The resource must be one the host advertised. Accepting an unadvertised
        one would let a peer fill the store with credentials for services this
        agent never mentioned, and give it no way to learn that they are useless.

        "Advertised" is three mechanisms, not one: "whether declared statically
        in `AgentInfo.protectedResources`, or discovered dynamically from a live
        `McpServerAuthRequiredState.resource` or
        `ToolCallAuthRequiredState.auth.resource` ... Servers MUST accept any
        `resource` value they have themselves advertised through one of these
        three mechanisms." Checking only the static list deadlocked the step-up
        flow for any provider that challenges for a resource it never declared
        up front: the client's token was refused -32602 and the parked call
        could never clear.
        """
        resource = params.get("resource")
        token = params.get("token")
        if not isinstance(resource, str) or not isinstance(token, str):
            raise errors.invalid_params("resource and token are required")

        known = {r.resource for r in self._protected_resources()}
        if resource not in known and resource not in self._dynamic_resources:
            raise errors.invalid_params(f"{resource!r} is not a protected resource of this agent")
        if not self.policy.may_push_token(connection.info, resource):
            self._audit("auth.refused", connection, allowed=False, detail={"resource": resource})
            raise errors.AhpError(-32009, f"Not permitted to authenticate {resource}")

        raw_scopes = params.get("scopes")
        scopes = (
            [s for s in raw_scopes if isinstance(s, str)] if isinstance(raw_scopes, list) else None
        )
        self.tokens.push(resource, token, scopes=scopes, client_id=connection.client_id)
        await self._resolve_auth_challenges(resource, scopes)
        # The resource, never the token. `AuditEvent` carries identifiers only,
        # and a credential in an audit record is a credential on disk.
        self._audit("auth.accepted", connection, detail={"resource": resource})
        # `AuthenticateResult` is `{}` on the wire. The reference's internal type
        # says `{authenticated: boolean}`; the wire handler returns `{}`, and the
        # spec agrees with the wire.
        return {}

    def _advertise_resource(self, resource: str) -> None:
        """Record a protected resource advertised through a live challenge.

        Called from the turn sink (`ToolCallAuthRequiredState.auth.resource`)
        and the publisher (`McpServerAuthRequiredState.resource`), the two
        dynamic mechanisms `authenticate` MUST honour alongside the static
        `AgentInfo` list.
        """
        self._dynamic_resources.add(resource)

    async def _resolve_auth_challenges(
        self, resource: str, scopes: Sequence[str] | None = None
    ) -> None:
        """Wake every tool call that was paused waiting for THIS credential.

        Resolved on the `authenticate` command rather than on an action, which
        is what makes step-up different from every other suspended request here:
        the resolution arrives as a COMMAND, not through `dispatchAction`. The
        registry does not care -- that is the point of having one.

        Only the calls whose challenge named the pushed resource, and whose
        required scopes the push covers: "It's resolved by the client obtaining
        a token for `auth.resource`", and the reference session checks both
        before resolving (`copilotAgentSession.ts:1756`). Waking everything on
        any push resumed a call blocked on resource B with a token for resource
        A -- across sessions, since the registry is host-global -- and the
        unsatisfied call just failed upstream again and re-challenged.
        """
        for request_id in list(self.pending.ids_of_kind("auth")):
            request = self.pending.get(request_id)
            if request is None:
                continue
            # A park that named no resource stays answerable by any push -- the
            # same anywhere-answerable default a channel-less park gets.
            if request.resource is not None and request.resource != resource:
                continue
            if not scopes_satisfied(
                scopes,
                request.required_scopes,
                unscoped_satisfies_any=self.tokens.unscoped_satisfies_any,
            ):
                continue
            if self.pending.resolve(request_id, RequestOutcome("accept", {"resource": resource})):
                for session in self._sessions.values():
                    await self._retract_input_needed(session, request_id)

    def require_auth(self) -> None:
        """Raise `-32007` if anything the agent needs is still unauthenticated.

        For an embedder to call from a provider hook. The `data` field is a MUST
        -- a client uses it to know *what* to authenticate -- so it is built from
        the advertised resources rather than left empty.
        """
        outstanding = self.tokens.unsatisfied(self._protected_resources())
        if outstanding:
            raise auth_required(outstanding)

    async def notify_auth_required(
        self, resource: str | ProtectedResource, *, reason: AuthRequiredReason = "required"
    ) -> None:
        """Tell subscribers a credential is needed, or has expired.

        Ephemeral and explicitly not replayed, so `-32007` on the next command
        is the complete fallback -- a client that missed this still finds out.

        The notification carries the resource's full metadata (0.8.0). A bare
        identifier is resolved against what the agent advertises, so the
        metadata a client receives is the same it saw on `AgentInfo`; one the
        agent never advertised goes out as `{resource}` alone.
        """
        if isinstance(resource, str):
            resource = next(
                (r for r in self._protected_resources() if r.resource == resource),
                ProtectedResource(resource),
            )
        await self.sequencer.notify(
            ROOT_URI, AUTH_REQUIRED_METHOD, auth_required_params(resource, reason=reason)
        )

    # ─── telemetry ───────────────────────────────────────────────────────

    async def emit_telemetry(self, signal: str, payload: Mapping[str, Any]) -> None:
        """Publish one OTLP/JSON batch on the channel advertised for `signal`.

        A thin pass-through, deliberately: "payloads on the wire are OTLP/JSON
        values verbatim; AHP only adds the routing envelope". Nothing here
        parses, validates or re-encodes the payload, so this host owes no OTLP
        implementation and cannot corrupt one.

        Ephemeral by design -- "telemetry is not replayed on reconnect" -- so it
        goes out as a notification and never touches the replay log. A signal
        this host did not advertise is dropped: a client that never saw the
        channel on `initialize` has not subscribed to it.
        """
        channel = self.telemetry.get(signal)
        method = _OTLP_METHODS.get(signal)
        if channel is None or method is None:
            return
        await self.sequencer.notify(channel, method, {"channel": channel, "payload": payload})

    # ─── completions ─────────────────────────────────────────────────────

    async def _completions(
        self, connection: Connection, params: Mapping[str, Any]
    ) -> dict[str, Any]:
        """Suggest attachments for what the user is typing.

        `CompletionsParams.channel` is documented as "the chat URI", but VS Code
        sends the **session** URI while upstream's own e2e suite sends a chat
        URI -- and the reference host accepts both. So this accepts both too:
        being strict here would break the one client that exists, over a field
        the spec's own implementations disagree about.
        """
        channel = params.get("channel")
        text = params.get("text")
        if not isinstance(channel, str) or not isinstance(text, str):
            raise errors.invalid_params("channel and text are required")
        if not self.policy.may_see_channel(connection.info, channel):
            raise errors.AhpError(-32009, f"Not permitted to observe {channel}")

        chat = channel
        session = self._sessions.get(channel)
        if session is not None:
            chat = session.chat_uri
        elif not any(channel in s.chat_uris for s in self._sessions.values()):
            raise errors.invalid_params(f"{channel} is neither a session nor a chat")

        provider = self._provider_of_channel(chat)
        if not isinstance(provider, Completes):
            return {"items": []}

        offset = params.get("offset")
        kind = params.get("kind")
        items = await provider.complete(
            CompletionRequest(
                kind=kind if isinstance(kind, str) else "",
                chat=chat,
                text=text,
                offset=offset if isinstance(offset, int) else len(text),
            )
        )
        return {"items": [item.to_wire() for item in items]}

    # ─── multi-chat ──────────────────────────────────────────────────────

    def _multichat(self, provider: AgentProvider) -> Mapping[str, Any] | None:
        """`AgentCapabilities.multipleChats`, or ``None``.

        Absent means "clients MUST NOT call `createChat` to open chats beyond
        the default one the session starts with" -- another client MUST that
        only the host can enforce, since a client that ignores it just sends the
        command anyway.
        """
        capability = provider.agent.capabilities.get("multipleChats")
        return capability if isinstance(capability, Mapping) else None

    async def _create_chat(self, connection: Connection, params: Mapping[str, Any]) -> None:
        """Open a second chat in a session.

        The chat URI is **client-chosen**, which contradicts `chat-channel.md:71`
        ("the server allocates the chat URI"). The types say client-chosen
        (`CreateChatParams.chat`), the reference host implements client-chosen,
        and VS Code's client sends one -- so three implementations agree against
        one sentence of prose. Recorded as an upstream question in
        `docs/roadmap.md` section 11.

        Returns ``None`` -- `result: null` per the CommandMap, see
        `_create_terminal`.
        """
        session_uri = params.get("channel")
        chat_uri = params.get("chat")
        if not isinstance(session_uri, str) or not isinstance(chat_uri, str):
            raise errors.invalid_params("channel and chat are required")
        session = self._sessions.get(session_uri)
        if session is None:
            raise errors.session_not_found(session_uri)
        if not self.policy.may_see_channel(connection.info, session_uri):
            raise errors.AhpError(-32009, f"Not permitted to modify {session_uri}")

        capability = self._multichat(self._provider_of(session))
        if capability is None:
            raise errors.invalid_params("this agent does not advertise multipleChats")
        if self.sequencer.has_channel(chat_uri):
            # `AlreadyExists` (-32010), not `SessionAlreadyExists` (-32003) --
            # the same reasoning as `_create_terminal`: -32003 is "a session
            # with the given URI already exists" and the shared helper's
            # message says "Session", on a URI that names no session.
            raise errors.AhpError(
                AHP_ERROR_CODES["AlreadyExists"], f"Channel already exists: {chat_uri}"
            )

        source = params.get("source")
        origin = self._chat_origin(session, capability, source)
        directories = self._chat_working_directories(session, params, source)

        created_at = now_iso()
        state: dict[str, Any] = {
            "resource": chat_uri,
            "title": _DEFAULT_CHAT_TITLE,
            "status": _STATUS_IDLE,
            "modifiedAt": created_at,
            # A forked CHAT inherits the source chat's transcript, exactly as a
            # forked SESSION does; a side chat does not, because its whole
            # point is a separate conversation that the client seeds with its
            # own context. `_chat_origin` has already validated the turn id.
            "turns": (
                self._copy_turns(origin["chat"], origin.get("turnId"))
                if origin is not None and origin["kind"] == "fork"
                else []
            ),
        }
        if origin is not None:
            state["origin"] = origin
        if directories is not None:
            state["workingDirectories"] = list(directories)
        await self.sequencer.register_channel(chat_uri, state, "chat")
        self._channel_created(connection, chat_uri, session=session_uri)
        session.chat_uris.add(chat_uri)

        session.published_chats[chat_uri] = {
            "title": state["title"],
            "status": _STATUS_IDLE,
            "modifiedAt": created_at,
        }
        await self.sequencer.publish(
            session_uri,
            {
                "type": "session/chatAdded",
                "summary": _with_origin(
                    {
                        "resource": chat_uri,
                        "title": state["title"],
                        "status": _STATUS_IDLE,
                        "modifiedAt": created_at,
                    },
                    origin,
                ),
            },
        )
        self._audit("chat.created", connection, channel=chat_uri)

        initial = params.get("initialMessage")
        if isinstance(initial, Mapping):
            # Delivered as an ordinary turn, so the whole turn machinery --
            # sequencing, the suspending primitive, cancellation -- applies to a
            # forked chat exactly as it does to the default one.
            started = {
                "type": "chat/turnStarted",
                "turnId": f"t-{uuid.uuid4()}",
                "startedAt": created_at,
                "message": dict(initial),
            }
            await self.sequencer.publish(chat_uri, started)
            # The action it just published, NOT a stub. `TurnRunner.run` reads
            # `turnId` off what it is handed and returns on its first line when
            # it is absent -- so a chat created with an `initialMessage` sat at
            # `activeTurn` forever with the agent never having seen the message,
            # and every later `chat/turnStarted` was rejected as "a turn is
            # already active". Wedged from birth, and silently: `createChat`
            # answered `{}`.
            await self._react(chat_uri, started)
        await self._mirror_summary(session)
        return

    def _chat_origin(
        self, session: _Session, capability: Mapping[str, Any], source: Any
    ) -> dict[str, Any] | None:
        """Validate `source` and turn it into the new chat's `origin`.

        Each mode has its own capability sub-flag, and "clients MUST only
        request `kind: 'fork'` when the selected agent advertises
        `capabilities.multipleChats.fork'" -- a client MUST, so the host checks
        it.
        """
        if not isinstance(source, Mapping):
            return None
        kind = source.get("kind")
        if kind not in ("fork", "sideChat"):
            raise errors.invalid_params(f"unknown chat source {kind!r}")
        flag = "fork" if kind == "fork" else "sideChat"
        if not capability.get(flag):
            raise errors.invalid_params(f"this agent does not advertise multipleChats.{flag}")

        source_chat = source.get("chat")
        if not isinstance(source_chat, str) or source_chat not in session.chat_uris:
            # "The source chat MUST belong to this session."
            raise errors.invalid_params("source chat does not belong to this session")

        origin: dict[str, Any] = {"kind": kind, "chat": source_chat}
        turn_id = source.get("turnId")
        if not isinstance(turn_id, str):
            # Required on BOTH source forms and on both `ChatOrigin` variants
            # that carry a chat (`commands.schema.json` ForkChatSource /
            # SideChatSource `required`, `state.schema.json` ChatOrigin
            # `required`). Accepting a source without one meant publishing a
            # `ChatOrigin` that fails the state schema -- so the whole
            # `Snapshot.state` union fails for every client that validates,
            # over a field the client simply forgot to send.
            raise errors.invalid_params("source.turnId is required")
        # Validated, not just copied. Publishing an origin that names a turn the
        # source chat does not have claims a provenance that never existed, and
        # nothing downstream ever checks it -- the same class of silent lie as a
        # fork that copies nothing and reports success.
        self._copy_turns(source_chat, turn_id)
        origin["turnId"] = turn_id
        selection = source.get("selection")
        if kind == "sideChat" and selection is not None:
            # "The host MUST snapshot and preserve this exact selection when it
            # accepts `createChat`; later source-turn deltas do not alter it."
            # Copied, therefore, never referenced -- and checked first, because
            # "preserve this exact selection" is what makes an unvalidated one
            # permanent: `{"text": ""}` and `{"text": 123}` were stored verbatim
            # into a `SideChatSelection` whose `text` is required and whose spec
            # says "MUST be non-empty".
            origin["selection"] = _side_chat_selection(selection)
        return origin

    def _chat_working_directories(
        self, session: _Session, params: Mapping[str, Any], source: Any
    ) -> Sequence[str] | None:
        """The chat's directory subset, validated against the session's set."""
        if isinstance(source, Mapping) and source.get("kind") == "fork":
            # "Forked chats inherit the source chat's `workingDirectories`; this
            # field is ignored for forks."
            return None
        requested = params.get("workingDirectories")
        if requested is None:
            return None
        if self._multiroot(self._provider_of(session)) is None:
            raise errors.invalid_params("this agent does not advertise multipleWorkingDirectories")
        state = self.sequencer.state_of(session.uri)
        owned = state.get("workingDirectories") if isinstance(state, Mapping) else None
        owned = owned if isinstance(owned, list) else []
        subset = [d for d in requested if isinstance(d, str)]
        for directory in subset:
            if directory not in owned:
                # "Every entry MUST be present in the owning session's
                # `workingDirectories`; the server MUST reject any entry that is
                # not." Without this a chat names a filesystem root the session
                # was never granted.
                raise errors.invalid_params(f"{directory} is not a session working directory")
        return subset

    async def _dispose_chat(self, connection: Connection, params: Mapping[str, Any]) -> None:
        """Dispose one chat.

        `DisposeChatParams` carries only `channel`, so the channel IS the chat --
        note that `chat-channel.md` says "the protocol does not currently expose
        a `disposeChat` command" while the types, the message map and the
        reference host all define it. Same contradiction as `createChat`, same
        resolution: three implementations against one sentence.

        Returns ``None`` -- `result: null` per the CommandMap, see
        `_create_terminal`.
        """
        chat_uri = params.get("channel")
        if not isinstance(chat_uri, str):
            raise errors.invalid_params("channel is required")
        session = next((s for s in self._sessions.values() if chat_uri in s.chat_uris), None)
        if session is None:
            raise errors.AhpError(-32008, f"No such chat: {chat_uri}")
        if not self.policy.may_see_channel(connection.info, chat_uri):
            raise errors.AhpError(-32009, f"Not permitted to dispose {chat_uri}")
        if chat_uri == session.chat_uri:
            # The default chat is the session. Disposing it would leave a
            # session with no `defaultChat`, which every client reads.
            raise errors.invalid_params("the default chat cannot be disposed")

        # Before the channel goes: a turn running in this chat has nowhere to
        # publish once it is dropped, and it holds the provider. Dropping the
        # channel underneath it left the agent still working on a chat nobody
        # can see, and -- worse -- left any confirmation it was parked on
        # advertised in `session/inputNeeded`, pinning the SESSION at
        # `InputNeeded` with a request that can never be answered because the
        # channel it must be answered on no longer exists. Cancelling first
        # ends the turn's pending scope, which is what retracts those entries
        # (`_run_turn`'s finally).
        await self._cancel_turn(session, chat_uri, "chat disposed")
        # And SAID so, on the channel, while it still exists. Cancelling the
        # task ends the turn for the host; a subscribed client learns nothing
        # from a channel that simply stops, and waits for a terminal action
        # that is never coming.
        await self._end_stranded_turn(session, chat_uri)

        session.chat_uris.discard(chat_uri)
        await self.sequencer.publish(session.uri, {"type": "session/chatRemoved", "chat": chat_uri})
        await self.sequencer.drop_channel(chat_uri)
        self._channel_dropped(chat_uri)
        self._audit("chat.disposed", connection, channel=chat_uri)
        await self._mirror_summary(session)
        return

    # ─── changesets ──────────────────────────────────────────────────────

    # ─── telling the policy which channels exist ─────────────────────────

    def _channel_created(
        self, connection: Connection | None, channel: str, *, session: str | None = None
    ) -> None:
        """Tell a `TracksChannels` policy about a channel, if it wants to know.

        Called at EVERY registration site rather than only at `createSession`,
        because a session goes on to create chats, terminals, changesets and
        watches, and a policy that only learned the session URI would refuse
        all of them. The prefix walk cannot substitute: a chat URI is
        `ahp-chat://<chatId>/<base64 session uri>`, so the session URI is
        inside it, not its parent.
        """
        if isinstance(self.policy, TracksChannels):
            info = connection.info if connection is not None else None
            self.policy.channel_created(info, channel, session=session)

    def _channel_dropped(self, channel: str) -> None:
        if isinstance(self.policy, TracksChannels):
            self.policy.channel_dropped(channel)

    def _session_metadata(self, session: str) -> Mapping[str, Any] | None:
        if isinstance(self.policy, TracksChannels):
            return self.policy.session_metadata(session)
        return None

    def _content_owner(self, uri: str) -> _Session | None:
        """The session whose store holds `uri`, if any."""
        for session in self._sessions.values():
            if session.content.owns(uri):
                return session
        return None

    async def publish_changeset(
        self,
        session_uri: str,
        changeset: Changeset,
        changes: Sequence[FileChange],
    ) -> str:
        """Publish (or refresh) a changeset and its file list.

        The channel is registered here, when the host mints the URI, so a
        subscribe is answered by an exact-string lookup and nothing ever parses
        a channel URI (invariant 15).
        """
        session = self._sessions.get(session_uri)
        if session is None:
            raise errors.session_not_found(session_uri)

        previous = session.changesets.get(changeset.uri)
        first = previous is None
        if not first:
            # Back to `computing` before the list is replaced. Without it the
            # file list swaps under the user with nothing to say a refresh
            # happened, and the client's progress bar -- which it renders for
            # exactly this status -- never appears. Only on a REFRESH: the
            # first publish already registers the channel in `computing`.
            await self.sequencer.publish(
                changeset.uri, {"type": "changeset/statusChanged", "status": "computing"}
            )

        already = self._reviewed_ids(session, changeset.uri)
        files = [
            file_entry(change, session.content, reviewed=_entry_id(change) in already)
            for change in changes
        ]
        session.changesets[changeset.uri] = changeset
        if first:
            await self.sequencer.register_channel(
                changeset.uri, {"status": "computing", "files": []}, "changeset"
            )
            # No connection here -- a changeset is published by the PROVIDER,
            # out of band. Ownership is inherited from the session it belongs
            # to, which is why `session` is part of the signature.
            self._channel_created(None, changeset.uri, session=session_uri)
        # Re-emitted whenever the ENTRY changed, not only on the first publish.
        # The catalogue is the only copy a client has of the label, the
        # description and `capabilities.review`, and it was written once and
        # never again -- so a changeset that became reviewable stayed
        # un-reviewable on screen while `_validate_review` read the new entry
        # and accepted the review anyway. Full-replacement semantics, so the
        # whole catalogue goes out.
        if previous is None or previous.to_catalogue_entry() != changeset.to_catalogue_entry():
            await self.sequencer.publish(
                session_uri,
                {
                    "type": "session/changesetsChanged",
                    "changesets": [c.to_catalogue_entry() for c in session.changesets.values()],
                },
            )

        # `contentChanged` replaces the file list wholesale and carries the
        # operations in the same action, so a client never sees a changeset with
        # files but no buttons.
        #
        # ALWAYS carried, empty list included: on this action "omit when
        # operations are unchanged", so omitting an empty list left the previous
        # buttons standing. That is not cosmetic -- `_declared_operation`
        # answers "did this changeset declare it" out of the channel's state, so
        # a republish that withdrew every operation left them all invocable.
        action: dict[str, Any] = {
            "type": "changeset/contentChanged",
            "files": files,
            "operations": self._operations_wire(session, changeset),
        }
        await self.sequencer.publish(changeset.uri, action)
        await self.sequencer.publish(
            changeset.uri, {"type": "changeset/statusChanged", "status": "ready"}
        )

        # The roll-up a session list renders, summed from the per-file diffs the
        # host just computed -- so the list and the changeset cannot disagree.
        await self._set_changes_summary(session, files)
        return changeset.uri

    async def _set_changes_summary(
        self, session: _Session, files: Sequence[Mapping[str, Any]]
    ) -> None:
        # `SessionSummary.changes` has no action of its own, so it cannot ride
        # on the session channel: it was written straight into `SessionState`,
        # which declares no such key, and with no envelope behind it every
        # already-subscribed client kept the old number while every later one
        # got a field the schema does not define. It belongs to the summary
        # alone, and `root/sessionSummaryChanged` is the frame that carries it.
        session.changes = changes_summary(files)
        await self._mirror_summary(session)

    def _reviewed_ids(self, session: _Session, channel: str) -> set[str]:
        """Which files are ticked, reconciled from the channel's own state.

        `changeset/contentChanged` replaces the file list wholesale, so a
        republish that did not restate the ticks cleared every one -- which
        reads from the outside as the checkbox being broken, and is how it was
        reported.

        Reconciled from state rather than recorded at dispatch, because the
        action has TWO originators. "Unlike every other `changeset/*` action
        this one is client-dispatchable ... The server MAY also originate it
        (e.g. an agent marking its own output reviewed)." Only the client path
        ran through `_react`, so the host's own tick -- the `ahs-review`
        operation the demo ships -- was wiped by the very republish that
        operation triggers. The reducer has already applied both by the time
        anyone republishes, so the state is the one place that has seen each.

        Ids absent from the current list keep whatever was remembered: a file
        that leaves the changeset and comes back should not silently lose its
        tick, and the state cannot say anything about a file it does not hold.
        """
        remembered = session.reviewed.setdefault(channel, set())
        state = self.sequencer.state_of(channel)
        entries = state.get("files") if isinstance(state, Mapping) else None
        for entry in entries or ():
            if not isinstance(entry, Mapping):
                continue
            identifier = entry.get("id")
            if not isinstance(identifier, str):
                continue
            # `is True` rather than truthiness: "absent is equivalent to
            # `false`", and an explicit `false` must clear the memory, not be
            # confused with an absent key.
            if entry.get("reviewed") is True:
                remembered.add(identifier)
            else:
                remembered.discard(identifier)
        return remembered

    def _operation_target(
        self, declared: Mapping[str, Any], operation: str, target: Any
    ) -> Mapping[str, Any] | None:
        """Validate `invokeChangesetOperation.target` against declared scopes.

        "Required iff the chosen scope is `resource` or `range`", and "the
        `kind` MUST match one of the operation's declared `scopes`". Checked
        here rather than left to each handler: an embedder writing a per-file
        operation should be able to trust that a target is present when the
        scope says it will be.

        Takes the PUBLISHED entry, not an id to look up. The lookup used to
        live here and returned an empty set for an id the changeset never
        declared, which made every `if declared` guard below vacuous -- so an
        undeclared id skipped the scope and target checks entirely, on its way
        to a handler that should never have been reached.
        """
        kinds = {kind for kind in declared.get("scopes") or () if isinstance(kind, str)}
        if target is None:
            if kinds and "changeset" not in kinds:
                raise errors.invalid_params(f"{operation!r} requires a target")
            return None
        if not isinstance(target, Mapping):
            raise errors.invalid_params("target must be an object")
        kind = target.get("kind")
        if kind not in ("resource", "range"):
            raise errors.invalid_params(f"unknown target kind {kind!r}")
        if kind not in kinds:
            raise errors.invalid_params(f"{operation!r} does not declare the {kind!r} scope")
        if not isinstance(target.get("resource"), str):
            raise errors.invalid_params("target.resource is required")
        if kind == "range":
            # The range variant requires `range`, and `TextRange` requires both
            # ends. Without this the handler is the first thing to notice, and
            # what it raises becomes -32603 -- "the host has a bug" for an
            # unambiguous caller mistake.
            span = target.get("range")
            if not isinstance(span, Mapping) or not all(
                isinstance(span.get(end), Mapping) for end in ("start", "end")
            ):
                raise errors.invalid_params("a range target requires range.start and range.end")
        return target

    def _declared_operation(self, channel: str, operation: str) -> Mapping[str, Any] | None:
        """The operation as this changeset currently publishes it, or ``None``.

        "The server validates that `operationId` exists in the changeset's
        current `operations` list" -- current, so the published state is the
        authority rather than the embedder's registry. The two differ on
        purpose: the demo gates `commit` out of the list when nothing is
        staged while leaving the handler registered, and without this check
        the button that is not on screen still ran.
        """
        state = self.sequencer.state_of(channel)
        operations = state.get("operations") if isinstance(state, Mapping) else None
        for entry in operations or ():
            if isinstance(entry, Mapping) and entry.get("id") == operation:
                return entry
        return None

    async def _invoke_changeset_operation(
        self, connection: Connection, params: Mapping[str, Any]
    ) -> dict[str, Any]:
        """Run an embedder-registered operation. There are no built-in ones.

        `commit`, `create-pr`, `discard-changes` and `sync` are VS Code private
        string constants, not protocol names. One is a credentialed network call
        and one irreversibly destroys work, so what an operation id means is the
        embedder's decision and this host ships none of them.
        """
        channel = params.get("channel")
        operation = params.get("operationId")
        if not isinstance(channel, str) or not isinstance(operation, str):
            raise errors.invalid_params("channel and operationId are required")
        if self.sequencer.reducer_of(channel) != "changeset":
            raise errors.invalid_params(f"{channel} is not a changeset")
        if not self.policy.may_invoke_operation(connection.info, channel, operation):
            raise errors.AhpError(-32009, f"Not permitted to invoke {operation}")

        # Membership FIRST. "Undeclared" is a refusal, not a licence: an id the
        # changeset does not currently offer is one no client should have been
        # able to press, and treating it as unconstrained is worse than
        # treating it as unknown -- it was reaching the handler with no scope
        # or target validation at all.
        declared = self._declared_operation(channel, operation)
        if declared is None:
            raise errors.invalid_params(f"{channel} does not declare {operation!r}")

        handler = self._operations.get(operation)
        if handler is None:
            raise errors.invalid_params(f"unknown operation {operation!r}")

        target = self._operation_target(declared, operation, params.get("target"))

        # Refused while the agent is writing. Committing or reverting mid-turn
        # races the agent's own writes on the same files, and nothing on screen
        # says so. The reference host both greys the buttons and refuses the
        # invoke; greying alone is advisory, since the request can still arrive
        # from a stale UI.
        owner = next((s for s in self._sessions.values() if channel in s.changesets), None)
        # ANY chat: a commit races the agent's writes wherever they come from.
        if owner is not None and owner.running():
            raise errors.invalid_params(f"{operation!r} is disabled while a turn is active")

        await self.sequencer.publish(
            channel,
            {
                "type": "changeset/operationStatusChanged",
                "operationId": operation,
                "status": "running",
            },
        )
        try:
            await handler(channel, operation, target)
        except Exception as exc:
            await self.sequencer.publish(
                channel,
                {
                    "type": "changeset/operationStatusChanged",
                    "operationId": operation,
                    "status": "error",
                    "error": {"message": f"{type(exc).__name__}: {exc}"},
                },
            )
            # AND fail the request. The client's operation mapper drops `error`
            # entirely and computes enablement from `status !== "disabled" &&
            # status !== "running"`, so an `error` status renders exactly like
            # `idle` -- a failed operation was visually identical to one that
            # did nothing, which is precisely the complaint. A rejected request
            # is the one failure channel the client does surface.
            raise errors.AhpError(
                JSON_RPC_ERROR_CODES["InternalError"],
                f"{operation} failed: {type(exc).__name__}: {exc}",
            ) from exc
        await self.sequencer.publish(
            channel,
            {
                "type": "changeset/operationStatusChanged",
                "operationId": operation,
                "status": "idle",
            },
        )
        return {}

    def _operations_wire(self, session: _Session, changeset: Changeset) -> list[dict[str, Any]]:
        """The operations, greyed out while the agent is writing.

        `status: "disabled"` is what the client reads for enablement. Without
        it a user can commit or revert mid-turn, racing the agent's own writes,
        with nothing on screen to suggest they should not.

        A snapshot of one instant, which is why `_sync_operation_status` exists:
        a provider can only publish a changeset from inside its own turn, so
        every publish sampled "busy" and, with nothing to re-evaluate it, every
        control stayed greyed for the life of the session.
        """
        busy = bool(session.running())
        wire: list[dict[str, Any]] = []
        for operation in changeset.operations:
            entry = operation.to_wire()
            if busy:
                entry["status"] = "disabled"
            wire.append(entry)
        return wire

    async def _sync_operation_status(self, session: _Session) -> None:
        """Re-grey, or un-grey, every operation this session publishes.

        Called at both ends of a turn. The gate is "is a turn in flight", which
        was sampled once at publish time -- and since a provider publishes from
        inside the turn that produced the changes, the answer was always yes and
        never revisited. Every button was disabled from the first publish until
        the session died.

        `busy` is recomputed here rather than passed in, so a turn that starts
        while this is queued still ends up with the right answer.

        Only `idle` and `disabled` are touched. `running` belongs to an
        invocation in flight and `error` to the last one that failed -- and the
        client renders `error` as the only trace a failed operation leaves, so
        overwriting either would erase feedback this gate knows nothing about.
        """
        target = "disabled" if session.running() else "idle"
        for changeset_uri in list(session.changesets):
            state = self.sequencer.state_of(changeset_uri)
            operations = state.get("operations") if isinstance(state, Mapping) else None
            for entry in operations or ():
                if not isinstance(entry, Mapping):
                    continue
                identifier = entry.get("id")
                if entry.get("status") not in ("idle", "disabled") or not isinstance(
                    identifier, str
                ):
                    continue
                if entry.get("status") == target:
                    continue
                await self.sequencer.publish(
                    changeset_uri,
                    {
                        "type": "changeset/operationStatusChanged",
                        "operationId": identifier,
                        "status": target,
                    },
                )

    def register_operation(self, operation_id: str, handler: OperationHandler) -> None:
        """Make an operation invocable. Explicit, per operation, by the embedder."""
        self._operations[operation_id] = handler

    def session_of_changeset(self, changeset_uri: str) -> str | None:
        """Which session owns this changeset, if any.

        An operation handler is given the CHANGESET uri, but republishing takes
        the SESSION uri -- and an operation that changes the tree has to
        republish, because the client discards the `invokeChangesetOperation`
        result entirely and the changeset is the only feedback it renders.
        Without this an embedder has to reach into private state to close that
        loop.
        """
        for uri, session in self._sessions.items():
            if changeset_uri in session.changesets:
                return uri
        return None

    # ─── resource watches ────────────────────────────────────────────────

    async def _create_resource_watch(
        self, connection: Connection, params: Mapping[str, Any]
    ) -> dict[str, Any]:
        if self.watcher is None:
            raise errors.AhpError(-32009, "This host does not watch resources")
        uri = params.get("uri")
        if not isinstance(uri, str):
            raise errors.invalid_params("uri is required")

        # Resolved first, so the policy and the watch both name the canonical
        # target rather than whatever route the peer took to it.
        info = await self.resources.resolve(uri)
        # THE JAIL, and it was missing here. `RootedFilesystemResourceProvider`
        # lets a STRICT ANCESTOR of the served root resolve, so a directory
        # picker can stat the parent of a path before the path itself -- and
        # every other surface then refuses it. This one did not, so a recursive
        # watch rooted at an ancestor (up to `file:///`) was accepted and the
        # poller walked it, reporting names, existence and change timing for
        # files the same peer is refused a read of. The provider's own docstring
        # promises "reads, writes and watches stay refused"; two of three were
        # true. Found by driving the Python client against this host.
        if not self._inside_the_jail(info.uri):
            raise errors.AhpError(-32009, f"Not permitted to watch {uri}")
        if not self.policy.may_access_resource(connection.info, "list", info.uri):
            raise errors.AhpError(-32009, f"Not permitted to watch {uri}")

        owned = sum(1 for w in self._watches.values() if w.owner is connection)
        if owned >= self._max_watches:
            # A cap, because `createResourceWatch` is unauthenticated beyond the
            # connection and each watch is a standing background cost.
            raise errors.AhpError(-32009, "Too many watches on this connection")

        request = WatchRequest(
            root=info.uri,
            recursive=bool(params.get("recursive")),
            excludes=_items(params.get("excludes")),
            includes=_items(params.get("includes")),
        )
        channel = new_watch_channel()
        await self.sequencer.register_channel(channel, request.to_state(), "resourceWatch")
        self._channel_created(connection, channel)
        self._watches[channel] = _Watch(request=request, owner=connection)
        return {"channel": channel}

    def channel_observed(self, channel: str) -> None:
        """First subscriber: start watching.

        Started here rather than at `createResourceWatch` so a client that
        creates a watch and never subscribes costs nothing, and so a watcher
        cannot outlive the audience that justified it.
        """
        watch = self._watches.get(channel)
        if watch is None or self.watcher is None or watch.started:
            return
        watch.started = True
        self._spawn(self._start_watch(channel, watch))

    def channel_unobserved(self, channel: str) -> None:
        """Last subscriber gone: stop watching and drop the channel."""
        watch = self._watches.pop(channel, None)
        if watch is None:
            return
        self._spawn(self._stop_watch(channel, watch))

    async def _release_watches(self, connection: Connection) -> None:
        """Drop the watches this connection created and nobody ever subscribed to.

        `channel_unobserved` was the only release path and it cannot fire
        without a subscriber, so a peer that called `createResourceWatch` and
        never subscribed left the channel registered, the `_Watch` in this dict
        and a STRONG REFERENCE to its closed connection behind -- for the life
        of the host, once per call. `max_watches_per_connection` does not bound
        it, because a fresh connection gets a fresh allowance. The spec makes
        the teardown a MUST: "when every subscriber has unsubscribed (or the
        underlying connection drops), the receiver MUST release the watcher"
        (`CreateResourceWatchParams`).

        `started` is the "has ever been observed" flag, and it is exactly the
        test that is wanted: an entry still here with `started` set has a live
        subscriber (an unobserved one is popped by `channel_unobserved`), and
        severing that peer's watch because the CREATOR walked away would break
        a channel the spec keeps alive until its last subscriber goes.
        """
        for channel, watch in list(self._watches.items()):
            if watch.owner is connection and not watch.started:
                del self._watches[channel]
                await self._stop_watch(channel, watch)

    async def _start_watch(self, channel: str, watch: _Watch) -> None:
        assert self.watcher is not None
        with contextlib.suppress(Exception):
            await self.watcher.start(watch.request, lambda c: self._on_changes(channel, c))

    async def _stop_watch(self, channel: str, watch: _Watch) -> None:
        if self.watcher is not None and watch.started:
            with contextlib.suppress(Exception):
                await self.watcher.stop(watch.request)
        if watch.flush is not None:
            watch.flush.cancel()
        await self.sequencer.drop_channel(channel)

    def _on_changes(self, channel: str, changes: Sequence[ResourceChange]) -> None:
        """Buffer a batch, and schedule one action for the whole interval.

        A `git checkout` produces thousands of events. Published individually
        that is thousands of sequence numbers, reducer passes and fan-outs --
        and enough log entries to evict this channel's own replay history.
        """
        watch = self._watches.get(channel)
        if watch is None:
            return
        watch.buffered.extend(changes)
        if watch.flush is None or watch.flush.done():
            watch.flush = asyncio.create_task(self._flush_watch(channel))
            self._background.add(watch.flush)
            watch.flush.add_done_callback(self._background.discard)

    async def _flush_watch(self, channel: str) -> None:
        await asyncio.sleep(DEFAULT_COALESCE_SECONDS)
        watch = self._watches.get(channel)
        if watch is None or not watch.buffered:
            return
        batch, watch.buffered = watch.buffered, []
        await self.sequencer.publish(
            channel,
            {
                "type": "resourceWatch/changed",
                "changes": {"items": [c.to_wire() for c in batch]},
            },
        )

    # ─── session configuration ───────────────────────────────────────────

    def _config_request(self, params: Mapping[str, Any]) -> ConfigRequest:
        values = params.get("config")
        working_directory = params.get("workingDirectory")
        provider = params.get("provider")
        query = params.get("query")
        prop = params.get("property")
        return ConfigRequest(
            provider=provider if isinstance(provider, str) else None,
            working_directory=working_directory if isinstance(working_directory, str) else None,
            values=values if isinstance(values, Mapping) else {},
            property=prop if isinstance(prop, str) else None,
            query=query if isinstance(query, str) else "",
        )

    async def _resolve_session_config(self, params: Mapping[str, Any]) -> dict[str, Any]:
        """What a session can be configured with, given what the client has chosen.

        Called repeatedly while the user sets a session up -- once per session in
        the measured VS Code trace -- and each answer is the **full** current
        property set, not a delta.

        A provider that does not implement `ConfiguresSessions` gets an empty
        schema rather than `MethodNotFound`. "This agent has nothing to
        configure" is a real answer; a refusal is indistinguishable from a
        broken host.
        """
        provider = self._provider_named(params.get("provider"))
        if not isinstance(provider, ConfiguresSessions):
            return {"schema": {"type": "object", "properties": {}}, "values": {}}

        resolved = await provider.resolve_config(self._config_request(params))
        schema: dict[str, Any] = {
            "type": "object",
            "properties": dict(resolved.properties),
        }
        if resolved.required:
            schema["required"] = list(resolved.required)
        return {"schema": schema, "values": dict(resolved.values)}

    async def _session_config_completions(self, params: Mapping[str, Any]) -> dict[str, Any]:
        """Values for a property whose schema declared `enumDynamic`.

        Answered with an empty list rather than refused when the provider does
        not implement it: a client only asks for a property whose schema *it was
        given*, so the honest failure is "no matches", not "no such method".
        """
        request = self._config_request(params)
        if request.property is None:
            raise errors.invalid_params("property is required")
        provider = self._provider_named(params.get("provider"))
        if not isinstance(provider, ConfiguresSessions):
            return {"items": []}

        items = await provider.complete_config(request)
        return {
            "items": [
                {
                    "value": item.value,
                    "label": item.label,
                    **({"description": item.description} if item.description else {}),
                }
                for item in items
            ]
        }

    async def _session_config_for(self, params: Mapping[str, Any]) -> dict[str, Any] | None:
        """`SessionState.config` for a session about to be created.

        The client has already walked `resolveSessionConfig` and passes what it
        settled on as `createSession.config`; the schema comes from the provider
        so the two agree. Values are filtered through the schema for the same
        reason a `root/configChanged` is: a value with no property is invisible
        to a client and unsettable, so publishing it only misleads.
        """
        provider = self._provider_named(params.get("provider"))
        if not isinstance(provider, ConfiguresSessions):
            return None
        resolved = await provider.resolve_config(self._config_request(params))
        if not resolved.properties:
            return None
        chosen = params.get("config")
        values = dict(resolved.values)
        if isinstance(chosen, Mapping):
            values.update(
                {k: v for k, v in chosen.items() if isinstance(k, str) and k in resolved.properties}
            )
        return {
            "schema": {"type": "object", "properties": dict(resolved.properties)},
            "values": values,
        }

    def _validate_session_config(
        self, connection: Connection, channel: str, action: Mapping[str, Any]
    ) -> str | None:
        """Gate `session/configChanged`, which is client-dispatchable.

        Same shape as the root gate, against the schema this session was created
        with. `sessionMutable` is the protocol's own marker for "the user may
        change this after creation"; without it a property is a creation-time
        choice and changing it later would leave the agent configured one way
        and the state saying another.
        """
        state = self.sequencer.state_of(channel)
        config = state.get("config") if isinstance(state, Mapping) else None
        schema = config.get("schema") if isinstance(config, Mapping) else None
        properties = schema.get("properties") if isinstance(schema, Mapping) else None
        if not isinstance(properties, Mapping):
            return "this session publishes no configuration"

        values = action.get("config")
        if not isinstance(values, Mapping):
            return "config must be an object"
        for key, value in values.items():
            prop = properties.get(key) if isinstance(key, str) else None
            if not isinstance(prop, Mapping):
                return f"{key!r} is not a configurable property"
            if not prop.get("sessionMutable"):
                return f"{key!r} cannot be changed after the session is created"
            if not type_matches(prop, value):
                return f"{key!r} does not accept that value"
            if not self.policy.may_set_root_config(connection.info, key, value):
                return f"{key!r} rejected by policy"
        if action.get("replace"):
            return "replacing the whole config is not permitted"
        return None

    def _validate_review(self, channel: str) -> str | None:
        """Reject review on a changeset that never advertised it.

        "Requires the changeset to advertise `capabilities.review`." The reducer
        enforces nothing -- the changeset channel has no validation table at all
        -- so a peer could otherwise mark files reviewed on a changeset whose
        client renders no review UI, and the flag would sit in state unexplained.
        """
        for session in self._sessions.values():
            entry = session.changesets.get(channel)
            if entry is not None:
                return None if entry.reviewable else "this changeset is not reviewable"
        return "unknown changeset"

    #: Which park each tool-resolving action may answer. A `toolCallId` names a
    #: park but NOT what that park is waiting for, and the two requests are
    #: answered by different actions: `SessionToolConfirmationRequest` says
    #: "Respond by dispatching `chat/toolCallConfirmed`",
    #: `SessionToolClientExecutionRequest` says "Execute and report the result
    #: by dispatching `chat/toolCallComplete`". Crossing them woke a provider
    #: with a value that meant something else entirely -- a refusal arriving as
    #: an empty success, or a result read as an approval and the tool then run
    #: for real underneath it.
    _PARK_ANSWERED_BY: Final = {
        "chat/toolCallConfirmed": {"confirm"},
        "chat/toolCallComplete": {"clienttool"},
    }

    def _tool_park_rejection(
        self, connection: Connection, channel: str, action: Mapping[str, Any]
    ) -> str | None:
        """Whether this peer may answer this tool call, in this way.

        Returns a reason, or None to accept. A call the host is not parked on at
        all is left alone here -- `_validate_client_action` already refuses that
        by id, and a server-side tool's confirmation is not ownership-gated.
        """
        action_type = action.get("type")
        request_id = self.pending.id_for_key(action.get("toolCallId"), channel=channel)
        if request_id is None:
            return None
        park = self.pending.get(request_id)
        if park is None:
            return None

        expected = self._PARK_ANSWERED_BY.get(str(action_type), set())
        if park.kind not in expected:
            if park.kind == "clienttool":
                return "that tool call is already running; execute it and report the result"
            return "that tool call is awaiting confirmation, not a result"

        # "The server SHOULD reject this action if the dispatching client does
        # not match the contributor's `clientId`" -- and it is only a rule for a
        # call a CLIENT owns. A server-side tool has no contributor and anyone
        # the policy admits may confirm it.
        if (
            park.kind == "clienttool"
            and park.owner is not None
            and connection.client_id != park.owner
        ):
            return "only the owning client may report that tool call's result"
        return None

    def _validate_truncate(self, channel: str) -> str | None:
        """Refuse to rewind a transcript the agent will still remember.

        The reducer drops the turns, so edit-and-resend *looks* right without a
        provider that can forget them -- and that is the problem. The user is
        shown a conversation being rewound, acts as though it was, and the agent
        answers from a history nobody can see any more.

        Refusing is stricter than the spec, which gates `chat/truncated` on
        nothing. It is the same choice made everywhere else here: a visible
        refusal beats a silent lie, and this is the one gap in the parity list
        where the user is actively misinformed rather than merely underserved.
        """
        session = next((s for s in self._sessions.values() if channel in s.chat_uris), None)
        if session is None:
            return None
        if isinstance(session.agent_session, TruncatesHistory):
            return None
        return "this agent cannot forget part of a conversation"

    def _validate_root_config(
        self, connection: Connection, action: Mapping[str, Any]
    ) -> str | None:
        """Gate `root/configChanged`, which is client-dispatchable.

        The schema is the gate, not the policy: an unknown key, a read-only one,
        or a value of the wrong type is refused whatever policy the embedder
        supplied -- and permissive policies are the norm on loopback. Policy is
        asked last, for the decisions a schema cannot express.
        """
        if self.root_config is None:
            # No schema published, so nothing is configurable. The reducer would
            # drop this anyway; rejecting says so out loud.
            return "this host publishes no configuration"
        config = action.get("config")
        if not isinstance(config, Mapping):
            return "config must be an object"
        for key, value in config.items():
            if not isinstance(key, str):
                return "config keys must be strings"
            rejection = self.root_config.rejection(key, value)
            if rejection is not None:
                return rejection
            if not self.policy.may_set_root_config(connection.info, key, value):
                return f"{key!r} rejected by policy"
        # `replace: true` drops every key the action omits, including ones this
        # peer could not have set. Refused: a merge expresses every legitimate
        # intent, and this does not.
        if action.get("replace"):
            return "replacing the whole config is not permitted"
        return None

    # ─── active clients ──────────────────────────────────────────────────

    async def _retire_active_client(self, connection: Connection) -> None:
        """Drop a departed client from every session that listed it.

        "The server SHOULD automatically dispatch that removal when an active
        client disconnects." Without it the session keeps advertising tools
        nobody can execute, and a provider that picks one waits forever for a
        result from a socket that is gone.
        """
        client_id = connection.client_id
        if not client_id:
            return
        for session in list(self._sessions.values()):
            state = self.sequencer.state_of(session.uri)
            if not isinstance(state, Mapping):
                continue
            clients = state.get("activeClients")
            if not isinstance(clients, list):
                continue
            if not any(
                isinstance(entry, Mapping) and entry.get("clientId") == client_id
                for entry in clients
            ):
                continue
            await self.sequencer.publish(
                session.uri,
                {"type": "session/activeClientRemoved", "clientId": client_id},
            )
            await self._fail_client_tools(session, client_id, "the client disconnected")

    async def _fail_client_tools(self, session: _Session, client_id: str, reason: str) -> None:
        """End every tool call this client was asked to run.

        "When removing a client, the host SHOULD also cancel that client's
        in-flight tool calls ... by dispatching `chat/toolCallComplete` with
        `result.success = false`."

        Without it the call is unanswerable by construction: only its owner may
        report a result and its owner is gone. `session/inputNeeded` goes on
        advertising a `toolClientExecution` nobody can satisfy, the session is
        pinned at `InputNeeded`, the provider waits on a future that will never
        resolve, and the park outlives `disposeSession`.

        The completion is PUBLISHED before the park is resolved, so state and
        the provider agree -- the same ordering rule as every other resolution
        (ADR 0005).
        """
        for park in self.pending.owned_by(client_id):
            if park.kind != "clienttool" or park.channel is None or park.key is None:
                continue
            with contextlib.suppress(Exception):
                await self.sequencer.publish(
                    park.channel,
                    {
                        "type": "chat/toolCallComplete",
                        "turnId": _turn_id_of(self.sequencer.state_of(park.channel)),
                        "toolCallId": park.key,
                        "result": {
                            "success": False,
                            "content": [{"type": "text", "text": reason}],
                            "pastTenseMessage": reason,
                        },
                    },
                )
            if self.pending.resolve(park.id, RequestOutcome(response="decline", payload=None)):
                with contextlib.suppress(Exception):
                    await self._retract_input_needed(session, park.id)

    # ─── working directories ─────────────────────────────────────────────

    def _multiroot(self, provider: AgentProvider) -> Mapping[str, Any] | None:
        """`AgentCapabilities.multipleWorkingDirectories`, or ``None``.

        Absent means "clients MUST NOT mutate a session's or chat's
        working-directory set and MUST NOT set more than one entry" -- a client
        MUST that only the host can actually enforce.
        """
        capability = provider.agent.capabilities.get("multipleWorkingDirectories")
        return capability if isinstance(capability, Mapping) else None

    def _admit_working_directories(
        self, connection: Connection, session: str, params: Mapping[str, Any]
    ) -> list[str]:
        """The directories `createSession` may seed, after capability and policy."""
        requested = [d for d in params.get("workingDirectories") or () if isinstance(d, str)]
        # Anything the resource provider will not serve is dropped HERE, before
        # it can become the session's working directory. Necessary because the
        # ancestor chain of the served root is deliberately walkable
        # (`resources._strict_ancestor`) so a client's directory picker can
        # traverse it -- and a picker that can traverse a directory will let
        # the user CHOOSE it. Accepting one produces a session whose every
        # subsequent resource call is refused: a working directory the host
        # cannot read is worse than no working directory, because the client
        # has no way to tell the difference until each probe fails.
        servable = [d for d in requested if self._inside_the_jail(d)]
        if len(servable) != len(requested):
            # Logged, never silent. A session that quietly loses its working
            # directory is indistinguishable from one that never had it, and
            # the operator is the only person who can tell whether the client
            # asked for the wrong thing or --serve-directory is too narrow.
            _log.warning(
                "dropped %d working director%s outside the served root: %s",
                len(requested) - len(servable),
                "y" if len(requested) - len(servable) == 1 else "ies",
                [d for d in requested if d not in servable],
            )
        requested = servable
        if not requested and self.default_directory is not None:
            # Fall back to the served root. A session with NO working directory
            # is not merely cosmetic: the client builds `folders[0]` -- and
            # therefore `gitRepository` and the `hasGitRepository` context key --
            # from `summary.workingDirectories[0]`, and the changeset picker is
            # gated on that key. With none, the dropdown that switches between
            # changesets does not render at all, so only the first changeset is
            # ever reachable.
            #
            # This is exactly the common path: VS Code sends `file:///` when it
            # has no better answer, the jail correctly refuses it, and the
            # session ends up with nothing.
            requested = [self.default_directory]
        if self._multiroot(self._provider_named(params.get("provider"))) is None:
            # "Servers without that capability treat only the first entry as the
            # session's working directory and ignore the rest." Truncate rather
            # than refuse: the spec makes this the server's defined behaviour,
            # not an error.
            requested = requested[:1]
        return [
            directory
            for directory in requested
            if self.policy.may_grant_working_directory(connection.info, session, directory)
        ]

    def _inside_the_jail(self, uri: str) -> bool:
        """Whether the installed resource provider would actually serve this.

        Feature-detected rather than assumed: a provider that is not a rooted
        filesystem jail (an in-memory store, a git object database) has no
        `root` to compare against and is trusted with whatever it was given.

        Note this is stricter than "resolves": strict ancestors of the root
        resolve, so a directory picker can walk to it, but nothing under them
        is served. A caller asking whether it may READ needs this answer, not
        the resolvable one.
        """
        # A jail that can answer for itself does: the Windows one compares
        # drive-aware and case-insensitively, which the POSIX paths below
        # cannot - `G:\\llm\\proj` would never be "under" `G:\\llm` as a
        # PurePosixPath, and the folder a user picked was silently dropped.
        serves = getattr(self.resources, "serves", None)
        if callable(serves):
            return bool(serves(uri))
        root = getattr(self.resources, "root", None)
        if root is None:
            return True
        try:
            path = PurePosixPath(path_from_file_uri(uri))
        except errors.AhpError:
            # Not a `file:` URI at all. Not this jail's business to judge.
            return True
        return path == PurePosixPath(root) or path.is_relative_to(PurePosixPath(root))

    def _validate_working_directory_action(
        self, connection: Connection, channel: str, action: Mapping[str, Any]
    ) -> str | None:
        """Enforce the multiroot MUSTs the reducers deliberately do not.

        Upstream is explicit that these live here: "the pure reducers apply these
        mutations verbatim ... the `immutablePrimary` guarantee therefore lives
        at the dispatch-validation / host-acceptance layer, not in the reducer".
        """
        multiroot = self._multiroot(self._provider_of_channel(channel))
        if multiroot is None:
            return "this agent does not advertise multipleWorkingDirectories"

        directory = action.get("directory")
        if not isinstance(directory, str):
            return "directory must be a string"

        if action["type"] == "session/workingDirectorySet":
            if not self.policy.may_grant_working_directory(connection.info, channel, directory):
                return "rejected by policy"
            return None

        state = self.sequencer.state_of(channel)
        existing = state.get("workingDirectories") if isinstance(state, Mapping) else None
        is_primary = isinstance(existing, list) and bool(existing) and existing[0] == directory
        # `primaryReplacement` wins over `immutablePrimary` when both are
        # advertised: "clients that recognize this capability MUST allow a
        # targeted replacement even when `immutablePrimary` is also `true`".
        replaceable = bool(multiroot.get("primaryReplacement"))

        if action["type"] == "session/workingDirectoryReplaced":
            replacement = action.get("replacement")
            if not isinstance(replacement, str):
                return "replacement must be a string"
            if is_primary and not replaceable:
                # "Replacing index `0` additionally requires primaryReplacement;
                # clients MUST NOT target an immutable primary."
                return "the primary working directory is not replaceable"
            # A replacement grants tool access to a directory the session did
            # not have, exactly as a set does, so it answers to the same policy.
            if not self.policy.may_grant_working_directory(connection.info, channel, replacement):
                return "rejected by policy"
            return None

        # session/workingDirectoryRemoved. "A host MAY decline to apply the
        # removal (e.g. the immutable primary at index 0), leaving the set
        # unchanged" -- declined loudly, so the client reverts its optimistic
        # prediction instead of showing a directory that is still in use. A
        # replaceable primary is protected too: "the host MUST reject such a
        # removal, leaving the protected slot intact".
        if is_primary and replaceable:
            return "the primary working directory can only be replaced, not removed"
        if is_primary and multiroot.get("immutablePrimary"):
            return "the primary working directory is immutable"
        return None

    def _copy_turns(self, chat_uri: str, turn_id: Any) -> list[Any]:
        """The source chat's turns up to and INCLUDING *turn_id*, deep-copied.

        "The server populates the new session with content from the source
        session up to and including the response of the specified turn"
        (`SessionForkSource`). Deep-copied because the result is "an
        independent copy": sharing the turn objects would make an edit in one
        session appear in the other.

        A `turn_id` that names no turn is `InvalidParams` rather than a silent
        empty copy -- claiming to have branched from a turn that does not exist
        is the same class of lie as returning success for a fork that copied
        nothing. An ABSENT id copies the whole chat, which is what VS Code's
        `/fork` command asks for: it forks at the last turn.

        Note the stored turn's identity field is `id`; only the ACTION that
        creates it carries `turnId`.
        """
        state = self.sequencer.state_of(chat_uri)
        turns = state.get("turns") if isinstance(state, Mapping) else None
        if not isinstance(turns, list):
            return []
        if turn_id is None:
            return copy.deepcopy(turns)
        if not isinstance(turn_id, str):
            raise errors.invalid_params("fork turnId must be a string")
        for index, turn in enumerate(turns):
            if isinstance(turn, Mapping) and turn.get("id") == turn_id:
                return copy.deepcopy(turns[: index + 1])
        raise errors.invalid_params(f"no such turn in the source chat: {turn_id}")

    def _fork_source(
        self, connection: Connection, params: Mapping[str, Any]
    ) -> tuple[_Session, list[Any]] | None:
        """Resolve `createSession.fork`, or ``None`` when it is absent.

        LEGACY: 0.9.0 removed session-level forking from `createSession` in
        favour of chat forking (`createChat` with a `fork` source), so a 0.9.0
        client never sends this. It stays for the 0.7.0 and 0.8.0 peers this
        host still negotiates -- VS Code's `/fork` among them.

        Until this existed the parameter was read by nobody: `createSession`
        accepted a fork, answered success, and produced an EMPTY session. A
        silent no-op is the worst available outcome -- the user watches their
        conversation not come across and has nothing to report.
        """
        fork = params.get("fork")
        if fork is None:
            return None
        if not isinstance(fork, Mapping):
            raise errors.invalid_params("fork must be an object")
        source_uri = fork.get("session")
        if not isinstance(source_uri, str):
            raise errors.invalid_params("fork.session is required")
        source = self._sessions.get(source_uri)
        if source is None:
            raise errors.session_not_found(source_uri)
        # A fork reads the source's whole transcript, so it is exactly as
        # sensitive as observing the channel -- and must be gated the same way,
        # or it becomes a way to read a session the connection may not see.
        if not self.policy.may_see_channel(connection.info, source_uri):
            raise errors.AhpError(-32009, f"Not permitted to observe {source_uri}")
        # `turnIndex` is accepted and ignored: it is not in the type, and a
        # client that sends one should not be failed over it.
        return source, self._copy_turns(source.chat_uri, fork.get("turnId"))

    async def _create_session(self, connection: Connection, params: Mapping[str, Any]) -> None:
        channel = params.get("channel")
        if not isinstance(channel, str):
            raise errors.invalid_params("channel is required")
        # The session URI is CLIENT-CHOSEN and opaque. Never parse or validate
        # its SHAPE: real clients use forms other than ahp-session:/<uuid>.
        #
        # But opaque is not the same as unowned. Checking `_sessions` alone let
        # a client name a channel this host had already registered for
        # something else -- an annotations channel, a chat, a terminal, or
        # `ahp-root://` itself -- and `register_channel` below would then
        # overwrite its state, with `disposeSession` dropping it outright.
        # Two ordinary commands from any admitted client permanently destroyed
        # the connection-level channel, and a client whose `subscribe` comes
        # back without a snapshot throws. Observed for real: a probe left the
        # running host serving session state on `ahp-root://`.
        #
        # So the check is against the sequencer, which knows every channel,
        # rather than against the session map, which knows only some of them.
        if channel in self._sessions or self.sequencer.has_channel(channel):
            raise errors.already_exists(channel)
        verdict = self.policy.may_create_session(connection.info, params)
        if not verdict:
            self._audit("session.refused", connection, channel=channel, allowed=False)
            raise errors.AhpError(
                -32009, policy_mod.reason_or(verdict, "Not permitted to create a session")
            )
        active_client = params.get("activeClient")
        if active_client is not None and (
            not isinstance(active_client, Mapping)
            or not js.strict_equal(active_client.get("clientId"), connection.client_id)
        ):
            # "The `clientId` MUST match the `clientId` the creating client
            # supplied in `initialize`" (`commands-session.ts`), and the
            # reference host rejects the mismatch with InvalidParams
            # (`protocolServerHandler.ts:1188`). Unchecked, client B could
            # claim the active-client role AS client A: tool executions were
            # then addressed to a peer that never volunteered, and disconnect
            # cleanup (`_retire_active_client`) fired for the wrong one. An
            # entry with no clientId at all is rejected too, mirroring the
            # reference -- present-but-unaddressable is malformed, not
            # ignorable.
            raise errors.invalid_params(
                "createSession.activeClient.clientId must match the connection's clientId"
            )

        # Absence, not falsiness (invariant 5): `provider: ""` is a present
        # value naming a provider no agent answers to, and `or` silently
        # swapped in the default -- a session served by an agent the client
        # never asked for. Only a missing key (or explicit null, which the
        # optional-string schema reads as absent) selects the default.
        requested_provider = params.get("provider")
        provider_id = (
            requested_provider if requested_provider is not None else self.provider.agent.provider
        )
        if provider_id not in self.providers:
            # Checked rather than copied through. Unvalidated, a client could
            # name any string and the host would publish the session under it
            # while actually serving it with the default provider -- and the
            # session list, which groups by provider, would show rows under an
            # agent that does not exist.
            raise errors.provider_not_found(str(provider_id))
        forked = self._fork_source(connection, params)
        if forked is None:
            working_directories = self._admit_working_directories(connection, channel, params)
        else:
            # "Ignored for forked sessions -- a fork inherits its working
            # directories from the source session." An OVERRIDE, not a
            # fallback: VS Code computes and sends `workingDirectories` on the
            # fork call anyway, so honouring the client's would quietly diverge
            # the fork from its source.
            source_state = self.sequencer.state_of(forked[0].uri)
            inherited = (
                source_state.get("workingDirectories")
                if isinstance(source_state, Mapping)
                else None
            )
            working_directories = list(inherited) if isinstance(inherited, list) else []
        # Resolved BEFORE the channel is registered, which is forced by the
        # protocol rather than chosen: `session/configChanged` carries values
        # only, and the reducer no-ops entirely when `SessionState.config` is
        # absent. A schema that is not in the initial state can never be added.
        session_config = await self._session_config_for(params)
        if forked is not None:
            # The client sends no `config` on a fork, so a configurable
            # provider silently lost the user's answers on every one. Inherited
            # from the source, which is what "an independent copy" means for
            # everything else about the session.
            source_state = self.sequencer.state_of(forked[0].uri)
            if isinstance(source_state, Mapping) and "config" in source_state:
                session_config = copy.deepcopy(source_state["config"])
        chat_uri = f"ahp-chat:/{uuid.uuid4()}"
        created_at = now_iso()
        session = _Session(
            uri=channel,
            chat_uri=chat_uri,
            provider_id=provider_id,
            # A fork carries its source's title. Read from the PUBLISHED
            # state, not from the source's `_Session.title`: that field is only
            # what the session was created with, and `session/titleChanged`
            # updates the channel without writing back to it. The client labels
            # a fork `forkedTitle || chatModel?.title || "Forked Session"`, so
            # a stale read here makes every fork of a renamed session read
            # "New Session".
            title=_published_title(self.sequencer.state_of(forked[0].uri))
            if forked is not None
            else "New Session",
            created_at=created_at,
        )
        session.chat_uris.add(chat_uri)
        token = params.get("progressToken")
        session.publisher = _Publisher(self, session, token if isinstance(token, str) else None)
        self._sessions[channel] = session

        session_state: dict[str, Any] = {
            "provider": provider_id,
            "title": session.title,
            "status": _STATUS_IDLE,
            "lifecycle": "creating",
            # `createSession.activeClient` is how a client publishes the tools
            # it can execute on the agent's behalf. VS Code sends its ENTIRE
            # tool set here, with full input schemas (experiments.md E12e), and
            # a host that drops it is throwing away the one tool surface that
            # needs no filesystem API at all.
            "activeClients": _active_clients(params.get("activeClient")),
            "chats": [],
        }
        if working_directories:
            # Seeded rather than left absent: a chat subset "MUST already be in
            # the session's `workingDirectories`", and that check is unanswerable
            # against a set the host never recorded.
            session_state["workingDirectories"] = working_directories
        if session_config is not None:
            session_state["config"] = session_config
        await self.sequencer.register_channel(channel, session_state, "session")
        # Before the response goes out, so a policy that refuses unowned
        # channels never has a window in which the peer's own session is
        # unreachable.
        self._channel_created(connection, channel)
        await self.sequencer.register_channel(
            chat_uri,
            {
                "resource": chat_uri,
                # A CHAT's name, not the session's. `ChatState` inlines every
                # field of the catalogue entry, so the two have to agree -- and
                # `session.title` here made the default chat's tab read "New
                # Session", which is the name of the thing that contains it.
                "title": _DEFAULT_CHAT_TITLE,
                "status": _STATUS_IDLE,
                "modifiedAt": created_at,
                # THE point of a fork. The client reads the transcript off the
                # chat channel, so an empty list here is a fork that visibly
                # lost the conversation while every command reported success.
                "turns": forked[1] if forked is not None else [],
            },
            "chat",
        )
        self._channel_created(connection, chat_uri, session=channel)
        # "Each session owns at most one annotations channel. The channel URI is
        # derived from the session URI by appending `/annotations`."
        #
        # Registered even though this host produces no annotations, because a
        # client subscribing to an unregistered channel gets a result with no
        # `snapshot`, and VS Code's client THROWS on that rather than tolerating
        # it (`remoteAgentHostProtocolClient.ts:863-869`). It subscribes
        # unconditionally: the committed reconnect capture in
        # `tests/integration/fixtures/vscode-1.131-client-requests.json` carries
        # the annotations URI in `subscriptions`, so before this the failure
        # happened on every single connect.
        await self.sequencer.register_channel(
            session.annotations_uri, {"annotations": []}, "annotations"
        )
        self._channel_created(connection, session.annotations_uri, session=channel)

        # Bring-up runs after the response so the client can subscribe first.
        # Hold a reference: a bare create_task can be garbage-collected mid-flight.
        self._audit("session.created", connection, channel=channel)
        task = asyncio.create_task(self._bring_up(session, params, working_directories, forked))
        self._background.add(task)
        task.add_done_callback(self._background.discard)
        return

    async def _bring_up(
        self,
        session: _Session,
        params: Mapping[str, Any],
        working_directories: Sequence[str],
        forked: tuple[_Session, list[Any]] | None = None,
    ) -> None:
        try:
            active_client = _active_clients(params.get("activeClient"))
            context = AgentSessionContext(
                publisher=session.publisher,
                session_uri=session.uri,
                chat_uri=session.chat_uri,
                provider_id=session.provider_id,
                # The ADMITTED list, not `params` again. Reading params here
                # bypassed both the capability truncation and the policy gate,
                # so the published SessionState and the provider's own context
                # disagreed about what the session may touch -- and the
                # provider got the wider of the two.
                working_directories=tuple(working_directories),
                # The provider is TOLD about the fork. Without this the host
                # publishes N turns of history the agent has never seen, and
                # the first reply after a fork answers with no context -- the
                # published state and the agent silently disagree.
                fork=(
                    ForkedFrom(session_uri=forked[0].uri, turns=tuple(forked[1]))
                    if forked is not None
                    else None
                ),
                config=params.get("config") or {},
                active_client_id=active_client[0]["clientId"] if active_client else None,
                client_tools=tuple(active_client[0]["tools"]) if active_client else (),
            )
            session.agent_session = await self._provider_of(session).create_session(context)
        except Exception as exc:
            await self.sequencer.publish(
                session.uri,
                {
                    "type": "session/creationFailed",
                    "error": {"message": f"{type(exc).__name__}: {exc}"},
                },
            )
            return
        await self._announce(session)

    async def _announce(self, session: _Session) -> None:
        """Tell root about a session whose agent is up, and make it ready."""
        summary = self._full_summary(session)
        # Remember what root was told, so the first `root/sessionSummaryChanged`
        # carries a real difference rather than restating the whole summary.
        session.published_summary = self._project_summary(session)
        await self.sequencer.notify(
            ROOT_URI, "root/sessionAdded", {"channel": ROOT_URI, "summary": summary}
        )
        await self.sequencer.publish(
            ROOT_URI,
            {"type": "root/activeSessionsChanged", "activeSessions": len(self._sessions)},
        )
        chat_summary = {
            "resource": session.chat_uri,
            # Not `session.title`. A chat tab reading "New Session" is a tab
            # labelled with the name of the thing that contains it, and
            # `ChatSummary.title` is REQUIRED so it cannot simply be omitted.
            "title": _DEFAULT_CHAT_TITLE,
            "status": _STATUS_IDLE,
            "modifiedAt": session.created_at,
        }
        session.published_chats[session.chat_uri] = {
            "title": _DEFAULT_CHAT_TITLE,
            "status": _STATUS_IDLE,
            "modifiedAt": session.created_at,
        }
        await self.sequencer.publish(
            session.uri, {"type": "session/chatAdded", "summary": chat_summary}
        )
        await self.sequencer.publish(
            session.uri,
            {"type": "session/defaultChatChanged", "defaultChat": session.chat_uri},
        )
        # Customizations and tools are session STATE, so they are published as
        # actions before `ready` -- a client subscribing on ready then sees them
        # in its snapshot rather than racing for them.
        if isinstance(session.agent_session, DescribesSession):
            described = await session.agent_session.describe()
            if described.customizations:
                await self.sequencer.publish(
                    session.uri,
                    {
                        "type": "session/customizationsChanged",
                        "customizations": list(described.customizations),
                    },
                )
            if described.server_tools:
                await self.sequencer.publish(
                    session.uri,
                    {"type": "session/serverToolsChanged", "tools": list(described.server_tools)},
                )

        await self.sequencer.publish(session.uri, {"type": "session/ready"})
        # Bring-up published customizations, tools and the chat catalogue, any of
        # which can move a summary field.
        await self._mirror_summary(session)
        # Persisted unconditionally, not just when the summary moved: a session
        # that exists is a session a client can come back to, and `_mirror_summary`
        # deliberately emits nothing when nothing changed -- which at bring-up is
        # exactly the case.
        await self._persist(session)

    async def _dispose_session(self, connection: Connection, params: Mapping[str, Any]) -> None:
        """Tear the session down, drop its channels, and tell the root channel.

        The spec: "the server tears down the session backend, drops associated
        subscriptions, and broadcasts `root/sessionRemoved`".
        """
        channel = params.get("channel")
        if not isinstance(channel, str):
            raise errors.invalid_params("channel is required")
        session = self._sessions.get(channel)
        if session is None:
            raise errors.session_not_found(channel)
        if not self.policy.may_see_channel(connection.info, channel):
            raise errors.AhpError(-32009, f"Not permitted to dispose {channel}")
        await self._teardown(session)

    async def _teardown(self, session: _Session) -> None:
        channel = session.uri
        for task in session.running():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        # After the tasks are down and before the channels go: a client
        # subscribed to a chat that was mid-turn otherwise sees the stream stop
        # with no terminal action on it, and goes on waiting for one. Disposing
        # a session is not a reason to strand every turn stream watching it.
        for mid_turn in [session.chat_uri, *sorted(session.chat_uris - {session.chat_uri})]:
            await self._end_stranded_turn(session, mid_turn)
        if session.agent_session is not None:
            # Deleted, not merely closed: `aclose` alone is also what a
            # shutdown does, and an agent living elsewhere too must tell them
            # apart.
            if isinstance(session.agent_session, DisposesSessions):
                try:
                    await session.agent_session.disposed()
                except Exception:
                    _log.exception("disposed() failed for %s", channel)
            await session.agent_session.aclose()

        del self._sessions[channel]
        for changeset_uri in session.changesets:
            # SAID, then dropped -- the same pre-teardown pattern as
            # `_end_stranded_turn` two loops up. "Existing subscriptions
            # receive `changeset/cleared` and the server unsubscribes them"
            # (changesets guide, lifecycle step 5): the reducer empties
            # `files` on it, so it is the terminal action a subscriber renders
            # as "this change set is gone" rather than a stream that silently
            # stops mid-list.
            await self.sequencer.publish(changeset_uri, {"type": "changeset/cleared"})
            await self.sequencer.drop_channel(changeset_uri)
            self._channel_dropped(changeset_uri)
        for owned_chat in session.chat_uris:
            await self.sequencer.drop_channel(owned_chat)
            self._channel_dropped(owned_chat)
        await self.sequencer.drop_channel(session.annotations_uri)
        self._channel_dropped(session.annotations_uri)
        await self.sequencer.drop_channel(channel)
        self._channel_dropped(channel)

        with contextlib.suppress(Exception):
            await self.store.delete(channel)
        await self.sequencer.notify(
            ROOT_URI, "root/sessionRemoved", {"channel": ROOT_URI, "session": channel}
        )
        await self.sequencer.publish(
            ROOT_URI,
            {"type": "root/activeSessionsChanged", "activeSessions": len(self._sessions)},
        )
        return

    async def _reconnect(self, connection: Connection, params: Mapping[str, Any]) -> dict[str, Any]:
        # `reconnect` establishes the connection when it arrives first. There is
        # no version to negotiate -- it carries no `protocolVersions` -- so we
        # adopt our most-preferred one. It also carries no credential: it
        # resumes on a client-asserted `clientId` alone, which is exactly why
        # admission is the Policy's decision and not this method's.
        first_request = not connection.initialized
        if first_request:
            connection.client_id = _connection_identity(params.get("clientId"))
            connection.protocol_version = self.supported_versions[0]
            if not self.policy.authorize_connection(connection.info):
                self._audit("connection.refused", connection, allowed=False)
                raise errors.AhpError(-32009, "Connection refused by policy")

        # An id this host has never admitted gets `NotFound`, which is the
        # client's cue to start over: it catches exactly this code and issues a
        # fresh `initialize` ("Server forgot client X; initializing a fresh
        # connection"). Resuming a stranger instead looked successful and was
        # not -- `initialize` is the ONLY place the client assigns
        # `defaultDirectory`, so a host that always accepts a reconnect leaves
        # every client permanently browsing from `/`, with no way to discover
        # otherwise.
        #
        # Checked BEFORE the connection is marked initialized. A handshake is
        # `initialize` or a *successful* reconnect; marking first meant the
        # refusal left the connection admitted anyway -- every later command
        # sailed past the initialize-first gate on a connection that never
        # negotiated a version, and the audit log recorded a resumption that
        # was refused.
        if connection.client_id not in self._known_clients:
            raise errors.AhpError(-32008, "unknown clientId; initialize instead")
        if first_request:
            connection.initialized = True
            self._audit("connection.resumed", connection)

        # De-duplicated before anything else looks at it. `subscriptions` is
        # peer-supplied and the schema does not forbid repeats; `Sequencer.replay`
        # dedupes too, but `refused` and the policy calls are computed here.
        requested = list(
            dict.fromkeys(uri for uri in params.get("subscriptions") or [] if isinstance(uri, str))
        )
        allowed = [uri for uri in requested if self.policy.may_see_channel(connection.info, uri)]
        # Refused channels are NOT silently dropped. `missing` is documented as
        # "subscriptions that cannot be resumed -- disposed sessions, or
        # resources the client may no longer observe", and a client uses it to
        # drop them from its local set. Filtering them out before `replay` sees
        # them tells the client nothing, so it keeps asking forever.
        refused = [uri for uri in requested if uri not in allowed]

        last_seen = params.get("lastSeenServerSeq")
        # `replay` registers this connection for what it resumes, and for
        # nothing else: a channel it reports `missing` must not stay subscribed
        # here, or the host says "drop this" and keeps delivering it -- to a URI
        # that a *different* client may later create a session on.
        result = await self.sequencer.replay(
            last_seen if isinstance(last_seen, int) else 0, allowed, subscriber=connection
        )
        # A telemetry channel is live but carries no state, so `replay` cannot
        # resume it and calls it missing. It is not gone: it exists for the life
        # of the host, this connection may see it, and `reconnect` returns no
        # `telemetry` map for the client to re-read -- so a client that dropped
        # it on the host's say-so could never get back to it. Re-registered and
        # struck from `missing` instead.
        advertised = set(self.telemetry.values())
        resumed_stateless = [uri for uri in allowed if uri in advertised]
        for uri in resumed_stateless:
            await self.sequencer.subscribe(connection, uri)
        # Refused channels join `missing` on the REPLAY arm only.
        # `ReconnectSnapshotResult` is `{type, snapshots}` -- `missing` exists
        # solely on `ReconnectReplayResult` -- and on the snapshot arm absence
        # from `snapshots` is already the drop signal a client acts on (the
        # reference client's snapshot branch reads nothing else,
        # `remoteAgentHostProtocolClient.ts:_applyReconnectResult`).
        if result.get("type") == "replay":
            missing = [uri for uri in result.get("missing", []) if uri not in resumed_stateless]
            result["missing"] = [*missing, *refused]
        return result

    # ─── client-dispatched actions ───────────────────────────────────────

    async def _dispatch_action(self, connection: Connection, params: Mapping[str, Any]) -> None:
        channel = params.get("channel")
        action = params.get("action")
        if not isinstance(channel, str) or not isinstance(action, Mapping):
            return
        if not self.sequencer.has_channel(channel):
            # Unknown channel: silently ignore, no echo. Specified asymmetry.
            return

        # `dispatchAction` carries only {channel, clientSeq, action} -- no
        # clientId -- so the host stamps origin from the connection. Getting
        # this wrong silently breaks optimistic reconciliation for every client
        # except the originator.
        client_seq = params.get("clientSeq")
        origin = {
            "clientId": connection.client_id,
            "clientSeq": client_seq if isinstance(client_seq, int) else 0,
        }

        rejection = self._validate_client_action(connection, channel, action)
        if rejection is not None:
            self._audit(
                "action.rejected",
                connection,
                channel=channel,
                allowed=False,
                reason=rejection,
                detail={"action": action.get("type")},
            )
            await self.sequencer.publish(channel, action, origin=origin, rejection_reason=rejection)
            return
        if action.get("type") in _TOOL_RESOLVING_ACTIONS:
            # Who approved a tool call is the single event an operator is most
            # likely to be asked about, and the protocol's own validation table
            # conditions approval on the call's STATUS, never on identity.
            self._audit(
                "toolcall.resolved",
                connection,
                channel=channel,
                detail={
                    "action": action.get("type"),
                    "toolCallId": action.get("toolCallId"),
                    "approved": action.get("approved"),
                },
            )

        await self.sequencer.publish(channel, action, origin=origin)
        await self._react(channel, action, connection)
        session = self._session_for(channel)
        if session is not None:
            await self._mirror_summary(session)

    def _validate_client_action(
        self, connection: Connection, channel: str, action: Mapping[str, Any]
    ) -> str | None:
        """Return a rejection reason, or ``None`` to accept.

        Invalid client actions MUST be echoed back with `rejectionReason` so the
        client can revert its optimistic prediction -- dropping them silently
        leaves that prediction applied forever.
        """
        action_type = action.get("type")
        if not isinstance(action_type, str):
            return "action has no type"
        # Protocol invariant, checked unconditionally and before any policy:
        # a client may only originate actions marked client-dispatchable.
        if not IS_CLIENT_DISPATCHABLE.get(action_type, False):
            return f"{action_type} is not client-dispatchable"
        if not self.policy.may_dispatch(connection.info, channel, action):
            return "rejected by policy"

        # Shape before meaning: a tool-call action missing a REQUIRED field is
        # sequenced and fanned out, then applied by no reducer -- while the host
        # resolves the parked request by `toolCallId` and runs the tool anyway.
        # Every client ends up with a call that state says was never answered.
        malformed = tool_call_dispatch_rejection(action)
        if malformed is not None:
            return malformed

        if self.sequencer.reducer_of(channel) == "terminal":
            state = self.sequencer.state_of(channel)
            claim = claim_from_wire(state.get("claim") if isinstance(state, Mapping) else None)
            return terminal_dispatch_rejection(
                action,
                claim=claim,
                client_id=connection.client_id,
                gated=self._claim_gated,
            )

        if action_type == "root/configChanged":
            return self._validate_root_config(connection, action)

        if action_type == "changeset/filesReviewChanged":
            return self._validate_review(channel)

        if action_type == "chat/truncated":
            return self._validate_truncate(channel)

        if action_type == "session/configChanged":
            return self._validate_session_config(connection, channel, action)

        if action_type == "session/titleChanged" and not isinstance(action.get("title"), str):
            # `SessionTitleChangedAction` declares `"required": ["type",
            # "title"]` with `"title": {"type": "string"}`
            # (actions.schema.json), and the reducer is `assign(state, "title",
            # get(action, "title"))` -- so an omitted title DELETES the key and
            # `null`/`7` are written straight through. Either way `SessionState`
            # and every `SessionSummary` projected from it lose a field their
            # own schemas require, and the cached catalogue keeps the old title
            # because `changes` has no way to say a field is gone. Rejected at
            # the boundary instead, which is also what tells the renaming
            # client its optimistic rename did not take.
            return "session/titleChanged requires a string title"

        if action_type in _WORKING_DIRECTORY_ACTIONS:
            return self._validate_working_directory_action(connection, channel, action)

        if action_type == "session/customizationToggled":
            return _enablement_rejection(action.get("enablement"))

        # Asked of the bound reducer, never of the URI's scheme (invariant 15).
        # Classifying by scheme happens to work for chat URIs this host mints and
        # breaks the moment a client names one, which `createChat` will allow.
        state = self.sequencer.state_of(channel)
        if self.sequencer.reducer_of(channel) == "chat" and isinstance(state, Mapping):
            if action_type == "chat/turnCancelled":
                # The id has to MATCH, not merely exist. `_end_turn` no-ops
                # unless `turnId` names the active turn, while `_react` kills
                # the running task unconditionally -- so an ordinary late
                # cancel, or the omitted-`turnId` cancel the Python client
                # sends, aborted the in-flight turn and left the host's own
                # state saying it was still running. Permanently: every later
                # `chat/turnStarted` is then rejected as "a turn is already
                # active", and no action can clear an `activeTurn` whose id
                # nothing knows. `turnId` is required by the schema.
                active = state.get("activeTurn")
                active_id = active.get("id") if isinstance(active, Mapping) else None
                if active_id is None:
                    return "no active turn to cancel"
                if not js.strict_equal(active_id, action.get("turnId")):
                    return "turnId does not name the active turn"
            if action_type == "chat/turnStarted" and state.get("activeTurn") is not None:
                return "a turn is already active"
            if action_type == "chat/turnResume":
                # This host never publishes a `resumable` error part, so there
                # is no turn a resume could reopen -- the reducer would no-op.
                # Rejected so the client drops its optimistic reopening.
                return "this host does not resume failed turns"
            if (
                action_type in _TOOL_RESOLVING_ACTIONS
                and self.pending.id_for_key(action.get("toolCallId"), channel=channel) is None
            ):
                # A tool call the host is not waiting on. Rejected rather than
                # ignored, so the client reverts its optimistic state instead of
                # rendering a call as answered forever.
                return "no tool call awaiting that id"
            if action_type in _TOOL_RESOLVING_ACTIONS:
                mismatch = self._tool_park_rejection(connection, channel, action)
                if mismatch is not None:
                    return mismatch
            if action_type in _INPUT_ACTIONS and not self.pending.is_open(
                action.get("requestId"), channel=channel
            ):
                # "Servers SHOULD reject client-dispatched input actions when no
                # unresolved input-request part has the matching requestId."
                # The reducers deliberately do not check this -- upstream states
                # the rule in prose and leaves it to the host -- and without it a
                # peer can answer a request that was never asked, or answer one
                # twice and resolve a future the second time round.
                #
                # `channel=` because "no unresolved part" is PER-CHANNEL: the
                # reducer searches only the dispatched channel's active turn,
                # and ids are minted globally. Without it, answering chat A's
                # question by dispatching to chat B resolved A's future while
                # reading the answers out of B's state -- so the answers went
                # nowhere, A's transcript still said unanswered, and A stayed
                # pinned in `InputNeeded` until the session was disposed.
                return "no open input request with that id"
        return None

    async def _react(
        self, channel: str, action: Mapping[str, Any], connection: Connection | None = None
    ) -> None:
        """Side effects a client action triggers on the agent."""
        action_type = action.get("type")
        if action_type == "session/customizationToggled":
            await self._react_to_toggle(channel, action)
            return
        if action_type == "session/configChanged":
            await self._react_to_config(channel, action)
            return
        if action_type == "session/isArchivedChanged":
            await self._react_to_archive(channel, action)
            return
        if action_type in _MCP_LIFECYCLE_ACTIONS:
            await self._react_to_mcp(channel, action)
            return
        if action_type in _ACTIVE_CLIENT_ACTIONS and connection is not None:
            await self._react_to_active_client(connection, channel, action)
            return
        if action_type in ("terminal/input", "terminal/resized"):
            # Before the session lookup: a terminal channel belongs to no
            # session's chat set, so anything after that lookup is unreachable
            # for it.
            await self._forward_to_terminal(channel, action)
            return
        if action_type in _CATALOGUE_TERMINAL_ACTIONS and channel in self._live_terminals:
            # `TerminalInfo` carries `title` and `claim`, and both of these
            # actions change one of them -- but the catalogue was republished
            # only on create/exit/dispose, so `RootState.terminals` reported the
            # OLD owner. That is the field a client reads to decide whether to
            # offer an input box, so a handed-over terminal stayed typeable in
            # the wrong window and unusable in the right one.
            #
            # Membership-checked because both actions are client-dispatchable at
            # any channel: aimed at a chat, they no-op in its reducer and must
            # not drag the root channel along with them.
            await self._publish_terminal_catalogue()
            return
        session = next((s for s in self._sessions.values() if channel in s.chat_uris), None)
        if session is None:
            return
        if action_type == "chat/turnStarted":
            await self._start_turn(session, channel, action)
        elif action_type == "chat/pendingMessageSet":
            await self._drain_queue(session, channel)
        elif action_type == "chat/truncated":
            await self._react_to_truncate(session, channel, action)
        elif action_type == "chat/turnCancelled":
            await self._cancel_turn(session, channel, "client cancelled")
        elif action_type == "chat/inputCompleted":
            # Resolved AFTER the reducer has applied the action (ADR 0005), so
            # the provider and the state every client can see never disagree
            # about whether the request was answered.
            request_id = action.get("requestId")
            if isinstance(request_id, str):
                response = action.get("response")
                resolved = self.pending.resolve(
                    request_id,
                    RequestOutcome(
                        response=response if isinstance(response, str) else "cancel",
                        # Read back out of state rather than off the action. The
                        # reducer has already overlaid the action's `answers`
                        # onto the drafts clients synchronised while the request
                        # was open -- "a user can answer one question on client A
                        # and another on client B" -- so the part now holds the
                        # merged result and re-deriving it here could only get it
                        # wrong.
                        payload=_answers_of(self.sequencer.state_of(channel), request_id),
                    ),
                )
                if resolved:
                    await self._retract_input_needed(session, request_id)
        elif action_type in _TOOL_RESOLVING_ACTIONS:
            # Scoped to the channel, like every other park lookup. Validation
            # has already refused a kind mismatch and a foreign owner, so
            # reaching here means this peer may answer this park in this way.
            request_id = self.pending.id_for_key(action.get("toolCallId"), channel=channel)
            if request_id is not None:
                if action_type == "chat/toolCallConfirmed":
                    approved = action.get("approved")
                    outcome = RequestOutcome(
                        response="accept" if approved is not False else "decline",
                        # `editedToolInput` is the client's rewrite of the
                        # parameters. Carried through so the provider runs what
                        # was approved rather than what it proposed.
                        payload={"toolInput": action["editedToolInput"]}
                        if "editedToolInput" in action
                        else {},
                    )
                else:
                    # A client-run tool reports success on the RESULT, and
                    # `ChatToolCallDeniedAction` says the owner "MUST dispatch
                    # this if it does not recognize the tool or cannot execute
                    # it". Reading every completion as an accept turned that
                    # refusal into a tool that ran and returned nothing, which
                    # the agent then reported as a result.
                    result = action.get("result")
                    failed = isinstance(result, Mapping) and result.get("success") is False
                    outcome = RequestOutcome(
                        response="decline" if failed else "accept", payload=result
                    )
                if self.pending.resolve(request_id, outcome):
                    await self._retract_input_needed(session, request_id)

    def _spawn(self, coroutine: Coroutine[Any, Any, None]) -> None:
        """Run something to completion outside the caller's cancellation scope.

        A bare `create_task` can be garbage-collected mid-flight, so the
        reference is held until it finishes.
        """
        task = asyncio.create_task(coroutine)
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    async def _retract_input_needed(self, session: _Session, request_id: str) -> None:
        await self.sequencer.publish(
            session.uri, {"type": "session/inputNeededRemoved", "id": request_id}
        )
        await self._mirror_summary(session)

    async def _retract_all(self, session: _Session, request_ids: Sequence[str]) -> None:
        for request_id in request_ids:
            with contextlib.suppress(Exception):
                await self._retract_input_needed(session, request_id)

    async def _forward_to_terminal(self, channel: str, action: Mapping[str, Any]) -> None:
        """Keystrokes and resizes go to the process, not into `content`.

        Echoing input into the buffer here would double it against the
        `terminal/data` the pty sends back -- which is exactly why the reducer
        treats `terminal/input` as a no-op.
        """
        terminal = self._live_terminals.get(channel)
        if terminal is None or terminal.process is None:
            return
        with contextlib.suppress(Exception):
            if action.get("type") == "terminal/input":
                data = action.get("data")
                if isinstance(data, str):
                    await terminal.process.write(data.encode())
            else:
                cols, rows = action.get("cols"), action.get("rows")
                if isinstance(cols, int) and isinstance(rows, int):
                    await terminal.process.resize(cols, rows)

    async def _react_to_active_client(
        self, connection: Connection, channel: str, action: Mapping[str, Any]
    ) -> None:
        """Expand any plugins the client just published.

        Done here rather than at `createSession` because a client re-publishes
        as its workspace changes -- "this is also how a client updates its
        published tools or customizations: re-dispatch with the full, updated
        entry" -- and `nonce` exists precisely so a host can tell one
        publication from the next.

        Expansion runs detached: it makes requests back to the client, and
        awaiting them inline would block this connection's notification handler
        on a response that can only arrive through the same read loop.
        """
        if channel not in self._sessions:
            return
        if action.get("type") == "session/activeClientRemoved":
            # The same SHOULD as on disconnect: a client that leaves the session
            # is as unable to answer as one whose socket dropped.
            removed = action.get("clientId")
            if isinstance(removed, str):
                await self._fail_client_tools(
                    self._sessions[channel], removed, "the client left the session"
                )
            return
        entry = action.get("activeClient")
        published = entry.get("customizations") if isinstance(entry, Mapping) else None
        for plugin in published if isinstance(published, list) else ():
            if not isinstance(plugin, Mapping) or plugin.get("type") != "plugin":
                continue
            if self._already_expanded(channel, plugin):
                continue
            self._spawn(self._expand_quietly(connection, channel, plugin))

    def _already_expanded(self, session_uri: str, plugin: Mapping[str, Any]) -> bool:
        """Whether this exact publication has been expanded before.

        `nonce` is "an opaque version token used by the host to detect changes",
        so a republication with an unchanged nonce is the same plugin and
        re-reading every child of it would be a round trip per file for nothing.
        """
        session = self._sessions.get(session_uri)
        if session is None:
            return False
        identity = (plugin.get("id"), plugin.get("nonce"))
        if identity in session.expanded_plugins:
            return True
        session.expanded_plugins.add(identity)
        return False

    async def _expand_quietly(
        self, connection: Connection, session_uri: str, plugin: Mapping[str, Any]
    ) -> None:
        """A client that cannot serve its own plugin is not an error here.

        It published something it could not back up, which is its problem; the
        session carries on without those children rather than failing anything.
        """
        try:
            await self.expand_client_plugin(connection, session_uri, plugin)
        except Exception:
            _log.debug("could not expand client plugin %s", plugin.get("id"))

    async def _react_to_mcp(self, channel: str, action: Mapping[str, Any]) -> None:
        """Route a client's start/stop request to whoever owns the runtime.

        The reducer already moves the customization to `starting`/`stopped`, so
        every client agrees on the intent without this. What it cannot do is
        make the server actually start -- the provider owns the process, and
        this host spawns nothing (see `ManagesMcpServers`).
        """
        session = self._sessions.get(channel)
        if session is None or not isinstance(session.agent_session, ManagesMcpServers):
            return
        customization_id = action.get("id")
        if not isinstance(customization_id, str):
            return
        starting = action.get("type") == "session/mcpServerStartRequested"
        with contextlib.suppress(Exception):
            if starting:
                await session.agent_session.start_mcp_server(customization_id)
            else:
                await session.agent_session.stop_mcp_server(customization_id)

    async def _react_to_toggle(self, channel: str, action: Mapping[str, Any]) -> None:
        """Tell the provider a customization was switched on or off.

        The reducer has already applied the decisions in state, so clients
        agree without this. What they cannot do is stop the *agent* using a
        disabled skill -- only the provider can, and only if it is told. The
        provider hears the *effective* value, which the spec defines as the
        most specific decision: ``enablement?.[0]?.enabled ?? true``.
        """
        session = self._sessions.get(channel)
        if session is None or not isinstance(session.agent_session, HandlesCustomizations):
            return
        customization_id = action.get("id")
        if not isinstance(customization_id, str):
            return
        with contextlib.suppress(Exception):
            await session.agent_session.customization_toggled(
                customization_id, _effective_enabled(action.get("enablement"))
            )

    def _terminal_command(self, action: Mapping[str, Any]) -> str | None:
        """The command a `!`-prefixed message asks for, if this host runs them.

        "Prefix that the host recognizes at the start of a user `Message.text`
        as a shorthand for executing the remainder as a terminal command."
        Advertised in `initialize` behind a real backend -- and until now
        advertised and not implemented, which is worse than absent: the input
        box promises a shortcut that silently goes to the agent instead.
        """
        if TERMINAL_COMMAND_PREFIX not in self._advertised_prefix():
            return None
        message = action.get("message")
        text = message.get("text") if isinstance(message, Mapping) else None
        if not isinstance(text, str) or not text.startswith(TERMINAL_COMMAND_PREFIX):
            return None
        command = text[len(TERMINAL_COMMAND_PREFIX) :].strip()
        return command or None

    def _advertised_prefix(self) -> str:
        """`!` when a backend is installed **and actually runs commands**.

        Read from the same condition `initialize` publishes, so the two cannot
        say different things.

        The class check alone was not enough. A host may install a backend that
        deliberately executes nothing — to satisfy a client that opens a terminal
        unconditionally, and explain itself in the panel instead of refusing and
        producing an error toast on every focus. Such a backend must NOT advertise
        `!`, for exactly the reason the refusing default does not: it would turn a
        working input into a dead end.

        Feature-detected via `runs_commands`, defaulting True, so every existing
        backend is unaffected and only one that opts out is treated as inert.
        """
        if self.terminals.__class__ is RefusingTerminalBackend:
            return ""
        if getattr(self.terminals, "runs_commands", True) is False:
            return ""
        return TERMINAL_COMMAND_PREFIX

    async def _run_terminal_command(
        self, session: _Session, channel: str, action: Mapping[str, Any], command: str
    ) -> None:
        """Execute a `!command` and report it as a tool call on the chat.

        A tool call rather than a bare text part, for two reasons: it is what
        the thing IS -- something ran, with an input and an output -- and it is
        the shape that carries `_meta.ptyTerminal`, which is what makes a client
        render a terminal instead of a paragraph of escape sequences.

        The terminal is created and disposed here rather than being left on the
        session. `!ls` is a one-shot; a peer that wants a terminal it can type
        into calls `createTerminal`.
        """
        turn_id = action.get("turnId")
        if not isinstance(turn_id, str):
            return
        sink = ActionTurnSink(
            self.sequencer,
            channel,
            turn_id,
            self.pending,
            session.uri,
            lambda: self._mirror_summary(session),
            self._advertise_resource,
        )
        call_id = f"terminal-{uuid.uuid4()}"
        started = time.monotonic()
        await sink.tool_call_started(
            call_id,
            "terminal",
            {"command": command},
            display_name=command,
            intention=f"Run {command!r}",
        )

        # A session claim, tied to the turn and the call that produced it: this
        # terminal belongs to the command, not to a client, so it dies with the
        # session rather than with whoever happened to type the `!`.
        claim = TerminalSessionClaim(session.uri, channel, turn_id, call_id)
        chunks: list[bytes] = []
        terminal_uri = f"ahp-terminal:/{uuid.uuid4()}"
        request = TerminalRequest(
            channel=terminal_uri,
            claim=claim,
            name=command,
            # The session's own first working directory, so `!ls` lists what the
            # user is looking at rather than wherever the host happens to run.
            cwd=_first_working_directory(self.sequencer.state_of(session.uri)),
            command=["/bin/sh", "-c", command],
        )
        try:
            process = await self.terminals.create(request, chunks.append)
        except Exception as exc:
            await sink.tool_call_completed(
                call_id,
                {"content": [{"type": "text", "text": f"{type(exc).__name__}: {exc}"}]},
                success=False,
                past_tense_message=f"Could not run {command!r}",
            )
            await self._settle_turn(session, channel, turn_id, started)
            return

        self._oneshot_terminals[terminal_uri] = process
        try:
            code = await process.wait()
        finally:
            # Reached on CANCELLATION, which is the whole point. A client's
            # `chat/turnCancelled` cancels this task while it is parked on
            # `wait()`, and the kill used to be on the line *after* that await:
            # a cancelled `!sleep 400` left the shell -- and every process in
            # its group -- running, invisible to `aclose()` because a one-shot
            # has no `_Terminal` and no entry in `_live_terminals`. Measured:
            # the child outlived the host process and reparented to init.
            #
            # `kill()` is the same SIGHUP -> SIGTERM -> SIGKILL escalation an
            # ordinary terminal gets, awaited here rather than spawned: a task
            # started during cancellation is not guaranteed to run before the
            # loop stops.
            with contextlib.suppress(Exception):
                await process.kill()
            # After the kill, deliberately. If the kill is itself interrupted --
            # a second cancel, a loop shutting down -- the entry stays in the
            # registry and `aclose` gets one more chance at the child.
            self._oneshot_terminals.pop(terminal_uri, None)
        # Decoded with `replace`: a command that writes invalid UTF-8 -- which
        # any binary output is -- must not take the turn down with it.
        output = b"".join(chunks).decode("utf-8", "replace")
        await sink.tool_call_completed(
            call_id,
            {"content": [{"type": "text", "text": output}]},
            success=code == 0,
            past_tense_message=f"Ran {command!r}" + ("" if code == 0 else f" (exit {code})"),
        )
        await self._settle_turn(session, channel, turn_id, started)

    async def _settle_turn(
        self, session: _Session, channel: str, turn_id: str, started: float
    ) -> None:
        """Complete a turn the host ran itself, and do the turn-end chores."""
        await self.sequencer.publish(
            channel,
            {
                "type": "chat/turnComplete",
                "turnId": turn_id,
                "duration": max(0, int((time.monotonic() - started) * 1000)),
            },
        )
        self._spawn(self._mark_unread(session))
        self._spawn(self._drain_queue(session, channel))
        with contextlib.suppress(Exception):
            await self._mirror_summary(session)

    async def _start_turn(
        self,
        session: _Session,
        channel: str,
        action: Mapping[str, Any],
        agent: AgentSession | None = None,
    ) -> None:
        """Run *action* as a turn on *channel*, by *agent* if given (an external
        turn) or else the session's own agent."""
        command = self._terminal_command(action)
        if command is not None:
            # Never reaches the provider. The user asked the HOST to run a
            # command; handing it to an agent as a message beginning with `!`
            # is what the prefix exists to stop.
            task = asyncio.create_task(
                self._run_terminal_command(session, channel, action, command)
            )
        else:
            await self._seed_title(session, channel, action)
            runner = TurnRunner(
                self.sequencer,
                channel,
                self.pending,
                session.uri,
                lambda: self._mirror_summary(session),
                self._advertise_resource,
            )
            session.runners[channel] = runner
            task = asyncio.create_task(self._run_turn(session, runner, action, agent))
        session.turns[channel] = task
        session.turn_started[channel] = time.monotonic()
        # Both ends of the turn, from ONE place each. A changeset operation is
        # greyed while a turn is in flight, and the done callback is the only
        # hook every path passes through -- normal completion, a raise, and the
        # `task.cancel()` in `_cancel_turn`, `_react_to_truncate` and
        # `aclose()`. Hanging it off each of those instead is how the gate came
        # to be evaluated once and never again.
        task.add_done_callback(lambda _: self._spawn(self._settle_operations(session)))
        await self._sync_operation_status(session)

    async def _settle_operations(self, session: _Session) -> None:
        """`_sync_operation_status`, safe to run after the session is gone.

        Fired from a task callback, so it can land during shutdown or after a
        `disposeSession` dropped the channels underneath it -- neither of which
        is a fault worth surfacing as an unretrieved task exception.
        """
        if self._sessions.get(session.uri) is not session:
            return
        with contextlib.suppress(Exception):
            await self._sync_operation_status(session)

    async def _cancel_turn(self, session: _Session, chat: str, reason: str) -> None:
        """Stop the turn running in ONE chat.

        Cancelling the task is the real signal: the provider's
        `send_user_message` coroutine runs inside it, so it takes a
        `CancelledError` at its next await and its sink stops publishing.

        `AgentSession.cancel()` is a courtesy on top -- and it is
        SESSION-scoped, because a session has one agent session shared by every
        chat. So it is only sent when nothing else in this session is still
        running. A provider that treats it as "stop everything" (the shipped
        `EchoProvider` sets a flag the whole session reads) would otherwise
        truncate an innocent chat's answer because a different chat was
        cancelled -- which is precisely the class of bug this method exists to
        close, one layer down.
        """
        for task in session.running(chat):
            task.cancel()
        session.turns.pop(chat, None)
        if session.agent_session is not None and not session.running():
            with contextlib.suppress(Exception):
                await session.agent_session.cancel(reason)

    async def _end_stranded_turn(self, session: _Session, chat: str) -> None:
        """Publish a terminal action for a turn nothing else will ever end.

        A turn stream ends on `chat/turnComplete`, `chat/turnCancelled` or
        `chat/error`, and a dropped channel produces none of the three: a
        `disposeSession` or `disposeChat` mid-turn left every subscriber
        awaiting a turn that had already stopped. The client's own `TurnStream`
        does not time out on the protocol's behalf -- it waits, and the caller
        of `await prompt()` waits with it.

        `chat/turnCancelled` -- "Turn was aborted; server stops processing" --
        is what dispose does, said in the spec's own words. Published while the
        channel still exists and after the task is cancelled, so the reducer
        clears `activeTurn` and nothing the runner was mid-way through can land
        behind it.
        """
        state = self.sequencer.state_of(chat)
        active = state.get("activeTurn") if isinstance(state, Mapping) else None
        turn_id = active.get("id") if isinstance(active, Mapping) else None
        started = session.turn_started.pop(chat, None)
        if not isinstance(turn_id, str):
            return
        # Measured, like `_settle_turn`'s. `duration` is required on every
        # terminal action and a client renders it as the turn's elapsed time,
        # so a hardcoded zero would show every disposed turn as instantaneous.
        elapsed = 0 if started is None else max(0, int((time.monotonic() - started) * 1000))
        await self.sequencer.publish(
            chat, {"type": "chat/turnCancelled", "turnId": turn_id, "duration": elapsed}
        )

    async def _react_to_truncate(
        self, session: _Session, channel: str, action: Mapping[str, Any]
    ) -> None:
        """Make the agent forget what the client just stopped showing.

        Two halves. "If there is an active turn it is silently dropped and the
        chat status returns to `idle`" -- the reducer does the status, this does
        the actual task, which would otherwise keep publishing deltas into a
        transcript that no longer has a turn to hang them on. Then the provider
        is told, because the reducer can only rewrite state and only the
        provider owns the agent's memory.

        `_validate_truncate` has already refused this for a provider that cannot
        forget, so reaching here means one can.
        """
        # THIS chat's turn. Truncating one chat's history must not abort a turn
        # running in another.
        await self._cancel_turn(session, channel, "history truncated")
        if not isinstance(session.agent_session, TruncatesHistory):
            return
        # Read EXACTLY as the reducer reads it. An absent key clears
        # everything; an explicit null is not the same thing -- the reducer
        # searches for a turn with that id, finds none and no-ops -- and a
        # string truncates after that turn. Collapsing null into absent here
        # would have the agent forget a whole conversation the client still
        # shows, which is the same defect as this one with the sides swapped.
        if "turnId" not in action:
            target: str | None = None
        elif isinstance(action["turnId"], str):
            target = action["turnId"]
        else:
            return
        with contextlib.suppress(Exception):
            await session.agent_session.history_truncated(channel, target)

    async def _drain_queue(self, session: _Session, channel: str) -> None:
        """Consume the next queued message, if the chat is free to run it.

        "If the chat is idle when a queued message is set, the server SHOULD
        immediately consume it and start a new turn" -- and
        `chat/pendingMessageRemoved` is "dispatched ... by the server when it
        consumes a message". We did neither, so a follow-up typed while the
        agent worked stayed in the chip forever: the client showed it queued and
        waited for a host that was never coming back for it.

        Called at both ends -- when a message is queued, and when a turn
        finishes -- because "idle" is a race either way round.
        """
        state = self.sequencer.state_of(channel)
        if not isinstance(state, Mapping):
            return
        steering = state.get("steeringMessage")
        if state.get("activeTurn") is not None:
            if isinstance(steering, Mapping):
                await self._steer(session, channel, steering)
            return
        # Idle: a steering message the turn never took (it ended first, or the
        # agent can't be steered) runs next, ahead of the queue.
        queued = state.get("queuedMessages")
        if isinstance(steering, Mapping):
            queued = [{**steering, "_kind": "steering"}]
        if not isinstance(queued, list) or not queued:
            return
        entry = queued[0]
        if not isinstance(entry, Mapping):
            return
        message = entry.get("message")
        if not isinstance(message, Mapping):
            return
        # Removed BEFORE the turn starts, and awaited. The entry has to leave
        # the queue while the chat is still idle: once `chat/turnStarted`
        # lands, a second `_drain_queue` would find the same entry, see a turn
        # already active, and leave it -- but a removal published *after* the
        # turn ended would race the next drain and run it twice.
        await self.sequencer.publish(
            channel,
            {
                "type": "chat/pendingMessageRemoved",
                "kind": "steering" if entry.get("_kind") == "steering" else "queued",
                "id": entry.get("id"),
            },
        )
        started = {
            "type": "chat/turnStarted",
            "turnId": f"queued-{uuid.uuid4()}",
            "startedAt": now_iso(),
            "message": dict(message),
        }
        # PUBLISHED, then run. A client-dispatched turn is published by the
        # dispatch path before `_react` runs it; a turn the host starts by
        # itself has no such path, and running it without publishing leaves the
        # chat with no turn at all -- the deltas arrive against a turn no client
        # has ever heard of. Not routed back through `_react`, which would run
        # it a second time.
        await self.sequencer.publish(channel, started)
        await self._start_turn(session, channel, started)

    async def _steer(self, session: _Session, channel: str, entry: Mapping[str, Any]) -> None:
        """Offer a steering message to the turn running on *channel*.

        Taken, it leaves the chat's pending slot and is noted in the transcript;
        not taken, it stays, and `_drain_queue` runs it once the chat is idle.
        """
        message = entry.get("message")
        runner = session.runners.get(channel)
        if (
            not isinstance(message, Mapping)
            or not isinstance(session.agent_session, SteersTurns)
            or runner is None
            or runner.sink is None
        ):
            return
        text = message.get("text")
        text = text if isinstance(text, str) else ""
        try:
            taken = await session.agent_session.steer(channel, UserMessage(text=text, raw=message))
        except Exception:
            _log.exception("steering %s failed", channel)
            return
        if not taken:
            return
        await self.sequencer.publish(
            channel,
            {"type": "chat/pendingMessageRemoved", "kind": "steering", "id": entry.get("id")},
        )
        await runner.sink.steered(text)

    async def _seed_title(self, session: _Session, channel: str, action: Mapping[str, Any]) -> None:
        """Name a still-unnamed session after the message that started it.

        Every session is called "New Session", so a list with three of them is
        three identical rows. The reference host writes a real title with a
        small model, which needs credentials we do not have -- but the first
        thing the user said is the same information in a rougher form, and it is
        right here.

        Only for a session nobody has named. A client can dispatch
        `session/titleChanged` to rename one, and overwriting that would make
        the rename look like it failed. The default chat only, too: a side chat
        asking "what does this do?" must not become the session's name.
        """
        if channel != session.chat_uri:
            return
        if _published_title(self.sequencer.state_of(session.uri)) != _DEFAULT_SESSION_TITLE:
            return
        message = action.get("message")
        text = message.get("text") if isinstance(message, Mapping) else None
        title = _title_from(text if isinstance(text, str) else "")
        if title is None:
            return
        await self.sequencer.publish(session.uri, {"type": "session/titleChanged", "title": title})
        await self._mirror_summary(session)

    async def _mark_unread(self, session: _Session) -> None:
        """Return the unread dot after the agent has answered.

        `session/isReadChanged` is a two-party protocol: a client dispatches it
        with `true` when a human looks at the session, and the host is what
        turns it back to `false` when something new arrives. Implementing only
        the client's half means the dot appears exactly once, on a session that
        has never been opened, and never again however many times the agent
        answers.

        Not while somebody is watching. `activeClients` is the session's own
        record of which clients have it open; marking one of those unread would
        put a badge on the session currently on screen.
        """
        state = self.sequencer.state_of(session.uri)
        if not isinstance(state, Mapping):
            return
        clients = state.get("activeClients")
        if isinstance(clients, list) and clients:
            return
        status = state.get("status")
        if not isinstance(status, int) or not status & SessionStatus.IS_READ:
            return
        with contextlib.suppress(Exception):
            await self.sequencer.publish(
                session.uri, {"type": "session/isReadChanged", "isRead": False}
            )
            await self._mirror_summary(session)

    async def _resume_if_restored(self, session: _Session) -> None:
        """Give a restored session its agent back, on its first turn.

        Restored sessions start without one (see :meth:`restore`) so a host
        with many stored sessions doesn't start many agents. The provider gets
        what it persisted plus what the session's state says now: its folders
        and its config *values*, which a client may have changed since.
        Failing leaves the session without an agent, and the turn then fails
        as `provider.resumeSession` -- which is what it is.
        """
        provider = self._provider_of(session)
        if session.agent_session is not None or not isinstance(provider, ResumableAgentProvider):
            return
        state = self.sequencer.state_of(session.uri)
        state = state if isinstance(state, Mapping) else {}
        config = state.get("config")
        values = config.get("values") if isinstance(config, Mapping) else None
        folders = state.get("workingDirectories")
        context = AgentSessionContext(
            publisher=session.publisher,
            session_uri=session.uri,
            chat_uri=session.chat_uri,
            provider_id=session.provider_id,
            working_directories=tuple(f for f in folders or () if isinstance(f, str)),
            config=dict(values) if isinstance(values, Mapping) else {},
            resume_state=session.resume_state,
        )
        try:
            session.agent_session = await provider.resume_session(context)
        except Exception:
            _log.exception("could not resume %s", session.uri)

    async def _react_to_config(self, channel: str, action: Mapping[str, Any]) -> None:
        """Tell the agent a `sessionMutable` property changed, then save it.

        Validation already limited the change to properties the provider
        declared mutable. A restored session with no agent yet gets nothing to
        call: the new values are in state, and resuming reads them from there.
        """
        session = self._sessions.get(channel)
        values = action.get("config")
        if session is None or not isinstance(values, Mapping):
            return
        if isinstance(session.agent_session, ReconfiguresSessions):
            try:
                await session.agent_session.config_changed(dict(values))
            except Exception:
                _log.exception("config_changed failed for %s", channel)
        await self._persist(session)

    async def _react_to_archive(self, channel: str, action: Mapping[str, Any]) -> None:
        """Tell the agent its session was archived or unarchived, then save it."""
        session = self._sessions.get(channel)
        archived = action.get("isArchived")
        if session is None or not isinstance(archived, bool):
            return
        if isinstance(session.agent_session, ArchivesSessions):
            try:
                await session.agent_session.archived_changed(archived)
            except Exception:
                _log.exception("archived_changed failed for %s", channel)
        await self._persist(session)

    async def _run_turn(
        self,
        session: _Session,
        runner: TurnRunner,
        action: Mapping[str, Any],
        agent: AgentSession | None = None,
    ) -> None:
        """Run one turn, then bring the root catalogue back in step.

        Deliberately mirrored at the two ends of the turn rather than per action.
        The spec sanctions exactly this: servers "MAY coalesce or debounce
        updates for noisy fields (for example, `modifiedAt` bumps while a turn is
        streaming)". Emitting per delta would be one root notification per token,
        to every connected client, to move a timestamp nobody is watching.
        """
        try:
            await self._resume_if_restored(session)
            await runner.run(agent or session.agent_session, action)
        finally:
            # Retracted in a DETACHED task on purpose. This one is frequently
            # the task being cancelled, and a cancelled task cannot be relied on
            # to finish another await -- but a session left advertising input
            # nobody can answer stays `InputNeeded` until it is disposed.
            if runner.abandoned:
                self._spawn(self._retract_all(session, [r.id for r in runner.abandoned]))
            # Detached for the same reason, and unconditional: a turn cancelled
            # mid-tool would otherwise leave the session list advertising a tool
            # that stopped running, with nothing that ever comes back to clear
            # it. `set_activity` is a no-op when there is nothing to clear.
            if runner.sink is not None:
                self._spawn(runner.sink.set_activity(None))
            self._spawn(self._mark_unread(session))
            # Detached, and after the turn task has released the channel: this
            # starts the NEXT turn, and starting it from inside the finally of
            # the turn it follows would make this chat's slot overwrite itself
            # while this frame still owns it.
            if session.runners.get(runner.channel) is runner:
                del session.runners[runner.channel]
            self._spawn(self._drain_queue(session, runner.channel))
            with contextlib.suppress(Exception):
                await self._mirror_summary(session)

    # ─── observability ───────────────────────────────────────────────────

    def _audit(
        self,
        kind: str,
        connection: Connection,
        *,
        channel: str | None = None,
        allowed: bool = True,
        reason: str | None = None,
        detail: Mapping[str, Any] | None = None,
    ) -> None:
        emit(
            self.audit,
            AuditEvent(
                kind=kind,
                client_id=connection.client_id or None,
                peer=connection.peer,
                channel=channel,
                allowed=allowed,
                reason=reason,
                detail=detail or {},
            ),
        )

    def counters(self) -> dict[str, int]:
        """A few numbers, without reaching into private attributes.

        "The port is open" is a weak liveness signal for a process whose
        interesting failure modes -- a wedged sequencer, a provider that stopped
        answering, suspended requests nobody will resolve -- all keep the port
        open. `pending` is the one to watch: it only grows when providers are
        waiting on clients that are not answering.

        Deliberately not a metrics endpoint. What scrapes this is the
        embedder's; what it means is documented here.
        """
        return {
            "connections": len(self._connections),
            "sessions": len(self._sessions),
            "activeTurns": sum(len(s.running()) for s in self._sessions.values()),
            "pendingRequests": len(self.pending),
            "watches": len(self._watches),
            "channels": self.sequencer.channel_count,
            "serverSeq": self.sequencer.server_seq,
            # Connections closed because the peer stopped reading. A non-zero
            # value here explains disconnects that otherwise look mysterious,
            # and a climbing one means a client or a proxy is not draining.
            "outboxOverflows": self._outbox_overflows,
        }

    # ─── shutdown ────────────────────────────────────────────────────────

    async def aclose(self) -> None:
        for session in self._sessions.values():
            for task in session.running():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            if session.agent_session is not None:
                await session.agent_session.aclose()
        # Terminals die WITH the host. Without this the shells outlive it:
        # measured, a child reparents to init and its own children survive, so
        # a host that has opened terminals over its life leaves a pile of
        # orphaned processes behind every time it stops.
        #
        # On SHUTDOWN only, never on connection close. VS Code drops a
        # connection deliberately and re-attaches to the same terminal URIs
        # (`reconnectTerminals` re-subscribes and never calls `createTerminal`)
        # -- disposing on disconnect would turn a routine reconnect into "all
        # your terminals died".
        for terminal in list(self._live_terminals.values()):
            if terminal.reaper is not None:
                terminal.reaper.cancel()
            with contextlib.suppress(Exception):
                await terminal.close()
        self._live_terminals.clear()
        # And the `!command` children, which are not in that map. Their own
        # `finally` normally kills them, but `_cancel_turn` pops the task from
        # `session.turns` before it finishes -- so the loop above never awaited
        # it, and a host that stopped in that window took the last chance to
        # reap the child with it.
        for process in list(self._oneshot_terminals.values()):
            with contextlib.suppress(Exception):
                await process.kill()
        self._oneshot_terminals.clear()
        for connection in list(self._connections):
            await connection.close()
        # Without this the last debounce window of a turn is lost -- which is
        # exactly the state a client is most likely to come back looking for.
        with contextlib.suppress(Exception):
            await self.store.aclose()
