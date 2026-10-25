"""`workflow({scriptPath})` from an untrusted caller: `.js` runs in the sandbox, `.py` is refused.

A client-published workflow runs with `python_dirs=()` (no `.py` trusted), as the provider
runs it. A `.js` under the run root still loads: it runs in the same Node sandbox, with the
same verbs and caps, as the caller's own script, so it grants nothing the caller lacks (plan
§4.3: `workflow({scriptPath})` is a path in the session's working directories). A `.py` is
arbitrary host code (plan §4.10) and is refused; so is any path outside the run root.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ahp_host_flow.engine import ClientWorkflow, RunOptions
from ahp_host_flow.sandbox import run_script

from ..fakes import FakeDispatcher  # noqa: TID252

CLIENT = (
    "const js = await workflow({scriptPath: 'tools/child.js'}, {n: 1})\n"
    "let py\n"
    "try { await workflow({scriptPath: 'tools/child.py'}) } catch (e) { py = e.message }\n"
    "let out\n"
    "try { await workflow({scriptPath: '../outside.js'}) } catch (e) { out = e.message }\n"
    "return [js, py, out]\n"
)


@pytest.mark.node
async def test_scriptpath_trust_untrusted_caller(node_path: str, tmp_path: Path) -> None:
    root = tmp_path / "proj"
    (root / "tools").mkdir(parents=True)
    (root / "tools" / "child.js").write_text(
        "return {n: args.n + 1, reach: [typeof process, typeof require].join()}\n",
        encoding="utf-8",
    )
    (root / "tools" / "child.py").write_text("async def main(args):\n    return 1\n")
    (tmp_path / "outside.js").write_text("return 'outside'\n", encoding="utf-8")
    filename = "ahp-flow-client://example.com/c.js"
    options = RunOptions(
        run_root=root,
        python_dirs=(),
        client_workflows=(ClientWorkflow("c", CLIENT, filename),),
        timeout=60,
    )
    js, py, out = await run_script(
        CLIENT, node=node_path, dispatcher=FakeDispatcher(), filename=filename, options=options
    )
    assert js == {"n": 2, "reach": "undefined,undefined"}
    assert "Python workflows are not allowed" in py
    assert "outside the run root" in out
