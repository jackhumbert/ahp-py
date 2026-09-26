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
from collections.abc import AsyncIterable, AsyncIterator, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, Protocol

from agent_host_server.provider.base import (
    AgentInfo,
    AgentSessionContext,
    ConfigRequest,
    ConfigResolution,
    ConfigValue,
    ModelInfo,
    SessionDirectory,
    ToolConfirmation,
    TurnSink,
    UserMessage,
)
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

from agent_host_server_claude.attachments import prompt_content
from agent_host_server_claude.claude_ai import (
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
from agent_host_server_claude.config import DEFAULT_STATE
from agent_host_server_claude.paths import directory_of
from agent_host_server_claude.permissions import (
    APPROVALS_PROPERTY,
    ASK,
    CONFIG_KEY,
    DISALLOWED_TOOLS,
    EXIT_PLAN_TOOL,
    PERMISSION_MODES,
    PLAN,
    approval_mode,
    describe,
    past_tense,
    pre_tool_use_decision,
    progress_line,
)
from agent_host_server_claude.remote_control import CONFIG_KEY as RC_CONFIG_KEY
from agent_host_server_claude.remote_control import (
    RemoteControlClient,
    auto_enable,
    bridge_of,
)
from agent_host_server_claude.remote_control import property_schema as rc_property
from agent_host_server_claude.roots import Roots, as_roots
from agent_host_server_claude.sessions import (
    NEW,
    SEARCH_LIMIT,
    ClaudeCodeSessions,
    describe_session,
    is_session_id,
    title_of,
)

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
ENTRYPOINT = "agent-host"

#: Claude Code's value for "whatever the account's default is".
DEFAULT_MODEL = "default"

#: What a session with no folder may use: nothing that touches the machine.
#: Its `cwd` is an empty directory of the agent's own, but Claude Code's shell
#: and file tools are not jailed to `cwd` - the OS user is the only boundary -
#: so a chat gets no file or shell tools at all, and no MCP servers from the
#: user's config (which can reach anything). The web tools still pass the
#: approval gate like any other.
CHAT_TOOLS: Final = ("WebSearch", "WebFetch")
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


@dataclass(frozen=True)
class Discovery:
    """What Claude Code says about this account at start-up."""

    models: tuple[ModelInfo, ...] = ()
    #: Whether Claude Code would turn Remote Control on for a session it
    #: started itself - the user's `remoteControlAtStartup`, then org policy.
    remote_control: bool = False


async def discover(root: Path, client_factory: ClientFactory = _default_client) -> Discovery:
    """Ask Claude Code once. Empty (no picker, no Remote Control) if it cannot say."""
    client = client_factory(ClaudeAgentOptions(cwd=str(root)))
    try:
        await client.connect()
        info = await client.get_server_info()
    except Exception:
        log.exception("could not ask Claude Code about this account; no models, no Remote Control")
        return Discovery()
    finally:
        try:
            await client.disconnect()
        except Exception:
            log.debug("disconnecting the start-up probe failed", exc_info=True)
    return Discovery(models=models_from_server_info(info), remote_control=auto_enable(info))


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
    ) -> None:
        self.context = context
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
        #: Approvals the other side answered: the call, and the timer that
        #: reports it approved unless its result says otherwise first.
        self._answered_elsewhere: dict[str, asyncio.Task[None]] = {}

    @property
    def _sink(self) -> TurnSink | None:
        return self._turn.sink if self._turn is not None else None

    # -- lifecycle -----------------------------------------------------------

    def _granted(self) -> list[Path]:
        """The session's folders as real paths, each inside a served folder."""
        granted: list[Path] = []
        for uri in self.working_directories:
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
        return cwd, extra, bool(granted) or cwd != self._chat_dir

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
        chat: dict[str, Any] = {}
        prompt: dict[str, Any] = {"type": "preset", "preset": "claude_code"}
        if not tools:
            chat = {"tools": list(CHAT_TOOLS), "strict_mcp_config": True}
            prompt["append"] = CHAT_PROMPT
        return ClaudeAgentOptions(
            cwd=str(cwd),
            add_dirs=list(extra),
            resume=self.claude_session_id,
            model=None if self._model == DEFAULT_MODEL else self._model,
            permission_mode=PERMISSION_MODES[self.approvals],  # type: ignore[arg-type]
            disallowed_tools=list(DISALLOWED_TOOLS),
            can_use_tool=self._can_use_tool,
            hooks={"PreToolUse": [HookMatcher(matcher=None, hooks=[self._pre_tool_use])]},
            include_partial_messages=True,
            # Echo each streamed user message when the CLI takes it in: the
            # only signal that a steered message joined the turn, and what
            # tells our own messages from ones typed elsewhere.
            extra_args={"replay-user-messages": None},
            system_prompt=prompt,  # type: ignore[arg-type]
            env={"CLAUDE_CODE_ENTRYPOINT": ENTRYPOINT},
            **chat,
        )

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

    def start_soon(self) -> None:
        """`start`, without holding up whoever created the session."""
        if self._starting is None:
            self._starting = asyncio.create_task(self.start())

    async def start(self) -> None:
        """Start the Claude client now, rather than on the first message.

        A session on claude.ai needs a running client to be reachable at all,
        so one with Remote Control starts as soon as it exists. A failure here
        is logged, not raised: the first turn tries again and reports it.
        """
        try:
            await self._ensure_client()
        except Exception:
            log.exception("starting the Claude client failed; the first turn will retry")

    async def _ensure_client(self) -> SdkClient:
        async with self._connecting:
            if self._client is None:
                options = self._options()
                # Pinned: the conversation lives under this folder from now on.
                self.directory = Path(str(options.cwd))
                client = self._client_factory(options)
                await client.connect()
                self._client = client
                self._reader = asyncio.create_task(self._read(client))
                if self.remote_control and not self.archived:
                    await self._enable_remote_control(client)
            return self._client

    async def aclose(self) -> None:
        client, self._client = self._client, None
        reader, self._reader = self._reader, None
        if reader is not None:
            reader.cancel()
        for timer in self._answered_elsewhere.values():
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
            self.start_soon()

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
        client, self._client = self._client, None
        reader, self._reader = self._reader, None
        self._starting = None
        if reader is not None:
            reader.cancel()
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
        if CONFIG_KEY not in values:
            return
        mode = approval_mode(values[CONFIG_KEY])
        if mode == self.approvals:
            return
        self.approvals = mode
        if self._client is not None:
            await self._client.set_permission_mode(PERMISSION_MODES[mode])

    async def _pre_tool_use(self, hook_input: Any, tool_use_id: str | None, context: Any) -> Any:
        return pre_tool_use_decision(str(hook_input.get("tool_name", "")), self.approvals)

    async def _announce(self, call_id: str, name: str, tool_input: Mapping[str, Any]) -> None:
        if call_id in self._announced or self._sink is None:
            return
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
        turn = self._turn
        if turn is not None and turn.sink is None:
            # Asked before a turn from elsewhere reached the host.
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(turn.attached.wait(), _ATTACH_TIMEOUT)
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
        try:
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
        except asyncio.CancelledError:
            # Either the turn ended here (the host already withdrew the
            # prompt, and reporting is a no-op), or it was answered on
            # claude.ai and the CLI withdrew the question. Which answer is
            # known only from the call's result, if it comes quickly.
            self._answered_elsewhere[call_id] = asyncio.create_task(
                self._report_answer_after_grace(sink, call_id)
            )
            raise
        if not outcome.approved:
            return PermissionResultDeny(message="The user declined this tool call.")
        if tool_name == EXIT_PLAN_TOOL:
            await self._left_plan_mode()
        approved = outcome.tool_input if isinstance(outcome.tool_input, dict) else tool_input
        return PermissionResultAllow(updated_input=approved)

    async def _report_answer_after_grace(self, sink: TurnSink, call_id: str) -> None:
        """No result yet, so the tool is running: it was approved elsewhere."""
        await asyncio.sleep(_ANSWER_GRACE)
        self._answered_elsewhere.pop(call_id, None)
        try:
            await self._answered(sink, call_id, denied=False)
        except Exception:
            log.exception("reporting an approval given elsewhere failed")

    async def _report_answer_from_result(
        self, sink: TurnSink, call_id: str, failed: bool, text: str
    ) -> None:
        await self._report_answer(sink, call_id, denied=failed and text.startswith(_DENIED))

    async def _report_answer(self, sink: TurnSink, call_id: str, *, denied: bool) -> None:
        timer = self._answered_elsewhere.pop(call_id, None)
        if timer is None:
            return
        timer.cancel()
        await self._answered(sink, call_id, denied=denied)

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
        self._turn = turn

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
            if self._restart_pending:
                await self._restart_client()
            try:
                client = await self._ensure_client()
            except PermissionError as error:
                await sink.turn_failed(str(error), error_type="agent.workingDirectory")
                return
            turn = _Turn(sink=sink)
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
                content = prompt_content(message.text, message.raw, self._roots)
                self._steers_pending.clear()
                await client.query(_user_message(content, self._send_id()))
                # No await between sending and this, so a steer is either
                # refused or sent after the message it steers.
                self._accepting_steers = True
                await turn.done.wait()
            finally:
                self._accepting_steers = False
                self._steers_pending.clear()
                self._abandon(turn)

    async def _external(self, text: str) -> None:
        """A message typed elsewhere (claude.ai, a phone) started a turn.

        Opened on the host as a turn no client asked for, so the conversation
        here does not skip it. Its messages wait until the host has started
        it; `external_turn` refuses while the chat still has a turn, which can
        be the one that just finished here, so it is retried briefly.
        """
        publisher = self.context.publisher
        if publisher is None:
            return
        turn = _Turn()
        self._begin(turn)

        async def run(sink: TurnSink) -> None:
            turn.sink = sink
            turn.attached.set()
            try:
                await turn.done.wait()
            finally:
                self._abandon(turn)

        for _ in range(40):
            if await publisher.external_turn(text, run):
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
        except Exception:
            log.exception("reading from Claude Code failed")
        finally:
            if self._client is client:
                # The next turn starts a new one, resuming this conversation.
                self._client = None
            turn = self._turn
            if turn is not None and not turn.done.is_set():
                if turn.sink is not None:
                    await turn.sink.turn_failed("Claude Code stopped", error_type="claude.exited")
                self._finish(turn)

    def _is_elsewhere(self, item: SdkUserMessage) -> bool:
        """A replayed message we did not send: typed on claude.ai, or injected."""
        return item.origin is not None and item.uuid is not None and item.uuid not in self._sent

    async def _on_message(self, item: Any) -> None:
        if isinstance(item, SdkUserMessage):
            if self._is_elsewhere(item):
                await self._on_message_from_elsewhere(item)
                return
            self._took_in(item)
        elif isinstance(item, SystemMessage):
            session_id = item.data.get("session_id")
            if item.subtype == "init" and isinstance(session_id, str):
                self.claude_session_id = session_id
            elif item.subtype == "status" and "permissionMode" in item.data:
                await self._on_mode_elsewhere(item.data["permissionMode"])
        elif isinstance(item, ResultMessage):
            self.claude_session_id = item.session_id or self.claude_session_id
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
        if isinstance(item, StreamEvent):
            await self._on_stream_event(item, sink)
        elif isinstance(item, AssistantMessage):
            turn.model = item.model or turn.model
            await self._on_assistant(item, sink)
        elif isinstance(item, SdkUserMessage):
            await self._on_tool_results(item, sink)
        elif isinstance(item, SystemMessage) and item.subtype == "permission_denied":
            call_id = item.data.get("tool_use_id")
            if isinstance(call_id, str):
                await self._report_answer(sink, call_id, denied=True)
        elif isinstance(item, ResultMessage):
            await self._on_result(item, sink, turn.model)
            if not self._steers_pending:
                self._finish(turn)

    async def _on_message_from_elsewhere(self, item: SdkUserMessage) -> None:
        text = _text_of(item.content).strip()
        turn = self._turn
        if turn is None:
            await self._external(text)
        elif turn.sink is not None:
            # Typed on claude.ai while a turn runs here: Claude Code takes it
            # in like a steered message, so it is shown where it landed.
            await turn.sink.text_delta(f"\n\n*Sent from another device:* {text}\n\n")

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
            await self._report_answer_from_result(sink, block.tool_use_id, failed, text)
            name, tool_input = self._inputs.get(block.tool_use_id, ("", {}))
            await sink.tool_call_completed(
                block.tool_use_id,
                {"content": [{"type": "text", "text": text}]},
                success=not failed,
                past_tense_message=past_tense(name, tool_input, failed=failed),
            )

    async def _on_result(self, item: ResultMessage, sink: TurnSink, model: str | None) -> None:
        usage = item.usage or {}
        await sink.usage(
            input_tokens=usage.get("input_tokens"),
            output_tokens=usage.get("output_tokens"),
            cache_read_tokens=usage.get("cache_read_input_tokens"),
            model=model,
        )
        if item.terminal_reason in _ABORTED:
            return  # the user pressed stop; the host already knows
        if item.is_error:
            detail = "; ".join(item.errors or []) or item.result or item.subtype
            await sink.turn_failed(detail, error_type=f"claude.{item.subtype}")


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
        #: `LOCAL`: only sessions running on this machine, so each machine's
        #: node lists its own - the broker files them under it, and several
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
        self._dismissed: set[str] = self._load_dismissed()

    @property
    def agent(self) -> AgentInfo:
        return AgentInfo(
            provider=self.provider_id,
            display_name=self._display_name,
            description="Claude Code, running on this machine as its user.",
            models=self._models,
            capabilities={"multipleWorkingDirectories": dict(WORKING_DIRECTORIES_CAPABILITY)},
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
            },
            values={
                CONFIG_KEY: approval_mode(request.values.get(CONFIG_KEY)),
                CONTINUE_KEY: chosen if is_session_id(chosen) else NEW,
                RC_CONFIG_KEY: self._remote_control(request.values.get(RC_CONFIG_KEY)),
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

    def _started(self, session: ClaudeSession) -> ClaudeSession:
        """A session on claude.ai must be reachable before its first message here.

        Not an archived one: starting it would reattach, which unarchives it.
        """
        if session.mirror_of is None:
            self._local.add(session)
        if session.remote_control and not session.archived:
            session.start_soon()
        return session

    async def create_session(self, context: AgentSessionContext) -> ClaudeSession:
        chosen = context.config.get(CONTINUE_KEY)
        remote_control = self._remote_control(context.config.get(RC_CONFIG_KEY))
        if not is_session_id(chosen):
            return self._started(
                ClaudeSession(
                    context,
                    root=self.roots,
                    client_factory=self._client_factory,
                    remote_control=remote_control,
                    chat_dir=self.chat_dir,
                )
            )
        continuation = await self.sessions.continue_from(chosen)
        return self._started(
            ClaudeSession(
                context,
                root=self.roots,
                client_factory=self._client_factory,
                claude_session_id=continuation.session_id,
                directory=continuation.directory,
                recap=continuation.recap,
                remote_control=remote_control,
                chat_dir=self.chat_dir,
            )
        )

    async def resume_session(self, context: AgentSessionContext) -> ClaudeSession:
        state = context.resume_state or {}
        mirrored = state.get(MIRROR_KEY)
        if isinstance(mirrored, str):
            return self._mirror(context, mirrored, backfill=state.get("backfill") is True)
        session_id = state.get("claudeSessionId")
        bridge = state.get("bridgeSessionId")
        return self._started(
            ClaudeSession(
                context,
                root=self.roots,
                client_factory=self._client_factory,
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
                chat_dir=self.chat_dir,
            )
        )

    async def resume_state_of(self, session: Any) -> Mapping[str, Any] | None:
        if not isinstance(session, ClaudeSession):
            return None
        if session.mirror_of is not None:
            return {MIRROR_KEY: session.mirror_of, CONFIG_KEY: session.approvals}
        if not session.claude_session_id and not session.bridge_session_id:
            return None
        state: dict[str, Any] = {
            CONFIG_KEY: session.approvals,
            RC_CONFIG_KEY: session.remote_control,
        }
        if session.claude_session_id:
            state["claudeSessionId"] = session.claude_session_id
        if session.bridge_session_id:
            state["bridgeSessionId"] = session.bridge_session_id
        if session.archived:
            state["archived"] = True
        if session.directory is not None:
            state["cwd"] = str(session.directory)
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
            try:
                await directory.open(uri, title="", resume_state={})
            except Exception:
                log.exception("could not bring back %s", uri)
        if self._claude_ai is not None and self._poller is None:
            self._poller = asyncio.create_task(self._watch_claude_ai())

    async def aclose(self) -> None:
        poller, self._poller = self._poller, None
        if poller is not None:
            poller.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await poller
        if self._claude_ai is not None:
            await self._claude_ai.aclose()

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
        )
        self._mirrors[remote_id] = session
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
