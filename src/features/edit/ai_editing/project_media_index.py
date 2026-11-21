"""Rebuildable SQLite lookup from global library hits to live project assets."""
from __future__ import annotations

import hashlib
import json
import math
import os
import sqlite3
import unicodedata
from collections.abc import Sequence
from contextlib import closing
from pathlib import Path
from typing import TYPE_CHECKING, Any

from src.features.edit.ai_editing.timebase import seconds_to_frame_range
from src.features.edit.core.asset_source_range import get_effective_source_range, is_subclip
from src.shared.media.metadata import Rational

if TYPE_CHECKING:
    from src.features.edit.core.project import EditProject, MediaAsset

_SCHEMA_VERSION = 1


def _source_key(path: str) -> str:
    name = unicodedata.normalize("NFC", os.path.normcase(os.path.realpath(path)))
    return hashlib.sha256(os.fsencode(name)).hexdigest()


def _asset_index_row(asset: MediaAsset) -> tuple[str, str, str, str, float, int, int, int] | None:
    try:
        fps = float(asset.fps or (24.0 if asset.media_type == "audio" else 0.0))
        duration = float(asset.duration_seconds or 0.0)
        if not math.isfinite(fps) or not math.isfinite(duration) or fps < 0 or duration < 0:
            return None
        total = int(asset.metadata.get("source_total_frames") or (
            math.ceil(duration * fps) if asset.media_type == "audio" else asset.total_frames or 0
        ))
        first, last = get_effective_source_range(asset)
        if is_subclip(asset) and (first is None or last is None):
            return None
        first, last = 0 if first is None else int(first), total - 1 if last is None else int(last)
        if total < 0 or first < 0 or (total > 0 and not first <= last < total):
            return None
        source = _source_key(asset.path)
    except (OSError, TypeError, ValueError, OverflowError):
        return None
    return (
        asset.asset_id, source, str(asset.metadata.get("global_library_asset_id") or ""),
        asset.media_type, fps, total, first, last,
    )


def sync_project_library_index(
    directory: Path, project: EditProject, archive_path: str | None,
    hits: Sequence[dict[str, Any]] = (),
) -> list[dict[str, Any]]:
    """Index an immutable project snapshot and resolve hits to usable local IDs.

    Each archive branch has its own rebuildable lookup. Only project IDs,
    source digests and frame ranges are stored here.
    """
    branch = _source_key(archive_path) if archive_path else "unsaved"
    project_key = hashlib.sha256(f"{project.project_id}:{branch}".encode("utf-8")).hexdigest()
    directory.mkdir(parents=True, exist_ok=True)
    filename = directory / f"{project_key}.sqlite3"
    rows = sorted(
        row for asset in project.assets.values()
        if asset.path and not asset.is_offline and asset.media_type in {"image", "video", "audio"}
        and (row := _asset_index_row(asset)) is not None
    )
    fingerprint = hashlib.sha256(json.dumps(rows, ensure_ascii=False).encode("utf-8")).hexdigest()
    with closing(sqlite3.connect(filename, timeout=2.0)) as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        if version == 0:
            connection.execute("""CREATE TABLE snapshot (
                id INTEGER PRIMARY KEY CHECK(id = 1), revision INTEGER NOT NULL, fingerprint TEXT NOT NULL
            )""")
            connection.execute("""CREATE TABLE media (
                asset_id TEXT PRIMARY KEY, source_key TEXT NOT NULL, library_asset_id TEXT NOT NULL,
                kind TEXT NOT NULL, fps REAL NOT NULL, total INTEGER NOT NULL,
                first INTEGER NOT NULL, last INTEGER NOT NULL
            )""")
            connection.execute("CREATE INDEX media_source ON media(source_key, kind)")
            connection.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")
        elif version != _SCHEMA_VERSION:
            raise sqlite3.DatabaseError("Unsupported project media index version")
        snapshot = connection.execute("SELECT fingerprint FROM snapshot WHERE id = 1").fetchone()
        if snapshot is None or snapshot[0] != fingerprint:
            connection.execute("DELETE FROM media")
            connection.executemany("INSERT INTO media VALUES (?, ?, ?, ?, ?, ?, ?, ?)", rows)
            connection.execute(
                """INSERT INTO snapshot VALUES (1, ?, ?) ON CONFLICT(id)
                   DO UPDATE SET revision = excluded.revision, fingerprint = excluded.fingerprint""",
                (project.revision, fingerprint),
            )
        return [_resolve_hit(connection, hit) for hit in hits]


def _resolve_hit(connection: sqlite3.Connection, hit: dict[str, Any]) -> dict[str, Any]:
    if not hit.get("online") or not hit.get("path"):
        return hit
    library_asset = str(hit.get("asset_id") or "")
    matches = connection.execute(
        """SELECT asset_id, fps, total, first, last FROM media
           WHERE source_key = ? AND kind = ? AND (library_asset_id = '' OR library_asset_id = ?)
           ORDER BY (library_asset_id = ?) DESC, last - first, asset_id""",
        (_source_key(str(hit["path"])), hit.get("kind"), library_asset, library_asset),
    )
    start, end = hit.get("start_seconds"), hit.get("end_seconds")
    for asset_id, fps, total, first, last in matches:
        if hit["kind"] == "image" or start is None or end is None:
            if hit["kind"] == "image" or (first == 0 and last >= total - 1):
                return {**hit, "project_asset_id": asset_id}
            continue
        if not fps or not total:
            continue
        try:
            frames = seconds_to_frame_range(float(start), float(end), Rational.from_float(fps))
            end_frame = min(total, frames.end_frame)
        except (TypeError, ValueError, OverflowError):
            continue
        if first <= frames.start_frame < end_frame <= last + 1:
            return {**hit, "project_asset_id": asset_id,
                    "project_source_in_frame": frames.start_frame, "project_source_out_frame": end_frame}
    return hit
