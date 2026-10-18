"""Workflow lookup for workflow(): names over ordered directories, or a path in a run root."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

# No dots, slashes or leading dash: a name can never traverse directories.
WORKFLOW_NAME: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*")
# Precedence order, highest first (plan §4.3); `config.SOURCES` is this tuple.
WORKFLOW_SOURCES: Final = ("client", "workspace", "user", "host")
# JS first: it wins a tie within one directory.
WORKFLOW_SUFFIXES: Final = (".js", ".py")


class WorkflowRefError(ValueError):
    """A workflow() reference that cannot be resolved; the message is what the script sees."""


@dataclass(frozen=True)
class ResolvedScript:
    source: str
    filename: str  # str(path), used for stack traces


@dataclass(frozen=True)
class WorkflowDir:
    """A workflow directory labeled with the source it came from (`workflow('host:x')`)."""

    source: str
    path: Path


@dataclass(frozen=True)
class ClientWorkflow:
    """A workflow a client published: its script text and the URI it lives at."""

    name: str
    source: str
    filename: str


def _invalid_name(name: str) -> WorkflowRefError:
    return WorkflowRefError(f"invalid workflow name: {name!r}")


def python_allowed(path: Path, python_dirs: Sequence[Path] | None) -> bool:
    """Whether a `.py` workflow at `path` may load; `None` allows any."""
    if python_dirs is None:
        return True
    resolved = path.resolve()
    return any(resolved.is_relative_to(d.resolve()) for d in python_dirs)


def _split(ref: str) -> tuple[str | None, str]:
    """`(source, name)` of a reference; the source is None for a bare name."""
    source, sep, name = ref.partition(":")
    if not sep:
        if not WORKFLOW_NAME.fullmatch(ref):
            raise _invalid_name(ref)
        return None, ref
    if source not in WORKFLOW_SOURCES:
        raise WorkflowRefError(
            f"unknown workflow source {source!r} in {ref!r}: use one of "
            + ", ".join(WORKFLOW_SOURCES)
        )
    if not WORKFLOW_NAME.fullmatch(name):
        raise _invalid_name(ref)
    return source, name


class Registry:
    """Ordered directories holding `<name>.js` or `<name>.py` workflows, and client workflows.

    A client workflow wins over any directory for a bare name; then the first directory with
    either file wins, and `.js` wins a tie within one directory (Claude Code's behaviour does
    not change). `source:name` reaches only that source's entries; an unlabeled directory (a
    bare Path) serves bare names only. `.py` files load only from `python_dirs` (None: any),
    and a client never supplies one.
    """

    def __init__(
        self,
        dirs: Sequence[Path | WorkflowDir],
        *,
        python_dirs: Sequence[Path] | None = None,
        client: Sequence[ClientWorkflow] = (),
    ) -> None:
        self._entries = tuple(d if isinstance(d, WorkflowDir) else WorkflowDir("", d) for d in dirs)
        self._python_dirs = None if python_dirs is None else tuple(python_dirs)
        self._client = tuple(c for c in client if c.filename.endswith(".js"))

    @property
    def dirs(self) -> tuple[Path, ...]:
        return tuple(d.path for d in self._entries)

    def find_client(self, ref: str) -> ClientWorkflow | None:
        source, name = _split(ref)
        if source not in (None, "client"):
            return None
        return next((c for c in self._client if c.name == name), None)

    def find(self, ref: str) -> Path | None:
        source, name = _split(ref)
        for d in self._entries:
            if source is not None and d.source != source:
                continue
            for suffix in WORKFLOW_SUFFIXES:
                path = d.path / f"{name}{suffix}"
                if suffix == ".py" and not python_allowed(path, self._python_dirs):
                    continue
                if path.is_file():
                    return path
        return None

    def names(self) -> list[str]:
        found: set[str] = {c.name for c in self._client}
        for d in self._entries:
            if not d.path.is_dir():
                continue
            for suffix in WORKFLOW_SUFFIXES:
                for path in d.path.glob(f"*{suffix}"):
                    if not WORKFLOW_NAME.fullmatch(path.stem):
                        continue
                    if suffix == ".py" and not python_allowed(path, self._python_dirs):
                        continue
                    found.add(path.stem)
        return sorted(found)


def resolve_workflow(
    ref: object,
    *,
    registry: Registry | None,
    run_root: Path | None,
    python_dirs: Sequence[Path] | None = None,
) -> ResolvedScript:
    """Turn a workflow() argument into script source, or raise WorkflowRefError."""
    display: str
    path: Path | None
    if isinstance(ref, str):
        display = ref
        if registry is None:
            _split(ref)
            path = None
        else:
            entry = registry.find_client(ref)
            if entry is not None:
                return ResolvedScript(entry.source, entry.filename)
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
        if path.suffix.lower() == ".py" and not python_allowed(path, python_dirs):
            raise WorkflowRefError(f"Python workflows are not allowed from {display!r}")
    else:
        raise WorkflowRefError("workflow() needs a name or {scriptPath}")
    try:
        source = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise WorkflowRefError(f"cannot read workflow {display!r}: {exc}") from exc
    return ResolvedScript(source, str(path))
