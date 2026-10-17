"""The `flow` command line: run a workflow script against an echo dispatcher."""

import argparse
import asyncio
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from ahp_host_flow.engine import EchoDispatcher, JournalError, RunOptions
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
            "Run a workflow script. agent() calls still go to an echo "
            "dispatcher that returns the prompt. --state-dir journals the run so it "
            "can be resumed."
        ),
    )
    run.add_argument(
        "path",
        nargs="?",
        help="the script to run; optional with --resume, where it replaces the journal's snapshot",
    )
    run.add_argument("--args", help="JSON passed to the script as `args`")
    run.add_argument("--node", help="Node binary (overrides AHP_FLOW_NODE and PATH)")
    run.add_argument("--concurrency", type=int, help="most agent() calls in flight at once")
    run.add_argument("--budget", type=int, help="output-token ceiling for the run")
    run.add_argument("--timeout", type=float, help="wall-clock limit for the run, in seconds")
    run.add_argument(
        "--workflows",
        action="append",
        default=[],
        help="directory searched by workflow(name); repeatable, searched in order",
    )
    run.add_argument(
        "--root", default=".", help="base directory for workflow({scriptPath}) (default: .)"
    )
    run.add_argument("--state-dir", help="directory for run journals; enables journaling")
    run.add_argument("--run-id", help="id for this run (default: generated)")
    run.add_argument(
        "--resume",
        metavar="RUN_ID",
        help="resume an earlier run from its journal: unchanged agent() calls are served from it",
    )
    return parser


def _stderr(line: str) -> None:
    print(line, file=sys.stderr)


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.path is None and args.resume is None:
        parser.error("a script path is needed unless --resume is given")
    if args.resume is not None and (args.args is not None or args.budget is not None):
        parser.error("--args and --budget come from the journal with --resume")

    script_args: Any = NO_ARGS
    if args.args is not None:
        try:
            script_args = json.loads(args.args)
        except json.JSONDecodeError as exc:
            parser.error(f"--args is not valid JSON: {exc}")

    try:
        options = RunOptions(
            concurrency=args.concurrency,
            budget_total=args.budget,
            timeout=args.timeout,
            workflow_dirs=tuple(Path(d).resolve() for d in args.workflows),
            run_root=Path(args.root).resolve(),
            state_dir=Path(args.state_dir).resolve() if args.state_dir else None,
            run_id=args.run_id,
            resume_from=args.resume,
        )
    except ValueError as exc:
        parser.error(str(exc))

    source: str | None = None
    filename = "workflow.js"
    if args.path is not None:
        path = Path(args.path)
        if not path.is_file():
            _stderr(f"flow: no such file: {path}")
            return 2
        source = path.read_text(encoding="utf-8")
        filename = str(path)

    node = find_node(args.node)
    if node is None:
        _stderr(f"flow: {NodeNotFoundError()}")
        return 1

    try:
        value = asyncio.run(
            run_script(
                source,
                node=node,
                dispatcher=EchoDispatcher(),
                args=script_args,
                options=options,
                filename=filename,
                on_log=lambda message: _stderr(f"[log] {message}"),
                on_phase=lambda title: _stderr(f"[phase] {title}"),
                on_run_start=lambda run_id: _stderr(f"[run] {run_id}"),
            )
        )
    except (SandboxError, JournalError, ValueError) as exc:
        _stderr(f"flow: {exc}")
        return 1
    print(json.dumps(value, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
