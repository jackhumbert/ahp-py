"""The `resource*` commands over the wire, at their edges.

`tests/unit/test_resource_jail.py` attacks the provider directly; this drives
the same code through the dispatcher, because most of what was wrong here lived
between the two: a parameter the host coerced before the provider ever saw it, a
grant the host issued that the provider then refused, a size nobody bounded.

Every case is a defect found by driving an independently-built client against
this host, and each one is stated as the rule it broke.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from ahp_protocol.channels import ROOT_URI
from ahp_protocol.transport import memory_pair

from ahp_host.core import Host, LoopbackSingleUserPolicy
from ahp_host.core.policy import ConnectionInfo
from ahp_host.core.resources import RootedFilesystemResourceProvider
from ahp_host.provider import EchoProvider

from .test_host_end_to_end import FakeClient

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def root(tmp_path: Path) -> Path:
    # `.resolve()` because macOS puts `tmp_path` under `/var`, which is itself a
    # symlink to `/private/var`: the provider canonicalises its root, so an
    # unresolved path is not inside its own jail.
    served = tmp_path.resolve() / "ws"
    served.mkdir()
    (served / "a.txt").write_text("hello")
    return served


async def _serve(host: Host) -> tuple[FakeClient, asyncio.Task[None]]:
    client_transport, server_transport = memory_pair()
    task = asyncio.create_task(host.serve(server_transport))
    client = FakeClient(client_transport)
    await client.request(
        "initialize",
        {
            "channel": ROOT_URI,
            "clientId": "resources",
            "protocolVersions": ["0.7.0"],
            "initialSubscriptions": [ROOT_URI],
        },
    )
    return client, task


@pytest.fixture
async def writable(root: Path) -> AsyncIterator[tuple[Path, FakeClient]]:
    host = Host(
        EchoProvider(),
        LoopbackSingleUserPolicy(),
        resources=RootedFilesystemResourceProvider(root, writable=True),
    )
    client, task = await _serve(host)
    try:
        yield root, client
    finally:
        task.cancel()
        await host.aclose()


async def _write(client: FakeClient, uri: str, **extra: Any) -> dict[str, Any]:
    params: dict[str, Any] = {
        "channel": ROOT_URI,
        "uri": uri,
        "data": "Z",
        "encoding": "utf-8",
    }
    params.update(extra)
    return await client.request("resourceWrite", params)


class TestConditionalWrites:
    async def test_a_failed_if_match_does_not_create_the_file(
        self, writable: tuple[Path, FakeClient]
    ) -> None:
        """`O_CREAT` ran before the etag comparison, so a conditional write to
        an absent path left a 0-byte file, answered Conflict, and permanently
        broke the `createOnly` retry that a Conflict invites."""
        root, client = writable
        absent = root / "nope.txt"

        failed = await _write(client, absent.as_uri(), ifMatch='W/"1-2"')

        assert "error" in failed
        assert not absent.exists(), "the write it refused to make created the file anyway"
        retry = await _write(client, absent.as_uri(), createOnly=True)
        assert "error" not in retry, retry.get("error")
        assert absent.read_text() == "Z"

    async def test_if_match_still_detects_a_change(self, writable: tuple[Path, FakeClient]) -> None:
        """Conflict for a file that IS there and does not match: dropping
        `O_CREAT` must not cost the check its whole purpose."""
        root, client = writable
        response = await _write(client, (root / "a.txt").as_uri(), ifMatch='W/"0-0"')
        assert response["error"]["code"] == -32011
        assert (root / "a.txt").read_text() == "hello"

    async def test_if_match_with_create_only_cannot_be_satisfied(
        self, writable: tuple[Path, FakeClient]
    ) -> None:
        """The two flags contradict: an absent file has no etag to match, and a
        present one is what `createOnly` MUST refuse. Answered from the file's
        actual state rather than by inventing a third code."""
        root, client = writable
        present = await _write(
            client, (root / "a.txt").as_uri(), ifMatch='W/"0-0"', createOnly=True
        )
        assert present["error"]["code"] == -32010
        absent = await _write(client, (root / "b.txt").as_uri(), ifMatch='W/"0-0"', createOnly=True)
        assert absent["error"]["code"] == -32008
        assert not (root / "b.txt").exists()


class TestWriteParameters:
    @pytest.mark.parametrize("mode", ["prepend", 7, None, {}, True])
    async def test_a_mode_outside_the_enum_is_refused(
        self, writable: tuple[Path, FakeClient], mode: Any
    ) -> None:
        """`ResourceWriteMode` is closed -- `truncate | append | insert`. Every
        other value fell through to the DEFAULT, which is the full overwrite:
        the most destructive of the three, chosen as the fallback for a value
        the caller demonstrably got wrong, and reported as success.

        `{}` is in the list because membership was tested against a frozenset,
        where an unhashable value raises `TypeError` -- InternalError to a peer.
        """
        root, client = writable
        response = await _write(client, (root / "a.txt").as_uri(), mode=mode)
        assert response["error"]["code"] == -32602, response
        assert (root / "a.txt").read_text() == "hello", "the file was overwritten anyway"

    async def test_an_absent_mode_still_means_truncate(
        self, writable: tuple[Path, FakeClient]
    ) -> None:
        """Only *absent* is the default. An explicit `null` is a value the enum
        does not contain (invariant 18)."""
        root, client = writable
        response = await _write(client, (root / "a.txt").as_uri())
        assert "error" not in response, response.get("error")
        assert (root / "a.txt").read_text() == "Z"

    @pytest.mark.parametrize("mode", ["append", "insert", "truncate"])
    async def test_a_negative_position_is_refused(
        self, writable: tuple[Path, FakeClient], mode: str
    ) -> None:
        """`append` reported success after NUL-padding the file past EOF;
        `insert` and `truncate` leaked `[Errno 22] Invalid argument` as
        InternalError -- "the host has a bug" for a caller's mistake."""
        root, client = writable
        response = await _write(client, (root / "a.txt").as_uri(), mode=mode, position=-5)
        assert response["error"]["code"] == -32602, response
        assert (root / "a.txt").read_bytes() == b"hello"

    async def test_a_whole_float_position_is_honoured(
        self, writable: tuple[Path, FakeClient]
    ) -> None:
        """The schema types `position` as `number`, so `2.0` is a legitimate way
        to say 2 -- and `isinstance(x, int)` silently turned it into 0, which
        for `truncate` is a full overwrite."""
        root, client = writable
        response = await _write(client, (root / "a.txt").as_uri(), mode="insert", position=2.0)
        assert "error" not in response, response.get("error")
        assert (root / "a.txt").read_text() == "heZllo"

    async def test_a_non_finite_position_is_refused(
        self, writable: tuple[Path, FakeClient]
    ) -> None:
        """`json.loads` accepts `Infinity` and `NaN` by default and `int()`
        raises on both, so a peer could pick which frames came back as
        InternalError with a Python exception name attached."""
        root, client = writable
        response = await _write(client, (root / "a.txt").as_uri(), position=float("inf"))
        assert response["error"]["code"] == -32602, response

    async def test_a_fractional_position_is_refused(
        self, writable: tuple[Path, FakeClient]
    ) -> None:
        root, client = writable
        response = await _write(client, (root / "a.txt").as_uri(), position=2.5)
        assert response["error"]["code"] == -32602


class TestSymlinkedWrites:
    async def test_a_write_through_a_symlink_is_denied_not_missing(
        self, writable: tuple[Path, FakeClient]
    ) -> None:
        """`O_NOFOLLOW` reports a symlink as ELOOP and the mapping turned that
        into NotFound, so a file `resourceRead` and `resourceResolve` both serve
        reported as absent.

        The link is still not followed: `_resource_write` asks the policy about
        the name the peer sent -- a file that does not exist yet has nothing to
        canonicalise -- so following would land the write on a path the policy
        never saw. The fix is the honest code, not the traversal.
        """
        root, client = writable
        os.symlink("a.txt", root / "link.txt")
        link = (root / "link.txt").as_uri()

        assert "error" not in await client.request(
            "resourceResolve", {"channel": ROOT_URI, "uri": link}
        )
        assert "error" not in await client.request(
            "resourceRead", {"channel": ROOT_URI, "uri": link}
        )

        response = await _write(client, link)
        assert response["error"]["code"] == -32009, response
        assert (root / "a.txt").read_text() == "hello"


class TestReadSize:
    async def test_a_file_above_the_cap_is_refused(self, root: Path) -> None:
        """One unprivileged read of a 64 MiB file drove host RSS from 29 MB to
        970 MB. `ResourceReadParams` has no offset or length, so there is no
        partial read to degrade to -- the only conformant answer is a refusal.
        """
        big = root / "big.bin"
        with big.open("wb") as handle:
            handle.truncate(2 * 1024 * 1024)
        host = Host(
            EchoProvider(),
            LoopbackSingleUserPolicy(),
            resources=RootedFilesystemResourceProvider(root),
            max_read_bytes=1024 * 1024,
        )
        client, task = await _serve(host)
        try:
            response = await client.request(
                "resourceRead", {"channel": ROOT_URI, "uri": big.as_uri()}
            )
            assert response["error"]["code"] == -32009, response
            # And the file is still resolvable and listable: the bound is on
            # the bytes, not on the existence of the thing.
            assert "error" not in await client.request(
                "resourceResolve", {"channel": ROOT_URI, "uri": big.as_uri()}
            )
        finally:
            task.cancel()
            await host.aclose()

    async def test_the_cap_is_raisable_by_the_embedder(self, root: Path) -> None:
        """An embedder serving large assets has to be able to lift it, or the
        bound is a feature removal rather than a safety default."""
        big = root / "big.bin"
        big.write_bytes(b"x" * (1024 * 1024))
        host = Host(
            EchoProvider(),
            LoopbackSingleUserPolicy(),
            resources=RootedFilesystemResourceProvider(root),
            max_read_bytes=None,
        )
        client, task = await _serve(host)
        try:
            response = await client.request(
                "resourceRead", {"channel": ROOT_URI, "uri": big.as_uri()}
            )
            assert "error" not in response, response.get("error")
            assert len(response["result"]["data"]) == 1024 * 1024
        finally:
            task.cancel()
            await host.aclose()


class TestGrantsMatchWhatFollows:
    async def test_a_read_only_host_does_not_grant_write(self, root: Path) -> None:
        """`isinstance(..., WritableResourceProvider)` is STRUCTURAL, so it was
        satisfied by a provider built `writable=False` -- the shipped
        `--serve-directory` default -- which then refused every write. "After a
        successful `resourceRequest`, the caller MAY use the corresponding
        `resource*` commands"; a grant that does not survive one call is worse
        than a refusal.
        """
        host = Host(
            EchoProvider(),
            LoopbackSingleUserPolicy(),
            resources=RootedFilesystemResourceProvider(root),
        )
        client, task = await _serve(host)
        try:
            uri = (root / "a.txt").as_uri()
            granted = await client.request(
                "resourceRequest", {"channel": ROOT_URI, "uri": uri, "write": True}
            )
            assert granted["error"]["code"] == -32009, granted
            # Reading is still granted, and still works.
            read_grant = await client.request(
                "resourceRequest", {"channel": ROOT_URI, "uri": uri, "read": True}
            )
            assert "error" not in read_grant, read_grant.get("error")
        finally:
            task.cancel()
            await host.aclose()

    async def test_a_writable_host_still_grants_write(
        self, writable: tuple[Path, FakeClient]
    ) -> None:
        root, client = writable
        uri = (root / "a.txt").as_uri()
        granted = await client.request(
            "resourceRequest", {"channel": ROOT_URI, "uri": uri, "write": True}
        )
        assert "error" not in granted, granted.get("error")
        assert "error" not in await _write(client, uri)


class _RecordingPolicy(LoopbackSingleUserPolicy):
    """Permits everything, and remembers what it was asked about."""

    def __init__(self) -> None:
        self.seen: list[tuple[str, str]] = []

    def may_access_resource(self, info: ConnectionInfo, operation: str, uri: str) -> bool:
        self.seen.append((operation, uri))
        return True


class TestResolveWithoutFollowing:
    @pytest.fixture
    async def linked(self, root: Path) -> AsyncIterator[tuple[Path, FakeClient, _RecordingPolicy]]:
        (root / "real").mkdir()
        (root / "real" / "f.txt").write_text("hi")
        os.symlink("real", root / "linkdir")
        os.symlink("real/f.txt", root / "link.txt")
        policy = _RecordingPolicy()
        host = Host(
            EchoProvider(),
            policy,
            resources=RootedFilesystemResourceProvider(root),
        )
        client, task = await _serve(host)
        try:
            yield root, client, policy
        finally:
            task.cancel()
            await host.aclose()

    async def test_it_echoes_the_requested_uri_through_a_symlinked_parent(
        self, linked: tuple[Path, FakeClient, _RecordingPolicy]
    ) -> None:
        """ "Canonical URI after symlink resolution. Equal to the requested URI
        when `followSymlinks` is `false`". Only the final component was treated
        as the link, so a symlinked PARENT was canonicalised anyway."""
        root, client, _ = linked
        asked = (root / "linkdir" / "f.txt").as_uri()

        result = (
            await client.request(
                "resourceResolve",
                {"channel": ROOT_URI, "uri": asked, "followSymlinks": False},
            )
        )["result"]

        assert result["uri"] == asked
        assert result["type"] == "file"

    async def test_the_policy_still_sees_the_canonical_path(
        self, linked: tuple[Path, FakeClient, _RecordingPolicy]
    ) -> None:
        """The echo is a wire-format promise, not a relaxation: a policy that
        refuses the target must not be walked around by naming a link to it."""
        root, client, policy = linked
        asked = (root / "linkdir" / "f.txt").as_uri()

        await client.request(
            "resourceResolve", {"channel": ROOT_URI, "uri": asked, "followSymlinks": False}
        )

        assert policy.seen == [("resolve", (root / "real" / "f.txt").as_uri())]

    async def test_follow_symlinks_is_not_honoured_on_a_read(
        self, linked: tuple[Path, FakeClient, _RecordingPolicy]
    ) -> None:
        """`followSymlinks` is declared on `ResourceResolveParams` and nowhere
        else. Honouring it on a read let a peer hand the policy the LINK's URI
        -- `resolve(follow_symlinks=False)` answers with the name it was given
        -- while the read went on following the link to its target, so a policy
        that refuses the target could be walked around by naming a link to it.
        """
        root, client, policy = linked
        asked = (root / "link.txt").as_uri()

        result = await client.request(
            "resourceRead", {"channel": ROOT_URI, "uri": asked, "followSymlinks": False}
        )

        assert result["result"]["data"] == "hi"
        assert policy.seen == [("read", (root / "real" / "f.txt").as_uri())]

    async def test_following_still_canonicalises(
        self, linked: tuple[Path, FakeClient, _RecordingPolicy]
    ) -> None:
        root, client, _ = linked
        asked = (root / "linkdir" / "f.txt").as_uri()
        result = (await client.request("resourceResolve", {"channel": ROOT_URI, "uri": asked}))[
            "result"
        ]
        assert result["uri"] == (root / "real" / "f.txt").as_uri()

    async def test_a_symlink_itself_is_still_described_as_one(
        self, linked: tuple[Path, FakeClient, _RecordingPolicy]
    ) -> None:
        """The other half of the same sentence: "When `false`, stat the link
        itself (lstat semantics) and report `type: 'symlink'`"."""
        root, client, _ = linked
        asked = (root / "linkdir").as_uri()
        result = (
            await client.request(
                "resourceResolve",
                {"channel": ROOT_URI, "uri": asked, "followSymlinks": False},
            )
        )["result"]
        assert result == {**result, "uri": asked, "type": "symlink"}
