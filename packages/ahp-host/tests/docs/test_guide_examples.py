"""Every Python block in the guide is executed here.

Documentation that CAN drift DOES drift. This project has already shipped a
comment claiming VS Code hides the Tools section for non-copilotcli providers
(real code, wrong path), a demo tree pointing at files that did not exist, and
a docstring saying a value was "a path, not a URI" while the host published it
into a field the spec declares a URI. Prose is not checked by anything, so it
rots quietly and then misleads someone.

So the guide's examples are not illustrations, they are tests. A block that
stops working fails here. A block that describes a default which changes fails
here. If you edit `docs/guide/`, this suite tells you whether you were right.

Blocks tagged ```text or ```console are prose and are skipped; everything
tagged ```python must run.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

GUIDE = Path(__file__).resolve().parents[2] / "docs" / "guide"

#: ```python fences only. A block may depend on names defined by earlier blocks
#: in the same file -- they share one namespace, in document order, which is
#: how a reader reads them.
_BLOCK = re.compile(r"^```python\n(.*?)^```", re.MULTILINE | re.DOTALL)


def _pages() -> list[Path]:
    return sorted(GUIDE.glob("*.md"))


def test_the_guide_exists() -> None:
    """A missing guide is the failure mode this whole file guards against."""
    assert _pages(), "docs/guide/ has no pages"


@pytest.mark.parametrize("page", _pages(), ids=lambda p: p.name)
def test_every_python_block_runs(page: Path) -> None:
    blocks = _BLOCK.findall(page.read_text())
    assert blocks, f"{page.name} has no executable examples"

    # One namespace per page, in document order: a later block may use a class
    # an earlier one defined, exactly as a reader would expect.
    namespace: dict[str, object] = {"__name__": f"guide_{page.stem}"}
    for index, source in enumerate(blocks, start=1):
        try:
            exec(compile(source, f"{page.name}#block{index}", "exec"), namespace)
        except Exception as failure:  # pragma: no cover - the message is the point
            raise AssertionError(
                f"{page.name} block {index} does not work:\n\n{source}\n{failure!r}"
            ) from failure
