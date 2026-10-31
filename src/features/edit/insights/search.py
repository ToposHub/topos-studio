"""T10 · 跨序列联合搜索（纯 DTO 层，无 Qt/无语义模型）。

统一检索片段名（clip→素材显示名）、素材名/日志字段、marker、字幕/
转录文本；匹配语义与既有 ``caption_workspace.find_in_clips`` 一致：
字面子串（re.escape 语义）+ 默认大小写不敏感，中文/标点/Unicode 天然
可用。

纪律（计划 T10）：

- 结果带 project/sequence/track/clip/cue 真实身份；重复出现各自独立
  成条（同素材两次使用/重复字幕/同名片段不合并）；
- 未加载序列进 ``excluded_unloaded``（不伪报零结果）；缺转录=没有
  cue 可搜，不自动 ASR、不调用外部模型；
- 空串/纯空白 → 不命中（与 find_in_clips 语义一致）；
- 查询语法只支持受限前缀 ``type:``/``timeline:``（timeline 按序列名
  称子串解析），未知前缀保留为普通文本；绝不 eval。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from typing import Iterable

from src.features.edit.insights.dto import InsightSnapshot

# 实体类型（稳定字符串，供 UI/导出引用）
ENTITY_CLIP = "clip"
ENTITY_CUE = "cue"
ENTITY_MARKER = "marker"
ENTITY_ASSET = "asset"
_ALL_ENTITIES = (ENTITY_CLIP, ENTITY_CUE, ENTITY_MARKER, ENTITY_ASSET)

# type: 前缀别名（用户友好，内部归一为 ENTITY_*）
_TYPE_ALIASES = {
    "clip": ENTITY_CLIP, "clips": ENTITY_CLIP,
    "cue": ENTITY_CUE, "cues": ENTITY_CUE,
    "subtitle": ENTITY_CUE, "dialog": ENTITY_CUE,
    "marker": ENTITY_MARKER, "markers": ENTITY_MARKER,
    "asset": ENTITY_ASSET, "assets": ENTITY_ASSET, "pool": ENTITY_ASSET,
}

_SAVED_QUERY_SCHEMA = "insights-saved-query/1"


@dataclass(frozen=True)
class SearchQuery:
    text: str = ""
    sequence_ids: tuple[str, ...] = ()        # 空 = 全部已加载序列（精确 id）
    timeline_name_terms: tuple[str, ...] = ()  # 序列名称子串（与 ids 取并集前先各自解析）
    track_types: tuple[str, ...] = ()         # 空 = 全部（video/audio/subtitle）
    entity_types: tuple[str, ...] = ()        # 空 = 全部实体
    min_duration_frames: int | None = None    # 只作用于 clip/cue
    max_duration_frames: int | None = None
    case_sensitive: bool = False


@dataclass(frozen=True)
class SearchHit:
    entity_type: str            # ENTITY_*
    sequence_id: str | None     # asset（素材池级）为 None
    sequence_name: str
    track_id: str | None
    track_type: str | None
    entity_id: str
    entity_label: str           # 显示名：clip→素材显示名；cue→单行化文本
    matched_field: str          # 命中字段（display_name/text/name/...）
    matched_text: str           # 命中字段全文
    context: str                # 命中点上下文片段
    timeline_start: int | None
    timeline_end: int | None

    @property
    def navigate_identity(self) -> tuple[str | None, str, str | None]:
        """(sequence_id, entity_id, track_id) —— 交给 host navigate_to 的身份。"""
        return self.sequence_id, self.entity_id, self.track_id


@dataclass(frozen=True)
class SearchResult:
    query: SearchQuery
    hits: tuple[SearchHit, ...]
    searched_sequences: tuple[str, ...]   # 实际参与搜索的 loaded 序列 id
    excluded_unloaded: tuple[str, ...]    # 未加载被排除的序列 id（如实报告）
    truncated: bool = False


# ── 受限查询语法 ─────────────────────────────────────────────────


def parse_query_string(raw: str) -> SearchQuery:
    """解析 ``type:cue timeline:ntsc 你好`` 形式的受限查询。

    只在词首识别 ``type:``/``timeline:`` 前缀；未知前缀（``foo:bar``）不
    吞并，保留为普通文本——文本搜索不丢字。无正则、无 eval。
    """
    entity_types: list[str] = []
    timeline_terms: list[str] = []
    text_parts: list[str] = []
    for token in raw.split():
        lowered = token.lower()
        if lowered.startswith("type:"):
            entity = _TYPE_ALIASES.get(lowered[len("type:"):].strip())
            if entity is not None:
                entity_types.append(entity)
                continue
        elif lowered.startswith("timeline:"):
            term = token[len("timeline:"):].strip()
            if term:
                timeline_terms.append(term)
                continue
        text_parts.append(token)
    return SearchQuery(
        text=" ".join(text_parts),
        entity_types=tuple(dict.fromkeys(entity_types)),
        timeline_name_terms=tuple(timeline_terms),
    )


# ── 搜索 ─────────────────────────────────────────────────────────


def _matches(haystack: str, needle: str, case_sensitive: bool) -> bool:
    if not needle:
        return False
    if case_sensitive:
        return needle in haystack
    return needle.lower() in haystack.lower()


def _context(text: str, needle: str, radius: int = 12) -> str:
    """命中点上下文片段（首处命中；不做大小写改写，原文截取）。"""
    idx = text.lower().find(needle.lower())
    if idx < 0:
        return text[: radius * 2]
    start = max(0, idx - radius)
    end = min(len(text), idx + len(needle) + radius)
    prefix = "…" if start > 0 else ""
    suffix = "…" if end < len(text) else ""
    return f"{prefix}{text[start:end]}{suffix}"


def _resolve_sequence_scope(query: SearchQuery, snapshot: InsightSnapshot) -> set[str] | None:
    """合并精确 id 与名称子串两种序列过滤；都为空 → None（不过滤）。"""
    ids = set(query.sequence_ids)
    if query.timeline_name_terms:
        for seq in snapshot.sequences:
            name = seq.name.lower()
            if any(term.lower() in name for term in query.timeline_name_terms):
                ids.add(seq.sequence_id)
    return ids or None


def unified_search(
    snapshot: InsightSnapshot,
    query: SearchQuery,
    *,
    limit: int = 2000,
) -> SearchResult:
    """在快照上执行联合搜索（纯内存线性扫描，无索引、无副作用）。"""
    resolved_scope = _resolve_sequence_scope(query, snapshot)
    text = query.text.strip()

    entity_filter = set(query.entity_types) or set(_ALL_ENTITIES)
    asset_by_id = {a.asset_id: a for a in snapshot.assets}

    searched: list[str] = []
    excluded: list[str] = []
    hits: list[SearchHit] = []

    def duration_ok(start: int | None, end: int | None) -> bool:
        if query.min_duration_frames is None and query.max_duration_frames is None:
            return True
        if start is None or end is None:
            return False  # 时长过滤只作用于有时间区间的实体
        duration = end - start
        if query.min_duration_frames is not None and duration < query.min_duration_frames:
            return False
        if query.max_duration_frames is not None and duration > query.max_duration_frames:
            return False
        return True

    for seq in snapshot.sequences:
        if seq.load_state != "loaded":
            excluded.append(seq.sequence_id)
            continue
        if resolved_scope is not None and seq.sequence_id not in resolved_scope:
            continue
        searched.append(seq.sequence_id)
        if not text:
            continue
        for track in seq.tracks:
            if query.track_types and track.track_type not in query.track_types:
                continue
            if ENTITY_CLIP in entity_filter and track.track_type in ("video", "audio"):
                for clip in track.clips:
                    asset = asset_by_id.get(clip.asset_id)
                    label = asset.display_name if asset else clip.asset_id
                    if _matches(label, text, query.case_sensitive) and duration_ok(
                        clip.timeline_start, clip.timeline_end
                    ):
                        hits.append(SearchHit(
                            entity_type=ENTITY_CLIP,
                            sequence_id=seq.sequence_id,
                            sequence_name=seq.name,
                            track_id=track.track_id,
                            track_type=track.track_type,
                            entity_id=clip.clip_id,
                            entity_label=label,
                            matched_field="display_name",
                            matched_text=label,
                            context=_context(label, text),
                            timeline_start=clip.timeline_start,
                            timeline_end=clip.timeline_end,
                        ))
            if ENTITY_CUE in entity_filter and track.track_type == "subtitle":
                for cue in track.cues:
                    if not _matches(cue.text, text, query.case_sensitive):
                        continue
                    if not duration_ok(cue.timeline_start, cue.timeline_end):
                        continue
                    hits.append(SearchHit(
                        entity_type=ENTITY_CUE,
                        sequence_id=seq.sequence_id,
                        sequence_name=seq.name,
                        track_id=track.track_id,
                        track_type=track.track_type,
                        entity_id=cue.clip_id,
                        entity_label=cue.text.replace("\n", " ⏎ "),
                        matched_field="text",
                        matched_text=cue.text,
                        context=_context(cue.text, text),
                        timeline_start=cue.timeline_start,
                        timeline_end=cue.timeline_end,
                    ))
        if text and ENTITY_MARKER in entity_filter:
            for marker in seq.markers:
                # 字段优先级：name → comment；同一 marker 只出一条
                for field_name, field_text in (
                    ("name", marker.name),
                    ("comment", marker.comment),
                ):
                    if field_text and _matches(field_text, text, query.case_sensitive):
                        hits.append(SearchHit(
                            entity_type=ENTITY_MARKER,
                            sequence_id=seq.sequence_id,
                            sequence_name=seq.name,
                            track_id=marker.track_id,
                            track_type=None,
                            entity_id=marker.marker_id,
                            entity_label=marker.name or marker.marker_id,
                            matched_field=field_name,
                            matched_text=field_text,
                            context=_context(field_text, text),
                            timeline_start=marker.frame,
                            timeline_end=marker.frame,
                        ))
                        break

    if text and ENTITY_ASSET in entity_filter:
        for asset in snapshot.assets:
            # 字段优先级：display_name → file_basename → comment →
            # scene/shot/take/reel → keywords；同一素材只出一条
            for field_name, field_text in (
                ("display_name", asset.display_name),
                ("file_basename", asset.file_basename),
                ("comment", asset.comment),
                ("scene", asset.scene),
                ("shot", asset.shot),
                ("take", asset.take),
                ("reel", asset.reel),
                ("keywords", " ".join(asset.keywords)),
            ):
                if field_text and _matches(field_text, text, query.case_sensitive):
                    hits.append(SearchHit(
                        entity_type=ENTITY_ASSET,
                        sequence_id=None,
                        sequence_name="",
                        track_id=None,
                        track_type=None,
                        entity_id=asset.asset_id,
                        entity_label=asset.display_name or asset.file_basename,
                        matched_field=field_name,
                        matched_text=field_text,
                        context=_context(field_text, text),
                        timeline_start=None,
                        timeline_end=None,
                    ))
                    break

    truncated = len(hits) > limit
    return SearchResult(
        query=replace(query, text=text),
        hits=tuple(hits[:limit]),
        searched_sequences=tuple(searched),
        excluded_unloaded=tuple(excluded),
        truncated=truncated,
    )


# ── 收藏查询导出（自有 JSON）────────────────────────────────────


def export_saved_queries(path: str, queries: Iterable[SearchQuery], *, name: str = "") -> None:
    payload = {
        "schema": _SAVED_QUERY_SCHEMA,
        "name": name,
        "queries": [
            {
                "text": q.text,
                "sequence_ids": list(q.sequence_ids),
                "timeline_name_terms": list(q.timeline_name_terms),
                "track_types": list(q.track_types),
                "entity_types": list(q.entity_types),
                "min_duration_frames": q.min_duration_frames,
                "max_duration_frames": q.max_duration_frames,
                "case_sensitive": q.case_sensitive,
            }
            for q in queries
        ],
    }
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=1)


_QUERY_KEYS = {
    "text", "sequence_ids", "timeline_name_terms", "track_types",
    "entity_types", "min_duration_frames", "max_duration_frames",
    "case_sensitive",
}


def load_saved_queries(path: str) -> list[SearchQuery]:
    with open(path, "r", encoding="utf-8") as fh:
        payload = json.load(fh)
    if not isinstance(payload, dict) or payload.get("schema") != _SAVED_QUERY_SCHEMA:
        raise ValueError(f"unknown saved-query schema: {payload.get('schema')!r}")
    queries: list[SearchQuery] = []
    for entry in payload.get("queries", []):
        if not isinstance(entry, dict):
            raise ValueError("saved query entry must be object")
        missing = _QUERY_KEYS - set(entry)
        unknown = set(entry) - _QUERY_KEYS
        if missing or unknown:
            raise ValueError(
                f"saved query entry keys invalid: "
                f"missing={sorted(missing)} unknown={sorted(unknown)}"
            )
        queries.append(SearchQuery(
            text=str(entry["text"]),
            sequence_ids=tuple(entry["sequence_ids"]),
            timeline_name_terms=tuple(entry["timeline_name_terms"]),
            track_types=tuple(entry["track_types"]),
            entity_types=tuple(entry["entity_types"]),
            min_duration_frames=entry["min_duration_frames"],
            max_duration_frames=entry["max_duration_frames"],
            case_sensitive=bool(entry["case_sensitive"]),
        ))
    return queries
