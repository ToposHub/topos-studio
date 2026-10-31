"""W0-A spike package: HTML offscreen renderer feasibility gate.

This package implements the W0-A browser feasibility gate from
``docs/motion_graphic_clip_remotion_inspired_optimization_plan_2026-08-12.md``.

It is deliberately self-contained: it does NOT modify ``core/model.py``, the
timeline, the UI or any project file format.  Its only output is evidence
(tests + an ADR) that decides whether the HTML renderer waves (W2+) may
proceed, and which renderer build to pin.

Public surface:

* :class:`HtmlRendererSpike` — drives a backend through the gate experiments.
* :class:`QtWebEngineGrabBackend` — the Qt WebEngine offscreen candidate.
* :class:`FrameCaptureBackend` — the Protocol future backends implement.
* fixtures + security corpus used by the gate tests and the ADR.
"""

from __future__ import annotations

from .capture_backend import (
    CancelToken,
    CaptureEvidence,
    CaptureFailure,
    CaptureFailureCode,
    CaptureSurface,
    CapturedFrame,
    FrameCaptureBackend,
    is_effectively_blank, is_uniform_frame,
    rgba_sha256,
)
from .determinism import (
    DeterminismReport,
    build_cache_key,
    frame_hashes_equal,
    summarize_out_of_order,
    summarize_repetition,
)
from .fixtures import (
    ATTACK_FIXTURES,
    AttackFixture,
    BENIGN_FIXTURES,
    HtmlFixture,
    attack_by_id,
    attack_vectors,
    benign_by_id,
)
from .html_spike import GateReport, HtmlRendererSpike, SecurityVerdict, SpikeConfig
from .qt_capture_backend import QtWebEngineGrabBackend
from .playwright_backend import PlaywrightChromiumBackend
from .security import (
    BudgetViolation,
    CspPolicy,
    DISABLED_FEATURES,
    ResourceBudget,
    ResourceDecision,
    ToposAssetBinding,
    ToposAssetRegistry,
    UrlPolicy,
    UrlPolicyDecision,
    UrlRequest,
    check_stylesheet_budget,
    ensure_font_lock_csp,
)

__all__ = [
    "ATTACK_FIXTURES",
    "AttackFixture",
    "BENIGN_FIXTURES",
    "BudgetViolation",
    "CancelToken",
    "CspPolicy",
    "CaptureEvidence",
    "CaptureFailure",
    "CaptureFailureCode",
    "CaptureSurface",
    "CapturedFrame",
    "DISABLED_FEATURES",
    "DeterminismReport",
    "FrameCaptureBackend",
    "GateReport",
    "HtmlFixture",
    "HtmlRendererSpike",
    "PlaywrightChromiumBackend",
    "QtWebEngineGrabBackend",
    "ResourceBudget",
    "ResourceDecision",
    "SecurityVerdict",
    "SpikeConfig",
    "ToposAssetBinding",
    "ToposAssetRegistry",
    "UrlPolicy",
    "UrlPolicyDecision",
    "UrlRequest",
    "attack_by_id",
    "attack_vectors",
    "benign_by_id",
    "build_cache_key",
    "check_stylesheet_budget",
    "ensure_font_lock_csp",
    "frame_hashes_equal",
    "is_effectively_blank", "is_uniform_frame",
    "rgba_sha256",
    "summarize_out_of_order",
    "summarize_repetition",
]
