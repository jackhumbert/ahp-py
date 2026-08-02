"""A conformance probe you can point at a live host.

The only mechanism in this library that produces **independent** evidence.
Upstream publishes no host library in any language, the sibling Python host
shares our reducers, and the one third-party host is pinned to a 0.3-era spec --
so every gate in the suite is either self-consistency or a counterparty we also
wrote. `doctor` converts adoption into conformance data: a host author points it
at their server and finds out, in twenty lines, that their root snapshot's
resource is ``ahp-root:/`` rather than ``ahp-root://``.

Each check is a MUST or a documented SHOULD, with the source named, so a failure
is a bug report rather than an opinion.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from agent_host_protocol.channels import ROOT_URI
from agent_host_protocol.transport import Transport
from agent_host_protocol.versions import DEFAULT_SUPPORTED_VERSIONS, parse_version

from agent_host_client.client.client import AhpClient, ClientConfig
from agent_host_client.client.errors import MethodNotFound, RpcError

__all__ = ["Finding", "Report", "diagnose"]


@dataclass(frozen=True, slots=True)
class Finding:
    ok: bool
    check: str
    detail: str
    #: Where the requirement comes from, so a failure can be filed.
    source: str
    #: A SHOULD that failed is worth saying and is not a conformance failure.
    severity: str = "must"

    def __str__(self) -> str:
        mark = "PASS" if self.ok else ("FAIL" if self.severity == "must" else "WARN")
        return f"[{mark}] {self.check}: {self.detail}  ({self.source})"


@dataclass(frozen=True, slots=True)
class Report:
    findings: list[Finding]

    @property
    def failures(self) -> list[Finding]:
        return [f for f in self.findings if not f.ok and f.severity == "must"]

    @property
    def ok(self) -> bool:
        return not self.failures

    def __str__(self) -> str:
        return "\n".join(str(f) for f in self.findings)


async def diagnose(transport: Transport, *, client_id: str = "ahp-doctor") -> Report:
    """Run every check against one connection and report."""
    findings: list[Finding] = []
    client = AhpClient(transport, ClientConfig(verify_negotiated_version=False))
    await client.connect()
    try:
        offered = list(DEFAULT_SUPPORTED_VERSIONS)
        try:
            result = await client.initialize(client_id=client_id, initial_subscriptions=[ROOT_URI])
        except Exception as exc:
            findings.append(
                Finding(
                    False,
                    "initialize",
                    f"the handshake failed: {exc}",
                    "specification/lifecycle.md",
                )
            )
            return Report(findings)

        findings.extend(_check_initialize(result, offered))
        findings.extend(await _check_ping(client))
        findings.extend(await _check_list_sessions(client))
        findings.extend(await _check_unknown_method(client))
        return Report(findings)
    finally:
        await client.shutdown()


def _check_initialize(result: Mapping[str, Any], offered: Sequence[str]) -> list[Finding]:
    findings: list[Finding] = []
    version = result.get("protocolVersion")
    findings.append(
        Finding(
            isinstance(version, str) and parse_version(version) is not None,
            "protocolVersion",
            f"got {version!r}",
            "specification/lifecycle.md -- MUST be a SemVer MAJOR.MINOR.PATCH string",
        )
    )
    findings.append(
        Finding(
            version in offered,
            "protocolVersion is one we offered",
            f"answered {version!r} for offered {list(offered)!r}",
            "specification/lifecycle.md -- MUST be one of the client's protocolVersions",
        )
    )
    seq = result.get("serverSeq")
    findings.append(
        Finding(
            isinstance(seq, int) and not isinstance(seq, bool),
            "serverSeq",
            f"got {seq!r}",
            "specification/lifecycle.md -- InitializeResult.serverSeq is required",
        )
    )
    snapshots = result.get("snapshots")
    findings.append(
        Finding(
            isinstance(snapshots, list),
            "snapshots is an array",
            f"got {type(snapshots).__name__}",
            "specification/lifecycle.md -- snapshots[] is required, even when empty",
        )
    )
    if isinstance(snapshots, list):
        resources = [s.get("resource") for s in snapshots if isinstance(s, Mapping)]
        root = [s for s in snapshots if isinstance(s, Mapping) and s.get("resource") == ROOT_URI]
        findings.append(
            Finding(
                bool(root),
                "root snapshot resource is byte-exactly 'ahp-root://'",
                "found it" if root else f"resources were {resources!r}",
                "specification/root-channel.md -- the root URI is compared with ===, "
                "so 'ahp-root:/' is a different channel",
            )
        )
        for snapshot in root:
            findings.append(
                Finding(
                    isinstance(snapshot.get("fromSeq"), int),
                    "snapshot.fromSeq",
                    f"got {snapshot.get('fromSeq')!r}",
                    "specification/subscriptions.md -- fromSeq carries the only formal "
                    "ordering rule and is required",
                )
            )
            state = snapshot.get("state")
            findings.append(
                Finding(
                    isinstance(state, Mapping) and isinstance(state.get("agents"), list),
                    "RootState.agents is an array",
                    f"got {type(state).__name__}",
                    "reference/root#rootstate",
                )
            )
    return findings


async def _check_ping(client: AhpClient) -> list[Finding]:
    try:
        await asyncio.wait_for(client.ping(), 10)
    except Exception as exc:
        return [
            Finding(
                False,
                "ping",
                f"failed: {exc}",
                "reference/common#ping -- the server MUST respond regardless of state",
            )
        ]
    return [Finding(True, "ping", "answered", "reference/common#ping")]


async def _check_list_sessions(client: AhpClient) -> list[Finding]:
    try:
        result = await asyncio.wait_for(client.list_sessions(), 10)
    except MethodNotFound:
        return [
            Finding(
                False,
                "listSessions",
                "answered MethodNotFound",
                "specification/root-channel.md -- listSessions has no capability gate",
                severity="should",
            )
        ]
    except Exception as exc:
        return [Finding(False, "listSessions", f"failed: {exc}", "specification/root-channel.md")]
    items = result.get("items")
    findings = [
        Finding(
            isinstance(items, list),
            "listSessions.items is an array",
            f"got {type(items).__name__}",
            "reference/root#listsessionsresult",
        )
    ]
    cursor = result.get("nextCursor")
    findings.append(
        Finding(
            cursor is None or isinstance(cursor, str),
            "nextCursor is an opaque string when present",
            f"got {type(cursor).__name__}",
            "specification/overview.md -- cursors are opaque and server-defined",
            severity="should",
        )
    )
    return findings


async def _check_unknown_method(client: AhpClient) -> list[Finding]:
    """A host MUST decline an unknown method, not hang or close.

    AHP has no capability object, so `-32601` is the only "no" available -- and a
    client that never probes cannot tell "declined" from "still thinking".
    """
    try:
        await asyncio.wait_for(client.request("thisMethodDoesNotExist", {}), 10)
    except RpcError as exc:
        return [
            Finding(
                exc.code == -32601,
                "an unknown method is declined with -32601",
                f"answered {exc.code}",
                "JSON-RPC 2.0 -- MethodNotFound",
            )
        ]
    except Exception as exc:
        return [
            Finding(
                False,
                "an unknown method is declined with -32601",
                f"raised {type(exc).__name__} instead of answering",
                "JSON-RPC 2.0 -- MethodNotFound",
            )
        ]
    return [
        Finding(
            False,
            "an unknown method is declined with -32601",
            "the host answered successfully, which means it accepts anything",
            "JSON-RPC 2.0 -- MethodNotFound",
        )
    ]
