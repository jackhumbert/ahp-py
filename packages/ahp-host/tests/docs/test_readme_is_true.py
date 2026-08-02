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


def _answered_commands() -> set[str]:
    """Every command name the dispatcher routes.

    Parsed from `host.py` rather than kept in a list here, for the same reason
    the flag check is derived from `--help`: a hand-kept list never contains
    the thing someone just added, which is the only case that matters.
    """
    source = (ROOT / "src" / "agent_host_server" / "core" / "host.py").read_text()
    found = set(re.findall(r'if method == "([a-zA-Z]+)"', source))
    # The two frozensets span lines, so match across them. An earlier version
    # of this parse missed `resourceRead` and `resourceCopy` and then blamed
    # the README, which is the wrong direction to be wrong in.
    for name in ("_RESOURCE_METHODS", "_RESOURCE_WRITE_METHODS"):
        block = re.search(name + r".*?\{(.*?)\}", source, re.DOTALL)
        assert block, f"{name} moved; fix this parse rather than the assertion"
        found |= {piece.strip().strip('"') for piece in block.group(1).split(",") if piece.strip()}
    return {name for name in found if name and name[0].islower()}


def test_every_command_we_answer_is_in_the_readme() -> None:
    """The README listed ten commands while the host answered twenty-nine, and
    claimed the terminal, changeset and resource-watch channels were "not
    registered and their commands not implemented" long after all three
    shipped. It also said "No command answers MethodNotFound any more" directly
    above "Every one returns a proper JSON-RPC MethodNotFound".

    Prose contradicting itself is not catchable by a test. Prose contradicting
    the dispatcher is.
    """
    answered = _answered_commands()
    assert len(answered) > 20, "the parse broke; fix it rather than the assertion"
    undocumented = {name for name in answered if f"`{name}`" not in README}
    assert not undocumented, f"commands the README does not mention: {sorted(undocumented)}"


def test_the_readme_does_not_claim_unimplemented_commands() -> None:
    """The other direction: a command named in the README that the host does
    not answer sends a reader to `MethodNotFound`."""
    answered = _answered_commands()
    # Only names shaped like our commands, and only those the README presents
    # in backticks as part of the surface.
    mentioned = set(re.findall(r"`(resource[A-Z][a-zA-Z]*|[a-z]+[A-Z][a-zA-Z]*)`", README))
    protocol_shaped = {
        name
        for name in mentioned
        if name.startswith(("resource", "create", "dispose", "session"))
        or name in {"initialize", "subscribe", "unsubscribe", "reconnect", "listSessions"}
    }
    phantom = protocol_shaped - answered
    assert not phantom, f"README names commands the host does not answer: {sorted(phantom)}"


def test_the_typed_marker_ships() -> None:
    """We advertise `Typing :: Typed` and shipped no `py.typed`, so a
    downstream mypy said "module is installed, but missing library stubs or
    py.typed marker" and typed every export as `Any`. Verified against a real
    installed wheel, not the source tree -- from a checkout it works either
    way, which is why nobody noticed."""
    import agent_host_server

    package = Path(agent_host_server.__file__).parent
    assert (package / "py.typed").is_file()

    classifiers = (ROOT / "pyproject.toml").read_text()
    assert "Typing :: Typed" in classifiers, "the marker ships but the claim was dropped"
