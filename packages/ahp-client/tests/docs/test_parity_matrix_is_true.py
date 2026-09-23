"""The parity matrix, and the claims it rests on.

The matrix is generated, so the interesting assertions are not "does the file
match" (though that is one of them) but "does the table in the code agree with
the vendored upstream types". Three independent design passes over this protocol
each wrote 28 or ~30 commands. It is 27, and only a derived table catches that.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest
from agent_host_protocol.conformance.corpus import CORPUS_ROOT
from agent_host_protocol.reducers import REDUCERS
from agent_host_protocol.types import ACTION_TYPES, IS_CLIENT_DISPATCHABLE

from agent_host_client.client.commands import CALLER_SCOPED, COMMANDS, ROOT_SCOPED
from agent_host_client.client.events import NOTIFICATION_METHODS
from agent_host_client.client.mirror import REDUCER_NAMES
from agent_host_client.serve.resources import _SNAKE, _VIRTUAL_METHODS
from agent_host_client.serve.router import REVERSE_METHODS

ROOT = Path(__file__).resolve().parents[2]
MESSAGES = (CORPUS_ROOT / "ts" / "messages.ts").read_text(encoding="utf-8")


def _map_entries(name: str) -> list[str]:
    match = re.search(rf"export interface {name} \{{(.*?)\n\}}", MESSAGES, re.S)
    assert match is not None, f"{name} missing from the vendored messages.ts"
    return re.findall(r"'([A-Za-z/]+)':", match.group(1))


def test_the_vendored_map_is_readable() -> None:
    """Guard the guard: a regex that silently matches nothing proves nothing."""
    assert len(_map_entries("CommandMap")) > 20


def test_every_upstream_command_has_a_wrapper() -> None:
    upstream = set(_map_entries("CommandMap"))
    assert upstream == COMMANDS, (
        f"missing: {sorted(upstream - COMMANDS)}; invented: {sorted(COMMANDS - upstream)}"
    )


def test_there_are_twenty_seven_of_them() -> None:
    """Pinned because every prose description of this protocol gets it wrong."""
    assert len(COMMANDS) == 27
    assert len(_map_entries("ClientNotificationMap")) == 2
    assert len(_map_entries("ServerCommandMap")) == 10
    assert len(_map_entries("ServerNotificationMap")) == 9


def test_root_and_caller_scoping_partition_the_command_set() -> None:
    assert set() == ROOT_SCOPED & CALLER_SCOPED
    assert ROOT_SCOPED | CALLER_SCOPED == COMMANDS


@pytest.mark.parametrize("method", sorted(ROOT_SCOPED))
def test_root_scoped_commands_are_declared_root_upstream(method: str) -> None:
    """Read the params interface out of the vendored types and check its channel.

    Getting this wrong is silent: the host sees a well-formed request against a
    channel it does not expect.
    """
    iface = _params_interface(method)
    assert _channel_declaration(iface) == "'ahp-root://'", (
        f"{method} is in ROOT_SCOPED but {iface} does not declare channel: 'ahp-root://'"
    )


@pytest.mark.parametrize("method", sorted(CALLER_SCOPED))
def test_caller_scoped_commands_are_not_declared_root_upstream(method: str) -> None:
    iface = _params_interface(method)
    assert _channel_declaration(iface) != "'ahp-root://'", (
        f"{method} is in CALLER_SCOPED but {iface} declares channel: 'ahp-root://'"
    )


def test_completions_is_caller_scoped() -> None:
    """Called out on its own because forcing it to root silently breaks every
    @-mention picker, and it is the exception all three reference clients note."""
    assert "completions" in CALLER_SCOPED
    assert "sessionConfigCompletions" in ROOT_SCOPED


def _params_interface(method: str) -> str:
    match = re.search(rf"'{method}':\s*\{{\s*params:\s*(\w+)", MESSAGES)
    assert match is not None, f"{method} not found in the vendored maps"
    return match.group(1)


def _channel_declaration(iface: str) -> str:
    """The `channel:` type an upstream `*Params` interface declares.

    A missing interface **fails** rather than defaulting. Defaulting to `URI`
    would let a params file that stopped being vendored quietly turn every
    root-scoped assertion into a tautology.
    """
    for path in sorted((CORPUS_ROOT / "ts").glob("commands*.ts")):
        text = path.read_text(encoding="utf-8")
        block = re.search(rf"export interface {iface}\b[^{{]*\{{(.*?)\n\}}", text, re.S)
        if block is None:
            if re.search(rf"export interface {iface} extends BaseParams \{{\}}", text):
                # No members of its own; `BaseParams.channel` is a plain URI.
                return "URI"
            continue
        channel = re.search(r"^\s*channel:\s*([^;]+);", block.group(1), re.M)
        return channel.group(1).strip() if channel else "URI"
    raise AssertionError(
        f"{iface} was not found in the vendored ts/commands*.ts -- "
        "is scripts/vendor_upstream.sh still fetching every commands.ts?"
    )


def test_all_nine_server_notifications_are_surfaced() -> None:
    """The TypeScript client surfaces five and drops four at a `default:` branch
    that reaches neither its subscriptions nor its `events()` stream."""
    assert set(_map_entries("ServerNotificationMap")) == NOTIFICATION_METHODS
    assert len(NOTIFICATION_METHODS) == 9


def test_all_seven_reducers_are_available() -> None:
    assert len(REDUCERS) == 7


def test_the_mirror_can_bind_every_reducer() -> None:
    """The matrix's "Mirrored here" column derives from `REDUCER_NAMES`, the set
    `StateMirror.bind` accepts. It must be all seven: the TS mirror wires four,
    and matching that would inherit its `ahp-chat:`-snapshots-ignored gap."""
    assert frozenset(REDUCERS) == REDUCER_NAMES


def test_the_router_routes_exactly_the_vendored_reverse_methods() -> None:
    """The matrix says every server→client request is routed; that claim is only
    derivable while the router's set and the vendored `ServerCommandMap` agree."""
    assert set(_map_entries("ServerCommandMap")) == REVERSE_METHODS


def test_the_reverse_here_column_matches_the_servers_dispatch_tables() -> None:
    """The first published matrix hardcoded '—' for the whole reverse direction
    while the code implemented nine of ten — asserted-false documentation, the
    exact failure class this repo exists to prevent. Parse the generated table
    and require its ticks to equal the servers' own dispatch tables.

    The one deliberate gap is also pinned: `createResourceWatch` is routed but
    no shipped server answers it (plan §1.3 argues the watch server should stay
    absent). A second unserved method appearing here is a decision to record,
    not a row to shrug at.
    """
    served = frozenset(_SNAKE) | _VIRTUAL_METHODS
    assert REVERSE_METHODS - served == {"createResourceWatch"}

    matrix = (ROOT / "docs" / "parity.md").read_text(encoding="utf-8")
    section = re.search(r"## Server → client requests.*?\n\n(\| Method.*?)\n\n", matrix, re.S)
    assert section is not None, "the reverse table is missing from docs/parity.md"
    rows = re.findall(r"\| `(\w+)` \| [^|]+ \| ([^|]+) \|", section.group(1))
    assert {name for name, _ in rows} == REVERSE_METHODS
    ticked = {name for name, cell in rows if "✅" in cell}
    assert ticked == served, f"ticked: {sorted(ticked)}; served: {sorted(served)}"


def test_dispatchable_action_count_is_pinned() -> None:
    dispatchable = [a for a in ACTION_TYPES if IS_CLIENT_DISPATCHABLE.get(a)]
    assert len(ACTION_TYPES) == 86
    assert len(dispatchable) == 39


def test_the_generated_matrix_is_not_stale() -> None:
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "generate_parity.py"), "--check"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
