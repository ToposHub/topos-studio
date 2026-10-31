"""R-05/R-06: three-tier template package store (bundled / user / project).

The unified catalog used to be read-only over the Python package's bundled
templates: users had to edit the wheel directory to add one.  This module
gives HTML template packages the same product loop presets already have:

  * three discoverable origins — ``bundled`` (read-only, ships with the
    app), ``user`` (per-machine, under the app-data directory) and
    ``project`` (``<project>/.topos/motion_graphic_templates``, travels
    with the project);
  * same-id conflict policy: project overrides user, user overrides
    bundled — the winner carries its real origin;
  * import (folder / zip), export (zip), remove and open-in-Finder, all
    validated up front and written atomically (temp dir + rename) so a
    failed write never leaves a half package behind;
  * ``bundled`` roots are strictly read-only; every mutator refuses them.

All operations raise :class:`TemplateStoreError` with an actionable
message instead of silently no-op'ing, so the UI can surface them.
"""

from __future__ import annotations

import os
import re
import shutil
import tempfile
import uuid
import zipfile
from dataclasses import dataclass
from pathlib import Path

from .motion_graphic_template_library import (
    MotionGraphicTemplatePackage,
    default_motion_graphic_template_root,
    load_motion_graphic_template_package,
)

ORIGIN_BUNDLED = "bundled"
ORIGIN_USER = "user"
ORIGIN_PROJECT = "project"
# First origin wins: project-local customisation beats the user library,
# which beats what shipped with the app.
_ORIGIN_PRECEDENCE = (ORIGIN_PROJECT, ORIGIN_USER, ORIGIN_BUNDLED)


class TemplateStoreError(ValueError):
    """Raised for invalid packages, path escapes or read-only origins."""


# A template id becomes a directory name directly under the origin root
# (``root / template_id``), so it must be a single safe path segment: no
# separators, no ``..`` traversal, and never ``.``/``..`` themselves
# (AUD-27704 — a hostile manifest id must not write outside the root, and
# ``overwrite=True`` must not rmtree a directory outside it).
_SAFE_TEMPLATE_ID = re.compile(r"^[A-Za-z0-9_.-]+$")

#: Zip import pre-extraction ceilings (AUD-27706): entry count and total
#: uncompressed size, enforced before ``extractall`` writes anything.
_MAX_ZIP_ENTRIES: int = 512
_MAX_ZIP_TOTAL_BYTES: int = 64 * 1024 * 1024


def _validated_template_id(template_id: str) -> str:
    """Return the id if it is a safe single path segment, else raise."""
    candidate = str(template_id).strip()
    if (
        not candidate
        or candidate in {".", ".."}
        or ".." in candidate
        or _SAFE_TEMPLATE_ID.fullmatch(candidate) is None
    ):
        raise TemplateStoreError(
            f"invalid template id {candidate!r}: ids may only contain letters, "
            "digits, '.', '_' and '-', and must not include '..'")
    return candidate


def _ensure_inside_root(root: Path, destination: Path) -> Path:
    """Assert ``destination`` resolves strictly below ``root`` (AUD-27704)."""
    root_resolved = root.resolve()
    destination_resolved = destination.resolve()
    if destination_resolved == root_resolved or root_resolved not in destination_resolved.parents:
        raise TemplateStoreError(
            f"template destination escapes the store root: {destination}")
    return destination


def user_template_root() -> Path:
    """Per-machine writable template root (app-data, never the wheel dir)."""
    try:
        from PyQt6.QtCore import QStandardPaths
        base = QStandardPaths.writableLocation(
            QStandardPaths.StandardLocation.AppDataLocation)
        if base:
            return Path(base) / "motion_graphic_templates"
    except Exception:  # noqa: BLE001 — store must work without Qt (tests/CLI)
        pass
    return Path.home() / ".topos-color" / "motion_graphic_templates"


def project_template_root(project_path: Path | str) -> Path:
    """Project-local template root that travels with the project files."""
    return Path(project_path) / ".topos" / "motion_graphic_templates"


@dataclass(frozen=True)
class StoredTemplatePackage:
    """One discovered package plus which origin it won."""

    package: MotionGraphicTemplatePackage
    origin: str
    root: Path

    @property
    def template_id(self) -> str:
        return self.package.template.template_id

    @property
    def writable(self) -> bool:
        return self.origin != ORIGIN_BUNDLED


def discover_stored_templates(
    project_path: Path | str | None = None,
    user_root: Path | None = None,
    bundled_root: Path | None = None,
) -> tuple[StoredTemplatePackage, ...]:
    """Merge the three origins with same-id precedence (project > user > bundled)."""
    roots: list[tuple[str, Path]] = []
    if project_path is not None:
        roots.append((ORIGIN_PROJECT, project_template_root(project_path)))
    roots.append((ORIGIN_USER, Path(user_root) if user_root is not None else user_template_root()))
    roots.append((ORIGIN_BUNDLED, Path(
        bundled_root) if bundled_root is not None else default_motion_graphic_template_root()))

    import logging
    logger = logging.getLogger(__name__)
    winners: dict[str, StoredTemplatePackage] = {}
    for origin, root in roots:
        if not root.is_dir():
            continue
        for child in sorted(root.iterdir(), key=lambda path: path.name.casefold()):
            if not child.is_dir() or child.name.startswith((".", "_")):
                continue
            try:
                package = load_motion_graphic_template_package(child)
            except (OSError, TypeError, ValueError) as exc:
                logger.warning("Ignoring invalid template package %s: %s", child, exc)
                continue
            template_id = package.template.template_id
            existing = winners.get(template_id)
            challenger = StoredTemplatePackage(package, origin, child)
            if existing is None or _ORIGIN_PRECEDENCE.index(origin) < _ORIGIN_PRECEDENCE.index(
                    existing.origin):
                winners[template_id] = challenger
    return tuple(sorted(winners.values(), key=lambda item: item.template_id.casefold()))


def validate_template_folder(source: Path | str) -> list[str]:
    """Actionable validation errors for a candidate package folder (empty = ok)."""
    source = Path(source)
    if not source.is_dir():
        return [f"not a folder: {source}"]
    try:
        load_motion_graphic_template_package(source)
    except (OSError, TypeError, ValueError) as exc:
        return [str(exc)]
    return []


def _replace_destination_atomically(tmp: Path, destination: Path) -> None:
    """Move a staged ``tmp`` directory onto ``destination`` (AUD-27705).

    An existing destination is renamed aside first and only deleted once the
    replacement is in place; if the swap fails midway the backup is restored,
    so a failed overwrite never destroys the previous template package.
    """
    backup: Path | None = None
    try:
        if destination.exists():
            backup = destination.with_name(f".{destination.name}.old-{uuid.uuid4().hex}")
            os.rename(destination, backup)
        os.rename(tmp, destination)
    except BaseException:
        if backup is not None and not destination.exists():
            os.rename(backup, destination)
        raise
    if backup is not None:
        shutil.rmtree(backup, ignore_errors=True)


def _atomic_copytree(source: Path, destination: Path, *, replace_existing: bool = False) -> None:
    """Copy a folder into place via a temp sibling + rename (no half packages)."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix=f".{destination.name}.tmp-", dir=str(destination.parent)))
    try:
        shutil.copytree(source, tmp, dirs_exist_ok=True)
        # mkdtemp created the leaf; copytree filled it.  Renaming onto an
        # occupied slot needs an explicit swap policy.
        if destination.exists() and not replace_existing:
            raise TemplateStoreError(f"destination already exists: {destination}")
        _replace_destination_atomically(tmp, destination)
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise


def import_template_folder(
    source: Path | str,
    origin_root: Path | str,
    *,
    origin: str = ORIGIN_USER,
    overwrite: bool = False,
) -> StoredTemplatePackage:
    """Validate and copy a template folder into a writable origin root."""
    source = Path(source)
    root = Path(origin_root)
    if (origin == ORIGIN_BUNDLED
            or root.resolve() == default_motion_graphic_template_root().resolve()):
        raise TemplateStoreError("the bundled template library is read-only; "
                                 "import into the user or project library instead")
    errors = validate_template_folder(source)
    if errors:
        raise TemplateStoreError("; ".join(errors))
    package = load_motion_graphic_template_package(source)
    template_id = _validated_template_id(package.template.template_id or source.name)
    destination = root / template_id
    _ensure_inside_root(root, destination)
    if destination.exists() and not overwrite:
        raise TemplateStoreError(
            f"a template named {template_id!r} already exists in {root}; "
            "pass overwrite=True to replace it")
    # AUD-27705: overwrite swaps the staged copy into place (old package is
    # renamed aside and only deleted after the replacement lands) instead of
    # rmtree-ing it up front where a failed copy would lose the original.
    _atomic_copytree(source, destination, replace_existing=overwrite)
    return StoredTemplatePackage(
        load_motion_graphic_template_package(destination),
        origin,
        destination,
    )


def import_template_zip(
    archive_path: Path | str,
    origin_root: Path | str,
    *,
    origin: str = ORIGIN_USER,
    overwrite: bool = False,
) -> StoredTemplatePackage:
    """Import a template packaged as a zip (expects the package at zip root)."""
    archive = Path(archive_path)
    if not archive.is_file():
        raise TemplateStoreError(f"not a file: {archive}")
    staging = Path(tempfile.mkdtemp(prefix="topos_template_import_"))
    try:
        with zipfile.ZipFile(archive) as zf:
            infos = zf.infolist()
            # AUD-27706: the per-file size caps in the template library only
            # apply after extraction; without a total ceiling a high-ratio
            # (malicious or corrupted) zip could fill the disk via extractall
            # before any validation runs.
            if len(infos) > _MAX_ZIP_ENTRIES:
                raise TemplateStoreError(
                    f"zip archive has too many entries: {len(infos)} > {_MAX_ZIP_ENTRIES}")
            total_uncompressed = sum(info.file_size for info in infos)
            if total_uncompressed > _MAX_ZIP_TOTAL_BYTES:
                raise TemplateStoreError(
                    f"zip archive expands to {total_uncompressed} bytes, over the "
                    f"{_MAX_ZIP_TOTAL_BYTES} byte limit")
            for info in infos:
                target = (staging / info.filename).resolve()
                if staging.resolve() not in target.parents:
                    raise TemplateStoreError(f"unsafe zip entry: {info.filename}")
            zf.extractall(staging)
        # Accept both "zip root == package root" and "one wrapping folder".
        manifest = staging / "manifest.json"
        folder = staging
        if not manifest.is_file():
            children = [child for child in staging.iterdir() if child.is_dir()]
            if len(children) == 1 and (children[0] / "manifest.json").is_file():
                folder = children[0]
        return import_template_folder(folder, origin_root, origin=origin,
                                        overwrite=overwrite)
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def export_template_zip(package_root: Path | str, destination: Path | str) -> Path:
    """Zip one package folder for sharing (manifest at the zip root)."""
    root = Path(package_root)
    errors = validate_template_folder(root)
    if errors:
        raise TemplateStoreError("; ".join(errors))
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    tmp = destination.with_suffix(destination.suffix + ".tmp")
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(root.rglob("*")):
            if path.is_file():
                zf.write(path, path.relative_to(root))
    os.replace(tmp, destination)
    return destination


def remove_template(stored: StoredTemplatePackage) -> None:
    """Delete a template package from disk; bundled templates are protected."""
    if not stored.writable:
        raise TemplateStoreError(
            f"bundled template {stored.template_id!r} cannot be deleted; "
            "override it with a user/project template instead")
    shutil.rmtree(stored.root)


def open_template_directory(path: Path | str) -> bool:
    """Reveal a template directory in the desktop file manager."""
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True)
    try:
        from PyQt6.QtCore import QUrl
        from PyQt6.QtGui import QDesktopServices
        return QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))
    except Exception:  # noqa: BLE001 — headless contexts
        return False


def ensure_user_template_root() -> Path:
    """Create (once) and return the user template root."""
    root = user_template_root()
    root.mkdir(parents=True, exist_ok=True)
    return root


__all__ = [
    "ORIGIN_BUNDLED", "ORIGIN_PROJECT", "ORIGIN_USER",
    "StoredTemplatePackage", "TemplateStoreError",
    "discover_stored_templates", "ensure_user_template_root",
    "export_template_zip", "import_template_folder", "import_template_zip",
    "open_template_directory", "project_template_root",
    "remove_template", "user_template_root", "validate_template_folder",
]
