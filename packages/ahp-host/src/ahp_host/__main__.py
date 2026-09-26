"""Run a demo host: ``python -m ahp_host``.

Serves the offline echo provider over WebSocket on loopback and prints the
VS Code settings to paste. This is a **demonstration**, not a deployment: it
uses :class:`LoopbackSingleUserPolicy`, which permits everything and is safe
only because the socket is bound to loopback.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import functools
import json
import logging
import secrets
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import Any, Final

from ahp_protocol.versions import DEFAULT_SUPPORTED_VERSIONS

from ahp_host.core import Host, HostInfo, LoopbackSingleUserPolicy
from ahp_host.core.config import RootConfig
from ahp_host.core.resources import RootedFilesystemResourceProvider
from ahp_host.provider import EchoProvider
from ahp_host.provider.demo_workspace import (
    DemoWorkspace,
    publish_workspace_changesets,
)
from ahp_host.ws import serve_websocket

_log = logging.getLogger(__name__)


def _terminal_backend(args: argparse.Namespace) -> Any:
    """The PTY backend, imported only when `--terminal` asks for it.

    `pty_backend` needs `fcntl` and `termios`, which exist only on POSIX. A
    top-level import made the whole demo host unstartable on Windows even
    without `--terminal`, where no terminal code runs at all.
    """
    try:
        from ahp_host.core.pty_backend import PtyTerminalBackend
    except ImportError as exc:
        raise SystemExit(f"--terminal needs a POSIX pty, unavailable here: {exc}") from exc
    return PtyTerminalBackend(
        default_cwd=str(Path(args.serve_directory).resolve()) if args.serve_directory else None
    )


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="ahp-host", description=__doc__)
    parser.add_argument("--port", type=int, default=4321)
    parser.add_argument("--bind", default="127.0.0.1")
    parser.add_argument(
        "--token",
        nargs="?",
        const="",
        default=None,
        help="require a connection token on the upgrade; omit the value to generate one",
    )
    parser.add_argument(
        "--allow-remote",
        action="store_true",
        help="bind off-loopback. AHP defines no authentication -- read docs/research.md §8.",
    )
    parser.add_argument("--delay", type=float, default=0.05, help="echo delta delay, seconds")
    parser.add_argument(
        "--agent-name",
        default="Echo",
        help="AgentInfo.displayName -- how clients label the agent",
    )
    parser.add_argument(
        "--model-name",
        default="Echo Model v1",
        help="the model name shown in VS Code's chat model picker",
    )
    parser.add_argument(
        "--wire-log",
        metavar="PATH",
        help="append every frame as ahp-inspector JSONL (open with `npx ahp-inspector`)",
    )
    parser.add_argument(
        "--customizations",
        action="store_true",
        help="advertise a demo plugin/agent/skill/instruction/hook/MCP tree, named AHS*",
    )
    parser.add_argument(
        "--elicit",
        action="store_true",
        help="make the echo agent stop and ask a question mid-turn (ADR 0005)",
    )
    parser.add_argument(
        "--confirm-tools",
        action="store_true",
        help="make the echo agent ask before running its tool, and honour edits to the input",
    )
    parser.add_argument(
        "--client-tools",
        action="store_true",
        help="make the echo agent delegate to a tool the CLIENT owns (no host filesystem)",
    )
    parser.add_argument(
        "--configurable",
        action="store_true",
        help=(
            "publish BOTH config schemas: the session one (reply style, prefix, "
            "dynamic greeting) and RootState.config, so the ~10 root/configChanged "
            "pushes a client sends on every connect stop being dropped"
        ),
    )
    parser.add_argument(
        "--serve-directory",
        metavar="PATH",
        help="expose PATH read-only over the resource* commands, jailed to that root",
    )
    parser.add_argument(
        "--writable",
        action="store_true",
        help="also allow writes under --serve-directory. A SECOND opt-in, on purpose.",
    )
    parser.add_argument(
        "--sequence-file",
        metavar="PATH",
        help=(
            "persist serverSeq here so it keeps increasing across a restart; "
            "without it a reconnecting client is correctly told to take fresh "
            "snapshots, on every reconnect, for the life of the host"
        ),
    )
    parser.add_argument(
        "--changes",
        action="store_true",
        help=(
            "the demo agent makes REAL edits in --serve-directory on every "
            "turn and publishes a changeset describing them, with working "
            "git stage/commit/revert buttons. Point --serve-directory at a "
            "scratch git repo, not at anything you care about"
        ),
    )
    parser.add_argument(
        "--terminal",
        action="store_true",
        help=(
            "install a REAL pty backend: `createTerminal` runs a shell. This is "
            "arbitrary command execution for anyone who can complete `initialize`, "
            "which on this demo means anyone who can reach the loopback port with "
            "the token. Off by default, and deliberately its own flag"
        ),
    )
    parser.add_argument(
        "--multi-chat",
        action="store_true",
        help=(
            "advertise capabilities.multipleChats{fork,sideChat}: chat tabs, "
            "fork-into-chat, and side chats. Without it a client refuses to "
            "open a second chat at all"
        ),
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    return parser.parse_args()


async def _run() -> None:
    args = _parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    token = args.token
    if token == "":
        token = secrets.token_urlsafe(24)

    workspace = (
        DemoWorkspace(Path(args.serve_directory)) if args.changes and args.serve_directory else None
    )
    host = Host(
        EchoProvider(
            delay=args.delay,
            display_name=args.agent_name,
            model_name=args.model_name,
            customizations=args.customizations,
            elicit=args.elicit,
            confirm_tools=args.confirm_tools,
            client_tools=args.client_tools,
            configurable=args.configurable,
            workspace=workspace,
            capabilities=(
                {"multipleChats": {"fork": True, "sideChat": True}} if args.multi_chat else None
            ),
        ),
        LoopbackSingleUserPolicy(),
        info=HostInfo(name="ahp-host (demo)"),
        # Echo's `complete()` scans for "#" and nothing else, so "#" is the
        # only honest advertisement. Naming "@" as well would open a picker
        # that is always empty.
        completion_trigger_characters=("#",),
        # `Path(None)` crashed the host outright when --terminal was passed
        # without --serve-directory. And the backend's own docstring says it
        # "does not choose a working directory ... a backend that defaulted to
        # the host's own directory would silently expose it" -- so the default
        # is the SERVED root when there is one, and nothing when there is not.
        # A client that sends no `cwd` then gets the host's directory from the
        # OS, which is the one case this cannot prevent.
        terminals=_terminal_backend(args) if args.terminal else None,
        # Behind the same flag as the session config schema: both mean "this
        # host is configurable", and a second flag for the other half would be
        # a distinction only this file cares about.
        root_config=(
            RootConfig(properties=DEMO_ROOT_CONFIG_PROPERTIES) if args.configurable else None
        ),
        resources=RootedFilesystemResourceProvider(
            Path(args.serve_directory), writable=args.writable
        )
        if args.serve_directory
        else None,
        # Told to the client, so it browses the directory we actually serve
        # rather than `/`.
        default_directory=Path(args.serve_directory).resolve().as_uri()
        if args.serve_directory
        else None,
        wire_log=Path(args.wire_log) if args.wire_log else None,
        sequence_file=Path(args.sequence_file) if args.sequence_file else None,
    )

    if workspace is not None:
        # Registered per operation, by name -- there is no "enable all
        # operations" switch, because an operation is a button that DOES
        # something and the embedder should have to say which.
        invoke = _demo_operations(host, workspace)
        for operation_id in ("ahs-stage", "ahs-commit", "ahs-revert", "ahs-review"):
            host.register_operation(operation_id, invoke)

    async with serve_websocket(
        host,
        bind=args.bind,
        port=args.port,
        connection_token=token,
        allow_remote=args.allow_remote,
    ) as server:
        emit = functools.partial(print, flush=True)
        emit(f"ahp-host listening on {server.url}")
        emit(f"speaking protocol {', '.join(DEFAULT_SUPPORTED_VERSIONS)}\n")

        # `chat.remoteAgentHosts` holds IRawRemoteAgentHostEntry objects, not
        # URLs: `address` and `name` are both required strings, `connectionToken`
        # is separate and VS Code appends it as `?tkn=` itself. The address is
        # stored scheme-less because the transport defaults to `ws://`; only
        # `wss://` is preserved.
        entry: dict[str, object] = {
            "address": f"{args.bind}:{server.bound_port}",
            "name": "Echo (ahp-host)",
        }
        if token:
            entry["connectionToken"] = token

        emit("To connect VS Code (1.131+), add to settings.json:\n")
        emit(
            json.dumps(
                {"chat.remoteAgentHostsEnabled": True, "chat.remoteAgentHosts": [entry]},
                indent=2,
            )
        )
        emit("\nThen open the Agent Sessions view and pick 'Echo'.  Ctrl-C to stop.")
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.Event().wait()

    await host.aclose()


#: `RootState.config` for the demo host: the keys VS Code actually pushes.
#:
#: Not invented. Every key below was observed arriving as `root/configChanged`
#: on a real connection, and they arrive UNGATED -- the client sends them to a
#: remote host whether or not a schema is advertised. Without a schema the
#: reducer's own guard drops all of them, which is what this host did: ten
#: pushes per connect, every one silently refused.
#:
#: Publishing the schema does NOT make the client send more. Its gated
#: forwarder deliberately fans out only to a LOCAL agent host, on the grounds
#: that a resolved shell path is local-machine-shaped and remote operators
#: should configure server-side. What the schema buys is (1) the pushes stop
#: being dropped, and (2) "Open Host Settings" opens a real document instead of
#: an empty `{}` that cannot be saved.
#:
#: Three of these are load-bearing rather than cosmetic:
#: `terminalAutoApproveEnabled`, `globalAutoApproveEnabled` and
#: `terminalAutoApproveRules` are the ONLY channel by which the user's
#: auto-approve preferences reach a host at all. A host that drops them cannot
#: honour them, and the user has no way to tell.
DEMO_ROOT_CONFIG_PROPERTIES: Final[dict[str, dict[str, Any]]] = {
    "telemetryLevel": {
        "type": "string",
        "title": "Telemetry level",
        "enum": ["all", "error", "crash", "off"],
    },
    "editTelemetryEnabled": {"type": "boolean", "title": "Edit telemetry"},
    "sessionSyncEnabled": {"type": "boolean", "title": "Session sync"},
    "terminalAutoApproveEnabled": {"type": "boolean", "title": "Auto-approve terminal commands"},
    "globalAutoApproveEnabled": {"type": "boolean", "title": "Auto-approve everything"},
    "terminalAutoApproveRules": {"type": "object", "title": "Terminal auto-approve rules"},
    "autoReplyEnabled": {"type": "boolean", "title": "Auto reply"},
    "preferLongContextEnabled": {"type": "boolean", "title": "Prefer long context"},
    "systemProxyEnabled": {"type": "boolean", "title": "Use the system proxy"},
    "copilotMultiRootEnabled": {"type": "boolean", "title": "Copilot multi-root"},
    "claudeMultiRootEnabled": {"type": "boolean", "title": "Claude multi-root"},
    "codexMultiRootEnabled": {"type": "boolean", "title": "Codex multi-root"},
    "codexAgentEnabled": {"type": "boolean", "title": "Codex agent"},
    "disableRepoInfoTelemetry": {"type": "boolean", "title": "Disable repo-info telemetry"},
    # Not pushed by a remote client -- the forwarder keeps this one local -- but
    # declared so the settings document can offer it to a human editor.
    "defaultShell": {"type": "string", "title": "Shell for host-managed terminals"},
}


class _HostPublisher:
    """Just enough of `SessionPublisher` for the operation handlers.

    They hold the Host rather than a provider's publisher, and republishing is
    the one thing they need.
    """

    def __init__(self, host: Host, session_uri: str) -> None:
        self._host = host
        self._session_uri = session_uri

    async def changes_published(self, changeset: Any, changes: Any) -> str:
        return await self._host.publish_changeset(self._session_uri, changeset, changes)


def _demo_operations(
    host: Host, workspace: DemoWorkspace
) -> Callable[[str, str, Mapping[str, Any] | None], Awaitable[None]]:
    """The changeset buttons, closed over the host and the workspace.

    They do REAL things. A button that logs and returns looks broken from the
    outside -- the user clicks, the status flickers idle -> running -> idle,
    and nothing changes, which is indistinguishable from a handler that failed
    silently. That is what shipped, and the user reported it.

    Anything raised here becomes the operation's `error`, which the client
    renders, so a failure is worth surfacing rather than swallowing.
    """

    async def invoke(
        changeset_uri: str, operation_id: str, target: Mapping[str, Any] | None
    ) -> None:
        state = host.sequencer.state_of(changeset_uri)
        files = state.get("files") if isinstance(state, Mapping) else None
        ids = [f["id"] for f in files or [] if isinstance(f, Mapping) and "id" in f]
        # `scopes` includes "resource" for stage and revert, so the client
        # sends the file the user clicked. Acting on anything else looks
        # exactly like a dead button, because that row does not change.
        resource = str(target.get("resource")) if target else None

        if operation_id == "ahs-review":
            await host.sequencer.publish(
                changeset_uri,
                {"type": "changeset/filesReviewChanged", "files": ids, "reviewed": True},
            )
            return

        if operation_id == "ahs-stage":
            _log.info("stage: %s", workspace.stage(ids, resource))
        elif operation_id == "ahs-commit":
            _log.info("commit: %s", workspace.commit("Changes from the AHP demo agent"))
        elif operation_id == "ahs-revert":
            _log.info("revert: %s", workspace.revert(ids, resource))

        # Republish from git. The client DISCARDS the invoke result -- it
        # awaits the call and assigns nothing -- so the changeset is the only
        # feedback it renders. Without this the buttons worked and looked
        # inert: commit landed a real commit while the file list sat unchanged.
        session_uri = host.session_of_changeset(changeset_uri)
        if session_uri is not None:
            await publish_workspace_changesets(
                _HostPublisher(host, session_uri), workspace, session_uri
            )
        _log.info("git status now:\n%s", workspace.status() or "(clean)")

    return invoke


def main() -> None:
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(_run())


if __name__ == "__main__":
    main()
