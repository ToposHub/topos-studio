# NLE Realtime Playback Rebuild — Verification Report

Date: 2026-06-13
Branch: fix-ui
Plan ref: `docs/professional_nle_realtime_playback_rebuild_plan_2026-06-13.md`

## Executive Summary

The plan describes a ground-up rebuild of the playback architecture across 11 phases (P0–P10). **Verification found the plan is ~95% already implemented** through the prior "时间线视频播放优化 1–6" + "声音播放修复" + "时间线播放优化" commits. The plan's Section 1 "current state" diagnosis is stale — it predates these implementations.

Three gaps were found and fixed:
1. **P0.4:** `tools/playback_trace_dump.py` — one-click trace export CLI tool (did not exist).
2. **P10 acceptance:** `test_seek_latency_p95` was flaky (~25% failure) due to under-sampled random benchmark (N=20, P95≈max). Fixed: N→200 + seeded local RNG. Now 16/16 benchmark tests pass reliably.
3. **P8 stale tests:** 3 pre-existing failures in `tests/video/` (`test_upload_async_data_conversion`, `test_upload_yuv_planar_falls_back_when_r16ui_self_test_fails`, `test_with_statement`) were found on a full-suite re-run. Root cause: the tests asserted **legacy contracts** that the zero-copy GPU optimizations intentionally superseded (`.tobytes()` → ndarray buffer-protocol DMA; manual self-test → auto-self-test on first >8-bit upload; 8-bit → separate R8 pool). The implementation is correct (documented in docstrings); the tests were updated to assert the current zero-copy/auto-test/R8-pool behaviour. Now `tests/video/` is 613 passed / 6 skipped / 0 failed.

> **Correction:** an earlier draft of this report claimed "726 all green / 66+ GPU zero-copy tests pass". That was over-stated — it was based on a curated subset, not a full `tests/video/` run. The 3 P8 failures above predate this work (identical at parent commit `4b6d111`) but should have been caught before the claim was written.

No production code changes were needed for P1–P9; each phase's exit criteria were verified against existing implementation and tests.

---

## Phase-by-Phase Verification Matrix

| Phase | Exit Criteria | Status | Evidence |
|---|---|---|---|
| **P0** Diagnostics & baseline | Trace schema, benchmark, material matrix, dump tool, failure thresholds | ✅ Fixed gap (dump tool) | `playback_frame_trace.py` (trace), `tests/benchmark/` (benchmarks), `MATERIAL_MATRIX.md`, `acceptance_criteria.py`, **new** `tools/playback_trace_dump.py` |
| **P1** PlaybackEngine state machine | Deterministic state transitions, no signal storms, generation invalidation | ✅ Already met | `EditPlaybackController`: pure O(1) tick(), `PlaybackState`+`PlaybackMode`, generation at worker layer |
| **P2** Audio master clock / no live seek | live_seek_count=0 during playback, underrun=0 | ✅ Already met | `AudioVideoSynchronizer` never instantiated in edit path. Three audio.seek sites all user-initiated or guarded (`is_playing` check). Covered by `test_audio_reanchor_does_not_seek_live_playback` |
| **P3** Video decode queue | UI thread no sync decode, queue-driven, generation cleanup | ✅ Already met | `PreviewRenderWorker(QThread)` with queue. `request_render` → `put_nowait`. Stale drops at 3 checkpoints. `test_playback_sync_decode_guard`, `test_video_playback_no_ui_copy` |
| **P4** Preroll & first-frame | No blue/empty viewer, clip warmup, preroll | ✅ Already met | `_emit_cached_placeholder_frame` (thumbnail during warmup), `_prewarm_sequence_decode_sources` (background open+probe ~70ms), dark clear color (0.08, not blue), `_schedule_current_frame_warmup` |
| **P5** Multitrack bundle & frame order | Same-generation layer bundle, no regressing frames, late-frame policy | ✅ Already met | `_execute_render(frame, layers, generation_id)` atomic per-frame. Monotonic frame guard + 3-layer generation filtering. `edit_gpu_preview_renderer` YUV→RGB + multi-track blend |
| **P6** Proxy / auto-degrade | Proxy ladder, priority, auto tier selection, invalidation | ✅ Already met | `proxy_manager.py`, `disk_proxy_service.py`, `proxy_generator.py`. 56 proxy tests pass |
| **P7** Render cache | Cache key with effect/color/track hash, scoped invalidation | ✅ Already met | `render_cache.py` + encoder + worker. `invalidate_cache(start, end)` scoped. 21 lifecycle tests pass |
| **P8** GPU upload / zero-copy | Texture pool, planar YUV, zero-copy, no QImage on hot path | ✅ Fixed 3 stale tests | `video_texture_pool.py`, `yuv_gpu_upload.py`, `macos_vt/`, `windows_hw/`. Tracked by `EditPlaybackFrameTrace.qimage_copy`. Impl correct; 3 stale tests (asserting pre-zero-copy contracts) updated. `tests/video/` now 613 pass / 0 fail |
| **P9** UI switch to engine | Timeline sends commands, viewer consumes ready frames, old path fallback | ✅ Already met | `TimelinePanel` → controller commands (not per-tick decode). `ProgramViewer.set_frame(texture/qimage)`. `EditPlaybackController` handles scrub. `PlaybackControllerTimer` is thin tick driver |
| **P10** Acceptance matrix | All benchmarks pass, metrics explainable | ✅ Fixed flaky test | 16/16 benchmark tests pass. Seek-latency P95 fixed (was ~25% flaky → now deterministic ~73ms) |

---

## Test Baseline

| Suite | Tests | Status |
|---|---:|---|
| Timeline renderer + audio + audio integration | 187 | ✅ all pass |
| Frame provider, queues, decoded submit, no-ui-copy, planar, coordinator, sync guard, proxy, cadence, preview | 195 | ✅ all pass |
| macOS VT zero-copy (3 files), Windows HW, GPU frame, render cache, clip provider, GPU decode | 168 | ✅ all pass |
| Render cache, disk proxy, visibility, composite graph | 154 | ✅ all pass |
| Benchmark acceptance (latency, performance, renderer queue, UI tick) | 16 | ✅ all pass |
| Playback trace dump tool | 6 | ✅ new, all pass |
| `tests/video/` full suite (YUV/PBO/zero-copy/texture pool) | 613 | ✅ 0 fail (3 stale P8 tests fixed) |

> **Note on counts:** the per-suite rows above were the curated subsets run during phase verification and were all green at the time. The full `tests/video/` re-run (613 pass / 6 skip / 0 fail) is the authoritative P8 baseline after the 3 stale-test fixes. The earlier "726 all green" headline double-counted across overlapping curated runs and missed the 3 P8 failures; it is withdrawn.

---

## Commits on fix-ui

1. `ad7d254` — P0: 播放诊断基准线 — 补齐 trace dump 工具
2. `ac506a6` — P10: 修复 seek-latency 基准测试统计不稳定 + 验证报告
3. (this commit) — P8: 修复 3 个零拷贝过时测试（PBO ndarray / R16UI 自测回退 / R8 池）+ 更正验证报告"全绿"过度声称

---

## What the Plan Got Wrong

The plan's Section 1 "current conclusion" describes a broken per-tick architecture that no longer exists. The actual current architecture is:

```
PlaybackControllerTimer (QTimer, ~fps interval)
  → EditPlaybackController.tick()          [pure O(1) frame scheduling]
  → frame_changed signal
  → TimelinePanel._on_timer_frame_ready   [backpressure check + playhead update]
  → EditPage._on_playhead_changed         [emits timeline_frame_requested]
  → TimelinePreviewRenderer.request_render [queue put_nowait]
  → PreviewRenderWorker(QThread)           [async decode + composite + GPU]
  → ProgramViewer.set_frame                [present ready texture]
```

Audio runs independently via `AudioPlaybackManager` with `AudioClock`. The `AudioVideoSynchronizer` (which would wire video→audio seek) is **never instantiated** in the edit path. The plan's 8-state enum (`LOADING_MEDIA/READY/PREROLLING/BUFFERING/SEEKING`) is not a single enum but the responsibilities are correctly distributed across the controller, renderer backpressure, and audio manager guards.

---

## Recommendations

1. **Update the plan document** to reflect that P1–P9 are complete, so future work focuses on P6/P7/P8 refinements (proxy quality tiers, render cache coverage, zero-copy on Linux) or new features beyond the plan's scope.
2. **Add real seek-latency measurement** to the benchmark suite when a headless decode pipeline test becomes feasible (the current synthetic test is a statistical smoke test, not a real perf measurement).
3. **Continue on fix-ui** or merge to dev/main when ready.
