# ADR 0002 — Pinned headless-browser backend passes the W0-A gate (W2 unblocked)

- **Status:** Accepted (W0-A gate passed on the pinned Playwright/Chromium backend)
- **Date:** 2026-08-12
- **Plan ref:** `docs/motion_graphic_clip_remotion_inspired_optimization_plan_2026-08-12.md` §W0-A, §3.3, S-00, §5.2
- **Supersedes:** the "to validate in W2" row of ADR 0001's support matrix
- **Implementation:** `src/features/teaching_animation/render/html_renderer_spike/playwright_backend.py`
- **CI marker:** `html_renderer` (separate job; skip-with-reason when chromium is absent)

## Context

ADR 0001 recorded that the Qt WebEngine offscreen path returns uniform blank
frames and therefore **cannot** be the Freeform HTML capture backend. ADR 0001's
decision #3 named the fallback: validate an **external, pinned headless
browser** as the first `FrameCaptureBackend` intended to pass the W0-A gate.
Until that backend existed and passed the gate, the plan's hard rule forbade
landing the HTML layer (FH-01), HTML editor or HTML export.

This ADR records that the named backend now **exists, is pinned, and passes the
full W0-A gate**, which removes the W0-A block on the W2 HTML waves.

## Decision

**Adopt `PlaywrightChromiumBackend` as the validated pinned headless capture
backend for the Freeform HTML renderer.** It implements the same
`FrameCaptureBackend` Protocol as the Qt backend, so the orchestrator
(`HtmlRendererSpike`) and the gate tests are renderer-agnostic.

### Pinned build

`backend_build = playwright-chromium/py{playwright_version}/chrome-{browser_version}`,
e.g. `playwright-chromium/py1.62.0/chrome-151.0.7922.34`. The browser revision
is read from the page's `navigator.userAgent` on first capture (not hardcoded),
so it honestly reflects the installed pinned binary and participates in the
cache key. Two captures with different `backend_build` values are NOT required
to be byte-identical (cross-build is perceptual-tolerance only); within one
build they MUST be (the gate asserts this).

### Capture pipeline (per `capture()` call)

1. **Fresh, isolated browser per capture** — each job launches its own chromium
   with an ephemeral context (no cookies/storage), so a crash cannot corrupt
   the next job and teardown leaves no page/process residue (the
   cancel/crash/restart gate).
2. **Deny-by-default request interceptor** — `page.route("**/*", …)` fulfils
   only bound `topos-asset://<id>` resources and aborts every other scheme
   (http/https/file/data/blob/javascript/about). No-network / no-file is
   enforced at the network layer, not just by the sanitizer.
3. **CSP injected as meta** into the authored document's `<head>` (preserving
   authored `<style>`); `script-src 'none'` blocks model scripts while the
   trusted bridge runs via `page.evaluate`, which is privileged and not subject
   to page CSP.
4. **Trusted seek bridge** (Topos-injected, never from model output) —
   `document.getAnimations().forEach(a => { a.pause(); a.currentTime = timeMs })`
   plus a `--topos-frame/time/progress` CSS-variable contract and an
   `animation-play-state: paused` backstop. Capture is a pure function of the
   frame, never the wall clock.
5. **Readiness** — wait `document.fonts.ready`, then two animation frames of
   settle time (paused animations do not advance) before capture.
6. **RGBA readback** — `page.screenshot(png)` clipped to the surface, decoded
   to tightly-packed RGBA8 via Pillow; uniform frames are classified
   `CaptureFailureCode.BLANK` (defence in depth — this backend does not exhibit
   the Qt blank failure, but the detector stays as a guard).
7. **Failure mapping** — playwright `TimeoutError` → `TIMEOUT`; any other
   playwright `Error` / process termination → `CRASH`; the cancel token is
   checked at launch, after load, and before capture → `CANCELLED`. A blank or
   failed capture is never returned as a valid frame.

## Gate result (this environment)

| Check | Result |
|---|---|
| `is_available()` (self-test, non-blank) | ✓ |
| Determinism — same frame ×5 byte-identical | ✓ |
| Out-of-order — shuffled seek == ordered seek | ✓ |
| Cancel token honoured → `CANCELLED` | ✓ |
| Security — all 24 attack-corpus requests denied | ✓ |
| Evidence records pinned build + UA + frozen animation count | ✓ |

Pinned build measured: `playwright-chromium/py1.62.0/chrome-151.0.7922.34`.
Gate test: `tests/render/html_spike/test_html_spike_playwright_gate.py`
(marker `html_renderer`; skips with reason when chromium is absent).

## Test-setup note (a latent harness bug this exposed)

The gate previously used a `160×90` surface against a fixture that authors a
`320×180` page. With a backend that always blanked (Qt offscreen) this was
harmless. With a **correct** backend the animated card (animating
`left: 24px → 216px`, width 80) moves off the 160px viewport at high seek
frames, producing a legitimately-uniform frame the blank detector would
misclassify as `BLANK`. The corrected gate config uses a `320×180` surface that
matches the authored page, so every seek frame keeps the card on-canvas. This
is a test-harness fix, not a relaxation of the gate.

## Support matrix (updated from ADR 0001)

| Backend | Offscreen RGBA | Determinism | Cancel/Crash | Status |
|---|---|---|---|---|
| `QtWebEngineGrabBackend` (Qt 6.x offscreen) | ✗ blank (ADR 0001 F1) | n/a | widget-based | **Gate blocked** (preview-only) |
| `PlaywrightChromiumBackend` (pinned chromium) | ✓ real RGBA | ✓ byte-identical ×5 + out-of-order | ✓ process-isolated, fresh per capture | **Gate PASSED — W2 unblocked** |
| SVG / structured renderer (existing) | ✓ | ✓ | ✓ | **Unaffected** |

## Consequences

- Positive: **the W0-A gate is passed.** Per the plan's hard rule, the HTML
  layer (FH-01), the `HtmlRendererService` (FH-04) and the HTML editor/export
  waves may now proceed against this backend.
- Positive: the renderer boundary stays the `FrameCaptureBackend` Protocol; the
  Qt backend remains as the interactive-preview path, the Playwright backend as
  the deterministic-export path. Cache keys and evidence distinguish them via
  `backend_build`.
- Negative: W2 now owns a managed external dependency (Playwright + a pinned
  chromium build) and a renderer-process lifecycle (launch/close per capture
  today; a session/pool is a W2 production refinement for throughput — see RD-11).
- Risk: cross-machine byte-identical reproduction requires the same pinned
  chromium build + font set; the cache key encodes both, and cross-build
  comparisons are perceptual-tolerance only (consistent with the plan's
  determinism definition in §2.3).

## Follow-ups (W2)

1. Implement `HtmlRendererService` (FH-04) against this backend with a
   session/pool for export throughput (RD-11 lifecycle), keeping one process
   per in-flight capture for crash isolation.
2. Land `FreeformHtmlMotionLayer` in the scene schema (FH-01) now that the gate
   is passed.
3. Wire the AQ-09 generation protocol's PREVIEW stage to this backend (it
   currently reports "blocked: no renderer backend").
4. Extend `capture()` with a real `asset_id → bytes` resolver so bound assets
   render their real content (the gate fulfils them to a deterministic 1×1
   placeholder today).

## References

- ADR 0001: `docs/adr/0001-html-renderer-offscreen-readback.md`
- Plan: §W0-A, §3.3 (HTML renderer implementation boundary), §5.2 (dependency
  order — "W0-A 通过后才允许 HTML 路线"), S-00
- [Playwright](https://playwright.dev/python/), [Chromium headless](https://developer.chrome.com/docs/chromium/new-headless)
