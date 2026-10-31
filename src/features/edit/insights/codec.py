"""codec —— InsightSnapshot 唯一序列化契约（T01 定稿，T14/T15 复用）。

契约要点（计划 T01/T15）：

- ``serialize_snapshot`` / ``parse_snapshot``：规范 JSON（排序键、
  ensure_ascii=False、UTF-8），同一 snapshot 序列化→重读恒等；
- ``payload_hash``：完整规范载荷的 sha256（不含 hash 字段本身，含
  capture_id/时间/revision）；parse 时强制重算比对，防篡改/损坏；
- ``content_hash``：仅逻辑内容（不含捕获时间/ID、revision 计数、导航
  状态）——T15 用它判断"编辑内容是否真的变化"；
- parse 失败一律返回结构化 :class:`SnapshotParseResult`（错误码 + 路径），
  不抛裸异常：``UNSUPPORTED_SCHEMA`` / ``UNKNOWN_FIELD`` /
  ``MISSING_FIELD`` / ``INVALID_JSON`` / ``HASH_MISMATCH`` /
  ``INVALID_TIMEBASE`` / ``INVALID_VALUE``；
- v1 对未知字段**严格拒绝**（前向兼容靠升 schema_version，不做静默吞并）。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from typing import Any


@dataclass(frozen=True)
class SnapshotParseErrorInfo:
    code: str
    message: str
    path: str = ""


@dataclass(frozen=True)
class SnapshotParseResult:
    ok: bool
    snapshot: InsightSnapshot | None = None
    error: SnapshotParseErrorInfo | None = None

from src.features.edit.insights.dto import (
    LOAD_STATE_LOADED,
    LOAD_STATE_UNLOADED,
    SCHEMA_VERSION,
    SOURCE_MAPPING_CONSTANT,
    SOURCE_MAPPING_IDENTITY,
    SOURCE_MAPPING_UNKNOWN,
    AssetEntry,
    ClipEntry,
    CueEntry,
    FpsSpec,
    InsightSnapshot,
    MarkerEntry,
    ProjectEntry,
    SequenceEntry,
    TrackEntry,
)

# ── 序列化（DTO → dict）─────────────────────────────────────────


def _fps_to_dict(fps: FpsSpec) -> dict[str, Any]:
    return {"num": fps.num, "den": fps.den, "drop_frame": fps.drop_frame}


def _asset_to_dict(a: AssetEntry) -> dict[str, Any]:
    return {
        "asset_id": a.asset_id,
        "display_name": a.display_name,
        "media_type": a.media_type,
        "is_offline": a.is_offline,
        "file_basename": a.file_basename,
        "reel": a.reel,
        "scene": a.scene,
        "shot": a.shot,
        "take": a.take,
        "keywords": list(a.keywords),
        "rating": a.rating,
        "flag": a.flag,
        "color_label": a.color_label,
        "comment": a.comment,
    }


def _clip_to_dict(c: ClipEntry) -> dict[str, Any]:
    return {
        "clip_id": c.clip_id,
        "sequence_id": c.sequence_id,
        "track_id": c.track_id,
        "track_type": c.track_type,
        "asset_id": c.asset_id,
        "timeline_start": c.timeline_start,
        "timeline_end": c.timeline_end,
        "source_start": c.source_start,
        "source_end": c.source_end,
        "is_enabled": c.is_enabled,
        "source_mapping": c.source_mapping,
        "reverse": c.reverse,
        "constant_speed": c.constant_speed,
        "linked_clip_id": c.linked_clip_id,
        "label_color": c.label_color,
    }


def _cue_to_dict(c: CueEntry) -> dict[str, Any]:
    return {
        "clip_id": c.clip_id,
        "sequence_id": c.sequence_id,
        "track_id": c.track_id,
        "text": c.text,
        "timeline_start": c.timeline_start,
        "timeline_end": c.timeline_end,
        "is_enabled": c.is_enabled,
    }


def _marker_to_dict(m: MarkerEntry) -> dict[str, Any]:
    return {
        "marker_id": m.marker_id,
        "sequence_id": m.sequence_id,
        "frame": m.frame,
        "name": m.name,
        "color": m.color,
        "comment": m.comment,
        "kind": m.kind,
        "clip_id": m.clip_id,
        "track_id": m.track_id,
    }


def _track_to_dict(t: TrackEntry) -> dict[str, Any]:
    return {
        "track_id": t.track_id,
        "sequence_id": t.sequence_id,
        "name": t.name,
        "track_type": t.track_type,
        "index": t.index,
        "is_enabled": t.is_enabled,
        "track_role": t.track_role,
        "clips": [_clip_to_dict(c) for c in t.clips],
        "cues": [_cue_to_dict(c) for c in t.cues],
    }


def _sequence_to_dict(s: SequenceEntry) -> dict[str, Any]:
    return {
        "sequence_id": s.sequence_id,
        "name": s.name,
        "fps": _fps_to_dict(s.fps),
        "width": s.width,
        "height": s.height,
        "sample_rate": s.sample_rate,
        "revision": s.revision,
        "load_state": s.load_state,
        "duration_frames": s.duration_frames,
        "tracks": [_track_to_dict(t) for t in s.tracks],
        "markers": [_marker_to_dict(m) for m in s.markers],
    }


def snapshot_to_payload_dict(snapshot: InsightSnapshot) -> dict[str, Any]:
    """完整规范载荷（不含 payload_hash/content_hash 本身）。"""
    return {
        "schema_version": snapshot.schema_version,
        "capture_id": snapshot.capture_id,
        "captured_at": snapshot.captured_at,
        "project": {
            "project_id": snapshot.project.project_id,
            "name": snapshot.project.name,
            "revision": snapshot.project.revision,
        },
        "sequences": [_sequence_to_dict(s) for s in snapshot.sequences],
        "assets": [_asset_to_dict(a) for a in snapshot.assets],
    }


# ── hash ─────────────────────────────────────────────────────────

_HASH_EXCLUDE = ("payload_hash", "content_hash")


def _sha256_of(payload: dict[str, Any]) -> str:
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def compute_payload_hash(snapshot: InsightSnapshot) -> str:
    """完整载荷 hash：含捕获 ID/时间与全部 revision。"""
    return _sha256_of(snapshot_to_payload_dict(snapshot))


def _content_payload(snapshot: InsightSnapshot) -> dict[str, Any]:
    """逻辑内容载荷：剥离捕获时间/ID、revision 计数（计划 §2.3）。

    项目/序列名称、fps、加载状态、全部实体内容保留——这些是"编辑内容"；
    duration_frames 是内容的派生量，一并保留（unloaded 的 None 亦如实）。
    """
    payload = snapshot_to_payload_dict(snapshot)
    payload.pop("capture_id", None)
    payload.pop("captured_at", None)
    payload["project"].pop("revision", None)
    for seq in payload["sequences"]:
        seq.pop("revision", None)
    return payload


def compute_content_hash(snapshot: InsightSnapshot) -> str:
    return _sha256_of(_content_payload(snapshot))


def finalize_snapshot(snapshot: InsightSnapshot) -> InsightSnapshot:
    """填充 payload/content hash 并返回新实例（capture 出口统一调用）。"""
    return replace(
        snapshot,
        payload_hash=compute_payload_hash(snapshot),
        content_hash=compute_content_hash(snapshot),
    )


def verify_payload_hash(snapshot: InsightSnapshot) -> bool:
    if not snapshot.payload_hash:
        return False
    return compute_payload_hash(snapshot) == snapshot.payload_hash


def verify_content_hash(snapshot: InsightSnapshot) -> bool:
    if not snapshot.content_hash:
        return False
    return compute_content_hash(snapshot) == snapshot.content_hash


# ── 反序列化（dict → DTO，严格校验）───────────────────────────────


class SnapshotParseError(Exception):
    """携带结构化字段的解析错误（parse 内部使用，外部拿 ParseResult）。"""

    def __init__(self, code: str, message: str, path: str = "") -> None:
        super().__init__(f"[{code}] {path}: {message}")
        self.code = code
        self.message = message
        self.path = path


def _require_keys(data: dict[str, Any], required: set[str], allowed: set[str], path: str) -> None:
    missing = required - set(data)
    if missing:
        raise SnapshotParseError(
            "MISSING_FIELD", f"missing keys: {sorted(missing)}", path
        )
    unknown = set(data) - allowed
    if unknown:
        raise SnapshotParseError(
            "UNKNOWN_FIELD", f"unknown keys: {sorted(unknown)}", path
        )


def _get_str(data: dict[str, Any], key: str, path: str) -> str:
    value = data[key]
    if not isinstance(value, str):
        raise SnapshotParseError("INVALID_VALUE", f"{key} must be str", path)
    return value


def _get_bool(data: dict[str, Any], key: str, path: str) -> bool:
    value = data[key]
    if not isinstance(value, bool):
        raise SnapshotParseError("INVALID_VALUE", f"{key} must be bool", path)
    return value


def _get_int(data: dict[str, Any], key: str, path: str) -> int:
    value = data[key]
    # bool 是 int 子类，显式排除
    if isinstance(value, bool) or not isinstance(value, int):
        raise SnapshotParseError("INVALID_VALUE", f"{key} must be int", path)
    return value


def _get_float_or_none(data: dict[str, Any], key: str, path: str) -> float | None:
    value = data[key]
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SnapshotParseError("INVALID_VALUE", f"{key} must be number|null", path)
    return float(value)


def _get_int_or_none(data: dict[str, Any], key: str, path: str) -> int | None:
    value = data[key]
    if value is None:
        return None
    return _get_int(data, key, path)


def _get_str_or_none(data: dict[str, Any], key: str, path: str) -> str | None:
    value = data[key]
    if value is None:
        return None
    if not isinstance(value, str):
        raise SnapshotParseError("INVALID_VALUE", f"{key} must be str|null", path)
    return value


def _parse_fps(data: dict[str, Any], path: str) -> FpsSpec:
    _require_keys(data, {"num", "den", "drop_frame"}, {"num", "den", "drop_frame"}, path)
    num = _get_int(data, "num", path)
    den = _get_int(data, "den", path)
    if num <= 0 or den <= 0:
        raise SnapshotParseError("INVALID_TIMEBASE", f"invalid fps {num}/{den}", path)
    return FpsSpec(num=num, den=den, drop_frame=_get_bool(data, "drop_frame", path))


def _parse_half_open(start: int, end: int, path: str) -> None:
    if start < 0 or end < start:
        raise SnapshotParseError(
            "INVALID_TIMEBASE", f"invalid half-open range [{start},{end})", path
        )


def _parse_asset(data: dict[str, Any]) -> AssetEntry:
    path = "asset"
    _require_keys(
        data,
        {"asset_id", "display_name", "media_type", "is_offline", "file_basename"},
        {
            "asset_id", "display_name", "media_type", "is_offline", "file_basename",
            "reel", "scene", "shot", "take", "keywords", "rating", "flag",
            "color_label", "comment",
        },
        path,
    )
    keywords = data.get("keywords", [])
    if not isinstance(keywords, list) or any(not isinstance(k, str) for k in keywords):
        raise SnapshotParseError("INVALID_VALUE", "keywords must be list[str]", path)
    rating = _get_int(data, "rating", path) if "rating" in data else 0
    if not 0 <= rating <= 5:
        raise SnapshotParseError("INVALID_VALUE", f"rating out of range: {rating}", path)
    return AssetEntry(
        asset_id=_get_str(data, "asset_id", path),
        display_name=_get_str(data, "display_name", path),
        media_type=_get_str(data, "media_type", path),
        is_offline=_get_bool(data, "is_offline", path),
        file_basename=_get_str(data, "file_basename", path),
        reel=_get_str(data, "reel", path) if "reel" in data else "",
        scene=_get_str(data, "scene", path) if "scene" in data else "",
        shot=_get_str(data, "shot", path) if "shot" in data else "",
        take=_get_str(data, "take", path) if "take" in data else "",
        keywords=tuple(keywords),
        rating=rating,
        flag=_get_str(data, "flag", path) if "flag" in data else "none",
        color_label=_get_str(data, "color_label", path) if "color_label" in data else "none",
        comment=_get_str(data, "comment", path) if "comment" in data else "",
    )


def _parse_clip(data: dict[str, Any]) -> ClipEntry:
    path = "clip"
    required = {
        "clip_id", "sequence_id", "track_id", "track_type", "asset_id",
        "timeline_start", "timeline_end", "source_start", "source_end",
        "is_enabled", "source_mapping",
    }
    allowed = required | {"reverse", "constant_speed", "linked_clip_id", "label_color"}
    _require_keys(data, required, allowed, path)
    timeline_start = _get_int(data, "timeline_start", path)
    timeline_end = _get_int(data, "timeline_end", path)
    source_start = _get_int(data, "source_start", path)
    source_end = _get_int(data, "source_end", path)
    _parse_half_open(timeline_start, timeline_end, f"{path}.timeline")
    _parse_half_open(source_start, source_end, f"{path}.source")
    mapping = _get_str(data, "source_mapping", path)
    if mapping not in (SOURCE_MAPPING_IDENTITY, SOURCE_MAPPING_CONSTANT, SOURCE_MAPPING_UNKNOWN):
        raise SnapshotParseError("INVALID_VALUE", f"bad source_mapping: {mapping}", path)
    track_type = _get_str(data, "track_type", path)
    if track_type not in ("video", "audio"):
        raise SnapshotParseError("INVALID_VALUE", f"clip track_type: {track_type}", path)
    return ClipEntry(
        clip_id=_get_str(data, "clip_id", path),
        sequence_id=_get_str(data, "sequence_id", path),
        track_id=_get_str(data, "track_id", path),
        track_type=track_type,
        asset_id=_get_str(data, "asset_id", path),
        timeline_start=timeline_start,
        timeline_end=timeline_end,
        source_start=source_start,
        source_end=source_end,
        is_enabled=_get_bool(data, "is_enabled", path),
        source_mapping=mapping,
        reverse=_get_bool(data, "reverse", path) if "reverse" in data else False,
        constant_speed=_get_float_or_none(data, "constant_speed", path) if "constant_speed" in data else None,
        linked_clip_id=_get_str_or_none(data, "linked_clip_id", path) if "linked_clip_id" in data else None,
        label_color=_get_str_or_none(data, "label_color", path) if "label_color" in data else None,
    )


def _parse_cue(data: dict[str, Any]) -> CueEntry:
    path = "cue"
    required = {
        "clip_id", "sequence_id", "track_id", "text",
        "timeline_start", "timeline_end", "is_enabled",
    }
    _require_keys(data, required, required, path)
    start = _get_int(data, "timeline_start", path)
    end = _get_int(data, "timeline_end", path)
    _parse_half_open(start, end, f"{path}.timeline")
    return CueEntry(
        clip_id=_get_str(data, "clip_id", path),
        sequence_id=_get_str(data, "sequence_id", path),
        track_id=_get_str(data, "track_id", path),
        text=_get_str(data, "text", path),
        timeline_start=start,
        timeline_end=end,
        is_enabled=_get_bool(data, "is_enabled", path),
    )


def _parse_marker(data: dict[str, Any]) -> MarkerEntry:
    path = "marker"
    required = {"marker_id", "sequence_id", "frame", "name", "color", "comment", "kind"}
    allowed = required | {"clip_id", "track_id"}
    _require_keys(data, required, allowed, path)
    return MarkerEntry(
        marker_id=_get_str(data, "marker_id", path),
        sequence_id=_get_str(data, "sequence_id", path),
        frame=_get_int(data, "frame", path),
        name=_get_str(data, "name", path),
        color=_get_str(data, "color", path),
        comment=_get_str(data, "comment", path),
        kind=_get_str(data, "kind", path),
        clip_id=_get_str_or_none(data, "clip_id", path) if "clip_id" in data else None,
        track_id=_get_str_or_none(data, "track_id", path) if "track_id" in data else None,
    )


def _parse_track(data: dict[str, Any]) -> TrackEntry:
    path = "track"
    required = {"track_id", "sequence_id", "name", "track_type", "index", "is_enabled"}
    allowed = required | {"track_role", "clips", "cues"}
    _require_keys(data, required, allowed, path)
    track_type = _get_str(data, "track_type", path)
    if track_type not in ("video", "audio", "subtitle"):
        raise SnapshotParseError("INVALID_VALUE", f"bad track_type: {track_type}", path)
    clips_raw = data.get("clips", [])
    cues_raw = data.get("cues", [])
    if not isinstance(clips_raw, list) or not isinstance(cues_raw, list):
        raise SnapshotParseError("INVALID_VALUE", "clips/cues must be lists", path)
    return TrackEntry(
        track_id=_get_str(data, "track_id", path),
        sequence_id=_get_str(data, "sequence_id", path),
        name=_get_str(data, "name", path),
        track_type=track_type,
        index=_get_int(data, "index", path),
        is_enabled=_get_bool(data, "is_enabled", path),
        track_role=_get_str(data, "track_role", path) if "track_role" in data else "standard",
        clips=tuple(_parse_clip(c) for c in clips_raw),
        cues=tuple(_parse_cue(c) for c in cues_raw),
    )


def _parse_sequence(data: dict[str, Any]) -> SequenceEntry:
    path = "sequence"
    required = {
        "sequence_id", "name", "fps", "width", "height", "sample_rate",
        "revision", "load_state", "duration_frames", "tracks", "markers",
    }
    _require_keys(data, required, required, path)
    load_state = _get_str(data, "load_state", path)
    if load_state not in (LOAD_STATE_LOADED, LOAD_STATE_UNLOADED):
        raise SnapshotParseError("INVALID_VALUE", f"bad load_state: {load_state}", path)
    duration = _get_int_or_none(data, "duration_frames", path)
    if load_state == LOAD_STATE_UNLOADED and duration is not None:
        raise SnapshotParseError(
            "INVALID_VALUE", "unloaded sequence must have duration_frames=null", path
        )
    if load_state == LOAD_STATE_LOADED and duration is None:
        raise SnapshotParseError(
            "INVALID_VALUE", "loaded sequence must collect duration_frames", path
        )
    fps_raw = data["fps"]
    if not isinstance(fps_raw, dict):
        raise SnapshotParseError("INVALID_VALUE", "fps must be object", path)
    tracks_raw = data["tracks"]
    markers_raw = data["markers"]
    if not isinstance(tracks_raw, list) or not isinstance(markers_raw, list):
        raise SnapshotParseError("INVALID_VALUE", "tracks/markers must be lists", path)
    return SequenceEntry(
        sequence_id=_get_str(data, "sequence_id", path),
        name=_get_str(data, "name", path),
        fps=_parse_fps(fps_raw, f"{path}.fps"),
        width=_get_int(data, "width", path),
        height=_get_int(data, "height", path),
        sample_rate=_get_int(data, "sample_rate", path),
        revision=_get_int(data, "revision", path),
        load_state=load_state,
        duration_frames=duration,
        tracks=tuple(_parse_track(t) for t in tracks_raw),
        markers=tuple(_parse_marker(m) for m in markers_raw),
    )


def _parse_snapshot_dict(data: dict[str, Any]) -> InsightSnapshot:
    path = "snapshot"
    _require_keys(
        data,
        {"schema_version", "capture_id", "captured_at", "project", "sequences", "assets"},
        {"schema_version", "capture_id", "captured_at", "project", "sequences",
         "assets", "payload_hash", "content_hash"},
        path,
    )
    schema = _get_str(data, "schema_version", path)
    if schema != SCHEMA_VERSION:
        raise SnapshotParseError(
            "UNSUPPORTED_SCHEMA",
            f"snapshot schema {schema!r} != supported {SCHEMA_VERSION!r}",
            path,
        )
    project_raw = data["project"]
    if not isinstance(project_raw, dict):
        raise SnapshotParseError("INVALID_VALUE", "project must be object", path)
    _require_keys(
        project_raw, {"project_id", "name", "revision"},
        {"project_id", "name", "revision"}, f"{path}.project",
    )
    sequences_raw = data["sequences"]
    assets_raw = data["assets"]
    if not isinstance(sequences_raw, list) or not isinstance(assets_raw, list):
        raise SnapshotParseError("INVALID_VALUE", "sequences/assets must be lists", path)
    captured_at = _get_float_or_none(data, "captured_at", path)
    if captured_at is None:
        raise SnapshotParseError("INVALID_VALUE", "captured_at must be number", path)
    snapshot = InsightSnapshot(
        schema_version=schema,
        capture_id=_get_str(data, "capture_id", path),
        captured_at=captured_at,
        project=ProjectEntry(
            project_id=_get_str(project_raw, "project_id", f"{path}.project"),
            name=_get_str(project_raw, "name", f"{path}.project"),
            revision=_get_int(project_raw, "revision", f"{path}.project"),
        ),
        sequences=tuple(_parse_sequence(s) for s in sequences_raw),
        assets=tuple(_parse_asset(a) for a in assets_raw),
        payload_hash=_get_str_or_none(data, "payload_hash", path) if "payload_hash" in data else None,
        content_hash=_get_str_or_none(data, "content_hash", path) if "content_hash" in data else None,
    )
    return snapshot


# ── 公开 API ─────────────────────────────────────────────────────


def serialize_snapshot(snapshot: InsightSnapshot) -> str:
    """规范 JSON 文本（含 hash 字段；hash 缺失时先补全）。"""
    finalized = snapshot
    if not snapshot.payload_hash or not snapshot.content_hash:
        finalized = finalize_snapshot(snapshot)
    payload = snapshot_to_payload_dict(finalized)
    payload["payload_hash"] = finalized.payload_hash
    payload["content_hash"] = finalized.content_hash
    return json.dumps(payload, sort_keys=True, ensure_ascii=False, indent=1)


def _error(code: str, message: str, path: str = "") -> SnapshotParseErrorInfo:
    return SnapshotParseErrorInfo(code=code, message=message, path=path)


def parse_snapshot(text: str | bytes) -> SnapshotParseResult:
    """解析并校验（schema/字段/时间基/payload hash）。结构化失败不抛异常。"""
    try:
        data = json.loads(text)
    except (ValueError, TypeError) as exc:
        return SnapshotParseResult(
            ok=False, error=_error("INVALID_JSON", f"json parse failed: {exc}")
        )
    if not isinstance(data, dict):
        return SnapshotParseResult(
            ok=False, error=_error("INVALID_VALUE", "top level must be object")
        )
    try:
        snapshot = _parse_snapshot_dict(data)
    except SnapshotParseError as exc:
        return SnapshotParseResult(
            ok=False,
            error=_error(exc.code, exc.message, exc.path),
        )
    # payload hash：存了就必须对得上（重算基于解析后重建的规范载荷）。
    if snapshot.payload_hash is not None:
        if compute_payload_hash(snapshot) != snapshot.payload_hash:
            return SnapshotParseResult(
                ok=False,
                error=_error(
                    "HASH_MISMATCH",
                    "payload_hash does not match recomputed canonical payload",
                ),
            )
    return SnapshotParseResult(ok=True, snapshot=snapshot)
