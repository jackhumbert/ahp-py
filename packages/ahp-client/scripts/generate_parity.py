#!/usr/bin/env python3
"""Generate `docs/parity.md` from the code, never from a hand-kept list.

Every row here is derived: the command table from
`agent_host_client.client.commands`, the notification set from
`client.events.NOTIFICATION_METHODS`, the reducer set from
`agent_host_protocol.reducers.REDUCERS`, the mirrored-channel column from the
mirror's bindable reducer set, the reverse direction from the serve router's
`REVERSE_METHODS` and the shipped servers' own dispatch tables, the
dispatchable actions from the generated upstream table, and the totals from the
vendored `ts/messages.ts`.

The reason is the sibling host's, and it applies harder here: *a hand-kept list
never contains the thing someone just added*. Three independent design passes
over this protocol each wrote "28 commands" or "~30 commands"; the real number is
27, and only a generated table catches that.

    python scripts/generate_parity.py            # write docs/parity.md
    python scripts/generate_parity.py --check    # fail if it would change
"""

from __future__ import annotations

import argparse
import re
import sys
from collections.abc import Sequence
from pathlib import Path

from agent_host_protocol.conformance.corpus import CORPUS_ROOT
from agent_host_protocol.reducers import REDUCERS
from agent_host_protocol.types import ACTION_TYPES, IS_CLIENT_DISPATCHABLE

from agent_host_client.client.commands import COMMANDS, ROOT_SCOPED
from agent_host_client.client.events import NOTIFICATION_METHODS
from agent_host_client.client.mirror import REDUCER_NAMES
from agent_host_client.serve.resources import _SNAKE, _VIRTUAL_METHODS
from agent_host_client.serve.router import REVERSE_METHODS

#: What a shipped server can actually answer, from the dispatch tables the
#: servers themselves route on -- `FileResourceServer` looks handlers up in
#: `_SNAKE` and `VirtualResourceServer` gates on `_VIRTUAL_METHODS`, so a
#: method outside both is `-32601` from every server this package provides.
#: This used to be a hand-kept `cross`, which claimed the whole reverse
#: direction was unimplemented while the router routed all ten.
_SERVED = frozenset(_SNAKE) | _VIRTUAL_METHODS

ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / "docs" / "parity.md"

#: What the TypeScript client ships a typed wrapper for. Everything else it
#: leaves to a raw `request()`. Counted from `clients/typescript/src/client/client.ts`.
_TS_WRAPPERS = frozenset(
    {
        "initialize",
        "reconnect",
        "subscribe",
        "ping",
        "resourceRead",
        "resourceWrite",
        "resourceList",
        "resourceCopy",
        "resourceDelete",
        "resourceMove",
        "resourceResolve",
        "resourceMkdir",
        "resourceRequest",
        "createResourceWatch",
        "completions",
        "sessionConfigCompletions",
    }
)

#: The five the TypeScript client turns into subscription events. The other four
#: hit a `default:` branch that calls neither `fanOut` nor the global tap, so
#: they reach nothing -- despite a source comment implying otherwise.
_TS_NOTIFICATIONS = frozenset(
    {
        "action",
        "root/sessionAdded",
        "root/sessionRemoved",
        "root/sessionSummaryChanged",
        "auth/required",
    }
)


def _map_entries(name: str) -> list[str]:
    """Method names declared in one of the vendored TypeScript maps."""
    source = (CORPUS_ROOT / "ts" / "messages.ts").read_text(encoding="utf-8")
    match = re.search(rf"export interface {name} \{{(.*?)\n\}}", source, re.S)
    if match is None:
        raise SystemExit(f"{name} not found in the vendored messages.ts")
    return re.findall(r"'([A-Za-z/]+)':", match.group(1))


def _table(rows: Sequence[tuple[str, ...]], headers: tuple[str, ...]) -> str:
    out = ["| " + " | ".join(headers) + " |", "|" + "---|" * len(headers)]
    out.extend("| " + " | ".join(row) + " |" for row in rows)
    return "\n".join(out)


def render() -> str:
    forward = _map_entries("CommandMap")
    client_notifications = _map_entries("ClientNotificationMap")
    reverse = _map_entries("ServerCommandMap")
    server_notifications = _map_entries("ServerNotificationMap")

    tick = "✅"
    cross = "—"

    command_rows = [
        (
            f"`{method}`",
            tick if method in _TS_WRAPPERS else cross,
            tick if method in COMMANDS else cross,
            "root" if method in ROOT_SCOPED else "caller",
        )
        for method in forward
    ]
    notification_rows = [
        (
            f"`{method}`",
            tick if method in _TS_NOTIFICATIONS else cross,
            tick if method in NOTIFICATION_METHODS else cross,
        )
        for method in server_notifications
    ]
    dispatchable = sorted(a for a in ACTION_TYPES if IS_CLIENT_DISPATCHABLE.get(a))
    channels = sorted(REDUCERS)

    if set(reverse) != REVERSE_METHODS:
        # The prose below says "every method is routed"; refuse to render it
        # the moment the router and the vendored map disagree.
        raise SystemExit("serve.router.REVERSE_METHODS disagrees with ServerCommandMap")
    unserved = ", ".join(f"`{m}`" for m in sorted(REVERSE_METHODS - _SERVED))
    reverse_rows = [(f"`{m}`", cross, tick if m in _SERVED else cross) for m in reverse]
    channel_rows = [(f"`{c}`", tick, tick if c in REDUCER_NAMES else cross) for c in channels]

    return f"""# Parity matrix

<!-- GENERATED by scripts/generate_parity.py. Do not edit by hand. -->

Every row is derived from the code and from the vendored upstream
`ts/messages.ts`, not from a list somebody maintains. `tests/docs/test_parity_matrix_is_true.py`
regenerates this file and fails if it would differ.

**One column this cannot fill in: "proven against a real host."** A derived
matrix is self-updating and also self-congratulatory — a wrapper that exists but
has never been exercised shows green. Treat the ticks as *implemented*, and the
interop suite as the evidence.

## Client → server requests ({len(forward)})

{_table(command_rows, ("Method", "TS client wrapper", "Here", "Channel"))}

## Client → server notifications ({len(client_notifications)})

{_table([(f"`{m}`", tick, tick) for m in client_notifications], ("Method", "TS client", "Here"))}

## Server → client notifications ({len(server_notifications)})

{_table(notification_rows, ("Method", "TS client", "Here"))}

The TypeScript client surfaces {len(_TS_NOTIFICATIONS)} of {len(server_notifications)}.
The remainder reach neither its per-URI subscriptions nor its global `events()`
stream, because its `default:` branch returns without publishing.

## Server → client requests ({len(reverse)})

The reverse direction. The TypeScript client ships a typed handler registry and
no implementations; `ahpx` implements two of ten, read-only. Here, every method
is routed by `serve.ResourceRouter`, and a tick means a shipped server
(`FileResourceServer` or `VirtualResourceServer`) answers it. The rest —
{unserved} — are declined with `-32601`, which is how a peer says no in a
protocol with no capability object; `docs/plan.md` §1.3 records why the watch
server stays absent.

{_table(reverse_rows, ("Method", "TS client", "Here"))}

## Channels ({len(channels)})

Reducers come from `agent-host-protocol`, so all seven are available to the
mirror, which binds any of them by name at registration. The TypeScript
`AhpStateMirror` wires four and silently ignores every `ahp-chat:` snapshot.

{_table(channel_rows, ("Reducer", "Available", "Mirrored here"))}

## Client-dispatchable actions ({len(dispatchable)} of {len(ACTION_TYPES)})

Enumerated from the generated `IS_CLIENT_DISPATCHABLE` table.

{chr(10).join(f"- `{a}`" for a in dispatchable)}
"""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="fail instead of writing")
    args = parser.parse_args()

    rendered = render()
    if args.check:
        current = TARGET.read_text(encoding="utf-8") if TARGET.exists() else ""
        if current != rendered:
            print(
                "docs/parity.md is stale; run `python scripts/generate_parity.py`",
                file=sys.stderr,
            )
            return 1
        return 0
    TARGET.parent.mkdir(parents=True, exist_ok=True)
    TARGET.write_text(rendered, encoding="utf-8")
    print(f"wrote {TARGET.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
