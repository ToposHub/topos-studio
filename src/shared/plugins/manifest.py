"""Cross-domain plugin manifest schema and validator (PL-01).

Shared by the GLSL/AI/Files plugin hosts. Discovery reads these declarations
without importing or executing plugin code; unknown contribution kinds are
preserved and marked unavailable instead of being dropped.

Design invariants (see docs/architecture/files-plugins-shared-contracts.md):
- manifest records are immutable; ``metadata`` is frozen (MappingProxyType)
- identity is a namespaced ``plugin_id`` (``<domain>.<name>``); legacy ids map
  to it and never define identity by translated name or absolute path
- manifests must not contain secrets or user absolute paths
- schema evolution is additive: unknown fields survive a round-trip
"""
from __future__ import annotations

import ast
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

SCHEMA_VERSION = "1.0"
KNOWN_SCHEMA_VERSIONS = ("1.0",)

# Contribution kinds understood by this host generation. Unknown kinds are
# retained with ``available=False``; they are never silently dropped.
CONTRIBUTION_KINDS = (
    "command",          # PL-04: the single launch entry (toolbar/launcher/menu/shortcut)
    "window",           # PL-04: window hosted/owned contribution
    "background_service",  # PL-03: library/global scoped resident work
    "format_provider",  # PL-05: probe/metadata/thumbnail/playback/export
    "inspector",        # PL-05: right-side property panel contribution
    "menu",             # structured menu entry (never raw HTML/JS)
)
RUN_LOCATIONS = ("host", "worker", "process")
WRITE_SEMANTICS = ("read", "write")
SELECTION_MODES = ("none", "single", "many")

_PLUGIN_ID_RE = re.compile(r"^[a-z][a-z0-9_]*\.[a-z0-9][a-z0-9._-]*$")
_LEGACY_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
_SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$")
_DOTTED_VERSION_RE = re.compile(r"^\d+(?:\.\d+)*$")
# Keys that must never appear in a manifest (leaked credentials / private paths).
_FORBIDDEN_METADATA_KEYS = re.compile(
    r"(token|secret|api[_-]?key|password|credential|authorization)", re.IGNORECASE)
_MAX_MANIFEST_BYTES = 1024 * 1024
_MAX_TEXT_FIELD = 2000

_SECRET_PATH_HINT = ("/users/", "/home/", "c:\\users\\")


@dataclass(frozen=True)
class ManifestDiagnostic:
    """One validation finding; ``severity`` is error/warning/info."""

    code: str
    severity: str
    message: str
    field: str = ""

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "severity": self.severity,
                "message": self.message[:500], "field": self.field}


def _frozen_mapping(value: Mapping[str, Any] | None) -> Mapping[str, Any]:
    if value is None:
        return MappingProxyType({})
    if not isinstance(value, Mapping):
        raise ValueError("必须是键值对象")
    return MappingProxyType(dict(value))


@dataclass(frozen=True)
class PlatformConstraint:
    """Empty tuples mean "no restriction"."""

    platforms: tuple[str, ...] = ()
    arches: tuple[str, ...] = ()

    def matches(self, platform: str, arch: str) -> bool:
        return ((not self.platforms or platform in self.platforms)
                and (not self.arches or arch in self.arches))

    def to_dict(self) -> dict[str, tuple[str, ...]]:
        return {"platforms": self.platforms, "arches": self.arches}


@dataclass(frozen=True)
class PluginCapability:
    """One declared contribution point with a stable id (PL-01/04/05)."""

    capability_id: str
    kind: str                     # CONTRIBUTION_KINDS or an unknown future kind
    title: str = ""
    description: str = ""
    available: bool = True        # False for preserved-but-unknown kinds
    input_schema: Mapping[str, Any] = field(default_factory=dict, repr=False)
    output_schema: Mapping[str, Any] = field(default_factory=dict, repr=False)
    params_schema: Mapping[str, Any] = field(default_factory=dict, repr=False)
    selection_mode: str = "none"  # none/single/many (PL-04)
    applies_to_kinds: tuple[str, ...] = ()   # image/video/... empty = all
    write_semantics: str = "read"
    cancellable: bool = True
    retriable: bool = False
    run_location: str = "host"    # host/worker/process
    resource_policy: Mapping[str, Any] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "input_schema", _frozen_mapping(self.input_schema))
        object.__setattr__(self, "output_schema", _frozen_mapping(self.output_schema))
        object.__setattr__(self, "params_schema", _frozen_mapping(self.params_schema))
        object.__setattr__(self, "resource_policy", _frozen_mapping(self.resource_policy))

    def to_dict(self) -> dict[str, Any]:
        return {
            "capability_id": self.capability_id, "kind": self.kind,
            "title": self.title[:200], "description": self.description[:_MAX_TEXT_FIELD],
            "available": self.available,
            "input_schema": dict(self.input_schema), "output_schema": dict(self.output_schema),
            "params_schema": dict(self.params_schema),
            "selection_mode": self.selection_mode,
            "applies_to_kinds": self.applies_to_kinds,
            "write_semantics": self.write_semantics,
            "cancellable": self.cancellable, "retriable": self.retriable,
            "run_location": self.run_location, "resource_policy": dict(self.resource_policy),
        }


@dataclass(frozen=True)
class WindowDeclaration:
    window_id: str
    command_id: str               # windows are opened through a command, never directly
    min_size: tuple[int, int] = (320, 240)
    default_size: tuple[int, int] = (800, 600)
    single_instance: bool = True
    ownership: str = "plugin"     # plugin/host

    def to_dict(self) -> dict[str, Any]:
        return {"window_id": self.window_id, "command_id": self.command_id,
                "min_size": list(self.min_size), "default_size": list(self.default_size),
                "single_instance": self.single_instance, "ownership": self.ownership}


@dataclass(frozen=True)
class FormatProviderDeclaration:
    """probe/metadata/thumbnail/playback/export declared separately (PL-05)."""

    provider_id: str
    priority: int = 100
    formats: tuple[str, ...] = ()  # extensions without dot; empty = declared-only
    magic: tuple[str, ...] = ()    # hex magic prefixes
    provides: tuple[str, ...] = ()  # subset of PROVIDES set below
    version: str = ""
    cache_signature: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"provider_id": self.provider_id, "priority": self.priority,
                "formats": self.formats, "magic": self.magic, "provides": self.provides,
                "version": self.version, "cache_signature": self.cache_signature}


FORMAT_PROVIDES = ("probe", "metadata", "thumbnail", "playback", "export")


@dataclass(frozen=True)
class BackgroundServiceDeclaration:
    service_id: str
    scope: str = "library"        # library/global
    auto_start: bool = False
    idle_policy: str = "stop"     # stop/pause/continue
    network_policy: str = "deny"  # deny/allow

    def to_dict(self) -> dict[str, Any]:
        return {"service_id": self.service_id, "scope": self.scope,
                "auto_start": self.auto_start, "idle_policy": self.idle_policy,
                "network_policy": self.network_policy}


@dataclass(frozen=True)
class PluginManifest:
    """Immutable cross-domain plugin declaration."""

    plugin_id: str
    display_name: str
    version: str = "1.0.0"
    schema_version: str = SCHEMA_VERSION
    host_api: tuple[int, int] = (1, 1)   # inclusive [min, max] host API range
    topos_min_version: str = ""
    legacy_ids: tuple[str, ...] = ()
    author: str = ""
    support: str = ""
    license: str = ""
    category: str = ""
    keywords: tuple[str, ...] = ()
    description: str = ""
    dependencies: tuple[str, ...] = ()
    optional_dependencies: tuple[str, ...] = ()
    platform: PlatformConstraint = field(default_factory=PlatformConstraint)
    capabilities: tuple[PluginCapability, ...] = ()
    windows: tuple[WindowDeclaration, ...] = ()
    format_providers: tuple[FormatProviderDeclaration, ...] = ()
    background_services: tuple[BackgroundServiceDeclaration, ...] = ()
    source: str = "builtin"       # builtin/local_dev/verified_third_party
    metadata: Mapping[str, Any] = field(default_factory=dict, repr=False)
    unknown_fields: Mapping[str, Any] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "metadata", _frozen_mapping(self.metadata))
        object.__setattr__(self, "unknown_fields", _frozen_mapping(self.unknown_fields))

    @property
    def unverified(self) -> bool:
        return self.source == "local_dev"

    def to_dict(self) -> dict[str, Any]:
        return {
            "plugin_id": self.plugin_id, "display_name": self.display_name,
            "version": self.version, "schema_version": self.schema_version,
            "host_api": list(self.host_api), "topos_min_version": self.topos_min_version,
            "legacy_ids": self.legacy_ids, "author": self.author[:200],
            "support": self.support[:200], "license": self.license[:100],
            "category": self.category, "keywords": self.keywords,
            "description": self.description[:_MAX_TEXT_FIELD],
            "dependencies": self.dependencies,
            "optional_dependencies": self.optional_dependencies,
            "platform": self.platform.to_dict(),
            "capabilities": [item.to_dict() for item in self.capabilities],
            "windows": [item.to_dict() for item in self.windows],
            "format_providers": [item.to_dict() for item in self.format_providers],
            "background_services": [item.to_dict() for item in self.background_services],
            "source": self.source, "metadata": dict(self.metadata),
            "unknown_fields": dict(self.unknown_fields),
        }


# ---------------------------------------------------------------------------
# Parsing (declarative JSON only — never imports plugin code)
# ---------------------------------------------------------------------------


def _diagnostic(code: str, severity: str, message: str, field_name: str = "") -> ManifestDiagnostic:
    return ManifestDiagnostic(code=code, severity=severity, message=message, field=field_name)


def _capability_from_mapping(raw: Mapping[str, Any], index: int) -> tuple[PluginCapability | None, list[ManifestDiagnostic]]:
    diagnostics: list[ManifestDiagnostic] = []
    capability_id = raw.get("capability_id")
    if not isinstance(capability_id, str) or not capability_id.strip():
        diagnostics.append(_diagnostic("CAPABILITY_ID_MISSING", "error",
                                       f"能力 #{index} 缺少 capability_id", f"capabilities[{index}]"))
        return None, diagnostics
    kind = raw.get("kind", "")
    known = kind in CONTRIBUTION_KINDS
    if not isinstance(kind, str) or not kind:
        diagnostics.append(_diagnostic("CAPABILITY_KIND_MISSING", "error",
                                       f"{capability_id}: 缺少 kind", f"capabilities[{index}].kind"))
        return None, diagnostics
    if not known:
        # Future contribution kind: keep the declaration, mark unavailable.
        diagnostics.append(_diagnostic("CAPABILITY_KIND_UNKNOWN", "warning",
                                       f"{capability_id}: 未知贡献类型 {kind}，保留声明并标记不可用",
                                       f"capabilities[{index}].kind"))
    for name, allowed in (("selection_mode", SELECTION_MODES), ("run_location", RUN_LOCATIONS),
                          ("write_semantics", WRITE_SEMANTICS)):
        value = raw.get(name)
        if value is not None and value not in allowed:
            diagnostics.append(_diagnostic(f"CAPABILITY_{name.upper()}_INVALID", "error",
                                           f"{capability_id}: {name} 必须是 {allowed}",
                                           f"capabilities[{index}].{name}"))
            return None, diagnostics
    return PluginCapability(
        capability_id=capability_id, kind=kind,
        title=str(raw.get("title", ""))[:200],
        description=str(raw.get("description", ""))[:_MAX_TEXT_FIELD],
        available=known,
        input_schema=_frozen_mapping(raw.get("input_schema") if isinstance(raw.get("input_schema"), Mapping) else {}),
        output_schema=_frozen_mapping(raw.get("output_schema") if isinstance(raw.get("output_schema"), Mapping) else {}),
        params_schema=_frozen_mapping(raw.get("params_schema") if isinstance(raw.get("params_schema"), Mapping) else {}),
        selection_mode=str(raw.get("selection_mode", "none")),
        applies_to_kinds=tuple(raw.get("applies_to_kinds", ()) or ()),
        write_semantics=str(raw.get("write_semantics", "read")),
        cancellable=bool(raw.get("cancellable", True)),
        retriable=bool(raw.get("retriable", False)),
        run_location=str(raw.get("run_location", "host")),
        resource_policy=_frozen_mapping(raw.get("resource_policy") if isinstance(raw.get("resource_policy"), Mapping) else {}),
    ), diagnostics


def parse_manifest_mapping(data: Mapping[str, Any]) -> tuple[PluginManifest | None, tuple[ManifestDiagnostic, ...]]:
    """Build a manifest from a JSON-like mapping. Never executes plugin code."""
    diagnostics: list[ManifestDiagnostic] = []
    if not isinstance(data, Mapping):
        return None, (_diagnostic("MANIFEST_NOT_OBJECT", "error", "manifest 必须是 JSON 对象"),)

    known_keys = set(PluginManifest.__dataclass_fields__)
    unknown = {key: value for key, value in data.items() if key not in known_keys}

    plugin_id = data.get("plugin_id")
    if not isinstance(plugin_id, str) or not _PLUGIN_ID_RE.match(plugin_id):
        diagnostics.append(_diagnostic("PLUGIN_ID_INVALID", "error",
                                       "plugin_id 必须形如 <domain>.<name>（小写字母数字）", "plugin_id"))
        return None, tuple(diagnostics)
    display_name = data.get("display_name")
    if not isinstance(display_name, str) or not display_name.strip():
        diagnostics.append(_diagnostic("DISPLAY_NAME_MISSING", "error", "缺少 display_name", "display_name"))
        return None, tuple(diagnostics)
    version = str(data.get("version", "1.0.0"))
    if not _SEMVER_RE.match(version):
        diagnostics.append(_diagnostic("VERSION_INVALID", "error", f"version 不是语义化版本：{version}", "version"))
        return None, tuple(diagnostics)

    legacy_ids = tuple(data.get("legacy_ids", ()) or ())
    if any(not isinstance(item, str) or not _LEGACY_ID_RE.match(item) for item in legacy_ids):
        diagnostics.append(_diagnostic("LEGACY_ID_INVALID", "error", "legacy_ids 含非法 id", "legacy_ids"))
        return None, tuple(diagnostics)

    schema_version = str(data.get("schema_version", SCHEMA_VERSION))
    if schema_version not in KNOWN_SCHEMA_VERSIONS:
        # Future schema: parse what we can, surface a warning, mark unusable later.
        diagnostics.append(_diagnostic("SCHEMA_VERSION_UNKNOWN", "warning",
                                       f"manifest schema {schema_version} 高于宿主已知 {KNOWN_SCHEMA_VERSIONS}",
                                       "schema_version"))

    host_api_raw = data.get("host_api", [1, 1])
    if (not isinstance(host_api_raw, (list, tuple)) or len(host_api_raw) != 2
            or any(isinstance(item, bool) or not isinstance(item, int) for item in host_api_raw)):
        diagnostics.append(_diagnostic("HOST_API_INVALID", "error", "host_api 必须是 [min, max] 整数区间", "host_api"))
        return None, tuple(diagnostics)
    host_api = (int(host_api_raw[0]), int(host_api_raw[1]))
    if host_api[0] > host_api[1]:
        diagnostics.append(_diagnostic("HOST_API_RANGE_INVALID", "error", "host_api 区间 min 大于 max", "host_api"))
        return None, tuple(diagnostics)

    topos_min = str(data.get("topos_min_version", "") or "")
    if topos_min and not _DOTTED_VERSION_RE.match(topos_min):
        diagnostics.append(_diagnostic("TOPOS_MIN_VERSION_INVALID", "error",
                                       f"topos_min_version 必须是点分数字：{topos_min}", "topos_min_version"))
        return None, tuple(diagnostics)

    def string_tuple(name: str) -> tuple[str, ...]:
        value = data.get(name, ()) or ()
        if not isinstance(value, (list, tuple)) or any(not isinstance(item, str) for item in value):
            raise ValueError(name)
        return tuple(value)

    try:
        dependencies = string_tuple("dependencies")
        optional_dependencies = string_tuple("optional_dependencies")
        keywords = string_tuple("keywords")
    except ValueError as exc:
        field_name = str(exc)
        diagnostics.append(_diagnostic("FIELD_NOT_STRING_LIST", "error", f"{field_name} 必须是字符串列表", field_name))
        return None, tuple(diagnostics)

    for secret_key in data.get("metadata", {}) if isinstance(data.get("metadata"), Mapping) else {}:
        if _FORBIDDEN_METADATA_KEYS.search(str(secret_key)):
            diagnostics.append(_diagnostic("SECRET_IN_METADATA", "error",
                                           f"manifest 不允许保存凭据类字段：{secret_key}", f"metadata.{secret_key}"))
    for text_field in ("description", "support"):
        value = str(data.get(text_field, "") or "")
        lowered = value.lower()
        if any(hint in lowered for hint in _SECRET_PATH_HINT):
            diagnostics.append(_diagnostic("USER_PATH_IN_MANIFEST", "error",
                                           f"{text_field} 含用户绝对路径，不允许", text_field))

    capabilities: list[PluginCapability] = []
    raw_capabilities = data.get("capabilities", ()) or ()
    if not isinstance(raw_capabilities, (list, tuple)):
        diagnostics.append(_diagnostic("CAPABILITIES_NOT_LIST", "error", "capabilities 必须是列表", "capabilities"))
        return None, tuple(diagnostics)
    for index, raw in enumerate(raw_capabilities):
        if not isinstance(raw, Mapping):
            diagnostics.append(_diagnostic("CAPABILITY_NOT_OBJECT", "error",
                                           f"能力 #{index} 不是对象", f"capabilities[{index}]"))
            continue
        capability, capability_diagnostics = _capability_from_mapping(raw, index)
        diagnostics.extend(capability_diagnostics)
        if capability is not None:
            capabilities.append(capability)

    windows: list[WindowDeclaration] = []
    for raw in data.get("windows", ()) or ():
        if not isinstance(raw, Mapping):
            continue
        windows.append(WindowDeclaration(
            window_id=str(raw.get("window_id", "")), command_id=str(raw.get("command_id", "")),
            min_size=tuple(raw.get("min_size", (320, 240))) if isinstance(raw.get("min_size", []), (list, tuple)) else (320, 240),  # type: ignore[arg-type]
            default_size=tuple(raw.get("default_size", (800, 600))) if isinstance(raw.get("default_size", []), (list, tuple)) else (800, 600),  # type: ignore[arg-type]
            single_instance=bool(raw.get("single_instance", True)),
            ownership=str(raw.get("ownership", "plugin")),
        ))

    providers: list[FormatProviderDeclaration] = []
    for raw in data.get("format_providers", ()) or ():
        if not isinstance(raw, Mapping):
            continue
        providers.append(FormatProviderDeclaration(
            provider_id=str(raw.get("provider_id", "")), priority=int(raw.get("priority", 100)),
            formats=tuple(raw.get("formats", ()) or ()), magic=tuple(raw.get("magic", ()) or ()),
            provides=tuple(raw.get("provides", ()) or ()), version=str(raw.get("version", "")),
            cache_signature=str(raw.get("cache_signature", "")),
        ))

    services: list[BackgroundServiceDeclaration] = []
    for raw in data.get("background_services", ()) or ():
        if not isinstance(raw, Mapping):
            continue
        services.append(BackgroundServiceDeclaration(
            service_id=str(raw.get("service_id", "")), scope=str(raw.get("scope", "library")),
            auto_start=bool(raw.get("auto_start", False)), idle_policy=str(raw.get("idle_policy", "stop")),
            network_policy=str(raw.get("network_policy", "deny")),
        ))

    platform_raw = data.get("platform", {}) or {}
    if not isinstance(platform_raw, Mapping):
        platform_raw = {}
    platform = PlatformConstraint(
        platforms=tuple(platform_raw.get("platforms", ()) or ()),
        arches=tuple(platform_raw.get("arches", ()) or ()),
    )

    manifest = PluginManifest(
        plugin_id=plugin_id, display_name=display_name, version=version,
        schema_version=schema_version, host_api=host_api, topos_min_version=topos_min,
        legacy_ids=legacy_ids,
        author=str(data.get("author", ""))[:200], support=str(data.get("support", ""))[:200],
        license=str(data.get("license", ""))[:100], category=str(data.get("category", "")),
        keywords=keywords, description=str(data.get("description", ""))[:_MAX_TEXT_FIELD],
        dependencies=dependencies, optional_dependencies=optional_dependencies,
        platform=platform, capabilities=tuple(capabilities), windows=tuple(windows),
        format_providers=tuple(providers), background_services=tuple(services),
        source=str(data.get("source", "builtin")),
        metadata=_frozen_mapping(data.get("metadata") if isinstance(data.get("metadata"), Mapping) else {}),
        unknown_fields=_frozen_mapping(unknown),
    )
    return manifest, tuple(diagnostics)


def parse_manifest_file(path: Path) -> tuple[PluginManifest | None, tuple[ManifestDiagnostic, ...]]:
    """Read a declarative manifest file (plugin.json). Size-bounded, no execution."""
    try:
        if path.stat().st_size > _MAX_MANIFEST_BYTES:
            return None, (_diagnostic("MANIFEST_TOO_LARGE", "error",
                                      f"manifest 超过 {_MAX_MANIFEST_BYTES} 字节", str(path)),)
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        return None, (_diagnostic("MANIFEST_UNREADABLE", "error", f"无法读取 manifest：{exc}", str(path)),)
    try:
        return parse_manifest_mapping(data)
    except (TypeError, ValueError, OverflowError, RecursionError) as exc:
        # User-controlled field shapes must not escape a discovery worker.
        # Per-field schema validation is separate from this file boundary.
        return None, (_diagnostic("MANIFEST_INVALID", "error", f"manifest 字段无效：{exc}", str(path)),)


def legacy_python_manifest(folder: Path) -> tuple[PluginManifest | None, tuple[ManifestDiagnostic, ...]]:
    """AST-extract a legacy ``plugins/<name>/__init__.py`` declaration.

    Mirrors the legacy loader's metadata constants without importing the
    module: NAME/VERSION/AUTHOR/DESCRIPTION/API_VERSION only. The result is a
    minimal manifest with a ``files.<folder>`` id plus legacy id mapping, so
    the unified inventory can host legacy and declarative records together.
    ``source`` 固定为 local_dev（未验证）：是否内建由宿主政策决定，不由
    目录位置自证（PR-03）。
    """
    init = folder / "__init__.py"
    diagnostics: list[ManifestDiagnostic] = []
    constants: dict[str, Any] = {}
    try:
        if init.stat().st_size > _MAX_MANIFEST_BYTES:
            return None, (_diagnostic("MANIFEST_TOO_LARGE", "error",
                                      f"{init} 超过 {_MAX_MANIFEST_BYTES} 字节", str(init)),)
        tree = ast.parse(init.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, SyntaxError, ValueError, RecursionError) as exc:
        return None, (_diagnostic("LEGACY_INIT_UNREADABLE", "error", f"无法解析插件声明：{exc}", str(init)),)
    for statement in tree.body:
        names: list[str] = []
        value: ast.expr | None = None
        if isinstance(statement, ast.Assign):
            names = [target.id for target in statement.targets if isinstance(target, ast.Name)]
            value = statement.value
        elif isinstance(statement, ast.AnnAssign) and isinstance(statement.target, ast.Name):
            names, value = [statement.target.id], statement.value
        for name in names:
            if name in {"NAME", "VERSION", "AUTHOR", "DESCRIPTION", "API_VERSION"} and value is not None:
                try:
                    literal = ast.literal_eval(value)
                except (ValueError, TypeError, SyntaxError):
                    continue
                if isinstance(literal, (str, int)) and not isinstance(literal, bool):
                    constants[name] = literal
    try:
        api_version = int(constants.get("API_VERSION", 1))
    except (TypeError, ValueError):
        api_version = None
    if api_version is None:
        diagnostics.append(_diagnostic("LEGACY_API_VERSION_INVALID", "warning",
                                       "API_VERSION 不是整数，按不兼容处理", "API_VERSION"))
        host_api = (0, 0)  # never intersects a real host range
    else:
        host_api = (api_version, api_version)
    manifest = PluginManifest(
        plugin_id=f"files.{folder.name}", display_name=str(constants.get("NAME", folder.name)),
        version=str(constants.get("VERSION", "0.0.1")) or "0.0.1",
        host_api=host_api, legacy_ids=(folder.name,),
        author=str(constants.get("AUTHOR", "Unknown"))[:200],
        description=str(constants.get("DESCRIPTION", ""))[:_MAX_TEXT_FIELD],
        source="local_dev",
    )
    return manifest, tuple(diagnostics)


# ---------------------------------------------------------------------------
# Compatibility & dependency validation across a manifest set (PL-01)
# ---------------------------------------------------------------------------


def _version_key(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version.split(".") if part.isdigit())


def validate_manifest_set(
    manifests: tuple[PluginManifest, ...], *,
    host_api: int, topos_version: str, platform: str, arch: str,
) -> dict[str, tuple[ManifestDiagnostic, ...]]:
    """Validate a whole set against host facts and each other.

    Returns per-plugin_id diagnostics covering: duplicate ids, host API range,
    topos min version, platform/arch, unknown/missing dependencies and cycles.
    Unknown *optional* dependencies are a warning; required ones are an error.
    """
    by_id: dict[str, PluginManifest] = {}
    result: dict[str, list[ManifestDiagnostic]] = {}
    for manifest in manifests:
        result.setdefault(manifest.plugin_id, [])
        if manifest.plugin_id in by_id:
            result[manifest.plugin_id].append(_diagnostic(
                "PLUGIN_ID_DUPLICATE", "error",
                f"重复的 plugin_id（也由 {by_id[manifest.plugin_id].version} 声明）", "plugin_id"))
            continue
        by_id[manifest.plugin_id] = manifest
        # legacy id collisions are identity conflicts too
    legacy_owner: dict[str, str] = {}
    for manifest in manifests:
        for legacy in manifest.legacy_ids:
            if legacy in legacy_owner and legacy_owner[legacy] != manifest.plugin_id:
                result[manifest.plugin_id].append(_diagnostic(
                    "LEGACY_ID_CONFLICT", "error",
                    f"legacy id {legacy} 同时映射到 {legacy_owner[legacy]} 与 {manifest.plugin_id}",
                    "legacy_ids"))
            legacy_owner[legacy] = manifest.plugin_id

    host_key = _version_key(topos_version) if topos_version else ()
    for manifest in manifests:
        findings = result[manifest.plugin_id]
        low, high = manifest.host_api
        if not (low <= host_api <= high):
            findings.append(_diagnostic(
                "HOST_API_INCOMPATIBLE", "error",
                f"宿主 API {host_api} 不在插件声明区间 [{low}, {high}] 内", "host_api"))
        if manifest.topos_min_version and host_key and host_key < _version_key(manifest.topos_min_version):
            findings.append(_diagnostic(
                "TOPOS_VERSION_INCOMPATIBLE", "error",
                f"需要 Topos >= {manifest.topos_min_version}，当前 {topos_version}", "topos_min_version"))
        if not manifest.platform.matches(platform, arch):
            findings.append(_diagnostic(
                "PLATFORM_INCOMPATIBLE", "error",
                f"声明平台 {manifest.platform.to_dict()} 不含当前 {platform}/{arch}", "platform"))
        seen_ids: set[str] = set()
        for capability in manifest.capabilities:
            if capability.capability_id in seen_ids:
                findings.append(_diagnostic(
                    "CAPABILITY_ID_DUPLICATE", "error",
                    f"能力 id 重复：{capability.capability_id}", "capabilities"))
            seen_ids.add(capability.capability_id)
        for window in manifest.windows:
            if not any(capability.capability_id == window.command_id
                       and capability.kind == "command" for capability in manifest.capabilities):
                findings.append(_diagnostic(
                    "WINDOW_COMMAND_MISSING", "error",
                    f"窗口 {window.window_id} 引用的 command 不存在：{window.command_id}", "windows"))
        for provider in manifest.format_providers:
            unknown_provides = [item for item in provider.provides if item not in FORMAT_PROVIDES]
            if unknown_provides:
                findings.append(_diagnostic(
                    "FORMAT_PROVIDES_UNKNOWN", "error",
                    f"provider {provider.provider_id} 声明未知能力 {unknown_provides}", "format_providers"))

    # Dependency resolution & cycle detection (required deps only).
    for manifest in manifests:
        for dependency in manifest.dependencies:
            if dependency not in by_id and dependency not in legacy_owner:
                result[manifest.plugin_id].append(_diagnostic(
                    "DEPENDENCY_MISSING", "error", f"缺少依赖插件：{dependency}", "dependencies"))
        for dependency in manifest.optional_dependencies:
            if dependency not in by_id and dependency not in legacy_owner:
                result[manifest.plugin_id].append(_diagnostic(
                    "OPTIONAL_DEPENDENCY_MISSING", "warning",
                    f"可选依赖不存在（功能可能受限）：{dependency}", "optional_dependencies"))

    def resolve(plugin_id: str, visiting: tuple[str, ...]) -> list[ManifestDiagnostic]:
        findings: list[ManifestDiagnostic] = []
        if plugin_id in visiting:
            chain = " -> ".join((*visiting, plugin_id))
            findings.append(_diagnostic("DEPENDENCY_CYCLE", "error", f"依赖循环：{chain}", "dependencies"))
            return findings
        manifest = by_id.get(plugin_id)
        if manifest is None:
            return findings
        for dependency in manifest.dependencies:
            target = by_id.get(dependency) or by_id.get(legacy_owner.get(dependency, ""))
            if target is not None:
                findings.extend(resolve(target.plugin_id, (*visiting, plugin_id)))
        return findings

    for manifest in manifests:
        result[manifest.plugin_id].extend(resolve(manifest.plugin_id, ()))

    return {plugin_id: tuple(findings) for plugin_id, findings in result.items()}


__all__ = [
    "SCHEMA_VERSION", "KNOWN_SCHEMA_VERSIONS", "CONTRIBUTION_KINDS", "RUN_LOCATIONS",
    "FORMAT_PROVIDES", "ManifestDiagnostic", "PlatformConstraint", "PluginCapability",
    "WindowDeclaration", "FormatProviderDeclaration", "BackgroundServiceDeclaration",
    "PluginManifest", "parse_manifest_mapping", "parse_manifest_file",
    "legacy_python_manifest", "validate_manifest_set",
]
