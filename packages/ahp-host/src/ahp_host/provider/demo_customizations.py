"""A fully-populated customization tree, for finding out what a client renders.

Every name is prefixed ``AHS`` so it is unmistakably ours in a UI that also
shows VS Code's own built-in customizations.

The shape is a two-level tree, which is easy to get wrong from the section
names alone: the top-level ``Customization`` union is only three variants --
``plugin``, ``directory`` and ``mcpServer``. Agents, skills, prompts, rules and
hooks are **children** of a container, never top-level entries.

Every ``uri`` here points at a file that really exists under
``examples/demo-plugin``, because a client does not take the declared
``children`` on faith -- VS Code re-expands a plugin by listing ``agents``,
``commands``, ``rules`` and ``skills`` under the plugin's own URI and reading
what it finds. A declared child whose URI 404s renders as nothing at all. ``ToolDefinition``
is not a customization at all; it lives on ``SessionState.serverTools``.

Whether any of this renders for a *remote* host is an open question: VS Code's
`registerExternalHarness` call sits in a contribution wired to the local agent
host service, and its `hiddenSections` hides Tools and Prompts for every
provider except `copilotcli`. This module exists to find out rather than assume.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

__all__ = ["demo_customizations", "demo_server_tools"]


def _demo_root() -> str:
    """The real directory behind this tree, as a `file:` URI.

    A customization is only as real as the resource behind it: a client expands
    a host-published plugin by reading its `uri` back through the `resource*`
    family, so a plugin pointing at a path that exists nowhere renders as an
    empty container. This tree used to point at `file:///ahs-demo/...` and did
    exactly that -- the wire log showed a client asking 215 times and being told
    NotFound every time.
    """
    # Beside this module, NOT `../../../examples`. A repo-relative path works
    # from a checkout and resolves to nothing once installed -- in a wheel it
    # became `<site-packages>/../examples/demo-plugin`, which does not exist,
    # so every customization pointed at a missing file and rendered as an empty
    # container. Silently, again. `demo_tree/` ships as package data.
    return (Path(__file__).resolve().parent / "demo_tree").as_uri()


_BASE = _demo_root()


def _agent(suffix: str, name: str, description: str, **extra: Any) -> dict[str, Any]:
    return {
        "type": "agent",
        "id": f"ahs-agent-{suffix}",
        "uri": f"{_BASE}/.github/agents/{suffix}.md",
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
            "uri": f"{_BASE}/.github",
            "name": "AHS Toolkit Plugin",
            "version": "0.1.0",
            # No `enabled`: since 0.8.0 a plugin carries scoped `enablement`
            # decisions, and an absent list means enabled by default.
            "children": [
                _agent(
                    "alpha",
                    "AHS Agent Alpha",
                    "A demo agent contributed by ahp-host.",
                    model="echo-1",
                    tools=["ahs_echo_tool"],
                ),
                _agent(
                    "bravo",
                    "AHS Agent Bravo (model-only)",
                    "Invocable by the model but not directly by a user.",
                    # The spec's field. VS Code 1.131.0 DECLARES it
                    # (channels-session/state.ts:907) and reads it nowhere --
                    # a grep of both shipping bundles finds zero readers. Sent
                    # for conformance, and on its own it does nothing: bravo
                    # stayed selectable in the picker with this set.
                    disableUserInvocation=True,
                    # The switch the client actually reads, via
                    # readAgentCustomizationMeta -> provideCustomAgents ->
                    # visibility.userInvocable -> refreshCustomPromptModes.
                    # `_meta` is legal on every customization
                    # (CustomizationBase, state.ts:683), so this is not schema
                    # abuse. It must be a JSON boolean -- the reader drops
                    # non-booleans, and the string "false" leaves it visible.
                    **{"_meta": {"userInvocable": False}},
                ),
                {
                    "type": "skill",
                    "id": "ahs-skill-charlie",
                    "uri": f"{_BASE}/.github/skills/charlie/SKILL.md",
                    "name": "AHS Skill Charlie",
                    "description": "A demo skill contributed by ahp-host.",
                    "enabled": True,
                },
                {
                    "type": "prompt",
                    "id": "ahs-prompt-delta",
                    "uri": f"{_BASE}/.github/commands/delta.md",
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
                    "uri": f"{_BASE}/.github/rules/echo.instructions.md",
                    "name": "AHS Instruction Echo",
                    "description": "A demo instruction/rule, applied to every request.",
                    "enabled": True,
                    "alwaysApply": True,
                },
                {
                    "type": "rule",
                    "id": "ahs-rule-foxtrot",
                    # Its own file, not a second pointer at echo's. A client
                    # renders one entry per FILE, so two customizations sharing
                    # a URI collapse into one and the second silently vanishes.
                    "uri": f"{_BASE}/.github/rules/foxtrot.instructions.md",
                    "name": "AHS Instruction Foxtrot (globbed)",
                    "description": "A demo instruction scoped to Python files.",
                    "enabled": True,
                    "globs": ["**/*.py"],
                },
                {
                    "type": "hook",
                    "id": "ahs-hook-golf",
                    # A hook manifest is JSON, not markdown, and `name` is the
                    # FILE BASENAME rather than a friendly title -- VS Code
                    # builds one as `{type, id, uri, name: basename(file)}`
                    # (copilotAgent.ts:3846-3852). A plugin contributes hooks
                    # through a single `hooks.json`.
                    "uri": f"{_BASE}/.github/hooks.json",
                    "name": "hooks.json",
                    "enabled": True,
                },
            ],
        },
        {
            "type": "directory",
            "id": "ahs-directory-skills",
            "uri": f"{_BASE}/.github/skills",
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
                    "uri": f"{_BASE}/.github/skills/hotel/SKILL.md",
                    "name": "AHS Skill Hotel",
                    "description": "A demo skill inside a directory container.",
                    "enabled": True,
                }
            ],
        },
        {
            # Hooks surface as a DIRECTORY container, not as plugin children.
            # `.github/hooks` is a real discovery path
            # (sessionCustomizationDiscovery.ts:147, recursive, writable) and
            # the plan doc is explicit that "hooks stay child-only", with the
            # containers being DirectoryCustomizations of `contents: Hook`.
            #
            # Worth knowing when this appears to do nothing: VS Code scans
            # hooks from the PRIMARY working directory only (index 0), so a
            # session whose working directory is not this repo will not find
            # them however they are published.
            "type": "directory",
            "id": "ahs-directory-hooks",
            "uri": f"{_BASE}/.github/hooks",
            "name": "AHS Hooks Directory",
            "contents": "hook",
            "writable": True,
            "enabled": True,
            "children": [
                {
                    "type": "hook",
                    "id": "ahs-hook-pre-tool",
                    "uri": f"{_BASE}/.github/hooks/pre-tool.json",
                    "name": "pre-tool.json",
                    "enabled": True,
                }
            ],
        },
        {
            "type": "mcpServer",
            "id": "ahs-mcp-india",
            "uri": f"{_BASE}/.github/mcp-india",
            "name": "AHS MCP Server India",
            # `state` is a discriminated union on `kind`; `ready` is the
            # no-extra-fields variant.
            "state": {"kind": "ready"},
        },
    ]


def demo_server_tools() -> list[dict[str, Any]]:
    """`SessionState.serverTools` -- NOT customizations, a separate field.

    **Nothing in VS Code 1.131.0 renders these, and no host-side change makes
    it.** Two independent gates, either fatal on its own:

    1. The field is reduced into state and read by no renderer. `serverTools`
       occurs eight times in the shipping bundle -- an MCP-Apps capability
       literal, the coalescing table, the reducer, an unrelated extension
       cache, and the version table. VS Code's OWN host publishes the action
       to itself and does not render it either.
    2. The Tools list is built from `[...toolsService.toolSets, ...static]`
       and nothing else, and the widget is constructed with the compile-time
       constant `"agent-host-copilotcli"`. Our session type is
       `remote-<authority>-<provider>`, so even its checkboxes write
       enablement under a key our session never reads.

    The comment that used to be here blamed `hiddenSections` and the
    `copilotcli` provider id. That was real code and the WRONG path: remote
    (ws://) hosts get `hiddenSections: [Models, McpServers]`, so Tools is not
    hidden for us -- there is simply nothing that can go in it.

    Kept because they are conformant, cost nothing, and another client may
    read them.
    """
    return [
        {
            "name": "ahs_echo_tool",
            "title": "AHS Echo Tool",
            "description": "A demo tool contributed by ahp-host. Echoes its input.",
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
