"""Workflow discovery across sources, and how it is published as customizations (plan §4.3)."""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Final, Protocol
from urllib.parse import urlparse
from urllib.request import url2pathname
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ahp_host.core.automations import CronExpression
from ahp_protocol.ext.flow import FLOW_META_KEY as FLOW_META_KEY

from ahp_host_flow.config import PYTHON_SOURCES_DEFAULT, SOURCES
from ahp_host_flow.engine import WORKFLOW_NAME, args_schema_error
from ahp_host_flow.sandbox import Sandbox, SandboxError, pyrunner

PLUGIN_ID: Final = "ahp-host-flow.host"
USER_ID: Final = "ahp-host-flow.user"
CLIENT_PREFIX: Final = "ahp-host-flow.client."
# A client is untrusted: bound what it can make the host read (as ahp_host's plugin copies do).
MAX_CLIENT_WORKFLOWS: Final = 200
MAX_CLIENT_WORKFLOW_BYTES: Final = 1024 * 1024

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


def python_dirs(dirs: Sequence[SourceDir], python_sources: Sequence[str]) -> tuple[Path, ...]:
    """The directories `.py` workflows may load from: those of `dirs` whose source is trusted.

    `dirs` holds only the sources `[workflows] sources` enables, so listing `workspace` in
    `python_sources` grants nothing while it is excluded there, and for the workspace it
    grants `.claude/workflows` only, never the working directory around it.
    """
    return tuple(d.path for d in dirs if d.source in python_sources and d.path.is_dir())


@dataclass(frozen=True)
class WorkflowEntry:
    name: str
    path: Path
    source: str
    container_id: str
    meta: Mapping[str, Any] | None
    shadowed_by: str | None
    # 'js' or 'python'.
    language: str = "js"
    # A client workflow lives at `uri` and is held in `text`; `path` is only its file name.
    uri: str | None = None
    text: str | None = None

    def location(self) -> str:
        return self.uri if self.uri is not None else self.path.as_uri()

    def filename(self) -> str:
        return self.uri if self.uri is not None else str(self.path)

    def source_text(self) -> str:
        if self.text is not None:
            return self.text
        return self.path.read_text(encoding="utf-8")


class PluginReader(Protocol):
    """Reads a plugin published into the session (ahp_host's `ReadsClientPlugins` satisfies it)."""

    async def list_plugin_resource(self, uri: str) -> Sequence[Mapping[str, Any]]: ...

    async def read_plugin_resource(self, uri: str) -> bytes: ...


@dataclass(frozen=True)
class ClientPlugin:
    plugin_id: str
    uri: str
    name: str
    nonce: str | None = None
    client_id: str | None = None

    @property
    def container_id(self) -> str:
        return f"{CLIENT_PREFIX}{self.plugin_id}"

    @property
    def workflows_uri(self) -> str:
        return f"{self.uri.rstrip('/')}/workflows"


def client_plugins(published: Sequence[Mapping[str, Any]]) -> list[ClientPlugin]:
    """The plugins a client published, from `ReadsClientPlugins.session_plugins()`."""
    result: list[ClientPlugin] = []
    seen: set[str] = set()
    for item in published:
        plugin_id, uri, name = item.get("id"), item.get("uri"), item.get("name")
        if not (isinstance(plugin_id, str) and isinstance(uri, str) and isinstance(name, str)):
            continue
        if plugin_id in seen:
            continue
        seen.add(plugin_id)
        nonce, client_id = item.get("nonce"), item.get("clientId")
        result.append(
            ClientPlugin(
                plugin_id,
                uri,
                name,
                nonce if isinstance(nonce, str) else None,
                client_id if isinstance(client_id, str) else None,
            )
        )
    return result


async def read_client_workflows(
    plugin: ClientPlugin, reader: PluginReader, *, node: str | None
) -> list[WorkflowEntry]:
    """The `.js` workflows in a client plugin's `workflows/`; a `.py` is never read (plan §4.10).

    `ReadsClientPlugins.read_plugin_resource` returns the whole file, so
    `MAX_CLIENT_WORKFLOW_BYTES` bounds what is kept, not what the client sends; the file count is
    bounded before any read.
    """
    try:
        listing = await reader.list_plugin_resource(plugin.workflows_uri)
    except Exception:
        _logger.debug("cannot list %s", plugin.workflows_uri, exc_info=True)
        return []
    names = sorted(
        item["name"]
        for item in listing
        if isinstance(item, Mapping)
        and isinstance(item.get("name"), str)
        and item.get("type") != "directory"
    )
    texts: dict[str, str] = {}
    for name in names:
        if name.endswith(".py"):
            _logger.info("%s: %s skipped: a client cannot supply a .py workflow", plugin.name, name)
            continue
        # A listed name is one file directly under `workflows/`: a separator or `..` would
        # make the read URI point somewhere else in (or out of) the client's plugin.
        if "/" in name or "\\" in name or name != Path(name).name or name in {".", ".."}:
            _logger.warning("%s: %r skipped: not a plain file name", plugin.name, name)
            continue
        path = Path(name)
        if path.suffix != ".js" or not WORKFLOW_NAME.fullmatch(path.stem):
            continue
        if len(texts) >= MAX_CLIENT_WORKFLOWS:
            _logger.warning(
                "%s: more than %d workflows, %s skipped", plugin.name, MAX_CLIENT_WORKFLOWS, name
            )
            continue
        uri = f"{plugin.workflows_uri}/{name}"
        try:
            data = await reader.read_plugin_resource(uri)
        except Exception:
            _logger.debug("cannot read %s", uri, exc_info=True)
            continue
        if len(data) > MAX_CLIENT_WORKFLOW_BYTES:
            _logger.warning(
                "%s: %s skipped: over %d bytes", plugin.name, name, MAX_CLIENT_WORKFLOW_BYTES
            )
            continue
        try:
            texts[name] = data.decode("utf-8")
        except UnicodeDecodeError:
            continue

    metas: dict[str, Mapping[str, Any] | None] = {}
    if node is not None and texts:
        try:
            async with Sandbox(node) as sandbox:
                for name, text in texts.items():
                    uri = f"{plugin.workflows_uri}/{name}"
                    try:
                        metas[name] = await sandbox.meta(text, filename=uri)
                    except SandboxError as exc:
                        _logger.warning("cannot read meta of %s: %s", uri, exc)
        except SandboxError as exc:
            _logger.warning("cannot read client workflow meta: %s", exc)
    return [
        WorkflowEntry(
            name=Path(name).stem,
            path=Path(name),
            source="client",
            container_id=plugin.container_id,
            meta=metas.get(name),
            shadowed_by=None,
            language="js",
            uri=f"{plugin.workflows_uri}/{name}",
            text=text,
        )
        for name, text in texts.items()
    ]


async def discover(
    dirs: Sequence[SourceDir],
    *,
    node: str | None,
    python_sources: Sequence[str] = PYTHON_SOURCES_DEFAULT,
    client: Sequence[WorkflowEntry] = (),
) -> list[WorkflowEntry]:
    """Every workflow in `client`, then `dirs`, in order; `meta` is read from source, never run.

    `.py` files count only in directories whose source is in `python_sources`, and a `.js` file
    of the same name in the same directory wins (as in the Registry).
    """
    found: list[tuple[SourceDir, Path, str]] = []
    for source_dir in dirs:
        if not source_dir.path.is_dir():
            continue
        candidates = list(source_dir.path.glob("*.js"))
        if source_dir.source in python_sources:
            candidates.extend(
                p for p in source_dir.path.glob("*.py") if not p.with_suffix(".js").exists()
            )
        for path in sorted(candidates, key=lambda p: p.name):
            if not WORKFLOW_NAME.fullmatch(path.stem):
                continue
            if path.is_symlink() or not path.is_file():
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            found.append((source_dir, path, text))

    metas: dict[Path, Mapping[str, Any] | None] = {}
    for _, path, text in found:
        if path.suffix != ".py":
            continue
        try:
            metas[path] = pyrunner.read_meta(text, str(path))
        except pyrunner.RpcError as exc:
            _logger.warning("cannot read meta of %s: %s", path, exc)
    js_found = [item for item in found if item[1].suffix == ".js"]
    if node is not None and js_found:
        async with Sandbox(node) as sandbox:
            for _, path, text in js_found:
                try:
                    metas[path] = await sandbox.meta(text, filename=str(path))
                except SandboxError as exc:
                    _logger.warning("cannot read meta of %s: %s", path, exc)

    entries: list[WorkflowEntry] = []
    first_source: dict[str, str] = {}
    for entry in client:
        entries.append(replace(entry, shadowed_by=first_source.get(entry.name)))
        first_source.setdefault(entry.name, entry.source)
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
                language="python" if path.suffix == ".py" else "js",
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


def schedule_of(meta: Mapping[str, Any] | None) -> tuple[dict[str, str] | None, str | None]:
    """The default schedule `meta.x.schedule` offers, and a warning when it is not usable.

    A cron string (UTC) or `{expression, timeZone?}`. The checks are the ones ahp-host runs
    on a saved definition, so an offered schedule always saves.
    """
    x = meta.get("x") if isinstance(meta, Mapping) else None
    if not isinstance(x, Mapping) or "schedule" not in x:
        return (None, None)
    raw = x["schedule"]
    expression: str
    tz: Any
    if isinstance(raw, str):
        expression, tz = raw, "UTC"
    elif isinstance(raw, Mapping) and isinstance(raw.get("expression"), str):
        expression, tz = raw["expression"], raw.get("timeZone", "UTC")
    else:
        return (None, "meta.x.schedule must be a cron string or {expression, timeZone?}")
    if not isinstance(tz, str):
        return (None, "meta.x.schedule must be a cron string or {expression, timeZone?}")
    try:
        CronExpression.parse(expression)
        ZoneInfo(tz)
    except (ValueError, ZoneInfoNotFoundError) as exc:
        return (None, f"meta.x.schedule {expression!r} is not a valid schedule: {exc}")
    return ({"expression": expression, "timeZone": tz}, None)


def args_schema_of(meta: Mapping[str, Any] | None) -> tuple[dict[str, Any] | None, str | None]:
    """The JSON Schema `meta.x.args` declares for the args, and a warning when unusable."""
    x = meta.get("x") if isinstance(meta, Mapping) else None
    if not isinstance(x, Mapping) or "args" not in x:
        return (None, None)
    raw = x["args"]
    error = args_schema_error(raw)
    if error is not None:
        return (None, error)
    return (dict(raw), None)


def _child(entry: WorkflowEntry) -> dict[str, Any]:
    phases = entry.meta.get("phases") if entry.meta is not None else None
    flow: dict[str, Any] = {
        "kind": "workflow",
        "phases": phases if isinstance(phases, list) else [],
        "source": entry.source,
    }
    if entry.shadowed_by is not None:
        flow["shadowedBy"] = entry.shadowed_by
    if entry.language == "python":
        flow["language"] = "python"
    warnings: list[str] = []
    schedule, warning = schedule_of(entry.meta)
    if schedule is not None:
        flow["schedule"] = schedule
    if warning is not None:
        warnings.append(warning)
    args_schema, warning = args_schema_of(entry.meta)
    if args_schema is not None:
        flow["argsSchema"] = args_schema
    if warning is not None:
        warnings.append(warning)
    if warnings:
        flow["warnings"] = warnings
        for text in warnings:
            _logger.warning("%s: %s", entry.name, text)
    return {
        "type": "prompt",
        "id": f"{entry.container_id}.{entry.name}",
        "uri": entry.location(),
        "name": entry.name,
        "description": _description(entry.meta),
        "enabled": True,
        "_meta": {FLOW_META_KEY: flow},
    }


def customizations(
    entries: Sequence[WorkflowEntry],
    dirs: Sequence[SourceDir],
    *,
    version: str,
    plugins: Sequence[ClientPlugin] = (),
) -> list[dict[str, Any]]:
    """One container per existing source directory; host directories share one plugin.

    A client plugin with workflows gets the provider's own container beside it, never the
    client's plugin id: the host owns that entry and re-expands it on every republish.
    """
    result: list[dict[str, Any]] = []
    for client_plugin in plugins:
        client_children = [
            _child(e) for e in entries if e.container_id == client_plugin.container_id
        ]
        if not client_children:
            continue
        result.append(
            {
                "type": "directory",
                "id": client_plugin.container_id,
                "uri": client_plugin.workflows_uri,
                "name": f"Workflows ({client_plugin.name})",
                "contents": "prompt",
                "writable": False,
                "enabled": True,
                "children": client_children,
            }
        )
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
