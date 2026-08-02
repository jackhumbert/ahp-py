"""Changesets: the catalogue, the channel, the diffs, review and operations.

The interesting property is that a changeset renders on a host that exposes **no
filesystem at all**. Content lives in a per-session store addressed by hash and
is served through a scoped `resourceRead`, so a diff needs the bytes as they were
*before* the edit -- which by the time anyone asks are no longer on disk.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest

from agent_host_server.core import Host, LoopbackSingleUserPolicy
from agent_host_server.core.changesets import Changeset, ChangesetOperation, FileChange
from agent_host_server.core.channels import ROOT_URI
from agent_host_server.provider import EchoProvider
from agent_host_server.transport import memory_pair

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

        async def handler(changeset: str, operation: str) -> None:
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

        async def handler(changeset: str, operation: str) -> None:
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

            async def handler(changeset: str, operation: str) -> None:
                raise AssertionError("the policy should have stopped this")

            blocked.register_operation("publish", handler)
            channel = await blocked.publish_changeset(uri, Changeset(label="c"), [_EDIT])
            response = await client.request(
                "invokeChangesetOperation", {"channel": channel, "operationId": "publish"}
            )
            assert response["error"]["code"] == -32009
        finally:
            await blocked.aclose()


class TestTheDemoActuallyPublishes:
    """`publish_changeset` shipped complete and had no caller.

    Seventeen tests exercised it, every one of them holding the Host directly.
    Nothing in a turn could reach it -- `SessionPublisher` had no method -- so
    no session ever published a changeset and the entire Changes surface was
    dead code with a green suite. That is the failure this class guards: not a
    wrong shape, an absent caller.
    """

    async def test_a_turn_publishes_a_changeset(self) -> None:
        host = Host(EchoProvider(changes=True), LoopbackSingleUserPolicy())
        try:
            client = await _session(host, "echo:/demo-changes")
            state = (await client.request("subscribe", {"channel": "echo:/demo-changes"}))[
                "result"
            ]["snapshot"]["state"]
            chat = state["chats"][0]["resource"]
            await client.request("subscribe", {"channel": chat})
            await client.notify(
                "dispatchAction",
                {
                    "channel": chat,
                    "clientSeq": 1,
                    "action": {
                        "type": "chat/turnStarted",
                        "turnId": "t1",
                        "startedAt": "1970-01-01T00:00:01.000Z",
                        "message": {"text": "goodbye", "origin": {"kind": "user"}},
                    },
                },
            )
            await client.collect(seconds=0.8)

            state = (await client.request("subscribe", {"channel": "echo:/demo-changes"}))[
                "result"
            ]["snapshot"]["state"]
            catalogue = state.get("changesets") or []
            assert catalogue, "a turn published no changeset"
            entry = catalogue[0]
            assert entry["changeKind"] == "session"

            changeset = (await client.request("subscribe", {"channel": entry["uriTemplate"]}))[
                "result"
            ]["snapshot"]["state"]
            assert changeset["status"] == "ready"

            # All three diff shapes, because a demo that only shows edits
            # teaches nothing about creations and deletions.
            shapes = set()
            for entry_ in changeset["files"]:
                edit = entry_["edit"]
                if edit.get("before") and edit.get("after"):
                    shapes.add("edit")
                elif edit.get("after"):
                    shapes.add("create")
                else:
                    shapes.add("delete")
            assert shapes == {"edit", "create", "delete"}

            # And the buttons, with the two fields that are required.
            operations = changeset["operations"]
            assert operations
            for operation in operations:
                assert operation["scopes"]
                assert operation["status"] == "idle"
        finally:
            await host.aclose()

    async def test_the_proposed_content_is_readable(self) -> None:
        """The diff editor fetches `after.content` back through `resourceRead`.
        A changeset whose content cannot be read renders as an empty diff."""
        host = Host(EchoProvider(changes=True), LoopbackSingleUserPolicy())
        try:
            client = await _session(host, "echo:/demo-read")
            state = (await client.request("subscribe", {"channel": "echo:/demo-read"}))["result"][
                "snapshot"
            ]["state"]
            chat = state["chats"][0]["resource"]
            await client.request("subscribe", {"channel": chat})
            await client.notify(
                "dispatchAction",
                {
                    "channel": chat,
                    "clientSeq": 1,
                    "action": {
                        "type": "chat/turnStarted",
                        "turnId": "t1",
                        "startedAt": "1970-01-01T00:00:01.000Z",
                        "message": {"text": "MARKER", "origin": {"kind": "user"}},
                    },
                },
            )
            await client.collect(seconds=0.8)

            state = (await client.request("subscribe", {"channel": "echo:/demo-read"}))["result"][
                "snapshot"
            ]["state"]
            uri = state["changesets"][0]["uriTemplate"]
            changeset = (await client.request("subscribe", {"channel": uri}))["result"]["snapshot"][
                "state"
            ]
            after = next(
                f["edit"]["after"]["content"]["uri"]
                for f in changeset["files"]
                if f["edit"].get("after") and f["edit"].get("before")
            )

            read = await client.request("resourceRead", {"channel": ROOT_URI, "uri": after})
            assert "error" not in read, read.get("error")
            # The turn's own message is in the proposed content, so the diff
            # belongs to the turn that produced it.
            assert "MARKER" in read["result"]["data"]
        finally:
            await host.aclose()


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
