"""Video-mode project library scanning and metadata.

The Project Manager (video mode only) scans a fixed library directory for
.topos archives and presents them as cards. This module provides the data
layer: directory resolution, lightweight metadata reads (no full project
load) and cover-thumbnail extraction.

The image workflow also uses the lightweight metadata reader to establish
which adjacent .topos archive belongs to a particular source.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import tempfile
import time
import uuid
import zipfile
import datetime
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

#: Default library location when the config value is empty.
DEFAULT_PROJECT_LIBRARY_NAME = "ToposProjects"
PROJECT_LIBRARY_DIRECTORIES_KEY = "project_library_directories"
_EXCLUDED_LIBRARIES_KEY = "excluded_project_library_directories"
PROJECT_BACKUP_DIRECTORY_NAME = "backups"

#: Filename of the project cover thumbnail inside the archive.
COVER_THUMBNAIL_PATH = "assets/thumbnails/project_cover.jpg"


def normalize_project_name(value: str | None, fallback: str = "Untitled Project") -> str:
    """Return a clean display/file stem without repeated .topos suffixes."""
    name = Path(str(value or "").strip()).name
    while name.lower().endswith(".topos"):
        name = name[:-len(".topos")].rstrip()
    name = name.strip(" .")
    return name or fallback


def ensure_project_path(value: str | Path) -> str:
    """Normalize a user-selected path to exactly one .topos suffix."""
    path = Path(value).expanduser()
    stem = normalize_project_name(path.stem or path.name)
    return str(path.with_name(f"{stem}.topos"))


@dataclass(frozen=True)
class ProjectMeta:
    """Lightweight metadata for a .topos archive, read without full loading."""

    path: str
    name: str
    project_mode: str
    created_at: float
    modified_at: float
    file_modified_at: float  # filesystem mtime (for sorting when internal is 0)
    preview_path: str | None = None  # first source asset used for the project card


@dataclass(frozen=True)
class ProjectBackupMeta:
    """Metadata for a timestamped project backup."""

    path: str
    project_path: str
    created_at: float


@dataclass(frozen=True)
class ProjectLibraryLocation:
    path: str
    available: bool


def project_library_key(path: str | Path) -> str:
    return os.path.normcase(os.path.abspath(Path(path).expanduser()))


def get_project_library_dir(config: Any = None, *, create: bool = True) -> Path:
    """Return the primary library and create it when requested."""
    custom = config.get("project_library_directory") if config is not None else None
    directory = Path.home() / "Documents" / DEFAULT_PROJECT_LIBRARY_NAME
    if isinstance(custom, (str, Path)) and custom and "\x00" not in str(custom):
        directory = Path(custom).expanduser()
    if create:
        try:
            directory.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            logger.warning("Could not create project library dir %s: %s", directory, exc)
    return directory


def _configured_library_keys(config: Any, key: str) -> list[str]:
    values = config.get(key) if config is not None else None
    if not isinstance(values, (list, tuple)):
        return []
    return [project_library_key(path) for path in values if isinstance(path, str) and path and "\x00" not in path]


def get_project_library_dirs(config: Any = None) -> list[Path]:
    """Include configured and recent locations until explicitly removed."""
    default = get_project_library_dir(config, create=False)
    configured = _configured_library_keys(config, PROJECT_LIBRARY_DIRECTORIES_KEY)
    excluded = set(_configured_library_keys(config, _EXCLUDED_LIBRARIES_KEY))
    recent = _configured_library_keys(config, "recent_projects")
    locations = [*configured, *(str(Path(path).parent) if Path(path).suffix.lower() == ".topos" else path
                                for path in recent)]
    primary = project_library_key(default)
    return [Path(primary), *(Path(path) for path in dict.fromkeys(locations)
                             if path != primary and path not in excluded)]


def get_project_backup_dir(
    project_path: str | Path, config: Any = None, *, create: bool = True
) -> Path:
    """Return the single archive directory used for project backups.

    An explicitly configured backup directory is shared by all projects. When
    it is empty, backups are kept in ``<project-library>/backups`` so they do
    not appear as projects in the library browser.
    """
    configured = ""
    if config is not None:
        try:
            configured = str(config.get("backup_directory") or "").strip()
        except Exception:
            configured = ""
    if configured:
        directory = Path(configured).expanduser()
    else:
        directory = Path(project_path).expanduser().parent / PROJECT_BACKUP_DIRECTORY_NAME
    # AUD-36806: mkdir failures (read-only volume, permission, name clash with a
    # regular file) must not propagate into scan/list/rename call sites. Callers
    # already tolerate a missing backup directory (iterdir/stat are guarded),
    # so log-and-continue mirrors get_project_library_dir().
    if create:
        try:
            directory.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            logger.warning("Could not create project backup dir %s: %s", directory, exc)
    return directory


def _backup_name_pattern(project_path: str | Path) -> re.Pattern[str]:
    stem = Path(project_path).stem
    return re.compile(
        rf"^{re.escape(stem)}_(?:legacy_)?(?P<stamp>\d{{8}}_\d{{6}})(?:_[A-Za-z0-9-]+)*\.topos$",
        re.IGNORECASE,
    )


def _next_backup_path(backup_dir: Path, project_stem: str, suffix: str = "") -> Path:
    """Return a collision-free, listable timestamped archive path."""
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    cleaned_suffix = re.sub(r"[^A-Za-z0-9_-]+", "_", suffix).strip("_")
    name = f"{project_stem}_{timestamp}"
    if cleaned_suffix:
        name = f"{name}_{cleaned_suffix}"
    candidate = backup_dir / f"{name}.topos"
    counter = 2
    while candidate.exists():
        candidate = backup_dir / f"{name}_{counter:06d}.topos"
        counter += 1
    return candidate


def _archive_current_project(project: Path, backup_dir: Path, purpose: str = "") -> Path:
    """Create a recoverable snapshot before a destructive replacement."""
    if not project.exists() or not project.is_file():
        raise FileNotFoundError(project)
    destination = _next_backup_path(backup_dir, project.stem, purpose)
    temporary = destination.with_suffix(".topos.tmp")
    try:
        shutil.copy2(project, temporary)
        os.replace(temporary, destination)
    except Exception:
        try:
            temporary.unlink()
        except OSError:
            pass
        raise
    return destination


def _migrate_legacy_backup(project_path: Path, backup_dir: Path) -> None:
    """Move the old adjacent ``.topos.bak`` file into the archive folder."""
    legacy = Path(f"{project_path}.bak")
    if not legacy.exists() or not legacy.is_file():
        return
    try:
        timestamp = datetime.datetime.fromtimestamp(legacy.stat().st_mtime).strftime(
            "%Y%m%d_%H%M%S"
        )
        destination = backup_dir / f"{project_path.stem}_legacy_{timestamp}.topos"
        suffix = 2
        while destination.exists():
            destination = backup_dir / (
                f"{project_path.stem}_legacy_{timestamp}_{suffix:02d}.topos"
            )
            suffix += 1
        shutil.move(str(legacy), str(destination))
        logger.info("Migrated legacy project backup %s to %s", legacy, destination)
    except OSError as exc:
        logger.warning("Could not migrate legacy project backup %s: %s", legacy, exc)


def _read_project_id(path: Path) -> str:
    """Read a stable project id from an archive without loading the project."""
    try:
        with zipfile.ZipFile(path, "r") as archive:
            manifest = _read_zip_json(archive, "manifest.json") or {}
            project = _read_zip_json(archive, "project.json") or {}
    except (zipfile.BadZipFile, OSError):
        return ""
    return str(project.get("project_id") or manifest.get("project_id") or "")


def _migrate_renamed_backups(project_path: Path, backup_dir: Path) -> None:
    """Recover archives left behind by project renames from older builds."""
    project_id = _read_project_id(project_path)
    if not project_id:
        return
    pattern = _backup_name_pattern(project_path)
    try:
        entries = tuple(backup_dir.iterdir())
    except OSError:
        return
    for entry in entries:
        if not entry.is_file() or entry.suffix.lower() != ".topos" or pattern.match(entry.name):
            continue
        if _read_project_id(entry) != project_id:
            continue
        try:
            destination = _next_backup_path(backup_dir, project_path.stem, "renamed")
            os.replace(entry, destination)
            logger.info("Recovered renamed project backup %s as %s", entry, destination)
        except OSError as exc:
            logger.warning("Could not migrate renamed backup %s: %s", entry, exc)


def list_project_backups(
    project_path: str | Path,
    config: Any = None,
) -> list[ProjectBackupMeta]:
    """List timestamped backups for one project, newest first.

    The legacy sidecar backup is migrated on first inspection so old projects
    also follow the new single-folder layout.
    """
    project = Path(project_path).expanduser()
    backup_dir = get_project_backup_dir(project, config)
    _migrate_legacy_backup(project, backup_dir)
    _migrate_renamed_backups(project, backup_dir)
    pattern = _backup_name_pattern(project)
    project_id = _read_project_id(project)
    results: list[ProjectBackupMeta] = []
    try:
        # AUD-36806: iterdir() is lazy — materialize inside the try so a
        # missing/unusable backup directory cannot raise from the loop below.
        entries = tuple(backup_dir.iterdir())
    except OSError:
        return results
    for entry in entries:
        match = pattern.match(entry.name)
        if not entry.is_file() or match is None:
            continue
        # A custom backup directory can be shared by projects from different
        # libraries. Filename stems alone are not an ownership boundary:
        # two users can both create "Untitled Project". When IDs are present,
        # they are the authoritative association and prevent cross-project
        # restore/delete actions.
        backup_project_id = _read_project_id(entry)
        if project_id and backup_project_id and backup_project_id != project_id:
            continue
        try:
            created_at = datetime.datetime.strptime(match["stamp"], "%Y%m%d_%H%M%S").timestamp()
        except (ValueError, OSError, OverflowError):
            try:
                created_at = entry.stat().st_mtime
            except OSError:
                continue
        results.append(ProjectBackupMeta(str(entry), str(project), created_at))
    results.sort(key=lambda item: (item.created_at, item.path), reverse=True)
    return results


def restore_project_backup(
    project_path: str | Path,
    backup_path: str | Path,
    config: Any = None,
) -> bool:
    """Restore a selected backup, first preserving the current project state."""
    project = Path(project_path).expanduser()
    backup = Path(backup_path).expanduser()
    if not backup.exists() or not backup.is_file() or not zipfile.is_zipfile(backup):
        return False
    if not project.parent.exists():
        project.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{project.stem}.restore.", suffix=".topos.tmp", dir=str(project.parent)
    )
    os.close(fd)
    try:
        # Keep the filename and archive's displayed project name aligned.
        _rewrite_project_archive(backup, temporary_name, name=project.stem)
        if project.exists():
            snapshot = _archive_current_project(
                project, get_project_backup_dir(project, config), purpose="before_restore"
            )
            logger.info("Created pre-restore backup %s", snapshot)
        os.replace(temporary_name, project)
        logger.info("Restored project %s from backup %s", project, backup)
        return True
    except (OSError, RuntimeError, ValueError, zipfile.BadZipFile) as exc:
        # AUD-36809: a backup that passed the is_zipfile precheck can still
        # turn out truncated/corrupt once actually read (TOCTOU) — that
        # surfaces as zipfile.BadZipFile, which is not an OSError. Without
        # catching it here, both the exception and our own mkstemp temp
        # file leak into the user's project directory.
        logger.error("Could not restore project backup %s: %s", backup, exc)
        try:
            os.remove(temporary_name)
        except OSError:
            pass
        return False


def register_project_library_dir(
    config: Any, directory: str | Path, *, explicit: bool = False
) -> None:
    """Remember a project location, respecting explicit library exclusions."""
    if config is None:
        return
    path = project_library_key(directory)
    current = _configured_library_keys(config, PROJECT_LIBRARY_DIRECTORIES_KEY)
    excluded = _configured_library_keys(config, _EXCLUDED_LIBRARIES_KEY)
    if path in excluded:
        if not explicit:
            return
        config.set(_EXCLUDED_LIBRARIES_KEY, [item for item in excluded if item != path], auto_save=False)
    if path not in current:
        config.set(PROJECT_LIBRARY_DIRECTORIES_KEY, [*current, path], auto_save=True)
    elif path in excluded:
        config.set(_EXCLUDED_LIBRARIES_KEY, [item for item in excluded if item != path], auto_save=True)


def remove_project_library_dir(config: Any, directory: str | Path) -> None:
    """Remove a location from the browser while leaving files and recents intact."""
    path = project_library_key(directory)
    if path == project_library_key(get_project_library_dir(config, create=False)):
        raise ValueError("The primary project library cannot be removed")
    current = _configured_library_keys(config, PROJECT_LIBRARY_DIRECTORIES_KEY)
    excluded = _configured_library_keys(config, _EXCLUDED_LIBRARIES_KEY)
    config.set(PROJECT_LIBRARY_DIRECTORIES_KEY, [item for item in current if item != path], auto_save=False)
    config.set(_EXCLUDED_LIBRARIES_KEY, list(dict.fromkeys([*excluded, path])), auto_save=True)


def _read_zip_json(zf: zipfile.ZipFile, member: str) -> Optional[dict]:
    """Read a JSON member from an open zip, returning None on any error."""
    try:
        with zf.open(member) as fh:
            value = json.load(fh)
            return value if isinstance(value, dict) else None
    except (KeyError, UnicodeError, json.JSONDecodeError, OSError, RuntimeError):
        return None


def _archive_float(value: Any, fallback: float) -> float:
    """Coerce an archive-supplied timestamp to float, never raising.

    AUD-36805: manifest/project JSON may carry malformed timestamps
    (strings, dicts, NaN text). A single bad value must degrade to the
    fallback instead of failing the whole library scan.
    """
    try:
        return float(value)
    except (TypeError, ValueError):
        return fallback


def read_project_meta(path: str, *, require_manifest_mode: bool = False) -> Optional[ProjectMeta]:
    """Read a .topos archive's metadata without a full load.

    Opens the zip and reads only manifest.json + project.json. Returns None
    if the archive is malformed. Mirrors the lightweight classifier pattern
    used elsewhere (welcome_dialog / file_controller).
    """
    try:
        with zipfile.ZipFile(path, "r") as zf:
            manifest = _read_zip_json(zf, "manifest.json") or {}
            if require_manifest_mode and (not isinstance(manifest, dict) or not manifest.get("project_mode")):
                return None
            project = _read_zip_json(zf, "project.json") or {}
    except (zipfile.BadZipFile, OSError) as exc:
        logger.debug("Cannot read project meta from %s: %s", path, exc)
        return None

    project_mode = manifest.get("project_mode", "standalone_image")
    if project_mode == "single_asset":
        project_mode = "standalone_image"
    name = project.get("name") or os.path.splitext(os.path.basename(path))[0]
    try:
        file_mtime = os.path.getmtime(path)
    except OSError:
        file_mtime = 0.0
    preview_path = None
    project_dir = Path(path).parent
    assets = project.get("assets")
    for asset in assets.values() if isinstance(assets, dict) else ():
        if not isinstance(asset, dict):
            continue
        relative = asset.get("path_relative")
        if isinstance(relative, str) and relative and "\x00" not in relative:
            candidate = project_dir / relative
            if not Path(relative).is_absolute() and os.path.isfile(candidate):
                preview_path = str(candidate)
                break
        asset_path = asset.get("path") or relative
        if not asset_path:
            continue
        candidate = Path(str(asset_path))
        if not candidate.is_absolute():
            candidate = project_dir / candidate
        preview_path = str(candidate)
        break

    return ProjectMeta(
        path=path,
        name=name,
        project_mode=project_mode,
        # AUD-36805: float() conversion must stay defensive — a malformed
        # timestamp in one archive must not abort the library scan.
        created_at=_archive_float(
            manifest.get("created_at", 0.0) or project.get("created_at", 0.0), 0.0
        ),
        modified_at=_archive_float(
            manifest.get("modified_at", 0.0) or file_mtime, file_mtime
        ),
        file_modified_at=file_mtime,
        preview_path=preview_path,
    )


def inspect_project_media(path: str) -> tuple[int, tuple[str, ...]]:
    """Check the media index without extracting the project or touching its cache."""
    from .resource_resolver import RESOURCE_INDEX_ARCHIVE_PATH, ResourceIndex
    from .topos_project import is_virtual_asset_path

    with zipfile.ZipFile(path) as archive:
        members = {info.filename: info for info in archive.infolist()}
        if "edit/media_pool.json" in members:
            assets = _read_zip_json(archive, "edit/media_pool.json")
        else:
            project = _read_zip_json(archive, "project.json")
            assets = project.get("assets", {}) if project is not None else None
        if not isinstance(assets, dict) or any(not isinstance(asset, dict) for asset in assets.values()):
            raise ValueError("Invalid project media index")
        resources = ResourceIndex.from_dict(_read_zip_json(archive, RESOURCE_INDEX_ARCHIVE_PATH))
        directory = Path(path).parent
        missing = []
        for asset_id, asset in assets.items():
            source = asset.get("path")
            uri = asset.get("resource_uri")
            if is_virtual_asset_path(source):
                continue
            entry = resources.by_uri(uri) if uri else resources.for_asset(asset_id)
            member = members.get(entry.archive_path) if entry is not None else None
            if entry is not None and member is not None and member.file_size == entry.size_bytes:
                continue
            candidates = (asset.get("path_relative"), asset.get("path_original"), source)
            if any(
                isinstance(value, str) and value and "\x00" not in value
                and (directory / value).is_file()
                for value in candidates
            ):
                continue
            missing.append(str(asset.get("display_name") or source or uri or asset_id))
        return len(assets), tuple(missing)


def is_video_project(meta: ProjectMeta) -> bool:
    """A project is a video project unless it is a standalone_image archive."""
    return meta.project_mode != "standalone_image"


def scan_video_projects(config: Any = None) -> list[ProjectMeta]:
    """Return video projects across all enabled libraries, newest first."""
    projects, _locations = scan_project_libraries(config)
    return projects


def scan_project_libraries(
    config: Any = None,
) -> tuple[list[ProjectMeta], list[ProjectLibraryLocation]]:
    """Read projects and report which registered directories could be listed."""
    results: list[ProjectMeta] = []
    locations: list[ProjectLibraryLocation] = []
    seen_paths: set[str] = set()
    if config is None or not config.get("project_library_directory"):
        get_project_library_dir(config)
    for lib_dir in get_project_library_dirs(config):
        try:
            entries = list(lib_dir.iterdir())
        except OSError as exc:
            logger.warning("Cannot scan project library %s: %s", lib_dir, exc)
            locations.append(ProjectLibraryLocation(str(lib_dir), False))
            continue
        locations.append(ProjectLibraryLocation(str(lib_dir), True))
        backup_dir = get_project_backup_dir(lib_dir / "__library__.topos", config, create=False)
        if project_library_key(lib_dir) == project_library_key(backup_dir):
            continue
        for entry in entries:
            if entry.suffix.lower() != ".topos" or entry.stem.lower().endswith(".topos") or not entry.is_file():
                continue
            key = project_library_key(entry)
            if key in seen_paths:
                continue
            seen_paths.add(key)
            meta = read_project_meta(os.path.abspath(entry))
            if meta is not None and is_video_project(meta):
                results.append(meta)
    results.sort(key=lambda m: (m.modified_at or m.file_modified_at or 0.0), reverse=True)
    return results, locations


def extract_cover_thumbnail(path: str) -> Optional[bytes]:
    """Return the raw bytes of a project's cover thumbnail, or None.

    The bytes are a JPEG image stored at ``assets/thumbnails/project_cover.jpg``
    inside the archive. Callers wrap them into a QPixmap/ QImage.
    """
    try:
        with zipfile.ZipFile(path, "r") as zf:
            with zf.open(COVER_THUMBNAIL_PATH) as fh:
                return fh.read()
    except (KeyError, zipfile.BadZipFile, OSError):
        return None


def _rewrite_project_archive(
    source: str | Path,
    destination: str | Path,
    *,
    name: str | None = None,
    project_id: str | None = None,
) -> None:
    """Rewrite only project metadata while preserving every archive member."""
    source_path = Path(source)
    destination_path = Path(destination)
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{destination_path.stem}.", suffix=".topos.tmp", dir=str(destination_path.parent)
    )
    os.close(fd)
    try:
        with zipfile.ZipFile(source_path, "r") as source_zip, zipfile.ZipFile(
            temporary_name, "w", compression=zipfile.ZIP_DEFLATED
        ) as destination_zip:
            metadata = {
                member: _read_zip_json(source_zip, member)
                for member in ("manifest.json", "project.json")
            }
            if any(value is None for value in metadata.values()):
                raise ValueError("Invalid project archive metadata")
            for info in source_zip.infolist():
                if info.filename not in metadata:
                    with source_zip.open(info) as reader, destination_zip.open(
                        info, "w", force_zip64=info.file_size >= zipfile.ZIP64_LIMIT
                    ) as writer:
                        shutil.copyfileobj(reader, writer)
                    continue
                payload = metadata[info.filename]
                if info.filename == "project.json" and name is not None:
                    payload["name"] = name
                if project_id is not None:
                    payload["project_id"] = project_id
                if info.filename == "manifest.json":
                    payload["modified_at"] = time.time()
                destination_zip.writestr(info, json.dumps(payload, ensure_ascii=False, indent=2))
        os.replace(temporary_name, destination_path)
    except Exception:
        try:
            os.remove(temporary_name)
        except OSError:
            pass
        raise


def _move_related_project_files(source: Path, destination: Path) -> list[tuple[Path, Path]]:
    """Move sidecar/autosave/cache companions and return completed moves.

    AUD-36807: if any move fails mid-way, the already-completed moves are
    rolled back before the exception propagates, so a failed rename never
    leaves companions stranded under the new project name.
    """
    moved: list[tuple[Path, Path]] = []
    related = (
        (Path(f"{source}.bak"), Path(f"{destination}.bak")),
        (Path(f"{source}.autosave"), Path(f"{destination}.autosave")),
    )
    try:
        for old, new in related:
            if old.exists():
                os.replace(old, new)
                moved.append((old, new))
        old_cache = source.parent / ".topos_cache" / source.stem
        new_cache = destination.parent / ".topos_cache" / destination.stem
        if old_cache.exists():
            new_cache.parent.mkdir(parents=True, exist_ok=True)
            os.replace(old_cache, new_cache)
            moved.append((old_cache, new_cache))
    except OSError:
        _rollback_moves(moved)
        raise
    return moved


def _move_project_backups(
    source: Path,
    destination: Path,
    config: Any = None,
) -> list[tuple[Path, Path]]:
    """Rename every timestamped backup to follow a renamed project.

    AUD-36807: same partial-move protection as
    ``_move_related_project_files`` — completed renames are rolled back if
    any backup rename fails.
    """
    backup_dir = get_project_backup_dir(source, config)
    moves: list[tuple[Path, Path]] = []
    try:
        for meta in list_project_backups(source, config):
            old = Path(meta.path)
            suffix = old.name[len(source.stem):]
            target = backup_dir / f"{destination.stem}{suffix}"
            if target.exists():
                # A project with this name may have been deleted in the past and
                # left backup history behind. Keep both histories intact.
                target = _next_backup_path(backup_dir, destination.stem, "renamed")
            os.replace(old, target)
            moves.append((old, target))
    except OSError:
        _rollback_moves(moves)
        raise
    return moves


def _rollback_moves(moves: list[tuple[Path, Path]]) -> None:
    """Best-effort rollback used when a multi-file rename fails."""
    for old, new in reversed(moves):
        try:
            if new.exists():
                os.replace(new, old)
        except OSError:
            logger.exception("Could not roll back project companion move %s", new)


def rename_project(path: str | Path, new_name: str, config: Any = None) -> str:
    """Rename a project file and its internal display name atomically."""
    source = Path(path)
    clean_name = normalize_project_name(new_name)
    if not clean_name:
        raise ValueError("Project name cannot be empty")
    target = source.with_name(f"{clean_name}.topos")
    if target.exists() and os.path.abspath(str(target)) != os.path.abspath(str(source)):
        raise FileExistsError(str(target))
    if os.path.abspath(str(target)) == os.path.abspath(str(source)):
        _rewrite_project_archive(source, source, name=clean_name)
        return str(source)
    _rewrite_project_archive(source, target, name=clean_name)
    sidecar_moves: list[tuple[Path, Path]] = []
    backup_moves: list[tuple[Path, Path]] = []
    try:
        sidecar_moves = _move_related_project_files(source, target)
        backup_moves = _move_project_backups(source, target, config)
        source.unlink()
    except Exception:
        _rollback_moves(backup_moves)
        _rollback_moves(sidecar_moves)
        try:
            target.unlink()
        except OSError:
            pass
        raise
    return str(target)


def duplicate_project(
    path: str | Path, new_name: str | None = None, *, destination_directory: str | Path | None = None
) -> str:
    """Create an independent copy with a fresh project ID and a unique name."""
    source = Path(path)
    directory = source.parent if destination_directory is None else Path(destination_directory)
    base_name = normalize_project_name(new_name, fallback=f"{source.stem} Copy")
    index = 1
    while True:
        stem = base_name if index == 1 else f"{base_name} {index}"
        target = directory / f"{stem}.topos"
        try:
            with target.open("xb"):
                pass
            break
        except FileExistsError:
            index += 1
    try:
        _rewrite_project_archive(source, target, name=target.stem, project_id=str(uuid.uuid4()))
    except Exception:
        target.unlink(missing_ok=True)
        raise
    return str(target)


def _move_to_trash(target: Path) -> None:
    """Send one generated project artifact to the system trash if available."""
    try:
        from send2trash import send2trash  # type: ignore
    except ImportError:
        send2trash = None
    if send2trash is not None:
        send2trash(str(target))
    elif target.is_dir():
        shutil.rmtree(target)
    else:
        target.unlink()


def delete_project(path: str | Path, config: Any = None) -> list[str]:
    """Move a project, every archive backup, and generated companions to trash."""
    source = Path(path)
    targets: list[Path] = [Path(f"{source}.bak"), Path(f"{source}.autosave")]
    # list_project_backups also migrates the legacy sidecar into the archive,
    # ensuring it is covered by this single deletion path.
    targets.extend(Path(meta.path) for meta in list_project_backups(source, config))
    cache = source.parent / ".topos_cache" / source.stem
    if cache.exists():
        targets.append(cache)
    # Main project is deliberately last: if its generated companions cannot
    # be trashed, the editable project remains available to the user.
    targets.append(source)

    moved: list[str] = []
    for target in targets:
        if not target.exists():
            continue
        _move_to_trash(target)
        moved.append(str(target))
    return moved
