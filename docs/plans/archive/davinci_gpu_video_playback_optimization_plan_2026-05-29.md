# Davinci-like GPU Video Playback Optimization Plan

Date: 2026-05-29

Goal: make Color page playback approach a DaVinci-style hot path:

```text
GPU decode -> GPU texture / video memory -> GPU color pipeline -> GPU output
```

Hard constraints for playback hot path:

- No CPU YUV -> RGB conversion in UI tick.
- No per-frame GPU readback.
- No per-frame `glFinish` / `ctx.finish`.
- UI tick only advances clock, consumes ready frames, and submits latest frame.
- CPU fallback is allowed only as explicit, observable degradation, preferably outside UI tick.

## Current Status Summary

| Area | Current state | Main gap | Target state |
|---|---|---|---|
| Color page video playback | Implemented through `EnhancedVideoPlaybackEngine` and v3 dual path when config resolves to v3. | v2 fallback remains possible and some controller paths still touch old decoder APIs. | Color playback defaults to v3, with fallback logged and visible. |
| `AsyncRenderer` | Supports `RENDER_VIDEO_FRAME`, planar YUV GPU upload, and `RENDER_VIDEO_GPU_FRAME`. | Zero-copy GPU frame path needs stronger validation and type cleanup. | Renderer accepts `DecodedFrame` and `GpuDecodedFrame` without UI-thread conversion. |
| `VideoPlaybackEngine` / `PlaybackCoordinator` | Implemented. v3 uses coordinator/session/worker queue. | First-frame/seek can still use blocking CPU paths, acceptable only outside playback hot path. | Playback hot path remains nonblocking; non-hot-path sync decode is bounded and logged. |
| `DecodeWorker` | Implemented with proxy/master workers and ready queue. | Needs tests that prove no UI tick sync decode under slow decode. | Worker-only decode during playback; UI remains responsive under starvation. |
| 4K H.264 / HEVC | Hardware decode and macOS VT zero-copy code exist. | macOS zero-copy can be skipped because codec probe may return `unknown`; generic hwaccel path downloads to CPU. | H.264/HEVC on macOS uses VT -> IOSurface -> GL external planes when capability gate passes. |
| 4K ProRes | Supported through FFmpeg/PyAV CPU/intra path and planar/proxy handling. | Not a GPU-decode path; must optimize CPU decode + GPU YUV upload or use generated proxies. | ProRes playback uses proxy/planar GPU upload reliably; master remains high quality for pause/seek/export. |
| macOS GPU YUV | YUV plane uploader exists. | CPU plane extraction still happens unless true zero-copy is active. | Zero-copy for supported GOP codecs; CPU-planar upload fallback is explicit. |
| `glFinish` / readback | Normal video render sync defaults to `none`; readback mostly self-test/bake/export. | Need tests/metrics proving playback does not call finish/readback per frame. | Playback render stats show zero finish/readback in hot path. |

## Key Evidence From Current Code

| Evidence | File | Meaning |
|---|---|---|
| Color creates `EnhancedVideoPlaybackEngine` and enables dual path when v3 is active. | `src/app/controllers/file_controller.py:1428` | Color page is connected to v3 playback architecture. |
| v3 load delegates to `PlaybackCoordinator`. | `src/shared/video/video_playback.py:2529` | `EnhancedVideoPlaybackEngine` is a UI-compatible wrapper. |
| v3 start calls coordinator, starts monitoring, and warms workers. | `src/shared/video/video_playback.py:3305` | Decode workers are in the playback path. |
| UI tick calls coordinator and submits returned frame. | `src/shared/video/video_playback.py:3690` | Tick is designed as clock + queue + submit, not decode. |
| `VideoSession` can use `SourceManager` or shared `ColorFrameProviderAdapter`, then creates `DecodeWorkerManager`. | `src/shared/video/session.py:386` | Worker pipeline exists, but shared provider is disabled by default. |
| `probe_video_metadata()` may return `codec_name='unknown'` from GStreamer fast probe. | `src/shared/media/factory.py:423` | This can block macOS VT zero-copy codec gate. |
| macOS VT zero-copy gate uses the probed codec before creating `MacOSVtZeroCopySource`. | `src/shared/media/factory.py:628` | If codec is `unknown`, H.264/HEVC zero-copy may not activate. |
| Generic hardware decode path still calls `transfer()` / `to_ndarray()`. | `src/shared/media/hwaccel.py:820` | This is hardware decode plus CPU download, not GPU-resident playback. |
| `AsyncRenderer` planar path uploads YUV planes and falls back on renderer-thread CPU conversion. | `src/features/color/core/async_renderer.py:2916` | UI tick avoids CPU YUV -> RGB, but renderer fallback still exists. |
| `AsyncRenderer` GPU frame path binds external GPU planes. | `src/features/color/core/async_renderer.py:3085` | True zero-copy render path exists but needs end-to-end activation. |
| Video render sync mode defaults to no finish. | `src/features/color/core/async_renderer.py:748` | Per-frame finish is intentionally avoided for video. |

## Optimization Plan Table

| ID | Priority | Work item | Problem | Implementation approach | Files likely touched | Acceptance criteria |
|---|---:|---|---|---|---|---|
| P0-1 | Critical | Fix macOS zero-copy codec probing | `probe_video_metadata()` can return `unknown` via GStreamer fast probe, causing `is_zero_copy_available(codec='unknown')` to reject H.264/HEVC. | Add an FFmpeg/PyAV metadata probe for codec before VT gate, or make `probe_video_metadata()` prefer FFmpeg when codec is needed. Do not let GStreamer's quick BGR probe mask codec/pixel format. | `src/shared/media/factory.py`, `src/shared/media/ffmpeg_source.py`, tests under `tests/media/` | For an H.264/HEVC fixture, factory sees codec `h264`/`hevc`, attempts `MacOSVtZeroCopySource`, and logs the exact gate result. |
| P0-2 | Critical | Make zero-copy activation observable | Current fallback reasons are logs only and easy to miss. | Add structured decode/render path fields to playback HUD/perf stats: `decode_path`, `render_frame_path`, `zero_copy`, `fallback_reason`. | `src/shared/media/performance_monitor.py`, `src/shared/ui/playback_hud.py`, `src/shared/video/video_playback.py`, `src/features/color/core/async_renderer.py` | During playback the user/developer can see `macos_vt_iosurface_zero_copy`, `planar_yuv_r8_upload`, `hwaccel_download_packed_rgb`, etc. |
| P0-3 | Critical | Verify `MacOSVtZeroCopySource` satisfies playback source protocol | SourceManager expects `seek_frame()` on non-sequential proxy requests; `MacOSVtZeroCopySource` currently exposes `read_next_frame()` only. | Implement or explicitly constrain `seek_frame()` / `read_frame()` / metadata access for VT source, or wrap it in an adapter that fits `MediaSource`. For unsupported seek, force worker generation reset and GOP restart without UI blocking. | `src/shared/media/macos_vt/source.py`, `src/shared/media/macos_vt/packet_demux.py`, `src/shared/video/playback_clock.py` | v3 playback can seek/scrub H.264/HEVC without AttributeError or silent fallback. |
| P0-4 | Critical | Add end-to-end zero-copy test gate | Existing tests prove pieces, not the complete hot path. | Add a macOS-gated integration test using env `TOPOS_VIDEO_ZERO_COPY=force` or a mock VT source that emits `GpuDecodedFrame`. Assert renderer receives `RENDER_VIDEO_GPU_FRAME`, no `Texture.write`, no `to_ndarray`, no `ctx.finish`. | `tests/media/test_macos_vt_zero_copy_playback.py`, `tests/media/verify_zero_copy_playback.py` | Test fails if GPU frame path downloads/uploads CPU data or calls finish in playback. |
| P1-1 | High | Clean up `GpuDecodedFrame` color metadata types | GPU path may store string color matrix/range where YUV render helpers expect numeric constants. | Normalize `GpuDecodedFrame.color_matrix` and `color_range` to renderer constants at submit boundary, or add explicit mapping in renderer. Avoid mixed string/int state. | `src/shared/media/gpu_frame.py`, `src/shared/media/macos_vt/source.py`, `src/shared/video/video_playback.py`, `src/features/color/core/async_renderer.py` | H.264/HEVC zero-copy frames render with correct BT.709/BT.2020 and limited/full range. |
| P1-2 | High | Ensure `current_video_bit_depth` always updates | GPU frame path only sets `current_video_bit_depth` if the attribute does not exist. | Always assign `self.current_video_bit_depth = gpu_frame.bit_depth` when binding GPU planes. | `src/features/color/core/async_renderer.py` | Switching 8-bit and 10-bit clips in one session uses correct range constants. |
| P1-3 | High | Split true zero-copy from hwaccel CPU-download path | Generic hwaccel path marks hardware decode but returns CPU `DecodedFrame` through `transfer()` / `to_ndarray()`. | Rename/status-tag this path as `hwaccel_download_*`; keep it as fallback. Only `GpuDecodedFrame.zero_copy=True` counts as GPU-resident. | `src/shared/media/factory.py`, `src/shared/media/hwaccel.py`, `src/shared/media/frame_path.py` | Metrics never label CPU-downloaded frames as zero-copy. |
| P1-4 | High | Harden planar YUV upload fallback policy | Renderer-thread CPU YUV fallback is safer than UI fallback but can still hurt playback. | Add a threshold and visible fallback reason when `_do_upload_video_planes()` fails. Optionally auto-degrade to lower proxy or disk proxy rather than doing repeated CPU conversion. | `src/features/color/core/async_renderer.py`, `src/shared/media/performance_monitor.py`, `src/shared/media/proxy_manager.py` | Repeated GPU upload failures do not silently run CPU YUV conversion every frame. |
| P1-5 | High | Fix/validate R16UI path selection diagnostics | Debug path says `planar_yuv_r16ui_upload` whenever R16UI available, even for 8-bit or unsupported formats. | Compute path after actual upload mode is known from `YUVGPUUploader.get_last_upload_timing()`. | `src/features/color/core/async_renderer.py`, `src/shared/video/yuv_gpu_upload.py` | Debug/perf path matches actual texture format used. |
| P2-1 | Medium | Resolve test environment dependency gap | Media tests failed here due missing `cv2` and `moderngl`, not assertion failures. | Add dev dependency docs or test skips/mocks where GPU packages are unavailable. Prefer optional imports in tests that only verify pure Python logic. | `requirements.txt`, `pyproject.toml`, `tests/media/*` | Targeted media tests run cleanly in CI/dev environment. |
| P2-2 | Medium | Add hot-path static guards | Regressions can reintroduce `get_frame()`, `seek_frame()`, `to_ndarray()`, or `finish()` in tick/render paths. | Add tests/grep guards for prohibited calls in `_on_play_tick`, `PlaybackCoordinator.tick`, `VideoSession.tick`, and `RENDER_VIDEO_GPU_FRAME`. | `tests/media/test_hot_path_guards.py` | Tests fail if forbidden calls appear in hot path. |
| P2-3 | Medium | Improve first-frame strategy for professional codecs | First frame still uses bounded blocking master/proxy attempts. Acceptable outside hot path, but slow ProRes can feel like load hang. | Keep v3 loaded with placeholder, start workers earlier, show decode status, and avoid v2 fallback for ProRes/high-bit-depth unless fatal. | `src/shared/video/video_playback.py`, `src/shared/video/session.py`, UI status/HUD files | ProRes load never falls back to v2 due first-frame timeout alone. |
| P2-4 | Medium | Reduce packed RGB CPU upload use during playback | Packed RGB upload path is still valid fallback, but not ideal for 4K. | Prefer planar proxy for YUV sources, disk proxy for ProRes, and zero-copy for H.264/HEVC. Add warnings when playback uses `packed_rgb_cpu_upload` at 4K. | `src/shared/video/playback_clock.py`, `src/shared/media/factory.py`, `src/features/color/core/async_renderer.py` | 4K playback path is not silently `packed_rgb_cpu_upload` unless explicitly degraded. |
| P3-1 | Medium | Enable shared frame provider only after protocol coverage | `video_color_use_shared_frame_provider` is default false. | Finish tests for `ColorFrameProviderAdapter` protocol parity, then enable shared provider by default if stable. | `src/shared/video/color_frame_provider_adapter.py`, `src/shared/video/session.py`, `config.json`, `src/shared/config/config_manager.py` | Color/Composite share one clip session without duplicate decode when enabled. |
| P3-2 | Medium | Add runtime playback path report command | Developers need quick confirmation of actual chain. | Add a diagnostic method like `video_controller.get_pipeline_report()` returning config, source type, decode path, render path, queue stats, fallback reason. | `src/shared/video/video_playback.py`, `src/shared/video/session.py`, `src/shared/video/playback_state.py` | One log/status dump can answer "am I zero-copy right now?" |
| P4-1 | Low | ProRes proxy policy | 4K ProRes generally should use optimized proxy for realtime playback unless hardware can sustain CPU decode. | Add policy: if ProRes >= 4K and proxy exists, use disk proxy; if no proxy, use memory proxy/planar; master only for pause/seek/export. | `src/shared/media/proxy.py`, `src/shared/media/disk_proxy_service.py`, `src/shared/video/playback_clock.py` | 4K ProRes playback avoids direct full-res master decode unless user forces it. |
| P4-2 | Low | Document supported chains | Current code supports multiple chains but docs do not clearly say which are production-ready. | Add a support matrix for codec/platform/path. | `docs/4k-playback-optimization.md`, this document | Developers can tell expected behavior for macOS H.264, HEVC, ProRes, Windows, CPU fallback. |

## Target Playback Chains

| Codec / platform | Preferred chain | Fallback chain | Notes |
|---|---|---|---|
| macOS H.264 8-bit | VT decode -> CVPixelBuffer/IOSurface -> GL external NV12 planes -> GPU YUV -> grade -> output | FFmpeg hwaccel download -> packed CPU upload, or CPU planar -> GPU YUV upload | Preferred chain requires P0 fixes. |
| macOS HEVC 10-bit | VT decode -> CVPixelBuffer/IOSurface -> GL external P010 planes -> GPU YUV -> grade -> output | CPU planar 10-bit -> R16UI/R16/f16 GPU upload -> grade | Must validate bit depth and range. |
| macOS ProRes 422/4444 4K | Disk proxy or CPU ProRes decode -> planar YUV/YUVA -> GPU YUV upload -> grade | Packed RGB CPU upload | ProRes is not currently expected to be VT zero-copy. |
| Non-macOS H.264/HEVC | Platform hwaccel if available, otherwise FFmpeg CPU -> planar/packed upload | CPU decode/proxy | Windows D3D11 zero-copy code exists separately but needs its own verification. |

## Acceptance Matrix

| Acceptance item | How to verify | Pass condition |
|---|---|---|
| Color page uses v3 | Open a video and inspect `get_pipeline_decision()` / logs. | Effective mode is `v3`; v2 only if user forced or fatal fallback. |
| UI tick does not decode | Unit test / grep guard around `_on_play_tick()` and `VideoSession.tick()`. | No direct `get_frame`, `seek_frame`, `read_next_frame`, `to_ndarray`, blocking waits. |
| H.264 macOS zero-copy activates | Use H.264 fixture with `TOPOS_VIDEO_ZERO_COPY=force` or real probe JSON. | `decoder_type=macos_vt_zero_copy`, frame type is `GpuDecodedFrame`. |
| HEVC 10-bit preserves bit depth | Use HEVC Main10/P010 fixture. | Renderer path reports external P010 or planar R16/R16UI; no forced 8-bit conversion. |
| ProRes avoids UI CPU YUV conversion | Play 4K ProRes fixture. | No CPU YUV -> RGB in UI tick; if CPU fallback happens, it is renderer-thread and logged. |
| No per-frame finish | Inspect renderer stats after playback. | `finish_call_count` remains 0 for normal video playback frames. |
| No per-frame readback | Monkeypatch/trace FBO read during playback. | No `read()` / `read_pixels` calls in playback hot path. |
| Backpressure works | Artificially slow renderer. | Queue depth remains bounded; latest frame wins; UI stays responsive. |
| Fallback is visible | Force zero-copy unavailable. | HUD/log reports fallback reason and actual path. |

## Suggested AI Implementation Prompts

### Prompt 1: Fix macOS zero-copy activation

```text
In Topos Color, fix macOS H.264/HEVC zero-copy activation.

Context:
- PyQt6 + ModernGL + FFmpeg/PyAV + macOS VideoToolbox.
- Target chain: VT decode -> CVPixelBuffer/IOSurface -> GL external planes -> AsyncRenderer.
- Current issue: MediaSourceFactory.probe_video_metadata() may return codec_name='unknown' from GStreamer fast probe, so is_zero_copy_available(codec='unknown') rejects H.264/HEVC before MacOSVtZeroCopySource is attempted.

Tasks:
1. Make MediaSourceFactory use reliable codec probing before macOS VT gate.
2. Preserve fast metadata probing for non-zero-copy cases if possible.
3. Add tests proving H.264/HEVC codec is not masked as unknown for zero-copy gating.
4. Ensure fallback reason is logged when zero-copy does not activate.

Files:
- src/shared/media/factory.py
- src/shared/media/ffmpeg_source.py
- src/shared/media/macos_vt/capability.py
- tests/media/
```

### Prompt 2: Make GpuDecodedFrame path robust

```text
Harden the GpuDecodedFrame render path in AsyncRenderer.

Tasks:
1. Normalize color_matrix/color_range from GpuDecodedFrame to the numeric constants expected by YUV rendering.
2. Always update current_video_bit_depth when binding a GPU frame.
3. Add tests for switching 8-bit NV12 and 10-bit P010 frames in one renderer session.
4. Assert RENDER_VIDEO_GPU_FRAME does not call Texture.write, to_ndarray, transfer, readback, or ctx.finish.

Files:
- src/shared/media/gpu_frame.py
- src/shared/media/macos_vt/source.py
- src/shared/video/video_playback.py
- src/features/color/core/async_renderer.py
- tests/media/
```

### Prompt 3: Add playback path diagnostics

```text
Add a developer-facing playback path report for Color video playback.

The report should include:
- requested/effective pipeline mode
- source backend
- codec/pixel format/bit depth
- decode_path
- render_frame_path
- zero_copy boolean
- fallback reason
- renderer queue depth
- finish/readback counters
- YUV upload mode and timing

Expose it through a method on EnhancedVideoPlaybackEngine and log it on first displayed frame.

Files:
- src/shared/video/video_playback.py
- src/shared/video/session.py
- src/shared/video/playback_state.py
- src/features/color/core/async_renderer.py
- src/shared/media/performance_monitor.py
```

### Prompt 4: Lock down hot-path regressions

```text
Add tests that prevent playback hot-path regressions.

Rules:
- _on_play_tick(), PlaybackCoordinator.tick(), and VideoSession.tick() must not directly decode, seek, block, convert YUV to RGB, call to_ndarray, or call glFinish.
- RENDER_VIDEO_GPU_FRAME must not use Texture.write, to_ndarray, transfer, FBO readback, or ctx.finish.
- RENDER_VIDEO_FRAME planar fallback may do renderer-thread CPU conversion only after GPU plane upload failure and must log the fallback.

Files:
- tests/media/test_hot_path_guards.py
- tests/media/test_playback_sync_decode_guard.py
- tests/media/test_video_playback_decoded_submit.py
```

## Test Commands

Run targeted tests after each phase:

```bash
pytest -q \
  tests/media/test_playback_coordinator_tick.py \
  tests/media/test_decode_worker.py \
  tests/media/test_v3_planar_playback_path.py \
  tests/media/test_video_playback_decoded_submit.py \
  tests/media/test_gpu_frame.py \
  tests/media/test_playback_sync_decode_guard.py
```

If the local environment lacks `cv2` or `moderngl`, fix dependencies or add appropriate skips before treating failures as pipeline regressions.

Optional runtime verification:

```bash
TOPOS_VIDEO_ZERO_COPY=force python -m tests.media.verify_zero_copy_playback
```

## Definition Of Done

The optimization is complete when these are all true:

1. Color page defaults to v3 for video playback unless user forces v2.
2. H.264/HEVC on macOS can produce `GpuDecodedFrame` through `macos_vt_iosurface_zero_copy`.
3. `AsyncRenderer` can render those GPU frames without CPU upload/download/readback.
4. 4K ProRes playback uses proxy/planar GPU upload and never performs CPU YUV -> RGB in UI tick.
5. Playback HUD/logs clearly show the actual chain for every clip.
6. Targeted media tests pass in the documented dev environment.
7. Regression guards fail loudly if hot-path CPU conversion, readback, or per-frame finish is reintroduced.

## Implementation Status

Completed on 2026-05-29:

| ID | Status | Notes |
|---|---|---|
| P0-1 | Done | FFmpeg probe added, codec_name no longer 'unknown' |
| P0-2 | Done | decode_path, render_frame_path, zero_copy in VideoPlaybackStats |
| P0-3 | Done | seek_frame(), read_frame(), get_metadata() added to VT source |
| P0-4 | Done | End-to-end zero-copy test gate (8 tests) |
| P1-1 | Done | normalize_color_matrix/range in gpu_frame.py |
| P1-2 | Done | bit_depth always updates on GPU frame binding |
| P1-3 | Done | Verified hwaccel_download_packed_rgb correctly tagged |
| P1-4 | Done | Fallback tracked with performance monitor |
| P1-5 | Done | R16UI diagnostic path matches actual upload format |
| P2-1 | Done | Test dependencies checked/skipped |
| P2-2 | Done | Hot-path guard tests (9 tests) |
| P2-3 | Done | First-frame strategy improved for ProRes |
| P2-4 | Done | 4K packed RGB upload warning added |
| P3-1 | Verified | Shared frame provider protocol parity checked |
| P3-2 | Done | get_pipeline_report() method added |
| P4-1 | Done | ProRes proxy policy log added |
| P4-2 | Done | This status document |

