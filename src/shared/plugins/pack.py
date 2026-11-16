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

    if not folder.is_dir():
        note("FOLDER_MISSING", "error", f"目录不存在：{folder}")
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
        if entry.startswith("/") or ".." in entry:
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
    report = validate_package_folder(folder, host_facts=host_facts)
    if strict and not report.ok:
        raise ValueError(f"包校验未通过，拒绝构建：{report.plugin_id or folder}；"
                         f"{';'.join(item.message for item in report.diagnostics if item.severity == 'error')}")
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name in sorted(report.entry_files):
            archive.write(folder / name, name)
    digest = package_digest(output)
    sidecar = output.with_name(output.name + ".sha256")
    sidecar.write_text(f"{digest}  {output.name}\n", encoding="utf-8")
    return output, ValidationReport(True, report.plugin_id, report.version,
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
    if digest is None:
        sidecar = package.with_name(package.name + ".sha256")
        if not sidecar.is_file():
            return False
        digest = sidecar.read_text(encoding="utf-8").split()[0]
    return hashlib.sha256(package.read_bytes()).hexdigest() == digest


__all__ = ["MANIFEST_NAME", "ValidationReport", "validate_package_folder",
           "build_package", "package_digest", "verify_package"]
