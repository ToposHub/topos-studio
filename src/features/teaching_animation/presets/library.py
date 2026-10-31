"""Folder-scanned preset library: discover, save, import, export, delete.

On-disk contract (see presets/README.md)::

    <root>/<category>/<preset_id>/
        preset.json    # manifest (optional for code presets — PRESET in build.py)
        build.py       # code-defined preset: PRESET meta + build() -> scene dict
        scene.json     # editor-saved preset: full scene document
        preview.gif    # animated preview rendered by the editor
        assets/        # optional associated assets

Category folders are the authoritative grouping — a preset always lives in
the folder matching its category, so the library stays organised by
construction and migrates wholesale by copying one directory.
"""

from __future__ import annotations

import ast
import json
import logging
import shutil
import tempfile
import uuid
import zipfile
from dataclasses import replace
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence

from ..core.model import TeachingSceneDocument
from ..core.validator import validate_scene
from .manifest import (
    PRESET_ENTRY_CODE,
    PRESET_ENTRY_SCENE,
    PresetError,
    PresetManifest,
    normalize_category,
    slugify,
)
from .paths import default_preset_roots, project_preset_root
from .preview import render_preset_gif

_logger = logging.getLogger(__name__)

_MAX_SCENE_BYTES = 8 * 1024 * 1024
_MAX_PREVIEW_BYTES = 32 * 1024 * 1024


def _warn_if_gui_thread(*, preset_save_hint: str) -> None:
    """Log a warning when a preview render is about to run on the GUI thread.

    AUD-28323: ``save_preset(render_preview=True)`` rasterizes up to 24
    frames synchronously; from a UI slot that freezes the editor. Pure
    best-effort diagnostics — headless callers (no QApplication) never
    trigger it, and any Qt import failure degrades to silence.
    """
    try:
        from PyQt6.QtCore import QCoreApplication, QThread
    except ImportError:  # pragma: no cover — Qt always present in app runs
        return
    app = QCoreApplication.instance()
    if app is not None and QThread.currentThread() is app.thread():
        _logger.warning(
            "save_preset(name=%r): synchronous GIF preview rendering on the "
            "GUI thread (AUD-28323) — pass render_preview=False and schedule "
            "PresetGifPreviewJob, or call save_preset from a worker thread",
            preset_save_hint,
        )


class PresetRecord:
    """One discovered preset package on disk."""

    __slots__ = ("manifest", "folder", "origin")

    def __init__(self, manifest: PresetManifest, folder: Path, origin: str) -> None:
        self.manifest = manifest
        self.folder = Path(folder)
        self.origin = str(origin)

    @property
    def preset_id(self) -> str:
        return self.manifest.preset_id

    @property
    def preview_path(self) -> Path | None:
        candidate = self.folder / self.manifest.preview
        return candidate if candidate.is_file() else None

    def matches(self, text: str) -> bool:
        needle = text.strip().lower()
        if not needle:
            return True
        haystack = " ".join((
            self.manifest.name, self.manifest.category,
            self.manifest.description, *self.manifest.tags,
            # English users type English: localized names must be searchable.
            *dict(self.manifest.name_i18n).values(),
            *dict(self.manifest.description_i18n).values(),
        )).lower()
        return needle in haystack

    def __repr__(self) -> str:  # pragma: no cover — debugging aid
        return f"PresetRecord({self.preset_id!r}, category={self.manifest.category!r}, origin={self.origin!r})"


def _read_code_info(path: Path) -> dict[str, Any]:
    """Read the ``PRESET`` metadata dict from a build.py without executing it."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError) as exc:
        raise PresetError(f"unreadable preset code: {exc}") from exc
    for node in tree.body:
        targets: list = []
        if isinstance(node, ast.Assign):
            targets = list(node.targets)
        elif isinstance(node, ast.AnnAssign) and node.target is not None:
            targets = [node.target]
        if any(isinstance(target, ast.Name) and target.id == "PRESET" for target in targets):
            try:
                value = ast.literal_eval(node.value)
            except ValueError as exc:
                raise PresetError(f"PRESET metadata must be a literal dict: {exc}") from exc
            if isinstance(value, dict):
                return dict(value)
            raise PresetError("PRESET metadata must be a dict literal")
    raise PresetError(f"{path.parent.name}: code preset lacks a PRESET metadata dict")


def _read_manifest(folder: Path) -> PresetManifest | None:
    preset_json = folder / "preset.json"
    if preset_json.is_file():
        if preset_json.stat().st_size > 64 * 1024:
            raise PresetError("preset.json exceeds 64 KiB")
        try:
            data = json.loads(preset_json.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise PresetError(f"unreadable preset.json: {exc}") from exc
        if not isinstance(data, dict):
            raise PresetError("preset.json must contain an object")
        return PresetManifest.from_dict(data)
    build_py = folder / PRESET_ENTRY_CODE
    if build_py.is_file():
        info = _read_code_info(build_py)
        info.setdefault("entry", PRESET_ENTRY_CODE)
        info.setdefault("preset_id", folder.name)
        return PresetManifest.from_dict(info)
    return None


class MgPresetLibrary:
    """Merged view over one or more preset roots (bundled + user)."""

    def __init__(self, roots: Sequence[Path] | None = None) -> None:
        # ``roots is None`` means "follow the default root set", which now
        # includes the project-local root while a project is open — such a
        # library must be given ``refresh_roots()`` chances to follow project
        # switches.  Explicit roots stay fixed forever.
        self._dynamic_roots = roots is None
        roots = list(roots) if roots is not None else default_preset_roots()
        self._roots: list[Path] = [Path(root) for root in roots]
        self._records: dict[str, PresetRecord] = {}
        self.errors: list[str] = []
        self.rescan()

    # -- discovery ---------------------------------------------------------

    @property
    def roots(self) -> tuple[Path, ...]:
        return tuple(self._roots)

    def refresh_roots(self) -> bool:
        """Recompute default roots and rescan when they changed.

        Returns whether a rescan happened.  Libraries built with explicit
        roots never change, so callers can invoke this unconditionally
        (panel show events, rescan buttons) without guarding.
        """
        if not self._dynamic_roots:
            return False
        roots = [Path(root) for root in default_preset_roots()]
        if roots == self._roots:
            return False
        self._roots = roots
        self.rescan()
        return True

    def rescan(self) -> int:
        records: dict[str, PresetRecord] = {}
        errors: list[str] = []
        project_root = project_preset_root()
        for root in self._roots:
            origin = (
                "bundled" if root == self._roots[0]
                else "project" if root == project_root
                else root.name
            )
            if not root.is_dir():
                continue
            for category_dir in sorted(root.iterdir()):
                if not category_dir.is_dir() or category_dir.name.startswith((".", "_")):
                    continue
                for folder in sorted(category_dir.iterdir()):
                    if not folder.is_dir() or folder.name.startswith((".", "_")):
                        continue
                    try:
                        manifest = _read_manifest(folder)
                        if manifest is None:
                            continue
                        category = normalize_category(category_dir.name)
                        manifest = replace(manifest, category=category)
                    except PresetError as exc:
                        errors.append(f"{folder}: {exc}")
                        continue
                    records[manifest.preset_id] = PresetRecord(manifest, folder, origin)
        self._records = records
        self.errors = errors
        return len(records)

    def records(self) -> tuple[PresetRecord, ...]:
        return tuple(sorted(self._records.values(),
                            key=lambda record: (record.manifest.category, record.manifest.name)))

    def get(self, preset_id: str) -> PresetRecord | None:
        return self._records.get(preset_id)

    def categories(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for record in self._records.values():
            counts[record.manifest.category] = counts.get(record.manifest.category, 0) + 1
        return dict(sorted(counts.items()))

    def search(self, text: str = "", category: str | None = None) -> tuple[PresetRecord, ...]:
        wanted = category.strip().lower() if category else None
        return tuple(record for record in self.records()
                     if (wanted is None or record.manifest.category == wanted)
                     and record.matches(text))

    # -- authoring ---------------------------------------------------------

    def save_preset(self, *, name: str, category: str, scene_data: Mapping[str, Any],
                    description: str = "", tags: Sequence[str] = (),
                    root: Path | None = None,
                    render_preview: bool = True) -> PresetRecord:
        """Write an editor-built scene as a preset folder (with GIF preview).

        AUD-28321: the scene is parsed and validated *before* anything is
        written, ``scene.json`` lands before ``preset.json`` (the manifest is
        the commit marker a rescan keys on), and any write failure cleans the
        folder up — an invalid or half-written preset can no longer persist
        as a "ghost" that scans fine but fails to insert.

        Args:
            name / category / description / tags: manifest fields.
            scene_data: full scene document dict (validated before write).
            root: target library root (default: first writable root).
            render_preview: render ``preview.gif`` synchronously before
                returning (default True).

        Thread affinity (AUD-28323): ``render_preview=True`` rasterizes up
        to 24 frames at the scene's native resolution on the *calling*
        thread — seconds for large scenes. Pure CPU (no OpenGL), but it
        must not run inside a UI slot: UI callers should pass
        ``render_preview=False`` and schedule
        :class:`.preview.PresetGifPreviewJob` after the save, or invoke
        this method from a worker thread. A GUI-thread call with
        ``render_preview=True`` logs a warning so regressions stay visible.
        """
        if render_preview:
            _warn_if_gui_thread(preset_save_hint=name)
        name = str(name).strip() or "Untitled Preset"
        category = normalize_category(category or "general")
        payload = dict(scene_data)
        try:
            scene = TeachingSceneDocument.from_dict(payload)
        except (TypeError, ValueError) as exc:
            raise PresetError(f"scene data is not a parseable scene document: {exc}") from exc
        result = validate_scene(scene)
        if not result.is_valid:
            details = "; ".join(issue.message for issue in result.errors[:4])
            raise PresetError(f"scene data failed validation: {details}")
        scene_payload = json.dumps(payload, ensure_ascii=False, indent=2)
        if len(scene_payload.encode("utf-8")) > _MAX_SCENE_BYTES:
            raise PresetError("scene.json exceeds 8 MiB")
        target_root = Path(root) if root is not None else self._writable_root()
        preset_id = f"{slugify(name)}_{uuid.uuid4().hex[:6]}"
        folder = target_root / category / preset_id
        if folder.exists():
            raise PresetError(f"preset folder already exists: {folder}")
        manifest = PresetManifest(preset_id=preset_id, name=name, category=category,
                                  description=str(description), tags=tuple(tags),
                                  entry=PRESET_ENTRY_SCENE)
        folder.mkdir(parents=True)
        try:
            (folder / PRESET_ENTRY_SCENE).write_text(scene_payload, encoding="utf-8")
            (folder / "preset.json").write_text(
                json.dumps(manifest.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError:
            shutil.rmtree(folder, ignore_errors=True)
            raise
        try:
            if render_preview:
                render_preset_gif(scene, folder / manifest.preview)
        except Exception as exc:  # noqa: BLE001 — a failed preview never blocks saving
            _logger.warning("preset preview rendering failed for %s: %s", preset_id, exc)
        record = PresetRecord(manifest, folder, target_root.name)
        self._records[manifest.preset_id] = record
        return record

    def _writable_root(self) -> Path:
        for root in reversed(self._roots):
            if root == self._roots[0] and len(self._roots) > 1:
                continue  # keep the bundled root read-only when a user root exists
            try:
                root.mkdir(parents=True, exist_ok=True)
                probe = root / ".write_test"
                probe.touch()
                probe.unlink()
                return root
            except OSError:
                continue
        raise PresetError("no writable preset library root")

    def delete(self, preset_id: str) -> bool:
        record = self._records.get(preset_id)
        if record is None:
            return False
        bundled = self._roots[0].resolve()
        if len(self._roots) > 1 and record.folder.resolve().is_relative_to(bundled):
            raise PresetError(
                "bundled presets cannot be deleted from the app; delete the folder on disk instead")
        shutil.rmtree(record.folder)
        self._records.pop(preset_id, None)
        return True

    # -- migration ---------------------------------------------------------

    def import_from(self, source: Path, *, root: Path | None = None) -> PresetRecord:
        """Import a preset folder or ``.zip`` archive into the library.

        Zips may contain one preset folder, a ``category/preset`` tree, or the
        flat preset files at the archive root.  Entries with absolute paths or
        ``..`` segments are rejected.
        """
        source = Path(source)
        with tempfile.TemporaryDirectory(prefix="topos_mg_import_") as temp:
            temp_dir = Path(temp)
            if source.is_dir():
                candidates = [source]
            elif zipfile.is_zipfile(source):
                candidates = list(self._extract_zip(source, temp_dir))
            else:
                raise PresetError(f"not a preset folder or zip archive: {source.name}")
            imported = None
            for candidate in candidates:
                imported = self._import_folder(candidate, root=root) or imported
            if imported is None:
                raise PresetError(f"no valid preset package found in {source.name}")
            return imported

    def _extract_zip(self, archive: Path, temp_dir: Path) -> Iterator[Path]:
        with zipfile.ZipFile(archive, "r") as bundle:
            names = bundle.namelist()
            for name in names:
                candidate = Path(name)
                if candidate.is_absolute() or ".." in candidate.parts:
                    raise PresetError(f"unsafe zip entry: {name}")
            total = sum(info.file_size for info in bundle.infolist()
                        if not info.is_dir())
            if total > 4 * _MAX_PREVIEW_BYTES:
                raise PresetError("preset archive exceeds 128 MiB")
            bundle.extractall(temp_dir)
        found: list[Path] = []
        if (temp_dir / "preset.json").is_file() or (temp_dir / PRESET_ENTRY_CODE).is_file():
            found.append(temp_dir)
            return iter(found)
        for preset_json in sorted(temp_dir.rglob("preset.json")):
            found.append(preset_json.parent)
        for build_py in sorted(temp_dir.rglob(PRESET_ENTRY_CODE)):
            folder = build_py.parent
            if all(folder != other for other in found):
                found.append(folder)
        return iter(found)

    def _import_folder(self, folder: Path, *, root: Path | None) -> PresetRecord | None:
        try:
            manifest = _read_manifest(folder)
            if manifest is None:
                return None
            entry = folder / manifest.entry
            if not entry.is_file():
                raise PresetError(f"missing entry file {manifest.entry}")
            if entry.stat().st_size > (256 * 1024 if manifest.entry == PRESET_ENTRY_CODE
                                       else _MAX_SCENE_BYTES):
                raise PresetError("preset entry file too large")
            preview = folder / manifest.preview
            if preview.is_file() and preview.stat().st_size > _MAX_PREVIEW_BYTES:
                raise PresetError("preview.gif exceeds 32 MiB")
        except PresetError:
            return None
        target_root = Path(root) if root is not None else self._writable_root()
        original_id = manifest.preset_id
        preset_id = manifest.preset_id
        category = normalize_category(manifest.category)
        target = target_root / category / preset_id
        while target.exists():
            preset_id = f"{manifest.preset_id}_{uuid.uuid4().hex[:4]}"
            target = target_root / category / preset_id
        manifest = replace(manifest, preset_id=preset_id, category=category)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(folder, target)
        if preset_id != original_id:
            # AUD-28320: the copied package still carries the conflicting id,
            # so a rescan would key the record under the old preset_id —
            # shadowing the existing preset and making ``get(new_id)`` miss
            # (fake "failed to rescan" while the files sit on disk).  Rewrite
            # the manifest under the deduplicated id; for code presets
            # ``preset.json`` (preferred by the scanner over build.py's
            # PRESET) is created carrying ``entry=build.py``.
            (target / "preset.json").write_text(
                json.dumps(manifest.to_dict(), ensure_ascii=False, indent=2),
                encoding="utf-8")
        self.rescan()
        record = self.get(preset_id)
        if record is None:
            raise PresetError(f"imported preset failed to rescan: {preset_id}")
        return record

    def export_to(self, target: Path | str, preset_ids: Sequence[str]) -> Path:
        """Bundle one or more presets as ``category/preset/…`` zip for sharing."""
        target = Path(target)
        if target.suffix.lower() != ".zip":
            target = target.with_suffix(".zip")
        missing = [preset_id for preset_id in preset_ids if preset_id not in self._records]
        if missing:
            raise PresetError(f"unknown presets: {', '.join(missing)}")
        target.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as bundle:
            for preset_id in preset_ids:
                record = self._records[preset_id]
                prefix = record.manifest.category
                for file in sorted(record.folder.rglob("*")):
                    if file.is_file():
                        bundle.write(file, f"{prefix}/{record.preset_id}/{file.relative_to(record.folder)}")
        return target


__all__ = ["MgPresetLibrary", "PresetError", "PresetRecord"]
