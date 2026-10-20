"""A small reference ACP *agent* (server side) that talks to an OpenAI-compatible endpoint.

It is the non-Claude agent the support matrix is checked against: `python -m
ahp_host_acp.reference` serves ACP over stdio and forwards each turn to a
`/v1/chat/completions` endpoint. Every module here imports only the standard library and
`ahp_host_acp.reference.*`, because the agent runs inside whatever environment launches it.
`python -m` still loads `ahp_host_acp/__init__` first (unavoidable, as for `relay.py`).
"""

from __future__ import annotations

import sys
from typing import Final

#: The module `python -m` runs.
REFERENCE_MODULE: Final = "ahp_host_acp.reference"


def command() -> tuple[str, ...]:
    """The argv that launches the reference agent with this interpreter."""
    return (sys.executable, "-m", REFERENCE_MODULE)
