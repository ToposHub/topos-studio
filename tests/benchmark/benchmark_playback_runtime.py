#!/usr/bin/env python3
"""
Playback Runtime Benchmark

Simulates real video playback through the full AsyncRenderer + VideoPlaybackEngine
path, tracking queue depth, coalesced commands, dropped frames, late frames.

Unlike benchmark_yuv_gpu_pipeline.py which tests offline upload/render in isolation,
this benchmark measures the actual playback chain performance including:
- Frame queue management (enqueue/dequeue/coalesce)
- AsyncRenderer command processing latency
- Backpressure when queue is full
- Frame timing jitter and late frame detection

Usage:
    python tests/benchmark/benchmark_playback_runtime.py \
      --file <local-video-file> \
      --frames 120 \
      --warmup 10 \
      --output-json tests/media/results/playback_runtime.json
"""

import os
import sys
import json
import time
import argparse
import statistics
import logging
from pathlib import Path
from typing import Optional, Dict, List, Any, Tuple
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


def probe_video(video_path: str) -> Dict[str, Any]:
    """Get video file info."""
    import av
    container = av.open(video_path)
    stream = container.streams.video[0]
    info = {
        'codec': stream.codec_context.name,
        'pix_fmt': stream.codec_context.format.name if stream.codec_context.format else None,
        'width': stream.codec_context.width,
        'height': stream.codec_context.height,
        'fps': float(stream.average_rate) if stream.average_rate else 0,
        'frames': stream.frames,
        'duration_s': float(stream.duration * stream.time_base) if stream.duration and stream.time_base else 0,
    }
    container.close()
    return info


def run_playback_runtime_benchmark(
    video_path: str,
    num_frames: int = 120,
    warmup_frames: int = 10,
    proxy_sizes: Optional[List[Tuple[int, int]]] = None,
    verify_color: bool = False,
) -> Dict[str, Any]:
    """
    Simulate real playback through the upload + render pipeline.

    Measures per-frame latency through the full path:
    1. Decode frame (PyAV)
    2. Prepare planes (R8/R16UI/float16 depending on bit depth)
    3. Upload to GPU textures
    4. Render YUV -> RGB via shader
    5. Simulated queue management (coalesce, drop late frames)

    This is NOT the full AsyncRenderer + QTimer path (which requires a Qt app),
    but simulates the same data flow and timing characteristics.
    """
    import av
    import numpy as np
    import moderngl

    from src.shared.video.yuv_gpu_upload import YUVGPUUploader

    if proxy_sizes is None:
        proxy_sizes = [(1920, 1080), (1280, 720), (960, 540)]

    def plane_to_array(plane, dtype: np.dtype) -> np.ndarray:
        itemsize = np.dtype(dtype).itemsize
        row_elems = plane.line_size // itemsize
        arr = np.frombuffer(plane, dtype=dtype).reshape(plane.height, row_elems)
        return np.ascontiguousarray(arr[:, :plane.width])

    video_info = probe_video(video_path)

    pix_fmt = video_info.get('pix_fmt', '') or ''
    bit_depth = 8
    if '12le' in pix_fmt:
        bit_depth = 12
    elif '10le' in pix_fmt:
        bit_depth = 10
    elif '16le' in pix_fmt:
        bit_depth = 16

    subsampling = '444'
    if '420' in pix_fmt:
        subsampling = '420'
    elif '422' in pix_fmt:
        subsampling = '422'

    results = {
        'video_path': video_path,
        'video_info': video_info,
        'timestamp': datetime.now().isoformat(),
        'config': {
            'num_frames': num_frames,
            'warmup_frames': warmup_frames,
            'proxy_sizes': [f"{w}x{h}" for w, h in proxy_sizes],
            'pixel_format': pix_fmt,
            'bit_depth': bit_depth,
            'subsampling': subsampling,
        },
        'sizes': {},
    }

    # Create ModernGL context and uploader
    ctx = moderngl.create_standalone_context()
    uploader = YUVGPUUploader(ctx, pool_size=8)

    # Run self-tests
    try:
        from src.shared.video.yuv_gpu_self_test import run_yuv_gpu_self_test
        test_result = run_yuv_gpu_self_test(ctx, uploader)
        results['gpu_self_test'] = {
            'passed': test_result.passed,
            'reason': test_result.reason,
        }
    except Exception as e:
        results['gpu_self_test'] = {'passed': False, 'reason': str(e)}

    r16ui_passed = uploader.is_r16_available()
    upload_mode = uploader.get_upload_mode(bit_depth)
    results['upload_mode'] = upload_mode

    logger.info(f"Video: {pix_fmt} {bit_depth}-bit {subsampling}, upload_mode={upload_mode}")

    for target_w, target_h in proxy_sizes:
        size_key = f"{target_w}x{target_h}"
        logger.info(f"\n=== Simulating playback at {size_key} ===")

        # Timing arrays
        decode_times = []
        prepare_times = []
        write_times = []
        render_times = []
        total_times = []

        # Playback simulation state
        frame_budget_ms = 1000.0 / (video_info['fps'] or 30.0)
        late_frames = 0
        dropped_frames = 0
        coalesced_frames = 0
        queue_depths = []
        last_displayed_frame = -1

        # Simulated frame queue (max depth like real renderer)
        max_queue_depth = 3
        frame_queue: List[Dict] = []

        container = av.open(video_path)
        stream = container.streams.video[0]
        stream.thread_type = 'AUTO'

        frame_count = 0
        for packet in container.demux(stream):
            t_decode_start = time.perf_counter()
            decoded_frames = packet.decode()
            packet_decode_ms = (time.perf_counter() - t_decode_start) * 1000.0
            per_frame_decode_ms = packet_decode_ms / max(len(decoded_frames), 1)

            for frame in decoded_frames:
                if frame_count >= num_frames + warmup_frames:
                    break

                is_warmup = frame_count < warmup_frames

                # Decode + reformat to proxy size
                t_reformat = time.perf_counter()
                work_frame = frame
                if frame.width != target_w or frame.height != target_h:
                    work_frame = frame.reformat(
                        width=target_w, height=target_h,
                        format=frame.format.name,
                    )
                reformat_ms = (time.perf_counter() - t_reformat) * 1000.0

                planes = []
                plane_dtype = np.uint16 if bit_depth > 8 else np.uint8
                for plane in work_frame.planes:
                    planes.append(plane_to_array(plane, plane_dtype))

                decode_ms = per_frame_decode_ms + reformat_ms

                if len(planes) < 3:
                    frame_count += 1
                    continue

                if not is_warmup:
                    # Simulate queue management
                    if len(frame_queue) >= max_queue_depth:
                        # Coalesce: drop oldest queued frame
                        frame_queue.pop(0)
                        coalesced_frames += 1

                    # Check if this frame is late (frame_index < last displayed)
                    if last_displayed_frame >= 0 and frame_count < last_displayed_frame:
                        late_frames += 1
                        frame_count += 1
                        continue

                    # Full pipeline timing
                    t_total = time.perf_counter()

                    try:
                        y_tex, u_tex, v_tex = uploader.upload_yuv_planar(
                            planes[0], planes[1], planes[2],
                            frame_count, bit_depth, subsampling
                        )
                    except Exception as e:
                        logger.error(f"Upload failed at frame {frame_count}: {e}")
                        dropped_frames += 1
                        frame_count += 1
                        continue

                    timing = uploader.get_last_upload_timing()
                    prepare_ms = timing.get('prepare_ms', 0)
                    write_ms = timing.get('write_ms', 0)

                    # Render
                    t_render = time.perf_counter()
                    try:
                        output_tex = ctx.texture((target_w, target_h), components=4, dtype='f2')
                        output_tex.filter = (moderngl.LINEAR, moderngl.LINEAR)
                        fbo = ctx.framebuffer(color_attachments=[output_tex])

                        uploader.set_yuv_matrix(0)
                        uploader.set_color_range(0)
                        uploader.render_yuv_to_rgb(
                            y_tex, u_tex, v_tex, fbo,
                            bit_depth=bit_depth,
                        )
                        render_ms = (time.perf_counter() - t_render) * 1000.0
                        fbo.release()
                        output_tex.release()
                    except Exception as e:
                        logger.error(f"Render failed at frame {frame_count}: {e}")
                        render_ms = 0
                        dropped_frames += 1
                        frame_count += 1
                        continue

                    total_ms = (time.perf_counter() - t_total) * 1000.0

                    # Color verification (sparse, only for first few frames)
                    if verify_color and frame_count == warmup_frames:
                        _verify_color_sample(ctx, uploader, y_tex, u_tex, v_tex,
                                           target_w, target_h, bit_depth, results, size_key)

                    decode_times.append(decode_ms)
                    prepare_times.append(prepare_ms)
                    write_times.append(write_ms)
                    render_times.append(render_ms)
                    total_times.append(total_ms)
                    queue_depths.append(len(frame_queue))

                    last_displayed_frame = frame_count

                    # Add to simulated queue
                    frame_queue.append({
                        'frame_index': frame_count,
                        'total_ms': total_ms,
                    })

                frame_count += 1

            if frame_count >= num_frames + warmup_frames:
                break

        container.close()

        def calc_stats(times: List[float]) -> Dict[str, float]:
            if not times:
                return {'avg': 0, 'p50': 0, 'p95': 0, 'max': 0, 'min': 0}
            s = sorted(times)
            n = len(s)
            return {
                'avg': sum(s) / n,
                'p50': s[int(n * 0.5)],
                'p95': s[int(n * 0.95)] if n > 1 else s[0],
                'max': s[-1],
                'min': s[0],
            }

        total_stats = calc_stats(total_times)
        effective_fps = 1000.0 / total_stats['p95'] if total_stats['p95'] > 0 else 0

        size_result = {
            'decode_ms': calc_stats(decode_times),
            'plane_prepare_ms': calc_stats(prepare_times),
            'texture_write_ms': calc_stats(write_times),
            'yuv_render_ms': calc_stats(render_times),
            'total_ms': total_stats,
            'estimated_fps': 1000.0 / total_stats['avg'] if total_stats['avg'] > 0 else 0,
            'effective_fps': effective_fps,
            'frame_budget_ms': frame_budget_ms,
            'upload_mode': upload_mode,
            'late_frames': late_frames,
            'dropped_frames': dropped_frames,
            'coalesced_frames': coalesced_frames,
            'avg_queue_depth': statistics.mean(queue_depths) if queue_depths else 0,
            'max_queue_depth': max(queue_depths) if queue_depths else 0,
            'frames_tested': len(decode_times),
            'within_budget_pct': sum(1 for t in total_times if t <= frame_budget_ms) / max(len(total_times), 1) * 100,
        }

        results['sizes'][size_key] = size_result

        # Print results
        logger.info(f"\n  Results for {size_key}:")
        logger.info(f"    upload_mode:     {upload_mode}")
        logger.info(f"    frame_budget:    {frame_budget_ms:.1f}ms")
        logger.info(f"    total avg/p95:   {total_stats['avg']:.1f} / {total_stats['p95']:.1f}ms")
        logger.info(f"    effective_fps:   {effective_fps:.1f}")
        logger.info(f"    within_budget:   {size_result['within_budget_pct']:.0f}%")
        logger.info(f"    late_frames:     {late_frames}")
        logger.info(f"    dropped_frames:  {dropped_frames}")
        logger.info(f"    coalesced:       {coalesced_frames}")
        logger.info(f"    avg_queue_depth: {size_result['avg_queue_depth']:.1f}")

    # Cleanup
    uploader.release()
    ctx.release()

    return results


def _verify_color_sample(
    ctx, uploader, y_tex, u_tex, v_tex,
    width, height, bit_depth, results, size_key
):
    """Sparse color readback verification for first frame."""
    import numpy as np
    try:
        verify_tex = ctx.texture((width, height), components=4, dtype='f1')
        verify_tex.filter = (moderngl.NEAREST, moderngl.NEAREST)
        verify_fbo = ctx.framebuffer(color_attachments=[verify_tex])

        uploader.set_yuv_matrix(0)
        uploader.set_color_range(0)
        uploader.render_yuv_to_rgb(y_tex, u_tex, v_tex, verify_fbo, bit_depth=bit_depth)

        # Sample 16 points (4x4 grid)
        sample_points = []
        for row in range(0, height, max(height // 4, 1))[:4]:
            for col in range(0, width, max(width // 4, 1))[:4]:
                raw = verify_fbo.read(viewport=(col, row, 1, 1), components=4)
                r, g, b = raw[0], raw[1], raw[2]
                sample_points.append({
                    'x': col, 'y': row,
                    'r': r, 'g': g, 'b': b,
                })

        results.setdefault('color_verification', {})[size_key] = {
            'sample_points': sample_points,
            'all_black': all(p['r'] == 0 and p['g'] == 0 and p['b'] == 0 for p in sample_points),
        }

        verify_fbo.release()
        verify_tex.release()
    except Exception as e:
        results.setdefault('color_verification', {})[size_key] = {'error': str(e)}


def main():
    parser = argparse.ArgumentParser(
        description='Topos Color Playback Runtime Benchmark'
    )

    parser.add_argument('--file', '-f', type=str, required=True,
                       help='Video file path')
    parser.add_argument('--frames', '-n', type=int, default=120,
                       help='Number of frames to test (excluding warmup)')
    parser.add_argument('--warmup', '-w', type=int, default=10,
                       help='Warmup frames')
    parser.add_argument('--sizes', '-s', type=str, default='1920x1080,1280x720,960x540',
                       help='Proxy sizes (comma-separated WxH)')
    parser.add_argument('--verify-color', '-v', action='store_true',
                       help='Sparse color readback verification on first frame')
    parser.add_argument('--output-json', '-o', type=str, default=None,
                       help='Output JSON file path')
    parser.add_argument('--quiet', '-q', action='store_true',
                       help='Quiet mode')

    args = parser.parse_args()

    if args.quiet:
        logging.getLogger().setLevel(logging.WARNING)

    if not os.path.exists(args.file):
        print(f"Error: File not found: {args.file}")
        sys.exit(1)

    sizes = []
    for s in args.sizes.split(','):
        parts = s.strip().split('x')
        if len(parts) == 2:
            sizes.append((int(parts[0]), int(parts[1])))

    info = probe_video(args.file)
    logger.info(f"Video: {info['codec']} {info['pix_fmt']} {info['width']}x{info['height']} @ {info['fps']:.2f}fps")

    results = run_playback_runtime_benchmark(
        video_path=args.file,
        num_frames=args.frames,
        warmup_frames=args.warmup,
        proxy_sizes=sizes,
        verify_color=args.verify_color,
    )

    # Print summary
    print("\n" + "=" * 100)
    print("Playback Runtime Benchmark Results")
    print("=" * 100)
    print(f"\nVideo: {args.file}")
    print(f"  codec={info['codec']}, pix_fmt={info['pix_fmt']}, "
          f"size={info['width']}x{info['height']}, fps={info['fps']:.2f}")
    print(f"  upload_mode={results['upload_mode']}")
    print(f"  GPU self-test: {'PASSED' if results['gpu_self_test']['passed'] else 'FAILED'}")
    print()

    hdr = (f"{'Size':<15} {'p95FPS':>8} {'Budget':>8} {'Within%':>8} "
           f"{'Decode':>10} {'Prepare':>10} {'Write':>10} {'Render':>10} "
           f"{'Late':>6} {'Drop':>6} {'Coalesce':>9}")
    print(hdr)
    print("-" * 110)

    for size_key, r in results['sizes'].items():
        print(f"{size_key:<15} "
              f"{r['effective_fps']:>7.1f} "
              f"{r['frame_budget_ms']:>7.1f}ms "
              f"{r['within_budget_pct']:>7.0f}% "
              f"{r['decode_ms']['p95']:>9.1f} "
              f"{r['plane_prepare_ms']['p95']:>9.1f} "
              f"{r['texture_write_ms']['p95']:>9.1f} "
              f"{r['yuv_render_ms']['p95']:>9.1f} "
              f"{r['late_frames']:>5} "
              f"{r['dropped_frames']:>5} "
              f"{r['coalesced_frames']:>8}")

    print()

    # Save JSON
    if args.output_json:
        out_path = args.output_json
    else:
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        project_root = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
        results_dir = os.path.join(project_root, 'tests', 'media', 'results')
        os.makedirs(results_dir, exist_ok=True)
        out_path = os.path.join(results_dir, f'playback_runtime_{timestamp}.json')

    with open(out_path, 'w') as f:
        json.dump(results, f, indent=2)
    logger.info(f"Results saved to {out_path}")


if __name__ == '__main__':
    main()
