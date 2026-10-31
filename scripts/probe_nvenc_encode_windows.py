#!/usr/bin/env python3
"""W6-01：NVENC 硬编导出实测——真实 open + 编码 + x264/x265 对照。

协议（计划 §4 / P0-06，同 AMF probe 先例）：
- 注册存在 ≠ 驱动可用（``probe_hwaccel_encoder_available`` 只查注册）；
  **可用性裁决 = CodecContext 真实 open + 编出可解码码流**。
- 每个编码器产出：open 结果、编码帧数、码流可回解帧数、码率、
  与 CPU 基线（libx264/libx265/libsvtav1 同码率）的 luma PSNR 对照。
- 诚实边界：合成渐变素材码率不跑满，PSNR 只证码流 sane；
  同码率画质 A/B 结论需真实素材（W6-01 正式验收项）。

素材：1280×720 渐变运动序列 30 帧（8bit 走 nv12；10bit 走 p010le），
确定性生成，NVENC 与 CPU 基线编码同一份帧。

产物（P9C-02）：``tests/media/results/windows_nvenc_encode_probe_nvidia.{md,json}``

用法：.venv/Scripts/python.exe scripts/probe_nvenc_encode_windows.py
"""

from __future__ import annotations

import json
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import av
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

ROOT = Path(__file__).resolve().parents[1]
RESULTS_DIR = ROOT / "tests" / "media" / "results"

WIDTH, HEIGHT, N_FRAMES = 1280, 720, 30
BIT_RATE = 8_000_000
FPS = 24


@dataclass
class Row:
    encoder: str
    pix_fmt: str
    bit_depth: int
    registered: bool
    verdict: str = ""
    encode_ms: float = 0.0
    packets: int = 0
    decoded_frames: int = 0
    out_bitrate_mbps: float = 0.0
    luma_psnr_db: float = 0.0
    baseline_encoder: str = ""
    baseline_psnr_db: float = 0.0
    detail: str = ""

    def as_dict(self) -> dict:
        return self.__dict__.copy()


def make_frame(i: int, ten_bit: bool) -> av.VideoFrame:
    """确定性渐变运动帧（nv12/p010le 双平面布局）。"""
    fmt = "p010le" if ten_bit else "nv12"
    frame = av.VideoFrame(WIDTH, HEIGHT, format=fmt)
    dtype = np.uint16 if ten_bit else np.uint8
    max_val = 1023 if ten_bit else 255
    yy, xx = np.mgrid[0:HEIGHT, 0:WIDTH]
    y = ((xx + i * 11) * 255 // max(WIDTH, 1) + (i * 37)) % (max_val + 1)
    frame.planes[0].update(y.astype(dtype).tobytes())
    uv_size = frame.planes[1].buffer_size
    if ten_bit:
        neutral = np.full(uv_size // 2, 512 << 6, dtype=np.uint16)
        frame.planes[1].update(neutral.tobytes())
    else:
        frame.planes[1].update(np.full(uv_size, 128, dtype=np.uint8).tobytes())
    return frame


def encode(encoder_name: str, pix_fmt: str, frames: list[av.VideoFrame],
           tag: str) -> tuple[Path | None, Row]:
    row = Row(encoder=encoder_name, pix_fmt=pix_fmt,
              bit_depth=10 if "10" in pix_fmt else 8,
              registered=encoder_name in av.codecs_available)
    out_path = Path(tempfile.gettempdir()) / f"nvenc_probe_{tag}.mp4"
    try:
        container = av.open(str(out_path), "w")
        stream = container.add_stream(encoder_name, rate=FPS)
        stream.width = WIDTH
        stream.height = HEIGHT
        stream.pix_fmt = pix_fmt
        stream.bit_rate = BIT_RATE
        t0 = time.perf_counter()
        for src in frames:
            for packet in stream.encode(src):
                container.mux(packet)
        for packet in stream.encode():
            container.mux(packet)
        container.close()
        row.encode_ms = round((time.perf_counter() - t0) * 1000, 1)
        row.verdict = "encoded"
    except Exception as exc:
        row.verdict = "open_or_encode_failed"
        row.detail = f"{type(exc).__name__}: {exc}"[:220]
        return None, row
    return out_path, row


def decode_and_psnr(path: Path, frames: list[av.VideoFrame],
                    ten_bit: bool) -> tuple[int, float, float]:
    """回解码：帧数、实测码率、对源帧的 luma PSNR（同索引对齐取 min）。"""
    dtype = np.uint16 if ten_bit else np.uint8
    peak = float(np.iinfo(dtype).max)
    src_y = [f.planes[0].to_bytes() if hasattr(f.planes[0], "to_bytes")
             else bytes(f.planes[0]) for f in frames]
    psnrs: list[float] = []
    n = 0
    container = av.open(str(path))
    total_bytes = path.stat().st_size
    for dec in container.decode(video=0):
        if n >= len(src_y):
            break
        y_ref = np.frombuffer(src_y[n], dtype=dtype).astype(np.float64)
        y_out = np.frombuffer(bytes(dec.planes[0]), dtype=dtype)[:y_ref.size]
        y_out = y_out[:y_ref.size].astype(np.float64)
        mse = float(((y_ref - y_out) ** 2).mean())
        psnrs.append(10 * np.log10(peak * peak / mse) if mse > 0 else 99.0)
        n += 1
    container.close()
    duration = len(frames) / FPS
    return n, total_bytes * 8 / duration / 1e6, round(float(np.mean(psnrs)), 2)


def main() -> int:
    from src.shared.export.encoder import probe_hwaccel_encoder_available

    plan = [
        # (硬编, 输入格式, CPU 基线, ten_bit)
        ("h264_nvenc", "nv12", "libx264", False),
        ("hevc_nvenc", "nv12", "libx265", False),
        ("hevc_nvenc", "p010le", "libx265", True),
        ("av1_nvenc", "nv12", "libsvtav1", False),
        ("av1_nvenc", "p010le", "libsvtav1", True),
    ]
    rows: list[Row] = []
    for idx, (hw_enc, pix, cpu_enc, ten_bit) in enumerate(plan):
        label = f"{hw_enc}_{pix}"
        frames = [make_frame(i, ten_bit) for i in range(N_FRAMES)]

        reg = probe_hwaccel_encoder_available(hw_enc)
        print(f"[{label}] registered={reg} encoding ...", flush=True)
        out_path, row = encode(hw_enc, pix, frames, tag=f"{idx}_{pix}")
        row.registered = reg
        row.baseline_encoder = cpu_enc

        if out_path is not None:
            n, mbps, psnr = decode_and_psnr(out_path, frames, ten_bit)
            row.decoded_frames = n
            row.out_bitrate_mbps = round(mbps, 2)
            row.luma_psnr_db = psnr
            if n == 0:
                row.verdict = "bitstream_undecodable"
            # CPU 基线（同帧、同码率目标；x264/x265/svtav1 ABR）
            base_pix = "yuv420p10le" if ten_bit else "yuv420p"
            base_frames = []
            for i, src in enumerate(frames):
                f = av.VideoFrame(WIDTH, HEIGHT, format=base_pix)
                dtype = np.uint16 if ten_bit else np.uint8
                f.planes[0].update(bytes(src.planes[0]))
                cw, ch = WIDTH // 2, HEIGHT // 2
                u = np.full((ch, cw), 512 if ten_bit else 128, dtype=dtype)
                v = np.full((ch, cw), 512 if ten_bit else 128, dtype=dtype)
                f.planes[1].update(u.tobytes())
                f.planes[2].update(v.tobytes())
                base_frames.append(f)
            base_path, base_row = encode(cpu_enc, base_pix, base_frames,
                                         tag=f"{idx}_cpu")
            if base_path is not None:
                bn, bmbps, bpsnr = decode_and_psnr(base_path, base_frames,
                                                   ten_bit)
                row.baseline_psnr_db = bpsnr
                if bn < n:
                    row.detail = (f"{row.detail}; baseline decoded {bn}/{n}"
                                  ).strip("; ")
        rows.append(row)

    gpu_line = ""
    try:
        from src.shared.media.windows_hw.gpu_capability import (
            probe_gpu_capability,
        )
        g = probe_gpu_capability()
        gpu_line = "; ".join(a.description for a in g.adapters)
    except Exception:
        gpu_line = "<probe unavailable>"

    report = {
        "date": datetime.now().isoformat(timespec="seconds"),
        "plan_item": "W6-01：NVENC 硬编导出实测（h264/hevc/av1_nvenc）",
        "machine": {"gpu": gpu_line, "pyav": av.__version__},
        "protocol": ("可用性裁决 = 真实 open + 编码 + 码流可回解；PSNR 为 "
                     f"{N_FRAMES} 帧 {WIDTH}x{HEIGHT} 渐变序列 luma 值，"
                     f"目标码率 {BIT_RATE // 1_000_000} Mbps（CPU 基线 ABR）"
                     "；合成素材码率不跑满，PSNR 只证码流 sane"),
        "rows": [r.as_dict() for r in rows],
    }
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    json_path = RESULTS_DIR / "windows_nvenc_encode_probe_nvidia.json"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                         encoding="utf-8")

    md = ["# NVENC 硬编导出实测（W6-01，NVIDIA 真机）", "",
          f"日期：{report['date']} ｜ GPU：{gpu_line} ｜ PyAV {av.__version__}",
          "",
          "| 编码器 | 格式 | 注册 | 裁决 | 编码耗时 | 回解帧 | 实测码率 | "
          "PSNR(dB) | 基线 | 基线 PSNR(dB) |",
          "|---|---|---|---|---|---|---|---|---|---|"]
    for r in rows:
        md.append(
            f"| {r.encoder} | {r.pix_fmt} | {r.registered} | {r.verdict} "
            f"| {r.encode_ms or '—'} | {r.decoded_frames} "
            f"| {r.out_bitrate_mbps or '—'} Mbps | {r.luma_psnr_db or '—'} "
            f"| {r.baseline_encoder} | {r.baseline_psnr_db or '—'} |")
    md += ["", "## detail", ""]
    for r in rows:
        if r.detail:
            md.append(f"- {r.encoder}/{r.pix_fmt}: {r.detail}")
    md += ["", "## 诚实边界", "",
           "- 可用性裁决 = 真实 open + 编码 + 回解（P0-06）；同码率画质 "
           "A/B 与参数域（rate control/B 帧/lookahead）调优需真实素材，"
           "合成渐变素材码率不跑满，PSNR 只证码流 sane。",
           ""]
    md_path = RESULTS_DIR / "windows_nvenc_encode_probe_nvidia.md"
    md_path.write_text("\n".join(md), encoding="utf-8")
    print(f"\nwritten: {json_path}\nwritten: {md_path}")
    for r in rows:
        print(f"  {r.encoder:10s} {r.pix_fmt:8s} -> {r.verdict:24s} "
              f"psnr={r.luma_psnr_db} cpu={r.baseline_psnr_db} "
              f"{r.detail[:70]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
