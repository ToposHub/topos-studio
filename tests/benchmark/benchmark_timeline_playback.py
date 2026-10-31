#!/usr/bin/env python3
"""
Timeline Video Playback Performance Benchmark

测试时间线视频播放链路各环节的帧速率和延迟:
1. 纯解码性能 (FFmpeg MediaSource)
2. VideoFrameProviderService 帧请求 (含缓存)
3. CPU 合成路径 (BGR → QImage)
4. GPU 解码路径 (EditGpuDecodeManager)
5. 端到端模拟播放帧率

Usage:
    .venv/bin/python tests/benchmark_timeline_playback.py
"""

import os
import sys
import time
import statistics
from pathlib import Path
from typing import Optional, Dict, List, Tuple

# Add project root to path
PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

# Test videos directory
TEST_VIDEOS_DIR = PROJECT_ROOT / "tests" / "test_videos"


def find_test_videos() -> Dict[str, str]:
    """Find available test videos."""
    videos = {}
    if TEST_VIDEOS_DIR.exists():
        for f in sorted(TEST_VIDEOS_DIR.iterdir()):
            if f.suffix.lower() in ('.mp4', '.mov', '.avi', '.mkv') and not f.name.startswith('.'):
                videos[f.name] = str(f)
    return videos


def bench_raw_decode(path: str, num_frames: int = 100) -> Dict:
    """
    Benchmark 1: Raw FFmpeg decode performance.
    Measures how fast frames can be decoded without any rendering.
    """
    print(f"\n{'='*70}")
    print(f"  Benchmark 1: Raw FFmpeg Decode")
    print(f"  File: {os.path.basename(path)}")
    print(f"{'='*70}")

    from src.shared.media.ffmpeg_source import FFmpegMediaSource

    source = FFmpegMediaSource(path)
    source.open()

    metadata = source.probe()
    print(f"  Resolution: {metadata.width}x{metadata.height}")
    print(f"  FPS: {metadata.fps.to_fps():.2f}")
    print(f"  Codec: {metadata.codec_name}")
    print(f"  Total frames: {metadata.total_frames}")
    print(f"  Pixel format: {getattr(metadata, 'pixel_format', 'unknown')}")

    warmup_count = min(3, metadata.total_frames)
    actual_frames = min(num_frames, max(0, metadata.total_frames - warmup_count))
    times = []

    # Warmup
    for i in range(warmup_count):
        source.read_frame(i)

    # Benchmark sequential decode
    start_total = time.perf_counter()
    for i in range(warmup_count, warmup_count + actual_frames):
        t0 = time.perf_counter()
        frame = source.read_frame(i)
        t1 = time.perf_counter()
        if frame is not None:
            times.append((t1 - t0) * 1000)

    total_time = time.perf_counter() - start_total

    source.close()

    if not times:
        return {"error": "No frames decoded"}

    fps = len(times) / (total_time or 1e-9)
    result = {
        "frames_decoded": len(times),
        "total_time_s": total_time,
        "fps": fps,
        "avg_ms": statistics.mean(times),
        "p50_ms": statistics.median(times),
        "p95_ms": sorted(times)[int(len(times) * 0.95)] if len(times) > 5 else max(times),
        "p99_ms": sorted(times)[int(len(times) * 0.99)] if len(times) > 5 else max(times),
        "min_ms": min(times),
        "max_ms": max(times),
    }

    print(f"\n  Results ({len(times)} frames):")
    print(f"    FPS:          {result['fps']:.1f}")
    print(f"    Avg decode:   {result['avg_ms']:.2f} ms")
    print(f"    P50 decode:   {result['p50_ms']:.2f} ms")
    print(f"    P95 decode:   {result['p95_ms']:.2f} ms")
    print(f"    P99 decode:   {result['p99_ms']:.2f} ms")
    print(f"    Min/Max:      {result['min_ms']:.2f} / {result['max_ms']:.2f} ms")

    return result


def bench_planar_decode(path: str, num_frames: int = 100) -> Dict:
    """
    Benchmark 1b: Planar YUV decode (for ProRes/DNxHR).
    Measures planar decode preserving YUV planes without CPU YUV→RGB conversion.
    """
    print(f"\n{'='*70}")
    print(f"  Benchmark 1b: Planar YUV Decode")
    print(f"  File: {os.path.basename(path)}")
    print(f"{'='*70}")

    from src.shared.media.ffmpeg_source import FFmpegMediaSource

    source = FFmpegMediaSource(path)
    source.open()

    metadata = source.probe()
    print(f"  Resolution: {metadata.width}x{metadata.height}")
    print(f"  Codec: {metadata.codec_name}")

    warmup_count = min(3, metadata.total_frames)
    actual_frames = min(num_frames, max(0, metadata.total_frames - warmup_count))
    times = []

    # Warmup
    if hasattr(source, 'read_frame_planar'):
        for i in range(warmup_count):
            source.read_frame_planar(i)
    else:
        print("  ⚠ read_frame_planar not available, skipping")
        source.close()
        return {"error": "planar not available"}

    # Benchmark
    start_total = time.perf_counter()
    for i in range(warmup_count, warmup_count + actual_frames):
        t0 = time.perf_counter()
        frame = source.read_frame_planar(i)
        t1 = time.perf_counter()
        if frame is not None:
            times.append((t1 - t0) * 1000)

    total_time = time.perf_counter() - start_total
    source.close()

    if not times:
        return {"error": "No frames decoded"}

    fps = len(times) / (total_time or 1e-9)
    result = {
        "frames_decoded": len(times),
        "total_time_s": total_time,
        "fps": fps,
        "avg_ms": statistics.mean(times),
        "p50_ms": statistics.median(times),
        "p95_ms": sorted(times)[int(len(times) * 0.95)] if len(times) > 5 else max(times),
    }

    print(f"\n  Results ({len(times)} frames):")
    print(f"    FPS:          {result['fps']:.1f}")
    print(f"    Avg decode:   {result['avg_ms']:.2f} ms")
    print(f"    P50 decode:   {result['p50_ms']:.2f} ms")
    print(f"    P95 decode:   {result['p95_ms']:.2f} ms")

    return result


def bench_frame_provider_service(path: str, num_frames: int = 100) -> Dict:
    """
    Benchmark 2: VideoFrameProviderService frame request (with caching).
    Measures the full service path including cache lookup and source selection.
    """
    print(f"\n{'='*70}")
    print(f"  Benchmark 2: VideoFrameProviderService (proxy)")
    print(f"  File: {os.path.basename(path)}")
    print(f"{'='*70}")

    from src.shared.video.frame_provider_service import VideoFrameProviderService

    service = VideoFrameProviderService()
    clip_id = service.open_clip(path)

    # Get clip info
    info = service.get_clip_info(clip_id)
    if info:
        print(f"  Resolution: {info.width}x{info.height}")
        print(f"  FPS: {info.fps:.2f}")
        print(f"  Codec: {info.codec_name}")

    actual_frames = min(num_frames, info.total_frames if info else num_frames)
    times = []
    cache_times = []
    miss_times = []

    # Benchmark frame requests (first pass = cache miss, second pass = cache hit)
    start_total = time.perf_counter()

    # First pass: all cache misses
    for i in range(actual_frames):
        t0 = time.perf_counter()
        frame = service.request_frame(clip_id, i, quality='proxy')
        t1 = time.perf_counter()
        if frame is not None:
            times.append((t1 - t0) * 1000)
            miss_times.append((t1 - t0) * 1000)

    first_pass_time = time.perf_counter() - start_total

    # Second pass: all cache hits
    hit_times = []
    start_total = time.perf_counter()
    for i in range(actual_frames):
        t0 = time.perf_counter()
        frame = service.request_frame(clip_id, i, quality='proxy')
        t1 = time.perf_counter()
        if frame is not None:
            hit_times.append((t1 - t0) * 1000)

    second_pass_time = time.perf_counter() - start_total

    # Stats
    stats = service.get_stats()
    print(f"\n  Cache miss path ({len(miss_times)} frames):")
    if miss_times:
        miss_fps = len(miss_times) / (first_pass_time or 1e-9)
        print(f"    FPS:          {miss_fps:.1f}")
        print(f"    Avg:          {statistics.mean(miss_times):.2f} ms")
        print(f"    P50:          {statistics.median(miss_times):.2f} ms")
        print(f"    P95:          {sorted(miss_times)[int(len(miss_times)*0.95)]:.2f} ms")

    print(f"\n  Cache hit path ({len(hit_times)} frames):")
    if hit_times:
        hit_fps = len(hit_times) / (second_pass_time or 1e-9)
        print(f"    FPS:          {hit_fps:.1f}")
        print(f"    Avg:          {statistics.mean(hit_times):.2f} ms")
        print(f"    P50:          {statistics.median(hit_times):.2f} ms")

    print(f"\n  Service stats:")
    print(f"    Cache hit rate: {stats['global']['cache_hit_rate']:.1%}")
    print(f"    Total served:   {stats['global']['total_frames_served']}")

    service.close_clip(clip_id)
    service.shutdown()

    return {
        "miss_fps": len(miss_times) / (first_pass_time or 1e-9) if miss_times else 0,
        "hit_fps": len(hit_times) / (second_pass_time or 1e-9) if hit_times else 0,
        "miss_avg_ms": statistics.mean(miss_times) if miss_times else 0,
        "hit_avg_ms": statistics.mean(hit_times) if hit_times else 0,
    }


def bench_cpu_composite(path: str, num_frames: int = 50) -> Dict:
    """
    Benchmark 3: CPU composite path (BGR extraction + resize + QImage conversion).
    This simulates the PreviewRenderWorker CPU fallback path.
    """
    print(f"\n{'='*70}")
    print(f"  Benchmark 3: CPU Composite Path (decode → BGR extract → resize → QImage)")
    print(f"  File: {os.path.basename(path)}")
    print(f"{'='*70}")

    import cv2
    import numpy as np
    from PyQt6.QtGui import QImage
    from src.shared.video.frame_provider_service import VideoFrameProviderService

    app = None
    if not hasattr(bench_cpu_composite, '_app'):
        from PyQt6.QtWidgets import QApplication
        bench_cpu_composite._app = QApplication.instance() or QApplication(sys.argv)
    app = bench_cpu_composite._app

    service = VideoFrameProviderService()
    clip_id = service.open_clip(path)
    info = service.get_clip_info(clip_id)
    viewport_size = (1920, 1080)

    actual_frames = min(num_frames, info.total_frames if info else num_frames)

    decode_times = []
    extract_times = []
    resize_times = []
    qimage_times = []
    total_times = []

    for i in range(actual_frames):
        t_total_start = time.perf_counter()

        # Step 1: Decode frame
        t0 = time.perf_counter()
        decoded_frame = service.request_frame(clip_id, i, quality='proxy')
        t1 = time.perf_counter()
        decode_ms = (t1 - t0) * 1000

        if decoded_frame is None:
            continue

        # Step 2: Extract BGR data
        t0 = time.perf_counter()
        data = getattr(decoded_frame, 'data', None)
        if data is None:
            continue

        pixel_format = getattr(decoded_frame, 'pixel_format', 'bgra')
        if pixel_format in ('bgr24', 'bgra'):
            bgr_data = data[..., :3] if data.shape[-1] == 4 else data
        elif pixel_format in ('rgb24', 'rgba'):
            bgr_data = np.flip(data[..., :3], axis=-1)
        else:
            bgr_data = data[..., :3] if data.ndim == 3 and data.shape[-1] >= 3 else data

        t1 = time.perf_counter()
        extract_ms = (t1 - t0) * 1000

        # Step 3: Resize to viewport
        t0 = time.perf_counter()
        h, w = bgr_data.shape[:2]
        if h != viewport_size[1] or w != viewport_size[0]:
            scale = min(viewport_size[0] / w, viewport_size[1] / h)
            new_w = int(w * scale)
            new_h = int(h * scale)
            bgr_data = cv2.resize(bgr_data, (new_w, new_h), interpolation=cv2.INTER_LINEAR)
        t1 = time.perf_counter()
        resize_ms = (t1 - t0) * 1000

        # Step 4: Convert to QImage
        t0 = time.perf_counter()
        bgr_contiguous = np.ascontiguousarray(bgr_data)
        qimg = QImage(
            bgr_contiguous.data,
            bgr_contiguous.shape[1],
            bgr_contiguous.shape[0],
            bgr_contiguous.shape[1] * 3,
            QImage.Format.Format_BGR888
        )
        qimg = qimg.copy()  # Must copy to avoid dangling reference
        t1 = time.perf_counter()
        qimage_ms = (t1 - t0) * 1000

        t_total_end = time.perf_counter()
        total_ms = (t_total_end - t_total_start) * 1000

        decode_times.append(decode_ms)
        extract_times.append(extract_ms)
        resize_times.append(resize_ms)
        qimage_times.append(qimage_ms)
        total_times.append(total_ms)

    service.close_clip(clip_id)
    service.shutdown()

    if not total_times:
        return {"error": "No frames processed"}

    result = {
        "frames": len(total_times),
        "fps": len(total_times) / (sum(total_times) / 1000) if total_times else 0,
        "total_avg_ms": statistics.mean(total_times),
        "decode_avg_ms": statistics.mean(decode_times) if decode_times else 0,
        "extract_avg_ms": statistics.mean(extract_times) if extract_times else 0,
        "resize_avg_ms": statistics.mean(resize_times) if resize_times else 0,
        "qimage_avg_ms": statistics.mean(qimage_times) if qimage_times else 0,
    }

    print(f"\n  Results ({result['frames']} frames):")
    print(f"    Total FPS:       {result['fps']:.1f}")
    print(f"    Total avg:       {result['total_avg_ms']:.2f} ms")
    print(f"    Decode avg:      {result['decode_avg_ms']:.2f} ms ({result['decode_avg_ms']/result['total_avg_ms']*100:.0f}%)")
    print(f"    BGR extract:     {result['extract_avg_ms']:.2f} ms ({result['extract_avg_ms']/result['total_avg_ms']*100:.0f}%)")
    print(f"    Resize avg:      {result['resize_avg_ms']:.2f} ms ({result['resize_avg_ms']/result['total_avg_ms']*100:.0f}%)")
    print(f"    QImage conv:     {result['qimage_avg_ms']:.2f} ms ({result['qimage_avg_ms']/result['total_avg_ms']*100:.0f}%)")

    return result


def bench_gpu_decode_manager(path: str, num_frames: int = 100) -> Dict:
    """
    Benchmark 4: EditGpuDecodeManager (GPU zero-copy / planar path).
    """
    print(f"\n{'='*70}")
    print(f"  Benchmark 4: EditGpuDecodeManager (GPU/Planar path)")
    print(f"  File: {os.path.basename(path)}")
    print(f"{'='*70}")

    from src.features.edit.core.edit_gpu_decode_manager import EditGpuDecodeManager

    manager = EditGpuDecodeManager()
    target_size = (1920, 1080)

    # First frame to detect source type
    frame0, frame_type0 = manager.get_frame(path, 0, target_size=target_size)
    print(f"  Detected frame type: {frame_type0}")
    if frame0 is not None:
        w = getattr(frame0, 'width', 0) or (frame0.data.shape[1] if hasattr(frame0, 'data') else 0)
        h = getattr(frame0, 'height', 0) or (frame0.data.shape[0] if hasattr(frame0, 'data') else 0)
        print(f"  Frame size: {w}x{h}")

    metadata = getattr(frame0, 'metadata', None)
    source_total_frames = int(getattr(metadata, 'total_frames', 0) or 0)
    actual_frames = min(num_frames, max(0, source_total_frames - 1)) if source_total_frames else num_frames

    times = []
    frame_types = {}

    start_total = time.perf_counter()
    # The source is now positioned after frame 0. Keep the timed pass
    # sequential; seeking back to zero measures scrub latency, not playback.
    for i in range(1, actual_frames + 1):
        t0 = time.perf_counter()
        frame, frame_type = manager.get_frame(path, i, target_size=target_size)
        t1 = time.perf_counter()

        if frame is not None:
            times.append((t1 - t0) * 1000)
            frame_types[frame_type] = frame_types.get(frame_type, 0) + 1

    total_time = time.perf_counter() - start_total

    manager.close_all()

    if not times:
        return {"error": "No frames decoded"}

    fps = len(times) / (total_time or 1e-9)
    result = {
        "frames": len(times),
        "fps": fps,
        "avg_ms": statistics.mean(times),
        "p50_ms": statistics.median(times),
        "p95_ms": sorted(times)[int(len(times) * 0.95)] if len(times) > 5 else max(times),
        "frame_types": frame_types,
    }

    print(f"\n  Results ({len(times)} frames):")
    print(f"    FPS:          {result['fps']:.1f}")
    print(f"    Avg decode:   {result['avg_ms']:.2f} ms")
    print(f"    P50 decode:   {result['p50_ms']:.2f} ms")
    print(f"    P95 decode:   {result['p95_ms']:.2f} ms")
    print(f"    Frame types:  {frame_types}")

    return result


def bench_sequential_vs_seek(path: str, num_frames: int = 100) -> Dict:
    """
    Benchmark 5: Sequential vs Seek decode performance.
    Sequential decode is critical for smooth playback.
    """
    print(f"\n{'='*70}")
    print(f"  Benchmark 5: Sequential vs Random-Seek Decode")
    print(f"  File: {os.path.basename(path)}")
    print(f"{'='*70}")

    from src.shared.media.ffmpeg_source import FFmpegMediaSource
    import random

    source = FFmpegMediaSource(path)
    source.open()

    metadata = source.probe()
    total = metadata.total_frames
    actual_frames = min(num_frames, total)

    # Sequential
    seq_times = []
    t_start = time.perf_counter()
    for i in range(actual_frames):
        t0 = time.perf_counter()
        frame = source.read_frame(i)
        t1 = time.perf_counter()
        if frame is not None:
            seq_times.append((t1 - t0) * 1000)
    seq_total = time.perf_counter() - t_start

    # Random seek
    random_indices = random.sample(range(0, total), min(actual_frames, total))
    seek_times = []
    t_start = time.perf_counter()
    for idx in random_indices:
        t0 = time.perf_counter()
        frame = source.read_frame(idx)
        t1 = time.perf_counter()
        if frame is not None:
            seek_times.append((t1 - t0) * 1000)
    seek_total = time.perf_counter() - t_start

    source.close()

    seq_fps = len(seq_times) / (seq_total or 1e-9) if seq_times else 0
    seek_fps = len(seek_times) / (seek_total or 1e-9) if seek_times else 0

    print(f"\n  Sequential ({len(seq_times)} frames):")
    print(f"    FPS: {seq_fps:.1f}, Avg: {statistics.mean(seq_times):.2f} ms, P95: {sorted(seq_times)[int(len(seq_times)*0.95)]:.2f} ms")

    print(f"\n  Random Seek ({len(seek_times)} frames):")
    print(f"    FPS: {seek_fps:.1f}, Avg: {statistics.mean(seek_times):.2f} ms, P95: {sorted(seek_times)[int(len(seek_times)*0.95)]:.2f} ms")

    ratio = statistics.mean(seek_times) / statistics.mean(seq_times) if seq_times and statistics.mean(seq_times) > 0 else 0
    print(f"\n  Seek penalty: {ratio:.1f}x slower than sequential")

    return {
        "seq_fps": seq_fps,
        "seek_fps": seek_fps,
        "seq_avg_ms": statistics.mean(seq_times) if seq_times else 0,
        "seek_avg_ms": statistics.mean(seek_times) if seek_times else 0,
        "seek_penalty": ratio,
    }


def main():
    print("╔══════════════════════════════════════════════════════════════════════╗")
    print("║     Timeline Video Playback Performance Benchmark                   ║")
    print("╚══════════════════════════════════════════════════════════════════════╝")

    # Find test videos
    videos = find_test_videos()
    if not videos:
        print("ERROR: No test videos found in tests/test_videos/")
        sys.exit(1)

    print(f"\nAvailable test videos:")
    for name, path in videos.items():
        size_mb = os.path.getsize(path) / (1024 * 1024)
        print(f"  - {name} ({size_mb:.1f} MB)")

    # Target FPS for real-time playback
    TARGET_FPS = 24.0
    FRAME_BUDGET_MS = 1000.0 / TARGET_FPS

    print(f"\n  Target: {TARGET_FPS} fps = {FRAME_BUDGET_MS:.1f} ms/frame budget")

    all_results = {}

    # Test a selection of codecs
    test_files = []
    # Prefer smaller files for faster testing
    preferred = ['2K_H265.mov', '2K_h264.mp4', '4K_H264.mov', '4K_H265.mov']
    for name in preferred:
        if name in videos:
            test_files.append((name, videos[name]))

    # Add ProRes if available
    for name in ['2K_prores422HQ.mov']:
        if name in videos:
            test_files.append((name, videos[name]))

    # Add DNxHR if available
    for name in ['2K_DNxHR HQX 12-bit.mov']:
        if name in videos:
            test_files.append((name, videos[name]))

    for name, path in test_files:
        print(f"\n\n{'#'*70}")
        print(f"#  Testing: {name}")
        print(f"{'#'*70}")

        file_results = {}

        # Benchmark 1: Raw decode
        try:
            file_results['raw_decode'] = bench_raw_decode(path, num_frames=60)
        except Exception as e:
            print(f"  ERROR: {e}")
            file_results['raw_decode'] = {"error": str(e)}

        # Benchmark 1b: Planar decode (for ProRes/DNxHR)
        try:
            file_results['planar_decode'] = bench_planar_decode(path, num_frames=60)
        except Exception as e:
            print(f"  ERROR: {e}")
            file_results['planar_decode'] = {"error": str(e)}

        # Benchmark 5: Sequential vs Seek
        try:
            file_results['seq_vs_seek'] = bench_sequential_vs_seek(path, num_frames=60)
        except Exception as e:
            print(f"  ERROR: {e}")
            file_results['seq_vs_seek'] = {"error": str(e)}

        # Benchmark 2: Frame provider service
        try:
            file_results['frame_provider'] = bench_frame_provider_service(path, num_frames=60)
        except Exception as e:
            print(f"  ERROR: {e}")
            file_results['frame_provider'] = {"error": str(e)}

        # Benchmark 4: GPU decode manager
        try:
            file_results['gpu_decode'] = bench_gpu_decode_manager(path, num_frames=60)
        except Exception as e:
            print(f"  ERROR: {e}")
            file_results['gpu_decode'] = {"error": str(e)}

        all_results[name] = file_results

    # Benchmark 3: CPU composite (only for first file, requires QApplication)
    if test_files:
        name, path = test_files[0]
        try:
            all_results[name]['cpu_composite'] = bench_cpu_composite(path, num_frames=30)
        except Exception as e:
            print(f"  ERROR in CPU composite bench: {e}")

    # ================================================================
    # Summary Report
    # ================================================================
    print(f"\n\n{'='*70}")
    print(f"  PERFORMANCE SUMMARY")
    print(f"  Target: {TARGET_FPS} fps ({FRAME_BUDGET_MS:.1f} ms/frame)")
    print(f"{'='*70}")

    for name, results in all_results.items():
        print(f"\n  📹 {name}")
        print(f"  {'─'*50}")

        # Raw decode
        rd = results.get('raw_decode', {})
        if 'fps' in rd:
            status = "✅ PASS" if rd['fps'] >= TARGET_FPS else "❌ FAIL"
            print(f"  Raw Decode:      {rd['fps']:7.1f} fps  (avg {rd['avg_ms']:.1f}ms, P95 {rd['p95_ms']:.1f}ms) {status}")

        # Planar decode
        pd = results.get('planar_decode', {})
        if 'fps' in pd:
            status = "✅ PASS" if pd['fps'] >= TARGET_FPS else "❌ FAIL"
            print(f"  Planar Decode:   {pd['fps']:7.1f} fps  (avg {pd['avg_ms']:.1f}ms) {status}")
        elif 'error' not in pd:
            pass  # not applicable

        # GPU decode
        gd = results.get('gpu_decode', {})
        if 'fps' in gd:
            ft = gd.get('frame_types', {})
            ft_str = ', '.join(f"{k}={v}" for k, v in ft.items())
            status = "✅ PASS" if gd['fps'] >= TARGET_FPS else "❌ FAIL"
            print(f"  GPU Decode:      {gd['fps']:7.1f} fps  (avg {gd['avg_ms']:.1f}ms, types: {ft_str}) {status}")

        # Frame provider (cache miss)
        fp = results.get('frame_provider', {})
        if 'miss_fps' in fp:
            status = "✅ PASS" if fp['miss_fps'] >= TARGET_FPS else "❌ FAIL"
            print(f"  FrameProvider (miss): {fp['miss_fps']:5.1f} fps  (avg {fp['miss_avg_ms']:.1f}ms) {status}")
            status = "✅ PASS" if fp['hit_fps'] >= TARGET_FPS * 10 else "⚠️ SLOW"
            print(f"  FrameProvider (hit):  {fp['hit_fps']:5.1f} fps  (avg {fp['hit_avg_ms']:.2f}ms) {status}")

        # Seek penalty
        ss = results.get('seq_vs_seek', {})
        if 'seek_penalty' in ss:
            print(f"  Seek penalty:    {ss['seek_penalty']:.1f}x  (seq {ss['seq_fps']:.0f}fps → seek {ss['seek_fps']:.0f}fps)")

        # CPU composite
        cc = results.get('cpu_composite', {})
        if 'fps' in cc:
            status = "✅ PASS" if cc['fps'] >= TARGET_FPS else "❌ FAIL"
            print(f"  CPU Composite:   {cc['fps']:7.1f} fps  (total {cc['total_avg_ms']:.1f}ms)")
            print(f"    Breakdown: decode={cc['decode_avg_ms']:.1f}ms, extract={cc['extract_avg_ms']:.1f}ms, resize={cc['resize_avg_ms']:.1f}ms, qimage={cc['qimage_avg_ms']:.1f}ms")

    print(f"\n{'='*70}")
    print("  Done.")
    print(f"{'='*70}")


if __name__ == '__main__':
    main()
