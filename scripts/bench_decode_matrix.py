"""B2.2 素材 × 解码路径能力矩阵（计划表 B · 机器 B 侧）。

目的（计划 §B2.2）：

- **发现式**枚举素材目录（不硬编码清单），逐素材生成内容指纹
  （size + 头部 md5）与格式探测（codec/profile/位深/色度/尺寸）；
- 每格记录**期望后端**（生产路由规则推演）与**实际后端**（真机实测），
  覆盖 cpu_planar / hw_download_planar / cuda_resident 三路径；
  首帧、稳态吞吐、CPU 字节、证据等级、fallback reason 逐格编码；
- GL-ready 终点复用 B1.2 harness（scripts/poc_cuda_gl_interop.py 的三个
  bencher），与路线选择数据同口径可比；
- 未知/失败/不支持分别编码（status: ok/error/unsupported/not_applicable），
  fail-closed。

仅产出机器 B（RTX 4090）原始数据；A（Mac）/C（RX 6800）部分由各自
机器运行本脚本产出，不代跑、不平均（计划 §B2.2）。

用法：
    .venv/Scripts/python.exe scripts/bench_decode_matrix.py --tag B22_20261024_run01
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

import av  # noqa: E402

TEST_VIDEOS = PROJECT_ROOT / "tests" / "test_videos"
WARMUP = 8
TIMED = 60

RESULT: dict = {
    "schema": "bench_decode_matrix/1",
    "date": datetime.now().isoformat(timespec="seconds"),
    "pyav": av.__version__,
    "machine_role": "B(RTX 4090)——A/C 部分由各自机器另行产出",
    "rows": [],
}


def _fingerprint(path: Path) -> dict:
    h = hashlib.md5()
    with open(path, "rb") as f:
        h.update(f.read(1 << 20))
    return {
        "size_bytes": path.stat().st_size,
        "head_md5_1mb": h.hexdigest(),
    }


def _probe(path: Path) -> dict:
    out: dict = {"path": path.name}
    try:
        c = av.open(str(path))
        s = c.streams.video[0]
        codec = s.codec_context.name if s.codec_context else ""
        pix_fmt = (s.codec_context.format.name
                   if s.codec_context and s.codec_context.format else "")
        profile = ""
        try:
            profile = str(s.codec_context.profile or "")
        except Exception:
            pass
        out.update({
            "codec": codec,
            "profile": profile,
            "pix_fmt": pix_fmt,
            "width": s.codec_context.width if s.codec_context else s.width,
            "height": s.codec_context.height if s.codec_context else s.height,
            "frames": int(s.frames or 0) or None,
            "duration_s": round(float(s.duration or 0
                                      and float(s.duration * s.time_base)), 3)
            if s.duration is not None and s.time_base is not None else None,
            "probe": "ok",
        })
        c.close()
    except Exception as exc:  # noqa: BLE001
        out.update({"probe": "error", "probe_error": f"{type(exc).__name__}: {exc}"})
    return out


def _expected_backend(codec: str) -> str:
    """生产路由规则推演（edit_gpu_decode_manager 3.4b/3.5/3.6/4 分支）。

    返回 (production_default, resident_candidate)。
    """
    c = (codec or "").lower()
    if c in ("h264", "hevc", "h265"):
        # NVIDIA 机 + auto：3.5 下载 planar 为现役默认；
        # 3.4b 驻留为 opt-in 候选（TOPOS_WIN_HW_RESIDENT_DECODE）
        return "win_hw_planar_download", "cuda_resident(opt-in)"
    if "topos" in c or c == "tpic":
        return "topos_native_cpu", None
    return "cpu_planar", None


def _bench_decode_only(path: Path) -> dict:
    """软解 decode-only（AUTO 帧线程，生产 ffmpeg_source 同款配置）。"""
    c = av.open(str(path))
    stream = c.streams.video[0]
    stream.thread_type = "AUTO"
    stream.codec_context.thread_count = 0
    t0 = time.perf_counter_ns()
    it = c.decode(stream)
    first = next(it)
    first_ms = (time.perf_counter_ns() - t0) / 1e6
    i = 0
    t0 = time.perf_counter_ns()
    for _ in it:
        i += 1
        if i >= WARMUP + TIMED:
            break
    elapsed_ms = (time.perf_counter_ns() - t0) / 1e6
    c.close()
    return {
        "first_frame_ms": round(first_ms, 2),
        "fps": round(i / (elapsed_ms / 1000.0), 1),
        "frames_measured": i,
        "sw_format": first.format.name,
    }


def _bench_hw_decode_only(path: Path, hw_owned: bool) -> dict:
    """硬解 decode-only（is_hw_owned=False 含隐式下载；True 为纯驻留）。"""
    from av.codec.hwaccel import HWAccel, HWDeviceType

    c = av.open(str(path), hwaccel=HWAccel(
        HWDeviceType.cuda, is_hw_owned=hw_owned))
    stream = c.streams.video[0]
    t0 = time.perf_counter_ns()
    it = c.decode(stream)
    first = next(it)
    first_ms = (time.perf_counter_ns() - t0) / 1e6
    i = 0
    t0 = time.perf_counter_ns()
    for _ in it:
        i += 1
        if i >= WARMUP + TIMED:
            break
    elapsed_ms = (time.perf_counter_ns() - t0) / 1e6
    fmt = first.format.name
    c.close()
    return {
        "first_frame_ms": round(first_ms, 2),
        "fps": round(i / (elapsed_ms / 1000.0), 1),
        "frames_measured": i,
        "hw_frame_format": fmt,
    }


def _gl_ready_bench(path: Path, ctx, bencher) -> dict:
    """GL-ready 终点（复用 B1.2 harness，同口径）。"""
    r = bencher(path, ctx, WARMUP, TIMED)
    total = [d + x for d, x in zip(r["decode_ms"], r["xfer_ms"])]
    if not total:
        return {"status": "no_samples",
                "interleave_errors": r.get("interleave_errors")}
    import numpy as np

    return {
        "status": "ok",
        "fps": round(1000.0 / float(np.mean(total)), 1),
        "decode_ms_mean": round(float(np.mean(r["decode_ms"])), 3),
        "xfer_ms_mean": round(float(np.mean(r["xfer_ms"])), 3),
        "cpu_bytes_per_frame": round(r["cpu_bytes"] / TIMED, 1),
        "interleaved_content_match": r.get("interleaved_content_match"),
    }


def run_matrix(frames_cap: int | None) -> dict:
    import moderngl
    from poc_cuda_gl_interop import (
        _bench_cpu_planar,
        _bench_hw_download,
        _bench_hw_zero_copy,
    )

    global TIMED
    if frames_cap:
        TIMED = frames_cap

    ctx = moderngl.create_standalone_context()
    assets = sorted(
        p for p in
        list(TEST_VIDEOS.glob("*.mov")) + list(TEST_VIDEOS.glob("*.mp4"))
        # 排除 macOS AppleDouble 资源叉残留（"._*"，非媒体）
        if not p.name.startswith("._")
    )
    for path in assets:
        row: dict = {"asset": path.name, **_fingerprint(path), **_probe(path)}
        codec = row.get("codec", "")
        # probe 失败 + 文件名含 topos → 厂商私有码流（PyAV 无解码器属预期）
        is_topos = ("topos" in codec.lower() or codec == "tpic"
                    or ("topos" in path.name.lower()
                        and row.get("probe") == "error"))
        expected, resident_candidate = _expected_backend(
            "topos" if is_topos and not codec else codec)
        row["expected_backend"] = expected
        row["resident_candidate"] = resident_candidate
        row["paths"] = {}

        hw_family = codec in ("h264", "hevc", "h265")

        # cpu planar（所有素材；topos 私有码流 PyAV 无解码器 → 预期 unsupported）
        if is_topos:
            row["paths"]["cpu_planar"] = {
                "status": "unsupported",
                "reason": "topos 私有码流不在 PyAV harness 范围（生产走 "
                          "topos native source，与本矩阵解耦）",
            }
        else:
            try:
                row["paths"]["cpu_planar"] = {
                    "status": "ok", "evidence": "L2", **_bench_decode_only(path)}
            except Exception as exc:  # noqa: BLE001
                row["paths"]["cpu_planar"] = {
                    "status": "error", "evidence": "L2",
                    "reason": f"{type(exc).__name__}: {str(exc)[:120]}"}

        if hw_family:
            # 期望后端实测：3.5 下载 planar（decode-only 含下载）
            try:
                row["paths"]["hw_download_planar"] = {
                    "status": "ok", "evidence": "L2",
                    **_bench_hw_decode_only(path, hw_owned=False)}
            except Exception as exc:  # noqa: BLE001
                row["paths"]["hw_download_planar"] = {
                    "status": "error", "evidence": "L2",
                    "reason": f"{type(exc).__name__}: {str(exc)[:120]}"}
            # 候选驻留（生产 3.4b 对应物）
            try:
                from src.shared.media.windows_hw.cuda_resident import (
                    WindowsCudaResidentSource,
                )

                src = WindowsCudaResidentSource(str(path))
                src.open()
                row["paths"]["cuda_resident"] = {
                    "status": "ok", "evidence": "L2",
                    "contract_format": src.resident_contract_format,
                    **_bench_hw_decode_only(path, hw_owned=True)}
                src.close()
            except Exception as exc:  # noqa: BLE001
                row["paths"]["cuda_resident"] = {
                    "status": "unsupported", "evidence": "L2",
                    "reason": f"{type(exc).__name__}: {str(exc)[:140]}"}
            # GL-ready 终点（与 B1.2 同口径）
            row["paths"]["gl_ready_cpu_planar"] = _try_gl(
                _bench_cpu_planar, path, ctx)
            row["paths"]["gl_ready_hw_download"] = _try_gl(
                _bench_hw_download, path, ctx)
            row["paths"]["gl_ready_cuda_resident"] = _try_gl(
                _bench_hw_zero_copy, path, ctx)
        else:
            row["paths"]["hw_download_planar"] = {
                "status": "not_applicable",
                "reason": f"codec={codec} 生产路由本就不选硬解（Intra 剪辑码流）",
            }
            row["paths"]["cuda_resident"] = {
                "status": "not_applicable", "reason": "同上"}

        RESULT["rows"].append(row)
        print(f"{path.name[:36]:36s} {codec:8s} "
              f"cpu={_fps_of(row, 'cpu_planar'):>7} "
              f"hw={_fps_of(row, 'hw_download_planar'):>7} "
              f"resident={_fps_of(row, 'cuda_resident'):>7}")
    ctx.release()
    return {"assets": len(RESULT["rows"])}


def _try_gl(bencher, path, ctx) -> dict:
    try:
        return _gl_ready_bench(path, ctx, bencher)
    except Exception as exc:  # noqa: BLE001
        return {"status": "error",
                "reason": f"{type(exc).__name__}: {str(exc)[:120]}"}


def _fps_of(row: dict, key: str) -> str:
    p = row["paths"].get(key, {})
    return str(p.get("fps", p.get("status", "-")))


def main() -> int:
    if sys.platform == "win32":
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    parser = argparse.ArgumentParser()
    parser.add_argument("--tag", default="")
    parser.add_argument("--frames", type=int, default=TIMED)
    args = parser.parse_args()

    try:
        git = subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                             capture_output=True, text=True,
                             cwd=str(PROJECT_ROOT))
        RESULT["git_commit"] = git.stdout.strip()
    except Exception:
        RESULT["git_commit"] = "unavailable"

    try:
        import moderngl
        info = moderngl.create_standalone_context().info
        RESULT["gpu"] = info.get("GL_RENDERER")
    except Exception as exc:  # noqa: BLE001
        RESULT["gpu"] = f"unavailable: {exc}"

    phase_errors = []
    try:
        summary = run_matrix(args.frames)
        RESULT["summary"] = summary
    except Exception as exc:  # noqa: BLE001
        phase_errors.append(f"matrix: {exc}")
        traceback.print_exc()

    out = PROJECT_ROOT / "scripts" / (
        f"bench_decode_matrix_{args.tag}.json" if args.tag
        else "bench_decode_matrix.json")
    out.write_text(json.dumps(RESULT, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    print(f"\n结果已保存: {out}")
    print(f"errors={len(phase_errors)}")
    return 0 if not phase_errors else 2


if __name__ == "__main__":
    raise SystemExit(main())
