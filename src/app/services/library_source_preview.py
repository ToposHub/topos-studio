"""Prepare read-only Edit sources before handing them to the Source Viewer."""
from __future__ import annotations

import math
import os
import stat
from functools import lru_cache

from src.features.edit.core.project import MediaAsset, media_type_from_probe, probe_media_metadata


@lru_cache(maxsize=32)
def _probe_source(path: str, signature: tuple[int, int, int, int, int]) -> MediaAsset:
    metadata = probe_media_metadata(path, include_pts_index=False)
    kind = media_type_from_probe(path, metadata)
    if kind not in ("image", "video", "audio"):
        raise ValueError("此文件无法在源监视器中预览。")
    if kind == "audio":
        duration = metadata.get("duration_seconds")
        if not isinstance(duration, (float, int)) or not math.isfinite(duration) or duration <= 0:
            raise ValueError("无法读取音频时长。")
        metadata = {**metadata, "fps": 24.0, "total_frames": math.ceil(duration * 24)}
    return MediaAsset(f"library-preview:{path}", path, kind, os.path.basename(path), metadata)


def _source_signature(path: str) -> tuple[int, int, int, int, int]:
    info = os.stat(path)
    if not stat.S_ISREG(info.st_mode):
        raise ValueError("预览目标必须是文件。")
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def prepare_library_source(path: str) -> MediaAsset:
    signature = _source_signature(path)
    asset = _probe_source(path, signature)
    if _source_signature(path) != signature:
        raise ValueError("源素材已变更，请重新打开。")
    return asset
