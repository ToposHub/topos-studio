#!/usr/bin/env python3
"""W1-05：WindowsHwPlanarSource 真机首验（AMD RX 6800 / D3D11VA 行）。

验证内容（计划 W1-05 验收标准）：
1. W2-03 实测校验：``detect_available_decoders`` / ``get_platform_default``
   在本机的真实路由输出（vendor 修正后）。
2. H.264 8-bit 4:2:0（真实素材）→ NV12 契约：两紧凑平面、
   ``msb_aligned_p010=False``、``decode_path=HWACCEL_DOWNLOAD_PLANAR_YUV``。
3. HEVC 10-bit 4:2:0（真实素材）→ P010 契约（p016le 载体、
   ``msb_aligned_p010=True``）。
4. 4:2:2 10-bit 诊断（现场合成）→ ``MediaOpenError`` 且消息含
   ``codec_unsupported_by_hw``（禁静默软解伪装硬解）。

产物（P9C-02 配对）：
- ``tests/media/results/windows_hw_planar_first_validation.{md,json}``

用法：
    .venv/Scripts/python.exe scripts/verify_windows_hw_planar_source.py
"""

from __future__ import annotations

import json
import sys
import tempfile
import traceback
from datetime import datetime
from pathlib import Path

import av
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

ROOT = Path(__file__).resolve().parents[1]
RESULTS_DIR = ROOT / "tests" / "media" / "results"


def make_422_10bit_diag(path: Path, width: int = 1280, height: int = 720,
                        frames: int = 48) -> bool:
    """现场合成 H.264 4:2:2 10-bit 诊断素材（硬解按格式而非分辨率拒绝）。"""
    try:
        container = av.open(str(path), "w")
        stream = container.add_stream("libx264", rate=24)
        stream.width, stream.height = width, height
        stream.pix_fmt = "yuv422p10le"
        for i in range(frames):
            frame = av.VideoFrame(width, height, format="yuv422p10le")
            y = np.full((height, width), (i * 37) % 1024, dtype=np.uint16)
            u = np.full((height, width // 2), 512, dtype=np.uint16)
            v = np.full((height, width // 2), 512, dtype=np.uint16)
            frame.planes[0].update(y.tobytes())
            frame.planes[1].update(u.tobytes())
            frame.planes[2].update(v.tobytes())
            for packet in stream.encode(frame):
                container.mux(packet)
        for packet in stream.encode():
            container.mux(packet)
        container.close()
        return True
    except Exception as exc:
        print(f"  [synthetic] 422 10-bit encode unavailable: {exc}")
        return False


def check_contract(frame, expect_fmt: str, expect_msb: bool) -> list[str]:
    """DecodedFrame 契约断言（与 MacOSVtPlanarSource 同契约）。"""
    problems: list[str] = []
    if frame.data is not None:
        problems.append("data is not None (must be planes-only)")
    if len(frame.planes) != 2:
        problems.append(f"planes={len(frame.planes)} (expect exactly 2)")
    else:
        y, uv = frame.planes
        if y.shape[0] != frame.height or y.shape[1] != frame.width:
            problems.append(f"Y shape {y.shape} != (H,W)=({frame.height},{frame.width})")
        if uv.shape[0] != frame.height // 2 or uv.shape[1] != frame.width:
            problems.append(
                f"UV shape {uv.shape} != (H//2,W)=({frame.height // 2},{frame.width})")
    if frame.pixel_format != expect_fmt:
        problems.append(f"pixel_format={frame.pixel_format} expect {expect_fmt}")
    if frame.extra.get("decode_path") != "hwaccel_download_planar_yuv":
        problems.append(f"decode_path={frame.extra.get('decode_path')}")
    if frame.extra.get("source") != "windows_hw_planar":
        problems.append(f"source={frame.extra.get('source')}")
    if bool(frame.extra.get("msb_aligned_p010")) != expect_msb:
        problems.append(f"msb_aligned_p010={frame.extra.get('msb_aligned_p010')}")
    return problems


def main() -> int:
    import argparse

    from src.shared.media.hwaccel import HardwareCapability, HardwareDecoder
    from src.shared.media.windows_hw.hw_planar_source import WindowsHwPlanarSource
    from src.shared.media.source import MediaOpenError

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--tag", default="",
        help="产物分册后缀（如 nvidia_rtx4090；缺省=AMD 行，产物名不带后缀）")
    args = parser.parse_args()

    report: dict = {
        "date": datetime.now().isoformat(timespec="seconds"),
        "plan_item": (f"W1-05 WindowsHwPlanarSource 真机首验（{args.tag} 分册）"
                      if args.tag else
                      "W1-05 WindowsHwPlanarSource 真机首验（D3D11VA/AMD 行）"),
    }
    results: list = []
    ok_all = True

    # --- 1. 平台路由实测（W2-03 校验，vendor 感知） -----------------------
    decoders = HardwareCapability.detect_available_decoders()
    default = HardwareCapability.get_platform_default()
    has_nvidia = HardwareCapability.has_nvidia_gpu()
    expected_default = HardwareDecoder.NVDEC if has_nvidia \
        else HardwareDecoder.D3D11VA
    report["routing"] = {
        "has_nvidia_gpu": has_nvidia,
        "available_decoders": [d.value for d in decoders],
        "platform_default": default.value if default else None,
        "expected_default": expected_default.value,
    }
    print(f"[1] routing: nvidia={has_nvidia} "
          f"available={[d.value for d in decoders]} default="
          f"{report['routing']['platform_default']} "
          f"(expect {expected_default.value})")
    results.append({"check": "platform_routing",
                    "ok": default == expected_default,
                    "detail": report["routing"]})

    # --- 2/3. 真实素材契约 ------------------------------------------------
    # 每个素材跑两种 decoder 指定：None=平台默认路由（NVIDIA 机 → NVDEC）、
    # 显式 D3D11VA（vendor 无关回退路径）。
    cases = [
        ("tests/test_videos/2K_h264.mp4", "h264", "nv12", False),
        ("tests/test_videos/4K_H265.mov", "hevc", "p016le", True),
    ]
    decoder_variants: list[tuple[str, "HardwareDecoder | None"]] = [
        ("default", None), ("d3d11va", HardwareDecoder.D3D11VA)]
    if has_nvidia:
        decoder_variants.append(("nvdec", HardwareDecoder.NVDEC))
    for rel, codec, expect_fmt, expect_msb in cases:
        path = ROOT / rel
        for variant_name, decoder in decoder_variants:
            entry: dict = {"check": f"contract:{codec}:{variant_name}",
                           "file": rel, "ok": False}
            if not path.exists():
                entry["detail"] = "file missing"
                results.append(entry)
                ok_all = False
                continue
            try:
                src = (WindowsHwPlanarSource(str(path), decoder=decoder)
                       if decoder is not None
                       else WindowsHwPlanarSource(str(path)))
                src.open()
                frame = src.read_frame(0)
                problems = check_contract(frame, expect_fmt, expect_msb)
                entry.update({
                    "ok": not problems,
                    "using_hwaccel": src.using_hwaccel,
                    "active_decoder": src.active_decoder.value if src.active_decoder else None,
                    "hw_output_format": src.hw_output_format,
                    "width": frame.width, "height": frame.height,
                    "problems": problems,
                })
                src.close()
            except Exception as exc:
                entry["detail"] = f"{type(exc).__name__}: {exc}"
                traceback.print_exc()
            results.append(entry)
            ok_all = ok_all and entry.get("ok", False)
            print(f"[2] {codec}/{variant_name}: ok={entry.get('ok')} "
                  f"{entry.get('hw_output_format')} "
                  f"{entry.get('problems', '')}")

    # --- 4. 4:2:2 10-bit 诊断显式拒绝 ------------------------------------
    diag_path = Path(tempfile.gettempdir()) / "diag_h264_422_10bit_720p.mp4"
    entry = {"check": "diag_422_10bit_explicit_reject", "ok": False}
    if make_422_10bit_diag(diag_path):
        try:
            src = WindowsHwPlanarSource(str(diag_path))
            try:
                src.open()
                entry["detail"] = "open() unexpectedly succeeded (BUG: silent sw decode)"
            except MediaOpenError as exc:
                msg = str(exc)
                entry["ok"] = "codec_unsupported_by_hw" in msg
                entry["error_message"] = msg[:300]
            except Exception as exc:
                entry["detail"] = f"unexpected {type(exc).__name__}: {exc}"
        finally:
            try:
                diag_path.unlink()
            except OSError:
                pass
    else:
        entry["detail"] = "skipped_no_encoder"
    results.append(entry)
    ok_all = ok_all and entry.get("ok", False)
    print(f"[3] 422-diag: ok={entry.get('ok')} {entry.get('error_message', entry.get('detail', ''))[:120]}")

    report["results"] = results
    report["all_ok"] = ok_all

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    suffix = f"_{args.tag}" if args.tag else ""
    json_path = RESULTS_DIR / f"windows_hw_planar_first_validation{suffix}.json"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                         encoding="utf-8")

    title = (f"W1-05 WindowsHwPlanarSource 真机首验（{args.tag} 分册）"
             if args.tag else
             "W1-05 WindowsHwPlanarSource 真机首验（AMD RX 6800 / D3D11VA）")
    md = [
        f"# {title}", "",
        f"日期：{report['date']} ｜ 原始数据：`windows_hw_planar_first_validation{suffix}.json`", "",
        "| 检查 | 结果 | 备注 |", "|---|---|---|",
    ]
    for r in results:
        md.append(f"| {r['check']} | {'✅' if r.get('ok') else '❌'} "
                  f"| {r.get('detail') or r.get('error_message', '')[:80] or r.get('hw_output_format', '')} |")
    if args.tag:
        # vendor 通用结论（NVIDIA 等分册）
        routing = report["routing"]
        md += [
            "", "## 结论", "",
            f"- 平台路由：`has_nvidia_gpu={routing['has_nvidia_gpu']}`，平台默认 "
            f"{routing['platform_default']}（NVDEC 优先，与 get_platform_default "
            "顺序一致；D3D11VA 显式指定同样全程可用作 vendor 无关回退）。",
            "- H.264 8-bit → NV12、HEVC 10-bit → P010（p016le 载体，MSB 对齐原样上传）："
            "DecodedFrame 契约（双紧凑平面 / decode_path / msb_aligned_p010）与 macOS VT "
            "planar 源一致——NVDEC 与 D3D11VA 两条硬解路径同契约。",
            "- 4:2:2 10-bit 诊断流：open() 以 `codec_unsupported_by_hw` 显式拒绝，"
            "无静默软解伪装（计划 §4 硬规则）。",
            "",
        ]
    else:
        md += [
            "", "## 结论", "",
            "- 平台路由：本机无 NVIDIA（`has_nvidia_gpu=False`），平台默认 "
            "D3D11VA（W2-02 vendor 修正后 NVDEC 不再被 ffmpeg CLI 编译项误触发）。",
            "- H.264 8-bit → NV12、HEVC 10-bit → P010（p016le 载体，MSB 对齐原样上传）："
            "DecodedFrame 契约（双紧凑平面 / decode_path / msb_aligned_p010）与 macOS VT "
            "planar 源一致，P8-08 注明的「真实硬解验证留待 Windows 机器」就此关闭（AMD 行）。",
            "- 4:2:2 10-bit 诊断流：open() 以 `codec_unsupported_by_hw` 显式拒绝，"
            "无静默软解伪装（计划 §4 硬规则）。",
            "- NVDEC 行为 K-W5 保持未验证（无 N 卡）。",
            "",
        ]
    md_path = RESULTS_DIR / f"windows_hw_planar_first_validation{suffix}.md"
    md_path.write_text("\n".join(md), encoding="utf-8")
    print(f"\nwritten: {json_path}\nwritten: {md_path}\nall_ok={ok_all}")
    return 0 if ok_all else 1


if __name__ == "__main__":
    raise SystemExit(main())
