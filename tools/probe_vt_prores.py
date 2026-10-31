#!/usr/bin/env python3
"""探针：VideoToolbox 解码 ProRes 的速度与输出格式。

注意 require_zero_copy 必须为 True：False 时 source.open 走 capability
兜底的硬编码 codec 集合（不含 prores），会误报 unsupported_codec。
实测 4K ProRes 422 HQ：18.3ms/帧（54.7fps），输出 p010。
"""
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

VIDEO = sys.argv[1] if len(sys.argv) > 1 else REPO_ROOT / "tests/test_videos/4K_prores422HQ.mov"

from src.shared.media.macos_vt.source import MacOSVtZeroCopySource

src = MacOSVtZeroCopySource()
ok = src.open(str(VIDEO), require_zero_copy=True)
print(f"open={ok} reason={src.fallback_reason}")
if not ok:
    sys.exit(1)
print(f"info: {src._info.width}x{src._info.height} fps={getattr(src._info, 'fps', '?')} "
      f"codec={getattr(src, '_codec', '?')}")

frames = 0
t0 = time.perf_counter()
fmt = None
for i in range(30):
    f = src.read_frame_pixel_buffer(i)
    if f is None:
        print(f"frame {i} -> None")
        continue
    if fmt is None:
        fmt = (f.pixel_format, f.bit_depth, f.width, f.height)
    f.release()
    frames += 1
dt = time.perf_counter() - t0
print(f"decoded {frames} frames in {dt*1000:.0f}ms -> {dt/max(frames,1)*1000:.1f}ms/frame "
      f"({frames/max(dt,1e-9):.1f} fps)")
print(f"pixel_format={fmt[0]} bit_depth={fmt[1]} size={fmt[2]}x{fmt[3]}")
src.close()
