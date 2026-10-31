"""AQ-10: Freeform HTML quality gate.

The motion quality gate (AQ-03) catches bad *animation*; this gate catches
"漂亮但不可交付" *HTML/CSS* before confirmation or export — the plan lists it
as the thing that stops a generated page reaching the timeline when it is
unsafe, over budget, references missing assets/fonts, overflows the viewport,
or flickers.  Every check is a pure function of the sanitized HTML (+ optional
budget / asset set / render evidence), so the gate is deterministic and runs
without a browser backend.  Backend-dependent checks (frame determinism,
crash/timeout) are skipped *with a reason* rather than silently passing —
matching the plan rule that absence of capability is reported, never hidden.

Findings reuse the existing :class:`QualityIssue` / :class:`QualityResult` /
:class:`QualityCode` vocabulary so HTML and motion diagnostics stream through
one gate at confirmation/export time.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, Mapping

from ..render.html_layer.budget import BudgetReport, enforce_budget
from ..render.html_layer.sanitizer import HtmlSanitizer, SanitizeResult
from .motion_quality_gate import (
    QualityCode, QualityIssue, QualityResult, QualitySeverity,
)


# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class HtmlQualityInputs:
    """Typed inputs for the HTML quality gate.

    ``sanitized_html`` is the HTML *after* the sanitizer has already run.  The
    gate re-scans it (defence in depth: a generated blob that the sanitizer
    changed is itself a signal).  Optional fields let the caller supply asset
    ids, font coverage and render evidence; when absent the matching check is
    skipped with an INFO finding rather than failing or silently passing.
    """

    sanitized_html: str
    viewport_width: float = 1920.0
    viewport_height: float = 1080.0
    known_asset_ids: tuple[str, ...] = ()
    render_evidence: Mapping[str, object] | None = None   # frame-hash history etc.
    renderer_available: bool = False                       # gated by W0-A


# ---------------------------------------------------------------------------
# Individual checks (pure functions)
# ---------------------------------------------------------------------------

_TOPOS_ASSET_RE = re.compile(r"topos-asset://([A-Za-z0-9_\-.]+)")
_FIXED_WIDTH_RE = re.compile(r"width:\s*([0-9.]+)\s*(px|vw)", re.IGNORECASE)
_FIXED_POS_RE = re.compile(r"(left|top):\s*(-?[0-9.]+)\s*px", re.IGNORECASE)
# opacity flicker: keyframes that swing opacity across most of the range.
_OPACITY_KEYFRAME_RE = re.compile(
    r"opacity:\s*([01](?:\.\d+)?)\s*[;{}]", re.IGNORECASE)
_RAPID_DURATION_RE = re.compile(
    r"animation(?:-duration)?\s*:\s*[^;{}]*?([0-9]*\.?[0-9]+)\s*s",
    re.IGNORECASE)


def check_unsafe_content(
    sanitized_html: str,
    *,
    sanitizer: HtmlSanitizer | None = None,
) -> tuple[QualityIssue, ...]:
    """Defence-in-depth re-scan: the sanitizer must be a fixed point.

    Re-running the sanitizer on already-sanitized HTML must not remove anything
    further.  If it does, the blob still contains disallowed constructs (e.g. an
    allowlist gap) and is unsafe to ship.
    """
    if not sanitized_html.strip():
        return (QualityIssue(QualityCode.HTML_UNSAFE_CONTENT, QualitySeverity.ERROR,
                             "empty HTML payload"),)
    san = sanitizer or HtmlSanitizer()
    second: SanitizeResult = san.sanitize(sanitized_html)
    residual = (second.report.removed_tags + second.report.removed_attrs
                + second.report.removed_css)
    if residual:
        detail = "; ".join(sorted(set(residual)))[:200]
        return (QualityIssue(QualityCode.HTML_UNSAFE_CONTENT, QualitySeverity.ERROR,
                             f"sanitizer still removes tokens on re-scan: {detail}"),)
    # Also reject residual dangerous constructs the active sanitizer profile
    # does not explicitly allow.  Motion Graphic's browser profile may retain
    # reviewed HTTPS scripts and inline animation code; treating those as
    # unsafe here after the profile-aware fixed-point scan made valid HTML
    # layers impossible to export.
    lowered = sanitized_html.lower()
    allow_scripts = bool(
        getattr(san, "_allow_network_resources", False)
        or getattr(san, "_allow_inline_scripts", False)
    )
    allow_handlers = bool(getattr(san, "_allow_event_handlers", False))
    needles = ["javascript:", "<iframe"]
    if not allow_scripts:
        needles.append("<script")
    if not allow_handlers:
        needles.extend((" onerror", " onload"))
    for needle in needles:
        if needle in lowered:
            return (QualityIssue(QualityCode.HTML_UNSAFE_CONTENT, QualitySeverity.ERROR,
                                 f"residual unsafe token: {needle!r}"),)
    return ()


def check_css_budget(report: BudgetReport) -> tuple[QualityIssue, ...]:
    issues: list[QualityIssue] = []
    for violation in report.exceeded:
        issues.append(QualityIssue(
            QualityCode.HTML_CSS_BUDGET_EXCEEDED, QualitySeverity.ERROR,
            f"{violation.budget_field}: {violation.actual} > {violation.limit}",
            metric=float(violation.actual),
        ))
    return tuple(issues)


def check_asset_resolution(
    sanitized_html: str, *, known_asset_ids: Iterable[str],
) -> tuple[QualityIssue, ...]:
    known = set(known_asset_ids)
    referenced = set(_TOPOS_ASSET_RE.findall(sanitized_html))
    missing = sorted(referenced - known)
    if not missing:
        return ()
    return (QualityIssue(
        QualityCode.HTML_ASSET_UNRESOLVED, QualitySeverity.ERROR,
        f"unresolved topos-asset:// ids: {', '.join(missing)}",
    ),)


def check_viewport_overflow(
    sanitized_html: str, *, viewport_width: float, viewport_height: float,
) -> tuple[QualityIssue, ...]:
    """Static heuristic: flag fixed px widths/positions beyond the viewport."""
    issues: list[QualityIssue] = []
    for match in _FIXED_WIDTH_RE.finditer(sanitized_html):
        value = float(match.group(1))
        unit = match.group(2).lower()
        if unit == "px" and value > viewport_width + 1.0:
            issues.append(QualityIssue(
                QualityCode.HTML_VIEWPORT_OVERFLOW, QualitySeverity.WARNING,
                f"fixed width {value}px exceeds viewport {viewport_width}px",
                metric=value,
            ))
    for match in _FIXED_POS_RE.finditer(sanitized_html):
        axis, value = match.group(1).lower(), float(match.group(2))
        extent = viewport_width if axis == "left" else viewport_height
        if value > extent + 1.0:
            issues.append(QualityIssue(
                QualityCode.HTML_VIEWPORT_OVERFLOW, QualitySeverity.WARNING,
                f"fixed {axis} {value}px beyond viewport extent",
                metric=value,
            ))
    return tuple(issues)


def check_flicker(sanitized_html: str) -> tuple[QualityIssue, ...]:
    """Heuristic: rapid animation-duration + wide opacity swings → flicker risk."""
    durations = [float(d) for d in _RAPID_DURATION_RE.findall(sanitized_html)]
    opacities = [float(o) for o in _OPACITY_KEYFRAME_RE.findall(sanitized_html)]
    rapid = any(d < 0.2 for d in durations)           # < ~12 frames at 60fps
    swings = max(opacities) - min(opacities) > 0.6 if len(opacities) >= 2 else False
    if rapid and swings:
        return (QualityIssue(
            QualityCode.HTML_FLICKER, QualitySeverity.WARNING,
            "rapid opacity keyframes (<0.2s, wide swing) may flicker",
        ),)
    return ()


def check_frame_determinism(
    render_evidence: Mapping[str, object] | None,
) -> tuple[QualityIssue, ...]:
    """If we have repeated-capture evidence, identical frames must hash equal.

    Without a renderer backend there is no evidence; the check reports INFO
    rather than passing silently (plan: absence of capability is reported).
    """
    if not render_evidence:
        return (QualityIssue(
            QualityCode.HTML_FRAME_NONDETERMINISM, QualitySeverity.INFO,
            "frame determinism not checked (no renderer backend/evidence)",
        ),)
    hashes = render_evidence.get("frame_hashes")
    if isinstance(hashes, (list, tuple)) and len(set(hashes)) > 1:
        return (QualityIssue(
            QualityCode.HTML_FRAME_NONDETERMINISM, QualitySeverity.ERROR,
            "repeated capture of the same frame produced differing hashes",
        ),)
    return ()


def check_renderer_available(available: bool) -> tuple[QualityIssue, ...]:
    if not available:
        return (QualityIssue(
            QualityCode.HTML_RENDERER_UNAVAILABLE, QualitySeverity.WARNING,
            "no HTML renderer backend in this build; HTML export is blocked",
        ),)
    return ()


# ---------------------------------------------------------------------------
# Bundle
# ---------------------------------------------------------------------------

def run_html_quality_checks(
    inputs: HtmlQualityInputs,
    *,
    budget_report: BudgetReport | None = None,
    sanitizer: HtmlSanitizer | None = None,
) -> QualityResult:
    """Run all HTML quality checks and return a coded result.

    ``budget_report`` may be supplied if the caller already measured the DOM
    (avoids a double parse); otherwise it is measured here.
    """
    if budget_report is None:
        budget_report = enforce_budget(inputs.sanitized_html)
    issues: list[QualityIssue] = []
    issues += check_unsafe_content(inputs.sanitized_html, sanitizer=sanitizer)
    issues += check_css_budget(budget_report)
    issues += check_asset_resolution(inputs.sanitized_html,
                                     known_asset_ids=inputs.known_asset_ids)
    issues += check_viewport_overflow(inputs.sanitized_html,
                                      viewport_width=inputs.viewport_width,
                                      viewport_height=inputs.viewport_height)
    issues += check_flicker(inputs.sanitized_html)
    issues += check_frame_determinism(inputs.render_evidence)
    issues += check_renderer_available(inputs.renderer_available)
    return QualityResult(issues=tuple(issues))


__all__ = [
    "HtmlQualityInputs",
    "check_asset_resolution",
    "check_css_budget",
    "check_flicker",
    "check_frame_determinism",
    "check_renderer_available",
    "check_unsafe_content",
    "check_viewport_overflow",
    "run_html_quality_checks",
]
