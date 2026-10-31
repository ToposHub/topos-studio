"""SemanticSegment 索引与查询（Phase 1）。

把不同 provider 的素材分析结果归一为可查询的 ``SemanticSegment``，让 AI 精确
表达“选择 a-interview 的 1250–1425 帧，因为这段覆盖 claim-fast-charge”（TAEP §5.4）。

本模块是**查询层**：不直接依赖某个模型 provider，也不直接写 timeline。provider
的输出在 P5 转换为 segment 后加入索引；planner/validator 只读查询。

规则（TAEP §5.4）：

- 分析结果只能提出剪辑候选，不能直接改变工程。
- ``user_verified`` 的 segment 优先于 AI 推断；二者分层保存。
- 支持 asset/kind/range/label 的过滤；分页避免把数小时转录一次塞进模型上下文。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Iterable, Sequence

from src.features.edit.ai_editing.editorial_models import (
    FrameRange,
    SegmentKind,
    SemanticSegment,
)

if TYPE_CHECKING:
    from src.features.edit.ai_editing.analysis_service import AnalysisService


@dataclass(frozen=True)
class EvidenceSearchResult:
    """A search hit with its complete evidence list, never a bare label."""

    segment: SemanticSegment
    score: float
    matched_fields: tuple[str, ...] = ()
    evidence: tuple[dict[str, Any], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "score": self.score,
            "matched_fields": list(self.matched_fields),
            "evidence": [dict(item) for item in self.evidence],
        }


class SemanticIndex:
    """内存中的 SemanticSegment 索引（P1 不持久化；P5 接 AssetAnalysis 存储）。

    线程模型：本类本身不做并发控制；调用方（service 层）负责。P1 用于测试与
    规划器的只读查询。
    """

    def __init__(self) -> None:
        self._segments: dict[str, SemanticSegment] = {}
        self._embeddings: dict[str, tuple[float, ...]] = {}

    # ---------------- 写入 ---------------- #

    def add(self, segment: SemanticSegment) -> None:
        self._segments[segment.segment_id] = segment

    def add_many(self, segments: Iterable[SemanticSegment]) -> None:
        for s in segments:
            self.add(s)

    def remove(self, segment_id: str) -> None:
        self._segments.pop(segment_id, None)
        self._embeddings.pop(segment_id, None)

    def clear(self) -> None:
        self._segments.clear()
        self._embeddings.clear()

    def add_embedding(self, segment_id: str, vector: Sequence[float]) -> None:
        """Attach a rebuildable vector cache entry to an indexed segment."""
        if segment_id not in self._segments:
            raise KeyError(f"cannot attach embedding to unknown segment: {segment_id}")
        values = tuple(float(value) for value in vector)
        if not values or not all(math.isfinite(value) for value in values):
            raise ValueError("embedding must be a non-empty finite vector")
        self._embeddings[segment_id] = values

    def __len__(self) -> int:
        return len(self._segments)

    def __contains__(self, segment_id: str) -> bool:
        return segment_id in self._segments

    def get(self, segment_id: str) -> SemanticSegment | None:
        return self._segments.get(segment_id)

    # ---------------- 查询 ---------------- #

    def for_asset(self, asset_id: str) -> list[SemanticSegment]:
        """某素材的全部 segment。"""
        return [s for s in self._segments.values() if s.asset_id == asset_id]

    def query(
        self,
        *,
        asset_id: str | None = None,
        kind: SegmentKind | str | None = None,
        within: FrameRange | None = None,
        label_contains: str | None = None,
        user_verified_only: bool = False,
        min_confidence: float = 0.0,
        limit: int | None = None,
    ) -> list[SemanticSegment]:
        """多维过滤查询。

        Args:
            asset_id: 限定素材。
            kind: 限定 segment 类型。
            within: 只返回与该 range 相交的 segment（基于 source_range）。
            label_contains: 在 payload 的常见文本字段里模糊匹配。
            user_verified_only: 只返回用户确认过的 segment。
            min_confidence: 最低置信度阈值。
            limit: 最多返回数（分页）。
        """
        kind_val = kind.value if isinstance(kind, SegmentKind) else kind
        results: list[SemanticSegment] = []
        for s in self._segments.values():
            if asset_id is not None and s.asset_id != asset_id:
                continue
            if kind_val is not None and s.kind.value != kind_val:
                continue
            if user_verified_only and not s.user_verified:
                continue
            if s.confidence < min_confidence:
                continue
            if within is not None and not s.source_range.overlaps(within):
                continue
            if label_contains is not None:
                if not _segment_matches_label(s, label_contains):
                    continue
            results.append(s)
        # 稳定排序：user_verified 优先，然后 confidence 降序，最后按 range.start
        results.sort(
            key=lambda s: (not s.user_verified, -s.confidence, s.source_range.start)
        )
        if limit is not None:
            results = results[:limit]
        return results

    def coverage(self, asset_id: str, kind: SegmentKind | None = None) -> list[tuple[int, int]]:
        """返回某素材被指定类型 segment 覆盖的帧区间列表（合并重叠）。

        用于 planner 判断“转录是否覆盖目标时长”。返回 ``[(start, end), ...]``，
        按 start 升序、已合并相交区间。
        """
        segs = self.query(asset_id=asset_id, kind=kind)
        intervals = [(s.source_range.start, s.source_range.end) for s in segs]
        if not intervals:
            return []
        intervals.sort()
        merged: list[tuple[int, int]] = [intervals[0]]
        for start, end in intervals[1:]:
            last_start, last_end = merged[-1]
            if start <= last_end:  # 相交或相邻
                merged[-1] = (last_start, max(last_end, end))
            else:
                merged.append((start, end))
        return merged

    def search_evidence(
        self,
        text: str,
        *,
        asset_id: str | None = None,
        limit: int = 20,
        min_confidence: float = 0.0,
    ) -> list[EvidenceSearchResult]:
        """Search text/OCR/tag/caption and return evidence lists with frames."""
        needle = text.strip().casefold()
        if not needle:
            return []
        results: list[EvidenceSearchResult] = []
        for segment in self._segments.values():
            if asset_id is not None and segment.asset_id != asset_id:
                continue
            if segment.confidence < min_confidence:
                continue
            fields = _matching_fields(segment, needle)
            if not fields:
                continue
            exact = any(needle == value.casefold() for value in _segment_text_values(segment))
            score = min(1.0, segment.confidence * (1.0 if exact else 0.8) + 0.2 * len(fields))
            results.append(EvidenceSearchResult(
                segment=segment, score=score, matched_fields=tuple(fields),
                evidence=(segment.to_dict(),),
            ))
        results.sort(key=lambda item: (-item.score, item.segment.source_range.start))
        return results[:max(0, limit)]

    def similarity_search(
        self,
        vector: Sequence[float],
        *,
        asset_id: str | None = None,
        limit: int = 20,
        min_score: float = -1.0,
    ) -> list[EvidenceSearchResult]:
        """Cosine search over external embeddings with the same evidence shape."""
        query = tuple(float(value) for value in vector)
        qnorm = math.sqrt(sum(value * value for value in query))
        if not query or qnorm == 0.0:
            return []
        results: list[EvidenceSearchResult] = []
        for segment_id, embedding in self._embeddings.items():
            segment = self._segments.get(segment_id)
            if segment is None or (asset_id is not None and segment.asset_id != asset_id):
                continue
            if len(embedding) != len(query):
                continue
            denom = qnorm * math.sqrt(sum(value * value for value in embedding))
            score = sum(a * b for a, b in zip(query, embedding)) / denom if denom else -1.0
            if score < min_score:
                continue
            results.append(EvidenceSearchResult(
                segment=segment, score=score, matched_fields=("embedding",),
                evidence=(segment.to_dict(),),
            ))
        results.sort(key=lambda item: (-item.score, item.segment.source_range.start))
        return results[:max(0, limit)]


def _segment_matches_label(segment: SemanticSegment, needle: str) -> bool:
    """在 segment payload 的常见文本字段里模糊匹配（大小写不敏感）。

    transcript: ``text``；tag: ``labels``；ocr: ``text``；shot: ``description``。
    """
    payload = segment.payload or {}
    needle_lower = needle.lower()
    # 单字段文本
    for key in ("text", "description", "label", "caption"):
        val = payload.get(key)
        if isinstance(val, str) and needle_lower in val.lower():
            return True
    # labels 列表
    labels = payload.get("labels", payload.get("tags"))
    if isinstance(labels, (list, tuple)):
        for lab in labels:
            if isinstance(lab, str) and needle_lower in lab.lower():
                return True
    return False


def _segment_text_values(segment: SemanticSegment) -> tuple[str, ...]:
    payload = segment.payload or {}
    values: list[str] = []
    for key in ("text", "description", "label", "caption"):
        value = payload.get(key)
        if isinstance(value, str):
            values.append(value)
    for key in ("labels", "tags"):
        value = payload.get(key)
        if isinstance(value, (list, tuple)):
            values.extend(str(item) for item in value if isinstance(item, str))
    return tuple(values)


def _matching_fields(segment: SemanticSegment, needle: str) -> list[str]:
    payload = segment.payload or {}
    fields: list[str] = []
    for key, value in payload.items():
        if isinstance(value, str) and needle in value.casefold():
            fields.append(key)
        elif isinstance(value, (list, tuple)) and any(
            isinstance(item, str) and needle in item.casefold() for item in value
        ):
            fields.append(key)
    return fields


def build_index_from_analysis(
    analysis: "AnalysisService",
    asset_ids: Iterable[str],
) -> SemanticIndex:
    """从 ``AnalysisService`` 拉指定 asset 的 segments 建 ``SemanticIndex``。

    TAEP §5.4 的唯一共享实现：workflow（路径 B）与 host bridge（路径 A，自由对话/
    legacy brief）都通过本函数构建索引，避免"证据收集"逻辑漂移成两份。

    规则：
    - 只读取 ``analysis.get_segments(asset_id)``，不写、不触发分析。
    - 无证据的 asset 贡献 0 个 segment（planner 据此报"请先分析素材"）。
    - 不依赖 Qt / 不触碰 timeline。
    """
    index = SemanticIndex()
    for asset_id in asset_ids:
        for seg in analysis.get_segments(asset_id):
            index.add(seg)
            get_embedding = getattr(analysis, "get_embedding", None)
            embedding = get_embedding(seg.segment_id) if callable(get_embedding) else None
            if embedding is not None:
                index.add_embedding(seg.segment_id, embedding)
    return index


__all__ = ["EvidenceSearchResult", "SemanticIndex", "build_index_from_analysis"]
