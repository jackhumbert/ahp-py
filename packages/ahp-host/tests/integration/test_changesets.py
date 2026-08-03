"""Changesets: the catalogue, the channel, the diffs, review and operations.

The interesting property is that a changeset renders on a host that exposes **no
filesystem at all**. Content lives in a per-session store addressed by hash and
is served through a scoped `resourceRead`, so a diff needs the bytes as they were
*before* the edit -- which by the time anyone asks are no longer on disk.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Mapping
from typing import Any

import pytest
from agent_host_protocol.channels import ROOT_URI
from agent_host_protocol.transport import memory_pair

from agent_host_server.core import Host, LoopbackSingleUserPolicy
from agent_host_server.core.changesets import Changeset, ChangesetOperation, FileChange
from agent_host_server.provider import EchoProvider

from .test_host_end_to_end import FakeClient

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
async def host() -> AsyncIterator[Host]:
    host = Host(EchoProvider(), LoopbackSingleUserPolicy())
    try:
        yield host
    finally:
        await host.aclose()


async def _session(host: Host, uri: str) -> FakeClient:
    client_transport, server_transport = memory_pair()
    task = asyncio.create_task(host.serve(server_transport))
    client = FakeClient(client_transport)
    client.serve_task = task  # type: ignore[attr-defined]
    await client.request(
        "initialize",
        {
            "channel": ROOT_URI,
            "clientId": "c1",
            "protocolVersions": ["0.7.0"],
            "initialSubscriptions": [ROOT_URI],
        },
    )
    await client.request("createSession", {"channel": uri, "provider": "echo"})
    await client.collect(seconds=0.3)
    await client.request("subscribe", {"channel": uri})
    return client


_EDIT = FileChange(
    uri="file:///work/main.py",
    before=b"print('one')\n",
    after=b"print('one')\nprint('two')\n",
)


class TestPublishing:
    async def test_the_catalogue_and_the_channel_both_appear(self, host: Host) -> None:
        uri = "echo:/cs-1"
        client = await _session(host, uri)
        channel = await host.publish_changeset(uri, Changeset(label="Session changes"), [_EDIT])
        await client.collect(seconds=0.3)

        state = (await client.request("subscribe", {"channel": uri}))["result"]["snapshot"]["state"]
        catalogue = state["changesets"]
        assert catalogue[0]["label"] == "Session changes"
        # A variable-free template "is itself a subscribable URI", so the
        # catalogue entry and the channel are the same string and nothing ever
        # parses a channel URI.
        assert catalogue[0]["uriTemplate"] == channel

        changeset = (await client.request("subscribe", {"channel": channel}))["result"]["snapshot"][
            "state"
        ]
        assert changeset["status"] == "ready"
        assert [f["id"] for f in changeset["files"]] == ["file:///work/main.py"]

    async def test_line_counts_are_computed(self, host: Host) -> None:
        uri = "echo:/cs-2"
        client = await _session(host, uri)
        channel = await host.publish_changeset(uri, Changeset(label="c"), [_EDIT])
        state = (await client.request("subscribe", {"channel": channel}))["result"]["snapshot"][
            "state"
        ]
        assert state["files"][0]["edit"]["diff"] == {"added": 1, "removed": 0}

    async def test_the_session_summary_carries_the_roll_up(self, host: Host) -> None:
        """Summed from the per-file diffs the host already computed, so the
        number in the session list and the number in the changeset agree."""
        uri = "echo:/cs-3"
        client = await _session(host, uri)
        await host.publish_changeset(uri, Changeset(label="c"), [_EDIT])
        await client.collect(seconds=0.3)
        item = (await client.request("listSessions", {"channel": ROOT_URI}))["result"]["items"][0]
        assert item["changes"] == {"files": 1, "additions": 1, "deletions": 0}

    async def test_the_roll_up_never_enters_the_session_channel(self, host: Host) -> None:
        """`SessionSummary` declares `changes`; `SessionState` does not.

        It was written straight into the session channel's state with no action
        behind it, so a client that subscribed afterwards was served a key the
        schema does not define while every client already subscribed kept the
        old number forever -- there being no envelope to carry the new one.
        `root/sessionSummaryChanged` is where it belongs, and it goes there.
        """
        uri = "echo:/cs-3b"
        client = await _session(host, uri)
        await client.collect(seconds=0.2)
        await host.publish_changeset(uri, Changeset(label="c"), [_EDIT])
        await client.collect(seconds=0.3)

        state = (await client.request("subscribe", {"channel": uri}))["result"]["snapshot"]["state"]
        assert "changes" not in state, "an undeclared key is served in SessionState"
        summaries = [
            n["params"]["changes"]
            for n in client.notifications
            if n.get("method") == "root/sessionSummaryChanged"
        ]
        assert any("changes" in c for c in summaries), "the roll-up reached nobody"

    async def test_content_is_readable_without_any_filesystem(self, host: Host) -> None:
        """The point of the store. This host installs no resource provider at
        all, and the diff still resolves -- because `before` no longer exists on
        disk by the time anyone asks for it."""
        uri = "echo:/cs-4"
        client = await _session(host, uri)
        channel = await host.publish_changeset(uri, Changeset(label="c"), [_EDIT])
        state = (await client.request("subscribe", {"channel": channel}))["result"]["snapshot"][
            "state"
        ]
        before_uri = state["files"][0]["edit"]["before"]["content"]["uri"]

        result = (await client.request("resourceRead", {"channel": ROOT_URI, "uri": before_uri}))[
            "result"
        ]
        assert result["data"] == "print('one')\n"

    async def test_a_creation_has_no_before(self, host: Host) -> None:
        uri = "echo:/cs-5"
        client = await _session(host, uri)
        channel = await host.publish_changeset(
            uri,
            Changeset(label="c"),
            [FileChange(uri="file:///work/new.py", after=b"fresh\n")],
        )
        state = (await client.request("subscribe", {"channel": channel}))["result"]["snapshot"][
            "state"
        ]
        edit = state["files"][0]["edit"]
        assert "before" not in edit
        assert edit["after"]["uri"] == "file:///work/new.py"

    async def test_a_deletion_keeps_its_id(self, host: Host) -> None:
        """ "Typically `after.uri` (or `before.uri` for deletions)\""""
        uri = "echo:/cs-6"
        client = await _session(host, uri)
        channel = await host.publish_changeset(
            uri,
            Changeset(label="c"),
            [FileChange(uri="file:///work/gone.py", before=b"bye\n")],
        )
        state = (await client.request("subscribe", {"channel": channel}))["result"]["snapshot"][
            "state"
        ]
        assert state["files"][0]["id"] == "file:///work/gone.py"
        assert "after" not in state["files"][0]["edit"]

    async def test_binary_content_reports_no_line_counts(self, host: Host) -> None:
        """Better than a bogus number: a client renders "changed" with no diff."""
        uri = "echo:/cs-7"
        client = await _session(host, uri)
        channel = await host.publish_changeset(
            uri,
            Changeset(label="c"),
            [FileChange(uri="file:///work/logo.png", before=b"\x89PNG\x00", after=b"\x89PNG\x01")],
        )
        state = (await client.request("subscribe", {"channel": channel}))["result"]["snapshot"][
            "state"
        ]
        # `added`/`removed`: what FileEdit.diff declares. Not the
        # `additions`/`deletions` of SessionSummary.changes -- the two
        # structures use different names, and sending the summary's names
        # per file rendered every file as +0 -0.
        assert state["files"][0]["edit"]["diff"] == {"added": 0, "removed": 0}

    async def test_identical_content_is_stored_once(self, host: Host) -> None:
        """Addressed by hash, so re-publishing an unchanged file does not grow
        the store."""
        uri = "echo:/cs-8"
        await _session(host, uri)
        same = FileChange(uri="file:///work/a", before=b"x", after=b"y")
        also = FileChange(uri="file:///work/b", before=b"x", after=b"y")
        await host.publish_changeset(uri, Changeset(label="c"), [same, also])
        assert len(host._sessions[uri].content) == 2

    async def test_disposing_the_session_drops_the_changeset_channel(self, host: Host) -> None:
        uri = "echo:/cs-9"
        client = await _session(host, uri)
        channel = await host.publish_changeset(uri, Changeset(label="c"), [_EDIT])
        await client.request("disposeSession", {"channel": uri})
        assert not host.sequencer.has_channel(channel)

    async def test_disposal_says_cleared_before_dropping_the_channel(self, host: Host) -> None:
        """ "Existing subscriptions receive `changeset/cleared` and the server
        unsubscribes them" (changesets guide, lifecycle step 5).

        `drop_channel` discards subscribers silently, so without the terminal
        action first, a subscriber's stream just stopped and it rendered the
        last file list forever."""
        uri = "echo:/cs-10"
        client = await _session(host, uri)
        channel = await host.publish_changeset(uri, Changeset(label="c"), [_EDIT])
        await client.request("subscribe", {"channel": channel})

        await client.request("disposeSession", {"channel": uri})
        await client.collect(seconds=0.3)

        kinds = [e["action"]["type"] for e in client.actions(channel)]
        assert "changeset/cleared" in kinds, "the subscriber never heard the changeset end"


class TestReview:
    async def test_review_is_refused_unless_advertised(self, host: Host) -> None:
        """ "Requires the changeset to advertise `capabilities.review`." The
        changeset channel has no validation table at all, so the reducer checks
        nothing and a peer could otherwise set a flag no client renders."""
        uri = "echo:/cs-r1"
        client = await _session(host, uri)
        channel = await host.publish_changeset(uri, Changeset(label="c"), [_EDIT])
        await client.request("subscribe", {"channel": channel})

        await client.notify(
            "dispatchAction",
            {
                "channel": channel,
                "clientSeq": 1,
                "action": {
                    "type": "changeset/filesReviewChanged",
                    "files": ["file:///work/main.py"],
                    "reviewed": True,
                },
            },
        )
        await client.collect(seconds=0.3)
        echoes = [
            e
            for e in client.actions(channel)
            if e["action"]["type"] == "changeset/filesReviewChanged"
        ]
        assert echoes
        assert "rejectionReason" in echoes[-1]

    async def test_review_works_when_advertised(self, host: Host) -> None:
        uri = "echo:/cs-r2"
        client = await _session(host, uri)
        channel = await host.publish_changeset(uri, Changeset(label="c", reviewable=True), [_EDIT])
        await client.request("subscribe", {"channel": channel})

        await client.notify(
            "dispatchAction",
            {
                "channel": channel,
                "clientSeq": 1,
                "action": {
                    "type": "changeset/filesReviewChanged",
                    "files": ["file:///work/main.py"],
                    "reviewed": True,
                },
            },
        )
        await client.collect(seconds=0.3)
        state = (await client.request("subscribe", {"channel": channel}))["result"]["snapshot"][
            "state"
        ]
        assert state["files"][0]["reviewed"] is True

    async def test_the_capability_is_a_presence_flag(self, host: Host) -> None:
        uri = "echo:/cs-r3"
        client = await _session(host, uri)
        await host.publish_changeset(uri, Changeset(label="c", reviewable=True), [_EDIT])
        state = (await client.request("subscribe", {"channel": uri}))["result"]["snapshot"]["state"]
        assert state["changesets"][0]["capabilities"] == {"review": {}}


class TestTheCatalogueStaysCurrent:
    """The catalogue entry was published once and never again.

    It is the only copy a client has of the label, the description and
    `capabilities.review`, and `_validate_review` reads the *updated* entry --
    so a changeset that became reviewable rendered no checkboxes while the host
    accepted review on it, and one that stopped being reviewable rendered
    checkboxes the host then refused.
    """

    def _catalogue(self, client: FakeClient, uri: str) -> list[list[dict[str, Any]]]:
        return [
            a["action"]["changesets"]
            for a in client.actions(uri)
            if a["action"]["type"] == "session/changesetsChanged"
        ]

    async def test_a_changed_entry_is_re_emitted(self, host: Host) -> None:
        uri = "echo:/cat-1"
        client = await _session(host, uri)
        channel = await host.publish_changeset(
            uri, Changeset(label="Before", description="old", reviewable=False), [_EDIT]
        )
        await host.publish_changeset(
            uri,
            Changeset(uri=channel, label="After", description="new", reviewable=True),
            [_EDIT],
        )
        await client.collect(seconds=0.3)

        state = (await client.request("subscribe", {"channel": uri}))["result"]["snapshot"]["state"]
        entry = state["changesets"][0]
        assert entry["label"] == "After"
        assert entry["description"] == "new"
        assert entry["capabilities"] == {"review": {}}
        # And it was carried by an action, not just healed in the snapshot: a
        # client subscribed the whole time has no other way to learn of it.
        assert self._catalogue(client, uri)[-1][0]["label"] == "After"

    async def test_an_unchanged_entry_is_not_re_emitted(self, host: Host) -> None:
        """Full-replacement semantics make a redundant catalogue frame a
        re-render of every row in the picker, and a republish happens after
        every operation."""
        uri = "echo:/cat-2"
        client = await _session(host, uri)
        changeset = Changeset(label="c", reviewable=True)
        await host.publish_changeset(uri, changeset, [_EDIT])
        await client.collect(seconds=0.3)
        once = len(self._catalogue(client, uri))

        await host.publish_changeset(
            uri, Changeset(uri=changeset.uri, label="c", reviewable=True), [_EDIT]
        )
        await client.collect(seconds=0.3)
        assert len(self._catalogue(client, uri)) == once

    async def test_the_review_gate_and_the_client_agree(self, host: Host) -> None:
        """The gate reads the current entry, so the client must have it."""
        uri = "echo:/cat-3"
        client = await _session(host, uri)
        channel = await host.publish_changeset(uri, Changeset(label="c", reviewable=True), [_EDIT])
        await host.publish_changeset(
            uri, Changeset(uri=channel, label="c", reviewable=False), [_EDIT]
        )
        await client.request("subscribe", {"channel": channel})
        await client.collect(seconds=0.2)

        state = (await client.request("subscribe", {"channel": uri}))["result"]["snapshot"]["state"]
        assert "capabilities" not in state["changesets"][0]

        await client.notify(
            "dispatchAction",
            {
                "channel": channel,
                "clientSeq": 1,
                "action": {
                    "type": "changeset/filesReviewChanged",
                    "files": ["file:///work/main.py"],
                    "reviewed": True,
                },
            },
        )
        await client.collect(seconds=0.3)
        echoes = [
            e
            for e in client.actions(channel)
            if e["action"]["type"] == "changeset/filesReviewChanged"
        ]
        assert "rejectionReason" in echoes[-1]


class TestOperations:
    async def test_no_operations_are_built_in(self, host: Host) -> None:
        """`commit`, `create-pr`, `discard-changes` and `sync` are VS Code
        private string constants. One is a credentialed network call and one
        irreversibly destroys work, so this library ships none of them."""
        uri = "echo:/cs-o1"
        client = await _session(host, uri)
        channel = await host.publish_changeset(
            uri,
            Changeset(label="c", operations=[ChangesetOperation(id="commit", label="Commit")]),
            [_EDIT],
        )
        response = await client.request(
            "invokeChangesetOperation", {"channel": channel, "operationId": "commit"}
        )
        assert response["error"]["code"] == -32602

    async def test_a_registered_operation_runs_and_reports_status(self, host: Host) -> None:
        uri = "echo:/cs-o2"
        client = await _session(host, uri)
        ran: list[str] = []

        async def handler(changeset: str, operation: str, target: Mapping[str, Any] | None) -> None:
            ran.append(operation)

        host.register_operation("publish", handler)
        channel = await host.publish_changeset(
            uri,
            Changeset(label="c", operations=[ChangesetOperation(id="publish", label="Publish")]),
            [_EDIT],
        )
        await client.request("subscribe", {"channel": channel})
        result = await client.request(
            "invokeChangesetOperation", {"channel": channel, "operationId": "publish"}
        )
        assert result["result"] == {}
        assert ran == ["publish"]

        await client.collect(seconds=0.3)
        statuses = [
            e["action"]["status"]
            for e in client.actions(channel)
            if e["action"]["type"] == "changeset/operationStatusChanged"
        ]
        assert statuses == ["running", "idle"]

    async def test_a_failing_operation_reports_an_error_rather_than_raising(
        self, host: Host
    ) -> None:
        """Every subscriber observes the failure -- "an inline error after a
        failed revert" -- rather than only the caller seeing a JSON-RPC error."""
        uri = "echo:/cs-o3"
        client = await _session(host, uri)

        async def handler(changeset: str, operation: str, target: Mapping[str, Any] | None) -> None:
            raise RuntimeError("upstream said no")

        host.register_operation("risky", handler)
        channel = await host.publish_changeset(
            uri,
            Changeset(label="c", operations=[ChangesetOperation(id="risky", label="Risky")]),
            [_EDIT],
        )
        await client.request("subscribe", {"channel": channel})
        await client.request(
            "invokeChangesetOperation", {"channel": channel, "operationId": "risky"}
        )
        await client.collect(seconds=0.3)
        errors_seen = [
            e["action"]
            for e in client.actions(channel)
            if e["action"]["type"] == "changeset/operationStatusChanged"
            and e["action"]["status"] == "error"
        ]
        assert errors_seen
        assert "upstream said no" in errors_seen[-1]["error"]["message"]

    async def test_the_policy_can_refuse(self, host: Host) -> None:
        class NoPublishing(LoopbackSingleUserPolicy):
            def may_invoke_operation(self, info: Any, changeset: str, operation: str) -> bool:
                return False

        blocked = Host(EchoProvider(), NoPublishing())
        try:
            uri = "echo:/cs-o4"
            client = await _session(blocked, uri)

            async def handler(
                changeset: str, operation: str, target: Mapping[str, Any] | None
            ) -> None:
                raise AssertionError("the policy should have stopped this")

            blocked.register_operation("publish", handler)
            channel = await blocked.publish_changeset(uri, Changeset(label="c"), [_EDIT])
            response = await client.request(
                "invokeChangesetOperation", {"channel": channel, "operationId": "publish"}
            )
            assert response["error"]["code"] == -32009
        finally:
            await blocked.aclose()


class TestContentIsStattable:
    """A client STATS BEFORE IT READS.

    Host-owned changeset content answered `resourceRead` and refused
    everything else, so VS Code's filesystem provider called `stat()`, got
    InvalidParams, and gave up before `resourceRead` was ever tried. The user
    saw "Unable to resolve nonexistent file" about content this host was
    holding and would happily have served -- and none of the seventeen
    changeset tests noticed, because every one of them called `resourceRead`
    directly, the way no client does.
    """

    async def test_resolve_answers_for_content(self, host: Host) -> None:
        uri = "echo:/stat-1"
        client = await _session(host, uri)
        channel = await host.publish_changeset(
            uri,
            Changeset(label="c"),
            [FileChange(uri="file:///work/a.txt", before=b"one\n", after=b"two\n")],
        )
        state = (await client.request("subscribe", {"channel": channel}))["result"]["snapshot"][
            "state"
        ]
        content = state["files"][0]["edit"]["after"]["content"]["uri"]

        resolved = await client.request("resourceResolve", {"channel": ROOT_URI, "uri": content})
        assert "error" not in resolved, resolved.get("error")
        assert resolved["result"]["type"] == "file"
        assert resolved["result"]["size"] == len(b"two\n")
        assert resolved["result"]["uri"] == content

    async def test_the_stat_then_read_sequence_a_client_uses(self, host: Host) -> None:
        """Both halves, in the order a filesystem provider does them."""
        uri = "echo:/stat-2"
        client = await _session(host, uri)
        channel = await host.publish_changeset(
            uri,
            Changeset(label="c"),
            [FileChange(uri="file:///work/b.txt", after=b"created\n")],
        )
        state = (await client.request("subscribe", {"channel": channel}))["result"]["snapshot"][
            "state"
        ]
        content = state["files"][0]["edit"]["after"]["content"]["uri"]

        assert "error" not in await client.request(
            "resourceResolve", {"channel": ROOT_URI, "uri": content}
        )
        read = await client.request("resourceRead", {"channel": ROOT_URI, "uri": content})
        assert read["result"]["data"] == "created\n"

    async def test_listing_content_is_still_refused(self, host: Host) -> None:
        """It is a file. Same answer a filesystem provider gives."""
        uri = "echo:/stat-3"
        client = await _session(host, uri)
        channel = await host.publish_changeset(
            uri, Changeset(label="c"), [FileChange(uri="file:///work/c.txt", after=b"x")]
        )
        state = (await client.request("subscribe", {"channel": channel}))["result"]["snapshot"][
            "state"
        ]
        content = state["files"][0]["edit"]["after"]["content"]["uri"]

        listed = await client.request("resourceList", {"channel": ROOT_URI, "uri": content})
        assert listed["error"]["code"] == -32602


class TestOperationTargets:
    """A per-file operation must be told WHICH file.

    `invokeChangesetOperation.target` is "required iff the chosen scope is
    `resource` or `range`", and the host dropped it: the handler was told a
    button had been pressed but not where. A per-file operation could only
    guess, and guessing wrong is indistinguishable from a button that does
    nothing -- the row you clicked does not change.
    """

    async def test_the_target_reaches_the_handler(self, host: Host) -> None:
        uri = "echo:/cs-t1"
        client = await _session(host, uri)
        seen: list[Any] = []

        async def handler(changeset: str, operation: str, target: Mapping[str, Any] | None) -> None:
            seen.append(target)

        host.register_operation("annotate", handler)
        channel = await host.publish_changeset(
            uri,
            Changeset(
                label="c",
                operations=[
                    ChangesetOperation(id="annotate", label="Annotate", scopes=("resource",))
                ],
            ),
            [_EDIT],
        )
        await client.request("subscribe", {"channel": channel})
        await client.request(
            "invokeChangesetOperation",
            {
                "channel": channel,
                "operationId": "annotate",
                "target": {"kind": "resource", "resource": "file:///work/a.txt"},
            },
        )
        await client.collect(seconds=0.3)

        assert seen == [{"kind": "resource", "resource": "file:///work/a.txt"}]

    async def test_a_resource_scoped_operation_requires_a_target(self, host: Host) -> None:
        """An embedder writing a per-file operation should be able to trust
        that a target is there when the scope says it will be."""
        uri = "echo:/cs-t2"
        client = await _session(host, uri)

        async def handler(changeset: str, operation: str, target: Mapping[str, Any] | None) -> None:
            raise AssertionError("should not run without a target")

        host.register_operation("annotate", handler)
        channel = await host.publish_changeset(
            uri,
            Changeset(
                label="c",
                operations=[
                    ChangesetOperation(id="annotate", label="Annotate", scopes=("resource",))
                ],
            ),
            [_EDIT],
        )
        response = await client.request(
            "invokeChangesetOperation", {"channel": channel, "operationId": "annotate"}
        )
        assert response["error"]["code"] == -32602

    async def test_a_target_kind_outside_the_declared_scopes_is_refused(self, host: Host) -> None:
        """"The `kind` MUST match one of the operation's declared `scopes`.""" ""
        uri = "echo:/cs-t3"
        client = await _session(host, uri)

        async def handler(changeset: str, operation: str, target: Mapping[str, Any] | None) -> None:
            raise AssertionError("should not run")

        host.register_operation("wide", handler)
        channel = await host.publish_changeset(
            uri,
            Changeset(
                label="c",
                operations=[ChangesetOperation(id="wide", label="Wide", scopes=("changeset",))],
            ),
            [_EDIT],
        )
        response = await client.request(
            "invokeChangesetOperation",
            {
                "channel": channel,
                "operationId": "wide",
                "target": {"kind": "resource", "resource": "file:///work/a.txt"},
            },
        )
        assert response["error"]["code"] == -32602

    async def test_a_changeset_scoped_operation_still_needs_no_target(self, host: Host) -> None:
        uri = "echo:/cs-t4"
        client = await _session(host, uri)
        seen: list[Any] = []

        async def handler(changeset: str, operation: str, target: Mapping[str, Any] | None) -> None:
            seen.append(target)

        host.register_operation("approve", handler)
        channel = await host.publish_changeset(
            uri,
            Changeset(
                label="c",
                operations=[ChangesetOperation(id="approve", label="Approve")],
            ),
            [_EDIT],
        )
        await client.request(
            "invokeChangesetOperation", {"channel": channel, "operationId": "approve"}
        )
        await client.collect(seconds=0.3)
        assert seen == [None]

    async def test_a_range_target_must_carry_its_range(self, host: Host) -> None:
        """`ChangesetOperationTarget`'s range variant requires `range`, and
        `TextRange` requires both ends.

        Left unchecked the handler is the first thing to notice, and what it
        raises comes back as -32603 -- "the host has a bug" for an unambiguous
        caller mistake."""
        uri = "echo:/cs-t5"
        client = await _session(host, uri)

        async def handler(changeset: str, operation: str, target: Mapping[str, Any] | None) -> None:
            assert target is not None
            target["range"]["start"]  # what an embedder writes, and may trust

        host.register_operation("annotate", handler)
        channel = await host.publish_changeset(
            uri,
            Changeset(
                label="c",
                operations=[ChangesetOperation(id="annotate", label="A", scopes=("range",))],
            ),
            [_EDIT],
        )
        for target in (
            {"kind": "range", "resource": "file:///work/main.py"},
            {"kind": "range", "resource": "file:///work/main.py", "range": {}},
            {"kind": "range", "resource": "file:///work/main.py", "range": {"start": {"line": 1}}},
        ):
            response = await client.request(
                "invokeChangesetOperation",
                {"channel": channel, "operationId": "annotate", "target": target},
            )
            assert response["error"]["code"] == -32602, target

        accepted = await client.request(
            "invokeChangesetOperation",
            {
                "channel": channel,
                "operationId": "annotate",
                "target": {
                    "kind": "range",
                    "resource": "file:///work/main.py",
                    "range": {"start": {"line": 1, "character": 0}, "end": {"line": 2}},
                },
            },
        )
        assert "error" not in accepted


class TestOperationMembership:
    """ "The server validates that `operationId` exists in the changeset's
    current `operations` list."

    It did not, and the miss compounded: the scope lookup answered "no declared
    scopes" for an id the changeset never published, and every target guard was
    written `if declared` -- so an undeclared id skipped scope and target
    validation too. Reachable on the shipped demo, which gates `commit` out of
    the published list while leaving its handler registered.
    """

    async def test_an_undeclared_operation_is_refused(self, host: Host) -> None:
        uri = "echo:/mem-1"
        client = await _session(host, uri)
        ran: list[str] = []

        async def handler(changeset: str, operation: str, target: Mapping[str, Any] | None) -> None:
            ran.append(operation)

        host.register_operation("gated-out", handler)
        channel = await host.publish_changeset(
            uri,
            Changeset(label="c", operations=[ChangesetOperation(id="published", label="P")]),
            [_EDIT],
        )
        response = await client.request(
            "invokeChangesetOperation", {"channel": channel, "operationId": "gated-out"}
        )
        await client.collect(seconds=0.3)
        assert response["error"]["code"] == -32602
        assert not ran, "an operation the changeset does not offer ran anyway"

    async def test_an_undeclared_id_does_not_disarm_target_validation(self, host: Host) -> None:
        uri = "echo:/mem-2"
        client = await _session(host, uri)
        seen: list[Any] = []

        async def handler(changeset: str, operation: str, target: Mapping[str, Any] | None) -> None:
            seen.append(target)

        host.register_operation("gated-out", handler)
        channel = await host.publish_changeset(
            uri,
            Changeset(label="c", operations=[ChangesetOperation(id="published", label="P")]),
            [_EDIT],
        )
        # A range target with no range, on an id nothing declares: refused for
        # the membership alone, before the shape is ever in question.
        response = await client.request(
            "invokeChangesetOperation",
            {
                "channel": channel,
                "operationId": "gated-out",
                "target": {"kind": "range", "resource": "file:///nope"},
            },
        )
        await client.collect(seconds=0.3)
        assert response["error"]["code"] == -32602
        assert not seen

    async def test_a_changeset_with_no_operations_offers_none(self, host: Host) -> None:
        """A registered handler is not an invitation. The catalogue is."""
        uri = "echo:/mem-3"
        client = await _session(host, uri)
        ran: list[str] = []

        async def handler(changeset: str, operation: str, target: Mapping[str, Any] | None) -> None:
            ran.append(operation)

        host.register_operation("revert", handler)
        channel = await host.publish_changeset(uri, Changeset(label="c"), [_EDIT])
        response = await client.request(
            "invokeChangesetOperation", {"channel": channel, "operationId": "revert"}
        )
        await client.collect(seconds=0.3)
        assert response["error"]["code"] == -32602
        assert not ran

    async def test_an_operation_dropped_by_a_republish_stops_working(self, host: Host) -> None:
        """ "Current" is the word that matters -- this is exactly the demo's
        shape, where the available operations are recomputed from git on every
        publish."""
        uri = "echo:/mem-4"
        client = await _session(host, uri)
        ran: list[str] = []

        async def handler(changeset: str, operation: str, target: Mapping[str, Any] | None) -> None:
            ran.append(operation)

        host.register_operation("commit", handler)
        changeset = Changeset(
            label="c", operations=[ChangesetOperation(id="commit", label="Commit")]
        )
        channel = await host.publish_changeset(uri, changeset, [_EDIT])
        first = await client.request(
            "invokeChangesetOperation", {"channel": channel, "operationId": "commit"}
        )
        await client.collect(seconds=0.3)
        assert "error" not in first
        assert ran == ["commit"]

        await host.publish_changeset(uri, Changeset(uri=channel, label="c", operations=[]), [_EDIT])
        second = await client.request(
            "invokeChangesetOperation", {"channel": channel, "operationId": "commit"}
        )
        await client.collect(seconds=0.3)
        assert second["error"]["code"] == -32602
        assert ran == ["commit"]


class TestReviewSurvivesARepublish:
    """Ticking Viewed then pressing any button cleared every tick.

    `changeset/contentChanged` replaces the file list wholesale, so the
    republish added so the buttons would visibly do something silently wiped
    the flags with it. From the outside that is the checkbox being broken --
    which is how it was reported.
    """

    async def test_a_reviewed_file_stays_reviewed(self, host: Host) -> None:
        uri = "echo:/rev-1"
        client = await _session(host, uri)
        channel = await host.publish_changeset(
            uri,
            Changeset(label="c", reviewable=True),
            [FileChange(uri="file:///work/a.txt", before=b"one\n", after=b"two\n")],
        )
        state = (await client.request("subscribe", {"channel": channel}))["result"]["snapshot"][
            "state"
        ]
        file_id = state["files"][0]["id"]
        assert not state["files"][0].get("reviewed")

        await client.notify(
            "dispatchAction",
            {
                "channel": channel,
                "clientSeq": 1,
                "action": {
                    "type": "changeset/filesReviewChanged",
                    "files": [file_id],
                    "reviewed": True,
                },
            },
        )
        await client.collect(seconds=0.3)

        # The republish an operation triggers.
        await host.publish_changeset(
            uri,
            Changeset(label="c", reviewable=True),
            [FileChange(uri="file:///work/a.txt", before=b"one\n", after=b"three\n")],
        )
        after = (await client.request("subscribe", {"channel": channel}))["result"]["snapshot"][
            "state"
        ]
        assert after["files"][0]["reviewed"] is True, "the tick was wiped by the republish"

    async def test_unreviewing_is_remembered_too(self, host: Host) -> None:
        uri = "echo:/rev-2"
        client = await _session(host, uri)
        changeset = Changeset(label="c", reviewable=True)
        change = FileChange(uri="file:///work/b.txt", before=b"x\n", after=b"y\n")
        channel = await host.publish_changeset(uri, changeset, [change])
        state = (await client.request("subscribe", {"channel": channel}))["result"]["snapshot"][
            "state"
        ]
        file_id = state["files"][0]["id"]

        for flag in (True, False):
            await client.notify(
                "dispatchAction",
                {
                    "channel": channel,
                    "clientSeq": 1,
                    "action": {
                        "type": "changeset/filesReviewChanged",
                        "files": [file_id],
                        "reviewed": flag,
                    },
                },
            )
            await client.collect(seconds=0.2)

        await host.publish_changeset(uri, changeset, [change])
        after = (await client.request("subscribe", {"channel": channel}))["result"]["snapshot"][
            "state"
        ]
        assert not after["files"][0].get("reviewed")

    async def test_a_host_originated_tick_is_remembered_too(self, host: Host) -> None:
        """ "The server MAY also originate it (e.g. an agent marking its own
        output reviewed)."

        Only the client-dispatch path was recorded, so the host's own tick was
        wiped by the next republish -- and the demo's `Mark reviewed` button
        republishes, so it erased itself on the way out.
        """
        uri = "echo:/rev-3"
        client = await _session(host, uri)
        changeset = Changeset(label="c", reviewable=True)
        change = FileChange(uri="file:///work/c.txt", before=b"x\n", after=b"y\n")
        channel = await host.publish_changeset(uri, changeset, [change])

        await host.sequencer.publish(
            channel,
            {
                "type": "changeset/filesReviewChanged",
                "files": ["file:///work/c.txt"],
                "reviewed": True,
            },
        )
        await host.publish_changeset(uri, changeset, [change])
        after = (await client.request("subscribe", {"channel": channel}))["result"]["snapshot"][
            "state"
        ]
        assert after["files"][0]["reviewed"] is True, "the host's own tick was wiped"

    async def test_a_host_originated_untick_is_remembered_too(self, host: Host) -> None:
        """The server "resets review explicitly ... by dispatching this action
        with `reviewed: false`". Memory that only ever grows is not memory."""
        uri = "echo:/rev-4"
        client = await _session(host, uri)
        changeset = Changeset(label="c", reviewable=True)
        change = FileChange(uri="file:///work/d.txt", before=b"x\n", after=b"y\n")
        channel = await host.publish_changeset(uri, changeset, [change])

        for flag in (True, False):
            await host.sequencer.publish(
                channel,
                {
                    "type": "changeset/filesReviewChanged",
                    "files": ["file:///work/d.txt"],
                    "reviewed": flag,
                },
            )
        await host.publish_changeset(uri, changeset, [change])
        after = (await client.request("subscribe", {"channel": channel}))["result"]["snapshot"][
            "state"
        ]
        assert not after["files"][0].get("reviewed")


class TestOperationsAreHonestAboutFailureAndTiming:
    """Two complaints with one root: the client shows nothing on its own.

    Its operation mapper drops `error` entirely and computes enablement from
    `status !== "disabled" && status !== "running"`, so an `error` status
    renders exactly like `idle`. A failed operation was indistinguishable from
    one that did nothing.
    """

    async def test_a_failing_operation_fails_the_request(self, host: Host) -> None:
        uri = "echo:/op-fail"
        client = await _session(host, uri)

        async def handler(changeset: str, operation: str, target: Mapping[str, Any] | None) -> None:
            raise RuntimeError("upstream said no")

        host.register_operation("risky", handler)
        channel = await host.publish_changeset(
            uri,
            Changeset(label="c", operations=[ChangesetOperation(id="risky", label="Risky")]),
            [_EDIT],
        )
        await client.request("subscribe", {"channel": channel})

        response = await client.request(
            "invokeChangesetOperation", {"channel": channel, "operationId": "risky"}
        )
        assert "error" in response, "a failed operation returned success"
        assert "upstream said no" in response["error"]["message"]

        # And the status still goes out, for every other subscriber.
        await client.collect(seconds=0.3)
        statuses = [
            a["action"]["status"]
            for a in client.actions(channel)
            if a["action"]["type"] == "changeset/operationStatusChanged"
        ]
        assert "error" in statuses

    async def test_operations_are_disabled_during_a_turn(self) -> None:
        # A provider slow enough that the turn is genuinely in flight when the
        # invoke arrives. With the default echo the turn finishes first and the
        # invoke is legitimately allowed, which is not what this is testing.
        host = Host(EchoProvider(delay=0.4), LoopbackSingleUserPolicy())
        uri = "echo:/op-busy"
        client = await _session(host, uri)
        ran: list[str] = []

        async def handler(changeset: str, operation: str, target: Mapping[str, Any] | None) -> None:
            ran.append(operation)

        host.register_operation("commit", handler)
        changeset = Changeset(
            label="c", operations=[ChangesetOperation(id="commit", label="Commit")]
        )
        channel = await host.publish_changeset(uri, changeset, [_EDIT])
        state = (await client.request("subscribe", {"channel": uri}))["result"]["snapshot"]["state"]
        chat = state["chats"][0]["resource"]
        await client.request("subscribe", {"channel": chat})

        # Start a turn and invoke while it runs.
        await client.notify(
            "dispatchAction",
            {
                "channel": chat,
                "clientSeq": 1,
                "action": {
                    "type": "chat/turnStarted",
                    "turnId": "t1",
                    "startedAt": "1970-01-01T00:00:01.000Z",
                    "message": {"text": "work", "origin": {"kind": "user"}},
                },
            },
        )
        # The turn starts on a NOTIFICATION, so let it actually begin --
        # otherwise the invoke races ahead of it and is legitimately allowed.
        await asyncio.sleep(0.15)
        refused = await client.request(
            "invokeChangesetOperation", {"channel": channel, "operationId": "commit"}
        )
        assert refused["error"]["code"] == -32602
        assert not ran, "an operation raced the agent's own writes"
        await host.aclose()

    async def test_the_buttons_grey_out_while_a_turn_runs(self, host: Host) -> None:
        """Refusing is the guarantee; greying is what tells the user."""
        uri = "echo:/op-grey"
        client = await _session(host, uri)
        changeset = Changeset(
            label="c", operations=[ChangesetOperation(id="commit", label="Commit")]
        )
        channel = await host.publish_changeset(uri, changeset, [_EDIT])
        idle = (await client.request("subscribe", {"channel": channel}))["result"]["snapshot"][
            "state"
        ]
        assert idle["operations"][0]["status"] == "idle"

        session = host._sessions[uri]
        # Any chat: the mid-turn gate on a changeset operation is
        # session-scoped, because a commit races the agent's writes wherever
        # they come from.
        session.turns["ahp-chat:/busy"] = asyncio.create_task(asyncio.sleep(5))
        try:
            await host.publish_changeset(uri, changeset, [_EDIT])
            busy = (await client.request("subscribe", {"channel": channel}))["result"]["snapshot"][
                "state"
            ]
            assert busy["operations"][0]["status"] == "disabled"
        finally:
            session.turns["ahp-chat:/busy"].cancel()


class TestTheDisabledGateIsReEvaluated:
    """`disabled` was sampled once, at publish time, and never revisited.

    A provider can only publish a changeset from inside the turn that produced
    the changes, so the sample was always "busy" -- and with nothing to
    re-evaluate it, every control on every changeset stayed greyed for the life
    of the session. Refusing the invoke was still correct; the user simply had
    no way to make it happen.
    """

    async def _busy_host(self) -> Host:
        # Slow enough that the turn is genuinely in flight when the changeset
        # is published, which is the shape being reproduced.
        return Host(EchoProvider(delay=0.3), LoopbackSingleUserPolicy())

    async def _start_turn(self, client: FakeClient, chat: str) -> None:
        await client.notify(
            "dispatchAction",
            {
                "channel": chat,
                "clientSeq": 1,
                "action": {
                    "type": "chat/turnStarted",
                    "turnId": "t1",
                    "startedAt": "1970-01-01T00:00:01.000Z",
                    "message": {"text": "work", "origin": {"kind": "user"}},
                },
            },
        )

    async def _settle(self, host: Host, uri: str) -> None:
        for _ in range(60):
            await asyncio.sleep(0.05)
            if not host._sessions[uri].running():
                # One more turn of the loop: the un-greying runs from the turn
                # task's done callback, which fires after the task finishes.
                await asyncio.sleep(0.1)
                return
        raise AssertionError("the turn never ended")

    async def test_a_changeset_published_mid_turn_comes_back_when_it_ends(self) -> None:
        host = await self._busy_host()
        uri = "echo:/gate-1"
        try:
            client = await _session(host, uri)
            state = (await client.request("subscribe", {"channel": uri}))["result"]["snapshot"][
                "state"
            ]
            chat = state["chats"][0]["resource"]
            await client.request("subscribe", {"channel": chat})

            await self._start_turn(client, chat)
            await asyncio.sleep(0.1)
            changeset = Changeset(
                label="c", operations=[ChangesetOperation(id="commit", label="Commit")]
            )
            channel = await host.publish_changeset(uri, changeset, [_EDIT])
            assert host.sequencer.state_of(channel)["operations"][0]["status"] == "disabled"

            await self._settle(host, uri)
            assert host.sequencer.state_of(channel)["operations"][0]["status"] == "idle"
        finally:
            await host.aclose()

    async def test_the_un_greying_is_an_action_not_just_a_snapshot(self) -> None:
        """A client subscribed the whole time has no snapshot to heal from."""
        host = await self._busy_host()
        uri = "echo:/gate-2"
        try:
            client = await _session(host, uri)
            state = (await client.request("subscribe", {"channel": uri}))["result"]["snapshot"][
                "state"
            ]
            chat = state["chats"][0]["resource"]
            await client.request("subscribe", {"channel": chat})
            await self._start_turn(client, chat)
            await asyncio.sleep(0.1)
            changeset = Changeset(
                label="c", operations=[ChangesetOperation(id="commit", label="Commit")]
            )
            channel = await host.publish_changeset(uri, changeset, [_EDIT])
            await client.request("subscribe", {"channel": channel})
            await self._settle(host, uri)
            await client.collect(seconds=0.3)

            statuses = [
                a["action"]["status"]
                for a in client.actions(channel)
                if a["action"]["type"] == "changeset/operationStatusChanged"
            ]
            assert statuses[-1] == "idle", statuses
        finally:
            await host.aclose()

    async def test_a_turn_greys_a_changeset_published_while_idle(self) -> None:
        """The other end of the same gate: a changeset published between turns
        must grey when the next one starts, not stay clickable."""
        host = await self._busy_host()
        uri = "echo:/gate-3"
        try:
            client = await _session(host, uri)
            state = (await client.request("subscribe", {"channel": uri}))["result"]["snapshot"][
                "state"
            ]
            chat = state["chats"][0]["resource"]
            await client.request("subscribe", {"channel": chat})
            changeset = Changeset(
                label="c", operations=[ChangesetOperation(id="commit", label="Commit")]
            )
            channel = await host.publish_changeset(uri, changeset, [_EDIT])
            assert host.sequencer.state_of(channel)["operations"][0]["status"] == "idle"

            await self._start_turn(client, chat)
            await asyncio.sleep(0.15)
            assert host.sequencer.state_of(channel)["operations"][0]["status"] == "disabled"

            await self._settle(host, uri)
            assert host.sequencer.state_of(channel)["operations"][0]["status"] == "idle"
        finally:
            await host.aclose()

    async def test_a_failed_operation_keeps_its_error_across_the_gate(self) -> None:
        """`error` is the only trace a failed operation leaves on screen, and
        the gate knows nothing about it. Only `idle` and `disabled` move."""
        host = await self._busy_host()
        uri = "echo:/gate-4"
        try:
            client = await _session(host, uri)
            state = (await client.request("subscribe", {"channel": uri}))["result"]["snapshot"][
                "state"
            ]
            chat = state["chats"][0]["resource"]
            await client.request("subscribe", {"channel": chat})

            async def handler(
                changeset: str, operation: str, target: Mapping[str, Any] | None
            ) -> None:
                raise RuntimeError("upstream said no")

            host.register_operation("risky", handler)
            channel = await host.publish_changeset(
                uri,
                Changeset(label="c", operations=[ChangesetOperation(id="risky", label="Risky")]),
                [_EDIT],
            )
            failed = await client.request(
                "invokeChangesetOperation", {"channel": channel, "operationId": "risky"}
            )
            assert "error" in failed
            assert host.sequencer.state_of(channel)["operations"][0]["status"] == "error"

            await self._start_turn(client, chat)
            await asyncio.sleep(0.15)
            assert host.sequencer.state_of(channel)["operations"][0]["status"] == "error"
            await self._settle(host, uri)
            assert host.sequencer.state_of(channel)["operations"][0]["status"] == "error"
        finally:
            await host.aclose()
