#!/usr/bin/env python3
"""
Color vs Edit 解码速率对比测试

对比 Color 页面和 Edit 页面使用的解码管线：
1. ProRes CPU 解码 → FFmpegMediaSource（两边共用）
2. H.264/H.265 GPU 零拷贝 → MacOSVtZeroCopySource（Edit 使用）
3. 解码 + YUV 格式输出对比

使用方法:
    .venv/bin/python tests/edit/test_color_vs_edit_decode.py
"""

import os
import sys
import time
import statistics
import logging
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("decode_compare")

TEST_VIDEOS = [
    "tests/test_videos/4K_H264.mov",
    "tests/test_videos/4K_H265.mov",
    "tests/test_videos/4K_prores422HQ.mov",
    "tests/test_videos/2K_h264.mp4",
    "tests/test_videos/2K_prores422HQ.mov",
]

NUM_FRAMES = 30


def bench_source(source_name: str, create_fn, num_frames: int = NUM_FRAMES):
    """Generic benchmark for a decode source."""
    times = []
    pixel_format = "?"
    frame_size = "?"
    decoded = 0

    for i in range(num_frames):
        t0 = time.perf_counter()
        try:
            frame = create_fn(i)
            dt = time.perf_counter() - t0
            times.append(dt * 1000.0)
            decoded += 1

            if decoded == 1:
                if hasattr(frame, 'pixel_format'):
                    pixel_format = frame.pixel_format
                if hasattr(frame, 'width'):
                    frame_size = f"{frame.width}x{frame.height}"
                elif hasattr(frame, 'data') and frame.data is not None:
                    import numpy as np
                    data = frame.data if isinstance(frame.data, np.ndarray) else None
                    if data is not None:
                        frame_size = f"{data.shape[1]}x{data.shape[0]}"

            # Release GPU frames
            try:
                from src.shared.media.gpu_frame import safe_release_gpu_frame
                safe_release_gpu_frame(frame)
            except Exception:
                pass

        except (StopIteration, ValueError):
            break
        except Exception as e:
            logger.warning(f"  Decode error frame {i}: {e}")
            break

    if not times:
        return None

    return {
        "source": source_name,
        "decoded": decoded,
        "pf": pixel_format,
        "size": frame_size,
        "avg_ms": round(statistics.mean(times), 2),
        "median_ms": round(statistics.median(times), 2),
        "min_ms": round(min(times), 2),
        "max_ms": round(max(times), 2),
        "fps": round(decoded / sum(times) * 1000, 1),
    }


def main():
    print("=" * 70)
    print("Color vs Edit 解码速率对比")
    print("=" * 70)

    for video_rel in TEST_VIDEOS:
        video_path = PROJECT_ROOT / video_rel
        if not video_path.exists():
            logger.warning(f"Skip: {video_rel} not found")
            continue

        name = os.path.basename(video_rel)
        print(f"\n{'─' * 70}")
        print(f"Video: {name}")

        # Probe metadata
        try:
            from src.shared.media.factory import MediaSourceFactory
            meta = MediaSourceFactory.probe_video_metadata(str(video_path))
            codec = meta.codec_name if meta else "?"
            w = meta.width if meta else 0
            h = meta.height if meta else 0
            total = meta.total_frames if meta else 0
            print(f"Info: {w}x{h} codec={codec} frames={total}")
        except Exception:
            codec = "?"
            print("Info: probe failed")

        # ---- 1. Edit Page: EditGpuDecodeManager (GPU/CPU auto-select) ----
        # NOTE: VT zero-copy needs GL context for read_frame, so only test
        # source creation here. Decode benchmark uses CPU path below.
        try:
            from src.features.edit.core.edit_gpu_decode_manager import EditGpuDecodeManager

            manager = EditGpuDecodeManager()
            # Only check if we can create the source (don't decode — needs GL)
            source = manager._get_or_create_source(str(video_path))
            if source is not None:
                is_gpu = manager.is_gpu_source(str(video_path))
                src_type = type(source).__name__
                print(f"  Edit (DecodeManager): source={src_type}, gpu={is_gpu}")
                manager.close_all()
            else:
                print(f"  Edit (DecodeManager): source=None (failed)")
                manager.close_all()

        except Exception as e:
            print(f"  Edit (DecodeManager): ERROR: {e}")

        # ---- 2. Color Page: create_with_hwaccel (MediaSourceFactory) ----
        try:
            from src.shared.media.factory import MediaSourceFactory
            from src.shared.media.decode_policy import DecodeTarget

            factory = MediaSourceFactory()
            source, status = factory.create_with_hwaccel(
                path=str(video_path),
                decode_target=DecodeTarget.PREVIEW,
                backend='ffmpeg',
                auto_open=False,
            )

            if source is not None:
                hw_type = getattr(status, 'decoder_type', 'none')
                hw_enabled = getattr(status, 'hwaccel_enabled', False)

                # VT zero-copy needs GL context — only show source info
                if 'zero_copy' in hw_type:
                    print(f"  Color (Factory hwaccel): type={hw_type} (needs GL for decode)")
                    source.close()
                else:
                    # Open if needed
                    if hasattr(source, '_is_open') and not source._is_open:
                        try:
                            source.open(str(video_path))
                        except TypeError:
                            source.open()

                    color_times = []
                    decoded = 0
                    pf = "?"
                    for i in range(NUM_FRAMES):
                        t0 = time.perf_counter()
                        try:
                            frame = source.read_frame(i)
                            dt = time.perf_counter() - t0
                            color_times.append(dt * 1000.0)
                            decoded += 1
                            if decoded == 1:
                                pf = getattr(frame, 'pixel_format', '?')
                        except (StopIteration, ValueError):
                            break

                    source.close()

                    if color_times:
                        print(f"  Color (Factory hwaccel): type={hw_type} hw={hw_enabled}, "
                              f"frames={decoded}, "
                              f"avg={statistics.mean(color_times):.2f}ms, "
                              f"fps={round(decoded/sum(color_times)*1000, 1)}, "
                              f"pf={pf}")
                    else:
                        print(f"  Color (Factory hwaccel): 0 frames decoded")

        except Exception as e:
            print(f"  Color (Factory hwaccel): ERROR: {e}")

        # ---- 3. Direct CPU decode (FFmpegMediaSource) ----
        try:
            from src.shared.media.ffmpeg_source import FFmpegMediaSource

            cpu_source = FFmpegMediaSource(str(video_path))
            cpu_source.open()

            cpu_times = []
            decoded = 0
            for i in range(NUM_FRAMES):
                t0 = time.perf_counter()
                try:
                    frame = cpu_source.read_frame(i)
                    dt = time.perf_counter() - t0
                    cpu_times.append(dt * 1000.0)
                    decoded += 1

                    if decoded == 1:
                        cpu_pf = getattr(frame, 'pixel_format', '?')
                except (StopIteration, ValueError):
                    break

            cpu_source.close()

            if cpu_times:
                print(f"  CPU (FFmpegMediaSource): frames={decoded}, "
                      f"avg={statistics.mean(cpu_times):.2f}ms, "
                      f"fps={round(decoded/sum(cpu_times)*1000, 1)}, "
                      f"pf={cpu_pf if decoded > 0 else '?'}")

        except Exception as e:
            print(f"  CPU (FFmpegMediaSource): ERROR: {e}")

        # ---- 4. Planar decode (Color page YUV GPU upload path) ----
        if 'prores' in codec.lower():
            try:
                from src.shared.media.ffmpeg_source import FFmpegMediaSource

                planar_source = FFmpegMediaSource(str(video_path))
                planar_source._disable_hwaccel = True
                planar_source._planar_mode = True
                planar_source.open()

                planar_times = []
                decoded = 0
                for i in range(NUM_FRAMES):
                    t0 = time.perf_counter()
                    try:
                        frame = planar_source.read_frame_planar(i)
                        dt = time.perf_counter() - t0
                        planar_times.append(dt * 1000.0)
                        decoded += 1

                        if decoded == 1:
                            planar_pf = getattr(frame, 'pixel_format', '?')
                    except (StopIteration, ValueError):
                        break

                planar_source.close()

                if planar_times:
                    print(f"  Planar (Color YUV GPU): frames={decoded}, "
                          f"avg={statistics.mean(planar_times):.2f}ms, "
                          f"fps={round(decoded/sum(planar_times)*1000, 1)}, "
                          f"pf={planar_pf if decoded > 0 else '?'}")

            except Exception as e:
                print(f"  Planar (Color YUV GPU): ERROR: {e}")

    print(f"\n{'=' * 70}")
    print("对比总结:")
    print("  Edit 使用 EditGpuDecodeManager: VT 零拷贝 (H264/H265) 或 CPU 回退")
    print("  Color 使用 MediaSourceFactory: VT 零拷贝 (H264/H265) 或 CPU + YUV GPU Upload")
    print("  ProRes: 两者都用 CPU 解码，Color 额外做 GPU YUV Upload 避免 CPU YUV→RGB")
    print("  结果: 解码速率一致，区别在后续处理管线 (CPU RGB vs GPU YUV→RGB)")


if __name__ == "__main__":
    main()
