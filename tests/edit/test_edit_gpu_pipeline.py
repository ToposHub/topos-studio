#!/usr/bin/env python3
"""
Edit Page GPU Pipeline Decode + Render Rate Benchmark

测试编辑页面全 GPU 管线的解码和渲染速率:
1. GPU 解码: MacOSVtZeroCopySource / FFmpegHwaccelSource → GpuDecodedFrame
2. CPU 对比: FFmpegMediaSource → DecodedFrame → NumPy → QImage
3. 渲染速率: 全链路吞吐 (decode + composite + display)

使用方法:
    .venv/bin/python tests/edit/test_edit_gpu_pipeline.py
    .venv/bin/python tests/edit/test_edit_gpu_pipeline.py --video tests/test_videos/4K_H264.mov
"""

import os
import sys
import time
import argparse
import logging
import statistics
from pathlib import Path
from typing import Optional, Dict, List, Any, Tuple

# Project root
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("edit_gpu_bench")


# ================================================================
# Test Videos
# ================================================================

DEFAULT_TEST_VIDEOS = [
    "tests/test_videos/4K_H264.mov",
    "tests/test_videos/4K_H265.mov",
    "tests/test_videos/4K_prores422HQ.mov",
    "tests/test_videos/2K_h264.mp4",
    "tests/test_videos/2K_H265.mov",
]


# ================================================================
# Phase 1: GPU Decode Benchmark (no GL context needed)
# ================================================================

def bench_gpu_decode(video_path: str, num_frames: int = 60) -> Dict[str, Any]:
    """
    Benchmark GPU decode path: MacOSVtZeroCopySource / FFmpegHwaccelSource.

    Returns dict with timing info.
    """
    import numpy as np

    results: Dict[str, Any] = {
        "video": os.path.basename(video_path),
        "num_frames": num_frames,
        "gpu_decode": None,
        "cpu_decode": None,
    }

    # --- GPU Decode (macOS VT zero-copy) ---
    # NOTE: VT zero-copy requires GL context for IOSurface→GL texture binding.
    # Skip direct VT zero-copy testing here; it's tested in Phase 3 (full pipeline).
    gpu_source = None
    gpu_frame_type = "none"
    gpu_times: List[float] = []
    gpu_frame_sizes: List[Tuple[int, int]] = []

    # Probe codec
    try:
        from src.shared.media.factory import MediaSourceFactory
        metadata = MediaSourceFactory.probe_video_metadata(video_path)
        codec = metadata.codec_name.lower() if metadata else ""
        total = metadata.total_frames if metadata else 0
        width = metadata.width if metadata else 0
        height = metadata.height if metadata else 0
    except Exception:
        metadata = None
        codec = ""
        total = 0
        width = 0
        height = 0

    logger.info(f"  Video: {os.path.basename(video_path)} "
                 f"{width}x{height} codec={codec} frames={total}")

    # Check zero-copy capability (without actually opening source — needs GL)
    try:
        from src.shared.media.macos_vt.capability import is_zero_copy_available
        zero_copy_capable = is_zero_copy_available(codec=codec)
        logger.info(f"  Zero-copy capable: {zero_copy_capable}")
        if zero_copy_capable:
            logger.info(f"  (VT zero-copy decode tested in Phase 3 with GL context)")
    except Exception as e:
        logger.debug(f"  Zero-copy check failed: {e}")

    except ImportError as e:
        logger.warning(f"  macOS VT zero-copy import failed: {e}")
    except Exception as e:
        logger.error(f"  GPU decode error: {e}", exc_info=True)
        if gpu_source:
            try:
                gpu_source.close()
            except Exception:
                pass

    # --- FFmpegHwaccelSource fallback ---
    if results["gpu_decode"] is None:
        try:
            from src.shared.media.factory import MediaSourceFactory

            from src.shared.media.decode_policy import DecodeTarget
            factory = MediaSourceFactory()
            source, status = factory.create_with_hwaccel(
                path=video_path,
                decode_target=DecodeTarget.PREVIEW,
                backend="ffmpeg",
                # Don't auto_open — MacOSVtZeroCopySource.open() needs path arg
                # and the factory already opens VT source during creation.
                auto_open=False,
            )
            # Open if not already opened
            if source is not None and hasattr(source, '_is_open') and not source._is_open:
                try:
                    source.open(video_path)
                except TypeError:
                    source.open()

            if source is not None:
                hwaccel_type = getattr(status, 'decoder_type', 'none')
                logger.info(f"  HW source created: decoder={hwaccel_type}")

                # VT zero-copy needs GL context — skip decode benchmark here
                if 'zero_copy' in hwaccel_type:
                    logger.info(f"  VT zero-copy source created (needs GL context, tested in Phase 3)")
                    results["gpu_decode"] = {
                        "source": "MacOSVtZeroCopySource",
                        "frame_type": "macos_vt_zero_copy (needs GL for decode)",
                        "decoded_frames": 0,
                        "note": "VT zero-copy requires GL context; tested in Phase 3",
                    }
                    source.close()
                else:
                    # Non-zero-copy hwaccel (FFmpegHwaccelSource) can decode without GL
                    hw_times: List[float] = []
                    decoded_count = 0
                    for i in range(num_frames):
                        t0 = time.perf_counter()
                        try:
                            frame = source.read_frame(i)
                            dt = time.perf_counter() - t0
                            hw_times.append(dt * 1000.0)
                            decoded_count += 1
                        except (StopIteration, Exception):
                            break

                    if hw_times:
                        results["gpu_decode"] = {
                            "source": f"FFmpegHwaccelSource({hwaccel_type})",
                            "frame_type": "DecodedFrame(hwaccel)",
                            "decoded_frames": decoded_count,
                            "avg_ms": round(statistics.mean(hw_times), 2),
                            "median_ms": round(statistics.median(hw_times), 2),
                            "min_ms": round(min(hw_times), 2),
                            "max_ms": round(max(hw_times), 2),
                            "fps": round(decoded_count / sum(hw_times) * 1000, 1) if sum(hw_times) > 0 else 0,
                        }
                        logger.info(f"  HW decode: {decoded_count} frames, "
                                     f"avg={results['gpu_decode']['avg_ms']:.2f}ms, "
                                     f"fps={results['gpu_decode']['fps']}")
                    source.close()
        except Exception as e:
            logger.error(f"  FFmpegHwaccelSource error: {e}", exc_info=True)

    # --- CPU Decode (baseline) ---
    try:
        from src.shared.media.ffmpeg_source import FFmpegMediaSource

        cpu_source = FFmpegMediaSource(video_path)
        cpu_source.open()

        cpu_times: List[float] = []
        decoded_count = 0
        for i in range(num_frames):
            t0 = time.perf_counter()
            try:
                frame = cpu_source.read_frame(i)
                dt = time.perf_counter() - t0
                cpu_times.append(dt * 1000.0)
                decoded_count += 1
            except (StopIteration, ValueError):
                break

        if cpu_times:
            results["cpu_decode"] = {
                "source": "FFmpegMediaSource",
                "decoded_frames": decoded_count,
                "avg_ms": round(statistics.mean(cpu_times), 2),
                "median_ms": round(statistics.median(cpu_times), 2),
                "min_ms": round(min(cpu_times), 2),
                "max_ms": round(max(cpu_times), 2),
                "fps": round(decoded_count / sum(cpu_times) * 1000, 1) if sum(cpu_times) > 0 else 0,
            }
            logger.info(f"  CPU decode: {decoded_count} frames, "
                         f"avg={results['cpu_decode']['avg_ms']:.2f}ms, "
                         f"fps={results['cpu_decode']['fps']}")

        cpu_source.close()
    except Exception as e:
        logger.error(f"  CPU decode error: {e}", exc_info=True)

    # --- Comparison ---
    if results["gpu_decode"] and results["cpu_decode"]:
        gpu_avg = results["gpu_decode"].get("avg_ms")
        cpu_avg = results["cpu_decode"].get("avg_ms")
        if gpu_avg and cpu_avg:
            speedup = cpu_avg / max(gpu_avg, 0.01)
            results["speedup"] = round(speedup, 2)
            logger.info(f"  GPU vs CPU speedup: {speedup:.2f}x")

    return results


# ================================================================
# Phase 2: EditGpuDecodeManager Benchmark
# ================================================================

def bench_decode_manager(video_path: str, num_frames: int = 30) -> Dict[str, Any]:
    """
    Benchmark EditGpuDecodeManager which wraps the source selection logic.
    """
    try:
        from src.features.edit.core.edit_gpu_decode_manager import EditGpuDecodeManager
    except Exception as e:
        logger.error(f"Cannot import EditGpuDecodeManager: {e}")
        return {"error": str(e)}

    manager = EditGpuDecodeManager()
    times: List[float] = []
    gpu_count = 0
    cpu_count = 0
    error_count = 0

    logger.info(f"  EditGpuDecodeManager bench: {os.path.basename(video_path)}")

    for i in range(num_frames):
        t0 = time.perf_counter()
        frame, frame_type = manager.get_frame(video_path, i)
        dt = time.perf_counter() - t0

        if frame is None:
            error_count += 1
            continue

        times.append(dt * 1000.0)
        if frame_type == 'gpu':
            gpu_count += 1
        else:
            cpu_count += 1

        from src.shared.media.gpu_frame import safe_release_gpu_frame
        safe_release_gpu_frame(frame)

    manager.close_all()

    result = {
        "video": os.path.basename(video_path),
        "total_frames": num_frames,
        "gpu_frames": gpu_count,
        "cpu_frames": cpu_count,
        "errors": error_count,
    }

    if times:
        # Source creation and the first seek are startup work, not the
        # per-frame realtime budget. Keep them visible, but report steady
        # playback separately so a 200ms cold open is not averaged into a
        # 25fps/40ms frame-budget conclusion.
        warmup_count = min(5, max(0, len(times) - 1))
        steady_times = times[warmup_count:] or times
        sorted_steady_times = sorted(steady_times)
        p95_index = round((len(sorted_steady_times) - 1) * 0.95)
        result.update({
            "cold_first_frame_ms": round(times[0], 2),
            "steady_avg_ms": round(statistics.mean(steady_times), 2),
            "steady_median_ms": round(statistics.median(steady_times), 2),
            "steady_p95_ms": round(sorted_steady_times[p95_index], 2),
            "steady_fps": round(len(steady_times) / sum(steady_times) * 1000, 1),
        })
        logger.info(f"  DecodeManager: gpu={gpu_count} cpu={cpu_count} err={error_count} "
                     f"cold={result['cold_first_frame_ms']:.2f}ms "
                     f"steady_avg={result['steady_avg_ms']:.2f}ms "
                     f"p95={result['steady_p95_ms']:.2f}ms "
                     f"steady_fps={result['steady_fps']}")

    return result


# ================================================================
# Phase 3: Full Pipeline Benchmark (needs QApplication + GL)
# ================================================================

def bench_full_pipeline(video_path: str, num_frames: int = 30) -> Dict[str, Any]:
    """
    Benchmark the full GPU pipeline: decode → render thread → texture output.

    Requires QApplication and QOpenGLWidget.
    """
    try:
        from PyQt6.QtWidgets import QApplication
        from PyQt6.QtGui import QOpenGLContext
    except ImportError as e:
        logger.error(f"Cannot import PyQt6: {e}")
        return {"error": "PyQt6 not available"}

    # Check if app exists
    app = QApplication.instance()
    if app is None:
        app = QApplication(sys.argv)

    try:
        from PyQt6.QtGui import QSurfaceFormat
        from PyQt6.QtOpenGLWidgets import QOpenGLWidget

        # Create a test GL widget to get a context
        class TestGLWidget(QOpenGLWidget):
            def __init__(self):
                super().__init__()
                self._gl_ctx = None

            def initializeGL(self):
                self._gl_ctx = self.context()

        widget = TestGLWidget()
        gl_format = QSurfaceFormat()
        gl_format.setVersion(3, 3)
        gl_format.setProfile(QSurfaceFormat.OpenGLContextProfile.CoreProfile)
        widget.setFormat(gl_format)
        widget.resize(100, 100)
        widget.show()
        app.processEvents()

        if widget._gl_ctx is None:
            logger.error("Failed to get GL context from test widget")
            return {"error": "No GL context"}

        # Now test the full GPU pipeline
        from src.features.edit.core.edit_gpu_preview_renderer import (
            EditGpuPreviewRenderer, EditRenderTask,
        )
        from src.features.edit.core.edit_gpu_decode_manager import EditGpuDecodeManager
        from src.shared.media.gpu_frame import is_gpu_decoded_frame, safe_release_gpu_frame

        # Create GPU render thread
        gpu_renderer = EditGpuPreviewRenderer(widget._gl_ctx)
        gpu_decode_manager = EditGpuDecodeManager()

        received_textures = []
        gpu_renderer.frame_ready.connect(
            lambda frame, gen, tid, w, h: received_textures.append((tid, w, h))
        )
        gpu_renderer.present_frame_ready.connect(
            lambda present: gpu_renderer.ack_present_slot(present.slot_id)
        )
        gpu_renderer.start()
        init_deadline = time.perf_counter() + 2.0
        while not gpu_renderer.is_initialized and time.perf_counter() < init_deadline:
            app.processEvents()
            time.sleep(0.005)
        if not gpu_renderer.is_initialized:
            gpu_renderer.stop()
            gpu_renderer.wait(3000)
            widget.close()
            return {"error": "GPU renderer did not initialize"}

        # Decode frames and submit
        decode_times: List[float] = []
        render_times: List[float] = []
        end_to_end_times: List[float] = []
        gpu_frames_count = 0
        planar_frames_count = 0

        for i in range(num_frames):
            t0 = time.perf_counter()
            frame, frame_type = gpu_decode_manager.get_frame(video_path, i)
            decode_dt = time.perf_counter() - t0

            if frame is None:
                continue

            decode_times.append(decode_dt * 1000.0)

            if frame_type in ('gpu', 'planar'):
                gpu_frames_count += 1
                if frame_type == 'planar':
                    planar_frames_count += 1
                layer = {
                    "frame_type": frame_type,
                    "track_info": {"opacity": 1.0},
                }
                if frame_type == 'gpu':
                    layer["gpu_frame"] = frame
                else:
                    layer["decoded_frame"] = frame
                task = EditRenderTask(
                    frame=i,
                    generation_id=0,
                    layers=[layer],
                    viewport_size=(1920, 1080),
                    output_fbo_size=(1920, 1080),
                )

                prev_count = len(received_textures)
                t1 = time.perf_counter()
                gpu_renderer.submit(task)

                # Wait for render to complete (with timeout)
                deadline = time.perf_counter() + 0.5
                while len(received_textures) == prev_count and time.perf_counter() < deadline:
                    app.processEvents()
                    time.sleep(0.001)

                render_dt = time.perf_counter() - t1
                render_times.append(render_dt * 1000.0)
                end_to_end_times.append((time.perf_counter() - t0) * 1000.0)
            else:
                safe_release_gpu_frame(frame)

        # Cleanup
        gpu_renderer.stop()
        gpu_renderer.wait(3000)
        gpu_decode_manager.close_all()
        widget.close()

        result = {
            "video": os.path.basename(video_path),
            "num_frames": num_frames,
            "gpu_frames": gpu_frames_count,
            "planar_frames": planar_frames_count,
            "textures_received": len(received_textures),
        }

        if decode_times:
            decode_steady = decode_times[min(5, max(0, len(decode_times) - 1)):] or decode_times
            result["decode_cold_ms"] = round(decode_times[0], 2)
            result["decode_steady_avg_ms"] = round(statistics.mean(decode_steady), 2)
            result["decode_steady_fps"] = round(len(decode_steady) / sum(decode_steady) * 1000, 1)

        if render_times:
            render_steady = render_times[min(5, max(0, len(render_times) - 1)):] or render_times
            result["render_steady_avg_ms"] = round(statistics.mean(render_steady), 2)
            result["render_steady_fps"] = round(len(render_steady) / sum(render_steady) * 1000, 1)

        if end_to_end_times:
            end_to_end_steady = end_to_end_times[min(5, max(0, len(end_to_end_times) - 1)):] or end_to_end_times
            result["end_to_end_cold_ms"] = round(end_to_end_times[0], 2)
            result["end_to_end_steady_avg_ms"] = round(statistics.mean(end_to_end_steady), 2)
            result["end_to_end_steady_p95_ms"] = round(
                sorted(end_to_end_steady)[round((len(end_to_end_steady) - 1) * 0.95)],
                2,
            )
            result["end_to_end_steady_fps"] = round(
                len(end_to_end_steady) / sum(end_to_end_steady) * 1000,
                1,
            )

        logger.info(f"  Full pipeline: gpu={gpu_frames_count}/{num_frames}, "
                     f"textures={len(received_textures)}, "
                     f"cold={result.get('end_to_end_cold_ms', 'N/A')}ms, "
                     f"steady={result.get('end_to_end_steady_avg_ms', 'N/A')}ms, "
                     f"p95={result.get('end_to_end_steady_p95_ms', 'N/A')}ms")

        return result

    except Exception as e:
        logger.error(f"Full pipeline bench error: {e}", exc_info=True)
        return {"error": str(e)}


# ================================================================
# Main
# ================================================================

def main():
    parser = argparse.ArgumentParser(description="Edit GPU Pipeline Benchmark")
    parser.add_argument("--video", type=str, help="Path to single test video")
    parser.add_argument("--all", action="store_true", help="Test all default videos")
    parser.add_argument("--frames", type=int, default=60, help="Number of frames to decode")
    parser.add_argument("--phase", choices=["decode", "manager", "full", "all"], default="all",
                        help="Which phase to benchmark")
    args = parser.parse_args()

    videos: List[str] = []
    if args.video:
        videos = [args.video]
    elif args.all:
        videos = DEFAULT_TEST_VIDEOS
    else:
        # Default: test one 4K H264
        videos = ["tests/test_videos/4K_H264.mov"]

    # Resolve paths
    resolved = []
    for v in videos:
        p = Path(v)
        if not p.is_absolute():
            p = PROJECT_ROOT / p
        if p.exists():
            resolved.append(str(p))
        else:
            logger.warning(f"Video not found: {v}")

    print("=" * 70)
    print("Edit Page GPU Pipeline Benchmark")
    print("=" * 70)

    all_results = []

    for video_path in resolved:
        print(f"\n{'─' * 70}")
        print(f"Video: {os.path.basename(video_path)}")
        print(f"{'─' * 70}")

        result: Dict[str, Any] = {"video": os.path.basename(video_path)}

        if args.phase in ("decode", "all"):
            print("\n--- Phase 1: Decode Benchmark ---")
            result["decode"] = bench_gpu_decode(video_path, args.frames)

        if args.phase in ("manager", "all"):
            print("\n--- Phase 2: EditGpuDecodeManager Benchmark ---")
            result["manager"] = bench_decode_manager(video_path, min(args.frames, 30))

        if args.phase in ("full", "all"):
            print("\n--- Phase 3: Full Pipeline Benchmark ---")
            result["full"] = bench_full_pipeline(video_path, min(args.frames, 30))

        all_results.append(result)

    # Summary
    print(f"\n{'=' * 70}")
    print("SUMMARY")
    print(f"{'=' * 70}")

    for r in all_results:
        name = r["video"]
        dec = r.get("decode", {})
        gpu_dec = dec.get("gpu_decode", {})
        cpu_dec = dec.get("cpu_decode", {})
        mgr = r.get("manager", {})
        full = r.get("full", {})

        print(f"\n{name}:")
        if gpu_dec:
            src = gpu_dec.get("source", "?")
            fps = gpu_dec.get("fps", "?")
            avg = gpu_dec.get("avg_ms", "?")
            ft = gpu_dec.get("frame_type", "?")
            print(f"  GPU Decode:   source={src}, type={ft}, avg={avg}ms, fps={fps}")
        else:
            print(f"  GPU Decode:   NOT AVAILABLE")

        if cpu_dec:
            fps = cpu_dec.get("fps", "?")
            avg = cpu_dec.get("avg_ms", "?")
            print(f"  CPU Decode:   avg={avg}ms, fps={fps}")

        if dec.get("speedup"):
            print(f"  Speedup:      {dec['speedup']}x")

        if mgr and "steady_avg_ms" in mgr:
            print(f"  DecodeMgr:    gpu={mgr.get('gpu_frames',0)} cpu={mgr.get('cpu_frames',0)}, "
                  f"cold={mgr.get('cold_first_frame_ms', '?')}ms, "
                  f"steady={mgr['steady_avg_ms']}ms, "
                  f"p95={mgr.get('steady_p95_ms', '?')}ms, "
                  f"fps={mgr.get('steady_fps', '?')}")

        if full and "error" not in full:
            print(f"  Full Pipeline: gpu={full.get('gpu_frames',0)}, textures={full.get('textures_received',0)}, "
                  f"cold={full.get('end_to_end_cold_ms','?')}ms, "
                  f"steady={full.get('end_to_end_steady_avg_ms','?')}ms, "
                  f"p95={full.get('end_to_end_steady_p95_ms','?')}ms, "
                  f"fps={full.get('end_to_end_steady_fps','?')}")
        elif full and "error" in full:
            print(f"  Full Pipeline: ERROR - {full['error']}")

    # Final verdict
    print(f"\n{'=' * 70}")
    gpu_working = any(
        r.get("decode", {}).get("gpu_decode") is not None
        for r in all_results
    )
    if gpu_working:
        print("GPU DECODE: WORKING")
        zero_copy = any(
            "zero_copy" in str(r.get("decode", {}).get("gpu_decode", {}).get("frame_type", ""))
            for r in all_results
        )
        if zero_copy:
            print("ZERO-COPY:  CONFIRMED (GpuDecodedFrame)")
        else:
            print("ZERO-COPY:  Not available (using hwaccel download)")
    else:
        planar_working = any(
            r.get("full", {}).get("planar_frames", 0) > 0
            for r in all_results
        )
        if planar_working:
            print("ZERO-COPY: DISABLED (safe VideoToolbox planar + GPU composite is working)")
        else:
            print("GPU DECODE: NOT AVAILABLE (CPU fallback will be used)")


if __name__ == "__main__":
    main()
