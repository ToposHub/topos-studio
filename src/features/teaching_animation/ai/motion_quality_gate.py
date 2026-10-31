"""AQ-03: motion quality gate — checks that catch bad motion a JSON test misses.

Plain schema/round-trip tests cannot see rhythm, overshoot, strobing or a
spring that never settles.  This gate runs pure checks over *evaluated* scene
state (per-frame visibility, spring settle frames, property value series,
resolved effects, font resolution) and emits coded ``QualityIssue`` diagnostics
so "漂亮但不可交付" motion is caught before confirmation/export — never silently.

Each check is a pure function of typed inputs; the ``MotionQualityGate`` bundles
them.  Severity is ``error`` (blocks export) or ``warning`` (worth showing).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Mapping, Sequence


class QualitySeverity(str, Enum):
    ERROR = "error"
    WARNING = "warning"
    INFO = "info"


class QualityCode(str, Enum):
    EMPTY_VISIBLE_FRAME = "empty_visible_frame"
    TRANSITION_OVERLAP_INVALID = "transition_overlap_invalid"
    SPRING_NOT_CONVERGED = "spring_not_converged"
    OVERSHOOT_OUT_OF_BOUNDS = "overshoot_out_of_bounds"
    STROBING = "strobing"
    FONT_UNRESOLVED = "font_unresolved"
    EFFECT_CAPABILITY_MISSING = "effect_capability_missing"
    CACHE_MISMATCH = "cache_mismatch"
    # AQ-10: Freeform HTML quality gate codes (additive — reused by the HTML
    # gate so the whole project speaks one quality vocabulary).
    HTML_UNSAFE_CONTENT = "html_unsafe_content"
    HTML_CSS_BUDGET_EXCEEDED = "html_css_budget_exceeded"
    HTML_ASSET_UNRESOLVED = "html_asset_unresolved"
    HTML_VIEWPORT_OVERFLOW = "html_viewport_overflow"
    HTML_FLICKER = "html_flicker"
    HTML_FRAME_NONDETERMINISM = "html_frame_nondeterminism"
    HTML_RENDERER_UNAVAILABLE = "html_renderer_unavailable"


@dataclass(frozen=True)
class QualityIssue:
    code: QualityCode
    severity: QualitySeverity
    message: str
    path: str = ""
    metric: float = 0.0


@dataclass(frozen=True)
class QualityResult:
    issues: tuple[QualityIssue, ...] = ()

    @property
    def errors(self) -> tuple[QualityIssue, ...]:
        return tuple(i for i in self.issues if i.severity == QualitySeverity.ERROR)

    @property
    def warnings(self) -> tuple[QualityIssue, ...]:
        return tuple(i for i in self.issues if i.severity == QualitySeverity.WARNING)

    @property
    def passed(self) -> bool:
        return not self.errors


# ---------------------------------------------------------------------------
# Individual checks (pure functions of typed inputs)
# ---------------------------------------------------------------------------

def check_empty_frames(
    visible_counts: Mapping[int, int],
    *,
    required_frames: Sequence[int],
) -> tuple[QualityIssue, ...]:
    """Flag required frames (first/mid/marker) that render nothing visible."""
    issues: list[QualityIssue] = []
    for frame in required_frames:
        count = visible_counts.get(frame, 0)
        if count <= 0:
            issues.append(QualityIssue(
                QualityCode.EMPTY_VISIBLE_FRAME, QualitySeverity.ERROR,
                f"frame {frame} has no visible elements (blank)", metric=float(frame)))
    return tuple(issues)


def check_transition_overlap(
    overlaps_frames: Sequence[tuple[str, int, int, int]],
) -> tuple[QualityIssue, ...]:
    """Each item: (transition_id, overlap_frames, in_duration, out_duration).

    An overlap larger than the shorter of the two transition halves is invalid.
    """
    issues: list[QualityIssue] = []
    for tid, overlap, in_dur, out_dur in overlaps_frames:
        limit = min(in_dur, out_dur)
        if limit > 0 and overlap > limit:
            issues.append(QualityIssue(
                QualityCode.TRANSITION_OVERLAP_INVALID, QualitySeverity.ERROR,
                f"transition {tid} overlap {overlap} > limit {limit}", path=tid, metric=float(overlap)))
    return tuple(issues)


def check_spring_convergence(
    springs: Sequence[tuple[str, int, int]],
) -> tuple[QualityIssue, ...]:
    """Each item: (label, settle_frame, scene_duration_frames)."""
    issues: list[QualityIssue] = []
    for label, settle, duration in springs:
        if settle > duration:
            issues.append(QualityIssue(
                QualityCode.SPRING_NOT_CONVERGED, QualitySeverity.WARNING,
                f"{label} settles at frame {settle}, beyond scene duration {duration}",
                path=label, metric=float(settle - duration)))
    return tuple(issues)


def check_overshoot(
    property_maxima: Sequence[tuple[str, str, float]],
    *,
    bounds: Mapping[str, tuple[float, float]] | None = None,
) -> tuple[QualityIssue, ...]:
    """Each item: (element_id, property, observed_max).  Default bounds keep
    opacity/rotation tight; scale/x/y allow overshoot."""
    default_bounds = {"opacity": (0.0, 1.0)}
    bounds = {**default_bounds, **(bounds or {})}
    issues: list[QualityIssue] = []
    for element_id, prop, maximum in property_maxima:
        if prop in bounds:
            low, high = bounds[prop]
            if maximum > high + 1e-6:
                issues.append(QualityIssue(
                    QualityCode.OVERSHOOT_OUT_OF_BOUNDS, QualitySeverity.WARNING,
                    f"{element_id}.{prop} overshoots to {maximum:.3f} > {high}",
                    path=f"{element_id}.{prop}", metric=float(maximum)))
    return tuple(issues)


def check_strobing(
    property_series: Sequence[tuple[str, str, Sequence[float]]],
    *,
    threshold: int = 4,
    toggle_delta: float = 0.5,
) -> tuple[QualityIssue, ...]:
    """Flag properties that flip on/off more than ``threshold`` times.

    Each item: (element_id, property, sampled_values_across_frames).  A "toggle"
    is a change larger than ``toggle_delta``; too many = visible flicker.
    """
    issues: list[QualityIssue] = []
    for element_id, prop, values in property_series:
        values = list(values)
        if len(values) < 2:
            continue
        toggles = 0
        for index in range(1, len(values)):
            if abs(values[index] - values[index - 1]) > toggle_delta:
                toggles += 1
        if toggles > threshold:
            issues.append(QualityIssue(
                QualityCode.STROBING, QualitySeverity.ERROR,
                f"{element_id}.{prop} toggles {toggles} times (> {threshold}); likely flicker",
                path=f"{element_id}.{prop}", metric=float(toggles)))
    return tuple(issues)


def check_font_resolution(
    families: Sequence[tuple[str, tuple[int, ...]]],
) -> tuple[QualityIssue, ...]:
    """Each item: (family, unresolved_codepoints).  Non-empty tuple = warning."""
    issues: list[QualityIssue] = []
    for family, unresolved in families:
        if unresolved:
            sample = ", ".join(f"U+{cp:04X}" for cp in unresolved[:4])
            issues.append(QualityIssue(
                QualityCode.FONT_UNRESOLVED, QualitySeverity.WARNING,
                f"font {family} has {len(unresolved)} unresolved codepoint(s): {sample}",
                path=family, metric=float(len(unresolved))))
    return tuple(issues)


def check_effect_capability(
    dropped_or_blocked: Sequence[tuple[str, str]],
) -> tuple[QualityIssue, ...]:
    """Each item: (effect_type, status) where status in {dropped, blocked, fallback}."""
    issues: list[QualityIssue] = []
    for effect_type, status in dropped_or_blocked:
        severity = QualitySeverity.ERROR if status == "blocked" else QualitySeverity.WARNING
        issues.append(QualityIssue(
            QualityCode.EFFECT_CAPABILITY_MISSING, severity,
            f"effect {effect_type} {status} by the render backend",
            path=effect_type))
    return tuple(issues)


def check_cache_mismatch(
    pairs: Sequence[tuple[str, str]],
) -> tuple[QualityIssue, ...]:
    """Each item: (preview_hash, export_hash) that must match but don't."""
    issues: list[QualityIssue] = []
    for preview_hash, export_hash in pairs:
        if preview_hash != export_hash:
            issues.append(QualityIssue(
                QualityCode.CACHE_MISMATCH, QualitySeverity.ERROR,
                f"preview/export hash mismatch: {preview_hash[:8]} != {export_hash[:8]}"))
    return tuple(issues)


# ---------------------------------------------------------------------------
# Bundle gate
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class MotionQualityInput:
    """Bundle of evaluated state the gate consumes."""

    visible_counts: Mapping[int, int] = field(default_factory=dict)
    required_frames: tuple[int, ...] = ()
    transition_overlaps: tuple[tuple[str, int, int, int], ...] = ()
    springs: tuple[tuple[str, int, int], ...] = ()
    property_maxima: tuple[tuple[str, str, float], ...] = ()
    strobing_series: tuple[tuple[str, str, tuple[float, ...]], ...] = ()
    font_families: tuple[tuple[str, tuple[int, ...]], ...] = ()
    effect_dropped: tuple[tuple[str, str], ...] = ()
    cache_pairs: tuple[tuple[str, str], ...] = ()


def run_motion_quality_gate(state: MotionQualityInput) -> QualityResult:
    """Run every check and return the aggregate result."""
    issues: list[QualityIssue] = []
    issues.extend(check_empty_frames(state.visible_counts, required_frames=state.required_frames))
    issues.extend(check_transition_overlap(state.transition_overlaps))
    issues.extend(check_spring_convergence(state.springs))
    issues.extend(check_overshoot(state.property_maxima))
    issues.extend(check_strobing(state.strobing_series))
    issues.extend(check_font_resolution(state.font_families))
    issues.extend(check_effect_capability(state.effect_dropped))
    issues.extend(check_cache_mismatch(state.cache_pairs))
    return QualityResult(tuple(issues))


__all__ = [
    "MotionQualityInput", "QualityCode", "QualityIssue", "QualityResult", "QualitySeverity",
    "run_motion_quality_gate",
    "check_cache_mismatch", "check_effect_capability", "check_empty_frames",
    "check_font_resolution", "check_overshoot", "check_spring_convergence",
    "check_strobing", "check_transition_overlap",
]
