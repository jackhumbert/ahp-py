"""One decision about whether the sibling host is importable, made once.

Four modules in this suite drive the client against the real
`ahp-host` in-process, and each used to decide for itself whether it
could -- two of them with a bare `importorskip`. That makes a CI job whose
cross-repo install silently failed report exactly what a healthy one reports:
green, with the interesting tests folded invisibly into the skip count.

`AHP_INTEROP_REQUIRED` is the same switch the sibling host's own interop suite
uses. CI sets it, in the job where the host *is* installed, so a missing
sibling fails loudly at collection instead of vanishing.

The env var asserts that the **install** worked, not that the platform can do
everything: `requires_sibling_pty` stays a plain skip, because a machine
without a POSIX pty is a fact about the machine, not a broken setup.
"""

from __future__ import annotations

import importlib
import importlib.util
import os

import pytest

SETUP = "needs the sibling host: pip install -e ../ahp-host"

_AVAILABLE = importlib.util.find_spec("ahp_host") is not None

if os.environ.get("AHP_INTEROP_REQUIRED") and not _AVAILABLE:
    raise RuntimeError(f"AHP_INTEROP_REQUIRED is set but the host is missing: {SETUP}")


def _pty_available() -> bool:
    """Import it rather than locate it: on Windows the module exists and raises."""
    if not _AVAILABLE:
        return False
    try:
        importlib.import_module("ahp_host.core.pty_backend")
    except Exception:
        return False
    return True


requires_sibling_host = pytest.mark.skipif(not _AVAILABLE, reason=SETUP)
requires_sibling_pty = pytest.mark.skipif(
    not _pty_available(), reason=f"{SETUP} -- and a POSIX pty"
)
