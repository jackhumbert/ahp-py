"""The provider behind a real `Host`: what a client actually sees."""

from __future__ import annotations

import json
from pathlib import Path

from ahp_host import ROOT_URI, Host, LoopbackSingleUserPolicy

from ahp_host_acp.provider import AcpProvider, AgentSpec

from .fakes import FAKE_AGENT
from .hosting import connect, found, open_session, run_turn, shut, state, text_of


def _provider(root: Path, log: Path) -> AcpProvider:
    return AcpProvider(root, AgentSpec(FAKE_AGENT, env={"FAKE_ACP_LOG": str(log)}))


async def test_chats_completions_and_file_edits_through_the_host(tmp_path: Path) -> None:
    (tmp_path / "notes.txt").write_text("one\n")
    host = Host(_provider(tmp_path, tmp_path / "agent.log"), LoopbackSingleUserPolicy())
    wire, serving = await connect(host)
    try:
        # The provider's own declaration, with no Host argument for it.
        assert wire.initialized["completionTriggerCharacters"] == ["/"]
        (agent,) = state(host, ROOT_URI)["agents"]
        assert agent["capabilities"] == {"multipleChats": {}}

        session = "acp:/one"
        default = await open_session(wire, session, "acp")
        await run_turn(wire, default, "t1", "hello")
        second = "ahp-chat:/two"
        response = await wire.request("createChat", {"channel": session, "chat": second})
        assert "error" not in response, response
        await wire.request("subscribe", {"channel": second})

        # A turn on the second chat runs in a conversation of its own.
        who = json.loads(text_of(await run_turn(wire, second, "t2", "whoami")))
        assert who["history"] == []
        mine = json.loads(text_of(await run_turn(wire, default, "t3", "whoami")))
        assert mine["history"] == ["hello"]
        assert mine["session"] != who["session"]

        # An edit's result is a diff the host stored, not just text.
        edited = await run_turn(wire, default, "t4", "edit")
        (edit,) = found(edited, lambda m: m.get("type") == "fileEdit")
        assert edit["after"]["uri"] == (tmp_path / "notes.txt").as_uri()
        assert edit["diff"] == {"added": 1, "removed": 0}
    finally:
        await shut(host, wire, serving)
