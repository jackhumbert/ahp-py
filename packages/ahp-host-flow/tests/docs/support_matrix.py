"""The README's support matrix, rendered from the provider classes.

This lives in ahp-host-flow's tests because that package's dev environment installs every
provider (ahp-host, ahp-host-acp and ahp-host-claude through its extras), so the classes can
be imported side by side. Test code only: `src/` never imports `ahp_host_acp` or
`ahp_host_claude` statically. Print the table with
`python -m tests.docs.support_matrix` from the package directory.
"""

import inspect
from pathlib import Path

from ahp_host.provider.base import (
    FollowsActiveClients,
    ResumesTurns,
    ReturnsStructuredOutput,
    SteersTurns,
    TruncatesHistory,
)
from ahp_host.provider.echo import EchoProvider, EchoSession
from ahp_host_acp.provider import AcpProvider, AcpSession
from ahp_host_claude.provider import ClaudeProvider, ClaudeSession

from ahp_host_flow.provider import FlowProvider, FlowSession

REPO = Path(__file__).resolve().parents[4]

ROWS: tuple[tuple[str, type, type], ...] = (
    ("echo", EchoSession, EchoProvider),
    ("ACP (any agent)", AcpSession, AcpProvider),
    ("ACP (reference agent)", AcpSession, AcpProvider),
    ("Claude", ClaudeSession, ClaudeProvider),
    ("flow", FlowSession, FlowProvider),
)

COLUMNS = (
    "Provider",
    "Session",
    "Steering",
    "Structured output",
    "Client tools",
    "Host policy",
    "Turn resume",
    "Session resume",
    "Fork",
    "History truncation",
    "Changesets",
    "Usage",
    "Cost",
    "Reasoning stream",
)

# Declared cells: behaviour that has no protocol, each stated from the code it comes from.
HOST_POLICY = {
    "echo": "none",
    # permissions.py, client_methods.py
    "ACP (any agent)": "advisory; hard with `host_tools`",
    # writes through fs/write_text_file when offered
    "ACP (reference agent)": "advisory; hard with `host_tools`",
    # the PreToolUse hook
    "Claude": "hard",
    # loading._with_flow_policy
    "flow": "`[policy]`, folded into workers'",
}
FORK = {
    "echo": "transcript only",
    "ACP (any agent)": "native (`session/fork`), else as context",
    "ACP (reference agent)": "as context",
    "Claude": "native",
    "flow": "transcript only",
}
CHANGESETS = {
    "echo": "demo",
    "ACP (any agent)": "from tool diffs",
    "ACP (reference agent)": "from tool diffs",
    "Claude": "from file edits",
    "flow": "worktrees",
}
USAGE = {
    "echo": "turn end (estimated)",
    "ACP (any agent)": "turn end",
    "ACP (reference agent)": "turn end",
    "Claude": "turn end; per request with `usage_per_message`",
    "flow": "per worker call",
}
COST = {
    "echo": "no",
    "ACP (any agent)": "if the agent reports it",
    "ACP (reference agent)": "no",
    "Claude": "yes",
    "flow": "summed from workers",
}
# What a detected "yes" in Session resume means for the agent's own conversation. The class check
# only proves the AHP side (the host restores the session and its transcript); whether the agent
# still holds the conversation depends on the agent, so the cell says so where it can differ.
SESSION_RESUME = {
    # provider._reattach: session/resume, else session/load, else a new ACP session
    "ACP (any agent)": (
        "yes (agent-native through `session/resume` or `session/load`, else host-side only)"
    ),
    # reference/agent.py advertises loadSession false and has no session/resume
    "ACP (reference agent)": "host-side only (the agent starts a new conversation)",
}
REASONING = {
    "echo": "no",
    "ACP (any agent)": "yes",
    "ACP (reference agent)": "yes (`reasoning_content`)",
    "Claude": "yes",
    "flow": "on worker chats",
}


def yes_no(flag: bool) -> str:
    return "yes" if flag else "no"


def resumes_sessions(provider: type) -> bool:
    """ResumableAgentProvider has data members, so issubclass would raise: look at the methods."""
    return all(
        inspect.iscoroutinefunction(getattr(provider, name, None))
        for name in ("resume_session", "resume_state_of")
    )


def detected(session: type, provider: type) -> dict[str, str]:
    return {
        "Steering": yes_no(issubclass(session, SteersTurns)),
        "Structured output": "native"
        if issubclass(session, ReturnsStructuredOutput)
        else "prompted",
        "Client tools": yes_no(issubclass(session, FollowsActiveClients)),
        "Turn resume": yes_no(issubclass(session, ResumesTurns)),
        "Session resume": yes_no(resumes_sessions(provider)),
        "History truncation": yes_no(issubclass(session, TruncatesHistory)),
    }


def render_matrix() -> str:
    lines = ["| " + " | ".join(COLUMNS) + " |", "|" + "---|" * len(COLUMNS)]
    for name, session, provider in ROWS:
        found = detected(session, provider)
        qualifier = SESSION_RESUME.get(name)
        if qualifier is not None and found["Session resume"] == "yes":
            found["Session resume"] = qualifier
        cells = {
            "Provider": name,
            "Session": f"`{session.__name__}`",
            **found,
            "Host policy": HOST_POLICY[name],
            "Fork": FORK[name],
            "Changesets": CHANGESETS[name],
            "Usage": USAGE[name],
            "Cost": COST[name],
            "Reasoning stream": REASONING[name],
        }
        lines.append("| " + " | ".join(cells[column] for column in COLUMNS) + " |")
    return "\n".join(lines)


if __name__ == "__main__":
    print(render_matrix())
