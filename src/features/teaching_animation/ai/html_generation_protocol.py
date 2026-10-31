"""AQ-09: controlled Freeform HTML generation protocol.

The plan is explicit that Freeform HTML is "自由设计、受控执行": the model may
design a page, but its output must never reach the project, the timeline or a
renderer without passing through a fixed pipeline.  Concretely the acceptance
is: "生成失败返回可修正原因；未经批准的 HTML 不写工程、不插时间线".

This module defines that pipeline as pure data + a pure orchestrator:

    CONSTRAIN  →  SANITIZE  →  STATIC_ANALYSIS  →  BIND_ASSETS
                →  PREVIEW (gated)  →  APPROVE  →  writable?

Each stage produces typed diagnostics.  The orchestrator returns an
:class:`HtmlGenerationOutcome` whose ``writable`` flag is ``True`` **only** when
the payload passed sanitization + static analysis with no errors AND the user
has explicitly approved it.  A failing or unapproved result carries the exact
reasons the model/user must fix, and is never writable — so the host can
hard-gate "write to project / insert into timeline" on ``outcome.writable``.

The PREVIEW stage is gated by the W0-A browser backend: when no renderer is
available the outcome reports ``preview_blocked`` (a reason, not a silent
pass), matching the plan rule that absence of capability is reported.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping

from ..render.html_layer.budget import enforce_budget
from ..render.html_layer.sanitizer import HtmlSanitizer, SanitizeResult
from ..core.html_page_deck import ensure_page_markers
from .html_quality_gate import HtmlQualityInputs, run_html_quality_checks
from .motion_quality_gate import QualityResult


# ---------------------------------------------------------------------------
# Pipeline shape
# ---------------------------------------------------------------------------

class HtmlPipelineStage(str, Enum):
    CONSTRAIN = "constrain"           # schema/constraints the model must satisfy
    SANITIZE = "sanitize"             # allowlist sanitizer runs
    STATIC_ANALYSIS = "static_analysis"  # budget + quality gate (no backend)
    BIND_ASSETS = "bind_assets"       # topos-asset:// resolution
    PREVIEW = "preview"               # low-res render (gated by W0-A)
    APPROVE = "approve"               # explicit user approval gate


@dataclass(frozen=True)
class HtmlGenerationConstraints:
    """The browser/runtime contract for one model-authored HTML document.

    The generic HTML path may enable the post-generation quality gate.  The
    Motion Graphic browser path disables that *design* gate, while retaining
    the sanitizer and renderer sandbox required to load model content safely.
    """

    allowed: tuple[str, ...] = ("html", "css")
    forbidden: tuple[str, ...] = (
        "script", "javascript:", "event handlers (on*)", "iframe",
        "remote urls", "file://", "@import", "external fonts",
    )
    max_duration_seconds: float = 10.0
    viewport_width: float = 1920.0
    viewport_height: float = 1080.0
    # Optional authoring hint.  Page count is not a visual template: the
    # editor owns timing and transitions after import.
    page_count: int | None = None
    # Keep this additive at the end of the dataclass so older positional
    # construction (duration, width, height, page_count) remains compatible.
    fps: float = 25.0
    # Motion Graphic Clip can run in browser/render-only mode.  In that mode
    # the host does not judge the model's design after generation; it only
    # records browser diagnostics and keeps the page available for user/LLM
    # edits.  The sanitizer and capture sandbox remain active because they are
    # renderer/security requirements, not an aesthetic gate.
    post_generation_quality_gate: bool = True
    # Motion Graphic's freeform browser profile may opt into public HTTPS
    # resources (CDN styles, fonts, images and external scripts).  The generic
    # HTML path remains offline by default.
    allow_network_resources: bool = False
    allowed_network_hosts: tuple[str, ...] = ()
    # Motion Graphic can explicitly run as a browser-authored document.  These
    # switches are opt-in so generic HTML imports remain conservative, but the
    # freeform clip path does not silently delete the model's JavaScript,
    # event-driven UI, Canvas/Web Animations or newer CSS properties.
    allow_inline_scripts: bool = False
    allow_event_handlers: bool = False
    allow_freeform_css: bool = False
    # The browser profile may use data/blob-backed images, fonts, audio and
    # video created inside the page.  This is intentionally visual-only; it
    # does not enable executable data URLs, workers or host access.
    allow_inline_visual_resources: bool = False
    # Opt into Chromium GPU compositing/WebGL for the freeform browser profile.
    # Generic HTML stays CPU/deterministic by default.
    allow_gpu_apis: bool = False

    def __post_init__(self) -> None:
        if self.max_duration_seconds <= 0:
            raise ValueError("max_duration_seconds must be positive")
        if self.fps <= 0:
            raise ValueError("fps must be positive")
        if self.viewport_width <= 0 or self.viewport_height <= 0:
            raise ValueError("viewport dimensions must be positive")


# ---------------------------------------------------------------------------
# Stage results + outcome
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class StageResult:
    stage: HtmlPipelineStage
    passed: bool
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"stage": self.stage.value, "passed": self.passed, "detail": self.detail}


@dataclass(frozen=True)
class HtmlGenerationOutcome:
    """The result of running raw model HTML through the controlled pipeline."""

    sanitized_html: str
    stages: tuple[StageResult, ...]
    quality: QualityResult
    approved: bool
    renderer_available: bool
    unresolved_assets: tuple[str, ...] = ()
    #: Summary of the PREVIEW capture when a backend was provided: backend_build,
    #: frame_hashes, deterministic, duration_ms, viewport, failure (if any).
    #: Raw RGBA bytes are deliberately NOT included (too large / not serializable).
    preview: Mapping[str, object] | None = None
    #: The single invariant the host gates on.  True ONLY when every stage that
    #: ran passed with no errors AND the user explicitly approved.  Unapproved
    #: or failing HTML is never writable (AQ-09 acceptance).
    writable: bool = False
    #: The original model output, before sanitization.  Carried on the outcome
    #: so the host can stamp ``authored_source`` onto the FreeformHtmlMotionLayer
    #: for FH-01 provenance (auditing what the sanitizer changed) without having
    #: to keep the raw HTML around separately.  Empty for an LLM-failure outcome.
    authored_source: str = ""

    @property
    def blocking_reasons(self) -> tuple[str, ...]:
        """Human-readable reasons the outcome is not writable."""
        reasons: list[str] = []
        for stage in self.stages:
            if not stage.passed:
                reasons.append(f"{stage.stage.value}: {stage.detail}")
        if not self.approved:
            reasons.append("not yet approved by the user")
        reasons.extend(err.message for err in self.quality.errors)
        return tuple(reasons)

    def to_dict(self) -> dict[str, Any]:
        return {
            "sanitized_html": self.sanitized_html,
            "stages": [s.to_dict() for s in self.stages],
            "quality_errors": [i.message for i in self.quality.errors],
            "quality_warnings": [i.message for i in self.quality.warnings],
            "approved": self.approved,
            "renderer_available": self.renderer_available,
            "unresolved_assets": list(self.unresolved_assets),
            "writable": self.writable,
            "blocking_reasons": list(self.blocking_reasons),
            "preview": (dict(self.preview) if self.preview else None),
            "authored_source": self.authored_source,
        }


# ---------------------------------------------------------------------------
# Orchestrator (pure function)
# ---------------------------------------------------------------------------

def run_html_generation_pipeline(
    raw_html: str,
    *,
    constraints: HtmlGenerationConstraints | None = None,
    known_asset_ids: tuple[str, ...] = (),
    approved: bool = False,
    renderer_available: bool = False,
    sanitizer: HtmlSanitizer | None = None,
    render_evidence: Mapping[str, object] | None = None,
    preview_backend: Any = None,
    preview_frame: int = 0,
    preview_surface: Any = None,
) -> HtmlGenerationOutcome:
    """Run raw model HTML through the controlled pipeline.

    Deterministic and side-effect free.  The host must gate every "write to
    project / insert into timeline" on ``outcome.writable``; nothing in this
    function touches the project.

    ``preview_backend`` (a ``FrameCaptureBackend``) wires the PREVIEW stage to
    the now-available pinned headless browser (ADR 0002): when provided and
    available, the sanitized HTML is captured at ``preview_frame`` (twice, for a
    determinism check) and the evidence is recorded on ``outcome.preview``.  A
    non-deterministic preview surfaces as an ``HTML_FRAME_NONDETERMINISM``
    quality error (which blocks writability); a capture failure is reported as
    a failed PREVIEW stage with the structured reason.
    """
    cons = constraints or HtmlGenerationConstraints()
    san = sanitizer or HtmlSanitizer(
        allow_network_resources=cons.allow_network_resources,
        allow_inline_scripts=cons.allow_inline_scripts,
        allow_event_handlers=cons.allow_event_handlers,
        allow_freeform_css=cons.allow_freeform_css,
        allow_inline_visual_resources=cons.allow_inline_visual_resources,
    )
    stages: list[StageResult] = []

    # 1. CONSTRAIN — record the constraints applied (the model is told these
    #    up-front; here we just stamp them onto the trace for auditability).
    stages.append(StageResult(HtmlPipelineStage.CONSTRAIN, True,
                              detail=f"allowed={list(cons.allowed)}"))

    # 2. SANITIZE — allowlist pass.  Conventional page/slide containers get a
    # harmless data marker before sanitization so the editor can own page
    # timing without asking the model to emit executable navigation code.
    authored_source = raw_html
    raw_html = ensure_page_markers(raw_html)
    sanitized: SanitizeResult = san.sanitize(raw_html)
    sanitize_ok = bool(sanitized.html.strip())
    detail = ""
    if not sanitize_ok:
        detail = "sanitizer produced empty output (parse error or all-removed)"
    elif sanitized.report.removed_tags or sanitized.report.removed_attrs or sanitized.report.removed_css:
        # Removals are expected (the model often emits disallowed tokens); the
        # stage passes because the sanitizer did its job, but we record what was
        # stripped so the user sees what changed.
        removed = sorted(set(sanitized.report.removed_tags + sanitized.report.removed_attrs
                             + sanitized.report.removed_css))
        detail = f"stripped {len(removed)} disallowed token kind(s)"
    stages.append(StageResult(HtmlPipelineStage.SANITIZE, sanitize_ok, detail))
    if not sanitize_ok:
        # Nothing further to analyse; bail with a non-writable outcome.
        return _finalize(sanitized.html, stages, QualityResult(issues=()),
                         approved=approved, renderer_available=renderer_available,
                         unresolved_assets=(), authored_source=authored_source)

    # 3. STATIC_ANALYSIS — optional design gate.  Browser-backed Motion
    # Graphic generation intentionally skips this stage's *judgement*; the
    # model/user own the design conversation.  The sanitizer and capture
    # backend still provide the runtime/security boundary below.
    # Page-boundary structure is a safety/correctness contract even for the
    # freeform browser profile.  It is cheap and deterministic, so run it
    # before spending a browser capture on a deck whose later page may already
    # be nested inside an inactive earlier page.
    page_structure_issues: tuple[str, ...] = ()
    try:
        from .html_design_quality import evaluate_html_design

        page_structure_issues = evaluate_html_design(
            sanitized.html,
            viewport_width=float(cons.viewport_width),
            viewport_height=float(cons.viewport_height),
        ).page_structure_issues
    except Exception:  # noqa: BLE001 - browser/static gates remain authoritative
        page_structure_issues = ()

    if cons.post_generation_quality_gate:
        budget_report = enforce_budget(sanitized.html)
        quality = run_html_quality_checks(
            HtmlQualityInputs(
                sanitized_html=sanitized.html,
                viewport_width=cons.viewport_width,
                viewport_height=cons.viewport_height,
                known_asset_ids=known_asset_ids,
                render_evidence=render_evidence,
                renderer_available=renderer_available,
            ),
            budget_report=budget_report,
            sanitizer=san,
        )
        # The quality gate's non-renderer errors (unsafe/budget/asset/overflow)
        # are hard static-analysis failures in the generic HTML path.
        from .motion_quality_gate import QualityCode, QualitySeverity
        hard_codes = {
            QualityCode.HTML_UNSAFE_CONTENT, QualityCode.HTML_CSS_BUDGET_EXCEEDED,
            QualityCode.HTML_ASSET_UNRESOLVED,
        }
        static_errors = [i for i in quality.issues
                         if i.severity == QualitySeverity.ERROR and i.code in hard_codes]
        static_detail: list[str] = []
        if static_errors:
            static_detail.append(f"{len(static_errors)} blocking static issue(s)")
        if page_structure_issues:
            static_detail.append("；".join(page_structure_issues[:3]))
        stages.append(StageResult(
            HtmlPipelineStage.STATIC_ANALYSIS,
            not static_errors and not page_structure_issues,
            detail="；".join(static_detail) if static_detail else "passed"))
    else:
        quality = QualityResult()
        stages.append(StageResult(
            HtmlPipelineStage.STATIC_ANALYSIS,
            not page_structure_issues,
            detail=("；".join(page_structure_issues[:3]) if page_structure_issues else
                    "skipped: browser/render-only mode leaves design decisions to the model and user")))

    # 4. BIND_ASSETS — resolve topos-asset:// ids against the known set.
    import re
    referenced = set(re.findall(r"topos-asset://([A-Za-z0-9_\-.]+)", sanitized.html))
    unresolved = tuple(sorted(referenced - set(known_asset_ids)))
    stages.append(StageResult(
        HtmlPipelineStage.BIND_ASSETS,
        True if not cons.post_generation_quality_gate else not unresolved,
        detail=(
            (f"render-only: {len(unresolved)} asset reference(s) reported, not a design gate"
             if unresolved else "render-only: browser asset binding deferred")
            if not cons.post_generation_quality_gate else
            (f"{len(unresolved)} unresolved asset id(s)" if unresolved else "passed")
        )))

    # 5. PREVIEW — when a pinned backend is provided (ADR 0002), capture the
    #    sanitized HTML at the preview frame TWICE so the same frame must render
    #    byte-identically (the determinism the W0-A gate asserts).  Capture
    #    failures and non-determinism are reported, never silently passed.  When
    #    no backend is supplied the stage reports blocked — not a writability
    #    blocker itself, but export cannot proceed downstream.
    preview_summary, determinism_issues, preview_passed = _run_preview(
        sanitized.html, cons=cons, known_asset_ids=known_asset_ids,
        preview_backend=preview_backend, preview_frame=preview_frame,
        preview_surface=preview_surface,
    )
    renderer_available = renderer_available or (
        preview_summary is not None and preview_summary.get("backend_available", False)
    )
    if preview_summary is None:
        stages.append(StageResult(
            HtmlPipelineStage.PREVIEW, False,
            detail="blocked: no HTML renderer backend supplied"))
    else:
        stages.append(StageResult(HtmlPipelineStage.PREVIEW, preview_passed,
                                  detail=preview_summary.get("detail", "")))
    # Fold any preview-driven determinism findings into the quality result so a
    # non-deterministic preview blocks writability via the quality gate.
    if determinism_issues:
        from .motion_quality_gate import QualityResult as _QR
        quality = _QR(issues=quality.issues + tuple(determinism_issues))

    # 6. APPROVE — explicit user gate.
    stages.append(StageResult(
        HtmlPipelineStage.APPROVE, approved,
        detail=("approved" if approved else "awaiting explicit user approval")))

    return _finalize(sanitized.html, stages, quality, approved=approved,
                     renderer_available=renderer_available,
                     unresolved_assets=unresolved, preview=preview_summary,
                     authored_source=authored_source)


def _run_preview(
    sanitized_html: str,
    *,
    cons: HtmlGenerationConstraints,
    known_asset_ids: tuple[str, ...],
    preview_backend: Any,
    preview_frame: int,
    preview_surface: Any,
) -> tuple[Mapping[str, object] | None, list, bool]:
    """Capture the sanitized HTML on the pinned backend (ADR 0002).

    Returns ``(summary, determinism_issues, passed)``.  ``summary`` is ``None``
    when no usable backend is available (caller reports the stage blocked).
    Otherwise the sanitized page is captured twice at ``preview_frame``; the two
    hashes must match (determinism).  A mismatch produces an
    ``HTML_FRAME_NONDETERMINISM`` error issue; a capture failure is recorded in
    the summary and reported as a failed stage.
    """
    if preview_backend is None:
        return None, [], False
    # Local import keeps the ai package decoupled from the render backend at
    # module import time; the backend is only needed when preview is requested.
    from ..render.html_renderer_spike import CaptureSurface, UrlPolicy
    from ..render.html_renderer_spike.security import CspPolicy
    from ..render.html_renderer_spike.capture_backend import (
        CapturedFrame, CaptureFailure, is_effectively_blank,
    )
    from .html_quality_gate import check_frame_determinism
    from ..core.html_page_deck import (
        PageDeckTiming, detect_page_count, page_deck_capture_html,
        page_start_frame,
    )

    try:
        backend_available = preview_backend.is_available()
    except Exception:
        backend_available = False
    if not backend_available:
        return {"backend_available": False,
                "detail": "blocked: backend self-test unavailable"}, [], False

    surface = preview_surface or CaptureSurface(
        max(8, int(cons.viewport_width)), max(8, int(cons.viewport_height)), 1.0)
    allowed_hosts = frozenset(cons.allowed_network_hosts)
    csp = CspPolicy(
        allowed_asset_ids=frozenset(known_asset_ids),
        allow_network_resources=cons.allow_network_resources,
        allowed_network_hosts=allowed_hosts,
        allow_inline_scripts=cons.allow_inline_scripts,
        allow_inline_visual_resources=cons.allow_inline_visual_resources,
    ).to_header()
    policy = UrlPolicy(
        frozenset(known_asset_ids),
        allow_network_resources=cons.allow_network_resources,
        allowed_network_hosts=allowed_hosts,
        allow_inline_visual_resources=cons.allow_inline_visual_resources,
        allow_gpu_apis=cons.allow_gpu_apis,
    )

    hashes: list[str] = []
    page_frames: list[dict[str, object]] = []
    page_failures: list[dict[str, object]] = []
    duration_ms = 0.0
    failure: str = ""
    page_count = detect_page_count(sanitized_html)
    preview_timing = PageDeckTiming(
        page_count=page_count,
        mode="default",
        default_seconds=max(0.001, cons.max_duration_seconds / page_count),
    )
    capture_html = page_deck_capture_html(sanitized_html, preview_timing)
    fps_num = max(1, round(float(cons.fps)))

    def _capture(frame: int) -> CapturedFrame | CaptureFailure:
        nonlocal duration_ms
        result = preview_backend.capture(
            capture_html, surface,
            frame=int(frame), fps_num=fps_num, fps_den=1,
            csp=csp, url_policy=policy, timeout_ms=20000,
        )
        evidence = getattr(result, "evidence", None)
        duration_ms += float(getattr(evidence, "duration_ms", 0.0) or 0.0)
        return result

    for _ in range(2):
        result = _capture(int(preview_frame))
        if isinstance(result, CaptureFailure):
            failure = f"{result.code.value}: {result.message}"
            break
        hashes.append(result.content_hash)

    if failure:
        summary = {
            "backend_available": True,
            "backend_build": preview_backend.backend_build,
            "viewport": [surface.width, surface.height],
            "frame": int(preview_frame),
            "failure": failure,
            "page_count": page_count,
            "detail": f"capture failed: {failure}",
        }
        return summary, [], False

    evidence = {"frame_hashes": hashes, "backend_build": preview_backend.backend_build}
    determinism_issues = list(check_frame_determinism(evidence))
    deterministic = len(set(hashes)) == 1

    # The first-frame determinism check is necessary but not sufficient for a
    # page deck.  Probe the first visible moment of every page with the same
    # timing and FPS that the editor uses.  A later blank page used to survive
    # this pipeline and only fail after the user had already confirmed it.
    for index, seconds in enumerate(preview_timing.durations):
        start = page_start_frame(index, float(cons.fps), preview_timing)
        page_frames_count = max(1, round(float(seconds) * float(cons.fps)))
        offset = 0 if index == 0 else min(
            max(1, round(float(cons.fps) * 0.1)), page_frames_count - 1,
        )
        frame = max(0, start + offset)
        page_result = _capture(frame)
        if isinstance(page_result, CaptureFailure):
            page_failures.append({
                "page": index + 1, "frame": frame,
                "failure": f"{page_result.code.value}: {page_result.message}",
            })
            continue
        near_blank, stats = is_effectively_blank(
            page_result.data, page_result.surface.width, page_result.surface.height,
        )
        if page_count > 1 and near_blank:
            page_failures.append({
                "page": index + 1, "frame": frame,
                "failure": f"near-uniform frame: {stats}",
            })
        else:
            page_frames.append({"page": index + 1, "frame": frame,
                                "hash": page_result.content_hash})

    preview_passed = deterministic and not page_failures
    summary = {
        "backend_available": True,
        "backend_build": preview_backend.backend_build,
        "viewport": [surface.width, surface.height],
        "frame": int(preview_frame),
        "frame_hashes": hashes,
        "deterministic": deterministic,
        "fps": fps_num,
        "page_count": page_count,
        "page_frames": page_frames,
        "page_failures": page_failures,
        "duration_ms": round(duration_ms, 2),
        "detail": (
            "passed (deterministic; every page visible)"
            if preview_passed else (
                "page renderability failed: "
                + "; ".join(
                    f"page {item['page']} @ frame {item['frame']}: {item['failure']}"
                    for item in page_failures
                )
                if page_failures else
                "non-deterministic: same frame produced differing hashes"
            )
        ),
    }
    # If determinism check produced an INFO finding (no evidence case), replace
    # it with the real verdict now that we have hashes.
    if deterministic:
        determinism_issues = []
    return summary, determinism_issues, preview_passed


def _finalize(
    sanitized_html: str,
    stages: list[StageResult],
    quality: QualityResult,
    *,
    approved: bool,
    renderer_available: bool,
    unresolved_assets: tuple[str, ...],
    preview: Mapping[str, object] | None = None,
    authored_source: str = "",
) -> HtmlGenerationOutcome:
    """Compute the final outcome and its writability invariant."""
    # Writable requires the SAFETY + APPROVAL stages to pass (sanitize, static
    # analysis, asset bind, approve) with no ERROR quality findings.  A supplied
    # browser backend is also a hard renderability gate: a backend that is
    # unavailable is a capability warning, but a backend that captured a blank
    # or incomplete page deck must block the write.  INFO/WARNING quality
    # findings surface to the host without blocking the write.
    safety_stages = {
        HtmlPipelineStage.CONSTRAIN, HtmlPipelineStage.SANITIZE,
        HtmlPipelineStage.STATIC_ANALYSIS, HtmlPipelineStage.BIND_ASSETS,
        HtmlPipelineStage.APPROVE,
    }
    stages_passed = all(s.passed for s in stages if s.stage in safety_stages)
    preview_stage = next((s for s in stages if s.stage == HtmlPipelineStage.PREVIEW), None)
    preview_backend_failed = bool(
        preview is not None
        and preview.get("backend_available", False)
        and preview_stage is not None
        and not preview_stage.passed
    )
    if preview_backend_failed:
        stages_passed = False
    no_hard_errors = not quality.errors
    writable = stages_passed and no_hard_errors and approved
    return HtmlGenerationOutcome(
        sanitized_html=sanitized_html,
        stages=tuple(stages),
        quality=quality,
        approved=approved,
        renderer_available=renderer_available,
        unresolved_assets=unresolved_assets,
        preview=preview,
        writable=writable,
        authored_source=authored_source,
    )


__all__ = [
    "HtmlGenerationConstraints",
    "HtmlGenerationOutcome",
    "HtmlPipelineStage",
    "StageResult",
    "run_html_generation_pipeline",
]
