#!/usr/bin/env python3
"""P1-03: CAMetalLayer <-> QOpenGLWidget coexistence probe (plan K3 input).

Questions this answers on the current machine (plan §2.3 / §13 #2):
  1. Can a CAMetalLayer-backed NSView coexist with a QOpenGLWidget in the
     SAME window (AA_ShareOpenGLContexts group)?  Do both show content?
  2. Z-order: who wins where they overlap?
  3. Resize: does the Metal layer track the hosting view?
  4. Present cadence: is nextDrawable present vsync-locked (~display Hz)?

Method: minimal QMainWindow, left = solid-green QOpenGLWidget, right =
native NSView hosting a CAMetalLayer (solid-red frames). Verify through
QScreen window grabs (pixel sampling), not through logs.

Artifacts: tests/media/results/qt_metal_coexistence.json + .md

Run: .venv/bin/python tools/probe_qt_metal_coexistence.py
"""
from __future__ import annotations

import ctypes
import json
import platform
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from PyQt6 import QtCore, QtWidgets  # noqa: E402  (macOS-only probe)
from PyQt6.QtOpenGLWidgets import QOpenGLWidget  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
OUT_DIR = REPO / "tests" / "media" / "results"

WIN_W, WIN_H = 800, 400
GL_GREEN = (0, 1, 0)
METAL_RED = (1, 0, 0)


def _git_sha() -> str:
    try:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=REPO,
            capture_output=True, text=True, check=True,
        ).stdout.strip()
    except Exception:
        return "unknown"


def _classify(px: tuple) -> str:
    """Classify a sampled pixel by dominant channel."""
    r, g, b = px[0], px[1], px[2]
    if g > 150 and r < 100 and b < 100:
        return "green(GL)"
    if r > 150 and g < 100 and b < 100:
        return "red(Metal)"
    if r > 240 and g > 240 and b > 240:
        return "white"
    if r < 30 and g < 30 and b < 30:
        return "black"
    return f"other({r},{g},{b})"


class GLViewport(QOpenGLWidget):
    """Stand-in for the 5 existing QOpenGLWidget subclasses."""

    def initializeGL(self) -> None:
        QOpenGLWidget.initializeGL(self)

    def paintGL(self) -> None:
        from OpenGL import GL
        GL.glClearColor(*GL_GREEN, 1.0)
        GL.glClear(GL.GL_COLOR_BUFFER_BIT)


def probe() -> Dict[str, Any]:
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    window = QtWidgets.QMainWindow()
    window.setWindowTitle("P1-03 coexistence probe")
    window.resize(WIN_W, WIN_H)
    central = QtWidgets.QWidget()
    window.setCentralWidget(central)

    gl_widget = GLViewport(central)
    gl_widget.setGeometry(0, 0, WIN_W // 2, WIN_H)
    gl_widget.show()

    metal_host = QtWidgets.QWidget(central)
    metal_host.setAttribute(QtCore.Qt.WidgetAttribute.WA_NativeWindow, True)
    metal_host.setAttribute(QtCore.Qt.WidgetAttribute.WA_DontCreateNativeAncestors, True)
    metal_host.setGeometry(WIN_W // 2, 0, WIN_W // 2, WIN_H)
    metal_host.show()

    window.show()
    QtWidgets.QApplication.processEvents()
    time.sleep(0.5)

    report: Dict[str, Any] = {
        "schema": "topos.qt_metal_coexistence.v1",
        "timestamp": datetime.now().isoformat(),
        "git_sha": _git_sha(),
        "os": platform.platform(),
    }

    # --- host a CAMetalLayer inside the right widget's NSView -------------
    import objc
    import Quartz
    import Metal

    nsview_ptr = int(metal_host.winId())
    host_view = objc.objc_object(c_void_p=ctypes.c_void_p(nsview_ptr))
    dev = Metal.MTLCreateSystemDefaultDevice()
    layer = Quartz.CAMetalLayer.alloc().init()
    layer.setDevice_(dev)
    layer.setPixelFormat_(80)  # BGRA8Unorm
    layer.setFramebufferOnly_(True)
    bounds = host_view.bounds()
    layer.setFrame_((bounds.origin, bounds.size))
    host_view.setBounds_(bounds)
    metal_nsview = Quartz.NSView.alloc().initWithFrame_((bounds.origin, bounds.size))
    metal_nsview.setLayer_(layer)
    metal_nsview.setWantsLayer_(True)
    host_view.addSubview_(metal_nsview)
    QtWidgets.QApplication.processEvents()

    def present_metal() -> Optional[str]:
        drawable = layer.nextDrawable()
        if drawable is None:
            return "nextDrawable=nil"
        rpd = Metal.MTLRenderPassDescriptor.renderPassDescriptor()
        ca = rpd.colorAttachments().objectAtIndexedSubscript_(0)
        ca.setTexture_(drawable.texture())
        ca.setLoadAction_(Metal.MTLLoadActionClear)
        ca.setStoreAction_(Metal.MTLStoreActionStore)
        # MTLClearColor is a plain C struct -> PyObjC maps it to a 4-tuple
        ca.setClearColor_((1.0, 0.0, 0.0, 1.0))
        cb = dev.newCommandQueue().commandBuffer()
        enc = cb.renderCommandEncoderWithDescriptor_(rpd)
        enc.endEncoding()
        cb.presentDrawable_(drawable)
        cb.commit()
        cb.waitUntilCompleted()
        return None

    err = present_metal()
    QtWidgets.QApplication.processEvents()
    time.sleep(0.4)
    report["metal_present_error"] = err

    # --- 1. coexistence: sample both halves ------------------------------
    img = QtWidgets.QApplication.primaryScreen().grabWindow(int(window.winId()))
    img = img.toImage()
    iw, ih = img.width(), img.height()
    gl_px = img.pixelColor(int(iw * 0.25), int(ih * 0.5))
    mt_px = img.pixelColor(int(iw * 0.75), int(ih * 0.5))
    gl_cls = _classify((gl_px.red(), gl_px.green(), gl_px.blue()))
    mt_cls = _classify((mt_px.red(), mt_px.green(), mt_px.blue()))
    report["coexistence"] = {
        "gl_pixel": gl_cls,
        "metal_pixel": mt_cls,
        "both_visible": gl_cls == "green(GL)" and mt_cls == "red(Metal)",
    }

    # --- 2. Z-order: metal view (window-level) overlaps the GL half -------
    # NOTE: subviews are clipped to their superview; to genuinely overlap the
    # GL widget the metal view must be re-parented to the window contentView.
    content_view = objc.objc_object(c_void_p=ctypes.c_void_p(int(window.winId())))
    window_bounds = content_view.bounds()
    metal_nsview.removeFromSuperview()
    content_view.addSubview_(metal_nsview)
    metal_nsview.setFrame_(((0, 0), (window_bounds.size.width / 2, window_bounds.size.height)))
    err2 = present_metal()
    QtWidgets.QApplication.processEvents()
    time.sleep(0.4)
    img = QtWidgets.QApplication.primaryScreen().grabWindow(int(window.winId())).toImage()
    ov_px = img.pixelColor(int(iw * 0.25), int(ih * 0.5))
    report["z_order_overlap"] = {
        "metal_present_error": err2,
        "over_gl_region_now": _classify((ov_px.red(), ov_px.green(), ov_px.blue())),
    }
    # restore: back into the right-half host
    metal_nsview.removeFromSuperview()
    host_view.addSubview_(metal_nsview)
    metal_nsview.setFrame_((bounds.origin, bounds.size))

    # --- 3. resize tracking ------------------------------------------------
    metal_nsview.setFrame_((bounds.origin, bounds.size))
    window.resize(1000, WIN_H)
    QtWidgets.QApplication.processEvents()
    time.sleep(0.3)
    new_bounds = host_view.bounds()
    layer.setFrame_((new_bounds.origin, new_bounds.size))
    err3 = present_metal()
    QtWidgets.QApplication.processEvents()
    time.sleep(0.4)
    img = QtWidgets.QApplication.primaryScreen().grabWindow(int(window.winId())).toImage()
    right_px = img.pixelColor(int(img.width() * 0.75), int(img.height() * 0.5))
    report["resize"] = {
        "metal_present_error": err3,
        "host_view_width_after_resize": float(new_bounds.size.width),
        "right_region_after_resize": _classify((right_px.red(), right_px.green(), right_px.blue())),
    }

    # --- 4. present cadence (vsync rough check) ---------------------------
    n = 60
    t0 = time.perf_counter()
    lock_errors: List[str] = []
    for _ in range(n):
        e = present_metal()
        if e:
            lock_errors.append(e)
            break
    elapsed = time.perf_counter() - t0
    fps = n / elapsed if elapsed > 0 else 0
    screen_hz = QtWidgets.QApplication.primaryScreen().refreshRate()
    report["present_cadence"] = {
        "frames": n,
        "wall_seconds": elapsed,
        "fps": fps,
        "screen_refresh_hz": screen_hz,
        "vsync_locked": abs(fps - screen_hz) < screen_hz * 0.15 if screen_hz > 0 else None,
        "errors": lock_errors,
    }

    window.close()
    return report


def main() -> int:
    report = probe()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out = OUT_DIR / "qt_metal_coexistence.json"
    out.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")

    co = report["coexistence"]
    k3_verdict = "PASS(共存可行)" if co["both_visible"] else "FAIL(需改 QOpenGLWidget 语义或分叉)"
    md = [
        "# P1-03 Qt 表面共存 probe",
        "",
        f"> {report['timestamp']}  git `{report['git_sha'][:12]}`",
        "",
        "| 检查项 | 结果 |",
        "|---|---|",
        f"| GL 与 Metal 同窗各自呈现 | GL={co['gl_pixel']} / Metal={co['metal_pixel']} → "
        f"{'是' if co['both_visible'] else '否'} |",
        f"| Z 序（Metal 压到 GL 区上方） | {report['z_order_overlap']['over_gl_region_now']} |",
        f"| resize 后 Metal 区域 | {report['resize']['right_region_after_resize']} "
        f"(host 宽 {report['resize']['host_view_width_after_resize']}) |",
        f"| present 节奏 | {report['present_cadence']['fps']:.1f} fps "
        f"@ 屏幕 {report['present_cadence']['screen_refresh_hz']} Hz → "
        f"{'vsync 锁定' if report['present_cadence']['vsync_locked'] else '未锁定/混合'} |",
        f"| **K3 预判** | **{k3_verdict}** |",
        "",
    ]
    (OUT_DIR / "qt_metal_coexistence.md").write_text("\n".join(md))
    print(json.dumps(report, indent=2, ensure_ascii=False))
    print(f"\nwrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
