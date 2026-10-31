#!/usr/bin/env python3
"""W1-04：Windows vendor × codec × 格式能力矩阵探测。

对每个 (素材, hwbackend) 组合：用 PyAV ``HWAccel`` 打开并解码 ≤N 帧，
按输出像素格式裁决（P8 期间确立的协议——PyAV 对不可用组合会**静默回退
软解**，输出仍是源格式；因此输出 ∈ {nv12, p010le, p016le} 才算硬解真实
生效）：

- ``supported``          输出为硬件半平面家族
- ``unsupported_silent_sw`` 会话成功但输出软件格式（静默回退）
- ``error:<msg>``        会话/解码失败

素材来源（计划 K-W5：只写本机有证据的行）：
- 真实拍摄素材：``tests/test_videos``（H.264 8bit 2K/4K、HEVC 10bit 2K/4K）
- 合成诊断素材：现场用 PyAV 编码 720p 短片（h264 4:2:2 10-bit / hevc
  4:2:2 10-bit / av1 4:2:0），编码器缺失时该行如实记 ``skipped_no_encoder``。

产物（P9C-02 约定，.md+.json 配对，按 GPU 分册避免跨机覆盖）：
- ``tests/media/results/windows_capability_matrix_<tag>.{md,json}``
  （``--tag`` 缺省按主适配器自动生成，如 ``nvidia_rtx4090``）

用法：
    .venv/Scripts/python.exe scripts/probe_windows_capability_matrix.py
    .venv/Scripts/python.exe scripts/probe_windows_capability_matrix.py --backends d3d11va,nvdec
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import av
from av.codec.hwaccel import HWAccel, HWDeviceType

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

ROOT = Path(__file__).resolve().parents[1]
RESULTS_DIR = ROOT / "tests" / "media" / "results"

HW_FORMATS = {"nv12", "p010le", "p016le"}
PROBE_FRAMES = 24

# hwbackend → PyAV HWDeviceType（nvdec 走 cuda 设备——NVDEC 会话由
# PyAV/FFmpeg 的 cuvid/nvdec 层经 CUDA 驱动创建）
DEVICE_TYPES = {
    "d3d11va": HWDeviceType.d3d11va,
    "d3d12va": HWDeviceType.d3d12va,
    "nvdec": HWDeviceType.cuda,
}


@dataclass
class Row:
    fixture: str
    source: str            # real | synthetic
    codec: str
    pix_fmt: str
    bit_depth: int
    chroma: str
    width: int
    height: int
    hwbackend: str
    verdict: str
    out_pix_fmt: str = ""
    fps: float = 0.0
    detail: str = ""

    def as_dict(self) -> dict:
        return self.__dict__.copy()


def probe_row(path: Path, hwbackend: str, meta: dict) -> Row:
    """解码 ≤PROBE_FRAMES 帧，按输出格式裁决。"""
    row = Row(fixture=path.name, source=meta["source"], codec=meta["codec"],
              pix_fmt=meta["pix_fmt"], bit_depth=meta["bit_depth"],
              chroma=meta["chroma"], width=meta["width"], height=meta["height"],
              hwbackend=hwbackend, verdict="")
    device = DEVICE_TYPES.get(hwbackend)
    try:
        accel = HWAccel(device)
        with av.open(str(path), hwaccel=accel) as container:
            frames = container.decode(video=0)
            t0 = time.perf_counter()
            n = 0
            out_fmt = ""
            for frame in frames:
                out_fmt = frame.format.name
                n += 1
                if n >= PROBE_FRAMES:
                    break
            dt = time.perf_counter() - t0
        row.out_pix_fmt = out_fmt
        row.fps = round(n / dt, 1) if dt > 0 else 0.0
        if out_fmt in HW_FORMATS:
            row.verdict = "supported"
        else:
            row.verdict = "unsupported_silent_sw"
            row.detail = f"session ok but output {out_fmt} (software fallback)"
    except Exception as exc:
        row.verdict = "error"
        row.detail = f"{type(exc).__name__}: {exc}"[:200]
    return row


def pick_av1_encoder() -> str | None:
    for cand in ("libsvtav1", "libaom-av1", "librav1e"):
        if cand in av.codecs_available:
            return cand
    return None


def make_synthetic(encoder: str, pix_fmt: str, name: str,
                   width: int = 1280, height: int = 720, frames: int = 48) -> Path | None:
    """现场编码合成诊断素材；编码器缺失/失败返回 None。

    平面填充直接用 ``frame.planes[i].update(bytes)``，避免跨格式 numpy
    形状拼接（10-bit 平面为 uint16，Y 与 UV 行数不同）。
    """
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
            y_val = (i * 37) % (1024 if ten_bit else 256)
            chroma_val = 512 if ten_bit else 128
            dtype = np.uint16 if ten_bit else np.uint8
            # yuv420p/yuv422p 均为 planar：plane0=Y，plane1=U，plane2=V
            y = np.full((height, width), y_val, dtype=dtype)
            if "420" in pix_fmt:
                cw, ch = width // 2, height // 2
            else:  # 422：色度全高半宽
                cw, ch = width // 2, height
            u = np.full((ch, cw), chroma_val, dtype=dtype)
            v = np.full((ch, cw), chroma_val, dtype=dtype)
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
        print(f"  [synthetic] {encoder}/{pix_fmt} encode unavailable: {exc}")
        return None


def real_assets() -> list[tuple[Path, dict]]:
    tv = ROOT / "tests" / "test_videos"
    spec = [
        ("2K_h264.mp4", "h264", "yuv420p", 8, "4:2:0", 1920, 1080),
        ("4K_H264.mov", "h264", "yuv420p", 8, "4:2:0", 3840, 2160),
        ("2K_H265.mov", "hevc", "yuv420p10le", 10, "4:2:0", 1920, 1080),
        ("4K_H265.mov", "hevc", "yuv420p10le", 10, "4:2:0", 3840, 2160),
    ]
    out = []
    for name, codec, pix, depth, chroma, w, h in spec:
        p = tv / name
        if p.exists():
            out.append((p, {"source": "real", "codec": codec, "pix_fmt": pix,
                            "bit_depth": depth, "chroma": chroma,
                            "width": w, "height": h}))
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--backends", default="d3d11va",
        help="逗号分隔的 hwbackend 列表（d3d11va/d3d12va/nvdec）")
    parser.add_argument(
        "--tag", default="",
        help="产物分册后缀（缺省按主适配器自动生成，如 nvidia_rtx4090）")
    args = parser.parse_args()
    backends = [b.strip() for b in args.backends.split(",") if b.strip()]
    for b in backends:
        if b not in DEVICE_TYPES:
            print(f"unknown backend: {b} (known: {sorted(DEVICE_TYPES)})")
            return 2

    from src.shared.media.windows_hw.gpu_capability import probe_gpu_capability

    gpu = probe_gpu_capability()
    vendor = gpu.primary_vendor
    adapters = [a.to_dict() for a in gpu.adapters]
    print(f"vendor={vendor} adapters={[a['description'] for a in adapters]}")

    tag = args.tag or _auto_tag(vendor, adapters)
    rows: list[Row] = []
    for path, meta in real_assets():
        for backend in backends:
            print(f"probe {path.name} @ {backend} ...", flush=True)
            rows.append(probe_row(path, backend, meta))

    # 合成诊断素材（编码器缺失 → 行如实 skipped）
    av1_encoder = pick_av1_encoder()
    synth_plan = [
        ("libx264", "yuv422p10le", "diag_h264_422_10bit_720p.mp4",
         "h264", 10, "4:2:2"),
        ("libx265", "yuv422p10le", "diag_hevc_422_10bit_720p.mp4",
         "hevc", 10, "4:2:2"),
        (av1_encoder, "yuv420p", "diag_av1_8bit_720p.mp4",
         "av1", 8, "4:2:0"),
    ]
    for enc, pix, name, codec, depth, chroma in synth_plan:
        p = make_synthetic(enc, pix, name)
        if p is None or not p.exists():
            rows.append(Row(fixture=name, source="synthetic", codec=codec,
                            pix_fmt=pix, bit_depth=depth, chroma=chroma,
                            width=1280, height=720, hwbackend=backends[0],
                            verdict="skipped_no_encoder",
                            detail=f"encoder {enc}/{pix} unavailable in PyAV"))
            continue
        meta = {"source": "synthetic", "codec": codec, "pix_fmt": pix,
                "bit_depth": depth, "chroma": chroma, "width": 1280, "height": 720}
        for backend in backends:
            rows.append(probe_row(p, backend, meta))

    report = {
        "date": datetime.now().isoformat(timespec="seconds"),
        "plan_item": f"W1-04 vendor × codec × 格式能力矩阵（{tag} 分册）",
        "machine": {
            "os": sys.platform,
            "vendor": vendor,
            "adapters": adapters,
            "pyav": av.__version__,
        },
        "k_w5_note": f"本表只含本机（{tag}）实测行；其它 vendor 行无真机不写。",
        "probe_protocol": "输出 ∈ {nv12,p010le,p016le} 才算 supported（防静默软解）",
        "rows": [r.as_dict() for r in rows],
    }
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    json_path = RESULTS_DIR / f"windows_capability_matrix_{tag}.json"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                         encoding="utf-8")

    md = [f"# Windows 能力矩阵（W1-04：{tag} 分册）", "",
          f"日期：{report['date']} ｜ GPU：{adapters[0]['description'] if adapters else '?'}"
          f" ｜ PyAV {av.__version__} ｜ 后端：{', '.join(backends)}", "",
          "裁决协议：输出 ∈ {nv12, p010le, p016le} 才算 supported（PyAV 会静默回退软解，",
          "只信输出格式，不信会话成败）。", "",
          "| 素材 | codec | 格式 | 分辨率 | 后端 | 裁决 | 输出格式 | fps |", "|---|---|---|---|---|---|---|---|"]
    for r in rows:
        md.append(
            f"| {r.fixture} | {r.codec} | {r.pix_fmt} | {r.width}×{r.height} "
            f"| {r.hwbackend} | {r.verdict} | {r.out_pix_fmt or '—'} | {r.fps or '—'} |")
    md += ["", "## 说明", "",
           f"- 本分册来自 {tag} 真机；其它 vendor 行按 K-W5 不写（无真机）。",
           "- 真实素材来自 `tests/test_videos`；4:2:2 诊断素材为现场合成（720p，",
           "  硬解单元按格式而非分辨率拒绝，720p 结论可代表）。", ""]
    md_path = RESULTS_DIR / f"windows_capability_matrix_{tag}.md"
    md_path.write_text("\n".join(md), encoding="utf-8")
    print(f"\nwritten: {json_path}\nwritten: {md_path}")
    for r in rows:
        print(f"  {r.fixture:40s} {r.hwbackend} -> {r.verdict} {r.out_pix_fmt}")
    return 0


def _auto_tag(vendor: str, adapters: list[dict]) -> str:
    """按主适配器生成产物分册名（如 nvidia_rtx4090 / amd_rx6800）。"""
    desc = adapters[0]["description"] if adapters else ""
    words = re.findall(r"[A-Za-z0-9]+", desc)
    model = ""
    for w in words:
        if any(c.isdigit() for c in w) and w.lower() not in ("pci", "pcie"):
            model = w.lower()
            break
    return f"{vendor}_{model}" if vendor != "unknown" else (model or "unknown_gpu")


if __name__ == "__main__":
    raise SystemExit(main())
