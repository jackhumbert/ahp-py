"""The `flow` command line: run a workflow script against an echo dispatcher."""

import argparse
import asyncio
import dataclasses
import json
import sys
import time
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ahp_host.core.automations import iso

from ahp_host_flow import redaction, shim
from ahp_host_flow.config import ConfigError, load_config
from ahp_host_flow.engine import (
    EchoDispatcher,
    JournalError,
    RunOptions,
    load_journal,
    redact_agent_record,
)
from ahp_host_flow.inprocess import FlowRunFailed, run_in_process
from ahp_host_flow.sandbox import (
    NO_ARGS,
    NodeNotFoundError,
    SandboxError,
    find_node,
    language_of,
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
        help=(
            "the .js or .py script to run; optional with --resume, "
            "where it replaces the journal's snapshot"
        ),
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
    run.add_argument(
        "--config",
        metavar="PATH",
        help=(
            "a flow.toml: run the script through its providers on an in-process host "
            "(concurrency, budget, timeout, workflows and state come from it)"
        ),
    )
    sub.add_parser(
        "shim",
        help="print shim/flow-shim.js, the dual-target shim to paste after a script's meta",
    )
    redact = sub.add_parser(
        "redact",
        help="redact one finished agent() call from a run's journal (and a host's shared view)",
        description=(
            "Redact call ORDINAL of run RUN_ID. --state-dir rewrites the journal only; "
            "--config also asks the host serving that flow.toml to redact the shared view "
            "(through <state_dir>/redact, which its poller works off). The inner agent's own "
            "context is not redacted. A run a `flow run` process is still writing is refused (the "
            "journal is locked while its writer is open)."
        ),
    )
    redact.add_argument("run_id", help="the run's id")
    redact.add_argument("ordinal", type=int, help="the agent() call's ordinal")
    redact.add_argument("--reason", default="redacted by an operator", help="recorded in the audit")
    where = redact.add_mutually_exclusive_group(required=True)
    where.add_argument("--config", metavar="PATH", help="a flow.toml whose running host to ask")
    where.add_argument("--state-dir", help="redact the journal only, with no host")
    redact.add_argument(
        "--wait",
        type=float,
        help="with --config: seconds to wait for the host's answer (default: 10)",
    )
    return parser


def _stderr(line: str) -> None:
    print(line, file=sys.stderr)


_CONFIG_EXCLUSIVE = (
    ("--concurrency", "concurrency"),
    ("--budget", "budget"),
    ("--timeout", "timeout"),
    ("--workflows", "workflows"),
    ("--state-dir", "state_dir"),
    ("--run-id", "run_id"),
    ("--resume", "resume"),
)


def _run_with_config(parser: argparse.ArgumentParser, args: argparse.Namespace) -> int:
    for flag, dest in _CONFIG_EXCLUSIVE:
        value = getattr(args, dest)
        # `--budget 0` and `--timeout 0` are values; `--workflows` is a list that is empty unset.
        if value or (value is not None and not isinstance(value, list)):
            parser.error(f"{flag} cannot be combined with --config")
    if args.path is None:
        parser.error("a script path is needed with --config")
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
    try:
        config = load_config(Path(args.config))
    except ConfigError as exc:
        _stderr(f"flow: {exc}")
        return 1
    if args.node is not None:
        config = dataclasses.replace(config, node=args.node)
    try:
        value = asyncio.run(
            run_in_process(
                config,
                path.read_text(encoding="utf-8"),
                args=script_args,
                root=Path(args.root),
                filename=path.name,
                on_note=lambda note: _stderr(f"[flow] {note}"),
            )
        )
    except FlowRunFailed as exc:
        _stderr(f"flow: {exc}")
        return 1
    print(json.dumps(value, indent=2))
    return 0


def _redact_journal(args: argparse.Namespace) -> int:
    state_dir = Path(args.state_dir)
    try:
        redact_agent_record(
            state_dir,
            args.run_id,
            args.ordinal,
            reason=args.reason,
            redacted_at=iso(datetime.now(UTC)),
        )
    except (JournalError, ValueError) as exc:
        _stderr(f"flow: {exc}")
        return 1
    report = {
        "ok": True,
        "runId": args.run_id,
        "ordinal": args.ordinal,
        "journal": True,
        "sharedView": False,
        "descendants": list(redaction.descendants(state_dir, args.run_id)),
    }
    print(json.dumps(report, indent=2))
    _stderr("flow: the journal is redacted; a running host's shared view is redacted with --config")
    return 0


def _redact_through_host(args: argparse.Namespace) -> int:
    try:
        config = load_config(Path(args.config))
    except ConfigError as exc:
        _stderr(f"flow: {exc}")
        return 1
    try:
        request = redaction.queue_request(config.state_dir, args.run_id, args.ordinal, args.reason)
    except ValueError as exc:
        _stderr(f"flow: {exc}")
        return 1
    result = redaction.result_path(request)
    deadline = time.monotonic() + args.wait
    while not result.exists():
        if time.monotonic() >= deadline:
            _stderr(
                "flow: no running host has picked up the request yet; "
                f"it stays queued at {request} and is applied when the host next starts"
            )
            return 3
        time.sleep(0.1)
    body = json.loads(result.read_text(encoding="utf-8"))
    print(json.dumps(body, indent=2))
    if body.get("ok") is True:
        return 0
    _stderr(f"flow: {body.get('error', 'redaction failed')}")
    done = body.get("done")
    if isinstance(done, list) and done:
        _stderr("flow: already redacted: " + "; ".join(str(d) for d in done))
        _stderr("flow: run the same command again to finish it")
    return 1


def _redact(parser: argparse.ArgumentParser, args: argparse.Namespace) -> int:
    if args.state_dir is not None:
        if args.wait is not None:
            parser.error("--wait only applies with --config")
        return _redact_journal(args)
    if args.wait is None:
        args.wait = 10.0
    return _redact_through_host(args)


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command == "redact":
        return _redact(parser, args)

    if args.command == "shim":
        sys.stdout.write(shim.source())
        return 0

    if args.config is not None:
        return _run_with_config(parser, args)

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
    script_name = args.path
    if script_name is None:
        try:
            script_name = load_journal(Path(args.state_dir), args.resume).header.filename
        except JournalError as exc:
            _stderr(f"flow: {exc}")
            return 1
    if node is None and language_of(str(script_name)) == "js":
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
