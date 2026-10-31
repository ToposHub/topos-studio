#!/usr/bin/env python
"""监看 CPU 路径 A/B 基准（计划 A A1.2 / H2 进入条件实测）。

用**生产同款代码**测监看开启时每帧新增的 CPU 成本：
- A（monitor off）：仅 viewer blit（GPU 显存内，基线）。
- B（monitor on）：blit + ProgramViewer 的 PBO 回读链
  （glReadPixels→PBO 异步 / glGetBufferSubData 取上一帧 /
  `_pbo_rgba_to_qimage` 翻转+丢 alpha+QImage.copy）
  + `submit_qimage` 的 image.copy()
  + monitor_output.`qimage_to_bgra_frame`（convertToFormat/frombuffer.copy/
  cv2.resize/canvas 填充/tobytes 四连拷）——与 A0.3 台账 §3.3 逐项对应。

退出码：0 成功；1 致命错误（无 GL/参数）。
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import sys
import time

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import numpy as np  # noqa: E402


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="监看回读链 A/B 基准（H2）")
    parser.add_argument("--width", type=int, default=3840)
    parser.add_argument("--height", type=int, default=2160)
    parser.add_argument("--frames", type=int, default=200)
    parser.add_argument("--warmup", type=int, default=30)
    parser.add_argument("--target-fps", type=float, default=25.0)
    parser.add_argument("--monitor-target", default="1920x1080",
                        help="qimage_to_bgra_frame 的目标尺寸 WxH")
    parser.add_argument("--out", default=os.path.join("bench_out", "monitor_readback"))
    args = parser.parse_args(argv)

    import moderngl
    from PyQt6.QtCore import QTimer
    from PyQt6.QtGui import QSurfaceFormat
    from PyQt6.QtOpenGLWidgets import QOpenGLWidget
    from PyQt6.QtWidgets import QApplication
    import OpenGL.GL as _gl
    from PyQt6.QtGui import QImage

    from src.features.edit.ui.program_viewer import ProgramViewer
    from src.shared.video.monitor_output import qimage_to_bgra_frame

    app = QApplication.instance() or QApplication(sys.argv[:1])
    fmt = QSurfaceFormat()
    fmt.setVersion(4, 1)
    fmt.setProfile(QSurfaceFormat.OpenGLContextProfile.CoreProfile)
    QSurfaceFormat.setDefaultFormat(fmt)

    tw, th = (int(x) for x in args.monitor_target.lower().split("x"))
    W, H = args.width, args.height
    state: dict = {"result": None, "error": None}

    class _Win(QOpenGLWidget):
        def __init__(self) -> None:
            super().__init__()
            self.setWindowTitle("bench_monitor_readback (A/B) — 按任意键结束")
            self.resize(960, 540)
            self.ctx = None
            self.src_tex = None
            self.draw_fbo = None
            self.pbos = []
            self.read_index = 0
            self.pending = False
            self.index = 0
            self.stage_times: dict = {}
            self.frame_totals: dict = {"off": [], "on": []}
            self._timer = None
            self._finished = False

        def initializeGL(self) -> None:
            try:
                self.ctx = moderngl.create_context()
                # 源纹理：4K 随机内容（内容不影响回读成本）
                data = np.random.default_rng(7).integers(
                    0, 256, (H, W, 4), dtype=np.uint8)
                self.src_tex = self.ctx.texture((W, H), components=4, dtype="f1")
                self.src_tex.write(data.tobytes())
                display = self.ctx.texture((W, H), components=4, dtype="f1")
                self.draw_fbo = self.ctx.framebuffer(color_attachments=[display])
                # PV 同款 PBO 对（GL_STREAM_READ，双缓冲环）
                pbos = _gl.glGenBuffers(2)
                if isinstance(pbos, int):
                    pbos = [pbos]
                self.pbos = list(pbos)
                for pbo in self.pbos:
                    _gl.glBindBuffer(_gl.GL_PIXEL_PACK_BUFFER, pbo)
                    _gl.glBufferData(_gl.GL_PIXEL_PACK_BUFFER, W * H * 4,
                                     None, _gl.GL_STREAM_READ)
                _gl.glBindBuffer(_gl.GL_PIXEL_PACK_BUFFER, 0)
                self._timer = QTimer(self)
                self._timer.timeout.connect(self._tick)
                self._timer.start(max(1, int(1000 / args.target_fps)))
            except Exception as e:  # noqa: BLE001
                state["error"] = f"initializeGL: {type(e).__name__}: {e}"
                self.close()

        def _stage(self, name: str, fn):
            t0 = time.perf_counter()
            out = fn()
            self.stage_times.setdefault(name, []).append(
                (time.perf_counter() - t0) * 1000.0)
            return out

        def _blit(self) -> None:
            # blit 源纹理 FBO → 显示 FBO（PV `_copy_shared_texture_to_display`
            # 的核心 GPU 工作：显存内 w*h*4 拷贝）
            _gl.glBindFramebuffer(_gl.GL_READ_FRAMEBUFFER,
                                  self._tex_fbo(self.src_tex))
            _gl.glBindFramebuffer(_gl.GL_DRAW_FRAMEBUFFER, self.draw_fbo.glo)
            _gl.glBlitFramebuffer(0, 0, W, H, 0, 0, W, H,
                                  _gl.GL_COLOR_BUFFER_BIT, _gl.GL_NEAREST)

        def _tex_fbo(self, tex):
            fbo = getattr(self, "_src_fbo", None)
            if fbo is None:
                fbo = self.ctx.framebuffer(color_attachments=[tex])
                self._src_fbo = fbo
            return fbo.glo

        def _monitor_chain(self, i: int) -> None:
            # --- PV `_submit_monitor_pbo_readback` 同款（1 帧延迟流水线）---
            read_index = self.read_index
            map_index = 1 - read_index
            byte_count = W * H * 4
            if self.pending:
                def fetch():
                    _gl.glBindBuffer(_gl.GL_PIXEL_PACK_BUFFER, self.pbos[map_index])
                    return _gl.glGetBufferSubData(
                        _gl.GL_PIXEL_PACK_BUFFER, 0, byte_count)
                raw = self._stage("pbo_fetch", fetch)
                if raw is not None:
                    def to_qimage():
                        return ProgramViewer._pbo_rgba_to_qimage(
                            raw, W, H, True)
                    image = self._stage("rgba_to_qimage", to_qimage)
                    if image is not None:
                        def submit_copy():
                            return image.copy()
                        owned = self._stage("submit_qimage_copy", submit_copy)
                        def to_bgra():
                            return qimage_to_bgra_frame(
                                owned, width=tw, height=th)
                        frame_bytes = self._stage("bgra_convert", to_bgra)
                        self.stage_times.setdefault(
                            "bgra_out_bytes", []).append(len(frame_bytes))
            # 异步发起本帧回读（DMA，不计 CPU）
            import ctypes
            _gl.glBindFramebuffer(_gl.GL_READ_FRAMEBUFFER, self.draw_fbo.glo)
            _gl.glBindBuffer(_gl.GL_PIXEL_PACK_BUFFER, self.pbos[read_index])
            _gl.glReadPixels(0, 0, W, H, _gl.GL_RGBA, _gl.GL_UNSIGNED_BYTE,
                             ctypes.c_void_p(0))
            _gl.glBindBuffer(_gl.GL_PIXEL_PACK_BUFFER, 0)
            self.read_index = map_index
            self.pending = True

        def _tick(self) -> None:
            if self._finished:
                return
            i = self.index
            self.index += 1
            if i >= args.warmup * 2 + args.frames * 2:
                self._finish()
                return
            try:
                self.makeCurrent()
                # A/B 交替成对（同帧条件）：off 先、on 后
                t0 = time.perf_counter()
                self._blit()
                off_ms = (time.perf_counter() - t0) * 1000.0
                t1 = time.perf_counter()
                self._blit()
                self._monitor_chain(i)
                on_ms = (time.perf_counter() - t1) * 1000.0
                if i >= args.warmup * 2:
                    self.frame_totals["off"].append(off_ms)
                    self.frame_totals["on"].append(on_ms)
                self.doneCurrent()
                self.update()
            except Exception as e:  # noqa: BLE001
                state["error"] = f"tick: {type(e).__name__}: {e}"
                self._finish()

        def paintGL(self) -> None:
            if self.draw_fbo is not None:
                _gl.glBindFramebuffer(_gl.GL_READ_FRAMEBUFFER, self.draw_fbo.glo)
                _gl.glBindFramebuffer(
                    _gl.GL_DRAW_FRAMEBUFFER, self.defaultFramebufferObject())
                _gl.glBlitFramebuffer(0, 0, W, H, 0, 0, self.width(),
                                      self.height(), _gl.GL_COLOR_BUFFER_BIT,
                                      _gl.GL_LINEAR)

        def _finish(self) -> None:
            if self._finished:
                return
            self._finished = True
            if self._timer:
                self._timer.stop()
            state["result"] = self._summarize()
            self.close()

        def _summarize(self) -> dict:
            def stats(values):
                values = sorted(values)
                if not values:
                    return {}
                n = len(values)
                return {
                    "frames": n,
                    "p50": round(values[n // 2], 3),
                    "p95": round(values[max(0, int(n * 0.95) - 1)], 3),
                    "max": round(values[-1], 3),
                }
            stages = {}
            for name, values in self.stage_times.items():
                if name == "bgra_out_bytes":
                    stages[name] = values[:1]
                    continue
                stages[name] = stats(values)
            off = stats(self.frame_totals["off"])
            on = stats(self.frame_totals["on"])
            return {
                "source": f"{W}x{H}", "monitor_target": f"{tw}x{th}",
                "blit_only": off,
                "blit_plus_monitor_chain": on,
                "monitor_delta_p50_ms": round(
                    (on.get("p50", 0.0) - off.get("p50", 0.0)), 3),
                "stages": stages,
                "readback_bytes_per_frame": W * H * 4,
                "budget_ms": round(1000.0 / args.target_fps, 1),
            }

        def keyPressEvent(self, event) -> None:
            self._finish()

    win = _Win()
    win.show()
    app.exec()

    if state.get("error"):
        print(f"错误：{state['error']}", file=sys.stderr)
        return 1
    result = state.get("result") or {}
    if not result:
        print("错误：无结果", file=sys.stderr)
        return 1

    head = os.popen("git rev-parse --short=12 HEAD").read().strip() or "dirty"
    out_dir = os.path.join(args.out, head)
    os.makedirs(out_dir, exist_ok=True)
    stamp = _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = os.path.join(out_dir, f"ab_{stamp}.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"args": vars(args), "result": result}, fh,
                  indent=2, ensure_ascii=False, default=str)
    print(json.dumps(result, indent=2, ensure_ascii=False))
    print(f"-> {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
