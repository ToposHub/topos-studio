"""W0-A gate against the pinned external headless-browser backend.

This is the experiment that **unblocks W2**: the Qt WebEngine offscreen path
returns uniform blank frames (ADR 0001), so the HTML layer could not be
shipped.  The pinned Playwright/Chromium backend is the named fallback, and
this test proves it passes the full W0-A gate — deterministic seek, out-of-
order stability, cancel, and the no-network/no-file/no-script security corpus.

Per the plan (§7.1.1) this runs as a separate ``html_renderer`` CI job and
MUST skip-with-reason (not silently pass) when chromium is not installed in
the environment.
"""

from __future__ import annotations

import pytest

from src.features.teaching_animation.render.html_renderer_spike import (
    BENIGN_FIXTURES,
    CaptureSurface,
    HtmlRendererSpike,
    PlaywrightChromiumBackend,
    SpikeConfig,
)


@pytest.fixture(scope="module")
def backend():
    return PlaywrightChromiumBackend()


@pytest.mark.html_renderer
def test_backend_self_reports_available_with_pinned_build(backend):
    """The backend must honestly report availability and a pinned build string."""
    if not backend.is_available():
        pytest.skip(
            "pinned headless browser unavailable in this environment "
            "(playwright/chromium not installed). W0-A gate cannot run; "
            "see docs/adr/0001-html-renderer-offscreen-readback.md and "
            "docs/adr/0002-html-renderer-pinned-headless-backend.md.")
    # The build string pins the python + browser revision for cache keys.
    assert backend.backend_build.startswith("playwright-chromium/py")
    # is_available captured a real chrome version into the build.
    assert "/chrome-" in backend.backend_build


@pytest.mark.html_renderer
def test_w0a_gate_passes_on_pinned_backend(backend):
    """The full W0-A gate: determinism + out-of-order + cancel + security."""
    if not backend.is_available():
        pytest.skip(
            "pinned headless browser unavailable in this environment "
            "(playwright/chromium not installed).")
    # The animated fixture authors a 320x180 page; the surface must match so
    # the animated card stays on-canvas for every seek frame (an undersized
    # surface would push it off-screen and produce a legitimately-uniform
    # frame that the blank detector would misclassify).
    config = SpikeConfig(
        surface=CaptureSurface(320, 180, 1.0),
        seek_frames=(0, 6, 12, 23),
        repetition_count=5,
    )
    spike = HtmlRendererSpike(backend)
    report = spike.run_gate(config, fixture=BENIGN_FIXTURES[1])

    assert report.backend_available is True
    assert report.determinism.same_frame_repeats_identical is True, \
        "repeated captures of the same frame must be byte-identical"
    assert report.out_of_order.out_of_order_identical is True, \
        "shuffled seek order must produce identical frames to ordered seek"
    assert report.cancel_honoured is True, \
        "a cancelled capture must report CANCELLED, never a stale frame"
    assert all(v.denied for v in report.security), \
        "every attack-corpus request must be denied by the runtime policy"


@pytest.mark.html_renderer
def test_evidence_records_browser_version_and_seek(backend):
    """Each capture records provenance for cache keys and cross-build diffs."""
    if not backend.is_available():
        pytest.skip("pinned headless browser unavailable in this environment.")
    spike = HtmlRendererSpike(backend)
    config = SpikeConfig(surface=CaptureSurface(320, 180, 1.0), seek_frames=(12,))
    result = spike._capture(BENIGN_FIXTURES[1], frame=12, config=config)
    assert hasattr(result, "evidence"), "expected a captured frame, not a failure"
    ev = result.evidence
    assert ev.backend_build == backend.backend_build
    assert "Chrome/" in ev.notes["ua"]            # the real browser version
    assert ev.notes["frame"] == 12
    assert ev.notes["frozen_animations"] == 1     # the seek bridge paused the animation
