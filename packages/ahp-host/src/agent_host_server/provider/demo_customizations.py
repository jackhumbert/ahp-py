"""A fully-populated customization tree, for finding out what a client renders.

Every name is prefixed ``AHS`` so it is unmistakably ours in a UI that also
shows VS Code's own built-in customizations.

The shape is a two-level tree, which is easy to get wrong from the section
names alone: the top-level ``Customization`` union is only three variants --
``plugin``, ``directory`` and ``mcpServer``. Agents, skills, prompts, rules and
hooks are **children** of a container, never top-level entries. ``ToolDefinition``
is not a customization at all; it lives on ``SessionState.serverTools``.

Whether any of this renders for a *remote* host is an open question: VS Code's
`registerExternalHarness` call sits in a contribution wired to the local agent
host service, and its `hiddenSections` hides Tools and Prompts for every
provider except `copilotcli`. This module exists to find out rather than assume.
"""

from __future__ import annotations

from typing import Any

__all__ = ["demo_customizations", "demo_server_tools"]

_BASE = "file:///ahs-demo"


def _agent(suffix: str, name: str, description: str, **extra: Any) -> dict[str, Any]:
    return {
        "type": "agent",
        "id": f"ahs-agent-{suffix}",
        "uri": f"{_BASE}/agents/{suffix}.md",
        "name": name,
        "description": description,
        "enabled": True,
        **extra,
    }


def demo_customizations() -> list[dict[str, Any]]:
    """The `SessionState.customizations` tree.

    Two containers plus an MCP server, exercising every child type and both
    container variants. `directory` additionally declares `contents` (which
    child type it holds) and `writable`.
    """
    return [
        {
            "type": "plugin",
            "id": "ahs-plugin-main",
            "uri": f"{_BASE}/plugins/ahs-toolkit",
            "name": "AHS Toolkit Plugin",
            "version": "0.1.0",
            "enabled": True,
            "children": [
                _agent(
                    "alpha",
                    "AHS Agent Alpha",
                    "A demo agent contributed by agent-host-server.",
                    model="echo-1",
                    tools=["ahs_echo_tool"],
                ),
                _agent(
                    "bravo",
                    "AHS Agent Bravo (model-only)",
                    "Invocable by the model but not directly by a user.",
                    disableUserInvocation=True,
                ),
                {
                    "type": "skill",
                    "id": "ahs-skill-charlie",
                    "uri": f"{_BASE}/skills/charlie/SKILL.md",
                    "name": "AHS Skill Charlie",
                    "description": "A demo skill contributed by agent-host-server.",
                    "enabled": True,
                },
                {
                    "type": "prompt",
                    "id": "ahs-prompt-delta",
                    "uri": f"{_BASE}/prompts/delta.prompt.md",
                    "name": "AHS Prompt Delta",
                    "description": (
                        "A demo prompt. VS Code hides the Prompts section "
                        "unless the provider is copilotcli."
                    ),
                    "enabled": True,
                },
                {
                    "type": "rule",
                    "id": "ahs-rule-echo",
                    "uri": f"{_BASE}/rules/echo.md",
                    "name": "AHS Instruction Echo",
                    "description": "A demo instruction/rule, applied to every request.",
                    "enabled": True,
                    "alwaysApply": True,
                },
                {
                    "type": "rule",
                    "id": "ahs-rule-foxtrot",
                    "uri": f"{_BASE}/rules/foxtrot.md",
                    "name": "AHS Instruction Foxtrot (globbed)",
                    "description": "A demo instruction scoped to Python files.",
                    "enabled": True,
                    "globs": ["**/*.py"],
                },
                {
                    "type": "hook",
                    "id": "ahs-hook-golf",
                    "uri": f"{_BASE}/hooks/golf.sh",
                    "name": "AHS Hook Golf",
                    "enabled": True,
                },
            ],
        },
        {
            "type": "directory",
            "id": "ahs-directory-skills",
            "uri": f"{_BASE}/skills",
            "name": "AHS Skills Directory",
            # A directory declares which child type it holds, and whether a
            # client may write into it.
            "contents": "skill",
            "writable": False,
            "enabled": True,
            "children": [
                {
                    "type": "skill",
                    "id": "ahs-skill-hotel",
                    "uri": f"{_BASE}/skills/hotel/SKILL.md",
                    "name": "AHS Skill Hotel",
                    "description": "A demo skill inside a directory container.",
                    "enabled": True,
                }
            ],
        },
        {
            "type": "mcpServer",
            "id": "ahs-mcp-india",
            "uri": f"{_BASE}/mcp/india",
            "name": "AHS MCP Server India",
            "enabled": True,
            # `state` is a discriminated union on `kind`; `ready` is the
            # no-extra-fields variant.
            "state": {"kind": "ready"},
        },
    ]


def demo_server_tools() -> list[dict[str, Any]]:
    """`SessionState.serverTools` -- NOT customizations, a separate field.

    VS Code hides its Tools section for any provider other than `copilotcli`
    (`hiddenSections` in `agentHostChatContribution.ts`), so these may not
    render even locally.
    """
    return [
        {
            "name": "ahs_echo_tool",
            "title": "AHS Echo Tool",
            "description": "A demo tool contributed by agent-host-server. Echoes its input.",
            "inputSchema": {
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
            },
        },
        {
            "name": "ahs_clock_tool",
            "title": "AHS Clock Tool",
            "description": "A second demo tool, so the list is visibly plural.",
            "inputSchema": {"type": "object", "properties": {}},
        },
    ]
