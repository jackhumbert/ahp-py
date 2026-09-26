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
        [sys.executable, "-m", "ahp_host", "--help"],
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
    """`from ahp_host import Host` raised ImportError against an
    installed wheel until this was noticed -- it is the first line anyone
    writes."""
    from ahp_host import AgentProvider, Host, Policy

    assert Host is not None
    assert AgentProvider is not None
    assert Policy is not None


def test_upstream_pin_matches_what_we_negotiate() -> None:
    """UPSTREAM.md said `0.6.0` on the wire long after we started preferring
    `0.7.0` and real clients started agreeing to it.

    A pin document that drifts is worse than none: it is the file someone reads
    to find out what this implementation actually speaks.
    """
    from ahp_protocol.versions import DEFAULT_SUPPORTED_VERSIONS

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
    source = (ROOT / "src" / "ahp_host" / "core" / "host.py").read_text()
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
    import ahp_host

    package = Path(ahp_host.__file__).parent
    assert (package / "py.typed").is_file()

    classifiers = (ROOT / "pyproject.toml").read_text()
    assert "Typing :: Typed" in classifiers, "the marker ships but the claim was dropped"


#: A bullet in the "genuinely absent" list whose bold lead is a bare action
#: name, e.g. ``- **`chat/usage`.** No producer, ...``. Deliberately not every
#: backticked action in that section: the `root/sessionSummaryChanged` bullet is
#: about a *field retraction* the wire cannot express, not about an action the
#: host never publishes, and a looser pattern would read it as a claim the code
#: contradicts.
_ABSENT_ACTION = re.compile(r"^- \*\*`([a-z]+/[A-Za-z]+)`\.?\*\*", re.MULTILINE)

#: Proof that the pattern still matches the shape it is looking for. The
#: README's own list is EMPTY of these today, which is the correct state and
#: also the state in which a broken regex is indistinguishable from a clean
#: bill of health -- so the extractor is exercised on a known input first.
_ABSENT_SAMPLE = "- **`chat/usage`.** No producer, so no token counts.\n"


def _absent_section() -> str:
    start = README.index("What is genuinely absent, and why:")
    return README[start : README.index("\n## ", start)]


def _published_action_types() -> str:
    return "\n".join(path.read_text(encoding="utf-8") for path in (ROOT / "src").rglob("*.py"))


def test_the_readme_does_not_call_an_action_absent_that_the_host_publishes() -> None:
    """The README said `chat/usage` had "no producer" long after one shipped.

    Nothing could catch that: it is prose about the *absence* of a feature, and
    every other check here is derived from what the host does have. This one
    reads the claim and looks for the thing it says is not there.

    A reader acts on this list -- it is the section that decides whether to
    build a client feature around a gap -- so a stale entry costs someone the
    work of routing around something that is right there.
    """
    assert _ABSENT_ACTION.findall(_ABSENT_SAMPLE) == ["chat/usage"], (
        "the pattern no longer matches the bullet shape it exists to find"
    )

    sources = _published_action_types()
    claimed_absent = set(_ABSENT_ACTION.findall(_absent_section()))
    published = {name for name in claimed_absent if f'"{name}"' in sources}
    assert not published, (
        f"the README calls these absent, and the host publishes them: {sorted(published)}"
    )


def test_the_install_block_supplies_every_sibling_dependency() -> None:
    """`pip install -e '.[ws]'` was the README's only install line, and it
    failed: `ahp-protocol` is not on PyPI — by design, now — so pip
    resolved it from an index that has never heard of it and stopped.

    Derived from `pyproject.toml` rather than from a list here, so a second
    sibling dependency cannot be added without the README learning about it.
    The package directory is what both supported forms contain — the
    `git+https://…#subdirectory=packages/<name>` install and the
    `-e ../<name>` checkout — so the assertion covers either.
    """
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    block = re.search(r"^dependencies\s*=\s*\[(.*?)\]", pyproject, re.S | re.M)
    assert block is not None, "pyproject declares no dependencies; this test has drifted"

    siblings = re.findall(r"[\"'](ahp-[a-z-]+)", block.group(1))
    assert siblings, "no sibling dependency found; if that is real, delete this test"

    start = README.index("## Try it")
    try_it = README[start : README.index("\n## ", start)]
    for name in siblings:
        repository = "packages/" + name
        assert repository in try_it, (
            f"{name} is a dependency and no index carries it, so the install block "
            f"has to supply it from its repository ({repository}) before the line "
            f"that needs it"
        )
