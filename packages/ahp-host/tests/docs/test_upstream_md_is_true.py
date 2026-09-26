"""`UPSTREAM.md`'s post-extraction claims, checked against the tree.

This file has drifted twice in ways a reader would act on: it argued
"`0.6.0` on the wire" long after the host started preferring `0.7.0`, and it
carried a vendored-file table and re-vendoring procedure for a `vendor/` tree
this repository no longer has. Both went stale silently because nothing
asserted them. These checks pin the corrected claims.
"""

from __future__ import annotations

import re
from pathlib import Path

from ahp_protocol import DEFAULT_SUPPORTED_VERSIONS

ROOT = Path(__file__).resolve().parents[2]
UPSTREAM = (ROOT / "UPSTREAM.md").read_text()


def test_it_does_not_describe_a_local_vendor_tree() -> None:
    """The pin moved to `ahp-protocol`; a vendored table here described
    files that do not exist and a procedure nobody could run. If a `vendor/`
    directory ever reappears, the 'Nothing is vendored here' sentence goes
    false and this file needs rewriting before the directory lands."""
    assert not (ROOT / "vendor").exists()
    assert "Nothing is vendored here" in UPSTREAM
    # Listed as vendored for months while the vendoring script (the sibling's,
    # and before that ours) never fetched it.
    assert "registry-snapshot" not in UPSTREAM


def test_the_preferred_wire_version_is_stated_where_the_prose_argues() -> None:
    """The old section argued the wire version and the corpus tag must differ,
    quoting `0.6.0` — unchecked, because only the pin *table* was asserted.
    Pin the prose sentence too, to the version we actually prefer."""
    assert f"prefers **`{DEFAULT_SUPPORTED_VERSIONS[0]}`**" in UPSTREAM


def test_the_interop_client_pin_matches_what_the_interop_tests_install() -> None:
    """The surviving claim of the rewritten section: the npm client the docs
    name must be the one `tests/interop/` actually installs."""
    pattern = r"@microsoft/agent-host-protocol@([\d.]+)"
    documented = set(re.findall(pattern, UPSTREAM))
    installed = set(re.findall(pattern, (ROOT / "tests/interop/test_real_client.py").read_text()))
    assert documented, "UPSTREAM.md no longer names the npm interop client"
    assert documented == installed, f"docs say {documented}, the interop tests use {installed}"
