"""Protocol version negotiation.

Since spec 1.0.0 the rule is normative and ships with a corpus
(``types/test-cases/version-negotiation.json``, gated by
``tests/conformance/test_version_negotiation.py``): the host selects the
**highest** offered version inside the caret range of any supported baseline
-- ``^1.0.0`` is ``>=1.0.0 <2.0.0``, ``^0.9.0`` is ``>=0.9.0 <0.10.0`` -- and
returns that exact offered string, whatever the client's preference order.
A malformed offer is an explicit error, not something to skip.

Two consequences that reverse what this module used to do:

* An offered version may now *exceed* the baseline: a ``1.0.0`` host accepts
  ``1.10.0``. SemVer makes MINOR and PATCH additive past 1.0, and every
  reducer here ignores an action type it does not know, so the newer client
  converges.
* ``["garbage", "0.9.0"]`` used to negotiate ``0.9.0``. It now raises
  :class:`InvalidProtocolVersionError`, which a host answers with ``-32602``.

The one deliberate extension over upstream: we hold a **set** of baselines
that may be wider than upstream's two, because the clients we care about do
not move in lockstep with the spec. The set is honest about *actions*, not
about every shape: the reducers are the pinned revision's, whatever version was
negotiated. 0.8.0's ``session/customizationToggled`` carries ``enablement``
where 0.7.0 carried ``enabled``, and 0.9.0 moves a turn's error into its
response parts -- an older peer gets those exactly as it would against
upstream's own reducers.

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
    "InvalidProtocolVersionError",
    "is_compatible",
    "negotiate",
    "parse_version",
]

#: Most-preferred first, mirroring the client-side convention. Upstream 1.0.0
#: advertises only ``1.0.0`` and ``0.9.0``; the three older MINORs stay for one
#: release after that drop (``UPSTREAM.md``, step 8) so a deployed client is not
#: refused by a routine dependency bump.
DEFAULT_SUPPORTED_VERSIONS: Final[tuple[str, ...]] = ("1.0.0", "0.9.0", "0.8.0", "0.7.0", "0.6.0")

# No leading zeros, no pre-release or build metadata. Matched with `fullmatch`:
# `$` would also match before a trailing newline, so `re.match` accepts
# "1.0.0\n" -- one of the corpus's invalid cases.
_SEMVER = re.compile(r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)")


class InvalidProtocolVersionError(ValueError):
    """An offered version is not a well-formed ``MAJOR.MINOR.PATCH`` string.

    Upstream's ``negotiateProtocolVersion`` throws here rather than skipping
    the entry, so a peer sees why it was refused instead of a bare
    ``UnsupportedProtocolVersion``.
    """

    def __init__(self, version: str) -> None:
        super().__init__(f"Invalid protocol version: {version!r}")
        self.version = version


def parse_version(version: str) -> tuple[int, int, int] | None:
    """Parse ``MAJOR.MINOR.PATCH``, or ``None`` if it is not one."""
    match = _SEMVER.fullmatch(version)
    if match is None:
        return None
    return int(match[1]), int(match[2]), int(match[3])


def is_compatible(offered: str, baseline: str) -> bool:
    """Whether *offered* lies in the caret range ``^baseline``.

    * majors must match;
    * when major is 0, minors must also match -- every 0.x minor is breaking;
    * when both are 0, patches must match too (``^0.0.3`` is exactly 0.0.3);
    * *offered* must be at least *baseline*.
    """
    a = parse_version(offered)
    b = parse_version(baseline)
    if a is None or b is None:
        return False
    if a[0] != b[0]:
        return False
    if a[0] == 0 and a[1] != b[1]:
        return False
    if a[0] == 0 and a[1] == 0 and a[2] != b[2]:
        return False
    return a >= b


def negotiate(
    offered: Sequence[str],
    supported: Iterable[str] = DEFAULT_SUPPORTED_VERSIONS,
) -> str | None:
    """Pick the highest offered version we can speak, or ``None`` to refuse.

    ``None`` means the caller MUST respond ``UnsupportedProtocolVersion``
    (-32005) with its supported baselines and close the connection.

    Raises :class:`InvalidProtocolVersionError` for the first malformed entry,
    even when another entry would have been acceptable -- the corpus pins
    ``["1.0.0", "invalid"]`` as invalid.
    """
    baselines = list(supported)
    best: tuple[int, int, int] | None = None
    best_version: str | None = None
    for candidate in offered:
        parsed = parse_version(candidate)
        if parsed is None:
            raise InvalidProtocolVersionError(candidate)
        if not any(is_compatible(candidate, baseline) for baseline in baselines):
            continue
        if best is None or parsed > best:
            best, best_version = parsed, candidate
    return best_version
