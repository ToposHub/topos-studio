# ADR 0001 — HTML renderer offscreen RGBA readback (W0-A feasibility gate)

- **Status:** Accepted (W0-A spike complete; Qt WebEngine offscreen path NOT viable)
- **Date:** 2026-08-12
- **Plan ref:** `docs/motion_graphic_clip_remotion_inspired_optimization_plan_2026-08-12.md` §W0-A, §1.6, S-00
- **Supersedes:** none
- **Implementation:** `src/features/teaching_animation/render/html_renderer_spike/`

## Context

The Motion Graphic Clip optimization plan gates the entire Freeform HTML
renderer effort (waves W2–W6, capability IDs FH-01…FH-13) behind a
**feasibility gate (W0-A)**: before any HTML layer lands in the project
schema, we must prove a browser backend can produce **deterministic, offline,
RGBA** frames — seek by `frame/fps`, repeatable hashes, no network, no file
access, cancellable, crash-recoverable.

The plan explicitly warned of the risk: Qt's `QWebEngineView` is a `QWidget`
whose page is composited in a **separate WebEngine process**; that is fine for
interactive preview, but it is *not* proof of windowless RGBA readback,
determinism, or export throughput. The existing `WebEnginePreviewBridge` only
pushes an SVG into a `QWebEngineView` for GUI preview — it never read pixels
back.

This ADR records the W0-A experiment and the resulting decision.

## Experiment

A standalone spike package implements the full gate contract with a pluggable
`FrameCaptureBackend`:

- **Fixtures** — 3 benign HTML pages (static, CSS-keyframe animated,
  asset/font referenced) + 24 malicious HTML/CSS payloads covering 10 abuse
  vectors (script, network, file, data-uri, blob, frame, popup, form,
  storage, permission, gpu-api, download, asset, css-expression).
- **Runtime sandbox** — strict CSP (`default-src 'none'`, `script-src 'none'`,
  `style-src 'unsafe-inline'`), deny-by-default URL policy with a single
  read-only `topos-asset://` scheme, ephemeral profile, disabled
  scripts/downloads/popups/storage/permissions/GPU-APIs, resource budgets.
- **Capture backend** — `QtWebEngineGrabBackend` loads the page offscreen,
  applies CSP, seeks the frame, and readbacks via `QWebEngineView.grab()`.
- **Determinism** — same-frame repetition + out-of-order seek probes with
  byte-level hash comparison; uniform-frame detector classifies blank captures
  as `CaptureFailureCode.BLANK` (never as a valid frame).

The gate is encoded so a backend that can only produce **blank frames**
self-reports unavailable (`is_available() == False`) and the real-browser test
**skips with reason** rather than passing.

## Findings

### F1 — Qt WebEngine offscreen `grab()` produces blank frames (BLOCKING)

On this environment (macOS, Qt 6.x, `QT_QPA_PLATFORM=offscreen`), regardless
of Chromium rasterizer flags (`--disable-gpu`, `--enable-software-rasterization`,
SwiftShader, `--disable-gpu-compositing`), `QWebEngineView.grab()` returns a
**uniform all-white frame**. The page IS loaded (`loadFinished` fires) and the
DOM IS interactive, but the composited pixels never reach the offscreen
widget's backing store, so the grabbed pixmap is blank.

This is exactly the failure mode the plan anticipated. The capture backend
classifies these as `CaptureFailureCode.BLANK`, so the gate is correctly
**NOT passed** on the Qt WebEngine offscreen path. The real-browser test skips
with the documented reason; the HTML layer schema (FH-01) therefore does NOT
land in this batch, per the plan's hard rule.

### F2 — The runtime sandbox and security corpus are sound (no browser needed)

All 24 attack fixtures are denied by the pure-data URL policy + CSP:
network/file/data/blob/javascript/iframe/object/popup/form/storage/permission/
download/css-expression vectors each produce a structured denial reason. This
layer is fully unit-tested and backend-independent; it carries forward
unchanged into W2.

### F3 — The determinism contract is implementable and testable

The cache-key composition, repetition probe, out-of-order probe and
uniform-frame detector are pure functions with full unit coverage. They are
ready to drive whichever backend eventually passes F1.

## Options considered

| Option | Verdict |
|---|---|
| Use the existing `WebEnginePreviewBridge` screenshot path for export | **Rejected.** It is a GUI preview of an SVG, not windowless RGBA; F1 shows offscreen grab() is blank. The plan forbids "playing WebView screenshot as export". |
| Force a display-backed `QWebEngineView` on a virtual framebuffer (Xvfb) | **Not viable on macOS / CI-portable.** Adds a platform dependency the project does not have; still single-process-fragile. |
| Qt WebEngine `printToPdf` + rasterize | **Rejected.** Vector PDF rasterization is a second renderer with its own determinism/animation story; does not honor CSS seek semantics. |
| **Pin an external, fixed-version headless browser renderer** (e.g. headless Chromium via CDP, or Playwright) as a separate `FrameCaptureBackend` | **Chosen for W2.** Decouples the capture lifecycle from Qt's QWidget model; gives hermetic process isolation, explicit RGBA readback APIs, pinned build for cache keys, and crash-restart boundaries the plan requires. |
| Abandon the HTML route entirely; SVG/structured-only | **Deferred.** Keep as the documented fallback (the SVG/structured path is unaffected and continues through W0-B/W1), but do not retire HTML before the external headless backend is measured. |

## Decision

1. **W0-A status: spike complete; gate NOT passed on Qt WebEngine offscreen.**
   No HTML layer schema, no HTML editor, no HTML export lands until a backend
   reports `is_available() == True` AND passes the determinism + cancel probes.
2. **Adopt the `FrameCaptureBackend` Protocol as the renderer boundary.** W2
   will implement `HtmlRendererService` against this Protocol, not against
   `QWebEngineView` directly.
3. **W2 will validate an external pinned headless-browser backend** (Chromium
   via CDP or Playwright, version-pinned) as the first `FrameCaptureBackend`
   intended to pass the gate. Its binary version, launch args and sandbox
   profile become part of `renderer_build` and the cache key.
4. **The runtime sandbox (CSP + URL policy + asset scheme + budgets) is frozen
   now** and reused unchanged by every future backend. F2 is its regression
   target.
5. **W0-B and W1 proceed immediately.** They depend only on the neutral core
   contracts and the motion language — not on the HTML gate. SVG/structured
   rendering is unaffected.

## Support matrix

| Backend | Offscreen RGBA | Determinism | Cancel/Crash | Status |
|---|---|---|---|---|
| `QtWebEngineGrabBackend` (Qt 6.x offscreen) | ✗ blank (F1) | n/a | widget-based | **Gate blocked** |
| External pinned headless Chromium (CDP) | pending W2 | pending W2 | process-isolated | **To validate in W2** |
| SVG / structured renderer (existing) | ✓ (QSvgRenderer) | ✓ existing | ✓ existing | **Unaffected, continues** |

## Consequences

- Positive: the renderer boundary is now a versioned Protocol; cache keys,
  evidence and the security corpus are backend-neutral and reusable.
- Positive: W0-B (core contracts) and W1 (motion language) are unblocked.
- Negative: W2's HTML renderer requires integrating and pinning an external
  headless browser, which adds a managed dependency and a renderer-process
  lifecycle to own.
- Risk: if no backend passes the gate on the target platforms, the HTML route
  is retired in favour of the SVG/structured path; this is the documented
  fallback and does not affect the rest of the roadmap.

## References

- Plan: `docs/motion_graphic_clip_remotion_inspired_optimization_plan_2026-08-12.md` §1.6, §3.3, §W0-A, S-00
- [Qt WebEngine Overview](https://doc.qt.io/qt-6/qtwebengine-overview.html)
- [QWebEngineView](https://doc.qt.io/qt-6/qwebengineview.html)
- Remotion (design reference only): [spring](https://www.remotion.dev/docs/spring), [interpolate](https://www.remotion.dev/docs/interpolate)
