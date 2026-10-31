# DaVinci-Style GPU Video Playback Follow-up Plan

Date: 2026-05-30
Scope: Color page playback, 4K H.264/HEVC, 4K ProRes, macOS GPU YUV, AsyncRenderer, VideoPlaybackEngine, DecodeWorker.

Target chain:

```text
GPU decode -> GPU texture / VRAM -> GPU grading -> GPU output
```

Hot-path rules:

- No CPU YUV -> RGB in the playback hot path.
- No per-frame GPU readback.
- No per-frame `glFinish` / `ctx.finish`.
- No per-frame CPU `Texture.write` for true zero-copy frames.

## Current Review Summary

The implementation is materially better than the previous baseline: it now has `GpuDecodedFrame`, macOS VT / IOSurface bridge pieces, zero-copy frame path reporting, UI preferences, and static hot-path tests. I also fixed two small issues during this review:

1. `AsyncRenderer._do_render_video_gpu_frame()` now converts string color metadata (`bt709`, `bt2020nc`, `limited`, `full`) into integer YUV shader uniform codes before rendering external GPU planes.
2. `video_playback.py` no longer hard-fails import in lightweight / test environments when optional `cv2`, GStreamer `gi`, or `moderngl`-backed legacy modules are unavailable.

Validation run:

```bash
pytest -q tests/media/test_hot_path_guards.py \
  tests/media/test_macos_vt_zero_copy.py \
  tests/media/test_macos_vt_zero_copy_e2e.py \
  tests/media/test_video_playback_no_ui_copy.py \
  tests/media/test_v3_hwaccel_policy.py
```

Result: `101 passed`.

## Remaining Big Issues

| Priority | Area | Problem | Risk | Acceptance |
|---|---|---|---|---|
| P0 | Runtime proof | `get_zero_copy_capability()` can report available without a real decode + IOSurface + GL bind probe. | UI may claim zero-copy while runtime falls back or fails. | Auto mode requires a successful probe, or records `probe_not_run` and keeps zero-copy disabled until forced. |
| P0 | VT decode API | `VtSession.submit_packet()` is documented as async but calls `_decode_packet_impl()`, which waits for and consumes a decoded frame. | Future async users can submit a packet and then find `get_decoded_frame()` empty. | Split into `decode_packet_sync()` and `submit_packet_async()`; async submit must never drain the output queue. |
| P0 | IOSurface bridge | The fallback `_CGLTexImageIOSurface2D` loader is not used by `_bind_pixel_buffer_impl()`, which imports `OpenGL.CGL.CGLTexImageIOSurface2D` again. | Systems missing PyOpenGL's CGL binding still fail despite fallback code. | Bridge uses one resolved function path and logs whether PyOpenGL or ctypes was used. |
| P0 | Lifecycle | `GpuDecodedFrame.__del__()` can release GPU resources from an arbitrary GC thread, while comments say release must happen on the render thread. | Race with GL texture / IOSurface lifetime, possible crash or stale texture. | Destructors only log leaks; actual release is explicitly queued to render thread or handled by renderer-owned frame slots. |
| P1 | External texture release | `IOSurfaceGLBridge.release_previous_frame()` releases a bridge-wide previous buffer instead of binding release ownership to a specific ring-buffer slot/frame. | Newer frames can invalidate resources still being sampled by GPU. | Each ring slot owns textures + retained pixel buffer; release frees the exact slot after renderer advances/fence passes. |
| P1 | ProRes | ProRes is not in the macOS zero-copy supported codec set and still depends on CPU decoded planar paths. | 4K ProRes can still hit renderer-thread CPU YUV -> RGB fallback after GPU planar upload failures. | Decide target: VT ProRes IOSurface path or optimized CPU decode -> GPU planar upload path. Pipeline report must show the chosen path clearly. |
| P1 | Fallback policy | After 3 planar upload failures, renderer permanently disables planar upload, then CPU fallback can run every frame. | Playback keeps working but violates the target hot-path rule silently. | CPU fallback is explicit, counted, user-visible, and can trigger a source/pipeline fallback decision instead of hidden per-frame conversion. |
| P1 | P010 / 10-bit | P010 external texture path maps to normalized `GL_R16` / `GL_RG16`; shader bit-depth handling must be proven against known 10-bit patterns. | Wrong luma/chroma scaling or washed HDR/10-bit output. | Golden-frame tests for NV12 limited/full and P010 limited/full match CPU reference within tolerance. |
| P2 | Diagnostics | `get_pipeline_report()` aggregates some counters, but does not yet prove "no CPU copy this frame". | Debug UI can look healthy while hot-path counters miss a fallback. | Report includes last N frames with decode path, render path, CPU upload count, texture write count, readback count, finish count, fallback reason. |

## Execution Plan

| Step | Task | Files | Output |
|---|---|---|---|
| 1 | Make zero-copy capability gated by real probe in auto mode. | `src/shared/media/macos_vt/capability.py`, `src/shared/media/macos_vt/vt_zero_copy_probe.py` | Auto mode only enables zero-copy after decode + IOSurface + GL bind succeeds. |
| 2 | Refactor `VtSession` sync/async APIs. | `src/shared/media/macos_vt/vt_session.py`, `src/shared/media/macos_vt/source.py` | No API method both submits and drains unless named sync. |
| 3 | Fix IOSurface GL bridge function resolution and CGL fallback. | `src/shared/media/macos_vt/iosurface_gl_bridge.py` | One tested bind function, with explicit fallback reason on failure. |
| 4 | Move GPU frame release ownership to renderer/ring slots. | `src/shared/media/gpu_frame.py`, `src/features/color/core/async_renderer.py`, `src/shared/media/macos_vt/iosurface_gl_bridge.py` | No GL/IOSurface release from GC thread. |
| 5 | Add real-media E2E harness. | `tests/media/`, `scripts/` or `tools/` | H.264 4K, HEVC 4K, P010 10-bit, ProRes clips produce machine-readable pipeline reports. |
| 6 | Add golden-frame color validation. | `tests/media/`, `src/shared/video/yuv_gpu_upload.py`, shaders | NV12/P010 output validated against CPU reference. |
| 7 | Make CPU fallback impossible to miss. | `src/features/color/core/async_renderer.py`, `src/shared/media/performance_monitor.py`, UI diagnostics | Fallbacks are visible in report/UI and fail strict test mode. |
| 8 | Decide and implement ProRes strategy. | `src/shared/media/factory.py`, `src/shared/media/macos_vt/*`, DecodeWorker / SourceManager files | ProRes report clearly shows target path and measured cost. |

## Suggested Strict CI Checks

1. Static guard: `RENDER_VIDEO_GPU_FRAME` contains no `.write(`, `to_ndarray`, `transfer`, `ctx.finish`, `glFinish`.
2. Runtime guard: strict zero-copy fixture fails if any zero-copy frame records `cpu_upload=True`, `texture_write=True`, `readback_count > 0`, or `finish_count` increases per frame.
3. Capability guard: auto mode fails closed if probe was not run or probe did not prove IOSurface-backed external texture binding.
4. Color guard: NV12/P010 limited/full golden frames stay within tolerance against CPU reference.

## Definition Of Done

The work should be considered complete only when a real 4K H.264/HEVC clip can produce a report like:

```json
{
  "decode_path": "macos_vt_iosurface_zero_copy",
  "render_frame_path": "gpu_decoded_external_yuv",
  "zero_copy": true,
  "cpu_upload_frames": 0,
  "texture_write_frames": 0,
  "readback_frames": 0,
  "per_frame_finish_calls": 0,
  "fallback_reason": ""
}
```

For 4K ProRes, the report must either show a proven GPU-resident path or explicitly identify the accepted non-zero-copy path with measured frame times and no hidden UI-thread conversion.
