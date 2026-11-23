"""Declarative independent-app discovery and content identity (no Qt or imports).

Discovery does not grant execution trust. The host owns that separate decision;
neither a manifest's ``source`` nor copying a directory makes it trusted.
"""
from __future__ import annotations

import hashlib
import os
import re
import stat
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Any

from src.shared.plugins.inventory import PluginRecord
from src.shared.plugins.manifest import (
    ManifestDiagnostic, PluginManifest, parse_manifest_file, validate_manifest_set,
)

PROTOCOL_VERSION = 1
MAX_PACKAGE_FILES = 10_000
MAX_PACKAGE_BYTES = 128 * 1024 * 1024
_RESERVED_ENTRY_DIRS = frozenset({
    "__pycache__", ".git", ".venv", "venv", ".pytest_cache", ".ruff_cache",
    ".mypy_cache", ".cache", "cache",
})
_IGNORED_DIRS = frozenset({".git"})
_GENERATED_BYTECODE = re.compile(r".+\.(?:cpython|pypy)-\d+(?:\.opt-\d+)?\.pyc")
_MODULE = re.compile(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*", re.ASCII)
_EXTENSION = re.compile(r"\.[a-z0-9][a-z0-9_-]{0,31}")


class IndependentAppError(ValueError):
    """An app package is invalid, incompatible, or changed while reading."""


@dataclass(frozen=True)
class IndependentAppSpec:
    plugin_id: str
    display_name: str
    version: str
    root: Path
    entrypoint: Path
    extensions: tuple[str, ...]
    required_modules: Mapping[str, str]
    format_modules: Mapping[str, Mapping[str, str]]
    fingerprint: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "extensions", tuple(self.extensions))
        object.__setattr__(self, "required_modules", MappingProxyType(dict(self.required_modules)))
        object.__setattr__(self, "format_modules", MappingProxyType({
            extension: MappingProxyType(dict(modules))
            for extension, modules in self.format_modules.items()
        }))


def _app_root(root: Path) -> Path:
    path = Path(root).expanduser()
    if path.is_symlink():
        raise IndependentAppError("独立应用目录不能是符号链接")
    try:
        path = path.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise IndependentAppError(f"独立应用目录不存在或不可读取：{exc}") from exc
    if not path.is_dir():
        raise IndependentAppError("独立应用根路径必须是目录")
    return path


def _package_files(root: Path) -> Iterable[tuple[str, Path]]:
    count = 0
    total = 0
    # Walk manually: symlinks and special files are rejected before any reads.
    pending = [root]
    while pending:
        directory = pending.pop()
        try:
            entries = directory.iterdir()
            for path in entries:
                count += 1
                if count > MAX_PACKAGE_FILES:
                    raise IndependentAppError("独立应用包超过目录条目数量限制")
                mode = path.lstat().st_mode
                if stat.S_ISLNK(mode):
                    raise IndependentAppError(f"独立应用包不允许符号链接：{path.relative_to(root)}")
                if stat.S_ISDIR(mode):
                    if path.name not in _IGNORED_DIRS:
                        pending.append(path)
                    continue
                if not stat.S_ISREG(mode):
                    raise IndependentAppError(f"独立应用包包含特殊文件：{path.relative_to(root)}")
                if path.name == ".DS_Store":
                    continue
                if path.parent.name == "__pycache__" and _GENERATED_BYTECODE.fullmatch(path.name):
                    # Host Python uses a private prefix and never executes these.
                    continue
                size = path.stat().st_size
                total += size
                if total > MAX_PACKAGE_BYTES:
                    raise IndependentAppError("独立应用包超过体积限制")
                yield path.relative_to(root).as_posix(), path
        except OSError as exc:
            raise IndependentAppError(f"无法读取独立应用目录：{exc}") from exc


def fingerprint_independent_app(root: Path) -> str:
    """Hash package names and bytes deterministically, excluding generated data."""
    canonical = _app_root(root)
    digest = hashlib.sha256()
    total_read = 0
    # Sorting the relative names makes traversal order and directory mtimes irrelevant.
    for name, path in sorted(_package_files(canonical)):
        encoded = name.encode("utf-8")
        digest.update(len(encoded).to_bytes(4, "big"))
        digest.update(encoded)
        file_digest = hashlib.sha256()
        try:
            before = path.stat(follow_symlinks=False)
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
            with os.fdopen(os.open(path, flags), "rb") as source:
                opened = os.fstat(source.fileno())
                if not stat.S_ISREG(opened.st_mode) or (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino):
                    raise IndependentAppError("独立应用内容在读取期间发生变化，请刷新后重试")
                read_bytes = 0
                while chunk := source.read(64 * 1024):
                    read_bytes += len(chunk)
                    total_read += len(chunk)
                    if read_bytes > MAX_PACKAGE_BYTES or total_read > MAX_PACKAGE_BYTES:
                        raise IndependentAppError("独立应用包超过体积限制")
                    file_digest.update(chunk)
                after = os.fstat(source.fileno())
            stamp_before = (before.st_size, before.st_mtime_ns, before.st_ctime_ns)
            stamp_after = (after.st_size, after.st_mtime_ns, after.st_ctime_ns)
            if stamp_before != stamp_after:
                raise IndependentAppError("独立应用内容在读取期间发生变化，请刷新后重试")
        except OSError as exc:
            raise IndependentAppError(f"无法计算独立应用内容摘要：{exc}") from exc
        digest.update(file_digest.digest())
    return digest.hexdigest()


def _modules(raw: object, field: str) -> Mapping[str, str]:
    if not isinstance(raw, Mapping) or len(raw) > 100:
        raise IndependentAppError(f"{field} 必须是有界的模块名与安装包提示映射")
    modules: dict[str, str] = {}
    for name, hint in raw.items():
        if (not isinstance(name, str) or not _MODULE.fullmatch(name)
                or not isinstance(hint, str) or not hint.strip() or len(hint) > 200
                or any(ord(char) < 32 for char in hint)):
            raise IndependentAppError(f"{field} 包含无效的模块名或安装包提示")
        modules[name] = hint
    return MappingProxyType(modules)


def _manifest(root: Path) -> tuple[PluginManifest | None, tuple[ManifestDiagnostic, ...]]:
    path = root / "topos.plugin.json"
    if path.is_symlink():
        raise IndependentAppError("独立应用清单不能是符号链接")
    if not path.is_file():
        raise IndependentAppError("独立应用缺少 topos.plugin.json")
    return parse_manifest_file(path)


def load_independent_app(root: Path, *,
                         host_facts: dict[str, Any] | None = None) -> IndependentAppSpec:
    """Validate an app and return its immutable declaration and package digest."""
    canonical = _app_root(root)
    # The first digest also rejects symlinks before reading the manifest.
    fingerprint = fingerprint_independent_app(canonical)
    manifest, findings = _manifest(canonical)
    if manifest is None:
        raise IndependentAppError("；".join(item.message for item in findings) or "独立应用清单无效")
    if host_facts is None:
        from src.shared.plugins.discovery import current_host_facts
        host_facts = current_host_facts()
    findings = (*findings, *validate_manifest_set((manifest,), **host_facts)[manifest.plugin_id])
    errors = [item.message for item in findings if item.severity == "error"]
    if errors:
        raise IndependentAppError("；".join(errors))
    app = manifest.metadata.get("independent_app")
    if not isinstance(app, Mapping):
        raise IndependentAppError("metadata.independent_app 必须是对象")
    protocol = app.get("protocol")
    if type(protocol) is not int or protocol != PROTOCOL_VERSION:
        raise IndependentAppError("不支持的独立应用协议版本；当前仅支持整数 1")
    entry = app.get("entrypoint")
    if not isinstance(entry, str) or not entry or "\\" in entry or "\x00" in entry:
        raise IndependentAppError("entrypoint 必须是包内相对 Python 文件路径")
    relative = PurePosixPath(entry)
    if (relative.is_absolute() or ":" in entry or any(part in {"", ".", ".."} for part in entry.split("/"))
            or relative.suffix != ".py" or any(part in _RESERVED_ENTRY_DIRS for part in relative.parts)):
        raise IndependentAppError("entrypoint 必须是包内相对 .py 文件路径，且不能位于缓存目录")
    entrypoint = canonical.joinpath(*relative.parts)
    if not entrypoint.is_file() or not entrypoint.resolve().is_relative_to(canonical):
        raise IndependentAppError("entrypoint 文件不存在或位于独立应用包之外")
    extensions_raw = app.get("extensions")
    if not isinstance(extensions_raw, (list, tuple)) or not extensions_raw or len(extensions_raw) > 100:
        raise IndependentAppError("extensions 必须是非空文件扩展名列表")
    extensions: list[str] = []
    for extension in extensions_raw:
        if not isinstance(extension, str) or not _EXTENSION.fullmatch(extension.lower()):
            raise IndependentAppError("extensions 必须使用带点的文件扩展名，例如 .docx")
        normalized = extension.lower()
        if normalized not in extensions:
            extensions.append(normalized)
    required = _modules(app.get("required_modules", {}), "required_modules")
    format_raw = app.get("format_modules", {})
    if not isinstance(format_raw, Mapping) or len(format_raw) > 100:
        raise IndependentAppError("format_modules 必须是按扩展名分组的模块映射")
    format_modules: dict[str, Mapping[str, str]] = {}
    for extension, modules in format_raw.items():
        if not isinstance(extension, str) or extension.lower() not in extensions:
            raise IndependentAppError("format_modules 中的扩展名必须已在 extensions 声明")
        normalized = extension.lower()
        if normalized in format_modules:
            raise IndependentAppError("format_modules 包含重复的扩展名")
        format_modules[normalized] = _modules(modules, f"format_modules.{normalized}")
    if not any(item.kind == "command" and item.available and item.run_location == "process"
               for item in manifest.capabilities):
        raise IndependentAppError("独立应用必须声明 run_location=process 的 command 能力")
    if fingerprint_independent_app(canonical) != fingerprint:
        raise IndependentAppError("独立应用内容在读取期间发生变化，请刷新后重试")
    return IndependentAppSpec(
        plugin_id=manifest.plugin_id, display_name=manifest.display_name,
        version=manifest.version, root=canonical, entrypoint=entrypoint,
        extensions=tuple(extensions), required_modules=required,
        format_modules=MappingProxyType(format_modules), fingerprint=fingerprint)


def discover_independent_apps(
    roots: Iterable[Path], desired_by_id: dict[str, bool] | None = None, *,
    host_facts: dict[str, Any] | None = None,
) -> list[PluginRecord]:
    """Read one directory level of app packages; never import or run their code."""
    records: list[PluginRecord] = []
    seen: set[Path] = set()
    for base in roots:
        root = Path(base).expanduser()
        if not root.is_dir() or root.is_symlink():
            continue
        try:
            entries = sorted(root.iterdir(), key=lambda item: item.name)
        except OSError:
            continue
        for entry in entries:
            if entry.name.startswith(".") or not entry.is_dir():
                continue
            manifest_path = entry / "topos.plugin.json"
            if not manifest_path.exists() and not manifest_path.is_symlink():
                continue
            key = entry.absolute()
            if key in seen:
                continue
            seen.add(key)
            manifest: PluginManifest | None = None
            try:
                if not entry.is_symlink():
                    manifest, _ = _manifest(entry)
                # Ordinary plugin declarations in the same root are not apps.
                if manifest is not None and "independent_app" not in manifest.metadata:
                    continue
                spec = load_independent_app(entry, host_facts=host_facts)
                records.append(PluginRecord(
                    plugin_id=spec.plugin_id, display_name=spec.display_name,
                    version=spec.version, source="local_app", install_state="discovered",
                    desired_enabled=(desired_by_id or {}).get(spec.plugin_id, True),
                    runtime_state="not_loaded", origin_path=str(spec.root), manifest=manifest))
            except (IndependentAppError, OSError) as exc:
                identity = manifest.plugin_id if manifest else f"app.{hashlib.sha256(os.fsencode(key)).hexdigest()[:16]}"
                message = str(exc)
                records.append(PluginRecord(
                    plugin_id=identity,
                    display_name=manifest.display_name if manifest else entry.name,
                    version=manifest.version if manifest else "", source="local_app",
                    install_state="discovered", desired_enabled=False,
                    runtime_state="rejected", failure_phase="validate", manifest=manifest,
                    origin_path=str(entry.absolute()), error_code="INDEPENDENT_APP_INVALID",
                    error_message=message, diagnostics=(ManifestDiagnostic(
                        code="INDEPENDENT_APP_INVALID", severity="error", message=message),)))
    return records
