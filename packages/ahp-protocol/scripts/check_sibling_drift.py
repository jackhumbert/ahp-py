#!/usr/bin/env python3
"""Report divergence between this package and the sibling host's copy.

`agent-host-server-py` has not been migrated onto this distribution yet
(ADR 0002), so it still carries its own copy of the extracted tree. Until that
lands, a fix can be made in one repo and not the other -- and the class of bug
most likely to be fixed here is precisely the class the 247-fixture corpus
cannot see, so both suites would stay green while the two implementations
disagree about what a peer just sent.

This reports; it does not fail. A fix landing in either repo is legitimate work,
and a gate that blocks on the sibling being in lockstep would just be turned
off. What matters is that the divergence is *visible on the day it lands*
instead of at migration time.

    python scripts/check_sibling_drift.py [--sibling PATH]

Exit codes: 0 clean or unavailable, 1 drifted (advisory).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SIBLING = ROOT.parent / "agent-host-server-py"

#: Subpackages copied wholesale. Everything under them is compared.
SUBPACKAGES = ("types", "reducers", "conformance", "transport")

#: Modules promoted out of the sibling's `core/` to this package's root, and the
#: shared scripts. `our path -> their path`, both relative to their repo roots.
PROMOTED: dict[str, str] = {
    "src/agent_host_protocol/versions.py": "src/agent_host_server/core/versions.py",
    "src/agent_host_protocol/errors.py": "src/agent_host_server/core/errors.py",
    "src/agent_host_protocol/channels.py": "src/agent_host_server/core/channels.py",
    "scripts/generate_tables.py": "scripts/generate_tables.py",
    "scripts/js_semantics_cases.py": "scripts/js_semantics_cases.py",
    "scripts/js_semantics_loader.mjs": "scripts/js_semantics_loader.mjs",
    "scripts/js_semantics_oracle.mjs": "scripts/js_semantics_oracle.mjs",
    "scripts/regenerate_js_semantics.sh": "scripts/regenerate_js_semantics.sh",
    "scripts/vendor_upstream.sh": "scripts/vendor_upstream.sh",
}

#: Divergence that is expected and explained. Keep this short, and keep the
#: reasons here rather than in a commit message -- a reader hitting a DRIFTED
#: line needs to know whether the difference is the point or the bug.
ALLOWED: dict[str, str] = {
    "conformance/corpus.py": (
        "CORPUS_ROOT resolves the packaged tree first so an installed wheel can "
        "run the suite; the sibling's version resolves only in a checkout"
    ),
    "src/agent_host_protocol/channels.py": (
        "reducer_name_for(uri) deleted -- scheme routing binds no reducer at all "
        "for the URIs real clients mint -- and replaced with reducer_for_state()"
    ),
    "src/agent_host_protocol/errors.py": (
        "adds from_json(), the receiving half a host never needed"
    ),
    "scripts/js_semantics_cases.py": (
        "annotated for `mypy --strict`; the sibling's copy fails its own gate"
    ),
}


def _normalise(text: str, package: str) -> str:
    """Erase the import-root rename so it does not dominate every diff."""
    return text.replace(package, "«pkg»")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sibling", type=Path, default=DEFAULT_SIBLING)
    args = parser.parse_args()

    theirs_root = args.sibling / "src" / "agent_host_server"
    if not theirs_root.is_dir():
        print(f"sibling not found at {args.sibling} -- skipping", file=sys.stderr)
        return 0

    ours_root = ROOT / "src" / "agent_host_protocol"
    pairs: list[tuple[str, Path, Path]] = []

    for sub in SUBPACKAGES:
        for ours in sorted((ours_root / sub).rglob("*.py")):
            rel = ours.relative_to(ours_root).as_posix()
            pairs.append((rel, ours, theirs_root / rel))

    for ours_rel, theirs_rel in PROMOTED.items():
        pairs.append((ours_rel, ROOT / ours_rel, args.sibling / theirs_rel))

    drifted: list[str] = []
    explained: list[str] = []
    identical: set[str] = set()
    missing: list[str] = []

    for rel, ours, theirs in pairs:
        if not ours.exists():
            missing.append(f"{rel} (absent here -- stale entry in this script?)")
            continue
        if not theirs.exists():
            missing.append(f"{rel} (absent from the sibling)")
            continue
        a = _normalise(ours.read_text(encoding="utf-8"), "agent_host_protocol")
        b = _normalise(theirs.read_text(encoding="utf-8"), "agent_host_server")
        if a == b:
            identical.add(rel)
        elif rel in ALLOWED:
            explained.append(rel)
        else:
            drifted.append(rel)

    for rel in explained:
        print(f"explained: {rel}\n    {ALLOWED[rel]}")
    for rel in missing:
        print(f"only here: {rel}")
    for rel in drifted:
        print(f"DRIFTED:   {rel}")

    # An ALLOWED entry that no longer differs is a stale exemption, and a stale
    # exemption is how a real divergence later gets waved through.
    stale = sorted(set(ALLOWED) & identical)
    for rel in stale:
        print(f"STALE EXEMPTION: {rel} is identical -- remove it from ALLOWED")

    print(f"\n{len(identical)} identical, {len(explained)} explained, {len(drifted)} drifted")

    if stale:
        return 1
    if drifted:
        print(
            f"\n{len(drifted)} file(s) diverge from agent-host-server-py. "
            "If the fix belongs in both, port it; if it belongs only here, add "
            "it to ALLOWED with the reason.",
            file=sys.stderr,
        )
        return 1
    print("\nno unexplained drift")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
