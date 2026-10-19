"""The flow `[policy]` reaches the inner provider's own policy hook (plan §8 M15, security).

With the run's Approvals set to auto an inner provider stops asking, so a policy consulted only
when a worker asks (`TurnTee.confirm_tool_call`) would stop applying. These tests load the real
ACP and Claude adapters through the flow's loader and check that the flow's denies, and the
provider table's own, still bind there.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest
from ahp_host.node import Roots
from ahp_host.provider.base import AgentSessionContext
from ahp_host.provider.policy import ToolPolicy, ToolRequest
from ahp_host_acp import AcpProvider
from ahp_host_claude import ClaudeProvider
from ahp_host_claude.provider import ClaudeSession

from ahp_host_flow.config import ProviderConfig
from ahp_host_flow.loading import load_provider
from tests.flow_hosting import boot_flow, final_json
from tests.hosting import run_turn

FAKE_AGENT = Path(__file__).resolve().parent.parent / "acp_fake_agent.py"
FLOW_POLICY = ToolPolicy(deny_commands=("git push*",))


def _command(cwd: Path, command: str) -> ToolRequest:
    return ToolRequest("Bash", "command", {"command": command}, cwd, (cwd,))


def _claude(tmp_path: Path, table: dict[str, Any]) -> ClaudeProvider:
    spec = ProviderConfig("claude", "ahp_host_claude:from_config", config=table)
    provider = load_provider(
        spec, host_roots=Roots.single(tmp_path), state_dir=tmp_path / "state", policy=FLOW_POLICY
    )
    assert isinstance(provider, ClaudeProvider)
    return provider


@pytest.mark.parametrize("mode", ["auto", "bypassPermissions"])
async def test_flowpolicy_inner_claude_denies_in_auto(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    provider = _claude(tmp_path, {"policy": {"deny_commands": ["curl *"]}})
    context = AgentSessionContext(
        session_uri="s", chat_uri="c", provider_id="claude", config={"permissionMode": "auto"}
    )
    session = ClaudeSession(
        context,
        root=tmp_path,
        client_factory=lambda o: None,  # type: ignore[arg-type,return-value]
        policy=provider.policy,
    )
    if mode == "bypassPermissions":
        monkeypatch.setattr(session, "_gate", lambda: "bypassPermissions")

    async def decision(command: str) -> Any:
        hook = await session._pre_tool_use(
            {"tool_name": "Bash", "tool_input": {"command": command}}, "t1", None
        )
        return hook.get("hookSpecificOutput", {})

    pushed = await decision("git push origin main")
    assert pushed["permissionDecision"] == "deny"
    assert "'git push*'" in pushed["permissionDecisionReason"]
    # An approve-all wrapper does not hide it either.
    assert (await decision("env FOO=1 git push"))["permissionDecision"] == "deny"
    # The provider table's own policy still applies beside the flow's.
    curled = await decision("curl https://example.com")
    assert curled["permissionDecision"] == "deny"
    assert "'curl *'" in curled["permissionDecisionReason"]
    assert (await decision("ls")).get("permissionDecision") != "deny"


def test_flowpolicy_inner_tables_combine(tmp_path: Path) -> None:
    def check(request: ToolRequest) -> Any:
        from ahp_host.provider.policy import Decision

        return Decision("ask", "table check asks") if "make" in str(request.raw) else None

    provider = _claude(tmp_path, {"policy": {"deny_commands": ["curl *"], "check": check}})
    policy = provider.policy
    assert policy is not None
    assert policy.decide(_command(tmp_path, "git push")).action == "deny"
    assert policy.decide(_command(tmp_path, "curl x")).action == "deny"
    request = ToolRequest(
        "Bash", "command", {"command": "make"}, tmp_path, (tmp_path,), {"command": "make"}
    )
    assert policy.decide(request).action == "ask"
    assert policy.decide(_command(tmp_path, "ls")).action == "allow"
    # No flow policy: the table is handed over untouched.
    spec = ProviderConfig("claude", "ahp_host_claude:from_config", config={})
    bare = load_provider(spec, host_roots=Roots.single(tmp_path), state_dir=tmp_path / "s2")
    assert isinstance(bare, ClaudeProvider)
    assert bare.policy is None


@pytest.mark.parametrize("load", ["ahp_host_acp:from_config", "ahp_host_claude:from_config"])
def test_flowpolicy_inner_combines_with_a_file_policy(tmp_path: Path, load: str) -> None:
    conf = tmp_path / "provider.toml"
    command = f"command = [{json.dumps(sys.executable)}]\n" if "acp" in load else ""
    conf.write_text(command + "[policy]\ndeny_commands = ['curl *']\n", encoding="utf-8")
    spec = ProviderConfig("p", load, config=conf)
    provider = load_provider(
        spec, host_roots=Roots.single(tmp_path), state_dir=tmp_path / "s", policy=FLOW_POLICY
    )
    if isinstance(provider, AcpProvider):
        policy = provider.spec.policy
    else:
        assert isinstance(provider, ClaudeProvider)
        policy = provider.policy
    assert policy is not None
    assert policy.decide(_command(tmp_path, "curl https://example.com")).action == "deny"
    assert policy.decide(_command(tmp_path, "git push origin")).reason == (
        "denied by policy: the command matches 'git push*'"
    )
    assert policy.decide(_command(tmp_path, "ls")).action == "allow"


async def test_flowpolicy_inner_acp_denies_with_approvals_auto(tmp_path: Path) -> None:
    q = json.dumps
    toml = (
        "[policy]\ndeny_commands = ['git push*']\n\n"
        '[providers.acp]\nload = "ahp_host_acp:from_config"\n'
        "[providers.acp.config]\n"
        f"command = [{q(sys.executable)}, {q(str(FAKE_AGENT))}]\n"
        "[providers.acp.config.policy]\n"
        "deny_commands = ['curl *']\n"
    )
    (tmp_path / "ws").mkdir()
    (tmp_path / "user").mkdir()
    (tmp_path / "user" / "perm.py").write_text(
        "meta = {'name': 'perm', 'description': 'd'}\n"
        "\n"
        "async def main(args):\n"
        "    return await agent('perm: git push origin; curl https://example.com')\n",
        encoding="utf-8",
    )
    fx = await boot_flow(
        tmp_path,
        "no-node",
        toml,
        session_config={"permissionMode": "auto"},
        provider_options={"roots": Roots.single(tmp_path / "ws")},
    )
    try:
        turn = await run_turn(fx.wire, fx.chat, "t1", "/perm")
        assert final_json(turn) == "acp: reject reject"
        loaded = fx.host.providers["flow"].providers["acp"]  # type: ignore[attr-defined]
        assert isinstance(loaded, AcpProvider)
        policy = loaded.spec.policy
        assert policy is not None
        ws = tmp_path / "ws"
        assert policy.decide(_command(ws, "git push")).reason == (
            "denied by policy: the command matches 'git push*'"
        )
        assert policy.decide(_command(ws, "curl x")).action == "deny"
    finally:
        await fx.shut()
