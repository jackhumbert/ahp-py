#!/usr/bin/env python3
"""Generate src/ahp_protocol/types/_generated.py from the vendored upstream
TypeScript source of truth.

Upstream generates every client -- Go, Rust, Kotlin, Swift -- and its own JSON
Schemas from types/*.ts via ts-morph. We do the same for the small set of tables
that are pure data:

  * ActionType             -- every action's wire string
  * IS_CLIENT_DISPATCHABLE -- which actions a client may originate
  * ACTION_INTRODUCED_IN   -- the action -> protocol version map
  * PROTOCOL_VERSION / SUPPORTED_PROTOCOL_VERSIONS
  * error codes

Run via `python scripts/generate_tables.py`; CI asserts the output is unchanged.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VENDOR = ROOT / "vendor" / "upstream" / "ts"
OUT = ROOT / "src" / "ahp_protocol" / "types" / "_generated.py"


def read(name: str) -> str:
    path = VENDOR / name
    if not path.exists():
        sys.exit(f"missing {path}; run scripts/vendor_upstream.sh first")
    return path.read_text(encoding="utf-8")


def parse_action_types(src: str) -> dict[str, str]:
    """`export const enum ActionType { RootAgentsChanged = 'root/agentsChanged', ... }`"""
    body = re.search(r"export const enum ActionType\s*\{(.*?)\n\}", src, re.S)
    if not body:
        sys.exit("could not locate `export const enum ActionType`")
    pairs = re.findall(r"(\w+)\s*=\s*'([^']+)'", body.group(1))
    if not pairs:
        sys.exit("ActionType enum parsed but empty")
    return dict(pairs)


def parse_client_dispatchable(src: str, members: dict[str, str]) -> dict[str, bool]:
    """`IS_CLIENT_DISPATCHABLE = { [ActionType.X]: true, ... }` -- keyed by enum member."""
    body = re.search(r"export const IS_CLIENT_DISPATCHABLE[^=]*=\s*\{(.*?)\n\};", src, re.S)
    if not body:
        sys.exit("could not locate `IS_CLIENT_DISPATCHABLE`")
    out: dict[str, bool] = {}
    for member, value in re.findall(r"\[ActionType\.(\w+)\]\s*:\s*(true|false)", body.group(1)):
        wire = members.get(member)
        if wire is None:
            sys.exit(f"IS_CLIENT_DISPATCHABLE names unknown ActionType.{member}")
        out[wire] = value == "true"
    return out


def parse_introduced_in(src: str, members: dict[str, str]) -> dict[str, str]:
    body = re.search(r"export const ACTION_INTRODUCED_IN[^=]*=\s*\{(.*?)\n\};", src, re.S)
    if not body:
        sys.exit("could not locate `ACTION_INTRODUCED_IN`")
    out: dict[str, str] = {}
    for member, version in re.findall(r"\[ActionType\.(\w+)\]\s*:\s*'([\d.]+)'", body.group(1)):
        wire = members.get(member)
        if wire is None:
            sys.exit(f"ACTION_INTRODUCED_IN names unknown ActionType.{member}")
        out[wire] = version
    return out


def parse_notification_introduced_in(src: str) -> dict[str, str]:
    """`NOTIFICATION_INTRODUCED_IN` keys are wire method strings, not
    `ActionType.` members -- the map covers `ServerNotificationMap` minus
    `action`, whose versioning ACTION_INTRODUCED_IN already carries."""
    body = re.search(r"export const NOTIFICATION_INTRODUCED_IN[^=]*=\s*\{(.*?)\n\};", src, re.S)
    if not body:
        sys.exit("could not locate `NOTIFICATION_INTRODUCED_IN`")
    return dict(re.findall(r"'([\w/]+)'\s*:\s*'([\d.]+)'", body.group(1)))


def parse_versions(src: str) -> tuple[str, list[str]]:
    current = re.search(r"export const PROTOCOL_VERSION = '([\d.]+)'", src)
    supported = re.search(
        r"export const SUPPORTED_PROTOCOL_VERSIONS[^=]*=\s*Object\.freeze\(\[(.*?)\]\)",
        src,
        re.S,
    )
    if not current or not supported:
        sys.exit("could not locate protocol version constants")
    return current.group(1), re.findall(r"'([\d.]+)'", supported.group(1))


def parse_error_codes(src: str, const_name: str) -> dict[str, int]:
    body = re.search(rf"export const {const_name} = \{{(.*?)\n\}} as const;", src, re.S)
    if not body:
        sys.exit(f"could not locate `{const_name}`")
    return {name: int(value) for name, value in re.findall(r"(\w+)\s*:\s*(-?\d+)", body.group(1))}


def pyrepr(value: object, indent: int = 4) -> str:
    pad = " " * indent
    if isinstance(value, dict):
        if not value:
            return "{}"
        items = "\n".join(f"{pad}{k!r}: {v!r}," for k, v in value.items())
        return "{\n" + items + "\n}"
    if isinstance(value, list):
        items = "\n".join(f"{pad}{v!r}," for v in value)
        return "[\n" + items + "\n]"
    return repr(value)


def main() -> None:
    actions_src = read("actions.ts")
    origin_src = read("action-origin.generated.ts")
    registry_src = read("registry.ts")
    errors_src = read("errors.ts")

    members = parse_action_types(actions_src)
    wire_types = sorted(members.values())
    dispatchable = parse_client_dispatchable(origin_src, members)
    introduced = parse_introduced_in(registry_src, members)
    notification_introduced = parse_notification_introduced_in(registry_src)
    current, supported = parse_versions(registry_src)
    jsonrpc_codes = parse_error_codes(errors_src, "JsonRpcErrorCodes")
    ahp_codes = parse_error_codes(errors_src, "AhpErrorCodes")

    # Invariants worth failing the build over.
    missing = set(wire_types) - set(dispatchable)
    if missing:
        sys.exit(f"IS_CLIENT_DISPATCHABLE is missing {len(missing)} actions: {sorted(missing)[:5]}")
    missing = set(wire_types) - set(introduced)
    if missing:
        sys.exit(f"ACTION_INTRODUCED_IN is missing {len(missing)} actions: {sorted(missing)[:5]}")
    if supported[0] != current:
        sys.exit("SUPPORTED_PROTOCOL_VERSIONS[0] must equal PROTOCOL_VERSION")

    pin = (ROOT / "vendor" / "upstream" / "PIN.json").read_text(encoding="utf-8")
    spec_tag = re.search(r'"specTag":\s*"([^"]+)"', pin)
    spec_commit = re.search(r'"specCommit":\s*"([^"]+)"', pin)

    body = f'''"""Generated from the vendored upstream TypeScript. DO NOT EDIT.

Regenerate with `python scripts/generate_tables.py`.

Source: {spec_tag.group(1) if spec_tag else "?"} ({spec_commit.group(1) if spec_commit else "?"})
"""

from __future__ import annotations

from typing import Final

#: The protocol version upstream's own source tree declares at the pinned tag.
#: This is NOT what we speak -- see ahp_protocol.types.versions.
UPSTREAM_PROTOCOL_VERSION: Final = {current!r}

#: Every version the upstream client at the pinned tag will negotiate.
UPSTREAM_SUPPORTED_PROTOCOL_VERSIONS: Final[tuple[str, ...]] = {tuple(supported)!r}

#: Every action's wire string, from `export const enum ActionType`.
ACTION_TYPES: Final[frozenset[str]] = frozenset({pyrepr(wire_types)})

#: Whether a client may originate each action. Upstream: "Servers SHOULD call
#: this to validate incoming `dispatchAction` requests and reject any action the
#: client is not allowed to originate." Unknown actions are NOT dispatchable.
IS_CLIENT_DISPATCHABLE: Final[dict[str, bool]] = {pyrepr(dispatchable)}

#: Action -> the protocol version that introduced it. Drives the outbound filter
#: implementing "the host only sends action types known to the negotiated
#: version" (docs/specification/versioning.md).
ACTION_INTRODUCED_IN: Final[dict[str, str]] = {pyrepr(introduced)}

#: Server->client notification method -> the protocol version that introduced
#: it (`ServerNotificationMap` minus `action`, which ACTION_INTRODUCED_IN
#: versions per action type). Upstream's `isNotificationKnownToVersion` is a
#: `<=` compare against the negotiated version; every method here predates the
#: oldest negotiable version at this pin, so the filter only bites after a pin
#: bump lands a newer notification -- vendored now so that bump cannot land
#: without the table.
NOTIFICATION_INTRODUCED_IN: Final[dict[str, str]] = {pyrepr(notification_introduced)}

#: Standard JSON-RPC 2.0 error codes.
JSON_RPC_ERROR_CODES: Final[dict[str, int]] = {pyrepr(jsonrpc_codes)}

#: AHP application error codes. Do not invent parallel ones.
AHP_ERROR_CODES: Final[dict[str, int]] = {pyrepr(ahp_codes)}
'''

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(body, encoding="utf-8")

    print(f"wrote {OUT.relative_to(ROOT)}")
    print(f"  actions:            {len(wire_types)}")
    print(f"  notifications:       {len(notification_introduced)} versioned")
    print(f"  client-dispatchable: {sum(dispatchable.values())}")
    print(f"  upstream version:    {current} (supports {', '.join(supported)})")
    print(f"  error codes:         {len(jsonrpc_codes)} json-rpc + {len(ahp_codes)} ahp")


if __name__ == "__main__":
    main()
