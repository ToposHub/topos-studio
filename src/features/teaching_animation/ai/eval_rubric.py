# DORMANT(2026-08-19 审计): 已实现且有测试，但暂无生产入口（有意休眠）。
# 接线前请勿在生产依赖；若半年内无接线计划建议删除。
"""Creative-satisfaction evaluation rubric (MG-24).

MG-24 gives the pipeline a way to measure "is the result actually good?",
not just "does it pass the safety checks".  The rubric is a set of typed
criteria — duration accuracy, key-message coverage, readability, brand
consistency, pacing, visual overlap, subtitle/audio alignment, user-edit
adherence — each with a score in [0, 1] and a short justification.  A
:class:`CreativeEvalTrace` records one evaluation run so regressions are
detectable.

The rubric is split into *objective* checks (computable from the scene +
inputs) and *subjective* checks (filled by a human reviewer).  The objective
checks live here; the subjective ones are slots the reviewer fills.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
from typing import Any, Iterable, Mapping


# --------------------------------------------------------------------------- #
# Criteria
# --------------------------------------------------------------------------- #


CRITERION_DURATION_ACCURACY = "duration_accuracy"
CRITERION_KEY_MESSAGE_COVERAGE = "key_message_coverage"
CRITERION_READABILITY = "readability"
CRITERION_BRAND_CONSISTENCY = "brand_consistency"
CRITERION_PACING = "pacing"
CRITERION_VISUAL_OVERLAP = "visual_overlap"
CRITERION_SUBTITLE_AUDIO = "subtitle_audio"
CRITERION_USER_EDIT_ADHERENCE = "user_edit_adherence"

OBJECTIVE_CRITERIA: frozenset[str] = frozenset({
    CRITERION_DURATION_ACCURACY,
    CRITERION_KEY_MESSAGE_COVERAGE,
    CRITERION_READABILITY,
    CRITERION_VISUAL_OVERLAP,
    CRITERION_SUBTITLE_AUDIO,
})

SUBJECTIVE_CRITERIA: frozenset[str] = frozenset({
    CRITERION_BRAND_CONSISTENCY,
    CRITERION_PACING,
    CRITERION_USER_EDIT_ADHERENCE,
})

ALL_CRITERIA: frozenset[str] = OBJECTIVE_CRITERIA | SUBJECTIVE_CRITERIA


# --------------------------------------------------------------------------- #
# Score + report
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class CriterionScore:
    criterion: str
    score: float                 # 0.0 .. 1.0
    weight: float = 1.0
    justification: str = ""
    objective: bool = True
    details: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.criterion not in ALL_CRITERIA:
            raise ValueError(f"Unknown criterion: {self.criterion!r}")
        if not 0.0 <= self.score <= 1.0:
            raise ValueError("score must be in [0, 1]")
        if self.weight < 0:
            raise ValueError("weight cannot be negative")

    def to_dict(self) -> dict[str, Any]:
        return {
            "criterion": self.criterion,
            "score": self.score,
            "weight": self.weight,
            "justification": self.justification,
            "objective": self.objective,
            "details": dict(self.details),
        }


@dataclass(frozen=True)
class CreativeEvalReport:
    scores: tuple[CriterionScore, ...] = ()
    task_category: str = ""
    notes: str = ""

    @property
    def weighted_score(self) -> float:
        total_weight = sum(score.weight for score in self.scores)
        if total_weight <= 0:
            return 0.0
        return sum(score.score * score.weight for score in self.scores) / total_weight

    @property
    def objective_score(self) -> float:
        objective = [s for s in self.scores if s.objective]
        if not objective:
            return 0.0
        return sum(s.score for s in objective) / len(objective)

    def score_for(self, criterion: str) -> CriterionScore | None:
        return next((s for s in self.scores if s.criterion == criterion), None)

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_category": self.task_category,
            "weighted_score": self.weighted_score,
            "objective_score": self.objective_score,
            "scores": [s.to_dict() for s in self.scores],
            "notes": self.notes,
        }


# --------------------------------------------------------------------------- #
# Trace (for regression detection)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class CreativeEvalTrace:
    """One persisted evaluation run, used to detect quality regression."""

    trace_id: str
    timestamp: float
    task_category: str
    brief_hash: str
    plan_hash: str
    scene_hash: str
    report: CreativeEvalReport
    fixture_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "trace_id": self.trace_id,
            "timestamp": self.timestamp,
            "task_category": self.task_category,
            "brief_hash": self.brief_hash,
            "plan_hash": self.plan_hash,
            "scene_hash": self.scene_hash,
            "fixture_id": self.fixture_id,
            "report": self.report.to_dict(),
        }

    def hash(self) -> str:
        payload = {
            "task_category": self.task_category,
            "brief_hash": self.brief_hash,
            "plan_hash": self.plan_hash,
            "scene_hash": self.scene_hash,
            "report": self.report.to_dict(),
        }
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- #
# Task categories (MG-24 acceptance: 4 task classes)
# --------------------------------------------------------------------------- #


TASK_AI_AGENT_INTRO = "ai_agent_intro"
TASK_PRODUCT_AD = "product_ad"
TASK_TUTORIAL_EXPLAINER = "tutorial_explainer"
TASK_VERTICAL_SOCIAL = "vertical_social"

SUPPORTED_TASK_CATEGORIES: frozenset[str] = frozenset({
    TASK_AI_AGENT_INTRO, TASK_PRODUCT_AD, TASK_TUTORIAL_EXPLAINER, TASK_VERTICAL_SOCIAL,
})


# --------------------------------------------------------------------------- #
# Objective scorers
# --------------------------------------------------------------------------- #


def score_duration_accuracy(
    actual_frames: int,
    expected_frames: int,
) -> CriterionScore:
    """1.0 when the duration matches exactly; falls off with the relative error."""
    if expected_frames <= 0:
        return CriterionScore(
            CRITERION_DURATION_ACCURACY, 0.0,
            justification="未提供期望时长。",
        )
    ratio = min(actual_frames, expected_frames) / max(actual_frames, expected_frames)
    return CriterionScore(
        CRITERION_DURATION_ACCURACY, float(ratio),
        justification=f"实际 {actual_frames} 帧，期望 {expected_frames} 帧。",
        details={"actual": actual_frames, "expected": expected_frames},
    )


def score_key_message_coverage(
    required_keys: Iterable[str],
    covered_keys: Iterable[str],
) -> CriterionScore:
    required = list(required_keys)
    covered_set = {key for key in covered_keys if key in required}
    if not required:
        return CriterionScore(
            CRITERION_KEY_MESSAGE_COVERAGE, 1.0,
            justification="未指定必须覆盖的关键信息。",
        )
    ratio = len(covered_set) / len(required)
    missing = [key for key in required if key not in covered_set]
    return CriterionScore(
        CRITERION_KEY_MESSAGE_COVERAGE, float(ratio),
        justification=f"覆盖 {len(covered_set)}/{len(required)} 条关键信息。",
        details={"missing": missing},
    )


def score_readability(
    contrast_ratios: Iterable[float],
    *,
    min_contrast: float = 4.5,
) -> CriterionScore:
    """Average fraction of text elements meeting the contrast threshold."""
    ratios = [r for r in contrast_ratios if r is not None]
    if not ratios:
        return CriterionScore(
            CRITERION_READABILITY, 1.0,
            justification="无可评估的文本对比度。",
        )
    passing = sum(1 for r in ratios if r >= min_contrast)
    ratio = passing / len(ratios)
    return CriterionScore(
        CRITERION_READABILITY, float(ratio),
        justification=f"{passing}/{len(ratios)} 个文本达到对比度 {min_contrast}:1。",
        details={"min_observed": min(ratios), "max_observed": max(ratios)},
    )


def score_visual_overlap(overlap_count: int, total_text_elements: int) -> CriterionScore:
    """Fewer overlaps → higher score."""
    if total_text_elements <= 0:
        return CriterionScore(CRITERION_VISUAL_OVERLAP, 1.0, justification="无文本元素。")
    ratio = max(0.0, 1.0 - overlap_count / total_text_elements)
    return CriterionScore(
        CRITERION_VISUAL_OVERLAP, float(ratio),
        justification=f"{overlap_count} 处文本重叠，共 {total_text_elements} 个文本。",
        details={"overlap_count": overlap_count, "total": total_text_elements},
    )


def score_subtitle_audio_alignment(
    aligned: int,
    total: int,
) -> CriterionScore:
    if total <= 0:
        return CriterionScore(
            CRITERION_SUBTITLE_AUDIO, 1.0,
            justification="无字幕-音频映射。",
        )
    return CriterionScore(
        CRITERION_SUBTITLE_AUDIO, aligned / total,
        justification=f"{aligned}/{total} 条字幕与音频对齐。",
        details={"aligned": aligned, "total": total},
    )


# --------------------------------------------------------------------------- #
# Trace store (lightweight, in-memory; the host persists to .topos)
# --------------------------------------------------------------------------- #


class CreativeEvalTraceStore:
    """Append-only store of evaluation traces for regression detection."""

    def __init__(self) -> None:
        self._traces: list[CreativeEvalTrace] = []

    def add(self, trace: CreativeEvalTrace) -> None:
        self._traces.append(trace)

    def for_category(self, category: str) -> tuple[CreativeEvalTrace, ...]:
        return tuple(t for t in self._traces if t.task_category == category)

    def latest(self, category: str) -> CreativeEvalTrace | None:
        traces = self.for_category(category)
        return traces[-1] if traces else None

    def regression_detected(
        self,
        category: str,
        *,
        threshold: float = 0.05,
    ) -> bool:
        """Return True if the latest trace dropped below the previous by ``threshold``."""
        traces = self.for_category(category)
        if len(traces) < 2:
            return False
        previous = traces[-2].report.weighted_score
        current = traces[-1].report.weighted_score
        return (previous - current) > threshold

    def all_traces(self) -> tuple[CreativeEvalTrace, ...]:
        return tuple(self._traces)


__all__ = [
    "CreativeEvalReport",
    "CreativeEvalTrace",
    "CreativeEvalTraceStore",
    "CriterionScore",
    "ALL_CRITERIA",
    "OBJECTIVE_CRITERIA",
    "SUBJECTIVE_CRITERIA",
    "CRITERION_DURATION_ACCURACY",
    "CRITERION_KEY_MESSAGE_COVERAGE",
    "CRITERION_READABILITY",
    "CRITERION_BRAND_CONSISTENCY",
    "CRITERION_PACING",
    "CRITERION_VISUAL_OVERLAP",
    "CRITERION_SUBTITLE_AUDIO",
    "CRITERION_USER_EDIT_ADHERENCE",
    "SUPPORTED_TASK_CATEGORIES",
    "TASK_AI_AGENT_INTRO",
    "TASK_PRODUCT_AD",
    "TASK_TUTORIAL_EXPLAINER",
    "TASK_VERTICAL_SOCIAL",
    "score_duration_accuracy",
    "score_key_message_coverage",
    "score_readability",
    "score_visual_overlap",
    "score_subtitle_audio_alignment",
]
