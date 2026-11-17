"""Deterministic package builder and one-shot validation (PL-10).

For plugin authors: validate a development folder (manifest, capabilities,
host compatibility, entry boundaries, packaging contents) and build a
reproducible zip whose byte content is stable for identical input — the
content digest ships beside the package so installs can verify integrity.

This is tooling only; it never executes plugin code and never invents fields.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from src.shared.plugins.manifest import (
    ManifestDiagnostic,
    parse_manifest_file,
    validate_manifest_set,
)

MANIFEST_NAME = "topos.plugin.json"


@dataclass(frozen=True)
class ValidationReport:
    ok: bool
    plugin_id: str
    version: str
    diagnostics: tuple[ManifestDiagnostic, ...]
    entry_files: tuple[str, ...] = ()
    package_bytes: int = 0
    content_digest: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "plugin_id": self.plugin_id, "version": self.version,
                "diagnostics": [item.to_dict() for item in self.diagnostics],
                "entry_files": list(self.entry_files), "package_bytes": self.package_bytes,
                "content_digest": self.content_digest}


def validate_package_folder(folder: Path, *, host_facts: dict[str, Any]) -> ValidationReport:
    """Author-side validation: same rules the installer enforces, run early."""
    folder = Path(folder)
    diagnostics: list[ManifestDiagnostic] = []

    def note(code: str, severity: str, message: str, field: str = "") -> None:
        diagnostics.append(ManifestDiagnostic(code=code, severity=severity,
                                              message=message, field=field))

    if folder.is_symlink():
        note("PACKAGE_SYMLINK_MEMBER", "error", "安装来源不能是符号链接")
        return ValidationReport(False, "", "", tuple(diagnostics))
    if not folder.is_dir():
        note("FOLDER_MISSING", "error", f"目录不存在：{folder}")
        return ValidationReport(False, "", "", tuple(diagnostics))
    # Check links, including dangling and directory links, before parsing the
    # manifest or reading any source contents into the package.
    linked = [path for path in folder.rglob("*") if path.is_symlink()]
    if linked:
        for path in linked:
            label = str(path.relative_to(folder)) if path != folder else "."
            note("PACKAGE_SYMLINK_MEMBER", "error", f"包内含符号链接：{label}")
        return ValidationReport(False, "", "", tuple(diagnostics))
    special = [path for path in folder.rglob("*") if not (path.is_dir() or path.is_file())]
    if special:
        for path in special:
            note("PACKAGE_SPECIAL_MEMBER", "error", f"包内含非常规文件：{path.relative_to(folder)}")
        return ValidationReport(False, "", "", tuple(diagnostics))
    manifest, parse_diagnostics = parse_manifest_file(folder / MANIFEST_NAME)
    diagnostics.extend(parse_diagnostics)
    if manifest is None:
        note("MANIFEST_MISSING", "error", f"缺少可解析的 {MANIFEST_NAME}")
        return ValidationReport(False, "", "", tuple(diagnostics))
    findings = validate_manifest_set((manifest,), **host_facts)
    diagnostics.extend(findings[manifest.plugin_id])
    entries = sorted(path.relative_to(folder).as_posix()
                     for path in folder.rglob("*") if path.is_file())
    for entry in entries:
        if "__pycache__" in entry or entry.endswith((".pyc", ".DS_Store")):
            note("JUNK_ENTRY", "warning", f"打包内容包含派生文件：{entry}")
        if entry.startswith("/") or ".." in Path(entry).parts or "\\" in entry or ":" in entry:
            note("ENTRY_PATH_INVALID", "error", f"非法路径条目：{entry}")
    if not entries:
        note("PACKAGE_EMPTY", "error", "包内没有任何文件")
    ok = not any(item.severity == "error" for item in diagnostics)
    return ValidationReport(ok, manifest.plugin_id, manifest.version,
                            tuple(diagnostics), tuple(entries))


def build_package(folder: Path, output: Path, *,
                  host_facts: dict[str, Any],
                  strict: bool = True) -> tuple[Path, ValidationReport]:
    """Deterministically zip ``folder``; fails instead of shipping an invalid
    package when ``strict`` (errors) — warnings still ship."""
    folder, output = Path(folder), Path(output)
    if output.resolve().is_relative_to(folder.resolve()):
        raise ValueError("构建输出必须放在源目录之外，避免把构建产物重新打包")
    if output.is_symlink() or output.with_name(output.name + ".sha256").is_symlink():
        raise ValueError("构建输出不能是符号链接")
    report = validate_package_folder(folder, host_facts=host_facts)
    if any(item.code in {"PACKAGE_SYMLINK_MEMBER", "PACKAGE_SPECIAL_MEMBER", "ENTRY_PATH_INVALID"}
           for item in report.diagnostics):
        raise ValueError("包内含符号链接、非常规文件或非法路径，拒绝构建")
    if strict and not report.ok:
        raise ValueError(f"包校验未通过，拒绝构建：{report.plugin_id or folder}；"
                         f"{';'.join(item.message for item in report.diagnostics if item.severity == 'error')}")
    output.parent.mkdir(parents=True, exist_ok=True)
    sidecar = output.with_name(output.name + ".sha256")
    # Write new inodes and replace names; an existing hardlink at output must
    # never truncate a source file (or another unrelated file).
    with tempfile.NamedTemporaryFile(dir=output.parent, prefix=".topos-package-", delete=False) as temp:
        temporary = Path(temp.name)
    temporary_sidecar: Path | None = None
    try:
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for name in sorted(report.entry_files):
                path = folder / name
                # Source mtimes and platform-specific permission bits must not
                # change package identity. Preserve only executable intent.
                info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
                info.create_system = 3
                mode = 0o755 if path.stat().st_mode & 0o111 else 0o644
                info.external_attr = (stat.S_IFREG | mode) << 16
                info.compress_type = zipfile.ZIP_DEFLATED
                with path.open("rb") as source, archive.open(info, "w") as sink:
                    shutil.copyfileobj(source, sink, length=1024 * 1024)
        digest = package_digest(temporary)
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=output.parent,
                                         prefix=".topos-digest-", delete=False) as temp_digest:
            temporary_sidecar = Path(temp_digest.name)
            temp_digest.write(f"{digest}  {output.name}\n")
        os.replace(temporary, output)
        os.replace(temporary_sidecar, sidecar)
    finally:
        temporary.unlink(missing_ok=True)
        if temporary_sidecar is not None:
            temporary_sidecar.unlink(missing_ok=True)
    return output, ValidationReport(report.ok, report.plugin_id, report.version,
                                    report.diagnostics, report.entry_files,
                                    output.stat().st_size, digest)


def package_digest(package: Path) -> str:
    """Stable digest of package bytes (deterministic build ⇒ same digest)."""
    hasher = hashlib.sha256()
    with open(package, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def verify_package(package: Path, digest: str | None = None) -> bool:
    """Integrity check; uses the ``.sha256`` sidecar when digest not given."""
    package = Path(package)
    try:
        if digest is None:
            sidecar = package.with_name(package.name + ".sha256")
            if not sidecar.is_file():
                return False
            parts = sidecar.read_text(encoding="utf-8").split()
            if not parts:
                return False
            digest = parts[0]
        if not isinstance(digest, str) or re.fullmatch(r"[0-9a-fA-F]{64}", digest) is None:
            return False
        return package_digest(package) == digest.lower()
    except (OSError, UnicodeError):
        return False


__all__ = ["MANIFEST_NAME", "ValidationReport", "validate_package_folder",
           "build_package", "package_digest", "verify_package"]
