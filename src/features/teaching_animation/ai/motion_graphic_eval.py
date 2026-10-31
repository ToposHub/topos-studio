"""Offline, versioned evaluation corpus for Motion Graphic HTML creation.

The regular unit tests protect individual functions.  This module adds a
small, stable *conversation-to-artifact* corpus that can be run against a
saved model response, a candidate provider, or deterministic HTML fixtures.
It intentionally never calls a model or browser: provider cost and network
availability must not make regression evidence non-reproducible.

Each case keeps the user turns and machine-checkable expectations together.
The evaluator combines the same acceptance gates used by production -- intent
requirements, per-page richness and responsive-layout diagnostics -- so a
passing score means the output cleared the actual staging contract.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Mapping

from .html_design_quality import evaluate_html_design
from .html_edit_autonomy import generation_intent_report


MOTION_GRAPHIC_EVAL_CORPUS_VERSION = "motion-graphic-html-2026.08-v1"


@dataclass(frozen=True)
class MotionGraphicEvalCase:
    """One privacy-safe multi-turn request and its acceptance expectations."""

    case_id: str
    user_turns: tuple[str, ...]
    expected_requirements: tuple[str, ...] = ()
    description: str = ""

    def __post_init__(self) -> None:
        if not self.case_id:
            raise ValueError("MotionGraphicEvalCase.case_id is required")
        if not self.user_turns:
            raise ValueError("MotionGraphicEvalCase.user_turns is required")


@dataclass(frozen=True)
class MotionGraphicEvalResult:
    """One case result suitable for provider comparison and CI regression."""

    case_id: str
    passed: bool
    issues: tuple[str, ...] = ()
    requirements: tuple[str, ...] = ()
    page_count: int = 0
    overall_score: float = 0.0
    responsive_layout_score: float = 0.0

    def to_dict(self) -> dict[str, object]:
        return {
            "case_id": self.case_id,
            "passed": self.passed,
            "issues": list(self.issues),
            "requirements": list(self.requirements),
            "page_count": self.page_count,
            "overall_score": round(self.overall_score, 3),
            "responsive_layout_score": round(self.responsive_layout_score, 3),
        }


@dataclass(frozen=True)
class MotionGraphicEvalReport:
    """Aggregate result for one corpus/model/version run."""

    corpus_version: str
    results: tuple[MotionGraphicEvalResult, ...]

    @property
    def passed_count(self) -> int:
        return sum(result.passed for result in self.results)

    @property
    def pass_rate(self) -> float:
        return self.passed_count / len(self.results) if self.results else 0.0

    @property
    def passed(self) -> bool:
        return bool(self.results) and self.passed_count == len(self.results)

    def to_dict(self) -> dict[str, object]:
        return {
            "corpus_version": self.corpus_version,
            "passed": self.passed,
            "passed_count": self.passed_count,
            "case_count": len(self.results),
            "pass_rate": round(self.pass_rate, 3),
            "results": [result.to_dict() for result in self.results],
        }


# These are deliberately representative conversations, not private user data.
# New cases should be additive so a historical provider score remains comparable.
DEFAULT_MOTION_GRAPHIC_EVAL_CASES: tuple[MotionGraphicEvalCase, ...] = (
    MotionGraphicEvalCase(
        case_id="agent_intro_1080p_responsive",
        user_turns=(
            "创建一个 15 秒 1080p Motion Graphic Clip，主题是「什么是 AI Agent？」。",
            "页面要自适应，不要把 1920×1080 写成固定网页画布。",
        ),
        expected_requirements=("包含文案「什么是 AI Agent？」",),
        description="基础生成：全高清交付、响应式网页和多页解释叙事。",
    ),
    MotionGraphicEvalCase(
        case_id="orange_revision_with_fourth_page",
        user_turns=(
            "把当前蓝色色系改成橙色色系，每一页内容再丰富一些。",
            "增加第 4 页总结，并让标题包含「下一步行动」。",
        ),
        expected_requirements=("使用橙色色系", "每页信息丰富", "至少 4 页", "包含文案「下一步行动」"),
        description="多轮修改：颜色语义、逐页丰富度、结构扩展和精确文案同时成立。",
    ),
    MotionGraphicEvalCase(
        case_id="explicit_brand_copy_and_color",
        user_turns=(
            "做一支新品发布网页动画，主标题是「新品发布」，品牌色必须使用 #ff8800。",
            "第 2 页加入「立即体验」。",
        ),
        expected_requirements=("包含文案「新品发布」", "使用颜色 #ff8800", "至少 2 页", "包含文案「立即体验」"),
        description="精确文字、色值与显式页码的确定性验收。",
    ),
)


def evaluate_motion_graphic_html_case(
    case: MotionGraphicEvalCase, generated_html: str,
    *, viewport_width: float = 1920.0, viewport_height: float = 1080.0,
) -> MotionGraphicEvalResult:
    """Evaluate one model artifact with the production acceptance functions."""
    intent = generation_intent_report(case.user_turns, generated_html)
    quality = evaluate_html_design(
        generated_html,
        viewport_width=viewport_width,
        viewport_height=viewport_height,
    )
    requirements = intent.requirements
    missing_expected = [
        value for value in case.expected_requirements if value not in requirements
    ]
    issues = list(intent.issues)
    issues.extend(f"评测用例未识别预期要求：{value}" for value in missing_expected)
    if quality.needs_repair:
        issues.append("设计完整度或响应式布局未通过生产门槛")
        issues.extend(quality.page_structure_issues)
    return MotionGraphicEvalResult(
        case_id=case.case_id,
        passed=not issues,
        issues=tuple(dict.fromkeys(issues)),
        requirements=requirements,
        page_count=len(quality.pages),
        overall_score=quality.overall_score,
        responsive_layout_score=quality.responsive_layout_score,
    )


def run_motion_graphic_eval(
    artifacts: Mapping[str, str] | Iterable[tuple[MotionGraphicEvalCase, str]],
    *, cases: Iterable[MotionGraphicEvalCase] = DEFAULT_MOTION_GRAPHIC_EVAL_CASES,
    viewport_width: float = 1920.0,
    viewport_height: float = 1080.0,
) -> MotionGraphicEvalReport:
    """Run the corpus against a mapping of ``case_id -> model HTML``.

    The iterable form is useful for one-off experimental cases; the mapping
    form makes provider/model CI runs compact and deterministic.
    """
    if isinstance(artifacts, Mapping):
        pairs = (
            (case, str(artifacts.get(case.case_id, "") or ""))
            for case in cases
        )
    else:
        pairs = iter(artifacts)
    results = tuple(
        evaluate_motion_graphic_html_case(
            case, html,
            viewport_width=viewport_width,
            viewport_height=viewport_height,
        )
        for case, html in pairs
    )
    return MotionGraphicEvalReport(
        corpus_version=MOTION_GRAPHIC_EVAL_CORPUS_VERSION,
        results=results,
    )


__all__ = [
    "DEFAULT_MOTION_GRAPHIC_EVAL_CASES",
    "MOTION_GRAPHIC_EVAL_CORPUS_VERSION",
    "MotionGraphicEvalCase",
    "MotionGraphicEvalReport",
    "MotionGraphicEvalResult",
    "evaluate_motion_graphic_html_case",
    "run_motion_graphic_eval",
]
