"""M16 · 真实窗口三相位基准：冷首帧 / seek 矩阵 / 连续播放（+冷启动基线）。

M16 延期门的 Mac 侧复开动作（deferred_gates：安静机+真实窗口 session）。
与 bench_frame_budget.py 的关系：复用其解码原语/场景与环境指纹（import
复用，不复制），窗口侧新增三相位与生产路由顺序：

    Topos 嗅探（is_topos_movie → ToposMediaSource + 原生 dylib 登记）
    → VT 零拷贝（MacOSVtZeroCopySource + IOSurfaceGLBridge，GL 线程）
    → VT planar → FFmpeg planar

口径声明（不冒充）：
- present 语义 = frameSwapped（swap 级可观测呈现；无物理扫描反馈）；
- 冷首帧 = 素材 open 前 → 首次 swap（含 open/decode/import/composite）；
- seek = seek 原语调用前 → 该帧 swap（seek 原语含定位+解码）；
- 播放 = QTimer 节拍驱动（非音钟），掉帧以 swaps<期望口径登记；
- 同素材 ≥5 组交错（素材顺序逐组轮转）给出跨组噪声带，不宣称单组结论。

用法（真实窗口——拒绝 QT_QPA_PLATFORM=offscreen）：
    .venv/bin/python scripts/bench_m16_real_window.py --runs 5 \
        --out docs/validation/upgrade_2026-10-24/M16/<head>/<ts>
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT))
sys.path.insert(0, str(_REPO_ROOT / "scripts"))

IS_MAC = sys.platform == "darwin"

# 默认真实素材矩阵（tests/test_videos；含 M16 验收"harness 补 Topos source"）
DEFAULT_MATERIALS = (
    "tests/test_videos/4K_H264.mov",
    "tests/test_videos/4K_H265.mov",
    "tests/test_videos/4K_prores422.mov",
    "tests/test_videos/2K_DNxHR 444 10bit.mov",
    "tests/test_videos/2K_topos_422.mov",
)

_SEEK_FRACTIONS = (0.50, 0.92, 0.08, 0.70, 0.30, 0.85, 0.12, 0.60, 0.40, 0.95)

_BENCH_VS = """
#version 330
out vec2 v_uv;
void main() {
    vec2 p = vec2((gl_VertexID << 1) & 2, gl_VertexID & 2);
    v_uv = p;
    gl_Position = vec4(p * 2.0 - 1.0, 0.0, 1.0);
}
"""

_RECT_FS = """
#version 330
in vec2 v_uv;
out vec4 out_color;
uniform sampler2DRect src_rect_tex;
void main() {
    out_color = texture(src_rect_tex, v_uv * vec2(textureSize(src_rect_tex)));
}
"""


# ---------------------------------------------------------------------------
# 路由解析辅助
# ---------------------------------------------------------------------------

def _topos_native_record() -> dict:
    """登记实际加载的 libtopos_codec 身份（M16 验收："登记实际 native 库"）。"""
    try:
        from src.shared.codec import topos_binding
        lib = topos_binding.load_library()
        path = str(getattr(lib, "_name", "") or "")
        digest = ""
        if path and os.path.isfile(path):
            h = hashlib.sha256()
            with open(path, "rb") as f:
                for chunk in iter(lambda: f.read(1 << 20), b""):
                    h.update(chunk)
            digest = h.hexdigest()[:16]
        return {"native_lib": path, "native_lib_sha256_16": digest}
    except Exception as e:  # noqa: BLE001
        return {"native_lib_error": f"{type(e).__name__}: {e}"}


def probe_material(path: str) -> dict:
    """av 探测帧数/帧率（seek 计划与口径登记；失败保守回退不阻塞）。"""
    import av
    info = {"frames": 240, "fps": 0.0, "probe": "fallback"}
    try:
        with av.open(path) as container:
            stream = container.streams.video[0]
            frames = int(getattr(stream, "frames", 0) or 0)
            if frames <= 0:
                dur = float(container.duration or 0) / 1_000_000.0
                rate = stream.average_rate
                if dur > 0 and rate:
                    frames = int(dur * float(rate))
            info["frames"] = max(2, frames)
            if stream.average_rate:
                info["fps"] = round(float(stream.average_rate), 3)
            info["probe"] = "av"
    except Exception as e:  # noqa: BLE001
        info["probe_error"] = f"{type(e).__name__}: {e}"
    return info


def _pct(vals, q):
    if not vals:
        return None
    s = sorted(vals)
    return round(s[min(len(s) - 1, int(len(s) * q))], 3)


# ---------------------------------------------------------------------------
# 三相位窗口
# ---------------------------------------------------------------------------

class _PhaseWindow:
    """单素材三相位窗口：cold → seek ×N → play（warmup+frames）。

    所有 GL 工作在 Qt 主线程 _tick 内 makeCurrent 后执行（与
    bench_frame_budget window 模式同一约定）。
    """

    def __init__(self, material: str, args, scene_cls) -> None:
        from PyQt6.QtCore import QTimer
        from PyQt6.QtGui import QSurfaceFormat
        from PyQt6.QtOpenGLWidgets import QOpenGLWidget
        import psutil

        fmt = QSurfaceFormat()
        fmt.setVersion(4, 1)
        fmt.setProfile(QSurfaceFormat.OpenGLContextProfile.CoreProfile)
        QSurfaceFormat.setDefaultFormat(fmt)

        self.material = material
        self.args = args
        self._scene_cls = scene_cls
        self.probe_info: dict = {}
        self.result: dict = {"material": os.path.basename(material),
                             "ok": False}

        self._phase = "cold"
        self._pending = None          # 待 swap 归账的帧号
        self._submitted_at: dict = {}
        self._step_t0 = 0.0
        self._keepalive: list = []    # planar 纹理（保活至下一帧）
        self._deferred_release = None  # 零拷贝租约延迟一帧归还
        self._play_i = 0
        self._seek_targets: list = []
        self._seek_i = 0
        self._done = False
        self._rss = [psutil.Process(os.getpid()).memory_info().rss]
        self._play_meta: list = []
        self._cold_record = None
        self._seek_records: list = []
        self._swap_count = 0

        win = self

        class _W(QOpenGLWidget):
            def __init__(self) -> None:
                super().__init__()
                self.setWindowTitle(
                    "bench_m16_real_window — 自动运行中")
                self.resize(1280, 720)
                self.scene = None
                self.ctx = None
                self.source = None
                self.bridge = None
                self.route: dict = {}
                self.kind = ""      # topos | zero_copy | planar
                self.errors: list = []

            def initializeGL(self) -> None:
                try:
                    import moderngl
                    self.ctx = moderngl.create_context()
                    self.scene = win._scene_cls(self.ctx)
                    self.scene.rect_program = self.ctx.program(
                        vertex_shader=_BENCH_VS, fragment_shader=_RECT_FS)
                    self.scene.vao_rect = self.ctx.vertex_array(
                        self.scene.rect_program, [])
                except Exception as e:  # noqa: BLE001
                    self.errors.append(
                        {"phase": "init", "error": f"{type(e).__name__}: {e}"})
                    win._finish("init_failed")

            def paintGL(self) -> None:
                if self.scene is None or self.scene.fbo is None:
                    return
                import OpenGL.GL as gl
                w, h = self.scene.fbo.size
                gl.glBindFramebuffer(
                    gl.GL_READ_FRAMEBUFFER, int(self.scene.fbo.glo))
                gl.glBindFramebuffer(
                    gl.GL_DRAW_FRAMEBUFFER, self.defaultFramebufferObject())
                gl.glBlitFramebuffer(
                    0, 0, w, h, 0, 0, self.width(), self.height(),
                    gl.GL_COLOR_BUFFER_BIT, gl.GL_LINEAR)

        self.win = _W()
        self.win.frameSwapped.connect(self._on_swapped)
        self._timer = QTimer(self.win)
        self._timer.setInterval(30)
        self._timer.timeout.connect(self._tick)

    # -- 路由与帧原语 ------------------------------------------------------
    def _open_source(self) -> None:
        """生产路由顺序打开素材（_tick 内 GL current 已保证）。"""
        import bench_frame_budget as bfb
        self.win.route = {"path": os.path.basename(self.material)}
        # 1) Topos 嗅探（生产：内容签名先于通用编解码探测）
        try:
            from src.shared.media.topos_source import is_topos_movie
            if is_topos_movie(self.material):
                from src.shared.media.topos_source import ToposMediaSource
                src = ToposMediaSource(self.material, decode_role="playback")
                src.open()
                self.win.source = src
                self.win.kind = "topos"
                self.win.route.update(
                    {"decode_route": "topos_native",
                     **_topos_native_record()})
                return
        except Exception as e:  # noqa: BLE001
            self.win.route["topos_sniff_error"] = f"{type(e).__name__}: {e}"
        # 2) VT 零拷贝（GL 线程桥初始化）
        if IS_MAC:
            zc_src, bridge, reason = bfb.try_open_zero_copy(self.material)
            if zc_src is not None:
                self.win.source = zc_src
                self.win.bridge = bridge
                self.win.kind = "zero_copy"
                self.win.route["decode_route"] = "macos_vt_zero_copy"
                return
            self.win.route["zero_copy_probe_fallback"] = reason
        # 3/4) VT planar → ffmpeg planar
        self.win.source, self.win.route = bfb.open_decode_source(
            self.material)
        self.win.kind = "planar"

    def _read_at(self, index: int):
        """读指定帧（seek 语义）；返回 (frame, decode_ms)。"""
        t0 = time.perf_counter()
        if self.win.kind == "zero_copy":
            frame = self.win.source.read_frame_pixel_buffer(index)
        else:
            self.win.source.seek_frame(index)
            frame = self.win.source.read_frame(index)
        return frame, (time.perf_counter() - t0) * 1000.0

    def _read_next(self):
        """顺序读帧（播放语义）；EOF 回绕登记。"""
        t0 = time.perf_counter()
        if self.win.kind == "zero_copy":
            frame = self.win.source.read_frame_pixel_buffer(self._play_i)
            if frame is None:
                self.win.route["eof_wraps"] = \
                    self.win.route.get("eof_wraps", 0) + 1
                frame = self.win.source.read_frame_pixel_buffer(0)
        else:
            try:
                frame = self.win.source.read_next_frame()
            except Exception:  # noqa: BLE001
                frame = None
            if frame is None:
                self.win.route["eof_wraps"] = \
                    self.win.route.get("eof_wraps", 0) + 1
                self.win.source.seek_frame(0)
                frame = self.win.source.read_frame(0)
        return frame, (time.perf_counter() - t0) * 1000.0

    def _composite(self, frame, index: int) -> tuple:
        """import/upload + composite → FBO；返回 (import_ms, w, h)。

        零拷贝：bridge bind（RECT/2D raw GL 名绑定采样）；planar：
        YUVGPUUploader 上传后首纹理采样（与 bench window 模式同口径，
        计时覆盖全部平面上传，画面为单采样呈现）。
        """
        import OpenGL.GL as gl
        import moderngl
        t0 = time.perf_counter()
        scene = self.win.scene
        if self.win.kind == "zero_copy":
            bound = self.win.bridge.bind_pixel_buffer(
                pixel_buffer=frame.pixel_buffer,
                frame_index=index,
                pixel_format=str(getattr(frame, "pixel_format", "nv12")),
                bit_depth=int(getattr(frame, "bit_depth", 8)),
            )
            if bound is None:
                raise RuntimeError(
                    "zero_copy_bind_failed: "
                    + str(getattr(self.win.bridge,
                                  "last_fallback_reason", "")))
            planes, release = bound
            w, h = int(planes[0].width), int(planes[0].height)
            scene.ensure_fbo(w, h)
            scene.fbo.use()
            is_rect = str(getattr(planes[0], "target", "2d")) == "rectangle"
            gl.glActiveTexture(gl.GL_TEXTURE0)
            gl.glBindTexture(
                gl.GL_TEXTURE_RECTANGLE if is_rect else gl.GL_TEXTURE_2D,
                int(planes[0].texture_id))
            if is_rect:
                scene.rect_program["src_rect_tex"].value = 0
                scene.vao_rect.render(mode=moderngl.TRIANGLES, vertices=3)
            else:
                scene.program["src_tex"].value = 0
                scene.vao.render(mode=moderngl.TRIANGLES, vertices=3)
            # 租约延迟一帧归还：本帧 draw 已入队，上一帧租约此刻才回环，
            # 避免环形缓冲复用仍在被采样的纹理名。
            prev = self._deferred_release
            self._deferred_release = release
            if prev is not None:
                try:
                    prev.release()
                except Exception:  # noqa: BLE001
                    pass
        else:
            textures, _bit_depth = scene.upload(frame, index)
            w, h = int(frame.width), int(frame.height)
            scene.ensure_fbo(w, h)
            scene.fbo.use()
            textures[0].use(0)
            scene.program["src_tex"].value = 0
            scene.vao.render(mode=moderngl.TRIANGLES, vertices=3)
            self._keepalive = [textures]
        import_ms = (time.perf_counter() - t0) * 1000.0
        return import_ms, w, h

    # -- 相位驱动 ----------------------------------------------------------
    def start(self) -> None:
        self._step_t0 = time.perf_counter()
        self._timer.start()

    def _tick(self) -> None:
        if self._done or self._pending is not None:
            return  # 上一帧未 swap：等待（三相位均不追加提交）
        try:
            self.win.makeCurrent()
            if self._phase == "cold":
                self._phase_cold()
            elif self._phase == "seek":
                self._phase_seek()
            elif self._phase == "play":
                self._phase_play()
        except Exception as e:  # noqa: BLE001
            self.win.errors.append(
                {"phase": self._phase,
                 "error": f"{type(e).__name__}: {e}"})
            self._advance_phase_on_error()
        finally:
            try:
                self.win.doneCurrent()
            except Exception:  # noqa: BLE001
                pass

    def _submit(self, i: int, decode_ms: float, import_ms: float) -> None:
        self._submitted_at[i] = time.perf_counter()
        self._last_meta = {"decode_ms": round(decode_ms, 3),
                           "import_ms": round(import_ms, 3)}
        self._pending = i
        self.win.update()

    def _phase_cold(self) -> None:
        self._open_source()
        self.win.route["gl"] = {
            k: self.win.ctx.info.get(k, "")
            for k in ("GL_RENDERER", "GL_VERSION")}
        frame, decode_ms = self._read_at(0)
        if frame is None:
            raise RuntimeError("cold decode_none")
        import_ms, _w, _h = self._composite(frame, 0)
        self._submit(0, decode_ms, import_ms)

    def _phase_seek(self) -> None:
        target = self._seek_targets[self._seek_i]
        frame, decode_ms = self._read_at(target)
        if frame is None:
            raise RuntimeError(f"seek decode_none @{target}")
        import_ms, _w, _h = self._composite(frame, target)
        self._submit(target, decode_ms, import_ms)

    def _phase_play(self) -> None:
        if self._play_i >= self.args.warmup + self.args.frames:
            self._finish("complete")
            return
        i = self._play_i
        frame, decode_ms = self._read_next()
        self._play_i += 1
        if frame is None:
            raise RuntimeError(f"play decode_none @{i}")
        import_ms, _w, _h = self._composite(frame, i)
        if i >= self.args.warmup and (i - self.args.warmup) % 24 == 0:
            import psutil
            self._rss.append(psutil.Process(os.getpid()).memory_info().rss)
        self._submit(i, decode_ms, import_ms)

    def _advance_phase_on_error(self) -> None:
        if self._phase == "cold":
            self._finish("cold_failed")
        elif self._phase == "seek":
            self._seek_i += 1
            if self._seek_i >= len(self._seek_targets):
                self._enter_play()
        else:
            self._finish("play_failed")

    def _on_swapped(self) -> None:
        now = time.perf_counter()
        self._swap_count += 1
        i = self._pending
        if i is None:
            return
        self._pending = None
        submitted = self._submitted_at.pop(i, None)
        if submitted is None:
            return
        present_ms = (now - submitted) * 1000.0
        step = {"frame": i, "present_ms": round(present_ms, 3),
                "step_total_ms": round((now - self._step_t0) * 1000.0, 3),
                **self._last_meta}
        self._step_t0 = now
        if self._phase == "cold":
            self._cold_record = step
            self._enter_seek()
        elif self._phase == "seek":
            self._seek_records.append(step)
            self._seek_i += 1
            if self._seek_i >= len(self._seek_targets):
                self._enter_play()
        else:  # play
            if i >= self.args.warmup:
                self._play_meta.append(step)

    def _enter_seek(self) -> None:
        self._phase = "seek"
        self._seek_i = 0
        total = max(2, int(self.probe_info.get("frames", 240)))
        self._seek_targets = [
            min(total - 2, max(1, int(round(f * total))))
            for f in _SEEK_FRACTIONS]
        self._timer.setInterval(30)

    def _enter_play(self) -> None:
        self._phase = "play"
        self._play_i = 0
        self._timer.setInterval(
            max(1, int(1000 / max(1.0, self.args.target_fps))))

    def _finish(self, why: str) -> None:
        if self._done:
            return
        self._done = True
        self._timer.stop()
        if self._deferred_release is not None:
            try:
                self._deferred_release.release()
            except Exception:  # noqa: BLE001
                pass
        if self.win.source is not None:
            try:
                self.win.source.close()
            except Exception:  # noqa: BLE001
                pass

        dec = [m["decode_ms"] for m in self._play_meta]
        imp = [m["import_ms"] for m in self._play_meta]
        pres = [m["present_ms"] for m in self._play_meta]
        totals = [m["step_total_ms"] for m in self._play_meta]
        seek_vals = [s["step_total_ms"] for s in self._seek_records]
        result = {
            "material": os.path.basename(self.material),
            "kind": self.win.kind,
            "route": self.win.route,
            "probe": self.probe_info,
            "finish_reason": why,
            "cold": self._cold_record,
            "seeks": self._seek_records,
            "seek_summary": {
                "n": len(seek_vals),
                "p50": _pct(seek_vals, 0.5),
                "p95": _pct(seek_vals, 0.95),
                "max": round(max(seek_vals), 3) if seek_vals else None,
                "min": round(min(seek_vals), 3) if seek_vals else None,
            } if seek_vals else None,
            "play": {
                "frames_measured": len(self._play_meta),
                "decode_ms_p50": _pct(dec, 0.5),
                "decode_ms_p95": _pct(dec, 0.95),
                "import_ms_p50": _pct(imp, 0.5),
                "present_ms_p50": _pct(pres, 0.5),
                "present_ms_p95": _pct(pres, 0.95),
                "frame_total_p50": _pct(totals, 0.5),
                "frame_total_p95": _pct(totals, 0.95),
                "swaps_total": self._swap_count,
                "target_fps": self.args.target_fps,
                "drop_ratio": round(
                    max(0.0, 1.0 - (len(self._play_meta)
                                    / max(1, self.args.frames))), 4),
            } if self._play_meta else None,
            "rss_bytes": self._rss,
            "errors": self.win.errors[:10],
        }
        result["ok"] = (
            why == "complete" and not self.win.errors
            and result["cold"] is not None and result["seek_summary"]
            and result["play"] and result["play"]["frames_measured"] >=
            self.args.frames * 0.5)
        self.result = result
        self.win.close()


def run_material_window(app, material: str, args, scene_cls) -> dict:
    """驱动单素材三相位窗口至关闭，返回结果 dict。"""
    win = _PhaseWindow(material, args, scene_cls)
    win.probe_info = probe_material(material)
    win.win.show()
    app.processEvents()
    win.start()
    app.exec()
    return win.result


def run_single_child(material: str, args) -> int:
    """子进程模式：单素材三相位窗口 → 结果 JSON 落 --out-file 后退出。

    每素材独立进程是有效性要求而非工程偏好：同进程内多个顺序窗口的
    IOSurface 桥/GL 对象名会跨上下文污染（实测第 3 个零拷贝窗口触发
    环槽检疫后 FBO 绑定 invalid operation 崩溃），独立进程同时满足
    M16 验收"独立进程复现与组合运行分别统计"的口径。
    """
    out_file = Path(args.out_file)
    out_file.parent.mkdir(parents=True, exist_ok=True)
    from PyQt6.QtCore import Qt
    from PyQt6.QtWidgets import QApplication
    # 生产对齐：src/main.py 在 QApplication 前开启全局上下文共享
    QApplication.setAttribute(
        Qt.ApplicationAttribute.AA_ShareOpenGLContexts, True)
    app = QApplication(sys.argv[:1])
    import bench_frame_budget as bfb
    result = run_material_window(app, material, args, bfb._BenchScene)
    with open(out_file, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    return 0 if result.get("ok") else 1


# ---------------------------------------------------------------------------
# 冷启动基线（--test-startup 口径：进程→MainWindow 构造+注册表就绪）
# ---------------------------------------------------------------------------

def measure_cold_start(samples: int) -> dict:
    cmd = [sys.executable, "apps/topos_edit/main.py",
           "--test-startup", "--no-startup-dialog"]
    times, rcs = [], []
    for _ in range(samples):
        t0 = time.perf_counter()
        proc = subprocess.run(cmd, cwd=str(_REPO_ROOT), capture_output=True)
        times.append(round((time.perf_counter() - t0) * 1000.0, 1))
        rcs.append(proc.returncode)
    ok_times = [t for t, rc in zip(times, rcs) if rc == 0]
    return {
        "command": " ".join(cmd[1:]),
        "caliber": "进程启动→MainWindow 构造+注册表就绪"
                   "（--test-startup，未进 exec/首绘）",
        "samples_ms": times,
        "returncodes": rcs,
        "p50_ms": sorted(ok_times)[len(ok_times) // 2] if ok_times else None,
        "min_ms": min(ok_times) if ok_times else None,
        "max_ms": max(ok_times) if ok_times else None,
        "ok": bool(ok_times) and len(ok_times) == samples,
    }


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--materials", nargs="*", default=None,
                        help="素材路径列表（默认 tests/test_videos 五件矩阵）")
    parser.add_argument("--runs", type=int, default=5,
                        help="同素材交错重复组数（素材顺序逐组轮转）")
    parser.add_argument("--frames", type=int, default=72, help="播放计时帧数")
    parser.add_argument("--warmup", type=int, default=8, help="播放预热帧数")
    parser.add_argument("--target-fps", type=float, default=24.0)
    parser.add_argument("--cold-start-samples", type=int, default=5)
    parser.add_argument("--out", required=True, help="输出目录")
    parser.add_argument("--allow-dirty", action="store_true")
    parser.add_argument("--single", default=None,
                        help="[内部] 子进程模式：单素材")
    parser.add_argument("--out-file", default=None,
                        help="[内部] 子进程模式：结果 JSON 路径")
    args = parser.parse_args(argv)

    if os.environ.get("QT_QPA_PLATFORM") == "offscreen":
        print("[m16-window] 拒绝运行：QT_QPA_PLATFORM=offscreen 不是真实窗口",
              file=sys.stderr)
        return 2

    if args.single:
        return run_single_child(args.single, args)

    import bench_frame_budget as bfb

    materials = args.materials or list(DEFAULT_MATERIALS)
    missing = [m for m in materials if not os.path.isfile(m)]
    if missing:
        print(f"[m16-window] 素材缺失: {missing}", file=sys.stderr)
        return 2

    env = bfb.environment_record(args.allow_dirty)
    env["session_note"] = (
        "真实窗口 session；每素材独立子进程（M16 验收'独立进程复现'口径，"
        "同时隔离顺序窗口的桥/GL 名字空间跨上下文污染）；基准期间 Agent 侧"
        "无并行测试负载（非独占安静机如实登记），素材顺序逐组轮转去相关")

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    child_dir = out / "single"
    child_dir.mkdir(exist_ok=True)

    runs = []
    child_rcs = []
    for run in range(args.runs):
        order = (materials[run % len(materials):]
                 + materials[:run % len(materials)])
        for material in order:
            tag = f"run{run + 1}_{os.path.basename(material)}"
            print(f"[m16-window] run {run + 1}/{args.runs}: "
                  f"{os.path.basename(material)}", flush=True)
            child_json = child_dir / f"{tag}.json"
            proc = subprocess.run(
                [sys.executable, os.path.abspath(__file__),
                 "--single", material, "--out-file", str(child_json),
                 "--out", str(out),
                 "--frames", str(args.frames), "--warmup", str(args.warmup),
                 "--target-fps", str(args.target_fps)],
                cwd=str(_REPO_ROOT), capture_output=True, text=True)
            child_rcs.append(proc.returncode)
            if child_json.is_file():
                with open(child_json, encoding="utf-8") as f:
                    result = json.load(f)
            else:
                result = {"material": os.path.basename(material),
                          "ok": False,
                          "errors": [{"error": f"child_rc={proc.returncode}",
                                      "stderr_tail": proc.stderr[-500:]}]}
            result["run"] = run + 1
            runs.append(result)
            cold = (result.get("cold") or {}).get("step_total_ms")
            seek = (result.get("seek_summary") or {}).get("p50")
            play = (result.get("play") or {}).get("frame_total_p95")
            print(f"    [{'ok' if result.get('ok') else 'FAIL'}] "
                  f"cold={cold}ms seek_p50={seek}ms play_p95={play}ms",
                  flush=True)

    cold_start = measure_cold_start(args.cold_start_samples)

    aggregate = {}
    for r in runs:
        a = aggregate.setdefault(r["material"], {
            "kinds": set(), "oks": 0, "runs": 0, "cold": [], "seek_p50": [],
            "seek_max": [], "play_dec_p50": [], "play_total_p95": [],
            "drop": []})
        a["runs"] += 1
        a["kinds"].add(r.get("kind", "?"))
        a["oks"] += 1 if r.get("ok") else 0
        if r.get("cold"):
            a["cold"].append(r["cold"]["step_total_ms"])
        if r.get("seek_summary"):
            a["seek_p50"].append(r["seek_summary"]["p50"])
            a["seek_max"].append(r["seek_summary"]["max"])
        if r.get("play"):
            a["play_dec_p50"].append(r["play"].get("decode_ms_p50"))
            a["play_total_p95"].append(r["play"].get("frame_total_p95"))
            a["drop"].append(r["play"].get("drop_ratio"))
    for name, a in aggregate.items():
        a["kinds"] = sorted(a["kinds"])
        for key in ("cold", "seek_p50", "seek_max", "play_dec_p50",
                    "play_total_p95", "drop"):
            vals = [v for v in a[key] if v is not None]
            a[key] = {
                "values": vals,
                "p50": sorted(vals)[len(vals) // 2] if vals else None,
                "min": min(vals) if vals else None,
                "max": max(vals) if vals else None,
                "band": round(max(vals) - min(vals), 3)
                if vals and len(vals) > 1 else 0.0,
            }

    summary = {
        "schema": "bench_m16_real_window/2",
        "environment": env,
        "child_returncodes": child_rcs,
        "phases": {
            "cold": "素材 open 前→首次 swap（open/decode/import/composite）",
            "seek": f"seek 原语前→swap，分位 {list(_SEEK_FRACTIONS)}",
            "play": f"QTimer {args.target_fps}fps 节拍，"
                    f"warmup {args.warmup} + {args.frames} 帧",
            "present_caliber": "frameSwapped（swap 级可观测，无物理扫描反馈）",
            "isolation": "每素材独立子进程",
        },
        "cold_start": cold_start,
        "aggregate": aggregate,
        "runs": runs,
    }
    out_file = out / "bench_m16_real_window.json"
    with open(out_file, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    print(f"[m16-window] 结果: {out_file}")
    all_ok = all(r.get("ok") for r in runs) and cold_start["ok"]
    print(f"[m16-window] overall: {'PASS' if all_ok else 'FAIL'}")
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
