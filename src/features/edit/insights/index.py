"""T20 · 搜索索引缓存：按 (capture_id, content_hash) 失效的有界缓存。

只索引搜索需要的列（实体身份 + 小写文本字段 + 时间区间），不复制原始
图像/音频/GPU 缓存。同 content_hash 的重捕获（例如仅 revision/导航变
化）直接复用旧索引；内容变化（content_hash 不同）自动失效重建。容量
有界（LRU），默认 4 条快照。
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from dataclasses import dataclass

from src.features.edit.insights.dto import (
    LOAD_STATE_LOADED,
    InsightSnapshot,
)


@dataclass(frozen=True)
class SearchRow:
    """可搜行（列裁剪后的最小字段集）。"""

    entity_type: str            # clip/cue/marker/asset
    sequence_id: str | None
    sequence_name: str
    track_id: str | None
    track_type: str | None
    entity_id: str
    label: str                  # clip→素材显示名；cue→text；marker/asset→显示名
    extra_texts: tuple[str, ...]   # 其余可搜字段（comment/keywords/...）
    timeline_start: int | None
    timeline_end: int | None


def build_search_rows(snapshot: InsightSnapshot) -> tuple[SearchRow, ...]:
    """从快照构建可搜行集（纯函数；只留需要字段）。"""
    asset_by_id = {a.asset_id: a for a in snapshot.assets}
    rows: list[SearchRow] = []
    for seq in snapshot.sequences:
        if seq.load_state != LOAD_STATE_LOADED:
            continue
        for track in seq.tracks:
            for clip in track.clips:
                asset = asset_by_id.get(clip.asset_id)
                label = asset.display_name if asset else clip.asset_id
                rows.append(SearchRow(
                    entity_type="clip",
                    sequence_id=seq.sequence_id,
                    sequence_name=seq.name,
                    track_id=track.track_id,
                    track_type=track.track_type,
                    entity_id=clip.clip_id,
                    label=label.lower(),
                    extra_texts=(),
                    timeline_start=clip.timeline_start,
                    timeline_end=clip.timeline_end,
                ))
            for cue in track.cues:
                rows.append(SearchRow(
                    entity_type="cue",
                    sequence_id=seq.sequence_id,
                    sequence_name=seq.name,
                    track_id=track.track_id,
                    track_type=track.track_type,
                    entity_id=cue.clip_id,
                    label=cue.text.lower(),
                    extra_texts=(),
                    timeline_start=cue.timeline_start,
                    timeline_end=cue.timeline_end,
                ))
        for marker in seq.markers:
            rows.append(SearchRow(
                entity_type="marker",
                sequence_id=seq.sequence_id,
                sequence_name=seq.name,
                track_id=marker.track_id,
                track_type=None,
                entity_id=marker.marker_id,
                label=marker.name.lower(),
                extra_texts=(marker.comment.lower(),),
                timeline_start=marker.frame,
                timeline_end=marker.frame,
            ))
    for asset in snapshot.assets:
        rows.append(SearchRow(
            entity_type="asset",
            sequence_id=None,
            sequence_name="",
            track_id=None,
            track_type=None,
            entity_id=asset.asset_id,
            label=asset.display_name.lower(),
            extra_texts=tuple(
                t.lower()
                for t in (
                    asset.file_basename, asset.comment, asset.scene,
                    asset.shot, asset.take, asset.reel,
                    " ".join(asset.keywords),
                )
                if t
            ),
            timeline_start=None,
            timeline_end=None,
        ))
    return tuple(rows)


class SearchIndexCache:
    """(capture_id, content_hash) → rows 的有界 LRU 缓存（线程安全）。"""

    def __init__(self, capacity: int = 4) -> None:
        self._capacity = max(1, capacity)
        self._lock = threading.Lock()
        self._cache: "OrderedDict[tuple[str, str], tuple[SearchRow, ...]]" = OrderedDict()
        self._stats = {"hits": 0, "misses": 0, "evictions": 0}

    def rows_for(self, snapshot: InsightSnapshot) -> tuple[SearchRow, ...]:
        if snapshot.content_hash is None:
            raise ValueError("snapshot must be finalized (content_hash present)")
        key = (snapshot.capture_id, snapshot.content_hash)
        with self._lock:
            cached = self._cache.get(key)
            if cached is not None:
                self._cache.move_to_end(key)
                self._stats["hits"] += 1
                return cached
        rows = build_search_rows(snapshot)
        with self._lock:
            self._stats["misses"] += 1
            self._cache[key] = rows
            while len(self._cache) > self._capacity:
                self._cache.popitem(last=False)
                self._stats["evictions"] += 1
        return rows

    def invalidate_capture(self, capture_id: str) -> int:
        """指定捕获（refresh 后旧捕获）全部失效；返回清除条数。"""
        with self._lock:
            stale = [k for k in self._cache if k[0] == capture_id]
            for key in stale:
                del self._cache[key]
            return len(stale)

    def clear(self) -> None:
        with self._lock:
            self._cache.clear()

    @property
    def stats(self) -> dict[str, int]:
        with self._lock:
            return dict(self._stats)

    def __len__(self) -> int:
        with self._lock:
            return len(self._cache)
