"""解码硬件基准测试（Windows + AMD）。

用 tests/test_videos 下的真实素材，在生产环境（PyAV）下测量 4 条解码路径：

- raw_sw_decode   纯 PyAV 软件解码（不转换、不拷贝）——CPU 解码能力上限
- raw_hw_decode   PyAV HWAccel(d3d11va) 硬解（含 GPU→CPU 自动下载为 NV12/P010）
- prod_cpu_planar 生产 CPU planar 源（FFmpegMediaSource，含 numpy 平面提取）
- prod_hw_planar  生产硬解 planar 源（WindowsHwPlanarSource，与上面同契约）
- prod_topos      （仅 --include-topos）Topos 原生 codec 生产源，加载库强校验

H.264/H.265 跑全部 4 条；ProRes/DNxHR 仅 CPU 两条（无硬解能力）。
方法论与 bench_hw_encode_windows.py 一致：先顺序 warmup 12 帧（会话建立、
GPU 升频、解码器 JIT），再计时后续 N 帧的顺序解码（播放热路径是顺序读，
不走 seek）。

B0.2（docs/perf_plan_B_windows_2026-10-22.md）测量口径：

- 分段计时：``open_ms``（容器打开+会话建立；prod 路径含 probe，raw 不含，
  语义随路径）/ ``first_frame_ms``（open 后首帧取回）/ ``warmup_ms``（首帧
  起至 warmup 完成）/ ``steady_*``（计时段，与旧 fps/ms_per_frame 同口径）。
- 旧字段 ``setup_ms`` 保留为**整个 bench 调用的墙钟**（含 open+warmup+
  steady+close），不是初始化耗时，仅作历史连续性对照。
- 线程/格式/缩放：raw 路径记录实际 thread_type/thread_count；所有路径记录
  首帧尺寸并与探测 meta 对比（``scaled``）。
- 结构化状态：每 mode 记 ``status``（ok / ok_partial / eof_before_measure /
  error），每 source 记 ok / partial / all_failed / missing / probe_error；
  顶层 ``summary`` 汇总。退出码：0=全部 ok；2=存在缺项/失败但有 ok 项；
  3=无一成功（含素材全缺）。
- 运行 manifest：git commit/branch/dirty、完整 args、GPU 驱动、CPU 逻辑核，
  供跨运行对齐（环境详情见 probe_env_matrix_windows.py 产物）。

用法：
    .venv/Scripts/python.exe scripts/bench_hw_decode_windows.py
    .venv/Scripts/python.exe scripts/bench_hw_decode_windows.py --frames-4k 90 --only H265
    .venv/Scripts/python.exe scripts/bench_hw_decode_windows.py --only 4K_H264.mov --hw-backend nvdec --tag B0_20261022_run01
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import subprocess
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path

import av

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.shared.media.ffmpeg_source import FFmpegMediaSource  # noqa: E402
from src.shared.media.windows_hw.hw_planar_source import (  # noqa: E402
    WindowsHwPlanarSource,
)

TEST_VIDEOS = PROJECT_ROOT / "tests" / "test_videos"

# (文件名, 是否测硬解)
SOURCES: list[tuple[str, bool]] = [
    ("2K_h264.mp4", True),
    ("4K_H264.mov", True),
    ("2K_H265.mov", True),
    ("4K_H265.mov", True),
    ("2K_prores422HQ.mov", False),
    ("4K_prores422HQ.mov", False),
    ("4K_prores4444.mov", False),
    ("2K_DNxHR HQX 12bit.mov", False),
    ("4K_DNxHR HQX 12bit.mov", False),
]

WARMUP_FRAMES = 12

TIMING_SEMANTICS = {
    "open_ms": "容器打开+解码会话建立；prod 路径含 probe（生产语义），raw 路径不含",
    "first_frame_ms": "open 完成后首帧取回耗时（含解码器首帧 JIT）",
    "warmup_ms": "首帧起至 warmup 完成的累计墙钟",
    "steady_ms": "计时段墙钟；fps/ms_per_frame 由此段计算（与旧 JSON 同口径）",
    "setup_ms": "整个 bench 调用墙钟（open+warmup+steady+close），非初始化耗时，仅历史对照",
    "frame_budget": "warmup 后的目标计时帧数；实际测量见 frames（EOF 早停为 ok_partial）",
}


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


def _gpu_info() -> tuple[str, str]:
    """(主适配器描述, 驱动版本) 动态探测（跨机归档不再写死）。"""
    try:
        from src.shared.media.windows_hw.gpu_capability import (
            probe_gpu_capability,
        )
        gpu = probe_gpu_capability()
        desc = gpu.adapters[0].description if gpu.adapters else "unknown"
        return desc, str(gpu.driver_version or "unknown")
    except Exception:
        return "unknown", "unknown"


def _git(args: list[str]) -> str:
    try:
        out = subprocess.run(
            ["git", *args], cwd=PROJECT_ROOT, capture_output=True, text=True,
            encoding="utf-8", errors="replace",
        )
        return out.stdout.strip() if out.returncode == 0 else ""
    except Exception:
        return ""


def sequential_bench(
    read_one,  # () -> object，返回 None 表示 EOF
    n_frames: int,
) -> dict:
    """顺序读取分段计时：open 后首帧 / warmup 余量 / steady 计时段。

    EOF 早停时以实际测量帧数结束（frames < n_frames），status 归
    ok_partial / eof_before_measure，不当作成功跑满。
    """
    t_post_open = time.perf_counter()
    first_frame_ms: float | None = None
    warmed = 0
    eof_hit = False
    while warmed < WARMUP_FRAMES:
        t0 = time.perf_counter()
        got = read_one() is not None
        dt = time.perf_counter() - t0
        if not got:
            eof_hit = True
            break
        if first_frame_ms is None:
            first_frame_ms = dt * 1000.0
        warmed += 1
    warmup_ms = (time.perf_counter() - t_post_open) * 1000.0

    measured = 0
    total_bytes = 0
    frame_size: str | None = None
    t0 = time.perf_counter()
    while measured < n_frames:
        frame = read_one()
        if frame is None:
            eof_hit = True
            break
        measured += 1
        total_bytes += getattr(frame, "nbytes", 0) or 0
        if frame_size is None:
            w = getattr(frame, "width", None)
            h = getattr(frame, "height", None)
            if isinstance(w, int) and isinstance(h, int) and w > 0:
                frame_size = f"{w}x{h}"
    steady_ms = (time.perf_counter() - t0) * 1000.0
    fps = measured / (steady_ms / 1000.0) if steady_ms > 0 else 0.0
    return {
        "frames": measured,
        "fps": round(fps, 1),
        "ms_per_frame": round(steady_ms / measured, 2) if measured else None,
        "mb_read": round(total_bytes / 1e6, 1),
        # —— B0.2 分段计时（真实口径）——
        "first_frame_ms": round(first_frame_ms, 2) if first_frame_ms is not None else None,
        "warmup_frames_done": warmed,
        "warmup_ms": round(warmup_ms, 1),
        "steady_ms": round(steady_ms, 1),
        "frame_size": frame_size,
        "eof_hit": eof_hit,
    }


def bench_raw_sw(path: Path, n_frames: int) -> dict:
    """纯软件解码上限：不解码后处理，仅迭代解码输出帧。"""
    t0 = time.perf_counter()
    c = av.open(str(path))
    s = c.streams.video[0]
    intra = s.codec_context.name in ('prores', 'dnxhd')
    s.thread_type = 'SLICE' if intra else 'AUTO'
    s.thread_count = 0
    it = c.decode(s)

    def read_one():
        return next(it, None)

    result = sequential_bench(read_one, n_frames)
    result["open_ms"] = round((time.perf_counter() - t0) * 1000.0, 1)
    result["threads"] = {
        "thread_type": "SLICE" if intra else "AUTO",
        "thread_count": s.thread_count,  # 0 = auto（ffmpeg 按核数）
        "cpu_logical": os.cpu_count(),
    }
    c.close()
    return result


def bench_raw_hw(path: Path, n_frames: int, hw_backend: str = "d3d11va") -> dict:
    """硬解上限：硬解 + PyAV 自动下载为 NV12/P010 软件帧（无 numpy 提取）。"""
    from av.codec.hwaccel import HWAccel, HWDeviceType

    t0 = time.perf_counter()
    if hw_backend == "nvdec":
        device = HWDeviceType.cuda  # PyAV 18 枚举无 nvdec 名；NVDEC 经 cuda 设备
    else:
        # HWDeviceType 是 int 枚举：按名字索引（HWDeviceType("d3d11va") 按值
        # 查找会 ValueError——旧脚本此处在本机 PyAV 18.1.0 上即失败）。
        device = HWDeviceType[hw_backend]
    c = av.open(str(path), hwaccel=HWAccel(device))
    s = c.streams.video[0]
    out_fmt = None
    it = c.decode(s)

    def read_one():
        nonlocal out_fmt
        f = next(it, None)
        if f is not None and out_fmt is None:
            out_fmt = f.format.name
        return f

    result = sequential_bench(read_one, n_frames)
    result["open_ms"] = round((time.perf_counter() - t0) * 1000.0, 1)
    result["threads"] = {"note": "ffmpeg hwaccel 内部默认", "cpu_logical": os.cpu_count()}
    c.close()
    result["out_fmt"] = out_fmt or "?"
    return result


def bench_prod_cpu_planar(path: Path, n_frames: int) -> dict:
    """生产 CPU planar 源（与 edit_gpu_decode_manager 第 4 层 fallback 同构造）。"""
    t0 = time.perf_counter()
    src = FFmpegMediaSource(
        str(path), preserve_planes=True, output_mode="planar_yuv"
    )
    src.open()
    meta = src.probe()
    result = sequential_bench(lambda: _next_or_none(src), n_frames)
    result["open_ms"] = round((time.perf_counter() - t0) * 1000.0, 1)  # 含 probe（生产语义）
    result["pix_fmt"] = meta.pixel_format
    result["threads"] = {"note": "FFmpegMediaSource 内部默认", "cpu_logical": os.cpu_count()}
    src.close()
    return result


def _next_or_none(src):
    try:
        return src.read_next_frame()
    except StopIteration:
        return None


def bench_prod_hw_planar(path: Path, n_frames: int,
                         decoder=None) -> dict:
    """生产硬解 planar 源（WindowsHwPlanarSource，含 numpy 平面提取）。

    ``decoder=None`` 用平台默认路由（NVIDIA 机 → NVDEC，生产语义）。
    """
    t0 = time.perf_counter()
    src = (WindowsHwPlanarSource(str(path), decoder=decoder)
           if decoder is not None else WindowsHwPlanarSource(str(path)))
    src.open()
    if not src.using_hwaccel:
        src.close()
        raise RuntimeError("hw session not active (silent SW fallback?)")

    result = sequential_bench(lambda: _next_or_none(src), n_frames)
    result["open_ms"] = round((time.perf_counter() - t0) * 1000.0, 1)
    result["out_fmt"] = src.hw_output_format or "?"
    result["hw_active"] = src.using_hwaccel
    src.close()
    return result


def bench_prod_topos(path: Path, n_frames: int) -> dict:
    """Topos 原生 codec 生产源（--include-topos；调用前已做强校验）。"""
    from src.shared.media.topos_source import ToposSource

    t0 = time.perf_counter()
    src = ToposSource(str(path), decode_role="playback")
    src.open()
    result = sequential_bench(lambda: _next_or_none(src), n_frames)
    result["open_ms"] = round((time.perf_counter() - t0) * 1000.0, 1)
    result["threads"] = {"note": "ToposSource 解码 worker 池", "cpu_logical": os.cpu_count()}
    src.close()
    return result


def frame_budget(meta_total: int | None, requested: int) -> int:
    """总帧数不足时按 available 缩减计时帧数。"""
    if meta_total and meta_total > WARMUP_FRAMES + 10:
        return max(30, min(requested, meta_total - WARMUP_FRAMES - 2))
    return requested


def _mode_status(r: dict | None, error: bool, budget: int) -> str:
    if error or r is None:
        return "error"
    frames = r.get("frames", 0)
    if frames == 0:
        return "eof_before_measure"
    if frames < budget or r.get("eof_hit"):
        return "ok_partial"
    return "ok"


def main() -> int:
    if sys.platform == "win32":
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    parser = argparse.ArgumentParser()
    parser.add_argument("--frames-2k", type=int, default=240)
    parser.add_argument("--frames-4k", type=int, default=120)
    parser.add_argument("--only", default=None, help="文件名子串过滤")
    parser.add_argument("--hw-backend", default=None,
                        help="raw_hw_decode 后端（d3d11va/nvdec；缺省 d3d11va）")
    parser.add_argument("--tag", default="",
                        help="产物分册后缀（如 nvidia_rtx4090）")
    parser.add_argument("--include-topos", action="store_true",
                        help="加测 Topos 原生 codec 项（加载库强校验，缺失即 fail-closed 记 error）")
    args = parser.parse_args()
    hw_backend = args.hw_backend or "d3d11va"

    gpu_desc, driver = _gpu_info()
    dirty = _git(["status", "--porcelain"])
    results: dict = {
        "date": datetime.now().isoformat(timespec="seconds"),
        "os": f"{platform.system()} {platform.release()} build {platform.version().split('.')[-1]}",
        "python": sys.version.split()[0],
        "pyav": av.__version__,
        "cpu": get_cpu_name(),
        "cpu_logical": os.cpu_count(),
        "gpu_hint": gpu_desc,
        "gpu_driver": driver,
        "warmup_frames": WARMUP_FRAMES,
        "hw_backend": hw_backend,
        "manifest": {
            "git_commit": _git(["rev-parse", "HEAD"]),
            "git_branch": _git(["branch", "--show-current"]),
            "git_dirty_files": len(dirty.splitlines()) if dirty else 0,
            "args": {k: v for k, v in vars(args).items()},
        },
        "timing_semantics": TIMING_SEMANTICS,
        "sources": {},
    }

    sources: list[tuple[str, str]] = [(name, "std") for name, _ in SOURCES]
    topos_gate: str = ""
    if args.include_topos:
        from src.shared.media.topos_source import (
            check_topos_codec_available,
            is_topos_movie,
        )

        available = check_topos_codec_available()
        topos_gate = "ok" if available else "topos_native_codec_unavailable"
        for p in sorted(TEST_VIDEOS.glob("*topos*.mov")):
            if is_topos_movie(str(p)):
                sources.append((p.name, "topos"))
        results["topos_codec_gate"] = {
            "check": "check_topos_codec_available()",
            "status": topos_gate,
        }

    summary = {"missing": 0, "probe_error": 0, "ok": 0, "partial": 0,
               "all_failed": 0, "modes": {"ok": 0, "ok_partial": 0,
                                          "eof_before_measure": 0, "error": 0}}

    for name, kind in sources:
        if args.only and args.only.lower() not in name.lower():
            continue
        path = TEST_VIDEOS / name
        if not path.exists():
            print(f"[跳过] 缺少素材: {name}")
            results["sources"][name] = {"status": "missing"}
            summary["missing"] += 1
            continue
        is_4k = name.startswith("4K")
        n = args.frames_4k if is_4k else args.frames_2k
        hw_applicable = kind == "std" and dict(SOURCES)[name]
        entry: dict = {"kind": kind, "hw_applicable": hw_applicable}
        budget = n

        print(f"\n=== {name} ===")
        try:
            probe_src = FFmpegMediaSource(str(path))
            meta = probe_src.probe()
            probe_src.close()
            total = meta.total_frames
            budget = frame_budget(total, n)
            entry["meta"] = {
                "codec": meta.codec_name,
                "pix_fmt": meta.pixel_format,
                "size": f"{meta.width}x{meta.height}",
                "total_frames": total,
                "fps": round(float(meta.fps), 2) if meta.fps else None,
            }
            m = entry["meta"]
            print(
                f"  codec={m['codec']} fmt={m['pix_fmt']} {m['size']}"
                f" 总帧={total} 目标帧率={m['fps']} 计时帧数={budget}"
            )
        except Exception as exc:  # noqa: BLE001
            print(f"  [探测失败] {exc}")
            entry["status"] = "probe_error"
            entry["error"] = f"probe: {exc}"
            results["sources"][name] = entry
            summary["probe_error"] += 1
            continue
        entry["frame_budget"] = budget

        if kind == "topos" and topos_gate != "ok":
            # 强校验 fail-closed：不做"可能碰巧能跑"的尝试（计划 B0.2）。
            entry["prod_topos"] = {
                "status": "error",
                "error": f"topos_native_codec_gate:{topos_gate}",
            }
            entry["status"] = "all_failed"
            results["sources"][name] = entry
            summary["all_failed"] += 1
            summary["modes"]["error"] += 1
            print("  prod_topos      FAIL: topos native codec 不可用（fail-closed）")
            continue

        mode_status: list[str] = []
        modes: list[tuple[str, object]] = [
            ("raw_sw_decode", lambda: bench_raw_sw(path, budget)),
            ("raw_hw_decode", lambda: bench_raw_hw(path, budget, hw_backend)),
            ("prod_cpu_planar", lambda: bench_prod_cpu_planar(path, budget)),
            ("prod_hw_planar", lambda: bench_prod_hw_planar(path, budget)),
            ("prod_topos", lambda: bench_prod_topos(path, budget)),
        ]
        for mode, run in modes:
            if mode in ("raw_hw_decode", "prod_hw_planar") and not hw_applicable:
                continue
            if mode == "prod_topos" and kind != "topos":
                continue
            if mode != "prod_topos" and kind == "topos":
                continue  # topos 项只跑生产 topos 源（无 raw/硬解语义）
            r = None
            err = False
            try:
                t0 = time.perf_counter()
                r = run()
                r["setup_ms"] = round((time.perf_counter() - t0) * 1000.0, 0)
                if r.get("frame_size") and r["frame_size"] != m["size"]:
                    r["scaled"] = f"{r['frame_size']} (meta {m['size']})"
                else:
                    r["scaled"] = None
            except Exception as exc:  # noqa: BLE001
                err = True
                r = {"error": f"{type(exc).__name__}: {exc}"}
                print(f"  {mode:16s} FAIL: {exc}")
                if "--debug" in sys.argv:
                    traceback.print_exc()
            status = _mode_status(None if err else r, err, budget)
            r["status"] = status
            entry[mode] = r
            mode_status.append(status)
            summary["modes"][status] += 1
            if not err:
                extra = r.get("out_fmt") or r.get("pix_fmt") or ""
                print(
                    f"  {mode:16s} {r['fps']:8.1f} fps  {r['ms_per_frame']:7.2f} ms/帧"
                    f"  open={r['open_ms']:.0f}ms 首帧={r['first_frame_ms']:.1f}ms"
                    f"  [{extra}] ({status})"
                )

        if mode_status and all(s == "ok" for s in mode_status):
            entry["status"] = "ok"
            summary["ok"] += 1
        elif mode_status and any(s in ("ok", "ok_partial") for s in mode_status):
            entry["status"] = "partial"
            summary["partial"] += 1
        else:
            entry["status"] = "all_failed"
            summary["all_failed"] += 1
        results["sources"][name] = entry

    results["summary"] = summary
    out_json = (PROJECT_ROOT / "scripts" /
                (f"bench_hw_decode_results_{args.tag}.json" if args.tag
                 else "bench_hw_decode_results.json"))
    out_json.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n结果已保存: {out_json}")
    print(f"summary: {json.dumps(summary)}")

    total_sources = summary["ok"] + summary["partial"] + summary["all_failed"]
    if total_sources == 0 or (summary["ok"] == 0 and summary["partial"] == 0):
        return 3
    if (summary["missing"] or summary["probe_error"] or summary["all_failed"]
            or summary["partial"] or summary["modes"]["error"]
            or summary["modes"]["ok_partial"]
            or summary["modes"]["eof_before_measure"]):
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
