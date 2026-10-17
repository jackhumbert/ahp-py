"""Workflow discovery across sources, and how it is published as customizations (plan §4.3)."""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final
from urllib.parse import urlparse
from urllib.request import url2pathname

from ahp_host_flow.config import SOURCES
from ahp_host_flow.engine import WORKFLOW_NAME
from ahp_host_flow.sandbox import Sandbox, SandboxError

FLOW_META_KEY: Final = "ahp-py/flow"
PLUGIN_ID: Final = "ahp-host-flow.host"
USER_ID: Final = "ahp-host-flow.user"

_logger = logging.getLogger(__name__)


def directory_of(value: str) -> Path | None:
    """A working directory given as a `file:` URI or a plain path; None for any other scheme."""
    parsed = urlparse(value)
    if parsed.scheme == "file":
        if parsed.netloc in ("", "localhost"):
            return Path(url2pathname(parsed.path))
        return None
    # A one-character scheme is a Windows drive letter.
    if len(parsed.scheme) > 1:
        return None
    return Path(value)


@dataclass(frozen=True)
class SourceDir:
    source: str
    path: Path
    container_id: str
    label: str


def source_dirs(
    *,
    working_directories: Sequence[Path],
    user_dir: Path | None,
    host_dirs: Sequence[Path],
    sources: Sequence[str],
) -> list[SourceDir]:
    """The directories to scan, in precedence order."""
    dirs: list[SourceDir] = []
    if "workspace" in sources:
        for index, wd in enumerate(working_directories):
            dirs.append(
                SourceDir(
                    "workspace",
                    wd / ".claude" / "workflows",
                    f"ahp-host-flow.workspace.{index}",
                    f"Workflows ({wd.name})",
                )
            )
    if "user" in sources and user_dir is not None:
        dirs.append(SourceDir("user", user_dir, USER_ID, "Workflows (user)"))
    if "host" in sources:
        dirs.extend(SourceDir("host", path, PLUGIN_ID, "ahp-host-flow") for path in host_dirs)
    return dirs


@dataclass(frozen=True)
class WorkflowEntry:
    name: str
    path: Path
    source: str
    container_id: str
    meta: Mapping[str, Any] | None
    shadowed_by: str | None


async def discover(dirs: Sequence[SourceDir], *, node: str | None) -> list[WorkflowEntry]:
    """Every workflow in `dirs`, in order; `meta` is parsed from the source, never run."""
    found: list[tuple[SourceDir, Path, str]] = []
    for source_dir in dirs:
        if not source_dir.path.is_dir():
            continue
        for path in sorted(source_dir.path.glob("*.js")):
            if not WORKFLOW_NAME.fullmatch(path.stem):
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            found.append((source_dir, path, text))

    metas: dict[Path, Mapping[str, Any] | None] = {}
    if node is not None and found:
        async with Sandbox(node) as sandbox:
            for _, path, text in found:
                try:
                    metas[path] = await sandbox.meta(text, filename=str(path))
                except SandboxError as exc:
                    _logger.warning("cannot read meta of %s: %s", path, exc)

    entries: list[WorkflowEntry] = []
    first_source: dict[str, str] = {}
    for source_dir, path, _ in found:
        name = path.stem
        entries.append(
            WorkflowEntry(
                name=name,
                path=path,
                source=source_dir.source,
                container_id=source_dir.container_id,
                meta=metas.get(path),
                shadowed_by=first_source.get(name),
            )
        )
        first_source.setdefault(name, source_dir.source)
    return entries


def resolve(entries: Sequence[WorkflowEntry], ref: str) -> WorkflowEntry | None:
    """`source:name` picks that source; a bare name takes the first match."""
    source, sep, name = ref.partition(":")
    if sep and source in SOURCES:
        return next((e for e in entries if e.source == source and e.name == name), None)
    return next((e for e in entries if e.name == ref), None)


def visible_names(entries: Sequence[WorkflowEntry]) -> list[str]:
    return sorted({e.name for e in entries})


def _description(meta: Mapping[str, Any] | None) -> str:
    if meta is None:
        return ""
    for key in ("whenToUse", "description"):
        value = meta.get(key)
        if isinstance(value, str):
            return value
    return ""


def _child(entry: WorkflowEntry) -> dict[str, Any]:
    phases = entry.meta.get("phases") if entry.meta is not None else None
    flow: dict[str, Any] = {
        "kind": "workflow",
        "phases": phases if isinstance(phases, list) else [],
        "source": entry.source,
    }
    if entry.shadowed_by is not None:
        flow["shadowedBy"] = entry.shadowed_by
    return {
        "type": "prompt",
        "id": f"{entry.container_id}.{entry.name}",
        "uri": entry.path.as_uri(),
        "name": entry.name,
        "description": _description(entry.meta),
        "enabled": True,
        "_meta": {FLOW_META_KEY: flow},
    }


def customizations(
    entries: Sequence[WorkflowEntry], dirs: Sequence[SourceDir], *, version: str
) -> list[dict[str, Any]]:
    """One container per existing source directory; host directories share one plugin."""
    result: list[dict[str, Any]] = []
    plugin: dict[str, Any] | None = None
    plugin_names: set[str] = set()
    for source_dir in dirs:
        if not source_dir.path.is_dir():
            continue
        children: list[dict[str, Any]] = []
        for entry in entries:
            if entry.container_id != source_dir.container_id:
                continue
            if entry.path.parent != source_dir.path:
                continue
            if source_dir.source == "host":
                if entry.name in plugin_names:
                    continue
                plugin_names.add(entry.name)
            children.append(_child(entry))
        if source_dir.source == "host":
            if plugin is None:
                plugin = {
                    "type": "plugin",
                    "id": PLUGIN_ID,
                    "uri": source_dir.path.as_uri(),
                    "name": "ahp-host-flow",
                    "version": version,
                    "children": [],
                }
                result.append(plugin)
            plugin["children"].extend(children)
            continue
        result.append(
            {
                "type": "directory",
                "id": source_dir.container_id,
                "uri": source_dir.path.as_uri(),
                "name": source_dir.label,
                "contents": "prompt",
                "writable": False,
                "enabled": True,
                "children": children,
            }
        )
    return result
