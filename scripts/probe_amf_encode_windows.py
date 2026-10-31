#!/usr/bin/env python3
"""W6-02（AMD 半边）：AMF 硬编导出实测——真实 open + 编码 + x264 对照。

协议（计划 §4 / P0-06）：
- 注册存在 ≠ 驱动可用（``probe_hwaccel_encoder_available`` 只查注册）；
  **可用性裁决 = CodecContext 真实 open + 编出可解码码流**。
- 每个编码器产出：open 结果、编码帧数、码流可回解帧数、码率、
  与 CPU 基线（libx264/libx265 同码率）的 luma PSNR 对照。
- RDNA2（RX 6800）无 AV1 硬编单元（RDNA3+ 才有）：av1_amf 预期 open
  失败——该行是「显式诊断回退 CPU」的真实案例，不是错误。

素材：1280×720 渐变运动序列 30 帧（yuv420p 走 nv12；hevc 10-bit 走
p010le），确定性生成，AMF 与 CPU 基线编码同一份帧。

产物（P9C-02）：``tests/media/results/windows_amf_encode_probe_amd.{md,json}``

用法：.venv/Scripts/python.exe scripts/probe_amf_encode_windows.py
"""

from __future__ import annotations

import json
import sys
import tempfile
import time
from dataclasses import dataclass, field
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
    hwaccel: str
    codec: str
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
    row = Row(encoder=encoder_name, hwaccel="amf" if "amf" in encoder_name else "cpu",
              codec="hevc" if "265" in encoder_name or "hevc" in encoder_name else
              ("av1" if "av1" in encoder_name else "h264"),
              pix_fmt=pix_fmt, bit_depth=10 if "10" in pix_fmt else 8,
              registered=encoder_name in av.codecs_available)
    out_path = Path(tempfile.gettempdir()) / f"amf_probe_{tag}.mp4"
    try:
        container = av.open(str(out_path), "w")
        stream = container.add_stream(encoder_name, rate=FPS)
        stream.width = WIDTH
        stream.height = HEIGHT
        stream.pix_fmt = pix_fmt
        stream.bit_rate = BIT_RATE
        t0 = time.perf_counter()
        for src in frames:
            # AMF 输入 nv12；x264/x265 输入 yuv420p——按 pix_fmt 直接喂
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
        ("h264_amf", "nv12", "libx264", False),
        ("hevc_amf", "nv12", "libx265", False),
        ("hevc_amf_10bit", "p010le", "libx265", True),
        ("av1_amf", "nv12", "libsvtav1", False),
    ]
    rows: list[Row] = []
    for tag, (hw_enc, pix, cpu_enc, ten_bit) in enumerate(plan):
        name_10 = "hevc_amf"
        encoder = name_10 if hw_enc == "hevc_amf_10bit" else hw_enc
        label = hw_enc
        frames = [make_frame(i, ten_bit) for i in range(N_FRAMES)]

        reg = probe_hwaccel_encoder_available(encoder)
        print(f"[{label}] registered={reg} encoding ...", flush=True)
        out_path, row = encode(encoder, pix, frames, tag=f"{label}_{pix}")
        row.registered = reg
        row.baseline_encoder = cpu_enc

        if out_path is not None:
            n, mbps, psnr = decode_and_psnr(out_path, frames, ten_bit)
            row.decoded_frames = n
            row.out_bitrate_mbps = round(mbps, 2)
            row.luma_psnr_db = psnr
            if n == 0:
                row.verdict = "bitstream_undecodable"
            # CPU 基线（同帧、同码率目标；x264 ABR / x265 ABR）
            base_pix = "yuv420p10le" if ten_bit else "yuv420p"
            base_frames = []
            for i, src in enumerate(frames):
                f = av.VideoFrame(WIDTH, HEIGHT, format=base_pix)
                dtype = np.uint16 if ten_bit else np.uint8
                f.planes[0].update(bytes(src.planes[0]))
                # 平面数不同（yuv420p 3 平面 vs nv12 2 平面）：重建 U/V
                cw, ch = WIDTH // 2, HEIGHT // 2
                u = np.full((ch, cw), 512 if ten_bit else 128, dtype=dtype)
                v = np.full((ch, cw), 512 if ten_bit else 128, dtype=dtype)
                f.planes[1].update(u.tobytes())
                f.planes[2].update(v.tobytes())
                base_frames.append(f)
            base_path, base_row = encode(cpu_enc, base_pix, base_frames,
                                         tag=f"{label}_cpu")
            if base_path is not None:
                bn, bmbps, bpsnr = decode_and_psnr(base_path, base_frames, ten_bit)
                row.baseline_psnr_db = bpsnr
                if bn < n:
                    row.detail = (f"{row.detail}; baseline decoded {bn}/{n}").strip("; ")
        rows.append(row)

    gpu_line = ""
    try:
        from src.shared.media.windows_hw.gpu_capability import probe_gpu_capability
        g = probe_gpu_capability()
        gpu_line = "; ".join(a.description for a in g.adapters)
    except Exception:
        gpu_line = "<probe unavailable>"

    report = {
        "date": datetime.now().isoformat(timespec="seconds"),
        "plan_item": "W6-02（AMD 半边）：AMF 硬编导出实测",
        "machine": {"gpu": gpu_line, "pyav": av.__version__},
        "protocol": ("可用性裁决 = 真实 open + 编码 + 码流可回解；PSNR 为 "
                     f"{N_FRAMES} 帧 {WIDTH}x{HEIGHT} 渐变序列 luma 值，"
                     f"目标码率 {BIT_RATE // 1_000_000} Mbps（x264/x265 为 ABR）"),
        "rows": [r.as_dict() for r in rows],
    }
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    json_path = RESULTS_DIR / "windows_amf_encode_probe_amd.json"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                         encoding="utf-8")

    md = ["# AMF 硬编导出实测（W6-02 AMD 半边，本机 RX 6800）", "",
          f"日期：{report['date']} ｜ GPU：{gpu_line} ｜ PyAV {av.__version__}",
          "",
          "| 编码器 | 格式 | 注册 | 裁决 | 编码耗时 | 回解帧 | 实测码率 | "
          "PSNR(dB) | 基线 PSNR(dB) |",
          "|---|---|---|---|---|---|---|---|---|"]
    for r in rows:
        md.append(
            f"| {r.encoder} | {r.pix_fmt} | {r.registered} | {r.verdict} "
            f"| {r.encode_ms or '—'} | {r.decoded_frames} "
            f"| {r.out_bitrate_mbps or '—'} Mbps | {r.luma_psnr_db or '—'} "
            f"| {r.baseline_psnr_db or '—'} |")
    md += ["", "## detail", ""]
    for r in rows:
        if r.detail:
            md.append(f"- {r.encoder}/{r.pix_fmt}: {r.detail}")
    md_path = RESULTS_DIR / "windows_amf_encode_probe_amd.md"
    md_path.write_text("\n".join(md), encoding="utf-8")
    print(f"\nwritten: {json_path}\nwritten: {md_path}")
    for r in rows:
        print(f"  {r.encoder:14s} {r.pix_fmt:10s} -> {r.verdict:26s} "
              f"psnr={r.luma_psnr_db} cpu={r.baseline_psnr_db} {r.detail[:80]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
