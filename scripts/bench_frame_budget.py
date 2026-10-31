#!/usr/bin/env python
"""帧预算基准 harness（计划 A A0.1/A0.2；docs/perf_frame_budget_schema_2026-10.md）。

目标：在**真实生产原语**（真实解码源路由、YuvGpuUploader、GlPassTimer、
FrameTraceCollector/TelemetryAggregator 的 schema v3 通道）上，对每帧做
decode / import_upload / composite(GPU query) / readback / present 分段归因，
输出带覆盖率与路由证据的 JSON 报告。本 harness 不做任何优化变更本身。

两种运行模式：
- offscreen（默认）：moderngl standalone context，无窗口。测量
  decode→planar 上传→GPU 合成 pass→（可选快照回读）；**不验收上屏、
  vsync 或可见掉帧**（计划 A §2.2 运行模式纪律）。
- window：QOpenGLWindow 真实 swap。在 offscreen 段之外追加
  submitted→frameSwapped 的 present 段观测（swap 级观测点，非物理
  屏幕扫描时间，不得如此解读）。

用法示例：
  .venv/bin/python scripts/bench_frame_budget.py --mode offscreen \
      --generate-media bench_out/frame_budget/media --frames 120
  .venv/bin/python scripts/bench_frame_budget.py --mode window \
      --material path/to/clip.mov --frames 240 --warmup 30

退出码：0 = 全部素材完成；1 = 致命错误（无素材）；2 = 部分或全部素材失败
（详见各 JSON 的 errors 列表）。
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import subprocess
import sys
import time

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import numpy as np  # noqa: E402
import moderngl  # noqa: E402

from src.shared.gpu.gl_pass_timer import GlPassTimer  # noqa: E402
from src.shared.video.frame_trace_schema import next_frame_trace_id  # noqa: E402
from src.shared.video.playback_frame_trace import (  # noqa: E402
    EditPlaybackFrameTrace,
    FrameTraceCollector,
)
from src.features.edit.core.realtime_playback.telemetry import (  # noqa: E402
    PlaybackFrameTelemetry,
    TelemetryAggregator,
)
from src.shared.video.pixel_copy_counters import (  # noqa: E402
    add_pixel_copy,
    CopyKind,
    reset_pixel_copies,
    snapshot_pixel_copies,
)

IS_MAC = sys.platform == "darwin"

# 合成 pass：全屏三角采样上传纹理 → RGBA8 FBO（代表性 GPU 工作量：
# 一次纹理采样 + w*h*4 写出）。
_COMPOSITE_VS = """
#version 330
out vec2 v_uv;
void main() {
    vec2 p = vec2((gl_VertexID << 1) & 2, gl_VertexID & 2);
    v_uv = p;
    gl_Position = vec4(p * 2.0 - 1.0, 0.0, 1.0);
}
"""
_COMPOSITE_FS = """
#version 330
in vec2 v_uv;
out vec4 out_color;
uniform sampler2D src_tex;
void main() { out_color = texture(src_tex, v_uv); }
"""


# ---------------------------------------------------------------------------
# 环境与素材
# ---------------------------------------------------------------------------


def _git_info() -> dict:
    def run(*args: str) -> str:
        try:
            return subprocess.check_output(
                ["git", *args], cwd=_REPO_ROOT, text=True,
                stderr=subprocess.DEVNULL).strip()
        except Exception:
            return ""
    return {
        "head": run("rev-parse", "HEAD"),
        "branch": run("branch", "--show-current"),
        "dirty_files": len(run("status", "--porcelain").splitlines()),
    }


def environment_record(allow_dirty: bool) -> dict:
    env: dict = {
        "schema": "bench_frame_budget.env.v1",
        "recorded_at_utc": _dt.datetime.now(_dt.timezone.utc).isoformat(),
        "python": sys.version.split()[0],
        "numpy": np.__version__,
        "moderngl": getattr(moderngl, "__version__", ""),
        "platform": sys.platform,
    }
    try:
        import av
        env["av"] = av.__version__
        env["ffmpeg"] = {k: ".".join(str(x) for x in v)
                         for k, v in av.library_versions.items()}
    except Exception as e:
        env["av"] = f"unavailable: {e}"
    try:
        import PyQt6.QtCore as qc
        env["pyqt6"] = qc.PYQT_VERSION_STR
        env["qt"] = qc.QT_VERSION_STR
    except Exception as e:
        env["pyqt6"] = f"unavailable: {e}"
    env["git"] = _git_info()
    # bench_manifest 指纹：仅当 Topos native codec 实际参与时强校验；
    # 纯 H264/HEVC/ProRes/DNxHR 场景记录降级原因（计划 A §2.2：
    # 不把 codec 库的存在当普通基准前置条件）。
    try:
        tools_dir = os.path.join(_REPO_ROOT, "tools")
        if tools_dir not in sys.path:
            sys.path.insert(0, tools_dir)
        import bench_manifest
        env["bench_manifest"] = bench_manifest.build_manifest(allow_dirty=allow_dirty)
    except Exception as e:
        env["bench_manifest"] = {
            "manifest": "light_no_topos",
            "reason": f"build_manifest unavailable: {type(e).__name__}: {e}",
        }
    return env


def generate_media(out_dir: str) -> list:
    """生成最小素材矩阵（无现成素材时的默认集；已存在则跳过）。"""
    import av

    specs = [
        # 编码器只负责造出目标格式的素材文件——被测对象是解码路由。
        # hevc 用 libx265（本机 hevc_videotoolbox avcodec_open2 EINVAL）；
        # DNxHR 在本 PyAV 构建无法开 dnxhr profile（如实记录并跳过，
        # 真实 DNxHR 素材可用 --material 供给）。
        ("h264_3840x2160_8bit.mp4", "libx264", "yuv420p", 3840, 2160,
         {"crf": "18"}),
        ("hevc_2560x1440_10bit.mov", "libx265", "yuv420p10le",
         2560, 1440, {"crf": "18"}),
        ("prores422hq_2560x1440_10bit.mov", "prores_ks", "yuv422p10le",
         2560, 1440, {"profile": "3"}),
        ("dnxhr_hqx_2560x1440.mov", "dnxhd", "yuv422p", 2560, 1440,
         {"vprofile": "dnxhr_hqx"}),
    ]
    os.makedirs(out_dir, exist_ok=True)
    generated, failures = [], []
    for name, codec, pix_fmt, w, h, opts in specs:
        path = os.path.join(out_dir, name)
        if os.path.exists(path) and os.path.getsize(path) > 0:
            generated.append(path)
            continue
        try:
            container = av.open(path, "w")
            stream = container.add_stream(codec, rate=24)
            stream.width, stream.height = w, h
            stream.pix_fmt = pix_fmt
            stream.options = dict(opts)
            # uint16 梯度避免 uint8 运算溢出提升出大临时数组。
            grad = (np.arange(w, dtype=np.uint32)[None, :]
                    + np.arange(h, dtype=np.uint32)[:, None])
            for i in range(60):
                frame_i = np.empty((h, w, 3), dtype=np.uint8)
                for c in range(3):
                    frame_i[..., c] = (
                        (grad + i * 7 + c * 40) % 256).astype(np.uint8)
                frame = av.VideoFrame.from_ndarray(frame_i, format="rgb24")
                frame = frame.reformat(width=w, height=h, format=pix_fmt)
                for packet in stream.encode(frame):
                    container.mux(packet)
            for packet in stream.encode():
                container.mux(packet)
            container.close()
            generated.append(path)
        except Exception as e:
            failures.append({"material": name,
                             "error": f"{type(e).__name__}: {e}"})
    if failures:
        print(f"[generate-media] 生成失败（跳过）: {failures}", file=sys.stderr)
    return generated


# ---------------------------------------------------------------------------
# 解码源选择（记录路由证据）
# ---------------------------------------------------------------------------


def open_decode_source(path: str) -> tuple:
    """按生产路由顺序尝试打开解码源，返回 (source, route_info)。

    路由证据（H1/H5）：零拷贝能力探测结果（真实 capability probe）、
    VT planar 会话可用性与回退原因、最终 FFmpeg planar 路由。
    """
    route: dict = {"path": os.path.basename(path)}
    if IS_MAC:
        try:
            from src.shared.media.macos_vt.capability import (
                get_zero_copy_capability,
            )
            cap = get_zero_copy_capability()
            route["zero_copy_capability"] = {
                "available": bool(getattr(cap, "available", False)),
                "reason": str(getattr(cap, "reason", "") or ""),
            }
        except Exception as e:
            route["zero_copy_capability"] = {
                "available": False, "reason": f"probe_failed: {e}"}
        try:
            from src.shared.media.macos_vt.source import MacOSVtPlanarSource
            vt = MacOSVtPlanarSource()
            if vt.open(path):
                route["decode_route"] = "macos_vt_planar"
                route["vt_fallback_reason"] = str(
                    getattr(vt, "_fallback_reason", "") or "")
                return vt, route
            route["vt_fallback_reason"] = str(
                getattr(vt, "_fallback_reason", "") or "open_failed")
        except Exception as e:
            route["vt_fallback_reason"] = f"exception: {type(e).__name__}: {e}"
    from src.shared.media.ffmpeg_source import FFmpegMediaSource
    source = FFmpegMediaSource(
        path, preserve_planes=True, output_mode="planar_yuv")
    source.open()
    route["decode_route"] = "ffmpeg_planar"
    return source, route


def try_open_zero_copy(path: str) -> tuple:
    """尝试零拷贝路由（A2.1 证据）：VT 零拷贝源 + IOSurface→GL 桥。

    必须在 GL context current 的线程调用（bridge.initialize 绑定当前
    CGL context）。成功返回 (source, bridge, "")；失败返回 (None, None,
    reason)——reason 是生产同款 fallback 词表证据。
    """
    if not IS_MAC:
        return None, None, "not_macos"
    try:
        from src.shared.media.macos_vt.source import MacOSVtZeroCopySource
        from src.shared.media.macos_vt.iosurface_gl_bridge import (
            IOSurfaceGLBridge,
        )
        source = MacOSVtZeroCopySource()
        if not source.open(path):
            return None, None, str(
                getattr(source, "_fallback_reason", "") or "open_failed")
        bridge = IOSurfaceGLBridge()
        if not bridge.initialize(planes_per_frame=2):
            return None, None, str(
                getattr(bridge, "last_fallback_reason", "")
                or "bridge_init_failed")
        return source, bridge, ""
    except Exception as e:
        return None, None, f"exception: {type(e).__name__}: {e}"


def read_next_frame(source, route: dict):
    """顺序读帧；EOF 时 seek 回 0（记 wrap 次数，不静默丢帧）。"""
    try:
        frame = source.read_next_frame()
    except Exception:
        frame = None
    if frame is None:
        route["eof_wraps"] = route.get("eof_wraps", 0) + 1
        source.read_frame(0)
        frame = source.read_next_frame()
    return frame


def _plane_geometry(frame) -> tuple:
    """返回 (is_semiplanar, bit_depth, subsampling)。"""
    planes = frame.planes
    pixel_format = str(getattr(frame, "pixel_format", "") or "")
    bit_depth = 8 if np.dtype(planes[0].dtype).itemsize == 1 else 10
    for token in ("p010", "p016", "10le", "10be", "12le", "12be", "16le", "16be"):
        if token in pixel_format:
            bit_depth = 16 if token.startswith("16") else (
                10 if token.startswith(("p010", "10")) else 12)
            break
    h, w = planes[0].shape[:2]
    if len(planes) >= 3:
        uh, uw = planes[1].shape[:2]
        if uw * 2 <= w and uh * 2 <= h:
            subsampling = "420"
        elif uw * 2 <= w:
            subsampling = "422"
        else:
            subsampling = "444"
    else:
        subsampling = "420"
    return len(planes) == 2, bit_depth, subsampling


# ---------------------------------------------------------------------------
# 场景执行
# ---------------------------------------------------------------------------


class _BenchScene:
    """offscreen 场景：uploader + composite FBO + GlPassTimer。"""

    def __init__(self, ctx) -> None:
        from src.shared.video.yuv_gpu_upload import YUVGPUUploader
        self.ctx = ctx
        self.uploader = YUVGPUUploader(ctx)
        self.timer = GlPassTimer(ctx)
        self.program = ctx.program(
            vertex_shader=_COMPOSITE_VS, fragment_shader=_COMPOSITE_FS)
        self.vao = ctx.vertex_array(self.program, [])
        self.fbo = None

    def ensure_fbo(self, w: int, h: int) -> None:
        if self.fbo is not None and self.fbo.size == (w, h):
            return
        if self.fbo is not None:
            self.fbo.color_attachments[0].release()
            self.fbo.release()
        tex = self.ctx.texture((w, h), components=4, dtype="f1")
        self.fbo = self.ctx.framebuffer(color_attachments=[tex])

    def upload(self, frame, index: int) -> tuple:
        semiplanar, bit_depth, subsampling = _plane_geometry(frame)
        if semiplanar:
            y, uv = frame.planes[0], frame.planes[1]
            if bit_depth > 8:
                textures = self.uploader.upload_p010(
                    y, uv, index, bit_depth=bit_depth)
            else:
                textures = self.uploader.upload_nv12(
                    y, uv, index, bit_depth=bit_depth)
        else:
            y, u, v = frame.planes[:3]
            textures = self.uploader.upload_yuv_planar(
                y, u, v, index, bit_depth=bit_depth,
                subsampling=subsampling)
        return textures, bit_depth

    def upload_mode(self) -> str:
        return str(getattr(self.uploader, "_last_upload_mode", ""))

    def r16ui_state(self) -> dict:
        uploader = self.uploader
        return {
            "r16ui_self_test_passed": bool(
                getattr(uploader, "_r16ui_self_test_passed", False)),
            "r16ui_disabled_reason": str(
                getattr(uploader, "_r16ui_disabled_reason", "") or ""),
        }


def _trace_deadline_ns(source) -> int:
    meta = getattr(source, "metadata", None)
    if meta is not None and getattr(meta, "fps", None) is not None:
        try:
            return int(1e9 / float(meta.fps.to_fps()))
        except Exception:
            pass
    return int(1e9 / 25.0)


def run_offscreen(material: str, args) -> dict:
    ctx = moderngl.create_standalone_context()
    scene = _BenchScene(ctx)
    source, route = open_decode_source(material)
    route["gl"] = {k: ctx.info.get(k, "")
                   for k in ("GL_RENDERER", "GL_VERSION", "GL_VENDOR")}
    route["gpu_timer_unsupported"] = scene.timer.unsupported

    collector = FrameTraceCollector(max_frames=max(600, args.frames * 2))
    collector.start_session(page="bench_frame_budget/offscreen")
    telemetry = TelemetryAggregator(window=max(8, args.frames))
    reset_pixel_copies()

    # A2.1 证据：零拷贝路由探测（VT 零拷贝源 + IOSurface→GL 桥，本线程
    # GL context current）。成功则本素材改测零拷贝逐帧成本（decode retain +
    # bridge bind），失败记录生产同款 fallback 原因。
    zc_source = zc_bridge = None
    if getattr(args, "zero_copy_probe", True):
        zc_source, zc_bridge, zc_reason = try_open_zero_copy(material)
        if zc_source is not None:
            route["decode_route"] = "macos_vt_zero_copy"
            route["zero_copy_open"] = True
        else:
            route["zero_copy_probe_fallback"] = zc_reason

    # A1.1：GC 停顿观测（sink=collector → trace summary 的 gc 分节）。
    from src.shared.video.gc_pause_observer import GcPauseObserver
    gc_observer = GcPauseObserver(sink=collector)
    gc_observer.start()

    frame_tag_to_trace: dict = {}
    deadline_ns = _trace_deadline_ns(source)
    decode_times: list = []
    upload_times: list = []
    import_times: list = []
    pts_seen: list = []  # N14：重复/停滞帧检出（解码 PTS 序列）
    errors: list = []
    started = time.perf_counter()
    index = 0
    total = args.warmup + args.frames
    while index < total:
        i = index
        index += 1
        try:
            t0 = time.perf_counter()
            if zc_source is not None:
                # ---- 零拷贝路由：VT retain（decode）+ IOSurface bind（import）
                vt_frame = zc_source.read_frame_pixel_buffer(i)
                if vt_frame is None:
                    route["eof_wraps"] = route.get("eof_wraps", 0) + 1
                    vt_frame = zc_source.read_frame_pixel_buffer(0)
                decode_ms = (time.perf_counter() - t0) * 1000.0
                if vt_frame is None:
                    errors.append({"frame": i, "error": "decode_none"})
                    continue
                trace_id = next_frame_trace_id()
                warmup = i < args.warmup
                collector.record(EditPlaybackFrameTrace(
                    frame_index=i, trace_id=trace_id,
                    page="bench_frame_budget",
                    decode_path="macos_vt_iosurface_zero_copy",
                    render_path="gpu_direct",
                    deadline_ns=0 if warmup else deadline_ns,
                ))
                frame_tag_to_trace[i] = trace_id
                t1 = time.perf_counter()
                bound = zc_bridge.bind_pixel_buffer(
                    pixel_buffer=vt_frame.pixel_buffer,
                    frame_index=i,
                    pixel_format=str(getattr(vt_frame, "pixel_format", "nv12")),
                    bit_depth=int(getattr(vt_frame, "bit_depth", 8)),
                )
                import_ms = (time.perf_counter() - t1) * 1000.0
                if bound is None:
                    errors.append({
                        "frame": i,
                        "error": f"zero_copy_bind_failed: "
                                 f"{getattr(zc_bridge, 'last_fallback_reason', '')}"})
                    vt_frame.release()
                    continue
                planes, bridge_release = bound
                # 基准只计时不再采样：立即归还（生产为 fence 延迟释放）。
                try:
                    bridge_release.release()
                except Exception:
                    pass
                vt_frame.release()
                if not warmup:
                    decode_times.append(decode_ms)
                    import_times.append(import_ms)
                    telemetry.record(PlaybackFrameTelemetry(
                        timeline_frame=i, trace_id=trace_id,
                        mode="realtime", priority="playhead",
                        source_kinds=("disk_proxy",),
                    ))
                    telemetry.enrich_last(
                        i, decode_ms=decode_ms, total_ms=decode_ms + import_ms,
                        trace_id=trace_id, upload_status="not_applicable")
                    collector.annotate(
                        trace_id,
                        import_upload_status="not_applicable",
                        extra={
                            "zero_copy_import": True,
                            "zero_copy_import_ms": round(import_ms, 3),
                            "planes": len(planes),
                        })
                continue  # 零拷贝路由不跑 planar 上传/合成 pass

            frame = read_next_frame(source, route)
            decode_ms = (time.perf_counter() - t0) * 1000.0
            if frame is None:
                errors.append({"frame": i, "error": "decode_none"})
                continue
            trace_id = next_frame_trace_id()
            warmup = i < args.warmup
            collector.record(EditPlaybackFrameTrace(
                frame_index=i, trace_id=trace_id,
                page="bench_frame_budget",
                decode_path=str(getattr(frame, "decode_path", "")
                                or route["decode_route"]),
                render_path="gpu_planar_upload",
                deadline_ns=0 if warmup else deadline_ns,
            ))
            frame_tag_to_trace[i] = trace_id

            t1 = time.perf_counter()
            textures, bit_depth = scene.upload(frame, i)
            upload_ms = (time.perf_counter() - t1) * 1000.0
            pts_seen.append(getattr(frame, "pts", None))  # N14
            nbytes = int(sum(int(p.nbytes) for p in frame.planes))
            add_pixel_copy(CopyKind.CPU_UPLOAD, nbytes)
            add_pixel_copy(CopyKind.TEXTURE_WRITE, nbytes)

            w, h = int(frame.width), int(frame.height)
            scene.timer.begin_frame(tag=i)
            with scene.timer.measure("composite"):
                scene.ensure_fbo(w, h)
                scene.fbo.use()
                textures[0].use(0)
                scene.program["src_tex"].value = 0
                scene.vao.render(mode=moderngl.TRIANGLES, vertices=3)
            completed = scene.timer.collect()

            if not warmup:
                decode_times.append(decode_ms)
                upload_times.append(upload_ms)
                telemetry.record(PlaybackFrameTelemetry(
                    timeline_frame=i, trace_id=trace_id,
                    mode="realtime", priority="playhead",
                    source_kinds=("disk_proxy",),
                ))
                telemetry.enrich_last(
                    i, decode_ms=decode_ms,
                    total_ms=decode_ms + upload_ms,
                    trace_id=trace_id, upload_ms=upload_ms)
                collector.annotate(
                    trace_id,
                    cpu_upload_bytes=nbytes, texture_write_bytes=nbytes,
                    import_upload_ms=upload_ms,
                    import_upload_status="measured",
                    extra={"upload_mode": scene.upload_mode(),
                           "bit_depth": bit_depth})
            # GPU query 迟到回填（真实时序：结果属于 RING_DEPTH-1 帧前的
            # 发起帧； annotate 找不到目标时计 missed，不静默）。
            for entry in completed:
                origin = int(entry.get("tag", -1))
                ms = entry.get("ms")
                origin_trace = frame_tag_to_trace.get(origin)
                if origin_trace is None or ms is None:
                    continue
                collector.annotate(
                    origin_trace, gpu_submit_ms=round(float(ms), 3),
                    gpu_time_unavailable=False)
            # 受控快照回读（分类演练 + 计数）
            if args.snapshot_every and i % args.snapshot_every == 0:
                small = scene.fbo.read(
                    viewport=(0, 0, max(4, w // 4), max(4, h // 4)),
                    components=3, alignment=1)
                add_pixel_copy(CopyKind.CPU_DOWNLOAD, len(small))
                if not warmup:
                    collector.annotate(
                        trace_id, cpu_download_bytes=len(small),
                        readback_kind="explicit_snapshot")
        except Exception as e:  # noqa: BLE001 单素材失败不终止整个基准
            errors.append({"frame": i, "error": f"{type(e).__name__}: {e}"})
    wall_s = time.perf_counter() - started
    gc_summary = gc_observer.stop()

    try:
        source.close()
    except Exception:
        pass
    if zc_source is not None:
        try:
            zc_source.close()
        except Exception:
            pass
    try:
        ctx.release()
    except Exception:
        pass

    ok = len(errors) == 0 and len(decode_times) >= args.frames * 0.9
    pixel_copies = snapshot_pixel_copies()
    # N14 · 实时能力口径（预登记阈值再判定）：
    # - wall_clock_fps：实测整帧吞吐（decode+upload 节奏，非仅完成率）；
    # - deadline：24fps 预算 41.667ms，每帧 decode+upload 总耗时超即 miss；
    # - miss_ratio > 5% → realtime_capacity.pass=False 且 ok=False（旧
    #   口径"≥90% 帧完成+无异常"把容量不足放绿——审计 N14 修正：不能
    #   拿 decode p50 解释"总帧均在预算"）；
    # - duplicate_pts：解码源重复 PTS（过期/停滞帧）计数。
    total_ms_list = [d + u for d, u in zip(decode_times, upload_times)]
    deadline_ms = 1000.0 / 24.0
    miss_count = sum(1 for t in total_ms_list if t > deadline_ms)
    miss_ratio = (miss_count / len(total_ms_list)) if total_ms_list else 0.0
    wall_clock_fps = (len(decode_times) / wall_s) if wall_s > 0 else 0.0
    seen = set()
    duplicate_pts = sum(
        1 for p in pts_seen
        if p is not None and (p in seen or seen.add(p)))
    realtime_capacity = {
        "deadline_ms": round(deadline_ms, 3),
        "deadline_fps_basis": 24.0,
        "miss_count": miss_count,
        "miss_ratio": round(miss_ratio, 4),
        "threshold_miss_ratio": 0.05,
        "wall_clock_fps": round(wall_clock_fps, 1),
        "duplicate_pts_count": duplicate_pts,
        "pass": miss_ratio <= 0.05,
    }
    if total_ms_list and not realtime_capacity["pass"]:
        ok = False
    result = {
        "material": os.path.basename(material),
        "mode": "offscreen",
        "frames_requested": args.frames,
        "frames_measured": len(decode_times),
        "warmup": args.warmup,
        "wall_s": round(wall_s, 3),
        "realtime_capacity": realtime_capacity,
        "route": route,
        "r16ui": scene.r16ui_state(),
        "telemetry_summary": telemetry.summary(),
        "trace_summary": collector.get_summary(),
        "pixel_copies": pixel_copies,
        "gc": gc_summary,
        "errors": errors[:20],
        "ok": ok,
    }
    if zc_source is not None:
        result["zero_copy_import_ms_p50"] = round(
            sorted(import_times)[len(import_times) // 2], 3
        ) if import_times else None
        result["zero_copy_import_ms_p95"] = round(
            sorted(import_times)[max(0, int(len(import_times) * 0.95) - 1)], 3
        ) if import_times else None
    # Phase 5 运行时门禁：无快照/无监看的离屏场景不应有任何 CPU 回读。
    if not getattr(args, "snapshot_every", 0):
        dl_bytes = int(pixel_copies.get("cpu_download", {}).get("bytes", 0))
        result["readback_guard"] = {
            "cpu_download_bytes": dl_bytes,
            "pass": dl_bytes == 0,
        }
        if dl_bytes:
            ok = False
            result["ok"] = False
    return result


def run_window(material: str, args) -> dict:
    """真实窗口模式：同一原语 + 真实 swap 的 present 段观测。

    present 语义：composite 完成 = submitted；frameSwapped = swap 级
    可观测呈现（paint/swap 级观测点；无平台 vsync/扫描反馈，不得解读
    为物理屏幕扫描时间）。
    """
    from PyQt6.QtCore import QTimer
    from PyQt6.QtGui import QSurfaceFormat
    from PyQt6.QtOpenGLWidgets import QOpenGLWidget
    from PyQt6.QtWidgets import QApplication
    import OpenGL.GL as _gl

    app = QApplication.instance() or QApplication(sys.argv[:1])
    fmt = QSurfaceFormat()
    fmt.setVersion(4, 1)
    fmt.setProfile(QSurfaceFormat.OpenGLContextProfile.CoreProfile)
    QSurfaceFormat.setDefaultFormat(fmt)

    state: dict = {"error": None}
    collector = FrameTraceCollector(max_frames=max(600, args.frames * 2))
    collector.start_session(page="bench_frame_budget/window")
    telemetry = TelemetryAggregator(window=max(8, args.frames))
    reset_pixel_copies()

    class _Win(QOpenGLWidget):
        def __init__(self) -> None:
            super().__init__()
            self.setWindowTitle("bench_frame_budget (window mode) — 按任意键结束")
            self.resize(1280, 720)
            self.scene = None
            self.source = None
            self.route = None
            self.index = 0
            self.submitted_perf: dict = {}
            self.frame_tag_to_trace: dict = {}
            self.swaps = 0
            self.swap_wall: list = []
            self.present_ms: list = []
            self.errors: list = []
            self.decode_times: list = []
            self.upload_times: list = []
            self.started = 0.0
            self.latest_fbo = None
            self._timer = None
            self._finished = False

        def initializeGL(self) -> None:
            try:
                ctx = moderngl.create_context()
                self.scene = _BenchScene(ctx)
                self.source, self.route = open_decode_source(material)
                self.route["gl"] = {k: ctx.info.get(k, "")
                                    for k in ("GL_RENDERER", "GL_VERSION")}
                self.started = time.perf_counter()
                self._timer = QTimer(self)
                self._timer.timeout.connect(self._tick)
                interval = max(1, int(1000 / max(1.0, args.target_fps)))
                self._timer.start(interval)
            except Exception as e:  # noqa: BLE001
                state["error"] = f"initializeGL: {type(e).__name__}: {e}"
                self.close()

        def _tick(self) -> None:
            if self._finished:
                return
            if self.index >= args.warmup + args.frames:
                self._finish()
                return
            i = self.index
            self.index += 1
            try:
                self.makeCurrent()  # QOpenGLWidget：_tick 在 paint 外做 GL 工作
                t0 = time.perf_counter()
                frame = read_next_frame(self.source, self.route)
                decode_ms = (time.perf_counter() - t0) * 1000.0
                if frame is None:
                    self.errors.append({"frame": i, "error": "decode_none"})
                    return
                trace_id = next_frame_trace_id()
                warmup = i < args.warmup
                collector.record(EditPlaybackFrameTrace(
                    frame_index=i, trace_id=trace_id,
                    page="bench_frame_budget",
                    decode_path=str(getattr(frame, "decode_path", "")),
                    render_path="gpu_planar_upload",
                ))
                self.frame_tag_to_trace[i] = trace_id
                t1 = time.perf_counter()
                textures, bit_depth = self.scene.upload(frame, i)
                upload_ms = (time.perf_counter() - t1) * 1000.0
                nbytes = int(sum(int(p.nbytes) for p in frame.planes))
                add_pixel_copy(CopyKind.CPU_UPLOAD, nbytes)
                add_pixel_copy(CopyKind.TEXTURE_WRITE, nbytes)
                w, h = int(frame.width), int(frame.height)
                self.scene.timer.begin_frame(tag=i)
                with self.scene.timer.measure("composite"):
                    self.scene.ensure_fbo(w, h)
                    self.scene.fbo.use()
                    textures[0].use(0)
                    self.scene.program["src_tex"].value = 0
                    self.scene.vao.render(mode=moderngl.TRIANGLES, vertices=3)
                for entry in self.scene.timer.collect():
                    origin = int(entry.get("tag", -1))
                    ms = entry.get("ms")
                    origin_trace = self.frame_tag_to_trace.get(origin)
                    if origin_trace and ms is not None:
                        collector.annotate(
                            origin_trace, gpu_submit_ms=round(float(ms), 3),
                            gpu_time_unavailable=False)
                # submitted：composite 完成（present 协议起点）
                self.submitted_perf[i] = time.perf_counter()
                collector.annotate(
                    trace_id, present_status="submitted",
                    present_submitted_mono=time.monotonic(),
                    present_slot_id=-1)
                if not warmup:
                    self.decode_times.append(decode_ms)
                    self.upload_times.append(upload_ms)
                    telemetry.record(PlaybackFrameTelemetry(
                        timeline_frame=i, trace_id=trace_id,
                        mode="realtime", priority="playhead"))
                    telemetry.enrich_last(
                        i, decode_ms=decode_ms,
                        total_ms=decode_ms + upload_ms,
                        trace_id=trace_id, upload_ms=upload_ms)
                    collector.annotate(
                        trace_id, cpu_upload_bytes=nbytes,
                        texture_write_bytes=nbytes,
                        import_upload_ms=upload_ms,
                        import_upload_status="measured")
                self.latest_fbo = (self.scene.fbo.glo, w, h)
                self.doneCurrent()
                self.update()  # paintGL 内 blit，Qt 自动 swap
            except Exception as e:  # noqa: BLE001
                self.errors.append(
                    {"frame": i, "error": f"{type(e).__name__}: {e}"})
                try:
                    self.doneCurrent()
                except Exception:
                    pass

        def paintGL(self) -> None:
            if self.latest_fbo is None:
                return
            fbo_glo, w, h = self.latest_fbo
            _gl.glBindFramebuffer(_gl.GL_READ_FRAMEBUFFER, int(fbo_glo))
            _gl.glBindFramebuffer(
                _gl.GL_DRAW_FRAMEBUFFER, self.defaultFramebufferObject())
            _gl.glBlitFramebuffer(
                0, 0, w, h, 0, 0, self.width(), self.height(),
                _gl.GL_COLOR_BUFFER_BIT, _gl.GL_LINEAR)

        def on_frame_swapped(self) -> None:
            self.swaps += 1
            now = time.perf_counter()
            self.swap_wall.append(now)
            if self.submitted_perf:
                latest = max(self.submitted_perf)
                dt_ms = (now - self.submitted_perf.pop(latest)) * 1000.0
                self.present_ms.append(dt_ms)
                trace = self.frame_tag_to_trace.get(latest)
                if trace is not None:
                    collector.annotate(trace, present_status="displayed")
                    telemetry.enrich_last(
                        latest, trace_id=trace, present_ms=dt_ms)

        def _finish(self) -> None:
            if self._finished:
                return
            self._finished = True
            if self._timer:
                self._timer.stop()
            wall = time.perf_counter() - self.started
            presented_fps = len(self.swap_wall) / wall if wall > 0 else 0.0
            state["result"] = {
                "material": os.path.basename(material),
                "mode": "window",
                "frames_requested": args.frames,
                "frames_measured": len(self.decode_times),
                "warmup": args.warmup,
                "target_fps": args.target_fps,
                "wall_s": round(wall, 3),
                "presented_swaps": self.swaps,
                "presented_fps": round(presented_fps, 2),
                "present_ms_p50": round(
                    sorted(self.present_ms)[len(self.present_ms) // 2], 3)
                if self.present_ms else None,
                "route": self.route,
                "r16ui": self.scene.r16ui_state()
                if self.scene else {},
                "telemetry_summary": telemetry.summary(),
                "trace_summary": collector.get_summary(),
                "pixel_copies": snapshot_pixel_copies(),
                "errors": self.errors[:20],
                "ok": (state["error"] is None and not self.errors
                       and self.swaps >= args.frames * 0.5),
            }
            self.close()

        def keyPressEvent(self, event) -> None:
            self._finish()

    win = _Win()
    win.frameSwapped.connect(win.on_frame_swapped)
    win.show()
    app.exec()

    if state.get("error"):
        return {"material": os.path.basename(material), "mode": "window",
                "ok": False, "errors": [{"error": state["error"]}]}
    return state.get("result") or {
        "material": os.path.basename(material), "mode": "window",
        "ok": False, "errors": [{"error": "no result (window closed early)"}]}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="帧预算基准 harness（A0.1/A0.2；schema v3）")
    parser.add_argument("--mode", choices=("offscreen", "window"),
                        default="offscreen")
    parser.add_argument("--material", action="append", default=[],
                        help="素材路径（可重复）")
    parser.add_argument("--media-dir", default="",
                        help="目录：取其中所有视频文件作为素材")
    parser.add_argument("--generate-media", default="",
                        help="目录：素材为空时生成最小素材矩阵到该目录")
    parser.add_argument("--frames", type=int, default=240,
                        help="计时帧数（预热之外）")
    parser.add_argument("--warmup", type=int, default=30,
                        help="预热帧数（不计入统计）")
    parser.add_argument("--target-fps", type=float, default=25.0,
                        help="window 模式 tick 节拍")
    parser.add_argument("--snapshot-every", type=int, default=0,
                        help="每 N 帧做一次受控快照回读（0=关闭）")
    parser.add_argument("--zero-copy-probe", action=argparse.BooleanOptionalAction,
                        default=True,
                        help="macOS 上尝试零拷贝路由（VT 零拷贝源 + IOSurface→GL "
                             "桥；仅 offscreen 模式）")
    parser.add_argument("--out", default=os.path.join("bench_out", "frame_budget"),
                        help="输出根目录")
    parser.add_argument("--allow-dirty", action="store_true")
    args = parser.parse_args(argv)

    materials = list(args.material)
    if args.media_dir and os.path.isdir(args.media_dir):
        exts = (".mp4", ".mov", ".mkv", ".mxf")
        materials += sorted(
            os.path.join(args.media_dir, f)
            for f in os.listdir(args.media_dir)
            if f.lower().endswith(exts))
    if not materials and args.generate_media:
        materials = generate_media(args.generate_media)
    if not materials:
        print("错误：没有可用素材（--material/--media-dir/--generate-media）",
              file=sys.stderr)
        return 1

    env = environment_record(args.allow_dirty)
    head = env["git"]["head"][:12] or "dirty"
    stamp = _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_dir = os.path.join(args.out, head, f"{args.mode}_{stamp}")
    os.makedirs(out_dir, exist_ok=True)

    results, failures = [], []
    for material in materials:
        print(f"[bench] {args.mode}: {os.path.basename(material)} "
              f"(frames={args.frames}, warmup={args.warmup})")
        try:
            result = (run_window(material, args)
                      if args.mode == "window" else run_offscreen(material, args))
        except Exception as e:  # noqa: BLE001
            result = {"material": os.path.basename(material),
                      "mode": args.mode, "ok": False,
                      "errors": [{"error": f"{type(e).__name__}: {e}"}]}
        results.append(result)
        if not result.get("ok"):
            failures.append(result["material"])
        name = os.path.splitext(os.path.basename(material))[0]
        path = os.path.join(out_dir, f"{name}__{args.mode}.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"env": env, "args": vars(args), "result": result},
                      fh, indent=2, ensure_ascii=False, default=str)
        print(f"       -> {path} ok={result.get('ok')}")

    combined = {
        "schema": "bench_frame_budget.v1",
        "env": env,
        "mode": args.mode,
        "materials": [r["material"] for r in results],
        "failures": failures,
        "results": results,
    }
    with open(os.path.join(out_dir, "summary.json"), "w", encoding="utf-8") as fh:
        json.dump(combined, fh, indent=2, ensure_ascii=False, default=str)

    ok_count = len(results) - len(failures)
    print(f"[bench] 完成：{ok_count}/{len(results)} 素材成功 -> {out_dir}")
    if failures and ok_count > 0:
        return 2
    if failures:
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
