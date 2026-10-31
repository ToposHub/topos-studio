"""
编码硬件基准测试（Windows + AMD）。

使用与导出管线完全相同的 PyAV 环境，解码 tests/test_videos 下的真实素材，
分别测量 CPU 软件编码（libx264/libx265/prores_ks）与 AMD AMF 硬件编码
（h264_amf/hevc_amf）的速度。

方法论：
- 预解码 N 帧到内存，编码计时不含解码时间（解码速度单独报告）。
- 每个编码器先跑 12 帧 warmup（GPU 升频/缓存预热），再计时完整 N 帧。
- 输出写到临时文件，报告编码 fps 与文件大小。

用法：
    .venv/Scripts/python.exe scripts/bench_hw_encode_windows.py [--frames-2k N] [--frames-4k N]
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import socket
import sys
import tempfile
import time
from datetime import datetime
from fractions import Fraction
from pathlib import Path

import av

PROJECT_ROOT = Path(__file__).resolve().parent.parent
TEST_VIDEOS = PROJECT_ROOT / "tests" / "test_videos"

SOURCES = {
    "2K": TEST_VIDEOS / "2K_h264.mp4",
    "4K": TEST_VIDEOS / "4K_H264.mov",
}

WARMUP_FRAMES = 12


def get_cpu_name() -> str:
    try:
        import winreg
        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"HARDWARE\DESCRIPTION\System\CentralProcessor\0",
        ) as key:
            return str(winreg.QueryValueEx(key, "ProcessorNameString")[0])
    except Exception:
        return platform.processor() or "unknown"


def get_gpu_name() -> str:
    try:
        import subprocess

        out = subprocess.run(
            [
                "powershell", "-NoProfile", "-Command",
                "(Get-CimInstance win32_VideoController | "
                "Select-Object -First 1 -ExpandProperty Name)",
            ],
            capture_output=True, text=True, timeout=30,
        )
        return out.stdout.strip() or "unknown"
    except Exception:
        return "unknown"


def probe_source(path: Path) -> dict:
    container = av.open(str(path))
    stream = container.streams.video[0]
    info = {
        "path": str(path),
        "codec": stream.codec_context.name,
        "pix_fmt": stream.codec_context.pix_fmt,
        "width": stream.width,
        "height": stream.height,
        "fps": float(stream.average_rate) if stream.average_rate else 0.0,
        "frames": stream.frames or 0,
    }
    container.close()
    return info


def decode_frames(path: Path, n: int) -> tuple[list, dict]:
    """预解码 n 帧到内存（保持源像素格式），返回 (frames, decode_info)。"""
    container = av.open(str(path))
    stream = container.streams.video[0]
    frames: list = []
    t0 = time.perf_counter()
    for frame in container.decode(stream):
        frames.append(frame)
        if len(frames) >= n:
            break
    dt = time.perf_counter() - t0
    container.close()
    info = {
        "decoded": len(frames),
        "decode_seconds": round(dt, 3),
        "decode_fps": round(len(frames) / dt, 1) if dt > 0 else 0.0,
    }
    return frames, info


def prepare_frames(frames: list, pix_fmt: str) -> list:
    """编码前把帧统一转到目标像素格式（不计时）。"""
    return [f.reformat(format=pix_fmt) for f in frames]


def bench_encoder(
    encoder_name: str,
    frames: list,
    width: int,
    height: int,
    fps: float,
    pix_fmt: str,
    out_path: Path,
    options: dict | None = None,
    threads: int | None = None,
    profile: str | None = None,
) -> dict:
    """计时编码全部帧（含 warmup），返回编码统计。"""
    rate = Fraction(round(fps * 1001), 1000) if fps else Fraction(24, 1)

    # prores_ks 仅 MOV 容器支持；其余 H.264/HEVC 用 MP4
    fmt = "mov" if "prores" in encoder_name else "mp4"
    suffix = ".mov" if fmt == "mov" else ".mp4"
    out_path = out_path.with_suffix(suffix)

    def run(frame_list: list, target: Path) -> float:
        container = av.open(str(target), "w", format=fmt)
        stream = container.add_stream(encoder_name, rate=rate)
        stream.width = width
        stream.height = height
        stream.pix_fmt = pix_fmt
        if options:
            stream.options = dict(options)
        if profile:
            try:
                stream.profile = profile
            except Exception:
                pass
        if threads is not None:
            stream.thread_count = threads
        t0 = time.perf_counter()
        for i, frame in enumerate(frame_list):
            frame.pts = i
            frame.time_base = Fraction(1, int(round(fps or 24)))
            for packet in stream.encode(frame):
                container.mux(packet)
        for packet in stream.encode():
            container.mux(packet)
        dt = time.perf_counter() - t0
        container.close()
        return dt

    # warmup（结果丢弃）
    warm = frames[:WARMUP_FRAMES]
    if warm:
        warm_path = out_path.with_suffix(".warm.mp4")
        try:
            run(warm, warm_path)
        finally:
            warm_path.unlink(missing_ok=True)

    dt = run(frames, out_path)
    size = out_path.stat().st_size
    out_path.unlink(missing_ok=True)
    return {
        "encoder": encoder_name,
        "options": options or {},
        "threads": threads if threads is not None else "auto",
        "encode_seconds": round(dt, 2),
        "encode_fps": round(len(frames) / dt, 1) if dt > 0 else 0.0,
        "frames": len(frames),
        "output_mb": round(size / 1024 / 1024, 1),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--frames-2k", type=int, default=300)
    parser.add_argument("--frames-4k", type=int, default=120)
    parser.add_argument("--only", default=None,
                        help="只跑名称包含该子串的运行（如 prores）")
    args = parser.parse_args()

    frame_budget = {"2K": args.frames_2k, "4K": args.frames_4k}

    # 增量模式：与已有结果合并（--only 重跑时保留其余数据）
    out_json = PROJECT_ROOT / "scripts" / "bench_hw_encode_results.json"
    prev_results: dict | None = None
    if args.only and out_json.exists():
        try:
            prev = json.loads(out_json.read_text(encoding="utf-8"))
            if prev.get("sources"):
                print(f"[增量] 载入已有结果: {out_json}")
                prev_results = prev
        except Exception:
            prev_results = None

    print("=" * 78)
    print("Topos Color 编码硬件基准测试 / Hardware Encode Benchmark")
    print("=" * 78)
    print(f"时间: {datetime.now().isoformat(timespec='seconds')}")
    print(f"CPU: {get_cpu_name()} "
          f"({os.cpu_count()} logical)")
    print(f"GPU: {get_gpu_name()}")
    print(f"Python {sys.version.split()[0]} / PyAV {av.__version__}")

    # DNxHR 编码器可用性检查（app 的 ENCODER_NAME_MAP 映射到 'dnxhr'）
    has_dnxhr = "dnxhr" in av.codecs_available
    has_dnxhd = "dnxhd" in av.codecs_available
    print(f"编码器可用: dnxhr={has_dnxhr} dnxhd={has_dnxhd} "
          f"h264_amf={'h264_amf' in av.codecs_available} "
          f"hevc_amf={'hevc_amf' in av.codecs_available}")

    # 各 H.264/HEVC 运行共用规格：CRF18 / medium 是高质量导出的典型设置
    H264_CRF = {"crf": "18", "preset": "medium"}
    HEVC_CRF = {"crf": "18", "preset": "medium"}

    runs_plan = [
        # (key, encoder, pix_fmt, options, threads, note)
        ("libx264 auto-thread", "libx264", "yuv420p", H264_CRF, 0, "CPU 全线程"),
        ("libx264 threads=4", "libx264", "yuv420p", H264_CRF, 4, "app 默认线程数"),
        ("h264_amf", "h264_amf", "yuv420p", {"quality": "balanced"}, None, "AMD 硬件"),
        ("libx265", "libx265", "yuv420p", HEVC_CRF, 0, "CPU 全线程"),
        ("hevc_amf", "hevc_amf", "yuv420p", {"quality": "balanced"}, None, "AMD 硬件"),
        ("prores_ks HQ", "prores_ks", "yuv422p10le", None, 0,
         "app 默认导出编码 (10-bit)"),
    ]
    if args.only:
        runs_plan = [r for r in runs_plan if args.only.lower() in r[0].lower()]

    tmp_dir = Path(tempfile.mkdtemp(prefix="topos_bench_"))
    results: dict = {"meta": {}, "sources": {}}
    results["meta"] = {
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "cpu": get_cpu_name(),
        "cpu_logical": os.cpu_count(),
        "gpu": get_gpu_name(),
        "os": f"{platform.system()} {platform.release()}",
        "python": sys.version.split()[0],
        "pyav": av.__version__,
        "dnxhr_encoder_available": has_dnxhr,
        "dnxhd_encoder_available": has_dnxhd,
        "warmup_frames": WARMUP_FRAMES,
    }

    for tier, src in SOURCES.items():
        if not src.exists():
            print(f"[跳过] 找不到素材: {src}")
            continue
        info = probe_source(src)
        n = min(frame_budget[tier], info["frames"] or frame_budget[tier])
        print()
        print("-" * 78)
        print(f"[{tier}] {src.name}  {info['width']}x{info['height']} "
              f"{info['fps']:.2f}fps {info['codec']}/{info['pix_fmt']}"
              f"  解码 {n} 帧...")
        frames, dec_info = decode_frames(src, n)
        print(f"  解码速度: {dec_info['decode_fps']} fps "
              f"({dec_info['decoded']} 帧 / {dec_info['decode_seconds']}s)")

        tier_results = {"source": info, "decode": dec_info, "runs": []}
        for key, enc, pix, opts, threads, note in runs_plan:
            if enc not in av.codecs_available:
                print(f"  [{key}] 不可用，跳过")
                tier_results["runs"].append(
                    {"run": key, "encoder": enc, "error": "encoder not available"})
                continue
            prepared = prepare_frames(frames, pix)
            out = tmp_dir / f"{tier}_{enc.replace('.', '_')}.mp4"
            try:
                stat = bench_encoder(
                    enc, prepared, info["width"], info["height"],
                    info["fps"], pix, out, options=opts, threads=threads,
                )
                stat.update({"run": key, "note": note, "pix_fmt": pix})
                tier_results["runs"].append(stat)
                print(f"  [{key:20s}] {stat['encode_fps']:8.1f} fps "
                      f"({stat['encode_seconds']:6.2f}s, "
                      f"{stat['output_mb']} MB)  {note}")
            except Exception as exc:
                tier_results["runs"].append(
                    {"run": key, "encoder": enc, "error": str(exc)})
                print(f"  [{key}] 失败: {exc}")
        results["sources"][tier] = tier_results

    # 增量合并：保留本次未重跑的项目
    if prev_results:
        for tier, prev_data in prev_results["sources"].items():
            if tier not in results["sources"]:
                results["sources"][tier] = prev_data
                continue
            cur_keys = {r.get("run") for r in results["sources"][tier]["runs"]}
            for prev_run in prev_data["runs"]:
                if prev_run.get("run") not in cur_keys:
                    results["sources"][tier]["runs"].append(prev_run)

    out_json.write_text(json.dumps(results, ensure_ascii=False, indent=2),
                        encoding="utf-8")
    print()
    print(f"结果已保存: {out_json}")
    print(f"临时目录: {tmp_dir}")

    # 汇总
    print()
    print("=" * 78)
    print("速度对比汇总（编码 fps，越高越好）")
    print("=" * 78)
    for tier, data in results["sources"].items():
        print(f"\n[{tier}] {Path(data['source']['path']).name}")
        for run in data["runs"]:
            if "error" in run:
                print(f"  {run['run']:22s} 失败: {run['error'][:60]}")
            else:
                print(f"  {run['run']:22s} {run['encode_fps']:>9.1f} fps "
                      f"| {run['note']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
