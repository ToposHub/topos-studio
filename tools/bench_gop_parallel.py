#!/usr/bin/env python3
"""GOP 分段并行编码端到端实测（ADR-C050 / gate review §5.6）。

口径：
- 完整产品路径：ToposVideoEncoder(gop='ip2') → MOV（open/encode_frame×N/close）；
  帧源 PyAV 解码与平面装配含入计时（导出现场的真实成本）；
- 对照：gop_size=0（单 GOP 全片，串行基线）vs gop_size=16 × workers 1/2/4；
- 输出：wall 秒、编码 fps、文件大小；另开 ToposMediaSource 全片解一遍验证
  可读（解帧数 == 编帧数）。
"""
from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from src.shared.export.topos_encoder import ToposVideoEncoder  # noqa: E402
from src.shared.export.video_encoder import FFmpegEncoderConfig  # noqa: E402
from src.shared.media.factory import MediaSourceFactory  # noqa: E402
from src.shared.media.metadata import Rational  # noqa: E402
from src.shared.media.frame import DecodedFrame  # noqa: E402

N = 64
CASES = [
    ("pan_2k", REPO / "tests/test_videos/2K_prores422HQ.mov", 226, 1920, 1080),
    ("static_4k", REPO / "tests/test_videos/4K_prores422HQ.mov", 425, 3840, 2160),
]
VARIANTS = [
    ("单GOP 串行(基线)", 0, 1),
    ("分段16 w=1", 16, 1),
    ("分段16 w=2", 16, 2),
    ("分段16 w=4", 16, 4),
]


def extract(src_path: Path, start: int, n: int, w: int, h: int):
    import av
    cont = av.open(str(src_path))
    frames = []
    for i, f in enumerate(cont.decode(video=0)):
        if i >= start:
            planes = []
            for p in f.planes:
                arr = np.frombuffer(ctypes_string_at(p.buffer_ptr, p.buffer_size),
                                    dtype="<u2").reshape(p.height, p.line_size // 2)
                planes.append(arr[:f.height, :p.width].copy())
            y, u, v = planes
            frames.append(DecodedFrame(
                frame_index=len(frames), pts=len(frames),
                time_seconds=len(frames) / 25.0,
                planes=(y, u, v), pixel_format='yuv422p10le',
                width=w, height=h))
            if len(frames) >= n:
                break
    cont.close()
    assert len(frames) == n
    return frames


def ctypes_string_at(ptr, size):
    import ctypes
    return ctypes.string_at(ptr, size)


def run(tag: str, src: Path, start: int, w: int, h: int,
        gop_size: int, workers: int, qp: int = 44) -> None:
    frames = extract(src, start, N, w, h)
    out = tempfile.mktemp(suffix=".mov")
    cfg = FFmpegEncoderConfig(
        codec='topos', width=w, height=h, fps=Rational(25, 1),
        pix_fmt='yuv422p10le', crf=qp, gop='ip2',
        gop_size=gop_size, gop_workers=workers)
    t0 = time.perf_counter()
    enc = ToposVideoEncoder(out, cfg)
    enc.open()
    for f in frames:
        enc.encode_frame(f)
    enc.close()
    dt = time.perf_counter() - t0
    size = os.path.getsize(out)
    # 可读性验证：产品解码路径全片解一遍
    srcm = MediaSourceFactory.create(out)
    srcm.open()
    total = srcm.probe().total_frames
    got = 0
    for _ in range(total):
        srcm.read_frame(got)
        got += 1
    srcm.close()
    ok = "OK" if got == N else f"FAIL({got})"
    print(f"[{tag:<10}] {w}x{h} qp{qp} gop_size={gop_size:<2} w={workers}  "
          f"wall {dt:6.2f}s  enc {N/dt:6.1f} fps  {size/1024:7.0f}KB  decode {ok}")
    os.unlink(out)


def main() -> None:
    import sys
    qp = int(sys.argv[1]) if len(sys.argv) > 1 else 44
    for tag, src, start, w, h in CASES:
        for label, gs, wk in VARIANTS:
            run(f"{tag}", src, start, w, h, gs, wk, qp=qp)
        print()


if __name__ == "__main__":
    main()
