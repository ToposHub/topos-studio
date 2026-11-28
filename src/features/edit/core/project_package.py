"""Portable copies of saved video projects, without activating their resources."""

from __future__ import annotations

import hashlib
import json
import os
import posixpath
import re
import shutil
import tempfile
import zipfile
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, BinaryIO

from src.shared.project.payload_cache import RESOURCE_INDEX_ARCHIVE_PATH
from src.shared.project.resource_resolver import ResourceIndex, make_resource_entry
from src.shared.project.topos_project import is_virtual_asset_path

from .project_archive import _sanitize_name, _unique_dest_name

_MEDIA_POOL = "edit/media_pool.json"
_SEQUENCES = "edit/timeline_sequences.json"
_STACKS = "color/color_stacks.json"
_COMPOSITES = "composite/graphs.json"
_KNOWN_REFS = ("edit/comp_clips.json", "edit/timelines.json", _SEQUENCES,
               _STACKS, _COMPOSITES, "color/looks.json")
_FRAME_PATTERN = re.compile(r"(#+|%0[1-9][0-9]*d)")
_IO_CHUNK = 1024 * 1024


class ProjectPackageCancelled(Exception):
    """The caller cancelled before publication."""


@dataclass(frozen=True)
class PackagePlan:
    estimated_bytes: int
    missing: tuple[str, ...]
    dependencies: tuple[str, ...]


@dataclass(frozen=True)
class PackageResult:
    path: str
    missing: tuple[str, ...]


@dataclass
class _Inventory:
    documents: dict[str, dict[str, Any]] = field(default_factory=dict)
    media: dict[Path, str] = field(default_factory=dict)
    luts: dict[Path, str] = field(default_factory=dict)
    linked: dict[Path, str] = field(default_factory=dict)
    frames: dict[str, Path] = field(default_factory=dict)
    sequence_dirs: dict[tuple[Path, ...], str] = field(default_factory=dict)
    missing: dict[str, None] = field(default_factory=dict)
    dependencies: dict[str, None] = field(default_factory=dict)
    estimated_bytes: int = 0


def _checkpoint(cancelled: Callable[[], bool] | None) -> None:
    if cancelled is not None and cancelled():
        raise ProjectPackageCancelled()


def _signature(info: os.stat_result) -> tuple[int, int, int, int]:
    return info.st_dev, info.st_ino, info.st_mtime_ns, info.st_size


def _read_document(archive: zipfile.ZipFile, name: str, *, required: bool = False) -> dict[str, Any]:
    try:
        value = json.loads(archive.read(name))
    except KeyError:
        if required:
            raise ValueError(f"Missing project archive member: {name}") from None
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"Invalid project archive member: {name}")
    return value


def _dicts(value: Any) -> Iterator[dict[str, Any]]:
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _dicts(child)
    elif isinstance(value, list):
        for child in value:
            yield from _dicts(child)


def _strings(value: Any) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, child in value.items():
            yield key
            yield from _strings(child)
    elif isinstance(value, list):
        for child in value:
            yield from _strings(child)


def _local_path(value: Any, root: Path) -> Path | None:
    if not isinstance(value, str) or not value or "\x00" in value or "://" in value:
        return None
    return Path(os.path.abspath(root / value))


def _asset_path(asset: dict[str, Any], root: Path) -> Path | None:
    candidates = [_local_path(asset.get(key), root)
                  for key in ("path_relative", "path_original", "path")]
    return next((p for p in candidates if p is not None and p.is_file()),
                next((p for p in candidates if p is not None), None))


def _embedded_size_matches(archive: zipfile.ZipFile, resources: ResourceIndex, uri: str) -> bool:
    entry = resources.by_uri(uri)
    if entry is None:
        return False
    try:
        return archive.getinfo(entry.archive_path).file_size == entry.size_bytes
    except KeyError:
        return False


def _asset_records(tables: list[dict[str, Any]]) -> Iterator[tuple[str, dict[str, Any]]]:
    for table in tables:
        for key, asset in table.items():
            if isinstance(asset, dict):
                yield key, asset


def _resource_name(names: set[str], path: Path, subdir: str) -> str:
    return f"{subdir}/{_unique_dest_name(_sanitize_name(path.name, 'source'), names)}"


def _collect_source(
    path: Path | None,
    display: str,
    sources: dict[Path, str],
    names: set[str],
    subdir: str,
    inventory: _Inventory,
) -> str | None:
    if path is None or not path.is_file():
        inventory.missing[display] = None
        return None
    if path not in sources:
        sources[path] = _resource_name(names, path, subdir)
        inventory.estimated_bytes += path.stat().st_size
    return sources[path]


def _collect_frames(
    paths: tuple[Path, ...],
    anchor: Path,
    inventory: _Inventory,
    names: set[str],
    cancelled: Callable[[], bool] | None,
) -> str:
    directory = inventory.sequence_dirs.get(paths)
    if directory is not None:
        return directory
    directory = "media/" + _unique_dest_name(_sanitize_name(anchor.stem, "Sequence"), names)
    inventory.sequence_dirs[paths] = directory
    used: dict[str, Path] = {}
    for frame in paths:
        _checkpoint(cancelled)
        folded = frame.name.casefold()
        if not frame.is_file() or (folded in used and used[folded] != frame):
            inventory.missing[str(frame)] = None
            continue
        if folded not in used:
            used[folded] = frame
            inventory.frames[f"{directory}/{frame.name}"] = frame
            inventory.estimated_bytes += frame.stat().st_size
    return directory


def _sequence_metadata(asset: dict[str, Any]) -> dict[str, Any] | None:
    metadata = asset.get("metadata")
    sequence = metadata.get("sequence") if isinstance(metadata, dict) else None
    return sequence if isinstance(sequence, dict) else None


def _collect_still_sequence(
    anchor: Path,
    sequence: dict[str, Any] | None,
    root: Path,
    inventory: _Inventory,
    names: set[str],
    cancelled: Callable[[], bool] | None,
) -> tuple[str, list[str]] | None:
    frames = sequence.get("frames") if sequence else None
    if not isinstance(frames, list) or not frames:
        inventory.missing[f"{anchor} — image sequence has no frame list"] = None
        return None
    paths = tuple(path for value in frames if (path := _local_path(value, root)) is not None)
    if len(paths) != len(frames):
        inventory.missing[f"{anchor} — one or more image-sequence frame paths are invalid"] = None
        return None
    if anchor.is_dir():
        paths = tuple(anchor / path.name for path in paths)
        anchor = paths[0]
    else:
        paths = tuple(anchor.parent / path.name for path in paths)
    directory = _collect_frames(paths, anchor, inventory, names, cancelled)
    if f"{directory}/{anchor.name}" not in inventory.frames:
        inventory.missing[str(anchor)] = None
    return f"{directory}/{anchor.name}", [f"{directory}/{path.name}" for path in paths]


def _collect_pattern(
    pattern: Path,
    inventory: _Inventory,
    names: set[str],
    cancelled: Callable[[], bool] | None,
) -> str | None:
    parts = _FRAME_PATTERN.split(pattern.name)
    if len(parts) < 3 or not pattern.parent.is_dir():
        inventory.missing[str(pattern)] = None
        return None
    matcher = re.compile("".join(r"-?[0-9]+" if index % 2 else re.escape(text)
                                 for index, text in enumerate(parts)))
    frames = tuple(sorted(path for path in pattern.parent.iterdir()
                          if matcher.fullmatch(path.name) and path.is_file()))
    if not frames:
        inventory.missing[str(pattern)] = None
        return None
    directory = _collect_frames(frames, pattern, inventory, names, cancelled)
    return f"{directory}/{pattern.name}"


def _asset_reference(asset: dict[str, Any], path: Path | None) -> str:
    return str(asset.get("display_name") or asset.get("name") or path or asset.get("resource_uri") or "(unnamed)")


def _set_media_path(asset: dict[str, Any], reference: str) -> None:
    asset.update(path=reference, path_relative=reference)
    asset.pop("path_original", None)


def _linked_archive_is_self_contained(path: Path) -> bool:
    # Preserve linked archives byte-for-byte, including pinned revision hashes.
    # Rewriting one would invalidate the source fingerprint, so external
    # requirements of a non-self-contained linked archive stay explicit.
    try:
        with zipfile.ZipFile(path) as archive:
            project = _read_document(archive, "project.json", required=True)
            resources = ResourceIndex.from_dict(_read_document(archive, RESOURCE_INDEX_ARCHIVE_PATH))
            assets = _read_document(archive, _MEDIA_POOL) or project.get("assets", {})
            if not isinstance(assets, dict):
                return False
            for asset_id, asset in _asset_records([assets]):
                source = asset.get("path")
                if isinstance(source, str) and is_virtual_asset_path(source):
                    continue
                entry = resources.for_asset(asset_id)
                uri = asset.get("resource_uri") or (entry.uri if entry else "")
                if not isinstance(uri, str) or not _embedded_size_matches(archive, resources, uri):
                    return False
            for item in _dicts(_read_document(archive, _STACKS)):
                for key in ("lut_path", "active_lut_path"):
                    value = item.get(key)
                    if isinstance(value, str) and value and not _embedded_size_matches(archive, resources, value):
                        return False
            for item in _dicts(_read_document(archive, _COMPOSITES)):
                node_type, params = item.get("type"), item.get("parameters")
                if not isinstance(node_type, str) or not isinstance(params, dict) or item.get("disabled"):
                    continue
                for key in _composite_fields(node_type, params):
                    value = params.get(key)
                    if isinstance(value, str) and value and not (
                        is_virtual_asset_path(value) or _embedded_size_matches(archive, resources, value)
                    ):
                        return False
                if node_type == "precomp" and (params.get("external_comp_ref") or params.get("ref_relative_path")):
                    return False
    except (OSError, ValueError, zipfile.BadZipFile):
        return False
    return True


def _collect_linked_archive(
    params: dict[str, Any],
    root: Path,
    archive: zipfile.ZipFile,
    resources: ResourceIndex,
    inventory: _Inventory,
    names: set[str],
    *,
    collect: bool,
    new_root: Path | None,
    cancelled: Callable[[], bool] | None,
) -> None:
    from src.features.composite.core.external_comp_refs import UPDATE_POLICY_PINNED, read_comp_ref
    from src.features.composite.render.external_comp_snapshot import EXTERNAL_ARCHIVE_ROLE

    try:
        reference = read_comp_ref(params)
    except ValueError:
        inventory.missing["Invalid external composition reference"] = None
        return
    if reference is None:
        return
    raw = reference.relative_path
    fingerprint = reference.revision_fingerprint if reference.update_policy == UPDATE_POLICY_PINNED else ""
    key = ["", posixpath.normpath(raw.replace("\\", "/"))]
    for entry in resources.entries_for_role(EXTERNAL_ARCHIVE_ROLE):
        edges = entry.metadata.get("reference_edges")
        if (
            isinstance(edges, list) and key in edges and (not fingerprint or entry.sha256 == fingerprint)
            and _embedded_size_matches(archive, resources, entry.uri)
        ):
            return
    path = _local_path(raw, root)
    if path is None or not path.is_file():
        inventory.missing[raw or reference.comp_id] = None
        return
    try:
        with zipfile.ZipFile(path) as linked:
            identity = _read_document(linked, "manifest.json").get("project_id")
            if not identity:
                identity = _read_document(linked, "project.json", required=True).get("project_id")
            graph = _read_document(linked, _COMPOSITES).get(reference.comp_id)
            if reference.project_id != identity or not isinstance(graph, dict):
                inventory.missing[f"{raw} — linked project or composition does not match"] = None
                return
    except (OSError, ValueError, zipfile.BadZipFile):
        inventory.missing[f"{raw} — cannot read linked composition"] = None
        return
    if fingerprint:
        digest = hashlib.sha256()
        with path.open("rb") as reader:
            while chunk := reader.read(_IO_CHUNK):
                _checkpoint(cancelled)
                digest.update(chunk)
        if digest.hexdigest() != fingerprint:
            inventory.missing[f"{raw} — pinned composition revision does not match"] = None
            return
    if collect:
        name = _collect_source(path, raw, inventory.linked, names, "linked_projects", inventory)
        if not _linked_archive_is_self_contained(path):
            inventory.missing[f"{raw} — linked composition requires additional files"] = None
    elif new_root is not None:
        name = _relative_path(path, new_root)
        if os.path.isabs(name):
            inventory.missing[f"{raw} — cannot rebase a linked project across volumes"] = None
            return
    else:
        return
    if name is None:
        return
    structured = params.get("external_comp_ref")
    if isinstance(structured, dict):
        structured["relative_path"] = name
    if not isinstance(structured, dict) or "ref_relative_path" in params:
        params["ref_relative_path"] = name


def _relative_path(path: Path, root: Path) -> str:
    try:
        return Path(os.path.relpath(path, root)).as_posix()
    except ValueError:
        # A project-only copy to a different Windows volume still needs a
        # resolvable link on the same workstation.
        return str(path)


def _composite_fields(node_type: str, params: dict[str, Any]) -> tuple[str, ...]:
    from src.shared.project.topos_project_io import composite_input_path_fields

    return composite_input_path_fields(node_type, params)


def _inventory(
    source: Path,
    archive: zipfile.ZipFile,
    scope: str,
    *,
    new_root: Path | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> _Inventory:
    if scope not in ("all", "used", "project"):
        raise ValueError(f"Unknown project package scope: {scope}")
    if len(archive.namelist()) != len(set(archive.namelist())):
        raise ValueError("Project archive contains duplicate members")
    result = _Inventory(estimated_bytes=source.stat().st_size)
    result.documents["manifest.json"] = _read_document(archive, "manifest.json", required=True)
    project = _read_document(archive, "project.json", required=True)
    result.documents["project.json"] = project
    for member in (_MEDIA_POOL, *_KNOWN_REFS, RESOURCE_INDEX_ARCHIVE_PATH):
        if member in archive.namelist():
            result.documents[member] = _read_document(archive, member)
    resources = ResourceIndex.from_dict(result.documents.get(RESOURCE_INDEX_ARCHIVE_PATH))
    rows = [result.documents.get(_MEDIA_POOL, {})]
    project_assets = project.get("assets")
    if isinstance(project_assets, dict):
        rows.append(project_assets)
    # Snapshot assets form part of the saved editing state and must reopen too.
    settings = project.get("settings")
    snapshots = settings.get("project_snapshots") if isinstance(settings, dict) else None
    if isinstance(snapshots, dict):
        rows.extend(table for item in _dicts(snapshots)
                    if isinstance(table := item.get("assets"), dict))
    records = list(_asset_records(rows))
    source_tables = dict(reversed(records))
    source_paths = {asset_id: _asset_path(asset, source.parent)
                    for asset_id, asset in source_tables.items()}
    indexed_assets = {entry.asset_id for entry in resources.entries.values()}
    sequence_metadata = {asset_id: _sequence_metadata(asset) for asset_id, asset in source_tables.items()}
    # Asset rows are rewritten in-place. Sequence frame discovery must keep
    # the original lists for the corresponding lighter asset reference rows.
    sequence_metadata = {asset_id: dict(sequence) if sequence is not None else None
                         for asset_id, sequence in sequence_metadata.items()}
    references = [value for member, value in result.documents.items()
                  if member not in ("manifest.json", "project.json", _MEDIA_POOL,
                                    RESOURCE_INDEX_ARCHIVE_PATH)]
    references.append({key: value for key, value in project.items() if key != "assets"})
    used = set(source_tables) if scope != "used" else {
        token for document in references for token in _strings(document) if token in source_tables
    }

    names: dict[str, set[str]] = {"media": set(), "luts": set(), "linked_projects": set()}
    archive_only = scope == "project"
    path_aliases: dict[Path, str] = {}
    # Resolve asset definitions before direct references so a graph that has
    # an asset ID and the same file path copies one media file.
    for asset_id, asset in records:
        _checkpoint(cancelled)
        if not archive_only and ("proxy_path" in asset or "proxy_status" in asset):
            asset["proxy_path"] = None
            asset["proxy_status"] = "none"
        authoritative = source_tables[asset_id]
        source_path = asset.get("path") or authoritative.get("path")
        if isinstance(source_path, str) and is_virtual_asset_path(source_path):
            continue
        entry = resources.for_asset(asset_id) if asset_id in indexed_assets else None
        uri = asset.get("resource_uri") or authoritative.get("resource_uri") or (entry.uri if entry else "")
        if isinstance(uri, str) and uri:
            if not _embedded_size_matches(archive, resources, uri) and asset_id in used:
                result.missing[_asset_reference(asset, None)] = None
            continue
        path = _asset_path(asset, source.parent)
        if path is None or not path.is_file():
            path = source_paths[asset_id] or path
        if archive_only or asset_id not in used:
            if new_root is not None and path is not None:
                # Omitted assets keep an explicit relink hint rather than an
                # old relative link that could resolve to a different file.
                _set_media_path(asset, _relative_path(path, new_root))
            if archive_only and (path is None or not path.is_file()):
                result.missing[_asset_reference(asset, path)] = None
            continue
        if path is not None and (asset.get("media_type") or authoritative.get("media_type")) == "image_sequence":
            sequence = sequence_metadata[asset_id]
            collected = _collect_still_sequence(path, sequence, source.parent, result, names["media"], cancelled)
            if collected is not None:
                ref, frames = collected
                current = _sequence_metadata(asset)
                if current is not None:
                    current["frames"] = frames
            else:
                ref = None
        else:
            ref = _collect_source(path, _asset_reference(asset, path), result.media, names["media"], "media", result)
        if ref is not None:
            _set_media_path(asset, ref)
            if path is not None:
                path_aliases[path] = ref
            if "is_offline" in asset:
                asset["is_offline"] = False
        elif new_root is not None and path is not None:
            _set_media_path(asset, _relative_path(path, new_root))

    for document in references:
        for item in _dicts(document):
            _checkpoint(cancelled)
            for key, prefix in (("font_family", "Font"), ("plugin_id", "Plugin")):
                value = item.get(key)
                if isinstance(value, str) and value.strip():
                    result.dependencies[f"{prefix}: {value.strip()}"] = None
            for key in ("lut_path", "active_lut_path"):
                raw = item.get(key)
                if not isinstance(raw, str) or not raw or is_virtual_asset_path(raw):
                    continue
                if raw.startswith("topos-res://"):
                    if not _embedded_size_matches(archive, resources, raw):
                        result.missing[raw] = None
                    continue
                path = _local_path(raw, source.parent)
                if archive_only:
                    if path is None or not path.is_file():
                        result.missing[raw] = None
                    elif new_root is not None:
                        item[key] = (
                            _relative_path(path, new_root)
                            if document is result.documents.get(_COMPOSITES) else str(path)
                        )
                    continue
                ref = _collect_source(path, raw, result.luts, names["luts"], "luts", result)
                if ref is not None:
                    item[key] = ref

    for item in _dicts(result.documents.get(_COMPOSITES, {})):
        node_type, params = item.get("type"), item.get("parameters")
        if not isinstance(node_type, str) or not isinstance(params, dict):
            continue
        if node_type == "precomp" and not item.get("disabled"):
            _collect_linked_archive(
                params, source.parent, archive, resources, result, names["linked_projects"],
                collect=not archive_only, new_root=new_root, cancelled=cancelled,
            )
        for key in _composite_fields(node_type, params):
            if key == "lut_path":
                continue  # The shared LUT collection above already rewrote it.
            raw = params.get(key)
            if not isinstance(raw, str) or not raw or is_virtual_asset_path(raw):
                continue
            if raw.startswith("topos-res://"):
                if not _embedded_size_matches(archive, resources, raw):
                    result.missing[raw] = None
                continue
            path = _local_path(raw, source.parent)
            if path is not None and _FRAME_PATTERN.search(str(path)):
                if archive_only:
                    if new_root is not None:
                        params[key] = _relative_path(path, new_root)
                else:
                    ref = _collect_pattern(path, result, names["media"], cancelled)
                    if ref is not None:
                        params[key] = ref
                continue
            if archive_only:
                if path is None or not path.is_file():
                    result.missing[raw] = None
                elif new_root is not None:
                    params[key] = _relative_path(path, new_root)
                continue
            if not path or not path.is_file():
                asset_id = params.get("asset_id")
                if isinstance(asset_id, str) and key == "file_path":
                    # A file_path may be a stale hint while the canonical
                    # original on the media pool item is available.
                    path = source_paths.get(asset_id) or path
            if (node_type == "model3d" and key == "model_path" and path is not None
                    and path.suffix.lower() in (".gltf", ".obj")):
                result.missing[f"{raw} — model may require adjacent files"] = None
            ref = path_aliases.get(path) if path is not None else None
            if ref is None:
                ref = _collect_source(path, raw, result.media, names["media"], "media", result)
            if ref is not None:
                params[key] = ref
    return result


def inspect_project_package(source_path: str, scope: str) -> PackagePlan:
    """Estimate a saved project's package and list missing dependencies."""
    source = Path(source_path).absolute()
    with zipfile.ZipFile(source) as archive:
        inventory = _inventory(source, archive, scope)
    return PackagePlan(inventory.estimated_bytes, tuple(inventory.missing), tuple(inventory.dependencies))


def _stream(
    reader: BinaryIO,
    writer: BinaryIO,
    cancelled: Callable[[], bool] | None,
    advance: Callable[[int], None] | None = None,
) -> None:
    while chunk := reader.read(_IO_CHUNK):
        _checkpoint(cancelled)
        writer.write(chunk)
        if advance is not None:
            advance(len(chunk))


def _copy(
    source: Path,
    destination: Path,
    cancelled: Callable[[], bool] | None,
    advance: Callable[[int], None],
) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with source.open("rb") as reader, destination.open("xb") as writer:
        original = _signature(os.fstat(reader.fileno()))
        _stream(reader, writer, cancelled, advance)
    if _signature(source.stat()) != original:
        raise OSError(f"The source changed while it was being copied: {source}")
    shutil.copystat(source, destination)


def _resource_entries(
    inventory: _Inventory, root: Path, cancelled: Callable[[], bool] | None,
) -> dict[str, Path]:
    # Keep the conventional sibling LUT copy, and also attach each LUT with a
    # canonical URI so Color can resolve it independently of process CWD.
    resource_files: dict[str, Path] = {}
    changes: dict[str, str] = {}
    for relative in inventory.luts.values():
        _checkpoint(cancelled)
        lut = root / relative
        entry = make_resource_entry(str(lut), resource_id="", kind="lut", roles=("lut",))
        resource_id = f"package-lut-{entry.sha256}"
        existing = resource_files.get(entry.archive_path)
        if existing is None:
            resource_files[entry.archive_path] = lut
            data = entry.to_dict()
            data["resource_id"] = resource_id
            index = inventory.documents.setdefault(RESOURCE_INDEX_ARCHIVE_PATH, {})
            index.setdefault("schema_version", 1)
            entries = index.setdefault("resources", [])
            if not isinstance(entries, list):
                raise ValueError("Invalid resource index")
            if not any(isinstance(item, dict) and item.get("resource_id") == resource_id for item in entries):
                entries.append(data)
        changes[relative] = f"topos-res://{resource_id}"

    for member, doc in inventory.documents.items():
        if member == _COMPOSITES:
            continue  # Composite LUT nodes open direct files, not resource URIs.
        for item in _dicts(doc):
            for key in ("lut_path", "active_lut_path"):
                value = item.get(key)
                if isinstance(value, str) and value in changes:
                    item[key] = changes[value]
    return resource_files


def _write_project(
    source: zipfile.ZipFile,
    destination: Path,
    documents: dict[str, dict[str, Any]],
    resources: dict[str, Path],
    cancelled: Callable[[], bool] | None,
) -> None:
    with zipfile.ZipFile(destination, "x", compression=zipfile.ZIP_DEFLATED, allowZip64=True) as bundle:
        for info in source.infolist():
            _checkpoint(cancelled)
            member = info.filename
            if member in resources:
                continue
            if member in documents:
                bundle.writestr(member, json.dumps(documents[member], ensure_ascii=False))
            else:
                # Preserve opaque member bytes including unknown modules. Keep
                # original compression type so a metadata rewrite does not
                # inflate a project without regard for the disk estimate.
                clone = zipfile.ZipInfo(member, info.date_time)
                clone.compress_type = info.compress_type
                clone.external_attr = info.external_attr
                with source.open(info) as reader, bundle.open(clone, "w", force_zip64=True) as writer:
                    _stream(reader, writer, cancelled)
        for member in documents.keys() - set(source.namelist()):
            bundle.writestr(member, json.dumps(documents[member], ensure_ascii=False))
        for member, path in resources.items():
            _checkpoint(cancelled)
            info = zipfile.ZipInfo(member)
            with path.open("rb") as reader, bundle.open(info, "w", force_zip64=True) as writer:
                _stream(reader, writer, cancelled)


def _write_zip(
    folder: Path, target: Path, package_name: str,
    cancelled: Callable[[], bool] | None,
    advance: Callable[[int], None],
) -> None:
    with zipfile.ZipFile(target, "x", compression=zipfile.ZIP_STORED, allowZip64=True) as bundle:
        for path in sorted(folder.rglob("*")):
            _checkpoint(cancelled)
            if not path.is_file():
                continue
            member = f"{package_name}/{path.relative_to(folder).as_posix()}"
            with path.open("rb") as reader, bundle.open(member, "w", force_zip64=True) as writer:
                _stream(reader, writer, cancelled, advance)


def _publish(staged: Path, parent: Path, stem: str, suffix: str = "") -> Path:
    for index in range(1, 10_000):
        postfix = "" if index == 1 else f" ({index})"
        output = parent / f"{stem}{postfix}{suffix}"
        # Windows rename is itself non-replacing. On POSIX reserve an owned,
        # empty target before atomic replacement, including on FAT volumes.
        reserved = False
        try:
            if os.name == "nt":
                staged.rename(output)
                return output
            if suffix:
                with output.open("xb"):
                    pass
            else:
                output.mkdir()
            reserved = True
            staged.replace(output)
            return output
        except FileExistsError:
            if reserved:
                raise
        finally:
            if reserved and staged.exists():
                if suffix:
                    output.unlink(missing_ok=True)
                else:
                    try:
                        output.rmdir()
                    except OSError:
                        pass
    raise FileExistsError(f"No available project package name for {stem}")


def export_project_package(
    source_path: str,
    destination: str,
    *,
    scope: str,
    zip_output: bool,
    allow_missing: bool,
    progress_callback: Callable[[int, int], None] | None = None,
    cancelled_fn: Callable[[], bool] | None = None,
    expected_missing: tuple[str, ...] | None = None,
) -> PackageResult:
    """Export into a new path under the chosen destination directory."""
    _checkpoint(cancelled_fn)
    source = Path(source_path).absolute()
    parent = Path(destination).absolute()
    if not parent.is_dir():
        raise NotADirectoryError(parent)
    signature = _signature(source.stat())
    stem = _sanitize_name(source.stem, "Project")
    package_name = f"{stem} Package"
    with zipfile.ZipFile(source) as archive:
        if scope != "project":
            # Dir packages always have a single dedicated root under parent.
            # Exported media paths stay relative inside it.
            new_root = parent / package_name
        else:
            new_root = parent
        inventory = _inventory(source, archive, scope, new_root=new_root, cancelled=cancelled_fn)
        missing = tuple(inventory.missing)
        if missing and not allow_missing:
            raise FileNotFoundError(f"Missing project files: {', '.join(missing[:5])}")
        if expected_missing is not None:
            newly_missing = sorted(set(missing) - set(expected_missing))
            if newly_missing:
                raise FileNotFoundError(f"Additional project files went missing: {', '.join(newly_missing[:5])}")
        _checkpoint(cancelled_fn)
        output_zip = zip_output and scope != "project"
        estimate = max(1, inventory.estimated_bytes)
        lut_payload = sum(path.stat().st_size for path in inventory.luts)
        estimate += lut_payload  # LUT is also embedded for Color's URI reader.
        total = estimate * (2 if output_zip else 1)
        if shutil.disk_usage(parent).free < total:
            raise OSError("Not enough free disk space for the project package")
        done = 0
        previous = -1

        def advance(increment: int) -> None:
            nonlocal done, previous
            done += increment
            percent = min(99, done * 100 // total)
            if progress_callback is not None and percent != previous:
                progress_callback(percent, 100)
            previous = percent

        with tempfile.TemporaryDirectory(prefix=".topos-package-", dir=parent) as temp:
            staging = Path(temp)
            folder = staging / "package"
            folder.mkdir()
            project = folder / f"{stem}.topos"
            for sources in (inventory.media, inventory.luts, inventory.linked):
                for path, relative in sources.items():
                    _copy(path, folder / relative, cancelled_fn, advance)
            for relative, frame in inventory.frames.items():
                _copy(frame, folder / relative, cancelled_fn, advance)
            resources = _resource_entries(inventory, folder, cancelled_fn)
            _write_project(archive, project, inventory.documents, resources, cancelled_fn)
            advance(source.stat().st_size)
            if scope != "project":
                dependencies = {
                    "requirements": list(inventory.dependencies),
                    "missing": list(missing),
                }
                (folder / "dependencies.json").write_text(
                    json.dumps(dependencies, ensure_ascii=False, indent=2), encoding="utf-8"
                )
            artifact, name, suffix = folder, package_name, ""
            if output_zip:
                artifact, suffix = staging / "package.zip", ".zip"
                _write_zip(folder, artifact, package_name, cancelled_fn, advance)
            elif scope == "project":
                artifact, name, suffix = project, f"{stem} Copy", ".topos"
            _checkpoint(cancelled_fn)
            if _signature(source.stat()) != signature:
                raise OSError("The source project changed during export; retry after saving")
            path = _publish(artifact, parent, name, suffix)
            if progress_callback is not None:
                progress_callback(100, 100)
    return PackageResult(str(path), missing)
