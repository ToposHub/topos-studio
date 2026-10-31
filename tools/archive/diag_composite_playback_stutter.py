"""Diagnose Composite-page playback stutter: replicate TimelineClipFrameProvider hot path.

The Composite page's TimelineClipFrameProvider (with
allow_external_gpu_frames=False, as wired by composite_controller) fetches
every playback frame via:

    service.request_frame(clip_id, source_frame, quality='proxy')   # blocking CPU decode
    + numpy flipud/ascontiguousarray prep (CPU)                     # before GPU upload

This tool replays that exact sequence at source FPS and reports per-frame
timings so we can see whether the decode+prep alone already misses the
frame budget (the Edit page avoids this via zero-copy + async decode).

Usage:
    .venv/bin/python tools/diag_composite_playback_stutter.py [video_path] [num_frames]
"""

from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np  # noqa: E402

from src.shared.video.frame_provider_service import get_frame_provider_service  # noqa: E402


def _prep_cost(frame) -> tuple[float, str]:
    """Replicate the provider's CPU prep before texture upload."""
    t0 = time.perf_counter()
    desc = "none"
    planes = getattr(frame, "planes", None)
    data = getattr(frame, "data", None)
    if data is None:
        data = getattr(frame, "image", None)
    if planes:
        # _prepare_planar_upload_planes: flipud+ascontiguous per plane
        for p in planes:
            np.ascontiguousarray(np.flipud(np.asarray(p)))
        desc = f"planar({len(planes)})"
    elif isinstance(data, np.ndarray):
        # _prepare_packed_frame_upload: channel slice + flipud + ascontiguous
        arr = data[..., : data.shape[-1]]
        np.ascontiguousarray(np.flipud(arr))
        desc = f"packed({data.dtype})"
    return (time.perf_counter() - t0) * 1000.0, desc


def main() -> int:
    video = sys.argv[1] if len(sys.argv) > 1 else "tests/test_videos/2K_h264.mp4"
    n_frames = int(sys.argv[2]) if len(sys.argv) > 2 else 120
    video = os.path.abspath(video)
    if not os.path.exists(video):
        print(f"video not found: {video}")
        return 1

    service = get_frame_provider_service()

    # zero-copy availability (what the Edit page uses)
    try:
        from src.shared.media.macos_vt import is_zero_copy_available
        print(f"zero_copy_available: {is_zero_copy_available()}")
    except Exception as exc:
        print(f"zero_copy probe failed: {exc}")

    t0 = time.perf_counter()
    clip_id = service.open_clip(video)
    print(f"open_clip: {(time.perf_counter() - t0) * 1000:.1f} ms -> {clip_id}")

    info = service.get_clip_info(clip_id)
    if info is not None:
        print(
            f"clip info: {getattr(info, 'width', '?')}x{getattr(info, 'height', '?')} "
            f"fps={getattr(info, 'fps', '?')} frames={getattr(info, 'total_frames', '?')} "
            f"pix_fmt={getattr(info, 'pixel_format', '?')} "
            f"transfer={getattr(info, 'color_transfer', '?')} "
            f"matrix={getattr(info, 'color_matrix', '?')} "
            f"range={getattr(info, 'color_range', '?')}"
        )
    session = service._clips.get(clip_id)
    if session is not None:
        print(f"session has _vt_source: {hasattr(session, '_vt_source')}")

    fps = float(getattr(info, "fps", 24.0) or 24.0)
    budget_ms = 1000.0 / fps

    # Warm-up seek to skip container-open latency on frame 0.
    service.request_frame(clip_id, 0, quality="proxy")

    decode_ms_list: list[float] = []
    prep_ms_list: list[float] = []
    fmt_seen: dict[str, int] = {}
    over_budget: list[tuple[int, float]] = []

    print(f"\nReplaying {n_frames} sequential frames (provider path: quality='proxy', "
          f"allow_master_fallback=True), budget={budget_ms:.1f}ms @{fps:g}fps")
    for i in range(n_frames):
        t_dec0 = time.perf_counter()
        frame = service.request_frame(clip_id, i, quality="proxy")
        dec_ms = (time.perf_counter() - t_dec0) * 1000.0
        if frame is None:
            print(f"  frame {i}: None!")
            continue
        fmt = str(getattr(frame, "pixel_format", "?"))
        fmt_seen[fmt] = fmt_seen.get(fmt, 0) + 1
        prep_ms, prep_desc = _prep_cost(frame)
        decode_ms_list.append(dec_ms)
        prep_ms_list.append(prep_ms)
        total = dec_ms + prep_ms
        if total > budget_ms:
            over_budget.append((i, total))
            print(f"  frame {i:5d}: decode={dec_ms:7.1f}ms prep={prep_ms:5.1f}ms "
                  f"({prep_desc}, fmt={fmt})  <-- OVER BUDGET")

    def stats(name: str, xs: list[float]) -> None:
        if not xs:
            print(f"{name}: no samples")
            return
        a = np.array(xs)
        print(f"{name}: n={len(xs)} mean={a.mean():.1f} p50={np.percentile(a, 50):.1f} "
              f"p95={np.percentile(a, 95):.1f} max={a.max():.1f} ms")

    print()
    stats("decode", decode_ms_list)
    stats("cpu-prep", prep_ms_list)
    total_arr = np.array(decode_ms_list) + np.array(prep_ms_list[: len(decode_ms_list)])
    if len(total_arr):
        effective_fps = 1000.0 / float(total_arr.mean())
        print(f"decode+prep total: mean={total_arr.mean():.1f} p95={np.percentile(total_arr, 95):.1f} "
              f"max={total_arr.max():.1f} ms -> effective {effective_fps:.1f} fps "
              f"(target {fps:g} fps)")
    print(f"pixel formats seen: {fmt_seen}")
    print(f"frames over budget: {len(over_budget)}/{len(decode_ms_list)}")

    service.close_clip(clip_id)
    return 0


if __name__ == "__main__":
    sys.exit(main())
