"""The demo tree has to be real.

A customization is a *pointer*. A client renders one by reading the file behind
its ``uri`` -- so a demo whose URIs name files that do not exist looks, from
the outside, exactly like a host that does not support customizations. That is
not a hypothetical: this suite exists because the demo shipped twice with a
layout invented from the spec, and both times the only symptom was an empty
plugin in someone else's UI.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_host_server.provider.demo_customizations import demo_customizations

_ROOT = Path(__file__).resolve().parents[2] / "examples" / "demo-plugin"

#: The directories a client expands under a plugin URI. Not a guess and not
#: this project's choice -- it is what VS Code's own tests read
#: (`chat/test/browser/actions/createPluginAction.test.ts`), and three of the
#: four obvious names are wrong: prompts live in `commands`, instructions in
#: `rules`, and a skill is a directory holding `SKILL.md`, not a file.
_EXPANDED = ("agents", "commands", "rules", "skills")


def _uris(entries: object) -> list[str]:
    found: list[str] = []
    if isinstance(entries, list):
        for entry in entries:
            found.extend(_uris(entry))
    elif isinstance(entries, dict):
        uri = entries.get("uri")
        if isinstance(uri, str):
            found.append(uri)
        found.extend(_uris(entries.get("children")))
    return found


def _relative(uri: str) -> str:
    marker = "/examples/demo-plugin/"
    assert marker in uri, f"demo URI escaped the demo tree: {uri}"
    return uri.split(marker, 1)[1]


@pytest.mark.parametrize("uri", _uris(demo_customizations()))
def test_every_declared_uri_exists(uri: str) -> None:
    path = _ROOT / _relative(uri)
    if path.name == "mcp-india":
        # An mcpServer's URI names a server, not a file on disk.
        return
    assert path.exists(), f"{uri} points at nothing"


@pytest.mark.parametrize("name", _EXPANDED)
def test_expanded_directories_are_populated(name: str) -> None:
    directory = _ROOT / ".github" / name
    assert directory.is_dir(), f"a client resolves {name}/ under the plugin URI"
    assert any(directory.iterdir()), f"{name}/ is empty, so it renders as nothing"


def test_a_skill_is_a_directory_holding_skill_md() -> None:
    for child in (_ROOT / ".github" / "skills").iterdir():
        assert child.is_dir(), f"{child.name}: a skill is a directory, not a file"
        assert (child / "SKILL.md").is_file(), f"{child.name}/SKILL.md is missing"


def test_no_two_customizations_share_a_uri() -> None:
    """One file, one rendered entry.

    A client builds the tree from what is on disk, so a second customization
    pointing at a URI another already claims does not render at all -- and
    nothing anywhere reports an error. That is how the globbed rule went
    missing from the demo while every test passed.
    """
    uris = _uris(demo_customizations())
    duplicates = {uri for uri in uris if uris.count(uri) > 1}
    assert not duplicates, f"these URIs are claimed twice: {sorted(duplicates)}"


def test_the_model_only_agent_carries_the_field_a_client_reads() -> None:
    """`disableUserInvocation` is declared by the spec and read by nobody.

    VS Code 1.131.0 declares it on AgentCustomization
    (channels-session/state.ts:907) and a grep of both shipping bundles finds
    zero readers. The switch it actually consults is `_meta.userInvocable`, via
    readAgentCustomizationMeta -> provideCustomAgents -> visibility.

    So the demo sends both, and this asserts both: sending only the spec field
    left the model-only agent selectable in the picker, which is the opposite
    of what its own description promises.
    """
    agents = [
        child
        for entry in demo_customizations()
        for child in entry.get("children") or []
        if child.get("type") == "agent"
    ]
    hidden = [a for a in agents if a.get("disableUserInvocation")]
    assert hidden, "the demo no longer shows a model-only agent"
    for agent in hidden:
        meta = agent.get("_meta")
        assert isinstance(meta, dict), f"{agent['id']} has no _meta"
        # A JSON boolean, not a string: the client's reader drops non-booleans,
        # and "false" would leave the agent visible.
        assert meta.get("userInvocable") is False, f"{agent['id']}"
