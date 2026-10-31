#!/usr/bin/env python3
"""
视频解码速度基准测试。

用法:
    .venv/bin/python tests/benchmark_decode_speed.py
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

TEST_VIDEOS_DIR = PROJECT_ROOT / "tests" / "test_videos"

WARMUP_FRAMES = 5
BENCH_FRAMES = 100


def get_test_videos() -> dict[str, Path]:
    videos = {}
    if TEST_VIDEOS_DIR.exists():
        for f in TEST_VIDEOS_DIR.iterdir():
            if f.suffix.lower() in ('.mp4', '.mov') and not f.name.startswith('.'):
                videos[f.name] = f
    return videos


def get_meta(source):
    """Get metadata from source (works before or after open)."""
    if source._metadata is not None:
        return source._metadata
    return source.probe()


def benchmark_sw_decode(video_path: str, num_frames: int = BENCH_FRAMES) -> dict:
    from src.shared.media.ffmpeg_source import FFmpegMediaSource
    source = FFmpegMediaSource(path=video_path)
    source.open()
    meta = get_meta(source)
    total = meta.total_frames or 300
    for i in range(WARMUP_FRAMES):
        source.read_frame(i % total)
    t0 = time.perf_counter()
    # 从预热结束位置继续，避免首个正式样本触发回退 seek 污染计时
    for i in range(WARMUP_FRAMES, WARMUP_FRAMES + num_frames):
        source.read_frame(i % total)
    elapsed = time.perf_counter() - t0
    source.close()
    return {"fps": num_frames / elapsed, "ms_per_frame": elapsed / num_frames * 1000,
            "resolution": f"{meta.width}x{meta.height}", "codec": meta.codec_name}


def benchmark_planar_noscale(video_path: str, num_frames: int = BENCH_FRAMES) -> dict:
    from src.shared.media.ffmpeg_source import FFmpegMediaSource
    source = FFmpegMediaSource(path=video_path, preserve_planes=True)
    source.open()
    meta = get_meta(source)
    total = meta.total_frames or 300
    for i in range(WARMUP_FRAMES):
        source.read_frame_planar(i % total)
    t0 = time.perf_counter()
    # 从预热结束位置继续，避免首个正式样本触发回退 seek 污染计时
    for i in range(WARMUP_FRAMES, WARMUP_FRAMES + num_frames):
        source.read_frame_planar(i % total)
    elapsed = time.perf_counter() - t0
    source.close()
    return {"fps": num_frames / elapsed, "ms_per_frame": elapsed / num_frames * 1000}


def benchmark_planar_reformat(video_path: str, num_frames: int = BENCH_FRAMES) -> dict:
    from src.shared.media.ffmpeg_source import FFmpegMediaSource
    source = FFmpegMediaSource(path=video_path, target_size=(1920, 1080), preserve_planes=False)
    source.open()
    meta = get_meta(source)
    total = meta.total_frames or 300
    for i in range(WARMUP_FRAMES):
        source.read_frame_planar(i % total)
    t0 = time.perf_counter()
    # 从预热结束位置继续，避免首个正式样本触发回退 seek 污染计时
    for i in range(WARMUP_FRAMES, WARMUP_FRAMES + num_frames):
        source.read_frame_planar(i % total)
    elapsed = time.perf_counter() - t0
    source.close()
    return {"fps": num_frames / elapsed, "ms_per_frame": elapsed / num_frames * 1000}


def benchmark_hwaccel(video_path: str, num_frames: int = BENCH_FRAMES) -> dict | None:
    try:
        from src.shared.media.hwaccel import FFmpegHwaccelSource
    except ImportError:
        return None
    source = FFmpegHwaccelSource(path=video_path)
    source.open()  # 必须先 open 才能 read_frame
    meta = get_meta(source)
    total = meta.total_frames or 300
    for i in range(WARMUP_FRAMES):
        source.read_frame(i % total)
    t0 = time.perf_counter()
    # 从预热结束位置继续，避免首个正式样本触发回退 seek 污染计时
    for i in range(WARMUP_FRAMES, WARMUP_FRAMES + num_frames):
        source.read_frame(i % total)
    elapsed = time.perf_counter() - t0
    source.close()
    return {"fps": num_frames / elapsed, "ms_per_frame": elapsed / num_frames * 1000}


def benchmark_decode_manager(video_path: str, num_frames: int = BENCH_FRAMES) -> dict:
    from src.features.edit.core.edit_gpu_decode_manager import EditGpuDecodeManager
    mgr = EditGpuDecodeManager()
    for i in range(WARMUP_FRAMES):
        mgr.get_frame(video_path, i)

    # Sequential（从预热结束位置继续，避免回退 seek 污染首个正式样本）
    t0 = time.perf_counter()
    for i in range(WARMUP_FRAMES, WARMUP_FRAMES + num_frames):
        mgr.get_frame(video_path, i)
    elapsed_seq = time.perf_counter() - t0

    # Cached (loop)
    t0 = time.perf_counter()
    for i in range(num_frames):
        mgr.get_frame(video_path, i % 300)
    elapsed_cache = time.perf_counter() - t0

    # Random seek
    import random
    random.seed(42)
    indices = [random.randint(0, 299) for _ in range(num_frames)]
    t0 = time.perf_counter()
    for idx in indices:
        mgr.get_frame(video_path, idx)
    elapsed_seek = time.perf_counter() - t0

    mgr.close_all()
    return {
        "fps_seq": num_frames / elapsed_seq, "ms_seq": elapsed_seq / num_frames * 1000,
        "fps_cache": num_frames / elapsed_cache, "ms_cache": elapsed_cache / num_frames * 1000,
        "fps_seek": num_frames / elapsed_seek, "ms_seek": elapsed_seek / num_frames * 1000,
    }


def main():
    videos = get_test_videos()
    if not videos:
        print("❌ 没有找到测试视频！")
        return

    print("=" * 90)
    print("  视频解码速度基准测试")
    print("=" * 90)
    print()

    priority = [
        "2K_h264.mp4", "2K_prores422HQ.mov", "2K_H265.mov", "2K_DNxHR HQX 12-bit.mov",
        "4K_H264.mov", "4K_H265.mov", "4K_prores422HQ.mov", "4K_DNxHR HQX 12-bit.mov",
    ]

    results_noscale = {}
    results_reformat = {}

    for name in priority:
        if name not in videos:
            continue
        path = str(videos[name])
        print(f"{'─' * 90}")
        print(f"📹 {name}")
        print(f"{'─' * 90}")

        # 1. CPU SW packed RGB
        try:
            r = benchmark_sw_decode(path)
            print(f"  CPU SW (packed RGB)                 {r['fps']:>8.1f} fps  ({r['ms_per_frame']:.1f} ms)  [{r['resolution']} {r['codec']}]")
        except Exception as e:
            print(f"  CPU SW (packed RGB)                 ❌ {e}")

        # 2. Planar no scale (优化 2)
        try:
            r = benchmark_planar_noscale(path)
            results_noscale[name] = r
            print(f"  Planar YUV (GPU scale ✅)            {r['fps']:>8.1f} fps  ({r['ms_per_frame']:.1f} ms)")
        except Exception as e:
            print(f"  Planar YUV (GPU scale ✅)            ❌ {e}")

        # 3. Planar with CPU reformat (旧路径)
        try:
            r = benchmark_planar_reformat(path)
            results_reformat[name] = r
            print(f"  Planar YUV (CPU reformat 🐢)         {r['fps']:>8.1f} fps  ({r['ms_per_frame']:.1f} ms)  [→ 1920x1080]")
        except Exception as e:
            print(f"  Planar YUV (CPU reformat 🐢)         ❌ {e}")

        # 4. HWAccel
        try:
            r = benchmark_hwaccel(path)
            if r and 'error' not in r:
                print(f"  HWAccel VT (PyAV)                   {r['fps']:>8.1f} fps  ({r['ms_per_frame']:.1f} ms)")
            else:
                print(f"  HWAccel VT (PyAV)                   ⏭  不可用")
        except Exception as e:
            print(f"  HWAccel VT (PyAV)                   ❌ {e}")

        # 5. DecodeManager
        try:
            dm = benchmark_decode_manager(path)
            print(f"  DecodeManager sequential            {dm['fps_seq']:>8.1f} fps  ({dm['ms_seq']:.1f} ms)")
            print(f"  DecodeManager cached                {dm['fps_cache']:>8.1f} fps  ({dm['ms_cache']:.1f} ms)")
            print(f"  DecodeManager random seek           {dm['fps_seek']:>8.1f} fps  ({dm['ms_seek']:.1f} ms)")
        except Exception as e:
            print(f"  DecodeManager                       ❌ {e}")

        print()

    # Summary table
    if results_noscale:
        print("=" * 90)
        print("  📊 优化效果总结 — Planar GPU scale (✅) vs CPU reformat (🐢)")
        print("=" * 90)
        print()
        print(f"  {'Video':<35s} {'GPU scale':>10s} {'CPU refmt':>10s} {'Speedup':>8s} {'Saved':>8s}")
        print(f"  {'─'*35} {'─'*10} {'─'*10} {'─'*8} {'─'*8}")
        for name in priority:
            if name in results_noscale and name in results_reformat:
                n = results_noscale[name]
                r = results_reformat[name]
                speedup = n['fps'] / r['fps'] if r['fps'] > 0 else 0
                saved = r['ms_per_frame'] - n['ms_per_frame']
                print(f"  {name:<35s} {n['fps']:>8.1f}f  {r['fps']:>8.1f}f  {speedup:>6.2f}×  {saved:>6.1f}ms")
        print()


if __name__ == "__main__":
    main()
