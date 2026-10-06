"""A Claude Code session's skills, agents, plugins and MCP servers, as AHP customizations.

The protocol's tree has two levels: the top holds only ``plugin``,
``directory`` and ``mcpServer`` entries, and agents, skills, prompts, rules and
hooks are children of a container (`SessionState.customizations`). Claude Code
knows all of it, spread over four answers, none of which knows everything:

* `system/init` (a session's first message): ``plugins`` with their paths and
  versions, the tool names (so a session without the ``Skill`` or ``Agent``
  tool lists no skills or agents), and MCP servers with their status;
* `get_server_info()`: ``commands`` - every user-invocable skill and command,
  with its description, ``builtin`` marking Claude Code's own - and ``agents``
  with their descriptions and models;
* `get_context_usage()`: where each skill and agent came from (``source``:
  ``userSettings``, ``projectSettings``, ``plugin`` with its ``pluginName``);
* `get_mcp_status()`: every MCP server's status, scope and configuration.

So containers follow Claude Code's own sources: one ``plugin`` per plugin, and
a ``directory`` each for your skills and agents (``~/.claude/skills``,
``~/.claude/agents``, or under ``CLAUDE_CONFIG_DIR``) and the project's
(``.claude/skills``, ``.claude/agents`` in the session's folder). Claude Code's
built-in skills and agents are not customizations anyone made, and are left
out, as is anything from a source this does not recognise - managed policy,
say - rather than being filed under the wrong one. A child's ``uri`` is the
file Claude Code reads it from when that file is where Claude Code's layout
puts it; a plugin not yet seen in `system/init` has no path, and gets a
``urn:claude-code:plugin:<name>`` until it is.

MCP servers are top-level, except a plugin's own (``plugin:<plugin>:<server>``),
which is a child of its plugin. The SDK's in-process servers (this adapter's
client tools) are not listed: they are the client's own tools coming back.

Every entry states whether it is on (`enabled` on a child, `enablement` on a
plugin or MCP server), deliberately: the host keeps a client's toggle on an
entry whose state leaves the field out, and here the field always says what
the agent is actually held to - a skill switched off is refused by the Skill
tool, an MCP server is on or off in Claude Code, and an agent cannot be
switched off at all, so a client's toggle on one does not take.
"""

from __future__ import annotations

import os
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final
from urllib.parse import quote

__all__ = [
    "Sources",
    "Tree",
    "agent_id",
    "build",
    "config_dir",
    "mcp_id",
    "mcp_state",
    "plugin_id",
    "skill_id",
]

#: Where each `source` Claude Code reports puts a skill or agent.
_USER: Final = frozenset({"userSettings"})
_PROJECT: Final = frozenset({"projectSettings", "localSettings"})
_PLUGIN: Final = "plugin"


def skill_id(name: str) -> str:
    return f"skill:{name}"


def agent_id(name: str) -> str:
    return f"agent:{name}"


def plugin_id(name: str) -> str:
    return f"plugin:{name}"


def mcp_id(name: str) -> str:
    return f"mcp:{name}"


def config_dir() -> Path:
    """Claude Code's configuration directory: ``CLAUDE_CONFIG_DIR``, else ``~/.claude``."""
    configured = os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(configured).expanduser() if configured else Path.home() / ".claude"


@dataclass
class Sources:
    """Everything Claude Code has said so far that the tree is built from."""

    init: Mapping[str, Any] | None = None
    commands: Sequence[Mapping[str, Any]] | None = None
    agents: Sequence[Mapping[str, Any]] = ()
    usage: Mapping[str, Any] | None = None
    mcp: Sequence[Mapping[str, Any]] | None = None


@dataclass
class Tree:
    """The published tree, and what a toggle or an agent pick needs from it."""

    customizations: list[dict[str, Any]] = field(default_factory=list)
    #: Agent customization URI -> the agent's name, for `UserMessage.agent_uri`.
    agents: dict[str, str] = field(default_factory=dict)
    #: Every skill listed, by id -> name.
    skills: dict[str, str] = field(default_factory=dict)
    #: A plugin's id -> its skills' ids.
    plugin_skills: dict[str, list[str]] = field(default_factory=dict)
    #: An MCP server's id -> its name, as Claude Code knows it.
    mcp: dict[str, str] = field(default_factory=dict)


def mcp_state(status: Any, error: Any = None) -> dict[str, Any]:
    """Claude Code's MCP connection status as `McpServerState`.

    ``needs-auth`` is reported as an error, not ``authRequired``: that state
    requires the server's RFC 9728 metadata, which Claude Code does not hand
    over, and signing in happens in Claude Code (``/mcp``) anyway.
    """
    if status == "connected":
        return {"kind": "ready"}
    if status == "pending":
        return {"kind": "starting"}
    if status == "disabled":
        return {"kind": "stopped"}
    if status == "needs-auth":
        message = "Needs you to sign in: run /mcp in Claude Code."
        return {"kind": "error", "error": {"errorType": "mcp.needsAuth", "message": message}}
    detail = error if isinstance(error, str) and error else f"MCP server status: {status}"
    return {"kind": "error", "error": {"errorType": "mcp.failed", "message": detail}}


def _names(value: Any) -> set[str]:
    if not isinstance(value, Sequence) or isinstance(value, str):
        return set()
    return {item for item in value if isinstance(item, str)}


def _first_existing(candidates: Sequence[Path], fallback: Path) -> str:
    for candidate in candidates:
        try:
            if candidate.is_file():
                return candidate.as_uri()
        except OSError:
            continue
    return fallback.as_uri()


@dataclass
class _Container:
    entry: dict[str, Any]
    path: Path | None
    children: list[dict[str, Any]] = field(default_factory=list)


class _Builder:
    def __init__(
        self,
        sources: Sources,
        cwd: Path,
        config: Path,
        disabled: Collection[str],
        workspace: str | None,
        hidden_servers: Collection[str],
    ) -> None:
        self.sources = sources
        self.cwd = cwd
        self.config = config
        self.disabled = disabled
        self.workspace = workspace
        self.hidden = hidden_servers
        self.tree = Tree()
        self.plugins: dict[str, _Container] = {}
        self.directories: dict[str, _Container] = {}
        init = sources.init or {}
        tools = _names(init.get("tools")) if "tools" in init else None
        self.skills_usable = tools is None or "Skill" in tools
        self.agents_usable = tools is None or bool(tools & {"Agent", "Task"})
        for plugin in init.get("plugins") or ():
            if isinstance(plugin, Mapping) and isinstance(plugin.get("name"), str):
                path = plugin.get("path")
                version = plugin.get("version")
                self._plugin(
                    plugin["name"],
                    Path(path) if isinstance(path, str) and path else None,
                    version if isinstance(version, str) else None,
                )
        self.descriptions: dict[str, str] = {}
        self.invocable: set[str] | None = None
        if sources.commands is not None:
            self.invocable = set()
            for command in sources.commands:
                name = command.get("name")
                if not isinstance(name, str):
                    continue
                self.invocable.add(name)
                description = command.get("description")
                if isinstance(description, str) and description:
                    self.descriptions[name] = description

    # -- containers ------------------------------------------------------

    def _plugin(
        self, name: str, path: Path | None = None, version: str | None = None
    ) -> _Container:
        # A plugin installed from a marketplace may be named `name@marketplace`
        # where its skills say only `name`.
        name = name.partition("@")[0] or name
        container = self.plugins.get(name)
        if container is None:
            identifier = plugin_id(name)
            uri = path.as_uri() if path is not None else f"urn:claude-code:plugin:{quote(name)}"
            entry: dict[str, Any] = {"type": "plugin", "id": identifier, "uri": uri, "name": name}
            if version is not None:
                entry["version"] = version
            entry["enablement"] = (
                [{"kind": "session", "enabled": False}] if identifier in self.disabled else []
            )
            entry["load"] = {"kind": "loaded"}
            container = _Container(entry=entry, path=path)
            self.plugins[name] = container
        return container

    def _directory(self, scope: str, contents: str) -> _Container:
        key = f"{scope}:{contents}"
        container = self.directories.get(key)
        if container is None:
            base = self.config if scope == "user" else self.cwd / ".claude"
            path = base / ("skills" if contents == "skill" else "agents")
            who = "Your" if scope == "user" else "Project"
            entry = {
                "type": "directory",
                "id": f"dir:{key}",
                "uri": path.as_uri(),
                "name": f"{who} {'skills' if contents == 'skill' else 'agents'}",
                "enabled": True,
                "contents": contents,
                # Clients write customizations through `resourceWrite`, and
                # this host serves nothing outside its folders.
                "writable": False,
                "load": {"kind": "loaded"},
            }
            container = _Container(entry=entry, path=path)
            self.directories[key] = container
        return container

    # -- children --------------------------------------------------------

    def _child(self, kind: str, identifier: str, uri: str, name: str) -> dict[str, Any]:
        # Only a skill can be switched off (a deny rule); an agent cannot.
        enabled = kind != "skill" or identifier not in self.disabled
        return {"type": kind, "id": identifier, "uri": uri, "name": name, "enabled": enabled}

    def skills(self) -> None:
        if not self.skills_usable:
            return
        usage = self.sources.usage or {}
        summary = usage.get("skills")
        listed = summary.get("skillFrontmatter") if isinstance(summary, Mapping) else None
        for skill in listed if isinstance(listed, Sequence) else ():
            if not isinstance(skill, Mapping) or not isinstance(skill.get("name"), str):
                continue
            name: str = skill["name"]
            shown = name
            source, plugin = skill.get("source"), skill.get("pluginName")
            if source == _PLUGIN:
                owner = plugin if isinstance(plugin, str) and plugin else name.partition(":")[0]
                short = name.split(":", 1)[1] if ":" in name else name
                shown = short
                container = self._plugin(owner)
                if container.path is not None:
                    uri = _first_existing(
                        [
                            container.path / "skills" / short / "SKILL.md",
                            container.path / "commands" / f"{short}.md",
                        ],
                        container.path / "skills" / short,
                    )
                else:
                    uri = f"{container.entry['uri']}:skill:{quote(short)}"
                group = self.tree.plugin_skills.setdefault(container.entry["id"], [])
                group.append(skill_id(name))
            elif source in _USER or source in _PROJECT:
                scope = "user" if source in _USER else "project"
                container = self._directory(scope, "skill")
                assert container.path is not None
                commands = container.path.parent / "commands"
                uri = _first_existing(
                    [container.path / name / "SKILL.md", commands / f"{name}.md"],
                    container.path / name,
                )
            else:
                continue
            child = self._child("skill", skill_id(name), uri, shown)
            description = self.descriptions.get(name)
            if description is not None:
                child["description"] = description
            if self.invocable is not None and name not in self.invocable:
                child["disableUserInvocation"] = True
            container.children.append(child)
            self.tree.skills[skill_id(name)] = name

    def agents(self) -> None:
        if not self.agents_usable:
            return
        details = {
            agent.get("name"): agent for agent in self.sources.agents if isinstance(agent, Mapping)
        }
        usage = self.sources.usage or {}
        listed = usage.get("agents")
        for agent in listed if isinstance(listed, Sequence) else ():
            if not isinstance(agent, Mapping) or not isinstance(agent.get("agentType"), str):
                continue
            name: str = agent["agentType"]
            source = agent.get("source")
            if source == _PLUGIN:
                owner, _, short = name.partition(":")
                container = self._plugin(owner if short else name)
                short = short or name
                if container.path is not None:
                    uri = (container.path / "agents" / f"{short}.md").as_uri()
                else:
                    uri = f"{container.entry['uri']}:agent:{quote(short)}"
            elif source in _USER or source in _PROJECT:
                container = self._directory("user" if source in _USER else "project", "agent")
                assert container.path is not None
                uri = (container.path / f"{name}.md").as_uri()
            else:
                continue
            child = self._child("agent", agent_id(name), uri, name.split(":", 1)[-1])
            detail = details.get(name) or {}
            description, model = detail.get("description"), detail.get("model")
            if isinstance(description, str) and description:
                child["description"] = description
            if isinstance(model, str) and model and model != "inherit":
                child["model"] = model
            container.children.append(child)
            self.tree.agents[uri] = name

    def mcp_servers(self) -> list[dict[str, Any]]:
        servers = self.sources.mcp
        if servers is None:
            servers = (self.sources.init or {}).get("mcp_servers") or ()
        top: list[dict[str, Any]] = []
        for server in servers:
            if not isinstance(server, Mapping) or not isinstance(server.get("name"), str):
                continue
            name: str = server["name"]
            if server.get("source") == "sdk" or name in self.hidden:
                continue
            identifier = mcp_id(name)
            config = server.get("config")
            url = config.get("url") if isinstance(config, Mapping) else None
            entry: dict[str, Any] = {
                "type": "mcpServer",
                "id": identifier,
                "uri": url
                if isinstance(url, str) and url
                else f"urn:claude-code:mcp:{quote(name)}",
                "name": name,
                "state": mcp_state(server.get("status"), server.get("error")),
            }
            if server.get("status") == "disabled":
                # Claude Code keeps a server switched off per project.
                entry["enablement"] = (
                    [{"kind": "workspace", "uri": self.workspace, "enabled": False}]
                    if self.workspace is not None
                    else [{"kind": "session", "enabled": False}]
                )
            else:
                entry["enablement"] = []
            self.tree.mcp[identifier] = name
            parts = name.split(":")
            if len(parts) >= 3 and parts[0] == "plugin":
                entry["name"] = ":".join(parts[2:])
                self._plugin(parts[1]).children.append(entry)
            else:
                top.append(entry)
        return top

    def build(self) -> Tree:
        self.skills()
        self.agents()
        servers = self.mcp_servers()
        out: list[dict[str, Any]] = []
        for _, container in sorted(self.plugins.items()):
            out.append({**container.entry, "children": container.children})
        for key in ("user:skill", "project:skill", "user:agent", "project:agent"):
            directory = self.directories.get(key)
            if directory is not None and directory.children:
                out.append({**directory.entry, "children": directory.children})
        out.extend(servers)
        self.tree.customizations = out
        return self.tree


def build(
    sources: Sources,
    *,
    cwd: Path,
    config: Path | None = None,
    disabled: Collection[str] = (),
    workspace: str | None = None,
    hidden_servers: Collection[str] = (),
) -> Tree:
    """The session's customization tree, from what Claude Code has said so far.

    *disabled* are the ids a client switched off here, carried into the tree
    so republishing it does not switch them back on. *workspace* is the
    session's primary folder as clients see it, for the enablement Claude Code
    keeps per project.
    """
    return _Builder(
        sources,
        cwd,
        config if config is not None else config_dir(),
        disabled,
        workspace,
        hidden_servers,
    ).build()
