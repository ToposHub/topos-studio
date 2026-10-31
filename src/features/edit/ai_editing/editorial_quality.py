"""Video-editing agent gap fills: usability score, editorial operations, editorial quality (VE-01/04/12).

The audit found VE-01 lacks a unified usability score, VE-04 lacks the six
three-track editing operations (ripple/roll/slip/slide/lift/extract), and
VE-12 lacks a pre-export editorial quality evaluator.  This module fills
those three gaps in one Qt-free contract layer.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping


# --------------------------------------------------------------------------- #
# VE-01: Usability score
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class UsabilityDimension:
    """One dimension of media usability."""

    name: str               # stability / sharpness / exposure / face_presence / content_label / copyright / license
    score: float            # 0..1
    weight: float = 1.0
    reason: str = ""

    def __post_init__(self) -> None:
        if not 0.0 <= self.score <= 1.0:
            raise ValueError("usability score must be in [0, 1]")
        if self.weight < 0:
            raise ValueError("weight cannot be negative")


@dataclass(frozen=True)
class UsableRange:
    """A sub-range of an asset recommended for (or advised against) use."""

    start_frame: int
    end_frame: int
    recommendation: str         # use / avoid / trim_candidate
    reason: str = ""

    def __post_init__(self) -> None:
        if self.start_frame < 0 or self.end_frame <= self.start_frame:
            raise ValueError("UsableRange must have start < end and non-negative start")


@dataclass(frozen=True)
class UsabilityReport:
    """VE-01 aggregate: usability score + recommended ranges per asset."""

    asset_id: str
    dimensions: tuple[UsabilityDimension, ...]
    overall_score: float
    recommended_ranges: tuple[UsableRange, ...] = ()
    avoid_ranges: tuple[UsableRange, ...] = ()
    notes: str = ""

    @property
    def is_usable(self) -> bool:
        return self.overall_score >= 0.5 and not any(d.name == "copyright" and d.score < 0.1 for d in self.dimensions)

    def to_dict(self) -> dict[str, Any]:
        return {
            "asset_id": self.asset_id,
            "overall_score": self.overall_score,
            "is_usable": self.is_usable,
            "dimensions": [
                {"name": d.name, "score": d.score, "weight": d.weight, "reason": d.reason}
                for d in self.dimensions
            ],
            "recommended_ranges": [
                {"start": r.start_frame, "end": r.end_frame,
                 "recommendation": r.recommendation, "reason": r.reason}
                for r in self.recommended_ranges
            ],
            "avoid_ranges": [
                {"start": r.start_frame, "end": r.end_frame,
                 "recommendation": r.recommendation, "reason": r.reason}
                for r in self.avoid_ranges
            ],
            "notes": self.notes,
        }


def compute_usability_score(dimensions: Iterable[UsabilityDimension]) -> float:
    """Weighted average of usability dimensions."""
    dims = list(dimensions)
    if not dims:
        return 0.0
    total_weight = sum(d.weight for d in dims)
    if total_weight <= 0:
        return 0.0
    return sum(d.score * d.weight for d in dims) / total_weight


# --------------------------------------------------------------------------- #
# VE-04: Three-track editing operations (ripple/roll/slip/slide/lift/extract)
# --------------------------------------------------------------------------- #


OP_INSERT = "insert"
OP_OVERWRITE = "overwrite"
OP_LIFT = "lift"               # remove content, leave gap
OP_EXTRACT = "extract"          # remove content + close gap (ripple delete)
OP_RIPPLE_TRIM = "ripple_trim"  # trim + shift subsequent clips
OP_ROLL = "roll"                # move edit point between two adjacent clips
OP_SLIP = "slip"                # shift source in/out without changing timeline position
OP_SLIDE = "slide"              # move clip along timeline without changing duration

THREE_TRACK_OPERATIONS: frozenset[str] = frozenset({
    OP_INSERT, OP_OVERWRITE, OP_LIFT, OP_EXTRACT,
    OP_RIPPLE_TRIM, OP_ROLL, OP_SLIP, OP_SLIDE,
})


@dataclass(frozen=True)
class TimelineEditOperation:
    """One typed timeline edit (VE-04).

    These extend the existing OperationKind (which has ADD/MOVE/TRIM/SPLIT/
    DELETE/REPLACE) with the six three-track operations the audit found
    missing.
    """

    op_id: str
    kind: str
    track_id: str
    clip_id: str = ""
    target_clip_id: str = ""       # for roll (the adjacent clip)
    start_frame: int | None = None
    duration_frames: int | None = None
    source_in_frame: int | None = None    # for slip
    shift_frames: int = 0                 # for ripple / slide
    parameters: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.op_id:
            raise ValueError("TimelineEditOperation.op_id is required")
        if self.kind not in THREE_TRACK_OPERATIONS:
            raise ValueError(
                f"Unsupported three-track operation: {self.kind!r}; "
                f"expected one of {sorted(THREE_TRACK_OPERATIONS)}"
            )
        if not self.track_id:
            raise ValueError("track_id is required")

    @property
    def affects_subsequent_clips(self) -> bool:
        """Whether downstream clips shift as a side effect."""
        return self.kind in {OP_EXTRACT, OP_RIPPLE_TRIM, OP_INSERT}

    def to_dict(self) -> dict[str, Any]:
        return {
            "op_id": self.op_id,
            "kind": self.kind,
            "track_id": self.track_id,
            "clip_id": self.clip_id,
            "target_clip_id": self.target_clip_id,
            "start_frame": self.start_frame,
            "duration_frames": self.duration_frames,
            "source_in_frame": self.source_in_frame,
            "shift_frames": self.shift_frames,
            "parameters": dict(self.parameters),
            "affects_subsequent_clips": self.affects_subsequent_clips,
        }


# --------------------------------------------------------------------------- #
# VE-12: Pre-export editorial quality evaluator
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class EditorialQualityFinding:
    check: str               # duration_accuracy / jump_cut_density / audio_continuity / beat_coverage / shot_repetition / subtitle_readability / narrative_coverage
    severity: str            # error / warning / info
    score: float             # 0..1
    message: str
    affected_clips: tuple[str, ...] = ()
    fix_suggestion: str = ""


@dataclass(frozen=True)
class EditorialQualityReport:
    """VE-12 pre-export quality assessment."""

    findings: tuple[EditorialQualityFinding, ...] = ()
    checks_run: tuple[str, ...] = ()

    @property
    def errors(self) -> tuple[EditorialQualityFinding, ...]:
        return tuple(f for f in self.findings if f.severity == "error")

    @property
    def warnings(self) -> tuple[EditorialQualityFinding, ...]:
        return tuple(f for f in self.findings if f.severity == "warning")

    @property
    def is_passing(self) -> bool:
        return not self.errors

    @property
    def overall_score(self) -> float:
        if not self.findings:
            return 0.0
        return sum(f.score for f in self.findings) / len(self.findings)

    def to_dict(self) -> dict[str, Any]:
        return {
            "is_passing": self.is_passing,
            "overall_score": self.overall_score,
            "error_count": len(self.errors),
            "warning_count": len(self.warnings),
            "checks_run": list(self.checks_run),
            "findings": [
                {"check": f.check, "severity": f.severity, "score": f.score,
                 "message": f.message, "affected_clips": list(f.affected_clips),
                 "fix_suggestion": f.fix_suggestion}
                for f in self.findings
            ],
        }


def evaluate_editorial_quality(
    *,
    clip_count: int,
    total_duration_frames: int,
    expected_duration_frames: int | None = None,
    jump_cut_count: int = 0,
    audio_gaps: int = 0,
    beats_total: int = 0,
    beats_covered: int = 0,
    repeated_shot_count: int = 0,
    subtitle_cps_max: float | None = None,
    subtitle_cps_threshold: float = 20.0,
) -> EditorialQualityReport:
    """Compute a pre-export editorial quality report (VE-12).

    Each check produces a score in [0, 1] and a severity.  Errors block
    export; warnings are advisory.
    """

    findings: list[EditorialQualityFinding] = []
    checks: list[str] = []

    # Duration accuracy.
    if expected_duration_frames is not None and expected_duration_frames > 0:
        checks.append("duration_accuracy")
        ratio = min(total_duration_frames, expected_duration_frames) / max(total_duration_frames, expected_duration_frames)
        severity = "error" if ratio < 0.85 else ("warning" if ratio < 0.95 else "info")
        findings.append(EditorialQualityFinding(
            check="duration_accuracy", severity=severity, score=ratio,
            message=f"实际 {total_duration_frames} 帧，期望 {expected_duration_frames} 帧。",
            fix_suggestion="调整片段长度或更新需求时长。",
        ))

    # Jump cut density.
    if clip_count > 0:
        checks.append("jump_cut_density")
        density = jump_cut_count / max(1, clip_count)
        score = max(0.0, 1.0 - density)
        severity = "warning" if density > 0.5 else "info"
        findings.append(EditorialQualityFinding(
            check="jump_cut_density", severity=severity, score=score,
            message=f"跳剪密度 {density:.2f}（{jump_cut_count}/{clip_count}）。",
            fix_suggestion="添加 B-roll 或转场以减少跳剪。",
        ))

    # Audio continuity.
    checks.append("audio_continuity")
    if audio_gaps > 0:
        score = max(0.0, 1.0 - audio_gaps * 0.1)
        findings.append(EditorialQualityFinding(
            check="audio_continuity", severity="warning", score=score,
            message=f"检测到 {audio_gaps} 处音频断裂。",
            fix_suggestion="检查音频轨道衔接或添加交叉淡化。",
        ))

    # Beat coverage.
    if beats_total > 0:
        checks.append("beat_coverage")
        ratio = beats_covered / beats_total
        severity = "error" if ratio < 0.5 else ("warning" if ratio < 0.8 else "info")
        findings.append(EditorialQualityFinding(
            check="beat_coverage", severity=severity, score=ratio,
            message=f"叙事 beat 覆盖 {beats_covered}/{beats_total}。",
            fix_suggestion="补充未覆盖段落的素材或调整叙事。",
        ))

    # Shot repetition.
    if clip_count > 0 and repeated_shot_count > 0:
        checks.append("shot_repetition")
        ratio = repeated_shot_count / clip_count
        score = max(0.0, 1.0 - ratio)
        findings.append(EditorialQualityFinding(
            check="shot_repetition", severity="warning", score=score,
            message=f"重复镜头占比 {ratio:.0%}。",
            fix_suggestion="替换重复镜头或使用不同角度。",
        ))

    # Subtitle readability.
    if subtitle_cps_max is not None:
        checks.append("subtitle_readability")
        ratio = min(1.0, subtitle_cps_threshold / max(1.0, subtitle_cps_max))
        severity = "error" if subtitle_cps_max > subtitle_cps_threshold * 1.5 else (
            "warning" if subtitle_cps_max > subtitle_cps_threshold else "info"
        )
        findings.append(EditorialQualityFinding(
            check="subtitle_readability", severity=severity, score=ratio,
            message=f"字幕最快 {subtitle_cps_max:.1f} 字/秒（阈值 {subtitle_cps_threshold:.1f}）。",
            fix_suggestion="缩短字幕或延长显示时间。",
        ))

    return EditorialQualityReport(
        findings=tuple(findings),
        checks_run=tuple(dict.fromkeys(checks)),
    )


__all__ = [
    "UsabilityDimension",
    "UsabilityReport",
    "UsableRange",
    "compute_usability_score",
    "TimelineEditOperation",
    "EditorialQualityFinding",
    "EditorialQualityReport",
    "evaluate_editorial_quality",
    "THREE_TRACK_OPERATIONS",
    "OP_INSERT",
    "OP_OVERWRITE",
    "OP_LIFT",
    "OP_EXTRACT",
    "OP_RIPPLE_TRIM",
    "OP_ROLL",
    "OP_SLIP",
    "OP_SLIDE",
]
