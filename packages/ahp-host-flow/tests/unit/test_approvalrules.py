"""Approval rules and the flow policy: config, the request mapping and the decision order."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from ahp_host.provider.base import ToolConfirmation
from ahp_host.provider.changes import FileChange
from ahp_host.provider.policy import ToolPolicy

from ahp_host_flow.approvals import Approver, policy_request
from ahp_host_flow.config import ApprovalRule, ConfigError, parse_config

PROVIDERS = {"claude": {"load": "m:C"}, "other": {"load": "m:D"}}


def _config(tmp_path: Path, **extra: Any) -> Any:
    # Two providers (the rules name one) need an explicit routing.default.
    return parse_config(
        {"providers": PROVIDERS, "routing": {"default": "claude"}, **extra}, base=tmp_path
    )


def _call(name: str = "Read", tool_input: Any = None, **kw: Any) -> ToolConfirmation:
    return ToolConfirmation("c1", name, f"Run {name}", tool_input=tool_input, **kw)


def _approver(
    rules: tuple[ApprovalRule, ...],
    policy: ToolPolicy | None = None,
    log: list[str] | None = None,
    **kw: Any,
) -> Approver:
    args: dict[str, Any] = {
        "provider": "claude",
        "phase": None,
        "principal": None,
        "cwd": Path.cwd(),
        "roots": (Path.cwd(),),
        "label": "agent() #0",
        "on_log": (log if log is not None else []).append,
    }
    return Approver(rules, policy, **{**args, **kw})


def test_approvalrules_config_parses(tmp_path: Path) -> None:
    cfg = _config(
        tmp_path,
        approvals={
            "rules": [
                {"read_only": True, "action": "allow"},
                {
                    "tool": "Bash",
                    "provider": "other",
                    "phase": "Build",
                    "persons": ["alice", "bob"],
                    "action": "deny",
                },
            ]
        },
        policy={"deny_commands": ["git push*"]},
    )
    assert cfg.approvals.rules == (
        ApprovalRule("allow", read_only=True),
        ApprovalRule("deny", "Bash", None, "other", "Build", ("alice", "bob")),
    )
    assert cfg.policy is not None
    assert cfg.policy.deny_commands == ("git push*",)
    assert _config(tmp_path).policy is None


@pytest.mark.parametrize(
    ("rule", "key"),
    [
        ({"tool": "*"}, "approvals.rules[0].action"),
        ({"tool": "*", "action": "maybe"}, "approvals.rules[0].action"),
        ({"action": "allow"}, "approvals.rules[0]"),
        ({"tool": "", "action": "allow"}, "approvals.rules[0].tool"),
        ({"phase": "", "action": "allow"}, "approvals.rules[0].phase"),
        ({"read_only": "yes", "action": "allow"}, "approvals.rules[0].read_only"),
        ({"provider": "nope", "action": "allow"}, "approvals.rules[0].provider"),
        ({"tool": "*", "persons": [], "action": "allow"}, "approvals.rules[0].persons"),
        ({"tool": "*", "persons": [""], "action": "allow"}, "approvals.rules[0].persons"),
        ({"tool": "*", "persons": "alice", "action": "allow"}, "approvals.rules[0].persons"),
        ({"tool": "*", "action": "allow", "extra": 1}, "approvals.rules[0]"),
    ],
)
def test_approvalrules_config_errors(tmp_path: Path, rule: dict[str, Any], key: str) -> None:
    with pytest.raises(ConfigError, match=key.replace("[", r"\[").replace("]", r"\]")):
        _config(tmp_path, approvals={"rules": [rule]})
    with pytest.raises(ConfigError, match="approvals"):
        _config(tmp_path, approvals={"nope": []})
    with pytest.raises(ConfigError, match="policy"):
        _config(tmp_path, policy={"nope": []})


def test_approvalrules_policy_request_pinned(tmp_path: Path) -> None:
    def make(call: ToolConfirmation) -> Any:
        return policy_request(call, cwd=tmp_path, roots=(tmp_path,))

    cmd = make(_call("Bash", {"command": "ls -l", "path": "x"}))
    assert (cmd.name, cmd.kind, dict(cmd.input)) == ("Bash", "command", {"command": "ls -l"})
    assert dict(cmd.raw) == {"command": "ls -l", "path": "x"}
    assert (cmd.cwd, cmd.roots) == (tmp_path, (tmp_path,))

    one = make(_call("Edit", {"file_path": "a"}))
    assert (one.kind, dict(one.input)) == ("write", {"paths": ("a",)})
    many = make(_call("Grep", {"path": "a", "paths": ["b", 3, "a"], "notebook_path": "n"}))
    assert dict(many.input) == {"paths": ("a", "n", "b")}

    target = tmp_path / "f.txt"
    edited = make(_call("Patch", {}, edits=(FileChange(uri=target.as_uri(), after=b"x"),)))
    assert (edited.kind, dict(edited.input)) == ("write", {"paths": (str(target),)})

    assert make(_call("Read", {"file_path": "a"}, read_only=True)).kind == "read"
    assert make(_call("Read", {"file_path": "a"}, read_only=False)).kind == "write"
    assert make(_call("Read", {"file_path": "a"})).kind == "write"

    bare = make(_call("Task", "not a mapping"))
    assert (bare.kind, dict(bare.input), dict(bare.raw)) == ("tool", {}, {})
    assert make(_call("Task", {"x": 1})).kind == "tool"


def test_approvalrules_policy_deny_beats_allow_rule() -> None:
    log: list[str] = []
    approver = _approver(
        (ApprovalRule("allow", tool="*"),), ToolPolicy(deny_commands=("git push*",)), log
    )
    call = _call("Bash", {"command": "git push"})
    out = approver.decide(call)
    assert out is not None
    assert out.approved is False
    assert out.reason == "denied"
    assert out.reason_message == "denied by policy: the command matches 'git push*'"
    assert out.tool_input == call.tool_input
    assert log == ["agent() #0: Bash: denied by policy: the command matches 'git push*'"]


def test_approvalrules_policy_ask_and_policy_asked_prompt() -> None:
    allow = (ApprovalRule("allow", tool="*"),)
    asking = _approver(allow, ToolPolicy(ask_paths=("*.env",)))
    assert asking.decide(_call("Read", {"file_path": "x.env"}, read_only=True)) is None
    assert asking.decide(_call("Read", {"file_path": "x.txt"}, read_only=True)) is not None
    plain = _approver(allow)
    assert plain.decide(_call("Bash", {"command": "ls"})) is not None
    assert plain.decide(_call("Bash", {"command": "ls"}, policy_asked=True)) is None


def test_approvalrules_read_only_rule() -> None:
    approver = _approver((ApprovalRule("allow", read_only=True),))
    out = approver.decide(_call("Read", read_only=True))
    assert out is not None
    assert out.approved is True
    assert out.selected_option is None
    assert approver.decide(_call("Read", read_only=False)) is None
    assert approver.decide(_call("Read", read_only=None)) is None


def test_approvalrules_person_scoped() -> None:
    rules = (ApprovalRule("allow", persons=("alice",), tool="*"),)
    assert _approver(rules, principal="alice").decide(_call()) is not None
    assert _approver(rules, principal="bob").decide(_call()) is None
    assert _approver(rules, principal=None).decide(_call()) is None


def test_approvalrules_first_match_and_log() -> None:
    log: list[str] = []
    rules = (
        ApprovalRule("allow", tool="R*", provider="claude"),
        ApprovalRule("allow", tool="Bash"),
        ApprovalRule("deny", tool="*"),
    )
    approver = _approver(rules, log=log)
    assert approver.decide(_call("Read")) is not None
    assert log == ["agent() #0: Read approved by rule 1"]
    denied = approver.decide(_call("Write"))
    assert denied is not None
    assert (denied.approved, denied.reason_message) == (False, "denied by rule 3")
    assert log[-1] == "agent() #0: Write denied by rule 3"
    phased = _approver((ApprovalRule("allow", phase="Build"),), phase=None)
    assert phased.decide(_call()) is None
    assert _approver((ApprovalRule("allow", phase="Build"),), phase="Build").decide(_call())


def test_approvalrules_agent_read_only_hint_needs_trust(tmp_path: Path) -> None:
    """An agent can call any tool a read (ACP's `kind`): a read_only rule must opt in to it."""
    untrusted = _approver((ApprovalRule("allow", read_only=True),))
    # A delete the agent labelled "read": the hint is all the rule would see.
    lying = _call("rm -rf ~", {"path": "/"}, read_only=True, read_only_from_agent=True)
    assert untrusted.decide(lying) is None
    # The provider's own hint (Claude's tool table) still matches.
    assert untrusted.decide(_call("Read", read_only=True)) is not None
    trusted = _approver((ApprovalRule("allow", read_only=True, trust_agent_hints=True),))
    out = trusted.decide(_call("read", read_only=True, read_only_from_agent=True))
    assert out is not None
    assert out.approved is True
    # A rule without a read_only key is not affected.
    assert _approver((ApprovalRule("allow", tool="read"),)).decide(lying) is None
    named = _approver((ApprovalRule("allow", tool="read"),))
    assert named.decide(_call("read", read_only=True, read_only_from_agent=True)) is not None
    cfg = _config(
        tmp_path,
        approvals={"rules": [{"read_only": True, "trust_agent_hints": True, "action": "allow"}]},
    )
    assert cfg.approvals.rules == (ApprovalRule("allow", read_only=True, trust_agent_hints=True),)
    with pytest.raises(ConfigError, match=r"approvals\.rules\[0\]\.trust_agent_hints"):
        _config(
            tmp_path,
            approvals={"rules": [{"read_only": True, "trust_agent_hints": 1, "action": "allow"}]},
        )


def test_approvalrules_pathless_read_fails_closed(tmp_path: Path) -> None:
    def make(call: ToolConfirmation) -> Any:
        return policy_request(call, cwd=tmp_path, roots=(tmp_path,))

    # ACP's read with its path only in `locations`.
    located = make(_call("read", {}, read_only=True, locations=(str(tmp_path / ".env"),)))
    assert (located.kind, dict(located.input)) == ("read", {"paths": (str(tmp_path / ".env"),)})
    in_raw = make(_call("read", {"locations": [{"path": "a.txt"}, {"line": 3}]}, read_only=True))
    assert dict(in_raw.input) == {"paths": ("a.txt",)}
    # No path at all: judged as a read of nothing, not as "any other tool".
    bare = make(_call("read", {}, read_only=True))
    assert (bare.kind, dict(bare.input)) == ("read", {"paths": ()})

    allow_all = (ApprovalRule("allow", tool="*"),)
    guarded = _approver(allow_all, ToolPolicy(deny_paths=("**/.env",)), cwd=tmp_path)
    denied = guarded.decide(_call("read", {}, read_only=True, locations=(str(tmp_path / ".env"),)))
    assert denied is not None
    assert denied.approved is False
    # A path-less read with path rules asks a person; the allow-all rule cannot answer it.
    assert guarded.decide(_call("read", {}, read_only=True)) is None
    # Without path rules nothing changes: the rule answers it.
    plain = _approver(allow_all, ToolPolicy(deny_commands=("git push*",)), cwd=tmp_path)
    assert plain.decide(_call("read", {}, read_only=True)) is not None
