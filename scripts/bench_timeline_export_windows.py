"""Windows 时间线导出基准（计划 2026-09-15 §6 步骤 0）。

协议（计划 §4.1 / §4.2）：

- 每个 (用例, 对照组) 先做一次 warmup 帧预热导出（产物丢弃），再连续
  ``--runs``（默认 3）次完整导出，输出固定 ``--frames``（默认 480）帧；
- 记录每次 wall time / FPS / 帧时 p50、p95（导出进度回调采样）、输出
  ffprobe（PyAV）与 SHA-256；
- 报告组间中位 FPS 比例。计划 §4.3 的 85% 门禁针对未来 D3D11 路径；
  本脚本现阶段产出的是 CPU / 现有硬解下载 / FFmpeg GPU 三组基线与其比值。

对照组（全部真实执行，无模拟）：

- ``cpu_pipeline``  TOPOS_DISABLE_WIN_HW_DECODE + TOPOS_DISABLE_WIN_HW_ENCODE
  （子进程隔离；纯 CPU 解码 + x264 编码 + CPU/OpenGL 渲染）；
- ``hw_download``   生产默认：硬解下载 planar（NV12/P010）+ vendor 硬编
  （本机 AMD 为 AMF）；
- ``ffmpeg_gpu_ref`` ffmpeg CLI：``-hwaccel`` 硬解 + vendor 硬编，同分辨率
  同码率，作吞吐参照（计划 §4.1 FFmpeg GPU 对照组）。

诚实边界：

- T2–T10 的用例协议已在下方声明，但运行依赖后续步骤（D3D11 时间线渲染器、
  多层时间线程序化构建、HDR 素材和页面驱动）。``runner_ready=False`` 的用例在结果里
  记 ``status="declared_not_run"`` 和 blocking 原因，绝不伪造基线数字。
- ffmpeg 参照组的帧时 p50/p95 来自 ``-progress`` 采样间隔近似；中位 FPS
  才是该组的primary指标。

用法::

    python scripts/bench_timeline_export_windows.py                 # 全协议
    python scripts/bench_timeline_export_windows.py --cases T0 --dry-run
    python scripts/bench_timeline_export_windows.py --cases T0 --runs 1

结果落盘：``tests/media/results/windows_timeline_export_<tag>_<date>.{json,md}``
（P9C-02 配对约定）。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    # 子进程以 `python scripts/bench_....py` 方式启动时，sys.path[0] 是
    # scripts/，必须显式注入仓库根才能 import src.*。
    sys.path.insert(0, str(ROOT))
RESULTS_DIR = ROOT / "tests" / "media" / "results"
TEST_VIDEOS_DIR = ROOT / "tests" / "test_videos"

SCHEMA_VERSION = 1
KIND = "windows_timeline_export_bench"

#: 计划 §4.3：T0/T1 中位吞吐门禁（未来 D3D11 路径 vs FFmpeg GPU 基线）。
D3D11_VS_FFMPEG_GATE_RATIO = 0.85

ALL_CASE_IDS: Tuple[str, ...] = tuple(f"T{index}" for index in range(11))
ALL_GROUP_IDS: Tuple[str, ...] = (
    "cpu_pipeline", "hw_download", "ffmpeg_gpu_ref", "d3d11_gpu_surface",
)

#: 各对照组的子进程环境开关。hw_download 组 = 生产默认（无额外开关）。
GROUP_ENV: Dict[str, Dict[str, str]] = {
    "cpu_pipeline": {
        "TOPOS_DISABLE_WIN_HW_DECODE": "1",
        "TOPOS_DISABLE_WIN_HW_ENCODE": "1",
        "TOPOS_EXPORT_GPU_RENDER": "0",
    },
    "hw_download": {},
    "ffmpeg_gpu_ref": {},
    "d3d11_gpu_surface": {
        "TOPOS_BACKEND_GRAPHICS": "d3d11",
        "TOPOS_EXPORT_GPU_RENDER": "1",
    },
}


# ======================================================================
# 用例矩阵（计划 §4.1 W-T0–W-T10）
# ======================================================================

@dataclass(frozen=True)
class BenchCase:
    """单个基准用例的协议声明。"""

    case_id: str
    title: str
    purpose: str
    sources: Tuple[str, ...]
    runner_ready: bool
    summary: str = ""
    coverage: str = ""
    output_resolution: Optional[Tuple[int, int]] = None
    blocking: str = ""


def case_matrix() -> Tuple[BenchCase, ...]:
    """返回 W-T0–W-T10 用例声明；未就绪项必须有 blocking 原因。"""
    cases: List[BenchCase] = [
        BenchCase(
            case_id="T0",
            title="单段 H.264 直通",
            purpose="解码 -> 编码的最低开销与 surface 连通性（计划 §4.1 T0）。",
            sources=("2K_h264.mp4", "4K_H264.mov"),
            runner_ready=True,
            summary="单段 2K H.264，无效果、无转场、无缩放；音频关闭以隔离 GPU 归因。",
        ),
        BenchCase(
            case_id="T1",
            title="resize + 色彩转换",
            purpose="验证最常见的渲染链：解码 -> 缩放 -> 色彩转换 -> 编码。",
            sources=("2K_h264.mp4", "2K_H265.mov"),
            runner_ready=True,
            summary="单段 2K H.264 输出 1280x720（H.264 yuv420p）。",
            coverage=(
                "已接线：resize + yuv420p 色彩转换；"
                "未接线：主级调色（效果实例程序化构建属后续批次，"
                "届时在 coverage 里显式升级，不冒充完整 T1）。"
            ),
            output_resolution=(1280, 720),
        ),
        BenchCase(
            case_id="T2",
            title="HEVC / P010 10-bit",
            purpose="验证 HEVC Main10、P010 和色彩元数据路径。",
            sources=("2K_H265.mov", "4K_H265.mov"),
            runner_ready=False,
            blocking="P010/10-bit 时间线与硬编 surface 接线尚未落地。",
        ),
        BenchCase(
            case_id="T3",
            title="两层合成 + 16 混合模式",
            purpose="验证多源解码和复合（位置、缩放、裁剪、透明度、混合模式）。",
            sources=("2K_h264.mp4", "2K_H265.mov"),
            runner_ready=False,
            blocking="两轨两层时间线的程序化构建（多轨 EditTrack 组装批次）。",
        ),
        BenchCase(
            case_id="T4",
            title="两层空间转场",
            purpose="验证过渡期资源、遮罩、并行提交（wipe / iris / slide / 用户空间转场）。",
            sources=("2K_h264.mp4", "2K_H265.mov"),
            runner_ready=False,
            blocking="空间转场时间线实例的程序化构建（转场接线批次）。",
        ),
        BenchCase(
            case_id="T5",
            title="内建效果代表组合",
            purpose="逐项测量 Blur / Sharpen / Glow / 形态学 / Edge / Denoise / Defocus。",
            sources=("2K_h264.mp4",),
            runner_ready=False,
            blocking="内建效果的时间线效果实例程序化构建（依赖效果接线批次）。",
        ),
        BenchCase(
            case_id="T6",
            title="10-bit 调色和 HDR 输出",
            purpose="验证 P010、PQ/HLG、色彩元数据和高位深输出。",
            sources=("2K_DNxHR HQX 10bit.mov", "4K_DNxHR HQX 10bit.mov"),
            runner_ready=False,
            blocking="真 HDR 元数据与 decoder/encoder 自有 P010 surface 尚未接线。",
        ),
        BenchCase(
            case_id="T7",
            title="ProRes 专业中间码流",
            purpose="验证 Windows ProRes 解码能力与准确回退。",
            sources=("2K_prores422HQ.mov", "4K_prores422HQ.mov"),
            runner_ready=False,
            blocking="ProRes GPU decode surface 与回退分类尚未接线。",
        ),
        BenchCase(
            case_id="T8",
            title="Topos 输入/输出",
            purpose="验证 Topos proxy、HQ 和 4444XQ 的 GPU decode/encode bridge。",
            sources=("2K_topos_proxy.mov", "2K_topos_hq.mov", "2K_topos_4444xq.mov"),
            runner_ready=False,
            blocking="Topos 原生 GPU decode/encode bridge 尚未落地。",
        ),
        BenchCase(
            case_id="T9",
            title="四轨 4K 压力时间线",
            purpose="找到显存、解码并行度、资源池和硬编队列瓶颈。",
            sources=("4K_H264.mov", "4K_H265.mov"),
            runner_ready=False,
            blocking="四轨 4K 时间线的程序化构建（多轨压力组装批次）。",
        ),
        BenchCase(
            case_id="T10",
            title="Color / Composite 页面",
            purpose="验证节点图、LUT、scope、合成与 Edit 导出的 frame contract 一致性。",
            sources=("2K_h264.mp4", "2K_H265.mov"),
            runner_ready=False,
            blocking="Color/Composite 页面 GPU texture lease 与导出交接尚未接线。",
        ),
    ]
    for case in cases:
        if not case.runner_ready and not case.blocking:
            raise ValueError(f"{case.case_id} 未就绪但缺 blocking 原因")
    return tuple(cases)


# ======================================================================
# 纯函数：分位数 / ffmpeg 命令 / 机器元信息
# ======================================================================

def percentile(sorted_samples: Sequence[float], q: float) -> float:
    """线性插值分位数。q ∈ [0, 1]。空输入抛 ValueError。"""
    if not sorted_samples:
        raise ValueError("分位数输入不可为空")
    if not 0.0 <= q <= 1.0:
        raise ValueError(f"分位数 q 越界: {q}")
    ordered = sorted(sorted_samples)
    if len(ordered) == 1:
        return float(ordered[0])
    pos = q * (len(ordered) - 1)
    low = int(pos)
    high = min(low + 1, len(ordered) - 1)
    frac = pos - low
    return float(ordered[low] * (1.0 - frac) + ordered[high] * frac)


def median(values: Sequence[float]) -> float:
    return percentile(values, 0.5)


def build_ffmpeg_gpu_cmd(
    ffmpeg_bin: str,
    source: Path,
    output: Path,
    frames: int,
    bitrate_bps: int,
    vendor: str,
    gpu_surface: bool,
    output_resolution: Optional[Tuple[int, int]] = None,
) -> List[str]:
    """构造 FFmpeg GPU 参照命令（计划 §4.1 FFmpeg GPU 对照组）。

    同分辨率、同码率、vendor 硬编。``output_resolution`` 用于 T1 等需要
    resize 的用例；D3D11/CUDA 表面优先使用对应硬件缩放器。``gpu_surface=True``
    时解码 surface 直接驻留 D3D11（``-hwaccel_output_format d3d11``）；机器
    不支持时调用方降级 ``gpu_surface=False``（hwaccel 下载模式），并在结果
    里记录 ``ref_mode_fallback``。
    """
    hwaccel, encoder, encoder_opts = _vendor_ref_map(vendor)
    cmd: List[str] = [
        ffmpeg_bin, "-y", "-loglevel", "error", "-nostats",
        "-hwaccel", hwaccel,
    ]
    if gpu_surface and hwaccel == "d3d11va":
        cmd += ["-hwaccel_output_format", "d3d11"]
    video_filter: Optional[str] = None
    if output_resolution is not None:
        out_width, out_height = output_resolution
        if out_width <= 0 or out_height <= 0:
            raise ValueError("output_resolution 必须为正数")
        if gpu_surface and hwaccel == "d3d11va":
            # Keep the FFmpeg reference on a D3D11 surface for T1 resize.
            # The ordinary `scale` filter would implicitly download pixels.
            video_filter = f"scale_d3d11={out_width}:{out_height}:nv12"
        elif gpu_surface and hwaccel == "cuda":
            video_filter = f"scale_cuda={out_width}:{out_height}"
        else:
            video_filter = f"scale={out_width}:{out_height}"
    cmd += ["-i", str(source), "-an"]
    if video_filter is not None:
        cmd += ["-vf", video_filter]
    cmd += [
        "-frames:v", str(frames),
        "-c:v", encoder,
    ]
    # 编码器私有选项按 vendor 区分：`-quality/-rc vbr_peak` 是 AMF 词表，
    # h264_nvenc 不认识（NVIDIA 真机参照会在首个 run 即失败）。
    cmd += list(encoder_opts)
    cmd += [
        "-b:v", str(bitrate_bps),
        "-maxrate", str(int(bitrate_bps * 1.2)),
        "-bufsize", str(bitrate_bps * 2),
        "-progress", "pipe:1",
    ]
    cmd += [str(output)]
    return cmd


def _vendor_ref_map(vendor: str) -> Tuple[str, str, Tuple[str, ...]]:
    if vendor in ("nvidia", "nvidia_open"):
        return "cuda", "h264_nvenc", ("-preset", "p5", "-rc", "vbr")
    # AMD 与未知 vendor 均按 d3d11va + AMF 参照（本计划当前真机为 AMD）。
    return "d3d11va", "h264_amf", ("-quality", "quality", "-rc", "vbr_peak")


def source_path(name: str) -> Path:
    return TEST_VIDEOS_DIR / name


def machine_tag() -> str:
    """主适配器 slug（如 ``amd_rx6800``）；探测失败退回 ``unknown``。"""
    if sys.platform != "win32":
        return "unknown"
    try:
        from src.shared.media.windows_hw.gpu_capability import (
            pick_primary_hardware_adapter,
            probe_gpu_capability,
        )

        cap = probe_gpu_capability()
        adapter = pick_primary_hardware_adapter(cap.adapters)
        if adapter is None:
            return "unknown"
        vendor = (adapter.vendor_name or "gpu").lower()
        tokens = [
            "".join(ch for ch in token.lower() if ch.isalnum())
            for token in (adapter.description or "").replace("-", " ").split()
        ]
        model = "".join(
            token for token in tokens
            if token and token != vendor
            and token not in ("radeon", "geforce", "graphics", "series")
        )
        return f"{vendor}_{model}" if model else vendor
    except Exception:
        return "unknown"


def git_rev() -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=str(ROOT), capture_output=True, text=True, timeout=10,
            check=True,
        )
        return out.stdout.strip()
    except Exception:
        return ""


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


# ======================================================================
# 管线组 runner（子进程模式：--run-group 由父进程调用）
# ======================================================================

def probe_source(path: Path) -> Dict[str, Any]:
    import av

    container = av.open(str(path))
    try:
        stream = next(s for s in container.streams if s.type == "video")
        return {
            "codec": stream.codec_context.name if stream.codec_context else "",
            "pix_fmt": stream.codec_context.pix_fmt if stream.codec_context else "",
            "width": int(stream.codec_context.width or 0),
            "height": int(stream.codec_context.height or 0),
            "fps": float(stream.average_rate) if stream.average_rate else 0.0,
            "total_frames": int(stream.frames or 0),
        }
    finally:
        container.close()


def build_input_manifest(cases: Sequence[BenchCase]) -> Dict[str, Dict[str, Any]]:
    """Hash and probe every unique material referenced by the selected cases.

    The manifest is captured even when a case is declared-not-run.  That makes
    a future Windows report reproducible and distinguishes a missing material
    from a missing native GPU capability without running any export.
    """
    manifest: Dict[str, Dict[str, Any]] = {}
    names = sorted({name for case in cases for name in case.sources})
    for name in names:
        path = source_path(name)
        try:
            relative_path = str(path.relative_to(ROOT))
        except ValueError:
            relative_path = str(path)
        item: Dict[str, Any] = {
            "path": relative_path,
            "exists": path.is_file(),
        }
        if not path.is_file():
            item["status"] = "missing"
            manifest[name] = item
            continue
        item["sha256"] = file_sha256(path)
        try:
            item["probe"] = probe_source(path)
            item["status"] = "ok"
        except (OSError, RuntimeError, ValueError, StopIteration) as exc:
            item["status"] = "probe_failed"
            item["probe_error"] = f"{type(exc).__name__}: {exc}"
        manifest[name] = item
    return manifest


def probe_output(path: Path) -> Dict[str, Any]:
    import av

    container = av.open(str(path))
    try:
        stream = next(s for s in container.streams if s.type == "video")
        ctx = stream.codec_context
        decoded_frames = 0
        for _frame in container.decode(video=0):
            decoded_frames += 1
        return {
            "codec": ctx.name if ctx else "",
            "pix_fmt": ctx.pix_fmt if ctx else "",
            "width": int(ctx.width or 0),
            "height": int(ctx.height or 0),
            "stream_frames": int(stream.frames or 0),
            "decoded_frames": decoded_frames,
        }
    finally:
        container.close()


def _ensure_qapp() -> None:
    try:
        from PyQt6.QtWidgets import QApplication

        if QApplication.instance() is None:
            QApplication([])
    except Exception:
        pass  # 无显示环境下导出器不依赖 QApplication 时照常工作


def build_timeline(
    case: BenchCase,
    frames: int,
    bitrate_bps: int,
    output_path: Path,
    source_meta: Dict[str, Any],
) -> Tuple[Any, Any, Any]:
    """程序化构建单段时间线并返回 (project, sequence, export_config)。"""
    from src.features.edit.core.project import EditProject, MediaAsset
    from src.features.edit.core.timeline_export import (
        ExportCodec,
        ExportSource,
        TimelineExportConfig,
    )
    from src.features.edit.core.timeline_v2 import (
        ClipInstance,
        EditTrack,
        Rational,
        TimelineSequence,
    )

    src = source_path(case.sources[0])
    if not src.exists():
        raise FileNotFoundError(f"测试素材缺失: {src}")
    fps = source_meta.get("fps") or 25.0
    fps_rational = Rational(max(1, int(round(fps))), 1)
    width = int(source_meta.get("width") or 1920)
    height = int(source_meta.get("height") or 1080)

    asset = MediaAsset(
        asset_id="bench_src", path=str(src), media_type="video",
        display_name=src.stem,
        metadata={
            "fps": fps,
            "total_frames": source_meta.get("total_frames", 0),
        },
    )
    project = EditProject.create(name="bench-timeline-export")
    project = project.with_media_asset_added(asset)

    clip = ClipInstance(
        clip_id="bench_c1", asset_id="bench_src", track_id="v1",
        timeline_start=0, timeline_duration=frames,
        source_start=0, source_duration=frames,
    )
    track = EditTrack(
        track_id="v1", name="V1", track_type="video", index=0,
        clips=(clip,),
    )
    sequence = TimelineSequence(
        timeline_id="bench", name="Bench",
        fps=fps_rational, width=width, height=height,
        video_tracks=(track,),
    )

    resolution = case.output_resolution
    config = TimelineExportConfig(
        source=ExportSource.TIMELINE,
        timeline_id="bench",
        output_path=str(output_path),
        codec=ExportCodec.H264,
        resolution=resolution,
        quality=bitrate_bps,
        quality_mode="vbr",
        include_audio=False,
    )
    return project, sequence, config


def run_group_in_subprocess(
    case: BenchCase,
    group: str,
    frames: int,
    warmup_frames: int,
    runs: int,
    bitrate_bps: int,
    workdir: Path,
    timeout_s: float,
    keep_outputs: bool = False,
) -> Dict[str, Any]:
    """在隔离子进程里跑一个 (case, group) 的 warmup + N 次 run。

    子进程隔离保证：环境开关（TOPOS_DISABLE_WIN_HW_*）与模块级能力缓存
    不跨组泄漏；每次运行从冷启动开始，贴近计划"冷缓存"协议。
    """
    child_script = Path(__file__).resolve()
    result_path = workdir / f"{case.case_id}_{group}_result.json"
    cmd = [
        sys.executable, str(child_script),
        "--run-group", case.case_id, group,
        "--frames", str(frames),
        "--warmup", str(warmup_frames),
        "--runs", str(runs),
        "--bitrate-bps", str(bitrate_bps),
        "--emit-json", str(result_path),
    ]
    if keep_outputs:
        # 产物保留必须传给子进程：否则 _export_once 测量后照常删除 mp4，
        # 父进程的 --keep-outputs 实际上什么都留不下。
        cmd.append("--keep-outputs")
    env = dict(os.environ)
    env.update(GROUP_ENV.get(group, {}))
    try:
        proc = subprocess.run(
            cmd, cwd=str(ROOT), env=env,
            capture_output=True, text=True, timeout=timeout_s,
        )
    except subprocess.TimeoutExpired as exc:
        # 计划 §8：任何失败保留产物与 reason；超时也必须落报告而不是
        # 让父进程带未处理异常退出。
        stderr_tail = exc.stderr or ""
        if isinstance(stderr_tail, bytes):
            stderr_tail = stderr_tail.decode("utf-8", errors="replace")
        return {
            "status": "error",
            "error": f"子进程超时 (>{timeout_s}s)",
            "stderr_tail": str(stderr_tail)[-2000:],
        }
    if not result_path.exists():
        return {
            "status": "error",
            "error": f"子进程未产出结果 (exit={proc.returncode})",
            "stderr_tail": proc.stderr[-2000:],
        }
    with result_path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def run_group_main(
    case_id: str,
    group: str,
    frames: int,
    warmup_frames: int,
    runs: int,
    bitrate_bps: int,
    emit_json: Path,
    keep_outputs: bool = False,
) -> int:
    """子进程入口：执行一个 (case, group) 的全部导出并写出 JSON。"""
    case = next(c for c in case_matrix() if c.case_id == case_id)
    # The exporter consults ConfigManager during construction.  A benchmark
    # must not depend on (or mutate) the user's roaming profile: on some
    # Windows installations the default %APPDATA%\Topos directory is subject
    # to controlled-folder/antivirus latency, which can make even a one-frame
    # run appear hung before decoding starts.  Keep every child process on an
    # isolated config file beside its result so setup I/O is reproducible and
    # cannot leak settings between groups.
    os.environ["TOPOS_CONFIG_PATH"] = str(
        emit_json.with_name(f"{case_id}_{group}_config.json")
    )
    if group == "d3d11_gpu_surface":
        # This group is a real capability check, not a simulated rendering
        # result.  Until the native bridge and T0 evidence exist, preserve the
        # declared-not-run state and include the exact gate snapshot.
        try:
            from src.shared.media.windows_hw.d3d11_export_capability import (
                probe_d3d11_export_capability,
            )

            capability = probe_d3d11_export_capability()
            payload = {
                "status": "ok" if capability.ready else "declared_not_run",
                "group": group,
                "blocking": "" if capability.ready else capability.reason,
                "capability": capability.to_dict(),
            }
        except (ImportError, OSError, RuntimeError, TypeError, ValueError) as exc:
            payload = {
                "status": "declared_not_run",
                "group": group,
                "blocking": f"d3d11_probe_failed:{type(exc).__name__}:{exc}",
            }
        _dump_json(emit_json, payload)
        return 0
    source = source_path(case.sources[0])
    src_meta = probe_source(source)
    available = src_meta.get("total_frames") or 0
    if available and available < frames:
        payload: Dict[str, Any] = {
            "status": "error",
            "error": f"素材帧数不足: 需要 {frames}，实际 {available}",
        }
        _dump_json(emit_json, payload)
        return 1

    workdir = emit_json.parent
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    _ensure_qapp()

    from src.features.edit.core.timeline_export import create_timeline_exporter

    group_runs: List[Dict[str, Any]] = []
    warmup_note = "skipped"
    if warmup_frames > 0:
        warmup_out = workdir / f"{case.case_id}_{group}_warmup.mp4"
        try:
            warmup_record = _export_once(
                case, min(warmup_frames, frames), bitrate_bps,
                warmup_out, src_meta, create_timeline_exporter,
                keep_output=keep_outputs,
            )
            warmup_note = f"done frames={warmup_record['frames']}"
        except Exception as exc:  # 预热失败不掩盖，但不阻断正式测量
            warmup_note = f"failed: {type(exc).__name__}: {exc}"

    for run_index in range(1, runs + 1):
        out_path = workdir / f"{case.case_id}_{group}_run{run_index}.mp4"
        record = _export_once(
            case, frames, bitrate_bps, out_path, src_meta,
            create_timeline_exporter, keep_output=keep_outputs,
        )
        record["run_index"] = run_index
        group_runs.append(record)

    fps_values = [
        float(r["fps"]) for r in group_runs
        if r.get("status") == "ok" and r.get("fps") is not None
    ]
    fallback_reasons = sorted({
        r["hw_fallback_reason"] for r in group_runs
        if r.get("hw_fallback_reason")
    })
    payload = {
        "status": "ok" if fps_values else "error",
        "group": group,
        "runs": group_runs,
        "warmup": warmup_note,
        "median_fps": median(fps_values) if fps_values else None,
        "hw_fallback_reasons": fallback_reasons,
        "env": {
            k: os.environ.get(k, "")
            for k in ("TOPOS_DISABLE_WIN_HW_DECODE", "TOPOS_DISABLE_WIN_HW_ENCODE")
        },
    }
    _dump_json(emit_json, payload)
    return 0 if fps_values else 1


def _export_once(
    case: BenchCase,
    frames: int,
    bitrate_bps: int,
    out_path: Path,
    src_meta: Dict[str, Any],
    exporter_factory: Any,
    keep_output: bool = False,
) -> Dict[str, Any]:
    """单次导出：构建时间线（计入 setup）→ 计时导出 → probe 输出。"""
    setup_start = time.perf_counter()
    project, sequence, config = build_timeline(
        case, frames, bitrate_bps, out_path, src_meta,
    )
    timeline_samples: List[float] = []

    def _on_progress(_current: int, _total: int, _percent: float) -> None:
        timeline_samples.append(time.perf_counter())

    exporter = exporter_factory(project, sequence, progress_callback=_on_progress)
    setup_s = time.perf_counter() - setup_start

    export_start = time.perf_counter()
    outcome = exporter.export_timeline(config)
    wall_s = time.perf_counter() - export_start

    record: Dict[str, Any] = {
        "frames_requested": frames,
        "setup_s": round(setup_s, 4),
        "wall_s": round(wall_s, 4),
    }
    ok = bool(outcome.success) and out_path.exists()
    record["status"] = "ok" if ok else (
        "cancelled" if getattr(outcome, "cancelled", False) else "failed"
    )
    if not ok:
        record["error_code"] = str(getattr(outcome, "error_code", ""))
        record["cause"] = str(getattr(outcome, "cause", ""))[:500]
        return record
    record["frames"] = frames
    record["fps"] = round(frames / wall_s, 3) if wall_s > 0 else None
    if len(timeline_samples) >= 3:
        deltas = [
            (timeline_samples[i] - timeline_samples[i - 1]) * 1000.0
            for i in range(1, len(timeline_samples))
        ]
        record["frame_ms_p50"] = round(percentile(deltas, 0.5), 3)
        record["frame_ms_p95"] = round(percentile(deltas, 0.95), 3)
    record["hw_fallback_reason"] = str(getattr(outcome, "hw_fallback_reason", ""))
    record["output_mb"] = round(out_path.stat().st_size / (1024 * 1024), 3)
    record["output_sha256"] = file_sha256(out_path)
    record["probe"] = probe_output(out_path)
    if not keep_output:
        out_path.unlink(missing_ok=True)
    return record


def run_ffmpeg_ref_group(
    case: BenchCase,
    frames: int,
    warmup_frames: int,
    runs: int,
    bitrate_bps: int,
    workdir: Path,
    timeout_s: float,
    keep_outputs: bool = False,
) -> Dict[str, Any]:
    """FFmpeg GPU 参照组：vendor 硬解 + 硬编（吞吐参照，计划 §4.1）。"""
    ffmpeg_bin = _ffmpeg_bin()
    if not ffmpeg_bin:
        return {"status": "error", "error": "PATH 上找不到 ffmpeg"}
    from src.shared.export.video_encoder import detect_hw_vendor

    vendor = detect_hw_vendor() or "amd"
    source = source_path(case.sources[0])
    src_meta = probe_source(source)
    available = src_meta.get("total_frames") or 0
    if available and available < frames:
        return {
            "status": "error",
            "error": f"素材帧数不足: 需要 {frames}，实际 {available}",
        }

    group_runs: List[Dict[str, Any]] = []
    ref_mode_fallback = False
    ref_mode_fallback_reason = ""
    gpu_surface = True
    warmup_done = False
    # 计划 W0 门禁：每组预热 60 帧 + 正式 3×480。gpu_surface 首跑失败降级
    # 后必须在 hwaccel_download 模式下重跑预热，正式 run 不允许冷启动。
    run_index = 0
    while run_index <= runs:
        out_path = workdir / f"{case.case_id}_ffmpeg_ref_r{run_index}.mp4"
        cmd = build_ffmpeg_gpu_cmd(
            ffmpeg_bin, source, out_path, frames, bitrate_bps, vendor,
            gpu_surface=not ref_mode_fallback,
            output_resolution=case.output_resolution,
        )
        started = time.perf_counter()
        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True, timeout=timeout_s,
            )
        except subprocess.TimeoutExpired:
            group_runs.append({
                "run_index": run_index, "status": "failed",
                "error": f"ffmpeg 超时 (>{timeout_s}s)",
            })
            run_index += 1
            continue
        wall_s = time.perf_counter() - started
        if proc.returncode != 0:
            if run_index == 0 and gpu_surface and not ref_mode_fallback:
                # gpu_surface 模式首跑失败 → 降级 hwaccel 下载模式，先重跑
                # 预热再进入正式 run（降级原因记入结果，诚实披露参照链形态）。
                ref_mode_fallback = True
                ref_mode_fallback_reason = (
                    (proc.stderr or proc.stdout or "").strip()[-1000:]
                )
                continue  # 仍在 run 0：以降级配置重做预热
            group_runs.append({
                "run_index": run_index, "status": "failed",
                "error": (proc.stderr or "").strip()[-500:],
            })
            run_index += 1
            continue
        if run_index == 0:
            warmup_done = True
            if not keep_outputs:
                out_path.unlink(missing_ok=True)
            run_index += 1
            continue
        group_runs.append({
            "run_index": run_index,
            "status": "ok",
            "frames": frames,
            "wall_s": round(wall_s, 4),
            "fps": round(frames / wall_s, 3) if wall_s > 0 else None,
            "output_mb": round(out_path.stat().st_size / (1024 * 1024), 3),
            "output_sha256": file_sha256(out_path),
            "probe": probe_output(out_path),
        })
        if not keep_outputs:
            out_path.unlink(missing_ok=True)
        run_index += 1

    ok_runs = [r for r in group_runs if r.get("status") == "ok"]
    fps_values = [
        float(r["fps"]) for r in ok_runs if r.get("fps") is not None
    ]
    if warmup_done:
        warmup_note = (
            "done (run 0, redone after gpu_surface fallback)"
            if ref_mode_fallback else "done (run 0)"
        )
    elif ref_mode_fallback:
        warmup_note = "failed: gpu_surface fallback warmup also failed"
    else:
        warmup_note = "n/a"
    payload: Dict[str, Any] = {
        "status": "ok" if fps_values else "error",
        "group": "ffmpeg_gpu_ref",
        "vendor": vendor,
        "ref_mode": (
            "hwaccel_download" if ref_mode_fallback else "gpu_surface"
        ),
        "ref_mode_fallback": ref_mode_fallback,
        "ref_mode_fallback_reason": ref_mode_fallback_reason,
        "runs": group_runs,
        "warmup": warmup_note,
        "median_fps": median(fps_values) if fps_values else None,
    }
    return payload


def _ffmpeg_bin() -> str:
    from src.shared.ffmpeg_locator import find_ffmpeg

    return find_ffmpeg() or ""


def _dump_json(path: Path, payload: Dict[str, Any]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)


# ======================================================================
# 结果组装与报告渲染（纯函数，可单测）
# ======================================================================

def summarize_group(group_payload: Dict[str, Any]) -> Dict[str, Any]:
    """把一组运行折叠成报告行：中位 FPS / p50 / p95 / 输出体积。"""
    runs = group_payload.get("runs", [])
    ok_runs = [r for r in runs if r.get("status") == "ok"]
    summary: Dict[str, Any] = {
        "status": group_payload.get("status", "error"),
        "runs_recorded": len(runs),
        "runs_ok": len(ok_runs),
    }
    if not ok_runs:
        summary["error"] = group_payload.get(
            "error",
            group_payload.get(
                "blocking",
                next((r.get("error") for r in runs if r.get("error")), ""),
            ),
        )
        return summary
    fps_values = [float(r["fps"]) for r in ok_runs]
    summary["median_fps"] = round(median(fps_values), 3)
    p50s = [r["frame_ms_p50"] for r in ok_runs if "frame_ms_p50" in r]
    p95s = [r["frame_ms_p95"] for r in ok_runs if "frame_ms_p95" in r]
    if p50s:
        summary["median_frame_ms_p50"] = round(median([float(v) for v in p50s]), 3)
    if p95s:
        summary["max_frame_ms_p95"] = round(max(float(v) for v in p95s), 3)
    sizes = [float(r["output_mb"]) for r in ok_runs]
    summary["median_output_mb"] = round(median(sizes), 3)
    shas = {r.get("output_sha256") for r in ok_runs}
    summary["output_sha256_stable"] = len(shas) == 1
    summary["output_sha256"] = sorted(shas)[0] if len(shas) == 1 else sorted(shas)
    probes = [r.get("probe", {}) for r in ok_runs]
    if probes:
        summary["probe"] = probes[0]
    if group_payload.get("hw_fallback_reasons"):
        summary["hw_fallback_reasons"] = group_payload["hw_fallback_reasons"]
    if group_payload.get("ref_mode_fallback_reason"):
        summary["ref_mode_fallback_reason"] = group_payload[
            "ref_mode_fallback_reason"
        ]
    return summary


def assemble_results(
    meta: Dict[str, Any],
    protocol: Dict[str, Any],
    case_results: Dict[str, Dict[str, Any]],
    input_manifest: Optional[Dict[str, Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """组装最终 JSON 报告结构（schema 见模块 docstring）。"""
    cases_out: Dict[str, Any] = {}
    for case_id, payload in case_results.items():
        entry: Dict[str, Any] = dict(payload)
        if payload.get("status") == "declared_not_run":
            cases_out[case_id] = entry
            continue
        groups = payload.get("groups", {})
        entry["group_summaries"] = {
            group: summarize_group(gpayload)
            for group, gpayload in groups.items()
        }
        ref = entry["group_summaries"].get("ffmpeg_gpu_ref", {})
        ratios: Dict[str, Any] = {}
        ref_fps = ref.get("median_fps")
        if isinstance(ref_fps, (int, float)) and ref_fps > 0:
            for other in ("cpu_pipeline", "hw_download"):
                other_fps = entry["group_summaries"].get(other, {}).get("median_fps")
                if isinstance(other_fps, (int, float)):
                    ratios[f"{other}_vs_ffmpeg"] = round(other_fps / ref_fps, 3)
        if ratios:
            entry["baseline_ratios"] = ratios
        cases_out[case_id] = entry
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": KIND,
        "meta": meta,
        "protocol": protocol,
        "inputs": input_manifest or {},
        "cases": cases_out,
    }


def render_markdown(results: Dict[str, Any]) -> str:
    """按 P9C-02 配对约定渲染 Markdown 报告。"""
    meta = results.get("meta", {})
    protocol = results.get("protocol", {})
    lines: List[str] = []
    lines.append(f"# Windows 时间线导出基准（{meta.get('gpu_tag', 'unknown')}）")
    lines.append("")
    lines.append(
        f"日期 {meta.get('date', '')} ｜ 原始数据："
        f"`{meta.get('json_name', '')}`"
    )
    lines.append("")
    lines.append(
        f"协议：输出 {protocol.get('frames')} 帧 ｜ 预热 "
        f"{protocol.get('warmup_frames')} 帧 ｜ 连续 "
        f"{protocol.get('runs')} 次 ｜ 中位数报告 ｜ topos "
        f"@ {meta.get('git_rev', '?')}"
    )
    lines.append("")
    lines.append("## 输入材料")
    lines.append("")
    lines.append("| 文件 | 状态 | SHA-256 | 探针 |")
    lines.append("|---|---|---|---|")
    for name, item in results.get("inputs", {}).items():
        probe = item.get("probe", {})
        probe_note = (
            f"{probe.get('codec', '')} {probe.get('width', 0)}x"
            f"{probe.get('height', 0)} {probe.get('pix_fmt', '')}"
        ).strip()
        lines.append(
            f"| {name} | {item.get('status', 'unknown')} | "
            f"{item.get('sha256', '—')} | {probe_note or item.get('probe_error', '—')} |"
        )
    lines.append("")
    lines.append("| 用例 | 组 | 状态 | 中位 FPS | p50 帧时 | p95 帧时 | 输出 MB | 备注 |")
    lines.append("|---|---|---|---|---|---|---|---|")
    for case_id, entry in results.get("cases", {}).items():
        title = entry.get("title", case_id)
        if entry.get("status") == "declared_not_run":
            lines.append(
                f"| {case_id} {title} | — | declared_not_run | — | — | — | — | "
                f"{entry.get('blocking', '')} |"
            )
            continue
        for group, summary in entry.get("group_summaries", {}).items():
            note_bits: List[str] = []
            if group == "ffmpeg_gpu_ref":
                note_bits.append(f"ref={entry.get('ref_mode', 'gpu_surface')}")
                if summary.get("ref_mode_fallback_reason"):
                    note_bits.append("ref_fallback_reason_recorded")
            if summary.get("status") == "declared_not_run":
                note_bits.append(summary.get("error", "capability gate"))
            if summary.get("hw_fallback_reasons"):
                note_bits.append("hw_fallback")
            lines.append(
                f"| {case_id} {title} | {group} | {summary.get('status')} | "
                f"{summary.get('median_fps', '—')} | "
                f"{summary.get('median_frame_ms_p50', '—')} | "
                f"{summary.get('max_frame_ms_p95', '—')} | "
                f"{summary.get('median_output_mb', '—')} | "
                f"{'；'.join(note_bits)} |"
            )
    lines.append("")
    lines.append("## 基线比值（现状，非 D3D11 门禁）")
    lines.append("")
    lines.append("| 用例 | 比值 | 数值 |")
    lines.append("|---|---|---|")
    for case_id, entry in results.get("cases", {}).items():
        for name, value in entry.get("baseline_ratios", {}).items():
            lines.append(f"| {case_id} | {name} | {value} |")
    lines.append("")
    lines.append("## 结论")
    lines.append("")
    conclusions = entry_conclusions(results)
    for item in conclusions:
        lines.append(f"- {item}")
    lines.append("")
    return "\n".join(lines)


def entry_conclusions(results: Dict[str, Any]) -> List[str]:
    """从结果推导结论要点（诚实陈述，不粉饰失败）。"""
    items: List[str] = []
    for case_id, entry in results.get("cases", {}).items():
        if entry.get("status") == "declared_not_run":
            continue
        summaries = entry.get("group_summaries", {})
        failed = [
            g for g, s in summaries.items()
            if s.get("status") not in {"ok", "declared_not_run"}
        ]
        if failed:
            items.append(
                f"{case_id}：组 {','.join(failed)} 失败——"
                "按计划 §6.1，失败组必须先归因，不得进入下一步门禁。"
            )
        declared = [
            (g, s.get("error", "capability gate"))
            for g, s in summaries.items()
            if s.get("status") == "declared_not_run"
        ]
        for group, reason in declared:
            items.append(f"{case_id}：组 {group} 未运行——{reason}。")
        ratios = entry.get("baseline_ratios", {})
        hw = ratios.get("hw_download_vs_ffmpeg")
        if isinstance(hw, (int, float)):
            items.append(
                f"{case_id}：现有硬解下载路径达到 FFmpeg GPU 参照的 "
                f"{hw * 100:.1f}%（计划 §4.3 的 85% 门禁适用于未来 D3D11 路径，"
                "此值是现状基线）。"
            )
    return items


# ======================================================================
# CLI
# ======================================================================

def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Windows 时间线导出基准（计划 2026-09-15 步骤 0）",
    )
    parser.add_argument("--cases", default=",".join(ALL_CASE_IDS),
                        help="逗号分隔用例 id（T0-T10），默认全部")
    parser.add_argument("--groups", default=",".join(ALL_GROUP_IDS),
                        help="逗号分隔对照组 id，默认全部")
    parser.add_argument("--frames", type=int, default=480,
                        help="每组正式导出帧数（默认 480）")
    parser.add_argument("--warmup", type=int, default=60,
                        help="预热帧数（默认 60，0 关闭预热）")
    parser.add_argument("--runs", type=int, default=3,
                        help="正式测量次数（默认 3）")
    parser.add_argument("--bitrate-mbps", type=int, default=20,
                        help="H.264 VBR 目标码率（默认 20 Mbps）")
    parser.add_argument("--tag", default="", help="机器 slug（默认自动探测）")
    parser.add_argument("--out-dir", default=str(RESULTS_DIR),
                        help="报告输出目录")
    parser.add_argument("--keep-outputs", action="store_true",
                        help="保留输出 mp4（默认测量后删除）")
    parser.add_argument("--timeout-s", type=float, default=900.0,
                        help="单组子进程/ffmpeg 超时（秒）")
    parser.add_argument("--dry-run", action="store_true",
                        help="只打印用例矩阵与组计划，不执行")
    parser.add_argument("--run-group", nargs=2, metavar=("CASE", "GROUP"),
                        help=argparse.SUPPRESS)  # 子进程内部入口
    parser.add_argument("--emit-json", default="", help=argparse.SUPPRESS)
    parser.add_argument("--bitrate-bps", type=int, default=0,
                        help=argparse.SUPPRESS)  # 子进程内部入口（精确码率）
    args = parser.parse_args(argv)

    # 子进程内部入口
    if args.run_group:
        case_id, group = args.run_group
        if not args.emit_json:
            parser.error("--run-group 需要 --emit-json")
        bitrate = args.bitrate_bps or args.bitrate_mbps * 1_000_000
        return run_group_main(
            case_id, group, args.frames, args.warmup, args.runs,
            bitrate, Path(args.emit_json), keep_outputs=args.keep_outputs,
        )

    cases = case_matrix()
    case_ids = [c.strip().upper() for c in args.cases.split(",") if c.strip()]
    group_ids = [g.strip() for g in args.groups.split(",") if g.strip()]
    unknown = [c for c in case_ids if c not in ALL_CASE_IDS] + [
        g for g in group_ids if g not in ALL_GROUP_IDS
    ]
    if unknown:
        parser.error(f"未知用例/组: {unknown}")

    date = time.strftime("%Y-%m-%d")
    tag = args.tag or machine_tag()
    bitrate_bps = args.bitrate_mbps * 1_000_000

    if args.dry_run:
        print(f"tag={tag} frames={args.frames} warmup={args.warmup} "
              f"runs={args.runs} bitrate={args.bitrate_mbps}Mbps")
        for case in cases:
            if case.case_id not in case_ids:
                continue
            flag = "READY" if case.runner_ready else f"BLOCKED ({case.blocking})"
            print(f"  {case.case_id} {case.title}: {flag}")
            if case.runner_ready:
                for group in group_ids:
                    print(f"    -> {group}")
        return 0

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    if args.keep_outputs:
        workdir = out_dir / f"outputs_{tag}_{time.strftime('%Y%m%d_%H%M%S')}"
        workdir.mkdir(parents=True, exist_ok=True)
    else:
        workdir = Path(tempfile.mkdtemp(prefix="topos_bench_export_"))

    meta = {
        "date": date,
        "gpu_tag": tag,
        "os": f"{sys.platform} {os.name}",
        "python": sys.version.split()[0],
        "git_rev": git_rev(),
        "bitrate_bps": bitrate_bps,
    }
    selected_cases = tuple(c for c in cases if c.case_id in case_ids)
    input_manifest = build_input_manifest(selected_cases)

    case_results: Dict[str, Dict[str, Any]] = {}
    failure_observed = False
    artifacts_preserved = False
    for case in cases:
        if case.case_id not in case_ids:
            continue
        if not case.runner_ready:
            case_results[case.case_id] = {
                "title": case.title,
                "status": "declared_not_run",
                "blocking": case.blocking,
            }
            continue
        print(f"[{case.case_id}] {case.title}", flush=True)
        groups_out: Dict[str, Any] = {}
        ref_meta: Dict[str, Any] = {}
        for group in group_ids:
            print(f"  group={group} ...", flush=True)
            started = time.perf_counter()
            if group == "ffmpeg_gpu_ref":
                payload = run_ffmpeg_ref_group(
                    case, args.frames, args.warmup, args.runs,
                    bitrate_bps, workdir, args.timeout_s,
                    keep_outputs=args.keep_outputs,
                )
                ref_meta = {
                    "ref_mode": payload.get("ref_mode"),
                    "vendor": payload.get("vendor"),
                }
            else:
                payload = run_group_in_subprocess(
                    case, group, args.frames, args.warmup, args.runs,
                    bitrate_bps, workdir, args.timeout_s,
                    keep_outputs=args.keep_outputs,
                )
            payload["elapsed_s"] = round(time.perf_counter() - started, 2)
            groups_out[group] = payload
            if payload.get("status") not in {"ok", "declared_not_run"}:
                failure_observed = True
            print(f"  group={group} status={payload.get('status')} "
                  f"({payload['elapsed_s']}s)", flush=True)
        case_results[case.case_id] = {
            "title": case.title,
            "purpose": case.purpose,
            "coverage": case.coverage,
            "status": "ok",
            "groups": groups_out,
            **ref_meta,
        }

    protocol = {
        "frames": args.frames,
        "warmup_frames": args.warmup,
        "runs": args.runs,
        "bitrate_mode": "vbr",
        "report": "median of runs; frame times from progress callback deltas",
        "case_matrix": list(ALL_CASE_IDS),
        "groups": list(ALL_GROUP_IDS),
        "input_manifest": "sha256 + PyAV video probe for every selected material",
        "output_validation": "PyAV decoded frame count + codec/pixel format/size + SHA-256",
        "gpu_evidence": (
            "D3D11 requires adapter LUID, native decode/render/yuv/encode timestamps "
            "and ExportGpuTrail cpu_upload_bytes=0/cpu_download_bytes=0"
        ),
    }
    if failure_observed and not args.keep_outputs:
        # The plan requires failed runs to remain inspectable.  Promote the
        # temporary work directory before cleanup so output packets and child
        # JSON are available next to the final report.
        failure_dir = out_dir / (
            f"failed_outputs_{tag}_{time.strftime('%Y%m%d_%H%M%S')}"
        )
        try:
            workdir.rename(failure_dir)
        except OSError:
            shutil.copytree(workdir, failure_dir, dirs_exist_ok=True)
        else:
            workdir = failure_dir
        artifacts_preserved = True
        meta["failure_artifacts_dir"] = str(failure_dir)
    json_name = f"windows_timeline_export_{tag}_{date}.json"
    md_name = f"windows_timeline_export_{tag}_{date}.md"
    meta["json_name"] = json_name
    results = assemble_results(meta, protocol, case_results, input_manifest)

    json_path = out_dir / json_name
    _dump_json(json_path, results)
    (out_dir / md_name).write_text(
        render_markdown(results), encoding="utf-8",
    )
    print(f"报告：{json_path}")
    print(f"报告：{out_dir / md_name}")

    if not args.keep_outputs and not artifacts_preserved:
        for leftover in workdir.glob("*.mp4"):
            leftover.unlink(missing_ok=True)
        for leftover in workdir.glob("*_result.json"):
            leftover.unlink(missing_ok=True)
        try:
            workdir.rmdir()
        except OSError:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
