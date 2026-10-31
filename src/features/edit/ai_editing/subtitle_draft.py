"""TAEP M7 — 字幕初稿（从转录证据归整为可编辑 typed artifact）。

字幕初稿把转录证据归整成**可编辑的 typed artifact**（用户首选项，
§11.1 M6 / §15.2）。AUD-14504 更正：``ADD_SUBTITLE`` 现已接入编译器
（``plan_compiler.COMPILABLE_OPERATIONS``），确认后的初稿可经该操作
直接编译进时间线，不再"强提交必 ``CompileError``"。

红线（§11.1 M7 / §18.1）：

- **只从 ``SegmentKind.TRANSCRIPT`` 生成**。OCR/字幕/网页文字是 untrusted data（§9），
  不能未经用户确认进入字幕（M8 视觉语义分析后才有 OCR 证据，首版明确忽略）。
- **每条 line 强制带 ``evidence_ref``**（M7：每个关键产物带证据）。
- **speaker 仅匿名 S1/S2**（M4 红线：不得把聚类结果宣称为真实身份）——直接沿用转录
  段已有的匿名 speaker，不在此处重新命名。
- ``over_fast_lines`` 是**可访问性质量信号**（§15.2 "阅读速度诊断"），只提示不阻断。

本模块是纯函数（无副作用、无 Qt、无 IO）；artifact 化与持久化由 ``WorkflowRunner`` /
``ArtifactStore`` 负责。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any, Sequence

from src.features.edit.ai_editing.editorial_models import (
    FrameRange,
    SegmentKind,
    SemanticSegment,
)

# 可访问性：每秒字符数阈值（§15.2）。超过 = 阅读速度过快，进 over_fast_lines。
# 15 cps 是常见广播/短视频字幕经验上限；只作质量信号，不阻断产出。
DEFAULT_MAX_CPS: float = 15.0

# 首版只从转录生成（M8 边界：OCR 不进字幕）。
_SOURCE_KIND_TRANSCRIPT = "transcript"


@dataclass(frozen=True)
class SubtitleLine:
    """字幕单行（句级）。

    ``source_range`` 来自 ``TranscriptSegment.to_frame_range()``（已是整帧半开区间，
    无需 ``seconds × fps`` 换算——M2 红线）。
    """

    line_id: str
    source_range: FrameRange
    text: str
    speaker: str | None  # 匿名 S1/S2（来自转录段，M4 红线）
    evidence_ref: str  # SemanticSegment.segment_id（强制引用）
    confidence: float
    reading_speed_cps: float | None  # 每秒字符数（None = 时长为 0 无法计算）

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "line_id": self.line_id,
            "source_range": self.source_range.to_dict(),
            "text": self.text,
            "evidence_ref": self.evidence_ref,
            "confidence": self.confidence,
        }
        if self.speaker is not None:
            d["speaker"] = self.speaker
        if self.reading_speed_cps is not None:
            d["reading_speed_cps"] = round(self.reading_speed_cps, 2)
        return d


@dataclass(frozen=True)
class SubtitleDraftResult:
    """字幕初稿产物（只读）。

    ``over_fast_lines`` 是 line_id 列表（阅读速度超过阈值的行）。
    ``average_cps`` 是所有可计算行的平均值（None = 无可计算行）。
    """

    lines: tuple[SubtitleLine, ...]
    average_cps: float | None
    over_fast_lines: tuple[str, ...]
    source_kind: str = _SOURCE_KIND_TRANSCRIPT

    @property
    def empty(self) -> bool:
        return not self.lines

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "lines": [ln.to_dict() for ln in self.lines],
            "over_fast_lines": list(self.over_fast_lines),
            "source_kind": self.source_kind,
        }
        if self.average_cps is not None:
            d["average_cps"] = round(self.average_cps, 2)
        return d


def build_subtitle_draft(
    segments: Sequence[SemanticSegment],
    *,
    max_cps: float = DEFAULT_MAX_CPS,
    fps: float = 24.0,
) -> SubtitleDraftResult:
    """从转录 segment 构建字幕初稿。

    Args:
        segments: 候选证据段。**只取 ``SegmentKind.TRANSCRIPT``**（M8 边界）；
            其它 kind（OCR/shot/tag/audio）被忽略，不进字幕。
        max_cps: 阅读速度阈值（字符/秒），超过则进 ``over_fast_lines``。
        fps: 用于把帧数换算为秒计算 cps。默认 24.0（与默认 target_duration 对齐）。

    Returns:
        ``SubtitleDraftResult``。无转录段时返回空结果（``empty=True``），不抛错——
        调用方（runner）在更上层判断证据缺失并走降级分支。
    """
    if fps <= 0:
        raise ValueError(f"fps must be > 0, got {fps}")

    lines: list[SubtitleLine] = []
    for seg in segments:
        # 红线：只从转录来。OCR 等 untrusted data 不进字幕（M8 边界预演）。
        if seg.kind != SegmentKind.TRANSCRIPT:
            continue
        payload = seg.payload or {}
        text = str(payload.get("text", "")).strip()
        if not text:
            # 无文本的转录段（异常）跳过，不产空字幕行。
            continue
        speaker = payload.get("speaker")
        cps = _compute_cps(seg.source_range.duration, text, fps)
        lines.append(SubtitleLine(
            line_id=f"sub-{uuid.uuid4().hex[:8]}",
            source_range=seg.source_range,
            text=text,
            speaker=speaker,
            evidence_ref=seg.segment_id,  # 强制证据引用
            confidence=float(seg.confidence),
            reading_speed_cps=cps,
        ))

    # 按 source_range.start 稳定排序（时间线顺序）。
    lines.sort(key=lambda ln: ln.source_range.start)

    over_fast = tuple(
        ln.line_id for ln in lines
        if ln.reading_speed_cps is not None and ln.reading_speed_cps > max_cps
    )
    cps_values = [ln.reading_speed_cps for ln in lines if ln.reading_speed_cps is not None]
    average_cps = sum(cps_values) / len(cps_values) if cps_values else None

    return SubtitleDraftResult(
        lines=tuple(lines),
        average_cps=average_cps,
        over_fast_lines=over_fast,
        source_kind=_SOURCE_KIND_TRANSCRIPT,
    )


def _compute_cps(duration_frames: int, text: str, fps: float) -> float | None:
    """计算每秒字符数（字符数 / 时长秒）。时长为 0 返回 None。"""
    if duration_frames <= 0:
        return None
    seconds = duration_frames / fps
    if seconds <= 0:
        return None
    return len(text) / seconds
