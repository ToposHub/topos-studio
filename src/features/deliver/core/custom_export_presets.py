"""B6: User-defined video export presets (persistent).

Storage: ``Presets/User/export/*.json`` (project-root Presets convention,
same as color user presets). Schema:

    {
      "preset_id": "user_my_film_master",
      "name": "My Film Master",
      "created_at": 1730000000.0,
      "is_custom": true,
      "video_config": {...},     # VideoConfig.to_dict()
      "audio_config": {...},     # AudioConfig.to_dict()
      "subtitle_config": {...}   # SubtitleConfig.to_dict()
    }

Robustness: unreadable/corrupt files are skipped with a logged warning —
one bad file never breaks the deliver page. Writes are atomic
(temp file + os.replace).
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from typing import Any, Dict, List, Optional

from src.features.deliver.core.video_export_job import (
    AudioConfig,
    SubtitleConfig,
    SubtitleMode,
    VideoConfig,
)
from src.features.deliver.core.video_export_presets import VideoPreset

logger = logging.getLogger(__name__)

__all__ = [
    "user_export_presets_dir",
    "list_user_presets",
    "save_user_preset",
    "delete_user_preset",
    "rename_user_preset",
    "user_preset_json_path",
]

#: Sub-directory under the project-root ``Presets/User`` folder.
_SUBDIR = os.path.join("export")
_ID_PREFIX = "user_"
_SLUG_RE = re.compile(r"[^0-9A-Za-z_-]+")


def presets_root() -> str:
    """Project-root Presets directory (same convention as color Gallery)."""
    return os.path.join(os.getcwd(), "Presets")


def user_export_presets_dir() -> str:
    """Directory holding user export preset JSON files (created on demand)."""
    path = os.path.join(presets_root(), "User", _SUBDIR)
    os.makedirs(path, exist_ok=True)
    return path


def user_preset_json_path(preset_id: str) -> str:
    """JSON file path for a user preset id (id is validated/sanitised)."""
    safe = _slug(preset_id)
    return os.path.join(user_export_presets_dir(), f"{safe}.json")


def _slug(name: str) -> str:
    slug = _SLUG_RE.sub("_", name.strip()).strip("_")
    return slug or "preset"


def _preset_id_for_name(name: str, existing: List[str]) -> str:
    """Derive a unique preset id from a display name."""
    base = f"{_ID_PREFIX}{_slug(name)}"
    candidate = base
    counter = 2
    while candidate in existing:
        candidate = f"{base}_{counter}"
        counter += 1
    return candidate


def _parse_preset(
    data: Dict[str, Any], source: str
) -> Optional[tuple[VideoPreset, float]]:
    """Parse one preset JSON dict into a VideoPreset; None on bad content."""
    try:
        name = str(data.get("name") or "").strip()
        if not name:
            raise ValueError("missing name")
        video_cfg = VideoConfig.from_dict(dict(data.get("video_config") or {}))
        audio_cfg = AudioConfig.from_dict(dict(data.get("audio_config") or {}))
        subtitle_raw = dict(data.get("subtitle_config") or {})
        if not subtitle_raw:
            subtitle_raw = {"mode": SubtitleMode.OFF, "track_id": None}
        subtitle_cfg = SubtitleConfig.from_dict(subtitle_raw)
        preset_id = str(data.get("preset_id") or "").strip() or _ID_PREFIX + _slug(name)
        created_at = float(data.get("created_at") or 0.0)
        return VideoPreset(
            preset_id=preset_id,
            title_key="",
            fallback_title=name,
            subtitle_key="",
            fallback_subtitle=_describe(video_cfg),
            video_config=video_cfg,
            audio_config=audio_cfg,
            subtitle_config=subtitle_cfg,
            order=1,  # user presets sort after built-ins via UI grouping
        ), created_at
    except Exception as exc:  # noqa: BLE001 — 单个坏文件不拖垮列表
        logger.warning("Skipping bad user export preset %s: %s", source, exc)
        return None


def _describe(video_cfg: VideoConfig) -> str:
    """One-line summary shown under the preset title on cards."""
    container = (video_cfg.container or "?").upper()
    codec = (video_cfg.codec or "?").upper()
    if video_cfg.resolution:
        res = f"{video_cfg.resolution[0]}×{video_cfg.resolution[1]}"
    else:
        res = "Source"
    return f"{container} · {codec} · {res}"


def list_user_presets() -> List[VideoPreset]:
    """Load all valid user presets, oldest first (stable across restarts)."""
    directory = user_export_presets_dir()
    entries: List[tuple[float, VideoPreset]] = []
    try:
        filenames = sorted(os.listdir(directory))
    except OSError:
        return []
    for filename in filenames:
        if not filename.endswith(".json"):
            continue
        path = os.path.join(directory, filename)
        try:
            with open(path, encoding="utf-8") as handle:
                data = json.load(handle)
            if not isinstance(data, dict):
                raise ValueError("not a JSON object")
        except (OSError, ValueError) as exc:
            logger.warning("Skipping unreadable user export preset %s: %s", path, exc)
            continue
        parsed = _parse_preset(data, path)
        if parsed is None:
            continue
        preset, created_at = parsed
        # AUD-11901: 旧格式预设缺 created_at 时回退 getmtime；文件可能在
        # listdir 与 getmtime 之间被外部删除（同步盘/手动清理），未捕获
        # OSError 会炸掉整个预设列表。按 0.0 降级参与排序即可。
        try:
            fallback_mtime = os.path.getmtime(path)
        except OSError as exc:
            logger.warning("Stale user export preset vanished %s: %s", path, exc)
            fallback_mtime = 0.0
        entries.append((created_at or fallback_mtime, preset))
    entries.sort(key=lambda item: (item[0], item[1].preset_id))
    return [preset for _created, preset in entries]


def _atomic_write_json(path: str, payload: Dict[str, Any]) -> None:
    tmp_path = f"{path}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    os.replace(tmp_path, path)


def _payload_for(
    preset_id: str,
    name: str,
    created_at: float,
    preset: VideoPreset,
) -> Dict[str, Any]:
    return {
        "preset_id": preset_id,
        "name": name,
        "created_at": created_at,
        "is_custom": True,
        "video_config": preset.video_config.to_dict(),
        "audio_config": preset.audio_config.to_dict(),
        "subtitle_config": preset.subtitle_config.to_dict(),
    }


def save_user_preset(
    name: str,
    video_config: VideoConfig,
    audio_config: Optional[AudioConfig] = None,
    subtitle_config: Optional[SubtitleConfig] = None,
) -> VideoPreset:
    """Persist a new custom preset; duplicates (same name) are rejected.

    Raises:
        ValueError: empty name or a preset with the same name already exists.
    """
    clean = (name or "").strip()
    if not clean:
        raise ValueError("preset name is empty")
    existing = list_user_presets()
    for preset in existing:
        if preset.fallback_title == clean:
            raise ValueError(f"duplicate preset name: {clean}")
    taken_ids = [preset.preset_id for preset in existing]
    preset_id = _preset_id_for_name(clean, taken_ids)
    new_preset = VideoPreset(
        preset_id=preset_id,
        title_key="",
        fallback_title=clean,
        subtitle_key="",
        fallback_subtitle=_describe(video_config),
        video_config=video_config,
        audio_config=audio_config or AudioConfig(),
        subtitle_config=subtitle_config or SubtitleConfig(mode=SubtitleMode.OFF),
        order=1,
    )
    path = user_preset_json_path(preset_id)
    _atomic_write_json(
        path,
        _payload_for(preset_id, clean, time.time(), new_preset),
    )
    logger.info("Saved user export preset %r → %s", clean, path)
    return new_preset


def delete_user_preset(preset_id: str) -> bool:
    """Delete a user preset by id. Returns True when a file was removed."""
    path = user_preset_json_path(preset_id)
    try:
        os.remove(path)
        logger.info("Deleted user export preset %s", preset_id)
        return True
    except FileNotFoundError:
        return False
    except OSError as exc:
        logger.warning("Failed to delete user export preset %s: %s", preset_id, exc)
        return False


def rename_user_preset(preset_id: str, new_name: str) -> Optional[VideoPreset]:
    """Rename a user preset (keeps id and creation time)."""
    clean = (new_name or "").strip()
    if not clean:
        raise ValueError("preset name is empty")
    others = [p for p in list_user_presets() if p.preset_id != preset_id]
    for preset in others:
        if preset.fallback_title == clean:
            raise ValueError(f"duplicate preset name: {clean}")
    path = user_preset_json_path(preset_id)
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
        if not isinstance(data, dict):
            raise ValueError("not a JSON object")
    except (OSError, ValueError) as exc:
        logger.warning("Cannot rename user export preset %s: %s", preset_id, exc)
        return None
    parsed = _parse_preset(data, path)
    if parsed is None:
        return None
    preset, created_at = parsed
    data["name"] = clean
    payload = _payload_for(preset_id, clean, created_at or time.time(), preset)
    _atomic_write_json(path, payload)
    return VideoPreset(
        preset_id=preset_id,
        title_key="",
        fallback_title=clean,
        subtitle_key="",
        fallback_subtitle=preset.fallback_subtitle,
        video_config=preset.video_config,
        audio_config=preset.audio_config,
        subtitle_config=preset.subtitle_config,
        order=1,
    )
