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
import contextlib
import json
import logging
import re
import uuid
import weakref
from collections.abc import (
    AsyncIterable,
    AsyncIterator,
    Awaitable,
    Callable,
    Mapping,
    Sequence,
)
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Final, Protocol

from ahp_host.core import AhpError
from ahp_host.provider.base import (
    AgentInfo,
    AgentInfoChanged,
    AgentSessionContext,
    ChatContext,
    ClientToolCall,
    CompletionItem,
    CompletionRequest,
    ConfigRequest,
    ConfigResolution,
    ConfigValue,
    ForkedFrom,
    IdentifiesTurn,
    ModelInfo,
    ResolvesInput,
    SessionDescription,
    SessionDirectory,
    ToolConfirmation,
    TurnSink,
    UserMessage,
)
from ahp_host.provider.changes import FileChange
from ahp_protocol.types import AHP_ERROR_CODES
from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
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

from ahp_host_claude import (
    background,
    client_tools,
    completions,
    customizations,
    edits,
    history,
    questions,
)
from ahp_host_claude.attachments import prompt_content
from ahp_host_claude.claude_ai import (
    ALL,
    BACKFILL_EXCHANGES,
    LOCAL,
    Api,
    LoginError,
    RemoteClient,
    RemoteSession,
    running_here,
    this_machine,
    uri_of,
)
from ahp_host_claude.client_tools import ClientTool, is_client_tool
from ahp_host_claude.config import DEFAULT_STATE
from ahp_host_claude.effort import CONFIG_KEY as EFFORT_KEY
from ahp_host_claude.effort import DEFAULT as EFFORT_DEFAULT
from ahp_host_claude.effort import PROPERTY as EFFORT_PROPERTY
from ahp_host_claude.effort import effort_level, sdk_effort
from ahp_host_claude.history import Rewind, TurnMark
from ahp_host_claude.models import CACHE_FILE, Learned, Limits, probe_limits
from ahp_host_claude.paths import directory_of
from ahp_host_claude.permissions import (
    APPROVALS_PROPERTY,
    ASK,
    CONFIG_KEY,
    DISALLOWED_TOOLS,
    EXIT_PLAN_TOOL,
    PERMISSION_MODES,
    PLAN,
    QUESTION_TOOL,
    approval_mode,
    choices,
    denial_message,
    describe,
    past_tense,
    pre_tool_use_decision,
    progress_line,
)
from ahp_host_claude.remote_control import CONFIG_KEY as RC_CONFIG_KEY
from ahp_host_claude.remote_control import (
    RemoteControlClient,
    auto_enable,
    bridge_of,
)
from ahp_host_claude.remote_control import property_schema as rc_property
from ahp_host_claude.roots import Roots, as_roots
from ahp_host_claude.sessions import (
    NEW,
    SEARCH_LIMIT,
    ClaudeCodeSessions,
    describe_session,
    is_session_id,
    title_of,
)
from ahp_host_claude.transcripts import transcript
from ahp_host_claude.usage import report as usage_report

log = logging.getLogger(__name__)

PROVIDER_ID = "claude"
#: The session config property naming a Claude Code conversation to continue.
CONTINUE_KEY = "continueFrom"
_PROVIDER_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")


def is_valid_provider_id(value: str) -> bool:
    """A provider id is how `createSession` names an agent; keep it plain."""
    return _PROVIDER_ID.fullmatch(value) is not None


#: What Claude Code records as having started these sessions. Not the SDK's
#: `sdk-py`: `claude --resume` hides every `sdk-*` session, and these are
#: meant to be found there like a terminal's. Not `cli` either: in a
#: non-interactive process Claude Code rewrites that to `sdk-cli`. A name of
#: its own is kept as written. Claude Code then also treats the session as a
#: terminal one in what it offers (claude.ai artifacts, its guide agent), which
#: is the point; those tools pass the same approval gate as any other.
ENTRYPOINT = "ahp-host"

#: Claude Code's value for "whatever the account's default is".
DEFAULT_MODEL = "default"

#: What a session with no folder may use: nothing that touches the machine.
#: Its `cwd` is an empty directory of the agent's own, but Claude Code's shell
#: and file tools are not jailed to `cwd` - the OS user is the only boundary -
#: so a chat gets no file or shell tools at all, and no MCP servers from the
#: user's config (which can reach anything). The web tools still pass the
#: approval gate like any other.
CHAT_TOOLS: Final = ("WebSearch", "WebFetch")


def chat_tools_subset(names: Sequence[str]) -> tuple[str, ...]:
    """`names`, checked to be a subset of `CHAT_TOOLS` (order kept, duplicates dropped).

    A host can take a web tool away from folderless sessions - one that cannot
    reach the internet has no use for WebFetch - but never add one: anything
    outside `CHAT_TOOLS` would touch the machine.
    """
    if isinstance(names, str):
        raise ValueError("chat_tools must be a list of tool names, not a string")
    out: list[str] = []
    for name in names:
        if name not in CHAT_TOOLS:
            raise ValueError(
                f"chat_tools: {name!r} is not allowed; choose from {', '.join(CHAT_TOOLS)}"
            )
        if name not in out:
            out.append(name)
    return tuple(out)


#: Told to Claude in a session with no folder, so it says why it cannot look
#: at a file rather than trying tools that are not there.
CHAT_PROMPT: Final = (
    "This conversation has no folder: you have no file, shell or code tools on "
    "this machine. If the user wants you to work with files, they can add a "
    "folder to this conversation, which gives you those tools there."
)
#: The capability that lets a client add a folder to a running session.
#: `immutablePrimary`: the session's `cwd` is fixed for its life (Claude Code
#: keeps a conversation under the folder it started in, so resuming it anywhere
#: else starts a blank one), and the first folder is where it points.
WORKING_DIRECTORIES_CAPABILITY: Final[Mapping[str, Any]] = {"immutablePrimary": True}


class SdkClient(Protocol):
    """The slice of `ClaudeSDKClient` this adapter uses; tests supply a fake."""

    async def connect(self) -> None: ...

    async def query(self, prompt: str | AsyncIterable[dict[str, Any]]) -> None: ...

    def receive_messages(self) -> AsyncIterator[Any]: ...

    async def interrupt(self) -> None: ...

    async def set_model(self, model: str | None = None) -> None: ...

    async def set_permission_mode(self, mode: Any) -> None: ...

    async def disconnect(self) -> None: ...

    async def get_server_info(self) -> dict[str, Any] | None: ...

    async def remote_control(
        self, enabled: bool, *, reattach: str | None = None, keep: bool = True
    ) -> Mapping[str, Any]: ...


ClientFactory = Callable[[ClaudeAgentOptions], SdkClient]


def _default_client(options: ClaudeAgentOptions) -> SdkClient:
    return RemoteControlClient(options=options)


def _entries(info: Mapping[str, Any] | None) -> list[Mapping[str, Any]]:
    entries = (info or {}).get("models")
    if not isinstance(entries, Sequence) or isinstance(entries, str):
        return []
    return [entry for entry in entries if isinstance(entry, Mapping)]


def models_from_server_info(
    info: Mapping[str, Any] | None,
    *,
    limits: Mapping[str, Limits] | None = None,
    learned: Learned | None = None,
) -> tuple[ModelInfo, ...]:
    """The picker entries Claude Code itself offers this account.

    Claude Code reports them at start-up (`get_server_info()["models"]`), with
    the account's own default first. Taking them from there, rather than from
    a list in this file, is what keeps a new model - or an account without a
    given one - right without a release of this adapter.

    *limits* are what the start-up probe found each one's window to be, and
    *learned* what earlier sessions' results said; see `models.py`, which also
    says why every entry takes images.
    """
    models: list[ModelInfo] = []
    for entry in _entries(info):
        value, name = entry.get("value"), entry.get("displayName")
        if not isinstance(value, str) or not value:
            continue
        meta = {
            key: entry[key]
            for key in ("description", "resolvedModel", "supportedEffortLevels")
            if key in entry
        }
        found = (limits or {}).get(value, Limits())
        if learned is not None:
            found = found.merged(learned.for_entry(entry))
        models.append(
            ModelInfo(
                id=value,
                name=name if isinstance(name, str) else value,
                max_context_window=found.context_window,
                max_prompt_tokens=found.max_prompt
                if found.max_prompt is not None
                else found.context_window,
                max_output_tokens=found.max_output,
                supports_vision=True,
                meta=meta or None,
            )
        )
    return tuple(models)


@dataclass(frozen=True)
class Discovery:
    """What Claude Code says about this account at start-up."""

    models: tuple[ModelInfo, ...] = ()
    #: Whether Claude Code would turn Remote Control on for a session it
    #: started itself - the user's `remoteControlAtStartup`, then org policy.
    remote_control: bool = False
    #: Its slash commands, as offered in the served folder: what a session
    #: completes ``/`` with until its own Claude client has said.
    commands: tuple[Mapping[str, Any], ...] = ()


async def discover(
    root: Path,
    client_factory: ClientFactory = _default_client,
    *,
    state_dir: Path | None = None,
) -> Discovery:
    """Ask Claude Code once. Empty (no picker, no Remote Control) if it cannot say.

    With *state_dir*, the models carry the limits sessions learned there.
    """
    client = client_factory(ClaudeAgentOptions(cwd=str(root)))
    try:
        await client.connect()
        info = await client.get_server_info()
        # The probe is idle and about to go, so trying each model on it
        # changes nothing anyone is using.
        limits = await probe_limits(client, _entries(info))
    except Exception:
        log.exception("could not ask Claude Code about this account; no models, no Remote Control")
        return Discovery()
    finally:
        try:
            await client.disconnect()
        except Exception:
            log.debug("disconnecting the start-up probe failed", exc_info=True)
    learned = Learned(state_dir / CACHE_FILE) if state_dir is not None else None
    commands = (info or {}).get("commands")
    return Discovery(
        models=models_from_server_info(info, limits=limits, learned=learned),
        remote_control=auto_enable(info),
        commands=tuple(c for c in commands if isinstance(c, Mapping))
        if isinstance(commands, Sequence)
        else (),
    )


async def discover_models(
    root: Path, client_factory: ClientFactory = _default_client
) -> tuple[ModelInfo, ...]:
    """Ask Claude Code which models it offers. Empty (no picker) if it cannot say."""
    return (await discover(root, client_factory)).models


def _user_message(
    content: str | list[dict[str, Any]], message_id: str, *, priority: str | None = None
) -> AsyncIterable[dict[str, Any]]:
    """One user message in the SDK's streaming-input form, with our own uuid.

    The CLI echoes it back with that uuid (`--replay-user-messages`), which is
    how a replay is told apart from a message typed somewhere else: those carry
    an `origin` and a uuid we never sent.

    ``priority: "next"`` is the CLI's own queue slot for "at the next tool
    boundary": it joins the turn in flight, or - if the model has already made
    its last tool call - is answered straight after it (a second result).
    """
    message: dict[str, Any] = {
        "type": "user",
        "uuid": message_id,
        "message": {"role": "user", "content": content},
        "parent_tool_use_id": None,
    }
    if priority is not None:
        message["priority"] = priority

    async def stream() -> AsyncIterator[dict[str, Any]]:
        yield message

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
        elif isinstance(item, TextBlock):
            parts.append(item.text)
    return "\n".join(parts)


#: How long a turn started elsewhere may take to reach the host before its
#: messages are dropped rather than holding up everything behind them.
_ATTACH_TIMEOUT = 10.0
#: An approval answered elsewhere: how long to wait for the tool's result to
#: say whether it was a denial, before reporting it approved.
_ANSWER_GRACE = 0.5
#: How the CLI words a denied tool call's result.
#: Only a fallback: `system/permission_denied` normally says so first. The
#: second is how a denial on claude.ai comes back.
_DENIED = ("The user doesn't want to proceed with this tool use", "Denied by user")


@dataclass(eq=False)
class _Turn:
    """The turn the stream's messages currently belong to."""

    sink: TurnSink | None = None
    #: Set once `sink` is: a turn started elsewhere gets one only when the host
    #: has started it, which is after its first message has arrived.
    attached: asyncio.Event = field(default_factory=asyncio.Event)
    done: asyncio.Event = field(default_factory=asyncio.Event)
    model: str | None = None
    #: Where the turn sits in Claude Code's transcript (`history.py`).
    mark: TurnMark | None = None
    #: The usage of the turn's latest main-loop request (`usage.py`).
    last_request: Mapping[str, Any] | None = None
    #: The Claude client it runs on: only that client's end can end it.
    client: Any = None
    #: The API error Claude Code reported on the turn's last answer, if any.
    api_error: str | None = None


def _turn_id_of(sink: TurnSink) -> str | None:
    """The AHP turn a sink publishes into (`IdentifiesTurn`), if it says.

    Edit-and-resend names the turn to keep by it. A sink that does not say
    leaves its turn unmarked, and a cut to it forgets everything.
    """
    if isinstance(sink, IdentifiesTurn):
        value = sink.turn_id
        return value if isinstance(value, str) and value else None
    return None


#: The `system` subtypes that carry a task's lifecycle (`background.py`).
_TASK_SUBTYPES: Final = frozenset({"task_started", "task_updated", "task_notification"})
#: `forward_subagent_text` is newer than the oldest SDK this runs on.
_FORWARDS_SUBAGENT_TEXT: Final = "forward_subagent_text" in ClaudeAgentOptions.__dataclass_fields__
#: A name a `Skill(name)` permission rule can carry as it is.
_RULE_SAFE: Final = re.compile(r"[A-Za-z0-9._:-]+")
#: The tool that starts a subagent, under both of Claude Code's names for it.
_SUBAGENT_TOOLS: Final = frozenset({"Agent", "Task"})
#: How Claude Code words a refused cut (`resume_drops_turn`), and a cut at a
#: message it cannot find.
_CUT_REFUSED: Final = "--resume-drops-turn"
_CUT_NOT_FOUND: Final = "No message found with message.uuid"
#: What Claude is told on the first prompt after a rewind (`history.py`).
REWOUND_NOTE: Final = (
    "[The user rewound this conversation to this point: what came after it is gone. "
    "Changes made to files in that part were not undone, so check the files before "
    "relying on them.]"
)


class ClaudeSession:
    def __init__(
        self,
        context: AgentSessionContext,
        *,
        root: Path | Roots,
        client_factory: ClientFactory,
        claude_session_id: str | None = None,
        approvals: str | None = None,
        directory: Path | None = None,
        recap: str | None = None,
        remote_control: bool = False,
        bridge_session_id: str | None = None,
        archived: bool = False,
        mirror_of: str | None = None,
        on_disposed: Callable[[], None] | None = None,
        chat_dir: Path | None = None,
        status_on_claude_ai: Callable[[str], Awaitable[str | None]] | None = None,
        chat_tools: Sequence[str] = CHAT_TOOLS,
        effort: str | None = None,
        marks: Sequence[TurnMark] = (),
        rewind: Rewind | None = None,
        rewound: bool = False,
        cost_baseline: float | None = None,
        disabled: Sequence[str] = (),
        commands: Sequence[Mapping[str, Any]] = (),
        on_model_usage: Callable[[Any], None] | None = None,
    ) -> None:
        self.context = context
        #: Chosen at creation (or restored on resume); a client may change it
        #: later (`config_changed`), which restarts Claude on the conversation.
        self.effort = effort_level(effort if effort is not None else context.config.get(EFFORT_KEY))
        #: What a session with no folder may use (`CHAT_TOOLS`, or the subset the
        #: host was configured with).
        self._chat_tools = tuple(chat_tools)
        #: Asks claude.ai whether a session is archived there (`start`).
        self._status_on_claude_ai = status_on_claude_ai
        #: The folders the session has now: the context's are only the ones it
        #: was created with, and a client may add or remove one later.
        self.working_directories: tuple[str, ...] = tuple(context.working_directories)
        #: Where a session with no folder runs: empty, and nobody's project.
        self._chat_dir = (chat_dir if chat_dir is not None else DEFAULT_STATE / "chat").resolve()
        #: The folders changed while a client was running with the old ones.
        self._restart_pending = False
        #: The claude.ai session this one mirrors (`claude_ai.py`), if it is
        #: one started elsewhere rather than here.
        self.mirror_of = mirror_of
        self._on_disposed = on_disposed
        #: The session's `cwd`, fixed at its first start and kept in its resume
        #: state: Claude Code keeps a conversation under the folder it started
        #: in, so a client started anywhere else would begin a blank one. When
        #: continuing another Claude Code conversation, that conversation's.
        self.directory = directory
        #: Posted once, at the start of the first reply.
        self._recap = recap
        #: Chosen at creation (or restored on resume); a client may change
        #: it later, which arrives through `config_changed`.
        self.approvals = approval_mode(
            approvals if approvals is not None else context.config.get(CONFIG_KEY)
        )
        #: Whether the session is also on claude.ai; see `remote_control.py`.
        self.remote_control = remote_control
        #: The claude.ai session to reattach to, so a restart keeps the same
        #: one. Kept while Remote Control is off, for when it is turned on.
        self.bridge_session_id = bridge_session_id
        self.session_url: str | None = None
        #: Archived by a client: filed away on claude.ai too, and not started.
        self.archived = archived
        self._roots = as_roots(root)
        self._root = self._roots.primary
        self._client_factory = client_factory
        self._client: SdkClient | None = None
        self._reader: asyncio.Task[None] | None = None
        self._starting: asyncio.Task[None] | None = None
        self._connecting = asyncio.Lock()
        self._model: str | None = context.model
        #: The SDK's session id: what `resume=` needs after a host restart.
        self.claude_session_id = claude_session_id
        #: Whoever the stream's messages are for right now, if anyone.
        self._turn: _Turn | None = None
        self._announced: set[str] = set()
        #: Each announced call's tool and input, for its past-tense line.
        self._inputs: dict[str, tuple[str, dict[str, Any]]] = {}
        self._streamed_messages: set[str] = set()
        self._lock = asyncio.Lock()
        #: Uuids of the user messages we sent and the CLI has not echoed yet.
        self._sent: set[str] = set()
        #: Steered into the running turn and not yet echoed back by the CLI
        #: (`--replay-user-messages`), i.e. not yet taken in. The turn stays
        #: open until this is empty, so an answer that comes after the turn's
        #: first result still lands in this turn rather than the next one.
        self._steers_pending: set[str] = set()
        self._accepting_steers = False
        #: Turns that ended here before Claude Code finished them (a stop, a
        #: failure): each still owes an aborted result, which must not end
        #: whatever turn comes next.
        self._stale_results = 0
        #: Approvals the other side answered: the call, the timer that
        #: reports it approved unless its result says otherwise first, and the
        #: sink of the chat that asked.
        self._answered_elsewhere: dict[str, tuple[asyncio.Task[None], TurnSink]] = {}
        #: The tools this session's clients run for it (`client_tools.py`),
        #: starting with the creator's; `active_clients_changed` keeps it current.
        creator = (
            [{"clientId": context.active_client_id, "tools": list(context.client_tools)}]
            if context.active_client_id is not None
            else []
        )
        self._client_tools = client_tools.offered(creator)
        #: The tools the running Claude client was started with.
        self._started_tools: tuple[ClientTool, ...] = ()
        #: Claude's id for each client tool call about to run, in order, keyed
        #: by tool: the MCP handler is not told it, and the hook before it is.
        self._client_calls: dict[str, list[tuple[str, dict[str, Any]]]] = {}
        #: Claude Code's tasks that have started and not ended, by task id --
        #: the ones shown as chat background work and the ones that may yet
        #: be (`background.py`).
        self._tasks: dict[str, background.Task] = {}
        #: Subagents with a worker chat, by the `tool_use_id` of the call that
        #: started them -- the `parent_tool_use_id` their messages carry.
        self._subagents: dict[str, background.Subagent] = {}
        #: Each worker chat opened this turn, by the spawning call: its result
        #: links to it, and a foreground subagent has ended by the time it
        #: comes.
        self._worker_chats: dict[str, tuple[str, str]] = {}
        #: Each AHP turn's place in Claude Code's transcript, oldest first, and
        #: a cut not yet taken (`history.py`).
        self.marks: list[TurnMark] = list(marks)
        self.rewind = rewind
        #: A rewind happened and Claude has not been told yet (the next prompt).
        self.rewound = rewound
        #: The running cost total the next result's is measured from; None
        #: when unknown (a resumed session from before it was kept).
        if cost_baseline is None and claude_session_id is None:
            cost_baseline = 0.0  # a new conversation has cost nothing yet
        self.cost_baseline: float | None = cost_baseline
        self._on_model_usage = on_model_usage
        #: What Claude Code has said about the session's skills, agents,
        #: plugins and MCP servers, and the tree last published from it.
        self._sources = customizations.Sources(commands=list(commands) or None)
        self._tree = customizations.Tree()
        self._published: list[dict[str, Any]] | None = None
        #: Customizations a client switched off here (ids).
        self.disabled: set[str] = set(disabled)
        #: The skills the running client was started without.
        self._started_denied: tuple[str, ...] = ()
        #: The custom agent the next turn runs as (`--agent`), and the one the
        #: running client was started as.
        self._agent: str | None = None
        self._started_agent: str | None = None
        self._refreshing: asyncio.Task[None] | None = None
        self._chores: set[asyncio.Task[None]] = set()
        #: What Claude's editing tools changed (`edits.py`).
        self._edits = edits.Edits(self._roots)
        #: Question calls asked here and not answered here yet, with the sink
        #: that asked and the questions (`_ask`).
        self._open_questions: dict[str, tuple[TurnSink, dict[str, Any]]] = {}
        #: The chat this session's conversation is, when it is not the
        #: session's default one: what out-of-turn publications name.
        self._scope: str | None = None
        #: The chat's own folders, when it has a subset of the session's.
        self.subset: tuple[str, ...] | None = None
        #: The transcript a new conversation starts from, when it is a fork
        #: Claude Code could not copy (`transcripts.py`); given with its first
        #: prompt.
        self.seed: str | None = None

    @property
    def _sink(self) -> TurnSink | None:
        return self._turn.sink if self._turn is not None else None

    # -- lifecycle -----------------------------------------------------------

    def _granted(self) -> list[Path]:
        """The chat's folders as real paths, each inside a served folder.

        The session's, or the chat's own subset of them (`ChatState.
        workingDirectories`) when it has one.
        """
        granted: list[Path] = []
        folders = self.working_directories
        if self.subset is not None:
            folders = tuple(uri for uri in self.subset if uri in self.working_directories)
        for uri in folders:
            real = self._roots.real_path(uri)
            if real is not None:
                granted.append(real)
                continue
            path = directory_of(uri)
            if path is not None:
                raise PermissionError(f"{path.resolve()} is outside this host's root {self._root}")
        return granted

    def working_directory(self) -> Path:
        """The session's `cwd`: where it started, else its first folder, else a chat's.

        Must lie inside a served folder, or be the chat directory.
        """
        if self.directory is not None:
            resolved = self.directory.resolve()
            if resolved != self._chat_dir and not self._roots.contains(resolved):
                raise PermissionError(f"{resolved} is outside this host's root {self._root}")
            return resolved
        granted = self._granted()
        return granted[0] if granted else self._chat_dir

    def _access(self) -> tuple[Path, tuple[Path, ...], bool]:
        """`cwd`, the other folders, and whether the session may touch any of it.

        Tools come with a folder, never without: a session's own folders, or
        the folder a continued conversation came from. A session that started
        as a chat keeps the chat directory as its `cwd` after a folder is
        added; the folder is added beside it, and granted.
        """
        cwd = self.working_directory()
        granted = self._granted()
        extra = tuple(path for path in granted if path != cwd)
        if self.subset is not None:
            # A chat narrowed to no folder gets no tools, whatever its `cwd`.
            return cwd, extra, bool(granted)
        return cwd, extra, bool(granted) or cwd != self._chat_dir

    def _gate(self) -> str:
        """The approval mode the gate applies: the session's, or Ask.

        Security-relevant. Claude Code's own `cwd` is the session's folder,
        fixed for the conversation's life, and its looser modes act there
        without asking. A chat whose folders leave that folder out is put in
        Ask, so every change there is put to a person first.
        """
        if self.subset is None or self.mirror_of is not None:
            return self.approvals
        try:
            cwd, _, tools = self._access()
        except PermissionError:
            return ASK
        if tools and not any(cwd == path or path in cwd.parents for path in self._granted()):
            return ASK
        return self.approvals

    @property
    def is_chat(self) -> bool:
        """No folder: no tools that touch the machine."""
        if self.mirror_of is not None:
            return False  # its tools are its own machine's business
        try:
            return not self._access()[2]
        except PermissionError:
            return True

    def _options(self) -> ClaudeAgentOptions:
        if self.mirror_of is not None:
            # Claude Code runs on the session's own machine, with its own
            # folders, tools and mode; all this host adds is its approvals.
            return ClaudeAgentOptions(can_use_tool=self._can_use_tool)
        cwd, extra, tools = self._access()
        if cwd == self._chat_dir:
            cwd.mkdir(parents=True, exist_ok=True, mode=0o700)
        options: dict[str, Any] = {}
        prompt: dict[str, Any] = {"type": "preset", "preset": "claude_code"}
        if not tools:
            options = {"tools": list(self._chat_tools), "strict_mcp_config": True}
            prompt["append"] = CHAT_PROMPT
        if self._client_tools:
            # Tools that run in a client, not here: offered with or without a
            # folder, since they touch nothing on this machine.
            options["mcp_servers"] = {
                client_tools.SERVER: client_tools.server(self._client_tools, self._run_client_tool)
            }
        if _FORWARDS_SUBAGENT_TEXT:
            # A subagent's own prose, not just its tool calls, so its worker
            # chat reads as a conversation.
            options["forward_subagent_text"] = True
        extra_args: dict[str, str | None] = {"replay-user-messages": None}
        if self._agent is not None:
            # Claude Code reads its main-thread agent at start-up only.
            extra_args["agent"] = self._agent
        if self.rewind is not None and self.claude_session_id is not None:
            # Load the conversation only up to the turn kept (`history.py`).
            options["resume_session_at"] = self.rewind.at
            if self.rewind.drops is not None:
                options["resume_drops_turn"] = self.rewind.drops
            if self.rewind.fork:
                # Another chat's conversation: a copy of it, which leaves the
                # original exactly as that chat has it.
                options["fork_session"] = True
        return ClaudeAgentOptions(
            cwd=str(cwd),
            add_dirs=list(extra),
            resume=self.claude_session_id,
            model=None if self._model == DEFAULT_MODEL else self._model,
            effort=sdk_effort(self.effort),  # type: ignore[arg-type]
            permission_mode=PERMISSION_MODES[self._gate()],  # type: ignore[arg-type]
            # A skill switched off here is a deny rule for it: the Skill tool
            # refuses it. Not the SDK's `skills` allowlist, which also
            # allow-lists every skill it names and drops local settings.
            disallowed_tools=[*DISALLOWED_TOOLS, *(f"Skill({n})" for n in self._denied_skills())],
            can_use_tool=self._can_use_tool,
            hooks={"PreToolUse": [HookMatcher(matcher=None, hooks=[self._pre_tool_use])]},
            include_partial_messages=True,
            # Echo each streamed user message when the CLI takes it in: the
            # only signal that a steered message joined the turn, and what
            # tells our own messages from ones typed elsewhere.
            extra_args=extra_args,
            system_prompt=prompt,  # type: ignore[arg-type]
            env={"CLAUDE_CODE_ENTRYPOINT": ENTRYPOINT},
            **options,
        )

    def _denied_skills(self) -> tuple[str, ...]:
        """The skills a client switched off here, alone or with their plugin.

        Only names a permission rule can carry: a name with a comma, a
        parenthesis or a space would be read as something else.
        """
        prefix = customizations.skill_id("")
        # A skill's id names it, so this holds before Claude Code has listed
        # anything; a plugin's skills are known only once it has.
        off = {i.removeprefix(prefix) for i in self.disabled if i.startswith(prefix)}
        for plugin, members in self._tree.plugin_skills.items():
            if plugin in self.disabled:
                off.update(self._tree.skills[member] for member in members)
        return tuple(name for name in sorted(off) if _RULE_SAFE.fullmatch(name))

    async def working_directories_changed(self, directories: Sequence[str]) -> None:
        """A client added, removed or replaced a folder (`FollowsWorkingDirectories`).

        Security-relevant: this is what gives a chat file and shell tools, and
        takes them away. Claude Code fixes its tools and folders when it
        starts, so a running client with the old ones is restarted - resuming
        the same conversation, in the same `cwd` - now if it is idle, else
        once its turn is over. The host has already refused a folder outside
        the served ones; one that got here anyway fails the next turn
        (`working_directory`), it is never silently dropped.
        """
        if self.mirror_of is not None:
            # Where it runs on its own machine: nothing here to restart.
            self.working_directories = tuple(directories)
            return
        try:
            before: Any = self._access()
        except PermissionError:
            before = None
        self.working_directories = tuple(directories)
        try:
            after: Any = self._access()
        except PermissionError:
            after = None
        if self._client is None or before == after:
            return
        self._restart_pending = True
        if not self._lock.locked() and self._turn is None:
            await self._restart_client()

    async def _restart_client(self) -> None:
        """Start again with the session's folders as they are now."""
        self._restart_pending = False
        await self._stop_client()
        if self.remote_control and not self.archived:
            # Reachable from claude.ai again straight away, as at creation.
            self.start_soon()

    def start_soon(self, *, unarchive: bool = False) -> None:
        """`start`, without holding up whoever created the session."""
        if self._starting is None:
            self._starting = asyncio.create_task(self.start(unarchive=unarchive))

    async def start(self, *, unarchive: bool = False) -> None:
        """Start the Claude client now, rather than on the first message.

        A session on claude.ai needs a running client to be reachable at all,
        so one with Remote Control starts as soon as it exists. A failure here
        is logged, not raised: the first turn tries again and reports it.
        """
        if not unarchive and await self._archived_on_claude_ai():
            log.info(
                "claude.ai session %s is archived there; left as it is", self.bridge_session_id
            )
            return
        try:
            await self._ensure_client()
        except Exception:
            log.exception("starting the Claude client failed; the first turn will retry")

    async def _archived_on_claude_ai(self) -> bool:
        """Whether the claude.ai session was archived there, by someone else.

        Starting would reattach to it, and reattaching un-archives: without
        this every host restart brought back what was archived on claude.ai.
        A message sent here still reattaches - that is someone using it. If
        claude.ai cannot be asked, it is started as before.
        """
        bridge, ask = self.bridge_session_id, self._status_on_claude_ai
        if bridge is None or ask is None:
            return False
        try:
            return await ask(bridge) == "archived"
        except Exception:
            log.debug("could not ask claude.ai about %s", bridge, exc_info=True)
            return False

    async def _ensure_client(self) -> SdkClient:
        async with self._connecting:
            if self._client is None:
                client = await self._connect()
                self._client = client
                self._reader = asyncio.create_task(self._read(client))
                if self.remote_control and not self.archived:
                    await self._enable_remote_control(client)
                self._refreshing = asyncio.create_task(self._after_start(client))
            return self._client

    async def _connect(self) -> SdkClient:
        """Start Claude Code, retrying once if it refuses a pending cut.

        A cut naming the one turn it drops is refused when more would go with
        it; it is then taken without that check (`history.py`). One at a
        message Claude Code cannot find cannot be taken at all, so the
        conversation is forgotten instead - the agent must not keep what the
        user was shown being taken back.
        """
        for attempt in range(2):
            options = self._options()
            # Pinned: the conversation lives under this folder from now on.
            self.directory = Path(str(options.cwd))
            self._started_tools = self._client_tools if self.mirror_of is None else ()
            self._started_agent = self._agent
            self._started_denied = self._denied_skills()
            client = self._client_factory(options)
            try:
                await client.connect()
            except Exception as error:
                if attempt or not self._recover_cut(str(error)):
                    raise
                continue
            return client
        raise AssertionError("unreachable")  # pragma: no cover

    def _recover_cut(self, error: str) -> bool:
        """Whether a failed start was a pending cut, now made takeable."""
        rewind = self.rewind
        if rewind is None:
            return False
        if _CUT_REFUSED in error and rewind.drops is not None:
            log.info("Claude Code refused to drop one turn alone; cutting without the check")
            self.rewind = Rewind(at=rewind.at)
            return True
        if _CUT_NOT_FOUND in error:
            log.warning("the turn to rewind to is not in the transcript; forgetting it all")
            self.rewind = None
            self.claude_session_id = None
            self.marks = []
            return True
        return False

    async def aclose(self) -> None:
        # The process and its background shells are going; say so while the
        # chat can still hear it.
        await self._forget_tasks()
        client, self._client = self._client, None
        reader, self._reader = self._reader, None
        if reader is not None:
            reader.cancel()
        refreshing, self._refreshing = self._refreshing, None
        if refreshing is not None:
            refreshing.cancel()
        for timer, _ in self._answered_elsewhere.values():
            timer.cancel()
        if client is not None:
            try:
                await client.disconnect()
            except Exception:  # a dying subprocess must not fail session disposal
                log.exception("disconnecting the Claude client failed")

    async def disposed(self) -> None:
        """Deleted here, so it goes from claude.ai too.

        Only a deletion: at shutdown the session stays there, offline, for
        the next start to reattach to. One already archived is already gone
        from there.
        """
        if self._on_disposed is not None:
            self._on_disposed()
        if self.bridge_session_id is None or self.archived:
            return
        self.archived = True
        if await self._archive_on_claude_ai():
            self.bridge_session_id = None

    async def archived_changed(self, is_archived: bool) -> None:
        """A client filed the session away, or brought it back.

        Archived, it is archived on claude.ai too, and its Claude process
        stops: nothing needs to reach it. Unarchived, the process starts again
        and reattaches, which brings the claude.ai session back. The
        bridge id is kept throughout, so it is the same session both ways.
        """
        if is_archived == self.archived:
            return
        self.archived = is_archived
        if is_archived:
            await self._archive_on_claude_ai()
            await self._stop_client()
        elif self.remote_control:
            self._starting = None
            # Archived on claude.ai by us; reattaching is the point.
            self.start_soon(unarchive=True)

    async def _archive_on_claude_ai(self) -> bool:
        """Archive the claude.ai session. Whether it worked.

        Three steps, because a session turned on as kept is never archived
        while it stays on: off, on again unkept (the same session), off.
        `self.archived` is set first, so starting a client for this does not
        turn Remote Control back on as kept.
        """
        bridge = self.bridge_session_id
        if bridge is None:
            return False
        try:
            client = await self._ensure_client()
            if self.session_url is not None:
                await client.remote_control(False)
            self.session_url = None
            await client.remote_control(True, reattach=bridge, keep=False)
            await client.remote_control(False)
        except Exception:
            log.warning("could not archive claude.ai session %s", bridge, exc_info=True)
            return False
        return True

    async def _stop_client(self) -> None:
        """Let the Claude process go; the next turn starts one again."""
        # Its background shells go with it.
        await self._forget_tasks()
        client, self._client = self._client, None
        reader, self._reader = self._reader, None
        self._starting = None
        if reader is not None:
            reader.cancel()
        refreshing, self._refreshing = self._refreshing, None
        if refreshing is not None:
            refreshing.cancel()
        if client is not None:
            try:
                await client.disconnect()
            except Exception:
                log.exception("disconnecting the Claude client failed")

    async def cancel(self, reason: str | None = None) -> None:
        if self._client is not None:
            try:
                await self._client.interrupt()
            except Exception:
                log.exception("interrupting the Claude client failed")

    # -- Remote Control ------------------------------------------------------

    async def _enable_remote_control(self, client: SdkClient) -> None:
        """Put the session on claude.ai. A failure leaves it local, not broken."""
        attempts = [self.bridge_session_id, None] if self.bridge_session_id else [None]
        for reattach in attempts:
            try:
                bridge = bridge_of(await client.remote_control(True, reattach=reattach))
            except Exception:
                log.warning("Remote Control refused (reattach=%s)", reattach, exc_info=True)
                continue
            if bridge is None:
                log.warning("Remote Control gave an answer this adapter does not understand")
                return
            new = bridge.bridge_session_id != self.bridge_session_id
            self.bridge_session_id = bridge.bridge_session_id
            self.session_url = bridge.session_url
            log.info("Remote Control: %s", bridge.session_url)
            if new:
                await self._save()
            return

    async def _save(self) -> None:
        """Have the host save this session now, with a new bridge id in it.

        The host saves a session when something happens in it. One that comes
        up, goes on claude.ai and sits idle would otherwise lose its bridge id
        at the next restart - and get a new claude.ai session each time.
        Restating the setting is the one thing a provider can do that saves.
        """
        publisher = self.context.publisher
        if publisher is None:
            return
        try:
            await publisher.config_changed({RC_CONFIG_KEY: self.remote_control})
        except Exception:
            log.warning("could not save the claude.ai session id", exc_info=True)

    async def _set_remote_control(self, enabled: bool) -> None:
        if enabled == self.remote_control:
            return
        self.remote_control = enabled
        if self.archived:
            return  # takes effect when it is unarchived
        if enabled:
            # Starting the client is what turns it on.
            client = await self._ensure_client()
            if self.session_url is None:
                await self._enable_remote_control(client)
            return
        self.session_url = None
        if self._client is not None:
            try:
                await self._client.remote_control(False)
            except Exception:
                log.warning("turning Remote Control off failed", exc_info=True)

    # -- customizations ------------------------------------------------------

    async def describe(self) -> SessionDescription:
        """The customization tree, as far as Claude Code has told it (`DescribesSession`).

        Asked at bring-up, which is before the Claude client starts for most
        sessions: then this is empty, and the tree is published as soon as the
        client says what it has.
        """
        return SessionDescription(customizations=list(self._published or ()))

    async def _after_start(self, client: SdkClient) -> None:
        """What a newly started client can say before anyone sends it anything.

        Its commands and agents (`get_server_info`), where each skill and
        agent came from (`get_context_usage`), and its MCP servers
        (`get_mcp_status`); then the file index is asked once, so it is built
        by the time someone types ``@``. Each is optional - a claude.ai
        mirror answers none of them - and none failing is an error.
        """
        try:
            info = await client.get_server_info()
        except Exception:
            info = None
        if isinstance(info, Mapping):
            commands, agents = info.get("commands"), info.get("agents")
            if isinstance(commands, Sequence):
                self._sources.commands = [c for c in commands if isinstance(c, Mapping)]
            if isinstance(agents, Sequence):
                self._sources.agents = [a for a in agents if isinstance(a, Mapping)]
        usage = await _optional(client, "context_usage")
        if isinstance(usage, Mapping):
            self._sources.usage = usage
        await self._read_mcp(client)
        await self._publish_customizations()
        await _optional(client, "file_suggestions", "")
        if self.mirror_of is None and self._denied_skills() != self._started_denied:
            # A plugin switched off before a restart: its skills are known
            # only now, so the client starts again with them refused.
            self._restart_pending = True
            asyncio.get_running_loop().call_soon(self._restart_when_idle)

    def _restart_when_idle(self) -> None:
        if self._restart_pending and not self._lock.locked() and self._turn is None:
            self._background(self._restart_client())

    def _background(self, work: Awaitable[None]) -> None:
        """Run *work* on its own task, kept until it is done."""

        async def run() -> None:
            try:
                await work
            except Exception:
                log.exception("background work for a session failed")

        task = asyncio.create_task(run())
        self._chores.add(task)
        task.add_done_callback(self._chores.discard)

    async def _read_mcp(self, client: SdkClient) -> None:
        status = await _optional(client, "get_mcp_status")
        servers = status.get("mcpServers") if isinstance(status, Mapping) else None
        if isinstance(servers, Sequence):
            self._sources.mcp = [server for server in servers if isinstance(server, Mapping)]

    def _workspace_uri(self) -> str | None:
        return self.working_directories[0] if self.working_directories else None

    async def _publish_customizations(self) -> None:
        """Republish the tree if it changed.

        A change only in MCP servers' states goes out as each server's
        lifecycle (`mcp_server_changed`); anything else replaces the tree,
        which is the only publication the host offers for it.
        """
        publisher = self.context.publisher
        tree = self._build_tree()
        if tree is None:
            return
        self._tree = tree
        published, self._published = self._published, tree.customizations
        if publisher is None or published == tree.customizations:
            return
        if published is None and not tree.customizations:
            return  # nothing to say, and nothing said before
        try:
            changed = _states_only(published, tree.customizations)
            if changed is not None:
                for identifier, state in changed:
                    await publisher.mcp_server_changed(identifier, state)
            else:
                await publisher.customizations_changed(tree.customizations)
        except Exception:
            log.exception("publishing the session's customizations failed")

    def _build_tree(self) -> customizations.Tree | None:
        try:
            cwd = self.working_directory()
        except PermissionError:
            return None
        return customizations.build(
            self._sources,
            cwd=cwd,
            disabled=self.disabled,
            workspace=self._workspace_uri(),
            hidden_servers=(client_tools.SERVER,),
        )

    async def _refresh_mcp(self) -> None:
        client = self._client
        if client is None or self.mirror_of is not None:
            return
        await self._read_mcp(client)
        await self._publish_customizations()

    async def _on_init(self, data: Mapping[str, Any]) -> None:
        """`system/init`: the session's tools, plugins and MCP servers, and its mode.

        Security-relevant: Claude Code reports the permission mode it actually
        started in, which a custom agent's own settings can change. Anything
        but the mode chosen here is put back - a session never runs looser
        than its setting because of an agent file.
        """
        self._sources.init = data
        mode = data.get("permissionMode")
        wanted = PERMISSION_MODES[self.approvals]
        if (
            self.mirror_of is None
            and isinstance(mode, str)
            and mode != wanted
            and self._client is not None
        ):
            log.info("Claude Code started in %s, not %s; putting it back", mode, wanted)
            try:
                await self._client.set_permission_mode(wanted)
            except Exception:
                log.warning("putting the permission mode back failed", exc_info=True)
        await self._publish_customizations()

    # -- chats -------------------------------------------------------------------

    async def chat_opened(self, context: ChatContext) -> None:
        """Refused: a claude.ai session has one conversation, on another machine.

        A local session (`LocalClaudeSession`) runs a conversation per chat.
        """
        raise AhpError(
            AHP_ERROR_CODES["PermissionDenied"],
            "This session runs on another machine, through claude.ai, as one "
            "conversation: it cannot open another chat.",
        )

    async def chat_closed(self, chat_uri: str) -> None:
        return

    # -- completions -------------------------------------------------------------

    async def complete(self, request: CompletionRequest) -> Sequence[CompletionItem]:
        """``/`` commands and ``@`` files for the message being typed (`completions.py`).

        Both come from the running Claude client. A session whose client has
        not started yet starts it now - someone is typing a message for it -
        and completes ``/`` from what the host learned at start-up meanwhile.
        """
        if request.kind not in ("userMessage", ""):
            return ()
        token = completions.token_of(request)
        if token is None:
            return ()
        if self._client is None and self.mirror_of is None and not self.archived:
            self.start_soon()
        if token.trigger == "/":
            hidden = _names((self._sources.init or {}).get("terminal_slash_commands"))
            return completions.slash_items(
                token, request.offset, self._sources.commands or (), hidden=hidden
            )
        client = self._client
        if client is None:
            return ()
        try:
            async with asyncio.timeout(_COMPLETION_TIMEOUT):
                reply = await _optional(client, "file_suggestions", token.typed)
            cwd = self.working_directory()
        except (TimeoutError, PermissionError):
            return ()
        if not isinstance(reply, Mapping):
            return ()
        return completions.file_items(token, request.offset, reply, cwd=cwd, roots=self._roots)

    # -- steering ------------------------------------------------------------

    async def steer(self, chat_uri: str, message: UserMessage) -> bool:
        """Send a message into the turn in flight (priority ``next``).

        Refused outside a turn, or once the turn has decided it is over; the
        host then runs the message as the next turn instead.
        """
        client = self._client
        text = message.text.strip()
        if not self._accepting_steers or client is None or not text:
            return False
        message_id = self._send_id()
        self._steers_pending.add(message_id)
        await client.query(_user_message(text, message_id, priority="next"))
        return True

    def _send_id(self) -> str:
        message_id = str(uuid.uuid4())
        self._sent.add(message_id)
        return message_id

    # -- permissions ---------------------------------------------------------

    async def config_changed(self, values: Mapping[str, Any]) -> None:
        """A client switched the approval mode or Remote Control mid-session.

        Security-relevant. The `PreToolUse` gate (`self.approvals`) moves
        first, then the running Claude client's own mode. If switching the
        client fails, the two disagree only in the safe direction: loosening,
        Claude Code's stricter mode still sends calls to the approval prompt;
        tightening to Ask, the gate already asks for everything regardless.
        """
        if RC_CONFIG_KEY in values:
            await self._set_remote_control(values[RC_CONFIG_KEY] is True)
        if EFFORT_KEY in values:
            await self._set_effort(effort_level(values[EFFORT_KEY]))
        if CONFIG_KEY not in values:
            return
        mode = approval_mode(values[CONFIG_KEY])
        if mode == self.approvals:
            return
        self.approvals = mode
        if self._client is not None:
            await self._client.set_permission_mode(PERMISSION_MODES[mode])

    async def _set_effort(self, level: str) -> None:
        """Claude Code reads effort only at start-up: restart on the same
        conversation, now if idle, else once the turn in flight is over."""
        if level == self.effort:
            return
        self.effort = level
        if self.mirror_of is not None or self._client is None:
            return
        self._restart_pending = True
        if not self._lock.locked() and self._turn is None:
            await self._restart_client()

    async def _pre_tool_use(self, hook_input: Any, tool_use_id: str | None, context: Any) -> Any:
        name = str(hook_input.get("tool_name", ""))
        if is_client_tool(name):
            # Security-relevant: allowed without this host's approval. The
            # client that runs it owns that decision (`client_tools.py`).
            call_id = tool_use_id or hook_input.get("tool_use_id")
            tool_input = hook_input.get("tool_input")
            if isinstance(call_id, str):
                self._client_calls.setdefault(name, []).append(
                    (call_id, tool_input if isinstance(tool_input, dict) else {})
                )
            return {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "allow",
                }
            }
        call_id = tool_use_id or hook_input.get("tool_use_id")
        tool_input = hook_input.get("tool_input")
        if name in edits.EDIT_TOOLS and isinstance(call_id, str) and isinstance(tool_input, dict):
            # The file as it is just before the call runs, for its diff.
            cwd = self._cwd()
            if cwd is not None:
                self._edits.starting(call_id, name, tool_input, cwd)
        return pre_tool_use_decision(name, self._gate())

    def _cwd(self) -> Path | None:
        try:
            return self.directory if self.directory is not None else self.working_directory()
        except PermissionError:
            return None

    async def _file_edit(
        self, sink: TurnSink, call_id: str, *, success: bool
    ) -> Mapping[str, Any] | None:
        """The call's `fileEdit` content, and the changeset brought up to date."""
        change = self._edits.finished(call_id, success=success)
        if change is None:
            return None
        await self._changed(change)
        try:
            return await sink.file_edit(change)
        except Exception:
            log.debug("a call's diff could not be stored", exc_info=True)
            return None

    async def _changed(self, change: FileChange) -> None:
        """Publish the changeset that now includes *change*."""
        publisher = self.context.publisher
        if publisher is None:
            return
        try:
            await publisher.changes_published(self._edits.changeset, self._edits.changes())
        except Exception:
            log.exception("publishing Claude's changes failed")

    # -- client tools --------------------------------------------------------

    async def active_clients_changed(self, clients: Sequence[Mapping[str, Any]]) -> None:
        """The session's clients changed (`FollowsActiveClients`).

        Who runs each tool follows at once. Claude Code fixes its tools when
        it starts, so a changed set of tools restarts the client - resuming
        the same conversation - now if it is idle, else once its turn is over,
        as a changed folder does. A client joining with no tools, or with the
        same ones, restarts nothing.
        """
        self._client_tools = client_tools.offered(clients)
        if self.mirror_of is not None or self._client is None:
            return
        if self._client_tools == self._started_tools:
            return
        self._restart_pending = True
        if not self._lock.locked() and self._turn is None:
            await self._restart_client()

    def _claimed_call(self, tool: ClientTool, arguments: dict[str, Any]) -> str | None:
        """Claude's id for this call, recorded by the hook just before it."""
        queue = self._client_calls.get(f"mcp__{client_tools.SERVER}__{tool.name}") or []
        for index, (_, tool_input) in enumerate(queue):
            if tool_input == arguments:
                return queue.pop(index)[0]
        return queue.pop(0)[0] if queue else None

    async def _run_client_tool(self, tool: ClientTool, arguments: dict[str, Any]) -> dict[str, Any]:
        """Have the client run one of its tools, as a call of this turn."""
        call_id = self._claimed_call(tool, arguments) or f"client-{uuid.uuid4()}"
        sink = self._sink
        if sink is None:
            return client_tools.error("No turn is running to ask the client in.")
        current = next((t for t in self._client_tools if t.name == tool.name), None)
        if current is None:
            return client_tools.error(f"No client offers {tool.title!r} any more.")
        try:
            result = await sink.run_client_tool(
                ClientToolCall(
                    call_id=call_id,
                    name=current.published,
                    client_id=current.client_id,
                    tool_input=arguments,
                    display_name=current.title,
                )
            )
        except LookupError as exc:
            return client_tools.error(str(exc))
        return client_tools.mcp_result(result)

    async def _announce(self, call_id: str, name: str, tool_input: Mapping[str, Any]) -> None:
        if call_id in self._announced or self._sink is None:
            return
        if is_client_tool(name):
            return  # the client's call: `run_client_tool` announces it, as the client's
        self._announced.add(call_id)
        self._inputs[call_id] = (name, dict(tool_input))
        display, _ = describe(name, tool_input)
        await self._sink.tool_call_started(call_id, name, dict(tool_input), display_name=display)
        await self._sink.tool_call_delta(
            call_id, invocation_message=progress_line(name, tool_input)
        )

    async def _can_use_tool(
        self, tool_name: str, tool_input: dict[str, Any], context: ToolPermissionContext
    ) -> PermissionResultAllow | PermissionResultDeny:
        call_id = context.tool_use_id
        worker = await self._worker_asking(call_id, context)
        sink: TurnSink | None
        if worker is not None and worker.sink is not None and call_id:
            # A subagent's call: asked in its own chat, where its row is - and
            # answerable while no turn runs in the parent.
            sink = worker.sink
            await self._announce_in(worker, call_id, tool_name, tool_input)
        else:
            turn = self._turn
            if turn is not None and turn.sink is None:
                # Asked before a turn from elsewhere reached the host.
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(turn.attached.wait(), _ATTACH_TIMEOUT)
            sink = self._sink
            if sink is None or not call_id:
                return PermissionResultDeny(message="No client is attached to approve this.")
            await self._announce(call_id, tool_name, tool_input)
        if tool_name == QUESTION_TOOL:
            return await self._ask(sink, call_id, tool_input)
        plan = tool_input.get("plan") if tool_name == EXIT_PLAN_TOOL else None
        if isinstance(plan, str) and plan.strip():
            # The plan lives in the tool's input, which a client may not render
            # readably; the user must be able to read what they are approving.
            await sink.text_delta(f"\n\n{plan.strip()}\n")
        display, message = describe(tool_name, tool_input)
        # Security-relevant: Claude Code's suggestions, narrowed to this
        # session, offered for the user to pick (`permissions.choices`).
        offered = choices(context.suggestions, self.approvals)
        cwd = self._cwd()
        preview = (
            edits.preview(tool_name, tool_input, cwd=cwd, roots=self._roots)
            if cwd is not None
            else None
        )
        try:
            outcome = await sink.confirm_tool_call(
                ToolConfirmation(
                    call_id=call_id,
                    name=tool_name,
                    display_name=display,
                    invocation_message=context.title or f"{display}: {message}",
                    tool_input=tool_input,
                    confirmation_title=context.title,
                    options=offered.options,
                    edits=(preview,) if preview is not None else (),
                )
            )
        except asyncio.CancelledError:
            # Either the turn ended here (the host already withdrew the
            # prompt, and reporting is a no-op), or it was answered on
            # claude.ai and the CLI withdrew the question. Which answer is
            # known only from the call's result, if it comes quickly.
            timer = asyncio.create_task(self._report_answer_after_grace(sink, call_id))
            self._answered_elsewhere[call_id] = (timer, sink)
            raise
        if not outcome.approved:
            return PermissionResultDeny(message=denial_message(outcome))
        if tool_name == EXIT_PLAN_TOOL:
            await self._left_plan_mode()
        approved = outcome.tool_input if isinstance(outcome.tool_input, dict) else tool_input
        chosen = offered.chosen(outcome)
        if chosen is None:
            return PermissionResultAllow(updated_input=approved)
        if chosen.type == "setMode" and isinstance(chosen.mode, str):
            # The gate moves with Claude Code's mode, before the next call asks.
            await self._follow_mode(_APPROVAL_MODES.get(chosen.mode, ASK))
        return PermissionResultAllow(updated_input=approved, updated_permissions=[chosen])

    async def _report_answer_after_grace(self, sink: TurnSink, call_id: str) -> None:
        """No result yet, so the tool is running: it was approved elsewhere."""
        await asyncio.sleep(_ANSWER_GRACE)
        self._answered_elsewhere.pop(call_id, None)
        try:
            await self._answered(sink, call_id, denied=False)
        except Exception:
            log.exception("reporting an approval given elsewhere failed")

    async def _report_answer_from_result(self, call_id: str, failed: bool, text: str) -> None:
        await self._report_answer(call_id, denied=failed and text.startswith(_DENIED))

    async def _report_answer(self, call_id: str, *, denied: bool) -> None:
        """Say how a prompt withdrawn here was answered, in the chat that asked."""
        entry = self._answered_elsewhere.pop(call_id, None)
        if entry is None:
            return
        timer, sink = entry
        timer.cancel()
        await self._answered(sink, call_id, denied=denied)

    async def _ask(
        self, sink: TurnSink, call_id: str, tool_input: dict[str, Any]
    ) -> PermissionResultAllow | PermissionResultDeny:
        """`AskUserQuestion`: put its questions to the user, and hand back the answers.

        Under Remote Control the CLI asks claude.ai too, and withdraws this
        one if the questions are answered there: this await is then cancelled,
        and the request is left open until the call's result says how it was
        answered, when it is withdrawn here with those answers
        (`_questions_answered_elsewhere`).
        """
        request = questions.input_request(tool_input, key=call_id)
        if request is None:
            return PermissionResultDeny(message=questions.UNREADABLE)
        self._open_questions[call_id] = (sink, dict(tool_input))
        outcome = await sink.request_input(request)  # cancelled: kept open, see above
        self._open_questions.pop(call_id, None)
        return questions.permission_result(tool_input, outcome)

    async def _questions_answered_elsewhere(self, call_id: str, failed: bool, result: Any) -> None:
        """The questions this host asked were answered on claude.ai: withdraw them here.

        With the answers given there (the call's result, `tool_use_result`),
        so the transcript here shows them and no client can answer again. A
        turn that ended here first has no request left to withdraw.
        """
        asked = self._open_questions.pop(call_id, None)
        if asked is None:
            return
        sink, tool_input = asked
        if not isinstance(sink, ResolvesInput):
            return
        answered = result.get("answers") if isinstance(result, Mapping) else None
        with contextlib.suppress(Exception):
            await sink.input_resolved(
                call_id,
                response="decline" if failed else "accept",
                answers=None if failed else questions.wire_answers(tool_input, answered),
            )

    async def _answered(self, sink: TurnSink, call_id: str, *, denied: bool) -> None:
        await sink.tool_call_confirmed(
            call_id,
            approved=not denied,
            reason_message="Declined on another device" if denied else None,
        )
        name, _ = self._inputs.get(call_id, ("", {}))
        if not denied and name == EXIT_PLAN_TOOL:
            await self._left_plan_mode()

    # -- the approval mode, when it moves on its own ----------------------------

    async def _left_plan_mode(self) -> None:
        """A plan was approved, here or on claude.ai.

        Security-relevant. Leaving plan mode must not leave the session looser
        than Ask: Claude Code drops to its own default mode, where the user's
        allow rules would apply, and in `plan` the gate stays out of the way.
        If the phone chose a looser mode as it approved, the CLI says so next
        (`system/status`) and that is followed like any other switch there.
        """
        if self.approvals == PLAN:
            await self._follow_mode(ASK)

    async def _on_mode_elsewhere(self, claude_mode: Any) -> None:
        """Claude Code's permission mode moved: switched on claude.ai, or ours echoed.

        Security-relevant. Followed, because whoever can switch it there can
        already answer every approval prompt there. A mode this adapter does
        not offer (`bypassPermissions`, `dontAsk`) is not followed: the client
        is put back in Ask, and so is the gate.
        """
        mode = _APPROVAL_MODES.get(claude_mode) if isinstance(claude_mode, str) else None
        if mode is None:
            mode = ASK
            if self._client is not None:
                await self._client.set_permission_mode(PERMISSION_MODES[ASK])
        await self._follow_mode(mode)

    async def _follow_mode(self, mode: str) -> None:
        """Move the gate to `mode`, and show clients here what is in force."""
        if mode == self.approvals:
            return
        self.approvals = mode
        publisher = self.context.publisher
        if publisher is not None:
            try:
                await publisher.config_changed({CONFIG_KEY: mode})
            except Exception:
                log.exception("publishing the approval mode failed")

    # -- turns ---------------------------------------------------------------

    def _begin(self, turn: _Turn) -> None:
        self._announced.clear()
        self._inputs.clear()
        self._client_calls.clear()
        self._worker_chats.clear()
        self._turn = turn

    def _mark(self, turn: _Turn, sink: TurnSink, prompt: str) -> None:
        """Note where *turn* starts in the transcript, for a later rewind."""
        mark = TurnMark(turn=_turn_id_of(sink), prompt=prompt, last=prompt)
        turn.mark = mark
        self.marks.append(mark)
        del self.marks[: -history.MAX_MARKS]

    async def _learn_customizations(self) -> None:
        """Start the Claude client if need be, and wait until it has said what it has."""
        try:
            await self._ensure_client()
        except Exception:
            return  # the turn's own start reports it
        task = self._refreshing
        if task is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(asyncio.shield(task), _ATTACH_TIMEOUT)

    def _pick_agent(self, message: UserMessage) -> None:
        """The custom agent this message asks for (`AgentSelection`), by its URI.

        Only an agent this session published can be picked; an unknown URI is
        ignored rather than started under a name Claude Code may not have.
        """
        uri = message.agent_uri
        if uri is None:
            self._agent = None
            return
        name = self._tree.agents.get(uri)
        if name is None:
            log.info("no agent is published as %s; running without one", uri)
        self._agent = name

    def _note_rewind(self, content: str | list[dict[str, Any]]) -> str | list[dict[str, Any]]:
        """Tell Claude, once, that the conversation was rewound (`history.py`).

        Not on a slash command, which Claude Code runs only from the start of
        the message: the note waits for the next prompt instead.
        """
        if not self.rewound:
            return content
        if isinstance(content, str):
            if content.lstrip().startswith("/"):
                return content
            self.rewound = False
            return f"{REWOUND_NOTE}\n\n{content}"
        first = content[0] if content else {}
        if str(first.get("text", "")).lstrip().startswith("/"):
            return content
        self.rewound = False
        return [{"type": "text", "text": REWOUND_NOTE}, *content]

    def _finish(self, turn: _Turn) -> None:
        turn.done.set()
        if self._turn is turn:
            self._turn = None

    def _abandon(self, turn: _Turn) -> None:
        """The turn ended here before Claude Code finished it."""
        if not turn.done.is_set():
            self._stale_results += 1
            self._finish(turn)

    async def send_user_message(self, message: UserMessage, sink: TurnSink) -> None:
        async with self._lock:
            if self.mirror_of is None:
                if message.agent_uri is not None and message.agent_uri not in self._tree.agents:
                    # Picked from a tree this process has not built yet (a
                    # restart since): start Claude Code to learn it.
                    await self._learn_customizations()
                self._pick_agent(message)
                if self._client is not None and self._agent != self._started_agent:
                    self._restart_pending = True
            client = await self._client_for(sink)
            if client is None:
                return
            turn = _Turn(sink=sink, client=client)
            turn.attached.set()
            self._begin(turn)
            try:
                if self._recap:
                    recap, self._recap = self._recap, None
                    await sink.text_delta(recap)
                picked = message.model.id if message.model is not None else None
                if picked is not None and picked != self._model:
                    # "default" is Claude Code's name for the account default;
                    # the SDK spells that as no model at all.
                    await client.set_model(None if picked == DEFAULT_MODEL else picked)
                    self._model = picked
                turn.model = self._model
                content = self._seeded(
                    self._note_rewind(
                        prompt_content(
                            message.text, message.raw, self._roots, message.attached_chats
                        )
                    )
                )
                await self._query(client, turn, sink, content)
            finally:
                self._accepting_steers = False
                self._steers_pending.clear()
                self._abandon(turn)

    async def _client_for(self, sink: TurnSink) -> SdkClient | None:
        """The running client, (re)started as the session now needs; None if it cannot be."""
        if self._restart_pending:
            await self._restart_client()
        try:
            return await self._ensure_client()
        except PermissionError as error:
            await sink.turn_failed(str(error), error_type="agent.workingDirectory")
            return None

    async def _query(
        self,
        client: SdkClient,
        turn: _Turn,
        sink: TurnSink,
        content: str | list[dict[str, Any]],
        *,
        resuming: TurnMark | None = None,
    ) -> None:
        """Send one user message for *turn*, and wait for the turn to end.

        *resuming*: the turn already has its place in the transcript (a failed
        turn resumed), which this message continues rather than starts.
        """
        self._steers_pending.clear()
        message_id = self._send_id()
        if resuming is None:
            self._mark(turn, sink, message_id)
        else:
            turn.mark = resuming
        await client.query(_user_message(content, message_id))
        # No await between sending and this, so a steer is either
        # refused or sent after the message it steers.
        self._accepting_steers = True
        await turn.done.wait()

    def _seeded(self, content: str | list[dict[str, Any]]) -> str | list[dict[str, Any]]:
        """A new conversation that continues one Claude Code could not copy.

        Its transcript (`seed`) goes before the first prompt, once - not
        before a slash command, which Claude Code runs only from the start of
        a message.
        """
        if self.seed is None or self.claude_session_id is not None:
            return content
        first = content if isinstance(content, str) else str((content or [{}])[0].get("text", ""))
        if first.lstrip().startswith("/"):
            return content
        seed, self.seed = self.seed, None
        preface = (
            "[This conversation continues an earlier one, which you have not seen. "
            f"Its transcript so far:]\n{seed}\n[End of the earlier transcript.]"
        )
        if isinstance(content, str):
            return f"{preface}\n\n{content}"
        return [{"type": "text", "text": preface}, *content]

    async def _external(self, text: str, prompt: str | None = None) -> None:
        """A message typed elsewhere (claude.ai, a phone) started a turn.

        Opened on the host as a turn no client asked for, so the conversation
        here does not skip it. Its messages wait until the host has started
        it; `external_turn` refuses while the chat still has a turn, which can
        be the one that just finished here, so it is retried briefly.
        *prompt* is the message's uuid in the transcript, which marks where
        the turn starts.
        """
        publisher = self.context.publisher
        if publisher is None:
            return
        turn = _Turn(client=self._client)
        self._begin(turn)

        async def run(sink: TurnSink) -> None:
            turn.sink = sink
            if prompt is not None and self.mirror_of is None:
                self._mark(turn, sink, prompt)
            turn.attached.set()
            try:
                await turn.done.wait()
            finally:
                self._abandon(turn)

        for _ in range(40):
            # `chat=` is this conversation's own chat (None for the default
            # one), so a background task reporting back into a side chat is
            # shown there rather than dropped.
            if await publisher.external_turn(text, run, chat=self._scope):
                return
            await asyncio.sleep(0.05)
        log.warning("could not open a turn for a message sent from elsewhere")
        self._finish(turn)

    # -- the stream ----------------------------------------------------------

    async def _read(self, client: SdkClient) -> None:
        """Everything Claude Code says, for the life of the client."""
        try:
            async for item in client.receive_messages():
                try:
                    await self._on_message(item)
                except Exception:
                    log.exception("handling a message from Claude Code failed")
        except asyncio.CancelledError:
            raise
        except Exception as error:
            log.exception("reading from Claude Code failed")
            # A cut it refused while loading: the next start takes it another way.
            self._recover_cut(str(error))
        finally:
            if self._client is client:
                # The next turn starts a new one, resuming this conversation.
                self._client = None
                # And whatever it was running in the background died with it.
                await self._forget_tasks()
            turn = self._turn
            # Not a turn on a newer client: a reader cancelled by a restart
            # ends here only once that restart has started the next turn.
            if turn is not None and not turn.done.is_set() and turn.client in (client, None):
                if turn.sink is not None:
                    # Resumable: what Claude Code had done is in its transcript,
                    # which the next process resumes.
                    await turn.sink.turn_failed(
                        "Claude Code stopped", error_type="claude.exited", resumable=True
                    )
                self._finish(turn)

    # -- background work -----------------------------------------------------

    async def _on_task(self, subtype: str, data: Mapping[str, Any]) -> None:
        """One task lifecycle message, as chat background work (1.0.0)."""
        task_id = data.get("task_id")
        if not isinstance(task_id, str) or not task_id:
            return
        if subtype == "task_started":
            tool_use_id = data.get("tool_use_id")
            started = background.Task(
                task_id=task_id,
                task_type=data.get("task_type") if isinstance(data.get("task_type"), str) else None,
                description=str(data.get("description") or ""),
                started_at=background.now_iso(),
                tool_use_id=tool_use_id if isinstance(tool_use_id, str) else None,
                command=self._command_of(tool_use_id),
                backgrounded=background.backgrounded(data),
                agent_type=data.get("subagent_type")
                if isinstance(data.get("subagent_type"), str)
                else None,
                prompt=data.get("prompt") if isinstance(data.get("prompt"), str) else None,
            )
            self._tasks[task_id] = started
            await self._show_task(started)
            return
        known = self._tasks.get(task_id)
        if known is None:
            return
        task = known
        status = data.get("status")
        if subtype == "task_updated":
            patch = data.get("patch")
            patch = patch if isinstance(patch, Mapping) else {}
            status = patch.get("status")
            flag = background.backgrounded(patch)
            if flag is not None and status not in background.TERMINAL_STATUSES:
                task.backgrounded = flag
                await self._show_task(task)
        if status in background.TERMINAL_STATUSES:
            del self._tasks[task_id]
            sink = self._sink
            if task.published and sink is not None:
                # Work this chat listed in the background ended while a turn
                # runs here: say so in it. With no turn running, its removal
                # from the background work list is what a client sees.
                summary = data.get("summary")
                label = task.description or task.command or "Background task"
                ending = f": {_first_line(summary)}" if isinstance(summary, str) and summary else ""
                with contextlib.suppress(Exception):
                    await sink.system_notification(
                        f"{label} {status}{ending}",
                        meta=_note_meta("task", status=status, taskId=task_id),
                    )
            await self._hide_task(task)
            subagent = self._subagents.pop(task.tool_use_id or "", None)
            if subagent is not None:
                subagent.done.set()

    def _command_of(self, tool_use_id: object) -> str | None:
        """The command line of the `Bash` call *tool_use_id*, if this session saw it."""
        if not isinstance(tool_use_id, str):
            return None
        name, tool_input = self._inputs.get(tool_use_id, ("", {}))
        command = tool_input.get("command") if name == "Bash" else None
        return command if isinstance(command, str) else None

    async def _show_task(self, task: background.Task) -> None:
        if task.task_type in background.SUBAGENT_TASKS:
            # In the foreground too: its conversation is its own either way.
            await self._open_subagent(task)
        subagent = self._subagents.get(task.tool_use_id or "")
        work = background.work_for(task, subagent.chat.resource if subagent else None)
        if work is None:
            await self._hide_task(task)
            return
        publisher = self.context.publisher
        if publisher is None:
            return
        try:
            await publisher.background_work_set(work, chat=self._scope)
        except Exception:
            log.exception("publishing background work failed")
            return
        task.published = True

    async def _hide_task(self, task: background.Task) -> None:
        publisher = self.context.publisher
        if not task.published or publisher is None:
            return
        task.published = False
        try:
            await publisher.background_work_removed(task.work_id, chat=self._scope)
        except Exception:
            log.exception("withdrawing background work failed")

    async def _forget_tasks(self) -> None:
        """Every task ended at once: the Claude Code process running them is gone."""
        tasks, self._tasks = list(self._tasks.values()), {}
        for task in tasks:
            await self._hide_task(task)
        subagents, self._subagents = list(self._subagents.values()), {}
        for subagent in subagents:
            subagent.done.set()

    async def _open_subagent(self, task: background.Task) -> None:
        """Give a subagent its own worker chat, and a turn on it."""
        publisher = self.context.publisher
        call_id = task.tool_use_id
        if publisher is None or call_id is None:
            return
        known = self._subagents.get(call_id)
        if known is not None:
            # Opened from its first message, before Claude Code said which
            # task it is: now it can be stopped, and backgrounded.
            if task.task_id:
                known.task = task
            return
        title = task.description or task.agent_type or "Subagent"
        try:
            chat = await publisher.open_tool_chat(title, tool_call_id=call_id, chat=self._scope)
        except Exception:
            log.exception("opening a subagent's chat failed")
            return
        subagent = background.Subagent(task=task, chat=chat)
        self._subagents[call_id] = subagent
        self._worker_chats[call_id] = (chat.resource, title)
        _, tool_input = self._inputs.get(call_id, ("", {}))
        prompt = task.prompt or tool_input.get("prompt")

        async def run(sink: TurnSink) -> None:
            subagent.sink = sink
            subagent.attached.set()
            pending, subagent.pending = subagent.pending, []
            for item in pending:
                await self._route_to_subagent(subagent, item)
            try:
                await subagent.done.wait()
            except asyncio.CancelledError:
                # A client stopped the worker chat: stop that task, and only
                # it -- the host does not interrupt the whole session for this.
                stop = getattr(self._client, "stop_task", None)
                if stop is not None and subagent.task.task_id:
                    with contextlib.suppress(Exception):
                        await stop(subagent.task.task_id)
                raise

        text = prompt if isinstance(prompt, str) and prompt else task.description
        if not await chat.run_turn(text, run):
            self._subagents.pop(call_id, None)

    async def _subagent_of(self, parent: str) -> background.Subagent | None:
        """The worker for messages carrying *parent*, opening one if it is new.

        A subagent's first message can come before Claude Code's
        `task_started` for it. The spawning call says enough to open its
        chat; the task fills in when it arrives.
        """
        subagent = self._subagents.get(parent)
        if subagent is not None:
            return subagent
        name, tool_input = self._inputs.get(parent, ("", {}))
        if name not in _SUBAGENT_TOOLS:
            return None
        description, agent_type = tool_input.get("description"), tool_input.get("subagent_type")
        await self._open_subagent(
            background.Task(
                task_id="",
                task_type="local_agent",
                description=description if isinstance(description, str) else "",
                started_at=background.now_iso(),
                tool_use_id=parent,
                backgrounded=tool_input.get("run_in_background") is True,
                agent_type=agent_type if isinstance(agent_type, str) else None,
            )
        )
        return self._subagents.get(parent)

    async def _worker_asking(
        self, call_id: str | None, context: ToolPermissionContext
    ) -> background.Subagent | None:
        """The subagent whose tool call this permission prompt is about, if any.

        Claude Code says a prompt comes from a subagent (`agent_id`), but not
        which spawning call that is. So: the worker that already shows the
        call, or whose task is that agent; failing both, the only worker
        running. None means the parent turn's own call.
        """
        if not self._subagents:
            return None
        for subagent in self._subagents.values():
            if call_id in subagent.announced or (
                context.agent_id is not None and context.agent_id == subagent.task.task_id
            ):
                return await self._attached(subagent)
        if context.agent_id is not None and len(self._subagents) == 1:
            (only,) = self._subagents.values()
            return await self._attached(only)
        return None

    @staticmethod
    async def _attached(subagent: background.Subagent) -> background.Subagent:
        if subagent.sink is None:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(subagent.attached.wait(), _ATTACH_TIMEOUT)
        return subagent

    async def _announce_in(
        self, subagent: background.Subagent, call_id: str, name: str, tool_input: Mapping[str, Any]
    ) -> None:
        sink = subagent.sink
        if sink is None or call_id in subagent.announced:
            return
        subagent.announced.add(call_id)
        subagent.inputs[call_id] = (name, dict(tool_input))
        display, _ = describe(name, tool_input)
        await sink.tool_call_started(call_id, name, dict(tool_input), display_name=display)
        await sink.tool_call_delta(call_id, invocation_message=progress_line(name, tool_input))

    async def _to_subagent(self, item: Any) -> bool:
        """Divert a message from a subagent to its worker chat."""
        parent = getattr(item, "parent_tool_use_id", None)
        if not isinstance(parent, str) or not isinstance(
            item, StreamEvent | AssistantMessage | SdkUserMessage
        ):
            return False
        subagent = await self._subagent_of(parent)
        if subagent is None:
            return False
        if subagent.sink is None:
            subagent.pending.append(item)
        else:
            await self._route_to_subagent(subagent, item)
        return True

    async def _route_to_subagent(self, subagent: background.Subagent, item: Any) -> None:
        """The parent-turn handlers' work, against the worker chat's own sink."""
        sink = subagent.sink
        if sink is None:
            return
        try:
            if isinstance(item, StreamEvent):
                event = item.event
                if event.get("type") == "message_start":
                    message_id = event.get("message", {}).get("id")
                    subagent.current_message = message_id if isinstance(message_id, str) else None
                elif event.get("type") == "content_block_delta":
                    delta = event.get("delta", {})
                    text = delta.get("text") or delta.get("thinking")
                    if text and subagent.current_message is not None:
                        subagent.streamed.add(subagent.current_message)
                    if delta.get("type") == "text_delta" and text:
                        await sink.text_delta(text)
                    elif delta.get("type") == "thinking_delta" and text:
                        await sink.reasoning_delta(text)
            elif isinstance(item, AssistantMessage):
                streamed = item.message_id is not None and item.message_id in subagent.streamed
                for block in item.content:
                    if isinstance(block, ToolUseBlock):
                        await self._announce_in(subagent, block.id, block.name, block.input)
                    elif not streamed and isinstance(block, TextBlock) and block.text:
                        await sink.text_delta(block.text)
                    elif not streamed and isinstance(block, ThinkingBlock) and block.thinking:
                        await sink.reasoning_delta(block.thinking)
            elif isinstance(item, SdkUserMessage) and not isinstance(item.content, str):
                for block in item.content:
                    if not isinstance(block, ToolResultBlock):
                        continue
                    if block.tool_use_id not in subagent.announced:
                        continue
                    failed = bool(block.is_error)
                    text = _text_of(block.content)
                    await self._report_answer_from_result(block.tool_use_id, failed, text)
                    name, tool_input = subagent.inputs.get(block.tool_use_id, ("", {}))
                    content: list[dict[str, Any]] = [{"type": "text", "text": text}]
                    diff = await self._file_edit(sink, block.tool_use_id, success=not failed)
                    if diff is not None:
                        content.append(dict(diff))
                    await sink.tool_call_completed(
                        block.tool_use_id,
                        {"content": content},
                        success=not failed,
                        past_tense_message=past_tense(name, tool_input, failed=failed),
                    )
        except Exception:
            # The worker turn may have been stopped under us; the parent and
            # the rest of the stream carry on regardless.
            log.debug("a subagent message could not be shown", exc_info=True)

    def _is_elsewhere(self, item: SdkUserMessage) -> bool:
        """A replayed message we did not send: typed on claude.ai, or injected."""
        return item.origin is not None and item.uuid is not None and item.uuid not in self._sent

    async def _on_message(self, item: Any) -> None:
        if await self._to_subagent(item):
            return
        cost: float | None = None
        if isinstance(item, SdkUserMessage):
            if self._is_elsewhere(item):
                await self._on_message_from_elsewhere(item)
                return
            self._took_in(item)
        elif isinstance(item, SystemMessage):
            session_id = item.data.get("session_id")
            if item.subtype == "init":
                if isinstance(session_id, str):
                    self.claude_session_id = session_id
                await self._on_init(item.data)
            elif item.subtype == "status" and "permissionMode" in item.data:
                await self._on_mode_elsewhere(item.data["permissionMode"])
            elif item.subtype == "compact_boundary":
                await self._on_compacted(item.data)
            elif item.subtype == "commands_changed":
                commands = item.data.get("commands")
                if isinstance(commands, list):
                    self._sources.commands = [c for c in commands if isinstance(c, Mapping)]
                    await self._publish_customizations()
            elif item.subtype == "permission_denied":
                # Before the turn check: a subagent's prompt, in its own chat,
                # can be answered with no turn running here.
                call_id = item.data.get("tool_use_id")
                if isinstance(call_id, str):
                    await self._report_answer(call_id, denied=True)
                return
            elif item.subtype in _TASK_SUBTYPES:
                # Before the turn check below: a background task ends whenever
                # it ends, usually with no turn running.
                await self._on_task(item.subtype, item.data)
        elif isinstance(item, ResultMessage):
            self.claude_session_id = item.session_id or self.claude_session_id
            cost = self._cost_of(item)
            if self._on_model_usage is not None:
                self._on_model_usage(item.model_usage)
            self._refresh_soon()
            if self._stale_results:
                self._stale_results -= 1
                if item.terminal_reason in _ABORTED:
                    return  # owed by a turn that already ended here
                self._stale_results = 0
        turn = self._turn
        if turn is None:
            return
        if turn.sink is None:
            try:
                await asyncio.wait_for(turn.attached.wait(), _ATTACH_TIMEOUT)
            except TimeoutError:
                log.warning("a turn from elsewhere never reached the host; dropping it")
                self._finish(turn)
                return
        sink = turn.sink
        assert sink is not None
        if isinstance(item, AssistantMessage | SdkUserMessage) and item.parent_tool_use_id is None:
            self._recorded(turn, item)
        if isinstance(item, StreamEvent):
            await self._on_stream_event(item, sink)
        elif isinstance(item, AssistantMessage):
            turn.model = item.model or turn.model
            if item.parent_tool_use_id is None and isinstance(item.usage, Mapping):
                turn.last_request = item.usage
            if item.parent_tool_use_id is None and item.error is not None:
                turn.api_error = str(item.error)
            await self._on_assistant(item, sink)
        elif isinstance(item, SdkUserMessage):
            await self._on_tool_results(item, sink)
        elif isinstance(item, ResultMessage):
            await self._on_result(item, sink, turn, cost)
            if not self._steers_pending:
                self._finish(turn)

    def _recorded(self, turn: _Turn, item: AssistantMessage | SdkUserMessage) -> None:
        """A transcript entry of *turn*: the latest place a rewind could keep it to.

        Also the sign a pending cut has been taken: the conversation has a
        message after the point it was cut at, so a plain resume now loads
        the cut one (`history.py`).
        """
        entry = getattr(item, "uuid", None)
        if not isinstance(entry, str) or not entry:
            return
        if turn.mark is not None:
            turn.mark.last = entry
        self.rewind = None

    async def _on_compacted(self, data: Mapping[str, Any]) -> None:
        """Claude Code compacted the conversation: a note in the turn it happened in."""
        sink = self._sink
        if sink is None:
            return
        meta = data.get("compact_metadata")
        meta = meta if isinstance(meta, Mapping) else {}
        trigger, before = meta.get("trigger"), meta.get("pre_tokens")
        how = "automatically" if trigger == "auto" else "on request" if trigger == "manual" else ""
        size = f" from {before:,} tokens" if isinstance(before, int) and before > 0 else ""
        text = " ".join(part for part in ("Conversation compacted", how) if part) + size
        details = {
            key: meta[key] for key in ("trigger", "pre_tokens", "post_tokens") if key in meta
        }
        with contextlib.suppress(Exception):
            await sink.system_notification(text, meta=_note_meta("compaction", **details))

    def _cost_of(self, item: ResultMessage) -> float | None:
        """This result's share of the running cost total, when it can be told.

        The total is the CLI's, "cumulative across turns": a turn's cost is
        how far it moved. A total that went down (a fresh process that kept no
        total) or a baseline never known gives no figure, only a new baseline.
        """
        total = item.total_cost_usd
        if not isinstance(total, int | float) or isinstance(total, bool):
            return None
        baseline, self.cost_baseline = self.cost_baseline, float(total)
        if baseline is None or total < baseline:
            return None
        return float(total) - baseline

    def _refresh_soon(self) -> None:
        """Ask for the MCP servers' states again, off the reader: a turn may have moved them."""
        if self.mirror_of is not None or self._client is None:
            return
        if self._refreshing is not None and not self._refreshing.done():
            return
        self._refreshing = asyncio.create_task(self._refresh_mcp())

    async def _on_message_from_elsewhere(self, item: SdkUserMessage) -> None:
        text = _text_of(item.content).strip()
        turn = self._turn
        if turn is None:
            await self._external(text, item.uuid)
        elif turn.sink is not None:
            # Typed on claude.ai while a turn runs here, or injected by Claude
            # Code (a background task reporting back): it takes it in like a
            # steered message, so it is noted where it landed.
            origin: Mapping[str, Any] = item.origin or {}
            kind = origin.get("kind")
            if kind == "task-notification":
                note = f"Background task update: {_first_line(text)}"
            else:
                note = f"Sent from another device: {text}"
            await turn.sink.system_notification(note, meta=_note_meta("message", origin=kind))

    def _took_in(self, item: SdkUserMessage) -> None:
        """A replay of our own message: if it is a steered one, it has joined."""
        if item.uuid is not None:
            self._sent.discard(item.uuid)
            self._steers_pending.discard(item.uuid)

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
            await self._report_answer_from_result(block.tool_use_id, failed, text)
            await self._questions_answered_elsewhere(
                block.tool_use_id, failed, item.tool_use_result
            )
            name, tool_input = self._inputs.get(block.tool_use_id, ("", {}))
            content: list[dict[str, Any]] = [{"type": "text", "text": text}]
            worker = self._worker_chats.get(block.tool_use_id)
            if worker is not None:
                # The forward edge of the worker chat's `tool` origin.
                resource, title = worker
                content.append({"type": "subagent", "resource": resource, "title": title})
            subagent = self._subagents.get(block.tool_use_id)
            if subagent is not None and not subagent.task.backgrounded:
                # A foreground subagent answers its call when it is done; one
                # moved to the background answers at once and carries on.
                del self._subagents[block.tool_use_id]
                subagent.done.set()
            diff = await self._file_edit(sink, block.tool_use_id, success=not failed)
            if diff is not None:
                content.append(dict(diff))
            await sink.tool_call_completed(
                block.tool_use_id,
                {"content": content},
                success=not failed,
                past_tense_message=past_tense(name, tool_input, failed=failed),
            )

    async def _on_result(
        self, item: ResultMessage, sink: TurnSink, turn: _Turn, cost: float | None
    ) -> None:
        reported = usage_report(
            item.usage,
            turn.last_request,
            model=turn.model,
            model_usage=item.model_usage,
            cost=cost,
            total_cost=item.total_cost_usd,
        )
        await sink.usage(
            input_tokens=reported.input_tokens,
            output_tokens=reported.output_tokens,
            cache_read_tokens=reported.cache_read_tokens,
            model=reported.model,
            meta=reported.meta,
        )
        if item.terminal_reason in _ABORTED:
            return  # the user pressed stop; the host already knows
        if item.is_error:
            detail = "; ".join(item.errors or []) or item.result or item.subtype
            await sink.turn_failed(
                detail,
                error_type=f"claude.{item.subtype}",
                resumable=_transient(item.api_error_status, turn.api_error),
            )


#: A completion is best-effort and the client asks again on the next key.
_COMPLETION_TIMEOUT: Final = 2.0


def _names(value: Any) -> set[str]:
    if not isinstance(value, Sequence) or isinstance(value, str):
        return set()
    return {item for item in value if isinstance(item, str)}


#: HTTP statuses of a failed request that are worth trying again: a timeout,
#: too many requests, the server failing or overloaded (Anthropic's 529).
_TRANSIENT_STATUS: Final = frozenset({408, 429, 500, 502, 503, 504, 529})
#: Claude Code's names for the same (`AssistantMessage.error`).
_TRANSIENT_ERRORS: Final = frozenset({"rate_limit", "overloaded", "server_error"})


def _transient(status: int | None, error: str | None) -> bool:
    """Whether a turn that failed this way can be resumed as it stands.

    Only for failures of the request itself, after Claude Code's own retries:
    the conversation up to the failed request is intact, and asking again is
    what anyone would do. Not for anything the same request would fail on
    again - a refused login, a bad request, a model that does not exist.
    """
    return status in _TRANSIENT_STATUS or error in _TRANSIENT_ERRORS


def _first_line(text: str, limit: int = 200) -> str:
    line = text.strip().splitlines()[0] if text.strip() else ""
    return line if len(line) <= limit else line[: limit - 1] + "…"


def _note_meta(kind: str, **details: Any) -> dict[str, Any]:
    """A system notification's `_meta`: what it is about, for a client to file it by."""
    return {"claudeCode": {"kind": kind, **{k: v for k, v in details.items() if v is not None}}}


async def _optional(client: Any, method: str, *args: Any) -> Any:
    """Call a client method that not every client has; None if it fails or is absent."""
    call = getattr(client, method, None)
    if call is None:
        return None
    try:
        return await call(*args)
    except Exception:
        log.debug("%s failed", method, exc_info=True)
        return None


def _states_only(
    before: Sequence[Mapping[str, Any]] | None, after: Sequence[Mapping[str, Any]]
) -> list[tuple[str, Mapping[str, Any]]] | None:
    """The MCP servers whose state alone changed, or None if anything else did."""
    if before is None or len(before) != len(after):
        return None
    changed: list[tuple[str, Mapping[str, Any]]] = []

    def walk(old: Mapping[str, Any], new: Mapping[str, Any]) -> bool:
        if {k: v for k, v in old.items() if k not in ("state", "children")} != {
            k: v for k, v in new.items() if k not in ("state", "children")
        }:
            return False
        if old.get("state") != new.get("state"):
            if new.get("type") != "mcpServer":
                return False
            changed.append((str(new["id"]), new["state"]))
        old_children, new_children = old.get("children") or [], new.get("children") or []
        if len(old_children) != len(new_children):
            return False
        return all(walk(a, b) for a, b in zip(old_children, new_children, strict=True))

    if not all(walk(a, b) for a, b in zip(before, after, strict=True)):
        return None
    return changed


class LocalClaudeSession(ClaudeSession):
    """A session this host runs: one that can also be rewound, reconfigured and split into chats.

    Split from `ClaudeSession` because a claude.ai mirror can do none of the
    things below - its Claude Code runs on another machine - and the host
    feature-detects each by whether the method exists (`TruncatesHistory`,
    `ManagesMcpServers`, `HandlesCustomizations`, `HostsChats`, `ResumesTurns`).

    **Chats.** Each of the session's chats is its own Claude Code conversation
    (`HostsChats`): the default chat's is this object's, and every other
    chat's is a `LocalClaudeSession` of its own, held here and run on its own
    Claude process, with its own transcript marks and resume state. The host
    talks to this object only; it routes each turn, steer, truncation, resume
    and stop by chat (`UserMessage.chat_uri`, `CancelsChats`), and passes on
    to every chat what is true of the session (approval mode, effort,
    folders, clients, archiving). What is the session's alone - Remote
    Control, the customization tree, MCP servers - stays here.
    """

    def __init__(
        self,
        context: AgentSessionContext,
        *,
        chat_states: Mapping[str, Any] | None = None,
        on_chat: Callable[[str, ClaudeSession | None], None] | None = None,
        **fields: Any,
    ) -> None:
        super().__init__(context, **fields)
        #: The default chat's session, when this is another chat's.
        self._parent: LocalClaudeSession | None = None
        #: The session's other chats, by URI.
        self._chats: dict[str, LocalClaudeSession] = {}
        #: What each was saved as, for its `chat_opened(restored=True)`.
        self._chat_states: dict[str, Any] = dict(chat_states or {})
        #: Tells the provider which session answers a chat (completions).
        self._on_chat = on_chat

    # -- chats -------------------------------------------------------------------

    @property
    def chats(self) -> Mapping[str, LocalClaudeSession]:
        return self._chats

    def _existing(self, chat_uri: str | None) -> LocalClaudeSession | None:
        """The conversation of *chat_uri*, if this session has it."""
        if chat_uri is None or chat_uri == self.context.chat_uri:
            return self
        return self._chats.get(chat_uri)

    def _routed(self, chat_uri: str | None) -> LocalClaudeSession:
        """*chat_uri*'s conversation, opened from what was saved if no one announced it."""
        found = self._existing(chat_uri)
        if found is not None:
            return found
        assert chat_uri is not None
        return self._open_chat(
            ChatContext(session_uri=self.context.session_uri, chat_uri=chat_uri, restored=True)
        )

    async def chat_opened(self, context: ChatContext) -> None:
        """A new chat in the session, or one that existed before a restart (`HostsChats`).

        A fork starts from the source chat's Claude Code conversation, cut at
        the turn it forked at and copied (`fork_session`), so the source stays
        as its chat has it; a side chat the same, with none of the source's
        turns in its own history. A source this adapter cannot cut - its turn
        predates the marks, or it is not running here - is given to Claude as
        its transcript instead. A restored chat picks up its saved
        conversation, and starts its Claude process with its first turn.
        """
        if self._parent is not None:
            await self._parent.chat_opened(context)
            return
        if self._existing(context.chat_uri) is not None:
            return
        self._open_chat(context)
        if not context.restored:
            await self._save()

    def _open_chat(self, context: ChatContext) -> LocalClaudeSession:
        saved = self._chat_states.get(context.chat_uri) if context.restored else None
        child = LocalClaudeSession(
            AgentSessionContext(
                session_uri=self.context.session_uri,
                chat_uri=context.chat_uri,
                provider_id=self.context.provider_id,
                working_directories=self.working_directories,
                model=self._model,
                config=self.context.config,
                publisher=self.context.publisher,
            ),
            root=self._roots,
            client_factory=self._client_factory,
            approvals=self.approvals,
            effort=self.effort,
            # Claude Code keeps a conversation under the folder it started in;
            # a fork must start where its source lives.
            directory=self.directory,
            archived=self.archived,
            chat_dir=self._chat_dir,
            chat_tools=self._chat_tools,
            commands=self._sources.commands or (),
            on_model_usage=self._on_model_usage,
        )
        child._parent = self
        child._scope = context.chat_uri
        child.disabled = self.disabled  # one set: a toggle is the session's
        child._tree = self._tree
        child._client_tools = self._client_tools
        if isinstance(saved, Mapping):
            _restore_conversation(child, saved)
        # The host's word on the chat's folders, restored or not.
        child.subset = (
            tuple(context.working_directories) if context.working_directories is not None else None
        )
        if not isinstance(saved, Mapping):
            source = context.fork or context.side_chat
            if source is not None:
                origin = self._existing(source.chat_uri) if source.chat_uri is not None else None
                _branch(child, origin, source, side=context.fork is None)
        self._chats[context.chat_uri] = child
        if self._on_chat is not None:
            self._on_chat(context.chat_uri, child)
        return child

    async def chat_closed(self, chat_uri: str) -> None:
        """The chat was disposed, or moved away: its Claude process goes (`HostsChats`)."""
        if self._parent is not None:
            await self._parent.chat_closed(chat_uri)
            return
        child = self._chats.pop(chat_uri, None)
        self._chat_states.pop(chat_uri, None)
        if child is not None:
            await child.aclose()
        if self._on_chat is not None:
            self._on_chat(chat_uri, None)
        await self._save()

    async def cancel_chat(self, chat_uri: str, reason: str | None = None) -> None:
        """Stop one chat's turn, and only that one (`CancelsChats`)."""
        target = self._existing(chat_uri)
        if target is not None:
            await ClaudeSession.cancel(target, reason)

    async def chat_working_directories_changed(
        self, chat_uri: str, directories: Sequence[str]
    ) -> None:
        """A chat's own folders changed (`FollowsChatWorkingDirectories`).

        Security-relevant, like the session's folders: what the chat's Claude
        may touch, and whether its gate is put in Ask (`_gate`). Restarts its
        Claude process on the same conversation if that changes anything.
        """
        target = self._existing(chat_uri)
        if target is None:
            return
        await target._set_folders(target.working_directories, tuple(directories))
        await self._save()

    async def send_user_message(self, message: UserMessage, sink: TurnSink) -> None:
        if self._parent is None:
            target = self._routed(message.chat_uri)
            if target is not self:
                await target.send_user_message(message, sink)
                return
        await super().send_user_message(message, sink)

    async def steer(self, chat_uri: str, message: UserMessage) -> bool:
        target = self._existing(chat_uri) if self._parent is None else self
        if target is None:
            return False
        return await ClaudeSession.steer(target, chat_uri, message)

    async def resume_turn(self, chat_uri: str, turn_id: str, sink: TurnSink) -> None:
        """Pick up a turn that failed on something transient (`ResumesTurns`).

        Only a failure that leaves the conversation intact is offered as
        resumable (`_transient`, or the Claude process ending): Claude Code's
        transcript holds the turn up to where it stopped, and resuming starts
        Claude Code on it again if it is not running and asks Claude to carry
        on - a short message Claude sees as the user's, which is how Claude
        Code itself continues after an API error. The transcript here shows
        no new message: what Claude says next is added to the same turn.
        """
        target = self._routed(chat_uri) if self._parent is None else self
        await target._resume(turn_id, sink)

    async def _resume(self, turn_id: str, sink: TurnSink) -> None:
        async with self._lock:
            client = await self._client_for(sink)
            if client is None:
                return
            turn = _Turn(sink=sink, client=client)
            turn.attached.set()
            self._begin(turn)
            turn.model = self._model
            mark = next((m for m in reversed(self.marks) if m.turn == turn_id), None)
            try:
                await self._query(client, turn, sink, RESUME_PROMPT, resuming=mark)
            finally:
                self._accepting_steers = False
                self._steers_pending.clear()
                self._abandon(turn)

    # -- what is the session's, passed on to every chat --------------------------

    async def config_changed(self, values: Mapping[str, Any]) -> None:
        await super().config_changed(values)
        shared = {key: values[key] for key in (CONFIG_KEY, EFFORT_KEY) if key in values}
        if self._parent is None and shared:
            for child in list(self._chats.values()):
                await ClaudeSession.config_changed(child, shared)

    async def working_directories_changed(self, directories: Sequence[str]) -> None:
        await super().working_directories_changed(directories)
        if self._parent is None:
            for child in list(self._chats.values()):
                await child._set_folders(tuple(directories), child.subset)

    async def active_clients_changed(self, clients: Sequence[Mapping[str, Any]]) -> None:
        await super().active_clients_changed(clients)
        if self._parent is None:
            for child in list(self._chats.values()):
                await ClaudeSession.active_clients_changed(child, clients)

    async def archived_changed(self, is_archived: bool) -> None:
        await super().archived_changed(is_archived)
        if self._parent is None:
            for child in list(self._chats.values()):
                child.archived = is_archived
                if is_archived:
                    await child._stop_client()

    async def aclose(self) -> None:
        children, self._chats = list(self._chats.values()), {}
        for child in children:
            await child.aclose()
        await super().aclose()

    async def _save(self) -> None:
        if self._parent is not None:
            await self._parent._save()
            return
        await super()._save()

    async def _follow_mode(self, mode: str) -> None:
        """The approval mode is the session's: every chat follows it."""
        if self._parent is not None:
            await self._parent._follow_mode(mode)
            return
        await super()._follow_mode(mode)
        for child in list(self._chats.values()):
            if child.approvals == mode:
                continue
            child.approvals = mode
            if child._client is not None:
                with contextlib.suppress(Exception):
                    await child._client.set_permission_mode(PERMISSION_MODES[child._gate()])

    async def _publish_customizations(self) -> None:
        """The tree is the session's: only the default chat's session publishes it."""
        if self._parent is not None:
            parent = self._parent._tree
            self._tree = parent if parent.customizations else (self._build_tree() or parent)
            return
        await super()._publish_customizations()
        for child in self._chats.values():
            child._tree = self._tree

    def _refresh_soon(self) -> None:
        if self._parent is None:
            super()._refresh_soon()

    async def _changed(self, change: FileChange) -> None:
        """A chat's changeset is its own (`chat=`); the session's covers every chat."""
        if self._parent is None:
            await super()._changed(change)
            return
        publisher = self.context.publisher
        if publisher is not None:
            try:
                await publisher.changes_published(
                    self._edits.changeset, self._edits.changes(), chat=self._scope
                )
            except Exception:
                log.exception("publishing a chat's changes failed")
        self._parent._edits.record(change)
        await ClaudeSession._changed(self._parent, change)

    async def _set_folders(
        self, directories: Sequence[str], subset: tuple[str, ...] | None
    ) -> None:
        """New folders for this conversation: restart its client if its access moved."""
        try:
            before: Any = self._access()
        except PermissionError:
            before = None
        gate_before = self._gate()
        self.working_directories = tuple(directories)
        self.subset = subset
        try:
            after: Any = self._access()
        except PermissionError:
            after = None
        if self._client is None or (before == after and gate_before == self._gate()):
            return
        self._restart_pending = True
        if not self._lock.locked() and self._turn is None:
            await self._restart_client()

    # -- edit-and-resend -------------------------------------------------------

    async def history_truncated(self, chat: str, turn_id: str | None) -> None:
        """Forget every turn after *turn_id*, or all of them (`TruncatesHistory`).

        The host has already cancelled any turn running in the chat; this
        waits for it to finish unwinding, then lets the Claude process go and
        notes the cut, which the next start takes (`history.py`). A session on
        claude.ai is started again straight away, at the cut.

        Files are not rewound. Claude Code could put back the files its own
        edit tools changed (`enable_file_checkpointing`, `rewind_files`), but
        not what a shell command did, and it would also put back anything
        changed by hand since - silently undoing someone's work to match a
        transcript. Instead Claude is told, with its next prompt, that the
        conversation was rewound and the files were not.
        """
        if chat != self.context.chat_uri:
            target = self._chats.get(chat) if self._parent is None else None
            if target is not None:
                await target.history_truncated(chat, turn_id)
                return
            # A worker chat's turns are a subagent's, which nothing here can
            # rewind; its chat is read-only besides.
            log.info("not rewinding %s: it is no chat of this session's own", chat)
            return
        async with self._lock:
            cut = history.plan(self.marks, turn_id, pending=self.rewind is not None)
            if cut.changes_nothing:
                return
            # The old process first: whatever it says from now on must not
            # count as the cut having been taken.
            await self._stop_client()
            if cut.forget_all:
                self.claude_session_id = None
                self.marks = []
                self.rewind = None
                self.cost_baseline = 0.0
            else:
                self.marks = list(cut.keep)
                rewind = cut.rewind if self.claude_session_id is not None else None
                if rewind is not None and self.rewind is not None and self.rewind.fork:
                    # Still the source chat's conversation until the fork is
                    # taken: cut a copy of it, never the original.
                    rewind = Rewind(at=rewind.at, fork=True)
                self.rewind = rewind
            self.rewound = True
            self._recap = None
        await self._save()
        if self.remote_control and not self.archived:
            self.start_soon()

    # -- MCP servers and customizations ---------------------------------------

    def _mcp_name(self, customization_id: str) -> str | None:
        return self._tree.mcp.get(customization_id)

    def _mcp_status(self, name: str) -> str | None:
        for server in self._sources.mcp or ():
            if server.get("name") == name:
                status = server.get("status")
                return status if isinstance(status, str) else None
        return None

    async def start_mcp_server(self, customization_id: str) -> None:
        """Start, or reconnect, one of Claude Code's MCP servers (`ManagesMcpServers`).

        One switched off is switched back on - which, as with Claude Code's
        own ``/mcp``, is for every session in the project - and any other is
        reconnected. Its new state is published either way, so a refusal
        shows as the state it is really in.
        """
        name = self._mcp_name(customization_id)
        if name is None:
            return
        client = await self._ensure_client()
        try:
            if self._mcp_status(name) == "disabled":
                await client.toggle_mcp_server(name, True)  # type: ignore[attr-defined]
            else:
                await client.reconnect_mcp_server(name)  # type: ignore[attr-defined]
        except Exception:
            log.warning("starting MCP server %s failed", name, exc_info=True)
        await self._republish_mcp()

    async def stop_mcp_server(self, customization_id: str) -> None:
        """Switch one of Claude Code's MCP servers off, as its ``/mcp`` does."""
        name = self._mcp_name(customization_id)
        if name is None:
            return
        client = await self._ensure_client()
        try:
            await client.toggle_mcp_server(name, False)  # type: ignore[attr-defined]
        except Exception:
            log.warning("stopping MCP server %s failed", name, exc_info=True)
        await self._republish_mcp()

    async def _republish_mcp(self) -> None:
        """Publish where the servers really are now, over the reducer's guess."""
        client = self._client
        if client is not None:
            await self._read_mcp(client)
        self._published = None  # whatever the client was told, say it again
        await self._publish_customizations()

    async def customization_toggled(self, customization_id: str, enabled: bool) -> None:
        """A client switched a customization on or off (`HandlesCustomizations`).

        * An MCP server is switched in Claude Code itself, as `start`/`stop`.
        * A skill, or a plugin's skills, become deny rules (``Skill(name)``),
          so the Skill tool refuses them; Claude Code reads those at start-up,
          so it restarts on the same conversation, now if idle, else after the
          turn in flight. The choice is kept with the session.
        * Anything else - an agent, a directory - Claude Code cannot switch
          off, so the tree is published again as it is: the toggle visibly
          does not take, rather than looking as if it did.
        """
        if customization_id in self._tree.mcp:
            if enabled:
                await self.start_mcp_server(customization_id)
            else:
                await self.stop_mcp_server(customization_id)
            return
        skillish = customization_id in self._tree.skills or (
            customization_id in self._tree.plugin_skills
        )
        if not skillish:
            self._published = None
            await self._publish_customizations()
            return
        if enabled:
            self.disabled.discard(customization_id)
        else:
            self.disabled.add(customization_id)
        await self._save()
        for session in (self, *self._chats.values()):
            if session._client is None or session._denied_skills() == session._started_denied:
                continue
            session._restart_pending = True
            if not session._lock.locked() and session._turn is None:
                await session._restart_client()


#: What Claude is told when a failed turn is resumed (`ResumesTurns`).
RESUME_PROMPT: Final = (
    "[Your previous response was cut off by an error before you finished. "
    "Carry on from where you stopped.]"
)


def _branch(
    target: ClaudeSession,
    origin: ClaudeSession | None,
    source: ForkedFrom,
    *,
    side: bool,
    check_folder: bool = False,
) -> None:
    """Start *target* from *origin*'s conversation, as it was at *source*'s turn.

    A copy (`fork_session`) cut at the turn's last transcript entry, with the
    turns it keeps marked as *origin* marked them (a fork's copied turns keep
    their ids); a side chat keeps none in its own history. If *origin* cannot
    be cut there - not running here, never started, or the turn predates its
    marks - or (*check_folder*) its conversation lives in a folder *target*
    was not given, *target* starts a new conversation with the transcript the
    client shows as its seed.
    """
    if origin is not None and origin.claude_session_id is not None:
        running = origin._turn.mark if origin._turn is not None else None
        point = history.branch_point(origin.marks, source.turn_id, running=running)
        folder = origin.directory
        allowed = not check_folder or (folder is not None and _may_live_in(target, folder))
        if point is not None and allowed:
            kept, at = point
            target.claude_session_id = origin.claude_session_id
            if folder is not None:
                target.directory = folder
            target.rewind = Rewind(at=at, fork=True)
            target.marks = [] if side else [TurnMark(m.turn, m.prompt, m.last) for m in kept]
            target.cost_baseline = None
            return
    target.seed = transcript(source.turns) or None


def _may_live_in(session: ClaudeSession, folder: Path) -> bool:
    """Whether *session* may run with its `cwd` in *folder*: one of its own folders."""
    try:
        granted = session._granted()
    except PermissionError:
        return False
    real = folder.resolve()
    return any(real == path or path in real.parents for path in granted)


def _conversation_state(session: ClaudeSession) -> dict[str, Any]:
    """One conversation's part of the resume state: a chat's, or the default chat's."""
    state: dict[str, Any] = {}
    if session.claude_session_id:
        state["claudeSessionId"] = session.claude_session_id
    if session.directory is not None:
        state["cwd"] = str(session.directory)
    if session.marks:
        state["turns"] = [mark.to_wire() for mark in session.marks]
    if session.rewind is not None:
        state["rewind"] = session.rewind.to_wire()
    if session.rewound:
        state["rewound"] = True
    if session.cost_baseline:
        state["costUsd"] = session.cost_baseline
    if session.subset is not None:
        state["folders"] = list(session.subset)
    if session.seed is not None:
        state["seed"] = session.seed
    return state


def _restore_conversation(session: ClaudeSession, state: Mapping[str, Any]) -> None:
    """The reverse of `_conversation_state`, onto a session just built."""
    session_id = state.get("claudeSessionId")
    session.claude_session_id = session_id if isinstance(session_id, str) else None
    cwd = state.get("cwd")
    if isinstance(cwd, str):
        session.directory = Path(cwd)
    marks = state.get("turns")
    session.marks = [
        mark
        for mark in (TurnMark.from_wire(m) for m in (marks if isinstance(marks, list) else ()))
        if mark is not None
    ]
    session.rewind = Rewind.from_wire(state.get("rewind"))
    session.rewound = state.get("rewound") is True
    cost = state.get("costUsd")
    session.cost_baseline = (
        float(cost)
        if isinstance(cost, int | float) and not isinstance(cost, bool)
        else (0.0 if session.claude_session_id is None else None)
    )
    folders = state.get("folders")
    if isinstance(folders, list):
        session.subset = tuple(f for f in folders if isinstance(f, str))
    seed = state.get("seed")
    session.seed = seed if isinstance(seed, str) else None


#: Claude Code permission mode -> approval mode.
_APPROVAL_MODES: Final[Mapping[str, str]] = {
    claude: approval for approval, claude in PERMISSION_MODES.items()
}

#: A result for a turn someone stopped.
_ABORTED = ("aborted_streaming", "aborted_tools")


class ClaudeProvider:
    """One host, one provider: Claude Code, rooted at one directory."""

    def __init__(
        self,
        root: Path | Roots,
        *,
        display_name: str = "Claude",
        client_factory: ClientFactory = _default_client,
        models: Sequence[ModelInfo] = (),
        provider_id: str = PROVIDER_ID,
        sessions: ClaudeCodeSessions | None = None,
        remote_control: bool = False,
        claude_ai: Api | None = None,
        claude_ai_scope: str = LOCAL,
        state_dir: Path | None = None,
        poll_s: float = 15.0,
        chat_dir: Path | None = None,
        machine: str | None = None,
        running_here: Callable[[], Mapping[str, str]] = running_here,
        status_on_claude_ai: Callable[[str], Awaitable[str | None]] | None = None,
        chat_tools: Sequence[str] = CHAT_TOOLS,
        commands: Sequence[Mapping[str, Any]] = (),
        rediscover: bool = False,
    ) -> None:
        if not is_valid_provider_id(provider_id):
            raise ValueError(f"invalid provider id: {provider_id!r}")
        self.provider_id = provider_id
        self.roots = as_roots(root)
        self.root = self.roots.primary
        self._display_name = display_name
        self._client_factory = client_factory
        self._models = tuple(models)
        self.sessions = sessions if sessions is not None else ClaudeCodeSessions(self.roots)
        #: Whether a new session is on claude.ai unless its creator says not.
        self.remote_control = remote_control
        #: Set to list the account's other Remote Control sessions here too.
        self._claude_ai = claude_ai
        self._claude_ai_status: Api | None = None
        self._status_override = status_on_claude_ai
        #: `LOCAL`: only sessions running on this machine, so each machine's
        #: node lists its own - the gateway files them under it, and several
        #: machines can list at once without listing anything twice. `ALL`:
        #: every session on the account, for machines that run no node.
        if claude_ai_scope not in (LOCAL, ALL):
            raise ValueError(f"claude_ai_scope must be {LOCAL!r} or {ALL!r}")
        self._scope = claude_ai_scope
        self._machine = (machine or this_machine()).casefold()
        self._running_here = running_here
        self._poll_s = poll_s
        self._directory: SessionDirectory | None = None
        self._poller: asyncio.Task[None] | None = None
        #: One sync at a time: two would both see a restored mirror as not yet
        #: started, and open it twice.
        self._sync_lock = asyncio.Lock()
        #: This host's own sessions, so their claude.ai twins are not listed twice.
        self._local: weakref.WeakSet[ClaudeSession] = weakref.WeakSet()
        self._mirrors: dict[str, ClaudeSession] = {}
        self._titles: dict[str, str] = {}
        #: Mirrors this host closed itself, as opposed to a person deleting one.
        self._closing: set[str] = set()
        self._dismissed_file = state_dir / "claude-ai-dismissed.json" if state_dir else None
        #: Where sessions with no folder run. Kept, not temporary: a chat's
        #: conversation is stored under it, and resuming needs it again.
        self.chat_dir = chat_dir or (state_dir or DEFAULT_STATE) / "chat"
        #: May only narrow `CHAT_TOOLS`: a session with no folder must never be
        #: handed a tool that touches the machine.
        self.chat_tools = chat_tools_subset(chat_tools)
        self._dismissed: set[str] = self._load_dismissed()
        #: Slash commands from start-up (`Discovery.commands`), for a session
        #: whose own Claude client has not said yet.
        self._commands = tuple(commands)
        #: Model limits results report, for the next start-up's picker.
        self.learned = Learned(state_dir / CACHE_FILE if state_dir is not None else None)
        #: Every session by its chat, for `complete`.
        self._by_chat: weakref.WeakValueDictionary[str, ClaudeSession] = (
            weakref.WeakValueDictionary()
        )
        self._state_dir = state_dir
        #: Ask Claude Code for the models again, if start-up could not.
        self._rediscover = rediscover
        self._rediscovery: asyncio.Task[None] | None = None
        #: The host's notifier (`UpdatesAgentInfo`), once it has given it.
        self._agent_changed: AgentInfoChanged | None = None
        self._announcing: set[asyncio.Task[Any]] = set()

    async def attach_agent_updates(self, changed: AgentInfoChanged) -> None:
        """The host's notifier for a changed picker (`UpdatesAgentInfo`).

        Two things change it after start-up: a model's limits learned from a
        session's results (`models.py`), and - if start-up discovery found no
        models (Claude Code not signed in yet, say) - discovery succeeding on
        a later try, with backoff, so the picker is not empty until a restart.
        """
        self._agent_changed = changed
        if self._rediscover and not self._models and self._rediscovery is None:
            self._rediscovery = asyncio.create_task(self._discover_again())

    def _learned_from(self, model_usage: Any) -> None:
        """A result's `model_usage`: kept for the next start, and published now."""
        if not self.learned.record(model_usage):
            return
        models = _with_learned(self._models, self.learned)
        if models != self._models:
            self._models = models
            self._announce_agent()

    def _announce_agent(self) -> None:
        changed = self._agent_changed
        if changed is None:
            return

        async def announce() -> None:
            try:
                await changed()
            except Exception:
                log.exception("publishing the agent's new models failed")

        task = asyncio.create_task(announce())
        self._announcing.add(task)
        task.add_done_callback(self._announcing.discard)

    async def _discover_again(self) -> None:
        for delay in _REDISCOVERY:
            await asyncio.sleep(delay)
            found = await discover(self.root, self._client_factory, state_dir=self._state_dir)
            if found.models:
                log.info("models: %s", ", ".join(m.id for m in found.models))
                self._models = found.models
                if not self._commands:
                    self._commands = found.commands
                self._announce_agent()
                return
        log.warning("Claude Code still names no models; the picker stays empty")

    #: What a host should advertise as `completionTriggerCharacters` for this
    #: provider (`Host(completion_trigger_characters=...)`).
    completion_trigger_characters: Final = completions.TRIGGERS

    async def complete(self, request: CompletionRequest) -> Sequence[CompletionItem]:
        """``/`` and ``@`` completions (`Completes`), from the chat's own session."""
        session = self._by_chat.get(request.chat)
        if session is None:
            return ()
        return await session.complete(request)

    @property
    def agent(self) -> AgentInfo:
        return AgentInfo(
            provider=self.provider_id,
            display_name=self._display_name,
            description="Claude Code, running on this machine as its user.",
            models=self._models,
            capabilities={
                "multipleWorkingDirectories": dict(WORKING_DIRECTORIES_CAPABILITY),
                # A conversation per chat; a fork or side chat copies its
                # source's (`LocalClaudeSession.chat_opened`).
                "multipleChats": {"fork": True, "sideChat": True},
            },
        )

    def _remote_control(self, value: Any) -> bool:
        return value if isinstance(value, bool) else self.remote_control

    async def resolve_config(self, request: ConfigRequest) -> ConfigResolution:
        """How tool calls are approved, which conversation (if any) to continue,
        and whether the session is also on claude.ai."""
        chosen = request.values.get(CONTINUE_KEY)
        return ConfigResolution(
            properties={
                CONFIG_KEY: APPROVALS_PROPERTY,
                CONTINUE_KEY: self.sessions.picker_property(await self.sessions.recent()),
                RC_CONFIG_KEY: rc_property(self.remote_control),
                EFFORT_KEY: dict(EFFORT_PROPERTY),
            },
            values={
                CONFIG_KEY: approval_mode(request.values.get(CONFIG_KEY)),
                CONTINUE_KEY: chosen if is_session_id(chosen) else NEW,
                RC_CONFIG_KEY: self._remote_control(request.values.get(RC_CONFIG_KEY)),
                EFFORT_KEY: effort_level(request.values.get(EFFORT_KEY)),
            },
        )

    async def complete_config(self, request: ConfigRequest) -> Sequence[ConfigValue]:
        """Search this machine's Claude Code conversations as the user types."""
        if request.property != CONTINUE_KEY:
            return ()
        found = await self.sessions.recent(request.query, limit=SEARCH_LIMIT)
        return [
            ConfigValue(
                value=info.session_id, label=title_of(info), description=describe_session(info)
            )
            for info in found
        ]

    async def _status_on_claude_ai(self, session_id: str) -> str | None:
        """For a local session's start: is its claude.ai session archived there?"""
        if self._status_override is not None:
            return await self._status_override(session_id)
        if self._claude_ai is None:
            # Only ever asked about a session already on claude.ai, so the
            # login this reads is one Claude Code here already has.
            self._claude_ai_status = self._claude_ai_status or Api()
            return await self._claude_ai_status.status(session_id)
        return await self._claude_ai.status(session_id)

    def _new_local(self, context: AgentSessionContext, **fields: Any) -> LocalClaudeSession:
        return LocalClaudeSession(
            context,
            root=self.roots,
            client_factory=self._client_factory,
            chat_dir=self.chat_dir,
            chat_tools=self.chat_tools,
            commands=self._commands,
            on_model_usage=self._learned_from,
            on_chat=self._register_chat,
            **fields,
        )

    def _register_chat(self, chat_uri: str, session: ClaudeSession | None) -> None:
        if session is None:
            self._by_chat.pop(chat_uri, None)
        else:
            self._by_chat[chat_uri] = session

    def _started(self, session: ClaudeSession) -> ClaudeSession:
        """A session on claude.ai must be reachable before its first message here.

        Not an archived one: starting it would reattach, which unarchives it.
        """
        self._by_chat[session.context.chat_uri] = session
        if session.mirror_of is None:
            self._local.add(session)
            session._status_on_claude_ai = self._status_on_claude_ai
        if session.remote_control and not session.archived:
            session.start_soon()
        return session

    async def create_session(self, context: AgentSessionContext) -> ClaudeSession:
        chosen = context.config.get(CONTINUE_KEY)
        remote_control = self._remote_control(context.config.get(RC_CONFIG_KEY))
        if not is_session_id(chosen):
            session = self._new_local(context, remote_control=remote_control)
            if context.fork is not None:
                # `createSession.fork`: the host has copied the source's turns
                # into this session, so its agent starts where they end.
                _branch(
                    session,
                    self._origin_of(context.fork),
                    context.fork,
                    side=False,
                    check_folder=True,
                )
            return self._started(session)
        continuation = await self.sessions.continue_from(chosen)
        return self._started(
            self._new_local(
                context,
                claude_session_id=continuation.session_id,
                directory=continuation.directory,
                recap=continuation.recap,
                remote_control=remote_control,
                # The conversation continued has cost what it has; this
                # session's turns are measured from its first result.
                cost_baseline=None,
            )
        )

    def _origin_of(self, fork: ForkedFrom) -> ClaudeSession | None:
        """The running conversation a fork names: its session's chat, if this host has it."""
        for session in list(self._local):
            if isinstance(session, LocalClaudeSession) and (
                session.context.session_uri == fork.session_uri
            ):
                return session._existing(fork.chat_uri)
        return None

    async def resume_session(self, context: AgentSessionContext) -> ClaudeSession:
        state = context.resume_state or {}
        mirrored = state.get(MIRROR_KEY)
        if isinstance(mirrored, str):
            return self._mirror(context, mirrored, backfill=state.get("backfill") is True)
        session_id = state.get("claudeSessionId")
        bridge = state.get("bridgeSessionId")
        marks = state.get("turns")
        cost = state.get("costUsd")
        disabled = state.get("disabled")
        chats = state.get("chats")
        session = self._started(
            self._new_local(
                context,
                chat_states=chats if isinstance(chats, Mapping) else None,
                claude_session_id=session_id if isinstance(session_id, str) else None,
                # The session's current config wins (a client may have changed
                # the mode since the last save); then the resume state, for a
                # host that passes no config back; a state from before
                # approvals existed resumes in `ask`.
                approvals=approval_mode(
                    context.config.get(
                        CONFIG_KEY, state.get(CONFIG_KEY, state.get("approvals", ASK))
                    )
                ),
                directory=Path(cwd) if isinstance(cwd := state.get("cwd"), str) else None,
                # Same order. A session from before Remote Control existed
                # follows today's default, like a new one.
                remote_control=self._remote_control(
                    context.config.get(RC_CONFIG_KEY, state.get(RC_CONFIG_KEY))
                ),
                bridge_session_id=bridge if isinstance(bridge, str) else None,
                archived=state.get("archived") is True,
                effort=effort_level(context.config.get(EFFORT_KEY, state.get(EFFORT_KEY))),
                marks=[
                    mark
                    for mark in (
                        TurnMark.from_wire(m) for m in (marks if isinstance(marks, list) else ())
                    )
                    if mark is not None
                ],
                rewind=Rewind.from_wire(state.get("rewind")),
                rewound=state.get("rewound") is True,
                cost_baseline=float(cost)
                if isinstance(cost, int | float) and not isinstance(cost, bool)
                else None,
                disabled=[d for d in disabled if isinstance(d, str)]
                if isinstance(disabled, list)
                else (),
            )
        )
        folders, seed = state.get("folders"), state.get("seed")
        if isinstance(folders, list):
            session.subset = tuple(f for f in folders if isinstance(f, str))
        session.seed = seed if isinstance(seed, str) else None
        return session

    async def resume_state_of(self, session: Any) -> Mapping[str, Any] | None:
        if not isinstance(session, ClaudeSession):
            return None
        if session.mirror_of is not None:
            return {MIRROR_KEY: session.mirror_of, CONFIG_KEY: session.approvals}
        chats = (
            {uri: _conversation_state(chat) for uri, chat in session.chats.items()}
            if isinstance(session, LocalClaudeSession)
            else {}
        )
        if (
            not session.claude_session_id
            and not session.bridge_session_id
            and not chats
            and session.seed is None
        ):
            return None
        state: dict[str, Any] = {
            CONFIG_KEY: session.approvals,
            RC_CONFIG_KEY: session.remote_control,
            **_conversation_state(session),
        }
        if session.effort != EFFORT_DEFAULT:
            state[EFFORT_KEY] = session.effort
        if session.bridge_session_id:
            state["bridgeSessionId"] = session.bridge_session_id
        if session.archived:
            state["archived"] = True
        if session.disabled:
            state["disabled"] = sorted(session.disabled)
        if chats:
            state["chats"] = chats
        return state

    # -- the account's other sessions, through claude.ai ------------------------

    async def attach_directory(self, directory: SessionDirectory) -> None:
        """The host's session list is ours to add to (`OpensSessions`).

        First, sessions restored from the last run are brought back: the host
        does that lazily, on a first turn, and one on claude.ai must be
        reachable before anyone here touches it. Then, if claude.ai sessions
        are wanted, they are watched.
        """
        self._directory = directory
        for uri in directory.uris():
            if uri.startswith(_MIRROR_URI):
                # Brought back by the first sync, and only if still active
                # on claude.ai.
                continue
            try:
                await directory.open(uri, title="", resume_state={})
            except Exception:
                log.exception("could not bring back %s", uri)
        if self._claude_ai is not None and self._poller is None:
            self._poller = asyncio.create_task(self._watch_claude_ai())

    async def aclose(self) -> None:
        rediscovery, self._rediscovery = self._rediscovery, None
        if rediscovery is not None:
            rediscovery.cancel()
        poller, self._poller = self._poller, None
        if poller is not None:
            poller.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await poller
        for api in (self._claude_ai, self._claude_ai_status):
            if api is not None:
                await api.aclose()

    async def _watch_claude_ai(self) -> None:
        warned = False
        while True:
            try:
                await self.sync_claude_ai()
                warned = False
            except asyncio.CancelledError:
                raise
            except LoginError as error:
                if not warned:
                    log.warning("claude.ai sessions: %s", error)
                    warned = True
            except Exception:
                log.exception("listing claude.ai sessions failed")
            await asyncio.sleep(self._poll_s)

    async def sync_claude_ai(self) -> None:
        """Match the host's list to the account's live Remote Control sessions.

        New ones are opened (with their last few exchanges and their folder),
        titles and busy/idle follow claude.ai, and one archived or gone there
        is closed here. One whose machine went away stays listed - its
        history is here - but a new one appears only while its machine is
        connected. This host's own sessions are skipped: they are listed
        already. With the `LOCAL` scope only sessions running on this machine
        are listed, and one found to run elsewhere is closed.
        """
        async with self._sync_lock:
            await self._sync_claude_ai()

    async def _sync_claude_ai(self) -> None:
        api, directory = self._claude_ai, self._directory
        if api is None or directory is None:
            return
        rows = {row.id: row for row in await api.sessions()}
        here = await asyncio.to_thread(self._running_here)
        own = {s.bridge_session_id for s in list(self._local) if s.bridge_session_id}
        listed = {
            uri.removeprefix(_MIRROR_URI): uri
            for uri in directory.uris()
            if uri.startswith(_MIRROR_URI)
        }
        elsewhere: set[str] = set()
        for remote_id, row in rows.items():
            if remote_id in own or remote_id in self._dismissed:
                continue
            if row.environment_kind != "bridge" or row.status != "active":
                continue
            local, folder = await self._whereabouts(api, row, here)
            if local is False:
                elsewhere.add(remote_id)
            if self._scope == LOCAL and not local and remote_id not in listed:
                continue
            if remote_id in listed and remote_id not in self._mirrors:
                # Restored from the last run: its agent starts now it is
                # known to be active there.
                await directory.open(listed[remote_id], title=row.title, resume_state={})
            if remote_id not in listed:
                if not row.live:
                    continue
                self._titles[remote_id] = row.title
                await directory.open(
                    uri_of(remote_id),
                    title=row.title,
                    resume_state={MIRROR_KEY: remote_id, "backfill": True},
                    working_directories=[folder] if folder else (),
                )
            await self._follow_row(row)
        for remote_id, uri in listed.items():
            found = rows.get(remote_id)
            gone = found is None or found.status != "active" or remote_id in own
            if gone or (self._scope == LOCAL and remote_id in elsewhere):
                self._closing.add(remote_id)
                try:
                    await directory.close(uri)
                finally:
                    self._closing.discard(remote_id)

    async def _whereabouts(
        self, api: Api, row: RemoteSession, here: Mapping[str, str]
    ) -> tuple[bool | None, str | None]:
        """Whether *row* runs on this machine (None: cannot tell), and its folder URI.

        This machine's registry is certain for what runs here now. Failing
        that, a session started by ``claude --remote-control`` names its
        machine and folder through its environment. One switched on from
        inside (desktop app, IDE) and no longer running here names neither;
        a listed one is then kept rather than dropped on a guess.
        """
        if row.id in here:
            return True, _folder_uri(here[row.id])
        if row.environment_id:
            machine = await api.machine_of(row.environment_id)
            if machine is not None:
                local = machine.name.casefold() == self._machine
                return local, _folder_uri(machine.directory) if local else None
        return None, None

    async def _follow_row(self, row: RemoteSession) -> None:
        session = self._mirrors.get(row.id)
        publisher = session.context.publisher if session is not None else None
        if publisher is None:
            return
        if self._titles.get(row.id) != row.title:
            self._titles[row.id] = row.title
            await publisher.title_changed(row.title)
        await publisher.activity_changed(_ACTIVITY.get(row.worker_status))

    def _mirror(
        self, context: AgentSessionContext, remote_id: str, *, backfill: bool
    ) -> ClaudeSession:
        api = self._claude_ai
        if api is None:
            raise LookupError(f"{remote_id} is a claude.ai session, and claude.ai is off here")

        def client(options: ClaudeAgentOptions) -> SdkClient:
            return RemoteClient(
                api, remote_id, options, backfill=BACKFILL_EXCHANGES if backfill else 0
            )

        def dismissed() -> None:
            self._mirrors.pop(remote_id, None)
            if remote_id not in self._closing:
                # Deleted here by a person: not listed again next time round.
                self._dismissed.add(remote_id)
                self._save_dismissed()

        session = ClaudeSession(
            context,
            root=self.roots,
            client_factory=client,
            mirror_of=remote_id,
            on_disposed=dismissed,
            chat_dir=self.chat_dir,
            chat_tools=self.chat_tools,
        )
        self._mirrors[remote_id] = session
        self._by_chat[context.chat_uri] = session
        session.start_soon()
        return session

    def _load_dismissed(self) -> set[str]:
        if self._dismissed_file is None:
            return set()
        try:
            data = json.loads(self._dismissed_file.read_text())
        except (OSError, ValueError):
            return set()
        return {item for item in data if isinstance(item, str)} if isinstance(data, list) else set()

    def _save_dismissed(self) -> None:
        if self._dismissed_file is None:
            return
        try:
            self._dismissed_file.write_text(json.dumps(sorted(self._dismissed)))
        except OSError:
            log.warning("could not save %s", self._dismissed_file, exc_info=True)


#: Seconds between tries at discovering the models, after start-up found none.
_REDISCOVERY: Final = (15.0, 30.0, 60.0, 120.0, 300.0, *(600.0,) * 18)


def _with_learned(models: Sequence[ModelInfo], learned: Learned) -> tuple[ModelInfo, ...]:
    """*models*, with what results have said about their limits filled in."""
    updated: list[ModelInfo] = []
    for model in models:
        meta = model.meta or {}
        found = learned.for_entry({"value": model.id, "resolvedModel": meta.get("resolvedModel")})
        window = model.max_context_window or found.context_window
        updated.append(
            replace(
                model,
                max_context_window=window,
                max_prompt_tokens=model.max_prompt_tokens or window,
                max_output_tokens=found.max_output
                if found.max_output is not None
                else model.max_output_tokens,
            )
        )
    return tuple(updated)


#: A mirrored session's resume state names its claude.ai session under this.
MIRROR_KEY: Final = "claudeAi"
_MIRROR_URI: Final = uri_of("")
#: claude.ai's `worker_status`, as a session's activity line.
_ACTIVITY: Final[Mapping[str, str]] = {
    "running": "Working",
    "requires_action": "Waiting for you",
}


def _folder_uri(folder: str | None) -> str | None:
    """A folder on this machine as a `file:` URI, or None if it is not one."""
    if not folder:
        return None
    path = Path(folder)
    return path.as_uri() if path.is_absolute() else None
