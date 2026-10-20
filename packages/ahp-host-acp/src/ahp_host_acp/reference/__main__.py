"""`ahp-acp-openai` / `python -m ahp_host_acp.reference`: the reference ACP agent over stdio."""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Sequence

from ahp_host_acp.reference.agent import Agent, ConfigError, from_env

EPILOG = """\
The agent is configured from the environment only (the API key has no option):
  AHP_OPENAI_BASE_URL   endpoint root, e.g. http://127.0.0.1:11434/v1 (required)
  AHP_OPENAI_MODEL      model name (required)
  AHP_OPENAI_API_KEY    bearer token (optional)
  AHP_OPENAI_TIMEOUT    seconds a model request may stay silent (default 300)
  AHP_OPENAI_MAX_STEPS  model requests per prompt (default 25)
  AHP_OPENAI_SYSTEM     system prompt; {cwd} is the session folder (optional)
"""


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="ahp-acp-openai",
        description="An ACP agent over stdio that forwards each turn to an OpenAI-compatible "
        "chat-completions endpoint.",
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.parse_args(argv)
    try:
        config = from_env(os.environ)
    except ConfigError as exc:
        print(f"ahp-acp-openai: {exc}", file=sys.stderr)
        return 2
    return Agent(
        config, stdin=sys.stdin.buffer, stdout=sys.stdout.buffer, stderr=sys.stderr
    ).serve()


if __name__ == "__main__":
    raise SystemExit(main())
