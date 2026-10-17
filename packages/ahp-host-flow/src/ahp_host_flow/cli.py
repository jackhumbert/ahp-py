"""The `flow` command line: run a workflow script against an echo dispatcher."""

import argparse
import asyncio
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from ahp_host_flow.engine import EchoDispatcher
from ahp_host_flow.sandbox import (
    NO_ARGS,
    NodeNotFoundError,
    SandboxError,
    find_node,
    run_script,
)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="flow", description="Run workflow scripts.")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser(
        "run",
        help="run a script and print its return value as JSON",
        description=(
            "Run a workflow script. M0 runs every agent() call against an echo "
            "dispatcher that returns the prompt."
        ),
    )
    run.add_argument("path", help="the script to run")
    run.add_argument("--args", help="JSON passed to the script as `args`")
    run.add_argument("--node", help="Node binary (overrides AHP_FLOW_NODE and PATH)")
    return parser


def _stderr(line: str) -> None:
    print(line, file=sys.stderr)


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    script_args: Any = NO_ARGS
    if args.args is not None:
        try:
            script_args = json.loads(args.args)
        except json.JSONDecodeError as exc:
            parser.error(f"--args is not valid JSON: {exc}")

    path = Path(args.path)
    if not path.is_file():
        _stderr(f"flow: no such file: {path}")
        return 2

    node = find_node(args.node)
    if node is None:
        _stderr(f"flow: {NodeNotFoundError()}")
        return 1

    source = path.read_text(encoding="utf-8")
    try:
        value = asyncio.run(
            run_script(
                source,
                node=node,
                dispatcher=EchoDispatcher(),
                args=script_args,
                filename=str(path),
                on_log=lambda message: _stderr(f"[log] {message}"),
                on_phase=lambda title: _stderr(f"[phase] {title}"),
            )
        )
    except SandboxError as exc:
        _stderr(f"flow: {exc}")
        return 1
    print(json.dumps(value, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
