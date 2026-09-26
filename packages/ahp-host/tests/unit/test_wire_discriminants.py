"""The literal key names, pinned.

Every defect this file guards was the same shape: a plausible key that no
client reads, shipped green because nothing anywhere asserted the literal
string. The conformance corpora cannot catch them -- the session reducer copies
`changesets` wholesale, and the fixtures compare structures the host produced
against structures the host produced.

The protocol genuinely mixes the two conventions, which is why guessing loses:

    ResponsePartKind         -> `kind`        (markdown, reasoning, toolCall)
    ChatSourceKind           -> `kind`        (fork, sideChat)
    ToolCallContributor      -> `kind`        (client, mcp)
    ChatInputQuestion        -> `kind`
    MessageAttachmentKind    -> `type`        (resource, embeddedResource)
    ToolResultContentType    -> `type`        (text, fileEdit, terminal)
    Changeset                -> `changeKind`  (neither!)

There is no rule to derive. Read the type.
"""

from __future__ import annotations

import json

from ahp_host.core.changesets import (
    Changeset,
    ChangesetOperation,
    ContentStore,
    FileChange,
    changes_summary,
    diff_counts,
    file_entry,
)
from ahp_host.core.config import RootConfig


class TestChangesetKeys:
    def test_the_catalogue_entry_says_change_kind(self) -> None:
        """`kind` produced an empty Changes view -- no tree, no diff, no
        review -- because the client's builder pushes nothing for a value it
        does not recognise, and logs nothing either."""
        entry = Changeset(label="c").to_catalogue_entry()
        assert entry["changeKind"] == "session"
        assert "kind" not in entry

    def test_per_file_diff_uses_added_and_removed(self) -> None:
        """`FileEdit.diff` is {added, removed}. We sent the SessionSummary
        names, so every file rendered +0 -0."""
        counts = diff_counts(b"one\n", b"one\ntwo\n")
        assert set(counts) == {"added", "removed"}
        assert counts == {"added": 1, "removed": 0}

    def test_the_summary_roll_up_uses_additions_and_deletions(self) -> None:
        """And this one really is different. Unifying them would break it."""
        store = ContentStore()
        entry = file_entry(FileChange(uri="file:///a", before=b"x\n", after=b"y\n"), store)
        summary = changes_summary([entry])
        assert set(summary) == {"files", "additions", "deletions"}
        assert summary["additions"] == 1
        assert summary["deletions"] == 1

    def test_an_operation_carries_the_two_required_fields(self) -> None:
        """Omitting them made the client's operations derived throw. A derived
        that throws is swallowed, so ONE malformed operation silently discarded
        every operation on the changeset."""
        wire = ChangesetOperation(id="stage", label="Stage").to_wire()
        assert wire["scopes"] == ["changeset"]
        assert wire["status"] == "idle"


class TestToolCallKeys:
    def test_tool_result_content_is_discriminated_by_type(self) -> None:
        """`ToolResultContentType`, not a `kind`. Every reader switches on
        `.type`, so a `kind` block is dropped and the output pane is empty."""
        from ahp_host.provider.echo import EchoSession

        source = EchoSession.__module__
        assert source  # keep the import meaningful
        # Asserted at the emission site rather than through a live turn: the
        # demo only emits these under --confirm-tools.
        import inspect

        import ahp_host.provider.echo as echo

        text = inspect.getsource(echo)
        assert '{"kind": "text"' not in text, "a tool result content block used `kind`"
        assert '{"type": "text"' in text

    def test_tool_input_is_json_encoded(self) -> None:
        """`ToolInput = string | ContentRef`. A bare object made the client
        abort the invocation and synthesise a failure, so client-contributed
        tools -- a feature this host advertises -- could never run."""
        from ahp_host.core.turn import _encoded_tool_input

        assert _encoded_tool_input({"text": "hi"}) == json.dumps({"text": "hi"})
        assert _encoded_tool_input("already a string") == "already a string"
        # A ContentRef is the one object form the union allows.
        assert _encoded_tool_input({"uri": "file:///x"}) == {"uri": "file:///x"}

    def test_a_result_always_carries_success_and_past_tense(self) -> None:
        """Both REQUIRED. Without them the client computes `completed &&
        success` as falsey and the row stays present-tense forever."""
        from ahp_host.core.turn import _tool_result

        assert _tool_result(None, True, None) == {
            "success": True,
            "pastTenseMessage": "Ran the tool",
        }
        failed = _tool_result({"content": []}, False, "Echo failed")
        assert failed["success"] is False
        assert failed["pastTenseMessage"] == "Echo failed"


class TestModelInfo:
    """`SessionModelInfo.provider` is non-optional, and we omitted it.

    The bare `{"id", "name"}` mapping was accepted in silence, so every adapter
    copied from the demo shipped the same incomplete picker: no section header,
    no "Max context" row, no usage meter, and image attachments refused.
    """

    def test_provider_defaults_to_the_owning_agent(self) -> None:
        from ahp_host.provider.base import AgentInfo, ModelInfo

        wire = AgentInfo(
            provider="echo",
            display_name="Echo",
            description="d",
            models=(ModelInfo(id="m", name="M"),),
        ).to_wire()
        assert wire["models"][0]["provider"] == "echo"

    def test_a_raw_mapping_still_gets_a_provider(self) -> None:
        """An embedder already building wire dicts is not forced to migrate,
        but is not left publishing an ungroupable model either."""
        from ahp_host.provider.base import AgentInfo

        wire = AgentInfo(
            provider="echo",
            display_name="Echo",
            description="d",
            models=({"id": "m", "name": "M"},),
        ).to_wire()
        assert wire["models"][0]["provider"] == "echo"

    def test_optional_fields_are_omitted_not_nulled(self) -> None:
        from ahp_host.provider.base import ModelInfo

        assert ModelInfo(id="m", name="M").to_wire("p") == {
            "id": "m",
            "name": "M",
            "provider": "p",
        }

    def test_the_demo_model_carries_token_limits(self) -> None:
        """Their ABSENCE is what a reader of echo.py would copy."""
        from ahp_host.provider import EchoProvider

        model = EchoProvider().agent.to_wire()["models"][0]
        assert model["maxPromptTokens"] > 0
        assert model["maxOutputTokens"] > 0
        assert model["policyState"] == "enabled"


class TestDemoRootConfig:
    """The keys a client really pushes, so they stop being dropped."""

    def test_every_observed_key_is_declared(self) -> None:
        from ahp_host.__main__ import DEMO_ROOT_CONFIG_PROPERTIES

        # Captured from real connections. `terminalAutoApproveRules` and the two
        # auto-approve booleans are the ONLY channel by which the user's
        # auto-approve preferences reach a host at all.
        observed = {
            "telemetryLevel": "all",
            "sessionSyncEnabled": False,
            "terminalAutoApproveEnabled": True,
            "globalAutoApproveEnabled": False,
            "autoReplyEnabled": False,
            "preferLongContextEnabled": False,
            "systemProxyEnabled": True,
            "terminalAutoApproveRules": {"ls": True},
            "codexAgentEnabled": False,
            "disableRepoInfoTelemetry": False,
        }
        config = RootConfig(properties=DEMO_ROOT_CONFIG_PROPERTIES)
        for key, value in observed.items():
            assert config.rejection(key, value) is None, f"{key} would be dropped"

    def test_an_undeclared_key_is_still_refused(self) -> None:
        """Accepting the ten does not mean accepting anything."""
        from ahp_host.__main__ import DEMO_ROOT_CONFIG_PROPERTIES

        config = RootConfig(properties=DEMO_ROOT_CONFIG_PROPERTIES)
        assert config.rejection("somethingElse", 1) is not None
        assert config.rejection("telemetryLevel", "not-an-enum-member") is not None
