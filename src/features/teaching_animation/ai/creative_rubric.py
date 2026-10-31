# DORMANT(2026-08-19 审计): 已实现且有测试，但暂无生产入口（有意休眠）。
# 接线前请勿在生产依赖；若半年内无接线计划建议删除。
"""AQ-08: creative quality rubric for generated Motion Graphic scenes.

The release gate already has a *technical* quality gate (``motion_quality_gate``)
that catches overshoot, strobing, font fallback and cache mismatch.  What it
cannot answer is "is this *creatively* good for its purpose?" — i.e. whether a
teaching clip is legible and well-paced, whether a data chart has clear
hierarchy, whether a brand title is on-brand, etc.

This rubric scores a finished :class:`TeachingSceneDocument` against six
dimensions, weighted per **content category**, producing a 0–1 weighted total
plus per-dimension diagnostics.  It is a pure function of the scene (no
rendering, no wall clock), so it is deterministic, cacheable and testable —
and usable as the "stable under a quality threshold" check the plan asks for
("同一 prompt 多次生成至少可以在结构与质量阈值下稳定交付").

The six dimensions:

* **readability**   — text size/emptiness; is the copy legible?
* **rhythm**        — animation + marker density in a healthy band?
* **hierarchy**     — distinct font sizes / z-order; a clear visual structure?
* **brand**         — restrained accent palette + token usage?
* **subtitle_audio**— narration/audio cues or subtitle markers present?
* **repetition**    — excessive duplicate content (clutter) penalised.

Weights are per category because a social card does not need the same rhythm
as a teaching explanation.  Categories: ``teaching_explanation``,
``data_chart``, ``brand_title``, ``social_card``.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping

from ..core.model import TeachingSceneDocument


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class RubricCategory(str, Enum):
    TEACHING_EXPLANATION = "teaching_explanation"
    DATA_CHART = "data_chart"
    BRAND_TITLE = "brand_title"
    SOCIAL_CARD = "social_card"


class RubricDimension(str, Enum):
    READABILITY = "readability"
    RHYTHM = "rhythm"
    HIERARCHY = "hierarchy"
    BRAND = "brand"
    SUBTITLE_AUDIO = "subtitle_audio"
    REPETITION = "repetition"


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class DimensionScore:
    dimension: RubricDimension
    score: float                       # 0.0 .. 1.0
    notes: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {"dimension": self.dimension.value, "score": round(self.score, 4),
                "notes": list(self.notes)}


@dataclass(frozen=True)
class RubricScore:
    category: RubricCategory
    dimensions: tuple[DimensionScore, ...]
    weighted_total: float              # 0.0 .. 1.0
    threshold: float
    passed: bool

    def score_for(self, dimension: RubricDimension) -> DimensionScore:
        for d in self.dimensions:
            if d.dimension == dimension:
                return d
        raise KeyError(dimension)

    def to_dict(self) -> dict[str, Any]:
        return {
            "category": self.category.value,
            "weighted_total": round(self.weighted_total, 4),
            "threshold": self.threshold,
            "passed": self.passed,
            "dimensions": [d.to_dict() for d in self.dimensions],
        }


# ---------------------------------------------------------------------------
# Weights + thresholds
# ---------------------------------------------------------------------------

#: Per-category weights.  They sum to 1.0 for each category.
CATEGORY_WEIGHTS: dict[RubricCategory, dict[RubricDimension, float]] = {
    RubricCategory.TEACHING_EXPLANATION: {
        RubricDimension.RHYTHM: 0.25,
        RubricDimension.READABILITY: 0.20,
        RubricDimension.HIERARCHY: 0.15,
        RubricDimension.SUBTITLE_AUDIO: 0.20,
        RubricDimension.BRAND: 0.10,
        RubricDimension.REPETITION: 0.10,
    },
    RubricCategory.DATA_CHART: {
        RubricDimension.HIERARCHY: 0.30,
        RubricDimension.READABILITY: 0.25,
        RubricDimension.RHYTHM: 0.15,
        RubricDimension.BRAND: 0.15,
        RubricDimension.REPETITION: 0.10,
        RubricDimension.SUBTITLE_AUDIO: 0.05,
    },
    RubricCategory.BRAND_TITLE: {
        RubricDimension.BRAND: 0.30,
        RubricDimension.HIERARCHY: 0.25,
        RubricDimension.READABILITY: 0.20,
        RubricDimension.RHYTHM: 0.10,
        RubricDimension.REPETITION: 0.10,
        RubricDimension.SUBTITLE_AUDIO: 0.05,
    },
    RubricCategory.SOCIAL_CARD: {
        RubricDimension.BRAND: 0.25,
        RubricDimension.HIERARCHY: 0.25,
        RubricDimension.READABILITY: 0.25,
        RubricDimension.REPETITION: 0.15,
        RubricDimension.RHYTHM: 0.05,
        RubricDimension.SUBTITLE_AUDIO: 0.05,
    },
}

DEFAULT_THRESHOLD = 0.60


# ---------------------------------------------------------------------------
# Per-dimension scorers (pure functions of the scene)
# ---------------------------------------------------------------------------

def _clamp01(value: float) -> float:
    return max(0.0, min(1.0, value))


def _text_elements(scene: TeachingSceneDocument) -> tuple[Any, ...]:
    return tuple(e for e in scene.elements if e.type == "text")


def _color_values(style: Mapping[str, Any]) -> tuple[str, ...]:
    """Collect colour-like values from a style dict (color/fill/stroke)."""
    out: list[str] = []
    for key in ("color", "fill", "stroke"):
        value = style.get(key)
        if isinstance(value, str) and value:
            out.append(value)
    return tuple(out)


def _score_readability(scene: TeachingSceneDocument) -> DimensionScore:
    texts = _text_elements(scene)
    notes: list[str] = []
    if not texts:
        return DimensionScore(RubricDimension.READABILITY, 0.35, ("no text elements",))
    sizes: list[float] = []
    empty = 0
    for el in texts:
        raw = el.style.get("font_size")
        try:
            sizes.append(float(raw))
        except (TypeError, ValueError):
            sizes.append(16.0)
        if not str(el.content).strip():
            empty += 1
    readable = sum(1.0 for s in sizes if s >= 18.0) / len(sizes)
    completeness = 1.0 - (empty / len(texts))
    score = _clamp01(0.5 * readable + 0.5 * completeness)
    if empty:
        notes.append(f"{empty} empty text element(s)")
    if readable < 0.5:
        notes.append("several text blocks below readable size (<18px)")
    return DimensionScore(RubricDimension.READABILITY, score, tuple(notes))


def _score_rhythm(scene: TeachingSceneDocument) -> DimensionScore:
    duration = max(1, scene.duration_frames)
    n_animations = len(scene.animations)
    n_markers = len(scene.markers)
    # Events per second at 24fps; healthy explanatory clips sit ~0.5–3/s.
    per_second = (n_animations + n_markers) / duration * 24.0
    notes: list[str] = []
    if per_second <= 0:
        notes.append("no animations or markers; pacing is static")
        score = 0.25
    elif per_second < 0.5:
        notes.append("sparse timing; consider more beats/stagger")
        score = 0.5 + per_second            # ramps up toward the healthy band
    elif per_second <= 3.0:
        score = 1.0                          # healthy band
    else:
        # Over-dense — risk of strobing; taper back down.
        notes.append("very dense timing; risk of clutter/strobing")
        score = _clamp01(1.0 - (per_second - 3.0) * 0.15)
    if n_markers:
        notes.append(f"{n_markers} marker(s) anchor narration beats")
    return DimensionScore(RubricDimension.RHYTHM, _clamp01(score), tuple(notes))


def _score_hierarchy(scene: TeachingSceneDocument) -> DimensionScore:
    texts = _text_elements(scene)
    notes: list[str] = []
    sizes: set[float] = set()
    for el in texts:
        try:
            sizes.add(round(float(el.style.get("font_size", 16.0)), 1))
        except (TypeError, ValueError):
            sizes.add(16.0)
    z_values = {float(e.z_index) for e in scene.elements}
    size_score = 1.0 if len(sizes) >= 3 else (0.6 if len(sizes) == 2 else 0.3)
    z_score = 1.0 if len(z_values) >= 2 else 0.5
    has_title_like = any(s >= 40.0 for s in sizes)
    if not has_title_like and texts:
        notes.append("no large headline-sized text; hierarchy may be flat")
    score = 0.6 * size_score + 0.4 * z_score
    return DimensionScore(RubricDimension.HIERARCHY, _clamp01(score), tuple(notes))


def _score_brand(scene: TeachingSceneDocument) -> DimensionScore:
    """Restrained palette + symbolic token usage earns the brand score."""
    palette: set[str] = set()
    token_refs = 0
    for el in scene.elements:
        style = el.style or {}
        palette.update(_color_values(style))
        for value in style.values():
            if isinstance(value, str) and value.startswith("$"):
                token_refs += 1
    bg = scene.background or {}
    bg_colors = _color_values(bg)
    palette -= set(bg_colors)               # ignore background in accent count
    notes: list[str] = []
    if palette:
        if len(palette) <= 3:
            palette_score = 1.0
        elif len(palette) <= 6:
            palette_score = 0.7
            notes.append(f"{len(palette)} distinct accent colours; consider consolidating")
        else:
            palette_score = 0.4
            notes.append(f"{len(palette)} distinct accent colours; palette is noisy")
    else:
        palette_score = 0.5                 # no explicit accents; neutral
    token_score = _clamp01(0.5 + 0.1 * token_refs) if token_refs else 0.4
    if token_refs:
        notes.append(f"{token_refs} token reference(s) used")
    score = 0.6 * palette_score + 0.4 * token_score
    return DimensionScore(RubricDimension.BRAND, _clamp01(score), tuple(notes))


def _score_subtitle_audio(scene: TeachingSceneDocument) -> DimensionScore:
    notes: list[str] = []
    has_audio_asset = any(getattr(ref, "kind", "") == "audio" for ref in scene.asset_refs)
    has_audio_plan = "audio_plan" in (scene.metadata or {})
    has_markers = bool(scene.markers)
    if has_audio_asset or has_audio_plan:
        notes.append("scene-owned audio present")
        score = 1.0
    elif has_markers:
        notes.append("markers can drive narration/subtitle alignment")
        score = 0.7
    else:
        notes.append("no audio or narration cues")
        score = 0.4
    return DimensionScore(RubricDimension.SUBTITLE_AUDIO, _clamp01(score), tuple(notes))


def _score_repetition(scene: TeachingSceneDocument) -> DimensionScore:
    texts = _text_elements(scene)
    notes: list[str] = []
    if not texts:
        return DimensionScore(RubricDimension.REPETITION, 0.8, ("no text to dedupe",))
    contents = [str(e.content).strip() for e in texts if str(e.content).strip()]
    if not contents:
        return DimensionScore(RubricDimension.REPETITION, 0.7, ("all text empty",))
    duplicates = len(contents) - len(set(contents))
    dup_ratio = duplicates / len(contents)
    if duplicates:
        notes.append(f"{duplicates} duplicated text value(s)")
    # A little templated repetition (e.g. numbered cards) is fine; heavy exact
    # duplication reads as clutter.
    score = _clamp01(1.0 - dup_ratio * 1.5)
    return DimensionScore(RubricDimension.REPETITION, score, tuple(notes))


_DIMENSION_SCORERS = {
    RubricDimension.READABILITY: _score_readability,
    RubricDimension.RHYTHM: _score_rhythm,
    RubricDimension.HIERARCHY: _score_hierarchy,
    RubricDimension.BRAND: _score_brand,
    RubricDimension.SUBTITLE_AUDIO: _score_subtitle_audio,
    RubricDimension.REPETITION: _score_repetition,
}


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def score_scene(
    scene: TeachingSceneDocument,
    category: RubricCategory,
    *,
    threshold: float = DEFAULT_THRESHOLD,
) -> RubricScore:
    """Score a finished scene against the rubric for ``category``.

    Deterministic and side-effect free; safe to call on every generation to
    enforce a "stable under quality threshold" gate (AQ-08).
    """
    weights = CATEGORY_WEIGHTS[category]
    dimensions: list[DimensionScore] = []
    weighted = 0.0
    weight_sum = 0.0
    for dimension, weight in weights.items():
        scorer = _DIMENSION_SCORERS[dimension]
        result = scorer(scene)
        dimensions.append(result)
        weighted += weight * result.score
        weight_sum += weight
    # Normalise against the category's weight sum (defensive against rounding).
    total = weighted / weight_sum if weight_sum else 0.0
    total = _clamp01(total)
    return RubricScore(
        category=category,
        dimensions=tuple(dimensions),
        weighted_total=total,
        threshold=threshold,
        passed=total >= threshold,
    )


__all__ = [
    "CATEGORY_WEIGHTS",
    "DEFAULT_THRESHOLD",
    "DimensionScore",
    "RubricCategory",
    "RubricDimension",
    "RubricScore",
    "score_scene",
]
