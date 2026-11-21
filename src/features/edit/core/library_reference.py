"""Library search references shared by the media pool and timeline."""
from __future__ import annotations

import json
import math
import os
import stat
from typing import TYPE_CHECKING, Any

from src.features.edit.core.asset_source_range import SourceRangeError
from src.shared.media.metadata import Rational

if TYPE_CHECKING:
    from src.features.edit.core.project import MediaAsset

LIBRARY_REFERENCE_MIME = "application/x-topos-library-reference"
_SOURCE_FIELDS = ("source_size", "source_mtime_ns", "source_ctime_ns")


def serialize_reference(hit: dict[str, Any]) -> bytes:
    fields = ("path", "asset_id", "online", "start_seconds", "end_seconds", *_SOURCE_FIELDS)
    return json.dumps({key: hit.get(key) for key in fields}, ensure_ascii=False).encode("utf-8")


def deserialize_reference(payload: bytes, path: str) -> dict[str, Any] | None:
    if not os.path.isabs(path) or "\x00" in path or len(payload) > 32768:
        return None
    try:
        reference = json.loads(payload)
    except (ValueError, RecursionError):
        return None
    if not isinstance(reference, dict) or reference.get("path") != path or reference.get("online") is not True:
        return None
    asset_id = reference.get("asset_id")
    if asset_id is not None and (not isinstance(asset_id, str) or len(asset_id) > 256):
        return None
    source_values = tuple(reference.get(key) for key in _SOURCE_FIELDS)
    requires_fingerprint = bool(asset_id) or any(value is not None for value in source_values)
    if requires_fingerprint and any(type(value) is not int for value in source_values):
        return None
    start, end = reference.get("start_seconds"), reference.get("end_seconds")
    if start is not None or end is not None:
        if not asset_id or not all(type(value) in (int, float) and 0 <= value < 1e9 for value in (start, end)):
            return None
        if start >= end:
            return None
    return reference


def source_matches(path: str, reference: dict[str, Any]) -> bool:
    try:
        info = os.stat(path)
    except (OSError, ValueError):
        return False
    return stat.S_ISREG(info.st_mode) and all(reference.get(key) in (None, value) for key, value in zip(
        _SOURCE_FIELDS, (info.st_size, info.st_mtime_ns, info.st_ctime_ns),
    ))


def excerpt_frame_range(
    asset: MediaAsset, reference: dict[str, Any], *, audio_fps: float = 24.0,
) -> tuple[int, int] | None:
    """Convert a half-open time span to the editor's inclusive source range."""
    start, end = reference.get("start_seconds"), reference.get("end_seconds")
    if start is None and end is None:
        return None
    if asset.media_type not in ("video", "audio"):
        raise SourceRangeError("素材类型不支持片段入出点")

    from src.features.edit.ai_editing.timebase import seconds_to_frame_range

    fps = asset.fps or (audio_fps if asset.media_type == "audio" else None)
    total = (math.ceil(asset.duration_seconds * fps) if asset.media_type == "audio"
             and fps and asset.duration_seconds else asset.total_frames)
    if not fps or not total:
        raise SourceRangeError("源时长或帧率未知")
    interval = seconds_to_frame_range(float(start), float(end), Rational.from_float(fps))
    first, last = interval.start_frame, min(total, interval.end_frame) - 1
    if first < 0 or last < first:
        raise SourceRangeError("片段入出点超出源素材")
    return first, last
