"""Namespaced ``InitializeResult._meta`` keys that specific clients read.

These are not spec fields: a client defines them, and a client that does not
know a key ignores it.
"""

from __future__ import annotations

from typing import Final

__all__ = ["AUTONOMOUS_AUTOMATIONS_META_KEY"]

#: The VS Code client treats an ``automations`` capability as usable only when
#: ``InitializeResult._meta`` carries this key set to ``true``. The value says
#: the host runs scheduled automations itself, with no client-side activation.
#: Without it, the client reports the host as needing an update.
AUTONOMOUS_AUTOMATIONS_META_KEY: Final = "vscode.autonomousAutomations"
