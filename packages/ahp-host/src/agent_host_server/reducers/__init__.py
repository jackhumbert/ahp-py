"""Pure state reducers, hand-ported from the upstream TypeScript.

Empty until step 2 of the build order (docs/plan.md §11). The port is gated on
the vendored 247-fixture corpus; see ADR 0004 for why it is all-or-nothing per
channel rather than incremental.
"""

from __future__ import annotations
