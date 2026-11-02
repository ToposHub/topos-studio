# Scope appearance review — 2026-11-02

Reference: the user's running DaVinci Resolve Color page, expanded Scopes dialog in Quad View. Visually inspected Parade, Waveform, Vectorscope and Histogram together. Returned Resolve to the original docked Parade layout afterwards.

Topos evidence: attached PNGs were captured from the real ScopePanel / native Qt shared-context presenter using the existing bench_gpu_scopes.py harness and tests/test_videos/2K_h264.mp4. The Resolve clip was different: these captures compare UI layout and annotations, not identical signal values.

Corrections after direct comparison:
- Histogram: black background, vertically stacked R/G/B plots, amber vertical guides and top 0–1023 ruler.
- Vectorscope: amber circumference, angle ticks and crosshair, open 75% target brackets. Static labels are composed in a transparent QWidget so the shared GL texture does not overwrite them. Reference target locations follow 1x/2x zoom.
- Presenter: device-pixel scaling of waveform label gutter; logical-pixel geometry and padding for vector labels. Corrected QRectF import so false-color legends render.
- Shared header: dedicated dark header instead of controls covering the trace.

Checks:
- Existing GPU benchmark synthetic numerical assertions and all eight mode content checks: passed.
- Native Qt shared-context presentation and screenshot captures: all eight modes passed.
- Focused regression tests: 16 passed, 1 native-window test skipped under the pytest offscreen configuration.
- Diff check and Python compilation passed. Independent source review found no actionable issue.

4K input / 512x288 analysis / mean of 30 passes (GPU scope work, not complete application playback): waveform 6.34 ms; vectorscope 2.02 ms; parade 5.20 ms; histogram 4.96 ms; split 10.94 ms. Full run in performance.log. These are absolute timings, not a controlled before/after speedup claim.

Limits: CIE and false color have not been visually matched against corresponding Resolve views. Topos retains its HDR headroom and existing measurement transforms. No pixel-identical or same-frame signal-accuracy claim is made; this review covers the four common scope layouts at the inspected sizes.
