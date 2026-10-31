"""TAEP M7 — 字幕初稿（subtitle_draft）测试。

验收（§11.1 M7 / §15.2 / M8 边界）：

- 从 TRANSCRIPT segment 产句级字幕，每条带 evidence_ref + 匿名 speaker。
- 只取 TRANSCRIPT：OCR/shot/tag/audio 段被忽略（M8 untrusted-data 边界）。
- 阅读速度（cps）超过阈值 → 进 over_fast_lines（可访问性质量信号，不阻断）。
- 无转录段 → 空结果（不抛错；上层 runner 据此走降级）。
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.edit_core

from src.features.edit.ai_editing.editorial_models import (
    FrameRange,
    SegmentKind,
    SemanticSegment,
)
from src.features.edit.ai_editing.subtitle_draft import (
    DEFAULT_MAX_CPS,
    SubtitleDraftResult,
    SubtitleLine,
    build_subtitle_draft,
)


def _transcript(seg_id: str, start: int, duration: int, text: str,
                *, speaker: str | None = "S1", confidence: float = 0.9) -> SemanticSegment:
    return SemanticSegment(
        segment_id=seg_id, asset_id="a1", source_range=FrameRange(start, duration),
        kind=SegmentKind.TRANSCRIPT, payload={"text": text, "speaker": speaker},
        confidence=confidence,
    )


def _shot(seg_id: str, start: int, duration: int) -> SemanticSegment:
    return SemanticSegment(
        segment_id=seg_id, asset_id="a1", source_range=FrameRange(start, duration),
        kind=SegmentKind.SHOT, payload={"description": "产品特写"}, confidence=0.9,
    )


def _ocr(seg_id: str, start: int, duration: int, text: str) -> SemanticSegment:
    return SemanticSegment(
        segment_id=seg_id, asset_id="a1", source_range=FrameRange(start, duration),
        kind=SegmentKind.OCR, payload={"text": text}, confidence=0.8,
    )


# ================================================================
# golden：从转录产字幕
# ================================================================


def test_build_draft_from_transcript_segments():
    segs = [
        _transcript("t0", 0, 120, "大家好，今天介绍新产品"),
        _transcript("t1", 120, 240, "它的续航非常出色"),
        _transcript("t2", 360, 120, "立即点击了解"),
    ]
    draft = build_subtitle_draft(segs, fps=24.0)
    assert isinstance(draft, SubtitleDraftResult)
    assert len(draft.lines) == 3
    # 每条带强制证据引用
    for ln in draft.lines:
        assert isinstance(ln, SubtitleLine)
        assert ln.evidence_ref  # 非空
        assert ln.text
        assert ln.speaker in ("S1", "S2")  # 匿名（M4 红线）
    # 证据 ref 对应原始 segment id
    refs = {ln.evidence_ref for ln in draft.lines}
    assert refs == {"t0", "t1", "t2"}
    # source_range 透传（整帧，无 seconds×fps 换算）
    assert draft.lines[0].source_range == FrameRange(0, 120)


def test_lines_sorted_by_time():
    segs = [
        _transcript("t2", 360, 120, "立即点击"),
        _transcript("t0", 0, 120, "开场"),
        _transcript("t1", 120, 240, "产品"),
    ]
    draft = build_subtitle_draft(segs)
    starts = [ln.source_range.start for ln in draft.lines]
    assert starts == sorted(starts)
    assert starts == [0, 120, 360]


def test_source_kind_is_transcript():
    draft = build_subtitle_draft([_transcript("t0", 0, 120, "x")])
    assert draft.source_kind == "transcript"


# ================================================================
# M8 边界：OCR 等 untrusted data 不进字幕
# ================================================================


def test_ocr_segments_ignored():
    """OCR 文字是 untrusted data（§9 / §18.1），首版不进字幕。"""
    segs = [
        _transcript("t0", 0, 120, "口播台词"),
        _ocr("o0", 0, 60, "屏幕文字"),  # 应被忽略
    ]
    draft = build_subtitle_draft(segs)
    assert len(draft.lines) == 1
    assert draft.lines[0].evidence_ref == "t0"


def test_shot_tag_audio_ignored():
    segs = [
        _transcript("t0", 0, 120, "台词"),
        _shot("s0", 0, 72),
    ]
    draft = build_subtitle_draft(segs)
    assert len(draft.lines) == 1


def test_only_ocr_yields_empty():
    """只有 OCR（无转录）→ 空结果（不抛错）。"""
    draft = build_subtitle_draft([_ocr("o0", 0, 60, "屏幕文字")])
    assert draft.empty
    assert draft.lines == ()


def test_empty_segments_yields_empty():
    draft = build_subtitle_draft([])
    assert draft.empty
    assert draft.average_cps is None
    assert draft.over_fast_lines == ()


def test_empty_text_transcript_skipped():
    """无文本的转录段（异常）跳过，不产空字幕行。"""
    seg = SemanticSegment(
        segment_id="t0", asset_id="a1", source_range=FrameRange(0, 120),
        kind=SegmentKind.TRANSCRIPT, payload={"text": "   "}, confidence=0.9,
    )
    draft = build_subtitle_draft([seg])
    assert draft.empty


# ================================================================
# 可访问性：阅读速度（cps）
# ================================================================


def test_over_fast_lines_detected():
    """极短时长 + 长文本 → cps 过快，进 over_fast_lines。"""
    # 24 帧 / 24fps = 1 秒；文本 22 字 → 22 cps > 15 阈值
    fast = _transcript("fast", 0, 24, "这是一段非常长的字幕文本用于测试过快检测逻辑")
    slow = _transcript("slow", 24, 240, "正常速度")
    draft = build_subtitle_draft([fast, slow], max_cps=15.0, fps=24.0)
    # over_fast_lines 存的是 line_id；通过 evidence_ref 回溯到 segment id
    fast_line_ids = _line_ids(draft, {fast.segment_id})
    assert any(lid in draft.over_fast_lines for lid in fast_line_ids)
    # slow 不在过快列表
    slow_line_ids = _line_ids(draft, {slow.segment_id})
    assert all(lid not in draft.over_fast_lines for lid in slow_line_ids)


def test_custom_max_cps_threshold():
    # 240 帧 / 24fps = 10 秒；文本 6 字 → 0.6 cps > 0.5 阈值
    seg = _transcript("t0", 0, 240, "六字测试文本哦")  # 7 字
    draft = build_subtitle_draft([seg], max_cps=0.5, fps=24.0)
    # 7 字 / 10s = 0.7 cps > 0.5 → 过快
    assert len(draft.over_fast_lines) == 1


def test_average_cps_computed():
    segs = [
        _transcript("t0", 0, 240, "六十六"),   # 3字/10s = 0.3
        _transcript("t1", 240, 240, "六十六"),  # 0.3
    ]
    draft = build_subtitle_draft(segs, fps=24.0)
    assert draft.average_cps is not None
    assert abs(draft.average_cps - 0.3) < 0.01


def test_minimal_duration_cps_is_finite():
    """极短时长（1 帧）段 → cps 可计算且有限（FrameRange 不允许 duration=0）。"""
    seg = SemanticSegment(
        segment_id="t0", asset_id="a1", source_range=FrameRange(0, 1),
        kind=SegmentKind.TRANSCRIPT, payload={"text": "x"}, confidence=0.9,
    )
    draft = build_subtitle_draft([seg], fps=24.0)
    assert len(draft.lines) == 1
    # 1帧/24fps ≈ 0.042s，1字 → ~24 cps；非 None
    assert draft.lines[0].reading_speed_cps is not None
    assert draft.lines[0].reading_speed_cps > 0


# ================================================================
# to_dict 序列化
# ================================================================


def test_to_dict_roundtrip_shape():
    draft = build_subtitle_draft([_transcript("t0", 0, 120, "测试")], fps=24.0)
    d = draft.to_dict()
    assert d["source_kind"] == "transcript"
    assert isinstance(d["lines"], list)
    assert "evidence_ref" in d["lines"][0]
    assert "over_fast_lines" in d


def test_invalid_fps_raises():
    with pytest.raises(ValueError):
        build_subtitle_draft([_transcript("t0", 0, 120, "x")], fps=0)


# ================================================================
# 辅助
# ================================================================


def _line_ids(draft: SubtitleDraftResult, seg_ids: set[str]) -> list[str]:
    """从 draft 里找出对应 seg_id 的 line_id。"""
    return [ln.line_id for ln in draft.lines if ln.evidence_ref in seg_ids]
