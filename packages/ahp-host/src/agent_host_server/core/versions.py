"""Protocol version negotiation.

Semantics are ported from the reference host's
``src/vs/platform/agentHost/common/state/protocol/version/negotiation.ts``, with
one deliberate extension: the reference host holds a single ``current`` version
and therefore speaks exactly one MINOR, so a host declaring ``0.7.0`` there
*rejects* an offered ``0.6.0``. We hold a **set**, because the two clients we
care about disagree -- VS Code prefers 0.7.0 while the installable npm client
speaks 0.6.0 -- and the action delta between them is entirely outside v0.1's
scope. See ADR 0002.

Correctness here is entirely ours: the reference client does not verify the
version a host returns, and will happily proceed on one it never offered
(``docs/experiments.md`` E3).
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from typing import Final

__all__ = [
    "DEFAULT_SUPPORTED_VERSIONS",
    "is_compatible",
    "negotiate",
    "parse_version",
]

#: Most-preferred first, mirroring the client-side convention.
DEFAULT_SUPPORTED_VERSIONS: Final[tuple[str, ...]] = ("0.7.0", "0.6.0")

_SEMVER = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")


def parse_version(version: str) -> tuple[int, int, int] | None:
    """Parse ``MAJOR.MINOR.PATCH``. Pre-release and build metadata are not allowed."""
    match = _SEMVER.match(version)
    if match is None:
        return None
    return int(match[1]), int(match[2]), int(match[3])


def is_compatible(offered: str, ours: str) -> bool:
    """Whether we can faithfully speak *offered* while implementing *ours*.

    * majors must match;
    * when major is 0, minors must also match -- every 0.x minor bump is breaking;
    * *offered* must not exceed *ours*: a host that only knows 0.1.0 cannot
      pretend to speak 0.1.5.
    """
    a = parse_version(offered)
    b = parse_version(ours)
    if a is None or b is None:
        return False
    if a[0] != b[0]:
        return False
    if a[0] == 0 and a[1] != b[1]:
        return False
    return a <= b


def negotiate(
    offered: Sequence[str],
    supported: Iterable[str] = DEFAULT_SUPPORTED_VERSIONS,
) -> str | None:
    """Pick the highest offered version we can speak, or ``None`` to refuse.

    ``None`` means the caller MUST respond ``UnsupportedProtocolVersion``
    (-32005) and close the connection.

    The client orders ``protocolVersions`` most-preferred-first, but like the
    reference host we deterministically pick the highest compatible entry rather
    than honouring that order -- the newest version both peers understand is
    always the best available behaviour.
    """
    supported_list = list(supported)
    best: tuple[int, int, int] | None = None
    best_version: str | None = None
    for candidate in offered:
        parsed = parse_version(candidate)
        if parsed is None:
            continue
        if not any(is_compatible(candidate, ours) for ours in supported_list):
            continue
        if best is None or parsed > best:
            best, best_version = parsed, candidate
    return best_version
