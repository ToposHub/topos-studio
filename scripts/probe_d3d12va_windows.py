#!/usr/bin/env python3
"""开放问题 5 跟进：d3d12va 解码后端实测（对照 d3d11va，本机 AMD RX 6800）。

背景：W1-04 矩阵发现 AV1 在 d3d11va hwaccel 下静默软解；本 PyAV 构建
（18.1.0）存在 ``av1_d3d12va``/``h264_d3d12va``/``hevc_d3d12va`` 命名
解码器（而 ``av1_d3d11va`` 命名解码器不存在——旧静默软解的结构性解释）。
本探针三路取证：

1. **HWAccel API**（与 W1-04 同协议可比）：``HWAccel(device_type)`` +
   ``av.open(hwaccel=...)``，输出 ∈ {nv12,p010le,p016le} 才算硬解生效；
   另跑 ``allow_software_fallback=False`` 严格模式取显式错误。
2. **命名 wrapper 解码器**：``CodecContext.create('av1_d3d12va','r')`` +
   手工喂包——wrapper 无软解回退，输出格式 'd3d12'（hw 帧）即生效，
   失败即显式异常。
3. d3d11va 对照行（同素材同脚本重跑，消除跨脚本变量）。

产物（P9C-02）：``tests/media/results/windows_d3d12va_probe_amd.{md,json}``

用法：.venv/Scripts/python.exe scripts/probe_d3d12va_windows.py
"""

from __future__ import annotations

import json
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import av
from av.codec.hwaccel import HWAccel, HWDeviceType

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

ROOT = Path(__file__).resolve().parents[1]
RESULTS_DIR = ROOT / "tests" / "media" / "results"

HW_FORMATS = {"nv12", "p010le", "p016le"}
PROBE_FRAMES = 24


@dataclass
class Row:
    route: str        # hwaccel_api | hwaccel_strict | wrapper_decoder
    backend: str      # d3d12va | d3d11va | <decoder name>
    fixture: str
    source: str
    codec: str
    bit_depth: int
    width: int
    height: int
    verdict: str
    out_format: str = ""
    fps: float = 0.0
    detail: str = ""

    def as_dict(self) -> dict:
        return self.__dict__.copy()


def make_synthetic(encoder: str, pix_fmt: str, name: str,
                   width: int = 1280, height: int = 720,
                   frames: int = 48) -> Path | None:
    """现场编码合成 AV1 诊断素材（平面 update 方式，10-bit 用 uint16）。"""
    import numpy as np
    import tempfile

    out_path = Path(tempfile.gettempdir()) / name
    ten_bit = "10le" in pix_fmt
    try:
        container = av.open(str(out_path), "w")
        stream = container.add_stream(encoder, rate=24)
        stream.width = width
        stream.height = height
        stream.pix_fmt = pix_fmt
        for i in range(frames):
            frame = av.VideoFrame(width, height, format=pix_fmt)
            dtype = np.uint16 if ten_bit else np.uint8
            y = np.full((height, width), (i * 37) % (1024 if ten_bit else 256),
                        dtype=dtype)
            cw, ch = width // 2, height // 2
            u = np.full((ch, cw), 512 if ten_bit else 128, dtype=dtype)
            v = np.full((ch, cw), 512 if ten_bit else 128, dtype=dtype)
            frame.planes[0].update(y.tobytes())
            frame.planes[1].update(u.tobytes())
            frame.planes[2].update(v.tobytes())
            for packet in stream.encode(frame):
                container.mux(packet)
        for packet in stream.encode():
            container.mux(packet)
        container.close()
        return out_path
    except Exception as exc:
        print(f"  [synthetic] {encoder}/{pix_fmt} unavailable: {exc}")
        return None


def probe_hwaccel(path: Path, device_type, *, strict: bool, meta: dict,
                  backend_label: str, route: str) -> Row:
    row = Row(route=route, backend=backend_label, fixture=path.name,
              source=meta["source"], codec=meta["codec"],
              bit_depth=meta["bit_depth"], width=meta["width"],
              height=meta["height"], verdict="")
    try:
        accel = HWAccel(device_type, allow_software_fallback=not strict)
        with av.open(str(path), hwaccel=accel) as container:
            frames = container.decode(video=0)
            t0 = time.perf_counter()
            n, out_fmt = 0, ""
            for frame in frames:
                out_fmt = frame.format.name
                n += 1
                if n >= PROBE_FRAMES:
                    break
            dt = time.perf_counter() - t0
        row.out_format = out_fmt
        row.fps = round(n / dt, 1) if dt > 0 else 0.0
        if out_fmt in HW_FORMATS:
            row.verdict = "supported"
        elif strict:
            row.verdict = "strict_negative"
            row.detail = f"session ok but output {out_fmt} (no explicit raise)"
        else:
            row.verdict = "silent_sw_fallback"
            row.detail = f"session ok but output {out_fmt} (software)"
    except Exception as exc:
        row.verdict = "error" if strict else "error"
        row.detail = f"{type(exc).__name__}: {exc}"[:200]
    return row


def probe_wrapper(decoder_name: str, path: Path, meta: dict) -> Row:
    row = Row(route="wrapper_decoder", backend=decoder_name,
              fixture=path.name, source=meta["source"], codec=meta["codec"],
              bit_depth=meta["bit_depth"], width=meta["width"],
              height=meta["height"], verdict="")
    try:
        ctx = av.codec.CodecContext.create(decoder_name, "r")
        n, out_fmt = 0, ""
        with av.open(str(path)) as container:
            stream = container.streams.video[0]
            for packet in container.demux(stream):
                for frame in ctx.decode(packet):
                    out_fmt = frame.format.name
                    n += 1
                    if n >= PROBE_FRAMES:
                        break
                if n >= PROBE_FRAMES:
                    break
        row.out_format = out_fmt
        row.verdict = ("supported" if out_fmt in HW_FORMATS or "d3d12" in out_fmt
                       else f"unexpected_output_{out_fmt}")
        row.detail = f"frames={n}"
    except Exception as exc:
        row.verdict = "error"
        row.detail = f"{type(exc).__name__}: {exc}"[:200]
    return row


def main() -> int:
    from src.shared.media.windows_hw.gpu_capability import probe_gpu_capability

    gpu = probe_gpu_capability()
    print(f"vendor={gpu.primary_vendor} "
          f"adapters={[a.description for a in gpu.adapters]}", flush=True)

    tv = ROOT / "tests" / "test_videos"
    real = [
        (tv / "2K_h264.mp4", dict(source="real", codec="h264", bit_depth=8,
                                  width=1920, height=1080)),
        (tv / "4K_H264.mov", dict(source="real", codec="h264", bit_depth=8,
                                  width=3840, height=2160)),
        (tv / "2K_H265.mov", dict(source="real", codec="hevc", bit_depth=10,
                                  width=1920, height=1080)),
        (tv / "4K_H265.mov", dict(source="real", codec="hevc", bit_depth=10,
                                  width=3840, height=2160)),
    ]
    real = [(p, m) for p, m in real if p.exists()]

    av1_enc = next((c for c in ("libsvtav1", "libaom-av1", "librav1e")
                    if c in av.codecs_available), None)
    synth: list[tuple[Path, dict]] = []
    if av1_enc:
        for pix, depth, name in (("yuv420p", 8, "diag_av1_8bit_720p_d3d12.mp4"),
                                 ("yuv420p10le", 10, "diag_av1_10bit_720p_d3d12.mp4")):
            p = make_synthetic(av1_enc, pix, name)
            if p and p.exists():
                synth.append((p, dict(source="synthetic", codec="av1",
                                      bit_depth=depth, width=1280, height=720)))

    rows: list[Row] = []
    for backend, dev in (("d3d12va", HWDeviceType.d3d12va),
                         ("d3d11va", HWDeviceType.d3d11va)):
        for path, meta in real + synth:
            print(f"probe hwaccel[{backend}] {path.name} ...", flush=True)
            rows.append(probe_hwaccel(path, dev, strict=False, meta=meta,
                                      backend_label=backend, route="hwaccel_api"))

    # 严格模式（无软解回退）只跑 d3d12va 的 AV1 行——本次问题的核心
    for path, meta in synth:
        print(f"probe strict d3d12va {path.name} ...", flush=True)
        rows.append(probe_hwaccel(path, HWDeviceType.d3d12va, strict=True,
                                  meta=meta, backend_label="d3d12va",
                                  route="hwaccel_strict"))

    # 命名 wrapper 解码器（无软解回退，输出 hw 帧格式）
    wrapper_assets = [("h264_d3d12va", real[:1]), ("hevc_d3d12va", real[2:3])]
    if synth:
        wrapper_assets.append(("av1_d3d12va", synth[:1]))
    for dec, assets in wrapper_assets:
        for path, meta in assets:
            print(f"probe wrapper {dec} {path.name} ...", flush=True)
            rows.append(probe_wrapper(dec, path, meta))

    report = {
        "date": datetime.now().isoformat(timespec="seconds"),
        "plan_item": "开放问题 5 跟进：AV1/HEVC/H264 d3d12va 实测（本机 AMD）",
        "machine": {"os": sys.platform, "vendor": gpu.primary_vendor,
                    "adapters": [a.to_dict() for a in gpu.adapters],
                    "pyav": av.__version__},
        "protocol": ("hwaccel_api 行：输出 ∈ {nv12,p010le,p016le} 才算 "
                     "supported；wrapper 行：输出含 'd3d12' 即硬解生效"),
        "rows": [r.as_dict() for r in rows],
    }
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    json_path = RESULTS_DIR / "windows_d3d12va_probe_amd.json"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                         encoding="utf-8")

    md = ["# d3d12va 解码实测（开放问题 5 跟进，本机 AMD RX 6800）", "",
          f"日期：{report['date']} ｜ PyAV {av.__version__} ｜ "
          "RDNA2 有 AV1 硬解硬件", "",
          "| 路线 | 后端 | 素材 | codec | 位深 | 分辨率 | 裁决 | 输出格式 | fps |",
          "|---|---|---|---|---|---|---|---|---|"]
    for r in rows:
        md.append(f"| {r.route} | {r.backend} | {r.fixture} | {r.codec} "
                  f"| {r.bit_depth} | {r.width}×{r.height} | {r.verdict} "
                  f"| {r.out_format or '—'} | {r.fps or '—'} |")
    md += ["", "## detail", ""]
    for r in rows:
        if r.detail:
            md.append(f"- {r.route}/{r.backend}/{r.fixture}: {r.detail}")
    md_path = RESULTS_DIR / "windows_d3d12va_probe_amd.md"
    md_path.write_text("\n".join(md), encoding="utf-8")
    print(f"\nwritten: {json_path}\nwritten: {md_path}")
    for r in rows:
        print(f"  {r.route:16s} {r.backend:14s} {r.fixture:32s} -> "
              f"{r.verdict} {r.out_format}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
