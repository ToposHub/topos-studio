#!/usr/bin/env python3
"""
YUV GPU Pipeline Performance Benchmark Script

精确测量 YUV GPU 上传管线的各阶段耗时。

测量指标：
- decode_ms: PyAV/FFmpeg 解码耗时
- plane_prepare_ms: Numpy 归一化、astype、buffer 准备
- texture_write_ms: Texture.write() 三平面上传
- render_ms: YUV shader 转 RGB + FBO render
- total_ms: decode + prepare + upload + render

输出格式：JSON

使用方法：
    python tests/benchmark/benchmark_yuv_gpu_pipeline.py \
      --file <local-video-file> \
      --sizes 1920x1080,1280x720,960x540 \
      --frames 180 \
      --warmup 20
"""

import os
import re
import subprocess
import sys
import json
import time
import argparse
import statistics
import logging
import platform
from pathlib import Path
from typing import Optional, Dict, List, Any, Tuple
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


def _git_sha() -> str:
    """当前 checkout 的 commit，用于跨 commit 比较基线。"""
    try:
        return subprocess.run(
            ['git', 'rev-parse', 'HEAD'],
            cwd=os.path.join(os.path.dirname(__file__), '..', '..'),
            capture_output=True, text=True, check=True,
        ).stdout.strip()
    except Exception:
        return 'unknown'


def _four_dim_render_path(gl_version: str) -> Dict[str, str]:
    """本 harness 实际执行的四维渲染路径（P0-02 统一 trace 字段）。

    PyAV/FFmpeg 软解 → numpy 平面交接（无 interop）→ ModernGL 上传 →
    GLSL fragment YUV 转换。任何一段变化时必须同步更新此描述，
    使归档可比、门禁可执行。
    """
    match = re.search(r'(\d+\.\d+)', gl_version or '')
    gl = f"opengl_{match.group(1)}" if match else 'opengl_unknown'
    return {
        'graphics_api': gl,
        'compute_api': 'glsl_fragment',
        'decode_api': 'cpu_ffmpeg_sw',
        'interop_api': 'none_numpy_handoff',
    }


def probe_video(video_path: str) -> Dict[str, Any]:
    """获取视频文件信息"""
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


def run_yuv_gpu_benchmark(
    video_path: str,
    sizes: List[Tuple[int, int]],
    num_frames: int = 180,
    warmup_frames: int = 20,
    force_mode: str = 'auto',
) -> Dict[str, Any]:
    """
    运行 YUV GPU pipeline benchmark。

    对每个 proxy 尺寸：
    1. 解码帧
    2. 上传到 GPU（分段计时）
    3. 渲染 YUV->RGB
    4. 记录各阶段耗时
    """
    import av
    import numpy as np
    import moderngl

    def plane_to_array(plane, dtype: np.dtype) -> np.ndarray:
        """Convert a PyAV plane to a tight contiguous ndarray.

        PyAV planes may include row padding. Benchmark upload sizes must use the
        visible plane width, not the padded line size, otherwise proxy-size
        results become misleading.
        """
        itemsize = np.dtype(dtype).itemsize
        row_elems = plane.line_size // itemsize
        arr = np.frombuffer(plane, dtype=dtype).reshape(plane.height, row_elems)
        return np.ascontiguousarray(arr[:, :plane.width])

    results = {
        'video_path': video_path,
        'video_info': probe_video(video_path),
        'timestamp': datetime.now().isoformat(),
        'git_sha': _git_sha(),
        'config': {
            'num_frames': num_frames,
            'warmup_frames': warmup_frames,
            'force_mode': force_mode,
        },
        'sizes': {},
        'gpu_self_test': None,
    }

    # 创建 ModernGL standalone context
    ctx = moderngl.create_standalone_context()
    gl_version = str(ctx.info.get('GL_VERSION', ''))
    gl_renderer = str(ctx.info.get('GL_RENDERER', ''))
    results['environment'] = {
        'os': platform.platform(),
        'machine': platform.machine(),
        'gl_version': gl_version,
        'gl_renderer': gl_renderer,
        'render_path': _four_dim_render_path(gl_version),
    }
    logger.info(f"GL: {gl_renderer} / {gl_version}")

    from src.shared.video.yuv_gpu_upload import YUVGPUUploader
    uploader = YUVGPUUploader(ctx, pool_size=5)

    # 运行 GPU self-test
    try:
        from src.shared.video.yuv_gpu_self_test import run_yuv_gpu_self_test
        test_result = run_yuv_gpu_self_test(ctx, uploader)
        results['gpu_self_test'] = {
            'passed': test_result.passed,
            'reason': test_result.reason,
            'max_error': test_result.max_abs_error,
        }
        logger.info(f"GPU self-test: {test_result}")
    except Exception as e:
        results['gpu_self_test'] = {'passed': False, 'reason': str(e)}
        logger.error(f"GPU self-test failed: {e}")

    # Run R16UI self-test
    r16ui_self_test_result = None
    try:
        from src.shared.video.yuv_gpu_self_test import run_r16ui_gpu_self_test
        r16ui_self_test_result = run_r16ui_gpu_self_test(ctx, uploader)
        results['r16ui_self_test'] = {
            'passed': r16ui_self_test_result.passed,
            'reason': r16ui_self_test_result.reason,
        }
        logger.info(f"R16UI self-test: {r16ui_self_test_result}")
    except Exception as e:
        results['r16ui_self_test'] = {'passed': False, 'reason': str(e)}
        logger.error(f"R16UI self-test failed: {e}")

    r16_available = uploader.is_r16_available()
    logger.info(f"R16UI mode: {'available' if r16_available else 'not available'}")

    for target_w, target_h in sizes:
        size_key = f"{target_w}x{target_h}"
        logger.info(f"\n=== Benchmarking {size_key} ===")

        # 打开视频并设置 proxy 尺寸
        container = av.open(video_path)
        stream = container.streams.video[0]
        stream.thread_type = 'AUTO'

        # 确定 pixel format 和 bit depth
        pix_fmt = results['video_info'].get('pix_fmt', '') or ''
        bit_depth = 8
        if '12le' in pix_fmt:
            bit_depth = 12
        elif '10le' in pix_fmt:
            bit_depth = 10
        elif '16le' in pix_fmt:
            bit_depth = 16

        # 确定子采样类型
        subsampling = '444'
        if '420' in pix_fmt:
            subsampling = '420'
        elif '422' in pix_fmt:
            subsampling = '422'

        logger.info(f"  pixel_format={pix_fmt}, bit_depth={bit_depth}, subsampling={subsampling}")

        decode_times = []
        prepare_times = []
        write_times = []
        render_times = []
        total_times = []

        t_size_wall_start = time.perf_counter()

        frame_count = 0
        for packet in container.demux(stream):
            t_packet_decode_start = time.perf_counter()
            decoded_frames = packet.decode()
            packet_decode_ms = (time.perf_counter() - t_packet_decode_start) * 1000.0
            per_frame_decode_ms = packet_decode_ms / max(len(decoded_frames), 1)

            for frame in decoded_frames:
                if frame_count >= num_frames + warmup_frames:
                    break

                is_warmup = frame_count < warmup_frames

                # 阶段 1: 解码 + proxy 尺寸重采样
                # packet.decode() 已在外层计时；这里把 packet decode 时间按帧均摊。
                # 如果指定 proxy 尺寸，用 PyAV reformat 生成对应尺寸的 YUV plane，
                # 避免三档尺寸实际上传同一份原始 4K plane。
                t_reformat_start = time.perf_counter()
                work_frame = frame
                if frame.width != target_w or frame.height != target_h:
                    work_frame = frame.reformat(
                        width=target_w,
                        height=target_h,
                        format=frame.format.name,
                    )
                reformat_ms = (time.perf_counter() - t_reformat_start) * 1000.0

                # 获取平面数据
                # 计入 decode 阶段：PyAV 帧的像素物化（含线程解码收尾）发生在
                # plane 访问时，不计入会把 4K decode 误报成 ~0ms（实测教训）。
                plane_dtype = np.uint16 if bit_depth > 8 else np.uint8
                t_planes_start = time.perf_counter()
                planes = []
                for plane in work_frame.planes:
                    planes.append(plane_to_array(plane, plane_dtype))
                planes_ms = (time.perf_counter() - t_planes_start) * 1000.0

                decode_ms = per_frame_decode_ms + reformat_ms + planes_ms

                if len(planes) < 3:
                    frame_count += 1
                    continue

                # 阶段 2+3: 上传到 GPU
                t_total_start = time.perf_counter()

                try:
                    use_r16ui = (force_mode == 'r16ui' or
                                 (force_mode == 'auto' and r16_available and bit_depth > 8))
                    if use_r16ui:
                        if subsampling == '444':
                            y_tex, u_tex, v_tex = uploader.upload_yuv444_r16ui(
                                planes[0], planes[1], planes[2],
                                frame_count, bit_depth)
                        elif subsampling == '422':
                            y_tex, u_tex, v_tex = uploader.upload_yuv422_r16ui(
                                planes[0], planes[1], planes[2],
                                frame_count, bit_depth)
                        else:
                            y_tex, u_tex, v_tex = uploader.upload_yuv420_r16ui(
                                planes[0], planes[1], planes[2],
                                frame_count, bit_depth)
                    else:
                        if subsampling == '444':
                            y_tex, u_tex, v_tex = uploader.upload_yuv444(
                                planes[0], planes[1], planes[2],
                                frame_count, bit_depth)
                        elif subsampling == '422':
                            y_tex, u_tex, v_tex = uploader.upload_yuv422(
                                planes[0], planes[1], planes[2],
                                frame_count, bit_depth)
                        else:
                            y_tex, u_tex, v_tex = uploader.upload_yuv420(
                                planes[0], planes[1], planes[2],
                                frame_count, bit_depth)
                except Exception as e:
                    logger.error(f"  Upload failed at frame {frame_count}: {e}")
                    frame_count += 1
                    continue

                timing = uploader.get_last_upload_timing()
                prepare_ms = timing.get('prepare_ms', 0)
                write_ms = timing.get('write_ms', 0)

                # 阶段 4: 渲染 YUV->RGB
                t_render_start = time.perf_counter()
                try:
                    output_tex = ctx.texture((target_w, target_h), components=4, dtype='f1')
                    output_tex.filter = (moderngl.LINEAR, moderngl.LINEAR)
                    fbo = ctx.framebuffer(color_attachments=[output_tex])

                    uploader.set_yuv_matrix(0)  # BT.709
                    uploader.set_color_range(0)  # Limited

                    uploader.render_yuv_to_rgb(
                        y_tex, u_tex, v_tex, fbo,
                        bit_depth=bit_depth,
                    )

                    render_ms = (time.perf_counter() - t_render_start) * 1000.0

                    # 清理
                    fbo.release()
                    output_tex.release()
                except Exception as e:
                    logger.error(f"  Render failed at frame {frame_count}: {e}")
                    render_ms = 0
                    frame_count += 1
                    continue

                total_ms = decode_ms + (time.perf_counter() - t_total_start) * 1000.0

                if not is_warmup:
                    decode_times.append(decode_ms)
                    prepare_times.append(prepare_ms)
                    write_times.append(write_ms)
                    render_times.append(render_ms)
                    total_times.append(total_ms)

                    if (frame_count - warmup_frames) % 50 == 0:
                        logger.info(f"    Frame {frame_count}: "
                                   f"decode={decode_ms:.1f}ms, "
                                   f"prepare={prepare_ms:.1f}ms, "
                                   f"write={write_ms:.1f}ms, "
                                   f"render={render_ms:.1f}ms, "
                                   f"total={total_ms:.1f}ms")

                frame_count += 1

            if frame_count >= num_frames + warmup_frames:
                break

        container.close()

        wall_ms = (time.perf_counter() - t_size_wall_start) * 1000.0

        # 计算统计
        def calc_stats(times: List[float]) -> Dict[str, float]:
            if not times:
                return {'avg': 0, 'p50': 0, 'p95': 0, 'max': 0}
            s = sorted(times)
            n = len(s)
            return {
                'avg': sum(s) / n,
                'p50': s[int(n * 0.5)],
                'p95': s[int(n * 0.95)] if n > 1 else s[0],
                'max': s[-1],
            }

        size_result = {
            'decode_ms': calc_stats(decode_times),
            'plane_prepare_ms': calc_stats(prepare_times),
            'texture_write_ms': calc_stats(write_times),
            'render_ms': calc_stats(render_times),
            'total_ms': calc_stats(total_times),
            'estimated_fps': 1000.0 / calc_stats(total_times)['avg'] if calc_stats(total_times)['avg'] > 0 else 0,
            'effective_fps': 1000.0 / calc_stats(total_times)['p95'] if calc_stats(total_times)['p95'] > 0 else 0,
            'pixel_format': pix_fmt,
            'bit_depth': bit_depth,
            'subsampling': subsampling,
            'frame_path': f"r16ui_direct" if (force_mode == 'r16ui' or (force_mode == 'auto' and r16_available and bit_depth > 8)) else ("r8_direct" if force_mode == 'auto' and bit_depth <= 8 else "float16"),
            'render_path': _four_dim_render_path(gl_version),
            'frames_tested': len(decode_times),
            # 墙钟地板：total_ms×frames 不会低于它。分项归因有疑问时以它为准。
            'wall_ms': wall_ms,
            'wall_effective_fps': (1000.0 * len(decode_times) / wall_ms) if wall_ms > 0 else 0,
        }

        results['sizes'][size_key] = size_result

        logger.info(f"\n  Results for {size_key}:")
        logger.info(f"    decode:     avg={size_result['decode_ms']['avg']:.1f}ms, "
                    f"p95={size_result['decode_ms']['p95']:.1f}ms")
        logger.info(f"    prepare:    avg={size_result['plane_prepare_ms']['avg']:.1f}ms, "
                    f"p95={size_result['plane_prepare_ms']['p95']:.1f}ms")
        logger.info(f"    write:      avg={size_result['texture_write_ms']['avg']:.1f}ms, "
                    f"p95={size_result['texture_write_ms']['p95']:.1f}ms")
        logger.info(f"    render:     avg={size_result['render_ms']['avg']:.1f}ms, "
                    f"p95={size_result['render_ms']['p95']:.1f}ms")
        logger.info(f"    total:      avg={size_result['total_ms']['avg']:.1f}ms, "
                    f"p95={size_result['total_ms']['p95']:.1f}ms")
        logger.info(f"    est. FPS:   {size_result['estimated_fps']:.1f}")

    # 清理
    uploader.release()
    ctx.release()

    return results


def main():
    parser = argparse.ArgumentParser(
        description='Topos Color YUV GPU Pipeline Performance Benchmark'
    )

    parser.add_argument(
        '--file', '-f',
        type=str,
        required=True,
        help='视频文件路径'
    )

    parser.add_argument(
        '--sizes', '-s',
        type=str,
        default='1920x1080,1280x720,960x540',
        help='Proxy 尺寸列表 (逗号分隔, 格式 WxH)'
    )

    parser.add_argument(
        '--frames', '-n',
        type=int,
        default=180,
        help='测试帧数 (不含 warmup)'
    )

    parser.add_argument(
        '--warmup', '-w',
        type=int,
        default=20,
        help='Warmup 帧数'
    )

    parser.add_argument(
        '--output-json', '-o',
        type=str,
        default=None,
        help='输出 JSON 文件路径'
    )

    parser.add_argument(
        '--mode', '-m',
        type=str,
        default='auto',
        choices=['auto', 'float16', 'r16ui'],
        help='上传模式: auto(自动选择), float16(强制float16), r16ui(强制R16UI)'
    )

    parser.add_argument(
        '--verify-color', '-v',
        action='store_true',
        help='运行颜色自检 (8-bit + R16UI 12-bit self-test)'
    )

    parser.add_argument(
        '--readback-samples',
        type=int,
        default=0,
        help='颜色回读采样数量 (0=不做回读, >0=回读N个像素并报告RGB值)'
    )

    parser.add_argument(
        '--quiet', '-q',
        action='store_true',
        help='安静模式'
    )

    args = parser.parse_args()

    if args.quiet:
        logging.getLogger().setLevel(logging.WARNING)

    if not os.path.exists(args.file):
        print(f"Error: File not found: {args.file}")
        sys.exit(1)

    # 解析尺寸
    sizes = []
    for s in args.sizes.split(','):
        parts = s.strip().split('x')
        if len(parts) == 2:
            sizes.append((int(parts[0]), int(parts[1])))

    if not sizes:
        print("Error: No valid sizes specified")
        sys.exit(1)

    # 先探测视频信息
    logger.info("Probing video file...")
    info = probe_video(args.file)
    logger.info(f"  codec={info['codec']}, pix_fmt={info['pix_fmt']}")
    logger.info(f"  size={info['width']}x{info['height']}, fps={info['fps']:.2f}")
    logger.info(f"  frames={info['frames']}, duration={info['duration_s']:.1f}s")

    # 运行 benchmark
    results = run_yuv_gpu_benchmark(
        video_path=args.file,
        sizes=sizes,
        num_frames=args.frames,
        warmup_frames=args.warmup,
        force_mode=args.mode,
    )

    # 输出结果
    print("\n" + "=" * 80)
    print("YUV GPU Pipeline Benchmark Results")
    print("=" * 80)
    print(f"\nVideo: {args.file}")
    print(f"  codec={info['codec']}, pix_fmt={info['pix_fmt']}, "
          f"size={info['width']}x{info['height']}, fps={info['fps']:.2f}")
    print(f"  git: {results.get('git_sha', 'unknown')}")
    env = results.get('environment', {})
    print(f"  GL: {env.get('gl_renderer', '?')} / {env.get('gl_version', '?')}")
    rp = env.get('render_path', {})
    if rp:
        print(f"  path: graphics={rp['graphics_api']} compute={rp['compute_api']} "
              f"decode={rp['decode_api']} interop={rp['interop_api']}")
    print(f"  GPU self-test: {'PASSED' if results['gpu_self_test']['passed'] else 'FAILED'}")
    r16ui_test = results.get('r16ui_self_test', {})
    print(f"  R16UI self-test: {'PASSED' if r16ui_test.get('passed', False) else 'FAILED'}"
          f" ({r16ui_test.get('reason', 'N/A')})")
    print()

    print(f"{'Size':<15} {'FPS':>8} {'p95FPS':>8} {'Total':>10} {'Decode':>10} {'Prepare':>10} {'Write':>10} {'Render':>10} {'Path':<20}")
    print("-" * 105)

    for size_key, r in results['sizes'].items():
        print(f"{size_key:<15} "
              f"{r['estimated_fps']:>7.1f} "
              f"{r.get('effective_fps', 0):>7.1f} "
              f"{r['total_ms']['avg']:>9.1f} "
              f"{r['decode_ms']['avg']:>9.1f} "
              f"{r['plane_prepare_ms']['avg']:>9.1f} "
              f"{r['texture_write_ms']['avg']:>9.1f} "
              f"{r['render_ms']['avg']:>9.1f} "
              f"{r['frame_path']:<20}")

    print()

    # 保存 JSON
    if args.output_json:
        with open(args.output_json, 'w') as f:
            json.dump(results, f, indent=2)
        logger.info(f"Results saved to {args.output_json}")
    else:
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        project_root = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
        results_dir = os.path.join(project_root, 'tests', 'media', 'results')
        os.makedirs(results_dir, exist_ok=True)
        default_path = os.path.join(results_dir, f'yuv_gpu_benchmark_{timestamp}.json')
        with open(default_path, 'w') as f:
            json.dump(results, f, indent=2)
        logger.info(f"Results saved to {default_path}")


if __name__ == '__main__':
    main()
