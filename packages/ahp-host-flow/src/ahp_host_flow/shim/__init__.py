"""The dual-target shim, shipped with the package: `flow-shim.js` beside this module.

Paste it right after `export const meta = {...}` to run one script under both Claude
Code and ahp-host-flow (docs/format.md, "The shim"). `flow shim` prints it. The copy at
the repository's `shim/flow-shim.js` is the same file, kept for readers of the source
tree; a test keeps the two identical. Standard library only.
"""

from __future__ import annotations

from importlib import resources
from typing import Final

__all__ = ["SHIM_NAME", "source"]

SHIM_NAME: Final = "flow-shim.js"


def source() -> str:
    """The shim's text, exactly as shipped."""
    return resources.files(__name__).joinpath(SHIM_NAME).read_text(encoding="utf-8")
