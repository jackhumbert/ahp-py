"""Workflow lookup for workflow(): names over ordered directories, or a path in a run root."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

# No dots, slashes or leading dash: a name can never traverse directories.
WORKFLOW_NAME: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*")


class WorkflowRefError(ValueError):
    """A workflow() reference that cannot be resolved; the message is what the script sees."""


@dataclass(frozen=True)
class ResolvedScript:
    source: str
    filename: str  # str(path), used for stack traces


def _invalid_name(name: str) -> WorkflowRefError:
    return WorkflowRefError(f"invalid workflow name: {name!r}")


class Registry:
    """Ordered list of directories holding `<name>.js` workflows; first match wins."""

    def __init__(self, dirs: Sequence[Path]) -> None:
        self._dirs = tuple(dirs)

    @property
    def dirs(self) -> tuple[Path, ...]:
        return self._dirs

    def find(self, name: str) -> Path | None:
        if not WORKFLOW_NAME.fullmatch(name):
            raise _invalid_name(name)
        for d in self._dirs:
            path = d / f"{name}.js"
            if path.is_file():
                return path
        return None

    def names(self) -> list[str]:
        found: set[str] = set()
        for d in self._dirs:
            if not d.is_dir():
                continue
            for path in d.glob("*.js"):
                if WORKFLOW_NAME.fullmatch(path.stem):
                    found.add(path.stem)
        return sorted(found)


def resolve_workflow(
    ref: object, *, registry: Registry | None, run_root: Path | None
) -> ResolvedScript:
    """Turn a workflow() argument into script source, or raise WorkflowRefError."""
    display: str
    path: Path | None
    if isinstance(ref, str):
        display = ref
        if registry is None:
            if not WORKFLOW_NAME.fullmatch(ref):
                raise _invalid_name(ref)
            path = None
        else:
            path = registry.find(ref)
        if path is None:
            raise WorkflowRefError(f"unknown workflow: {ref!r}")
    elif isinstance(ref, Mapping) and isinstance(ref.get("scriptPath"), str):
        display = ref["scriptPath"]
        if run_root is None:
            raise WorkflowRefError("workflow({scriptPath}) needs a run root")
        root = run_root.resolve()
        path = (root / display).resolve()
        if not path.is_relative_to(root):
            raise WorkflowRefError(f"workflow path is outside the run root: {display!r}")
    else:
        raise WorkflowRefError("workflow() needs a name or {scriptPath}")
    try:
        source = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise WorkflowRefError(f"cannot read workflow {display!r}: {exc}") from exc
    return ResolvedScript(source, str(path))
