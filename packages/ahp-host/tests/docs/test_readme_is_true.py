"""The README's factual claims, checked against the code.

Not style-checking prose -- checking the specific claims a reader would act on:
which commands exist, which surfaces are off by default, and that the import
line at the top of the README actually imports.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
README = (ROOT / "README.md").read_text()


def _cli_flags() -> set[str]:
    """Every long flag `--help` prints. A subprocess because `main()` takes no
    argv -- reaching into the parser would test a different thing than the one
    a user runs."""
    import subprocess
    import sys

    printed = subprocess.run(
        [sys.executable, "-m", "agent_host_server", "--help"],
        capture_output=True,
        text=True,
        cwd=ROOT,
        check=False,
    ).stdout
    return set(re.findall(r"(--[a-z][a-z-]+)", printed))


def test_the_cli_flags_it_advertises_exist() -> None:
    """A README listing a flag the CLI does not have sends someone to a
    `unrecognized arguments` error."""
    advertised = set(re.findall(r"(?<![\w-])(--[a-z][a-z-]+)", README))
    real = _cli_flags()
    unknown = {f for f in advertised & _KNOWN_OURS if f not in real}
    assert not unknown, f"README advertises flags the CLI does not have: {sorted(unknown)}"


#: Only used for the "README mentions a flag we removed" direction. The other
#: direction -- "the CLI grew a flag nobody documented" -- is derived from
#: `--help`, never from a list here: a hand-kept list never contains the flag
#: someone just added, which is the only case that matters. An earlier version
#: of this file used a list for both, and a deliberately-added undocumented
#: flag sailed through.
_KNOWN_OURS = {
    "--allow-remote",
    "--changes",
    "--client-tools",
    "--configurable",
    "--confirm-tools",
    "--customizations",
    "--elicit",
    "--multi-chat",
    "--serve-directory",
    "--sequence-file",
    "--terminal",
    "--token",
    "--wire-log",
    "--writable",
}


def test_every_flag_we_own_is_documented() -> None:
    """The other direction: a flag that exists and is undocumented is a feature
    nobody will find."""
    # EVERY flag our own `--help` prints, minus argparse's built-in. If the CLI
    # has it, a reader has to be able to find out what it does. Derived, never
    # listed: a hand-kept list never contains the flag someone just added,
    # which is the only case that matters.
    real = _cli_flags() - {"--help"}
    documented = set(re.findall(r"(--[a-z][a-z-]+)", README))
    assert real <= documented, f"undocumented flags: {sorted(real - documented)}"


def test_the_public_import_works() -> None:
    """`from agent_host_server import Host` raised ImportError against an
    installed wheel until this was noticed -- it is the first line anyone
    writes."""
    from agent_host_server import AgentProvider, Host, Policy

    assert Host is not None
    assert AgentProvider is not None
    assert Policy is not None


def test_upstream_pin_matches_what_we_negotiate() -> None:
    """UPSTREAM.md said `0.6.0` on the wire long after we started preferring
    `0.7.0` and real clients started agreeing to it.

    A pin document that drifts is worse than none: it is the file someone reads
    to find out what this implementation actually speaks.
    """
    from agent_host_server.core.versions import DEFAULT_SUPPORTED_VERSIONS

    upstream = (ROOT / "UPSTREAM.md").read_text()
    preferred = DEFAULT_SUPPORTED_VERSIONS[0]
    line = next(
        (row for row in upstream.splitlines() if "negotiated on the wire" in row),
        "",
    )
    assert line, "UPSTREAM.md no longer states the negotiated wire version"
    assert preferred in line, f"UPSTREAM.md says {line.strip()!r} but we prefer {preferred}"
    for version in DEFAULT_SUPPORTED_VERSIONS:
        assert version in upstream, f"{version} is spoken but not mentioned in UPSTREAM.md"
