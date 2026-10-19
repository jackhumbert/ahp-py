"""Answering an inner agent's tool confirmation from `[approvals]` rules and `[policy]`.

Security decision (plan §8 M15 item 3): the flow-level `[policy]` is checked first and always
wins. A rule can never allow what the policy denies or asks: a policy deny is final, a policy
ask goes to a person, and only a call the policy lets through reaches the rules. A call the
inner provider's own policy asked about (`ToolConfirmation.policy_asked`) is likewise never
approved by a rule. Neither is denied by a rule either: both are prompted, so a rule (allow or
deny) decides only calls the policy lets through and the inner policy did not ask about.
Nothing is decided here when no rule matches: the call is prompted as before.

This hook runs only when a worker asks. So that an approve-all (which stops a worker asking)
cannot switch the policy off, the loader also hands it to the inner provider's own policy hook
(`loading._with_flow_policy`).
"""

from __future__ import annotations

import fnmatch
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, Final

from ahp_host.provider.base import ToolConfirmation, ToolConfirmationOutcome
from ahp_host.provider.policy import ToolPolicy, ToolRequest

from ahp_host_flow.catalog import directory_of
from ahp_host_flow.config import ApprovalRule

# Tool-input keys that hold a path.
PATH_KEYS: Final = ("file_path", "path", "notebook_path")


def policy_request(call: ToolConfirmation, *, cwd: Path, roots: tuple[Path, ...]) -> ToolRequest:
    """Describe *call* to a `ToolPolicy`: a command, a read or write of paths, or any other tool."""
    raw: Mapping[str, Any] = call.tool_input if isinstance(call.tool_input, Mapping) else {}
    kind = "tool"
    request_input: Mapping[str, Any] = {}
    command = raw.get("command")
    if isinstance(command, str):
        kind, request_input = "command", {"command": command}
    else:
        paths = [v for key in PATH_KEYS if isinstance(v := raw.get(key), str)]
        listed = raw.get("paths")
        if isinstance(listed, list):
            paths.extend(v for v in listed if isinstance(v, str))
        # ACP names a call's paths in `toolCall.locations`, beside its input.
        paths.extend(call.locations)
        located = raw.get("locations")
        if isinstance(located, list):
            paths.extend(
                loc["path"]
                for loc in located
                if isinstance(loc, Mapping) and isinstance(loc.get("path"), str)
            )
        for edit in call.edits:
            for uri in (edit.uri, edit.renamed_to):
                if uri is not None and (path := directory_of(uri)) is not None:
                    paths.append(str(path))
        # A call the provider calls a read or a write is a file tool, even when no path can
        # be read from it: judged with no paths, which a policy with path rules asks about
        # (fail closed) instead of letting it through as "any other tool".
        if paths or call.read_only is not None:
            kind = "read" if call.read_only is True else "write"
            request_input = {"paths": tuple(dict.fromkeys(paths))}
    return ToolRequest(call.name, kind, request_input, cwd, roots, dict(raw))


class Approver:
    """Decides a confirmation from the policy and the rules, or returns None to prompt."""

    def __init__(
        self,
        rules: tuple[ApprovalRule, ...],
        policy: ToolPolicy | None,
        *,
        provider: str,
        phase: str | None,
        principal: str | None,
        cwd: Path,
        roots: tuple[Path, ...],
        label: str,
        on_log: Callable[[str], None],
    ) -> None:
        self._rules = rules
        self._policy = policy
        self._provider = provider
        self._phase = phase
        self._principal = principal
        self._cwd = cwd
        self._roots = roots
        self._label = label
        self._log = on_log

    def decide(self, call: ToolConfirmation) -> ToolConfirmationOutcome | None:
        if self._policy is not None:
            verdict = self._policy.decide(policy_request(call, cwd=self._cwd, roots=self._roots))
            if verdict.action == "deny":
                reason = verdict.reason or "denied by policy"
                self._log(f"{self._label}: {call.name}: {reason}")
                return self._denial(call, reason)
            if verdict.action == "ask":
                return None
        if call.policy_asked:
            return None
        for n, rule in enumerate(self._rules, 1):
            if not self._matches(rule, call):
                continue
            if rule.action == "allow":
                self._log(f"{self._label}: {call.name} approved by rule {n}")
                return ToolConfirmationOutcome(approved=True, tool_input=call.tool_input)
            self._log(f"{self._label}: {call.name} denied by rule {n}")
            return self._denial(call, f"denied by rule {n}")
        return None

    def _matches(self, rule: ApprovalRule, call: ToolConfirmation) -> bool:
        if rule.tool is not None and not fnmatch.fnmatchcase(call.name, rule.tool):
            return False
        if rule.read_only is not None:
            if call.read_only is not rule.read_only:
                return False
            # Security decision: an agent-supplied hint (ACP's `toolCall.kind`) is the
            # agent's word; any call can be labelled a read. It matches only a rule that
            # says to trust it, so a `read_only` rule cannot be steered by the agent.
            if call.read_only_from_agent and not rule.trust_agent_hints:
                return False
        if rule.provider is not None and rule.provider != self._provider:
            return False
        if rule.phase is not None and (self._phase is None or rule.phase != self._phase):
            return False
        return rule.persons is None or (
            self._principal is not None and self._principal in rule.persons
        )

    @staticmethod
    def _denial(call: ToolConfirmation, message: str) -> ToolConfirmationOutcome:
        return ToolConfirmationOutcome(
            approved=False, tool_input=call.tool_input, reason="denied", reason_message=message
        )
