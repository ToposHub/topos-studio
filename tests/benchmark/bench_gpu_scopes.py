#!/usr/bin/env python3
"""GPU 示波器全链路功能 + 性能基准。

三部分：
  1. standalone ModernGL context：真实视频帧（tests/test_videos）跑全部 8 种
     示波器模式的功能验证（读回输出纹理做内容断言——仅测试代码允许 read，
     生产路径零回读）与每模式计时。
  2. 合成帧（纯色/色块）做严格数值校验：直方图列和 vs numpy、CIE 红峰位置、
     parade 三段通道、falsecolor LUT 档位、zebra 条纹、luma 白迹线。
  3. Qt 共享上下文 + GpuScopeWidget：真实跨 context blit 链路（cocoa，
     非 offscreen），ScopePanel 收到 ScopeGpuFrame 后 presenter 应呈现非黑帧。

用法：
    .venv/bin/python tests/benchmark/bench_gpu_scopes.py            # 全部
    .venv/bin/python tests/benchmark/bench_gpu_scopes.py --perf     # 仅计时
"""
import argparse
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

import cv2
import moderngl
import numpy as np

VIDEO_DIR = REPO_ROOT / "tests" / "test_videos"
VIDEO_FILES = [
    "4K_H264.mov",
    "2K_h264.mp4",
    "2K_prores422HQ.mov",
]

MODES = ['waveform', 'vectorscope', 'split', 'parade', 'luma',
         'histogram', 'cie', 'falsecolor']


def load_real_frame(name: str, time_s: float = 1.0) -> np.ndarray:
    """Decode one BGR uint8 frame from a real test video."""
    path = VIDEO_DIR / name
    if not path.exists():
        alt = sorted(VIDEO_DIR.glob("*.mp4")) + sorted(VIDEO_DIR.glob("*.mov"))
        if not alt:
            raise RuntimeError(f"no test videos under {VIDEO_DIR}")
        path = alt[0]
    cap = cv2.VideoCapture(str(path))
    fps = cap.get(cv2.CAP_PROP_FPS) or 24.0
    cap.set(cv2.CAP_PROP_POS_MSEC, time_s * 1000.0)
    ok, frame = cap.read()
    if not ok or frame is None:
        cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
        ok, frame = cap.read()
    cap.release()
    if not ok:
        raise RuntimeError(f"failed to decode frame from {path}")
    print(f"  video: {path.name} frame={frame.shape[1]}x{frame.shape[0]} "
          f"@{time_s:.1f}s")
    return frame


def frame_to_texture(ctx: moderngl.Context, bgr: np.ndarray):
    """Upload a BGR uint8 frame as an RGBA16F GL texture (renderer-like)."""
    h, w = bgr.shape[:2]
    rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB).astype(np.float32) / 255.0
    rgba = np.concatenate(
        [rgb, np.ones((h, w, 1), dtype=np.float32)], axis=2)
    tex = ctx.texture((w, h), 4, dtype='f2')
    tex.filter = (moderngl.LINEAR, moderngl.LINEAR)
    tex.write(rgba.astype(np.float16))
    return tex


def make_demand(mode: str, analysis, revision=1):
    from src.features.color.core.scope_types import ScopeDemand
    return ScopeDemand(
        revision=revision, source_revision=1, mode=mode,
        display_size=(400, 300), analysis_size=analysis,
    )


def read_tex(tex) -> np.ndarray:
    """Read a ModernGL texture top-down as float32 RGBA in [0, ~] range.

    Uses ``texture.read()`` (no temporary FBO): on Apple GPUs an FBO created
    around an f2 texture can poison subsequent FBO reads with stale data,
    which would corrupt the very values this benchmark validates.  (The
    production pipeline never reads textures; this is test-only code.)
    """
    h, w = tex.size[1], tex.size[0]
    comps = tex.components
    if tex.dtype == 'f2':
        arr = np.frombuffer(tex.read(), np.float16).reshape(h, w, comps)
        arr = arr.astype(np.float32)
    elif tex.dtype == 'f4':
        arr = np.frombuffer(tex.read(), np.float32).reshape(h, w, comps)
    else:
        arr = np.frombuffer(tex.read(), np.uint8).reshape(h, w, comps)
        arr = arr.astype(np.float32) / 255.0
    return np.flipud(arr)


def report(name: str, ok: bool, detail: str = ""):
    mark = "PASS" if ok else "FAIL"
    print(f"    [{mark}] {name}" + (f" — {detail}" if detail else ""))
    return ok


# ---------------------------------------------------------------------------
# Part 1+2: standalone context — correctness on synthetic frames + timing on
# real video frames.
# ---------------------------------------------------------------------------
def run_standalone(args) -> bool:
    from src.features.color.core.scope_types import (
        DEFAULT_ANALYSIS_SIZE, PLAYBACK_ANALYSIS_SIZE,
        HIGH_QUALITY_ANALYSIS_SIZE,
    )
    from src.shared.gpu.gpu_scope_engine import GpuScopeEngine

    ok = True
    ctx = moderngl.create_standalone_context()
    engine = GpuScopeEngine(ctx)
    if not engine.load_shaders():
        print("FATAL: scope shaders failed to compile on real GPU")
        return False
    print("  shaders: 11 scope programs compiled OK")

    # ---------------- strict correctness on synthetic frames ----------
    print("  -- correctness (synthetic frames) --")
    aw, ah = PLAYBACK_ANALYSIS_SIZE

    # -- histogram column sums vs numpy histogram --
    rng = np.random.default_rng(11)
    syn = (rng.random((ah, aw, 3)) ** 2.2).astype(np.float32)  # HDR-ish skew
    tex = frame_to_texture(ctx, (syn[:, :, ::-1] * 255).astype(np.uint8))
    frame = make_demand('histogram', (aw, ah))
    outputs = engine.render_demand(frame, tex)
    ctx.finish()
    analysis = read_tex(engine._pool['sample_tex'])[:, :, :3]
    hist_sum = read_tex(engine._pool['hist_sum'])[0, :, :3]
    gpu_counts = np.expm1(hist_sum)
    if args.debug:
        print(f"    hist_sum[0,:4]: {np.round(hist_sum[0, :4], 3)} "
              f"gpu_total={gpu_counts.sum():.1f}")
    for ch, cname in enumerate("RGB"):
        ref, _ = np.histogram(analysis[:, :, ch], bins=512, range=(0.0, 1.0))
        got = gpu_counts[:, ch]
        total_err = abs(got.sum() - ref.sum()) / max(1.0, ref.sum())
        # bin edges can shift by one cell through point snapping; compare
        # with a 3x1 box blur and require > 0.98 correlation.
        k = np.array([1.0, 1.0, 1.0])
        g = np.convolve(got, k, 'same')
        r = np.convolve(ref.astype(np.float64), k, 'same')
        corr = float(np.corrcoef(g, r)[0, 1]) if r.std() > 0 else 1.0
        ok &= report(
            f"histogram[{cname}] vs numpy",
            total_err < 0.01 and corr > 0.98,
            f"total_err={total_err:.4f} corr={corr:.4f}",
        )
    tex.release()

    # -- CIE: pure red peak must land at the BT.709 red primary --
    red = np.zeros((ah, aw, 3), np.uint8)
    red[:, :, 2] = 255
    tex = frame_to_texture(ctx, red)
    frame = make_demand('cie', (aw, ah))
    engine.render_demand(frame, tex)
    ctx.finish()
    cie = read_tex(engine._pool['cie_density'])[:, :, 0]
    peak = np.unravel_index(np.argmax(cie), cie.shape)
    from src.shared.gpu.gpu_scope_engine import CIE_SIZE
    ch_cie, cw_cie = CIE_SIZE[1], CIE_SIZE[0]
    exp_x = 0.640 / 0.8 * cw_cie
    # read_tex flips vertically: the row index is top-down (high y at top).
    exp_y = (1.0 - 0.330 / 0.9) * ch_cie
    err = float(np.hypot(peak[1] - exp_x, peak[0] - exp_y))
    ok &= report("cie pure-red peak at 709 red", err <= 4.0,
                 f"peak=({peak[1]},{peak[0]}) expected≈({exp_x:.0f},{exp_y:.0f})"
                 f" dist={err:.1f} bins")
    tex.release()

    # -- parade thirds are channel-monochrome --
    grad = np.zeros((ah, aw, 3), np.float32)
    grad[:, :, 0] = np.linspace(0, 1, aw, dtype=np.float32)[None, :]  # R ramp
    grad[:, :, 1] = 0.5
    grad[:, :, 2] = 0.25
    tex = frame_to_texture(ctx, (grad[:, :, ::-1] * 255).astype(np.uint8))
    frame = make_demand('parade', (aw, ah))
    engine.render_demand(frame, tex)
    ctx.finish()
    par = read_tex(engine._pool['wf_display'])[:, :, :3]
    third = par.shape[1] // 3
    # Exact accumulation check on the density texture (pre-tonemap, no
    # graticule): each third must contain its own channel's samples only.
    dens_par = read_tex(engine._pool['wf_density'])
    dens_third = dens_par.shape[1] // 3
    ratios = []
    for idx, dom_ch in enumerate((0, 1, 2)):
        seg = dens_par[:, idx * dens_third:(idx + 1) * dens_third, :3]
        energy = seg.sum(axis=(0, 1))
        dom = energy[dom_ch]
        others = energy.sum() - dom
        ratios.append(dom / max(1e-6, others))
    ok &= report("parade thirds channel-isolated (density)", min(ratios) > 20.0,
                 f"dominance ratios={[round(r, 1) for r in ratios]}")
    lum = par.sum(axis=2)
    col_energy = lum.max(axis=0)
    # R ramp must fill the left third edge-to-edge; flat 0.5 G stays a band.
    r_cols = np.where(col_energy[:third] > 0.05 * col_energy[:third].max())[0]
    ok &= report("parade R ramp spans full third",
                 r_cols.min() <= 2 and r_cols.max() >= third - 3,
                 f"cols {r_cols.min()}..{r_cols.max()} of {third}")
    tex.release()

    # -- falsecolor stops + zebra --
    from src.features.color.core.scope_types import ScopeDemand
    fc = np.full((ah, aw, 3), 0.55, np.float32)          # luma 0.55 -> green
    tex = frame_to_texture(ctx, (fc[:, :, ::-1] * 255).astype(np.uint8))
    frame = make_demand('falsecolor', (aw, ah))
    engine.render_demand(frame, tex)
    ctx.finish()
    fc_img = read_tex(engine._pool['fc_display'])
    cx, cy = fc_img.shape[1] // 2, fc_img.shape[0] // 2
    px = fc_img[cy, cx, :3]
    ok &= report("falsecolor luma 0.55 -> green stop",
                 abs(px[1] - 1.0) < 0.05 and px[0] < 0.05 and px[2] < 0.05,
                 f"center px={np.round(px, 3)}")

    over = np.full((ah, aw, 3), 1.0, np.float32)          # luma 1.0 -> white
    tex2 = frame_to_texture(ctx, (over[:, :, ::-1] * 255).astype(np.uint8))
    frame = ScopeDemand(
        revision=frame.revision, source_revision=frame.source_revision,
        mode='falsecolor', display_size=frame.display_size,
        analysis_size=(aw, ah), falsecolor_mode='zebra',
        zebra_threshold=1.0,
    )
    engine.render_demand(frame, tex2)
    ctx.finish()
    zb = read_tex(engine._pool['fc_display'])
    mid = zb[zb.shape[0] // 2, :, :3]
    has_white = float((mid.sum(axis=1) > 2.5).mean())
    has_gray = float(((mid > 0.7) & (mid < 0.95)).all(axis=1).mean())
    ok &= report("zebra stripes over exposure",
                 has_white > 0.2 and has_gray > 0.2,
                 f"white={has_white:.2f} gray={has_gray:.2f}")
    tex.release()
    tex2.release()

    # -- luma white trace --
    tex = frame_to_texture(ctx, (grad[:, :, ::-1] * 255).astype(np.uint8))
    frame = make_demand('luma', (aw, ah))
    engine.render_demand(frame, tex)
    ctx.finish()
    lu = read_tex(engine._pool['wf_display'])[:, :, :3]
    trace = lu.max(axis=0)  # per column max
    neutral = trace[(trace > 0.2).any(axis=1)]
    chroma = neutral.max(axis=1) - neutral.min(axis=1)
    ok &= report("luma trace is neutral (white)",
                 float(np.median(chroma)) < 0.15,
                 f"median chroma={np.median(chroma):.3f}")
    tex.release()

    # ---------------- display pass sanity on a real frame -------------
    print("  -- display content (real frame) --")
    bgr = load_real_frame(VIDEO_FILES[0])
    tex = frame_to_texture(ctx, bgr)
    for mode in MODES:
        frame = make_demand(mode, PLAYBACK_ANALYSIS_SIZE)
        outputs = engine.render_demand(frame, tex)
        ctx.finish()
        key, (tex_id, size) = next(iter(outputs.items()))
        gl_tex = next(t for t in engine._pool.values()
                      if isinstance(t, moderngl.Texture) and t.glo == tex_id)
        img = read_tex(gl_tex)[:, :, :3]
        lit = float((img.max(axis=2) > 0.05).mean())
        ok &= report(f"{mode} renders non-empty",
                     lit > 0.01, f"{size[0]}x{size[1]} lit={lit:.2f}")
    tex.release()

    # ---------------- performance on real frames -----------------------
    if not args.skip_perf:
        print("  -- performance (real frames, avg of 30 passes) --")
        for name in VIDEO_FILES:
            try:
                bgr = load_real_frame(name)
            except RuntimeError as exc:
                print(f"    skip {name}: {exc}")
                continue
            tex = frame_to_texture(ctx, bgr)
            h, w = bgr.shape[:2]
            print(f"    source {w}x{h} [{name}]")
            for label, analysis in (
                ("playback512", PLAYBACK_ANALYSIS_SIZE),
                ("hq768", HIGH_QUALITY_ANALYSIS_SIZE),
                ("idle320", DEFAULT_ANALYSIS_SIZE),
            ):
                for mode in MODES:
                    frame = make_demand(mode, analysis)
                    engine.render_demand(frame, tex)  # warm pool/shaders
                    ctx.finish()
                    n = 30
                    t0 = time.perf_counter()
                    for _ in range(n):
                        engine.render_demand(frame, tex)
                    ctx.finish()
                    ms = (time.perf_counter() - t0) / n * 1000.0
                    print(f"      {label:>11} {mode:<12} {ms:7.2f} ms/pass")
            tex.release()

        # CPU reference: the legacy numpy waveform on the same 4K frame.
        from src.shared.ui.widgets.vectorscope_widget import (
            _downsample_columns, _waveform_density,
        )
        bgr = load_real_frame(VIDEO_FILES[0])
        n = 10
        t0 = time.perf_counter()
        for _ in range(n):
            small = _downsample_columns(bgr.astype(np.float32), 1024)
            _waveform_density(small[:, :, 2])
        cpu_ms = (time.perf_counter() - t0) / n * 1000.0
        print(f"    CPU reference (single-channel waveform only): "
              f"{cpu_ms:.2f} ms — GPU runs all channels + display")
    ctx.release()
    return ok


# ---------------------------------------------------------------------------
# Part 3: Qt shared context — real cross-context blit into GpuScopeWidget.
# ---------------------------------------------------------------------------
def run_qt_present() -> bool:
    import os
    if os.environ.get("QT_QPA_PLATFORM", "").lower() == "offscreen":
        print("  skip (QOpenGLWidget unsupported on offscreen platform; "
              "run without QT_QPA_PLATFORM=offscreen)")
        return True
    from PyQt6.QtCore import Qt, QTimer
    from PyQt6.QtGui import QImage, QOpenGLContext, QOffscreenSurface, \
        QSurfaceFormat
    from PyQt6.QtWidgets import QApplication

    # 与 src/main.py 一致：默认 GL 格式 + 全局上下文共享，否则
    # QOpenGLWidget 之间 / 渲染线程 context 不在一个 share group。
    QApplication.setAttribute(
        Qt.ApplicationAttribute.AA_ShareOpenGLContexts, True)
    fmt = QSurfaceFormat()
    fmt.setVersion(4, 5)
    fmt.setProfile(QSurfaceFormat.OpenGLContextProfile.CoreProfile)
    fmt.setRenderableType(QSurfaceFormat.RenderableType.OpenGL)
    fmt.setSamples(4 if sys.platform != 'win32' else 0)
    fmt.setSwapBehavior(QSurfaceFormat.SwapBehavior.DoubleBuffer)
    QSurfaceFormat.setDefaultFormat(fmt)

    app = QApplication.instance() or QApplication([])

    from src.features.color.ui.scope_panel import ScopePanel
    panel = ScopePanel()
    panel.resize(500, 320)
    panel.show()

    # Qt promotes the first realized QOpenGLWidget context to the global
    # share context; realization needs a running event loop so the window
    # is exposed and the platform GL context is really created. The whole
    # check therefore runs inside the event loop via a single-shot timer
    # and reports through a mutable holder.
    result: dict = {'ok': False}

    def _run() -> None:
        try:
            result['ok'] = _present_checks(panel)
        except Exception as exc:
            import traceback
            traceback.print_exc()
            result['ok'] = False
        finally:
            app.quit()

    QTimer.singleShot(300, _run)
    app.exec()
    panel.hide()
    panel.deleteLater()
    return result['ok']


def _present_checks(panel) -> bool:
    from PyQt6.QtGui import QImage, QOpenGLContext, QOffscreenSurface
    from PyQt6.QtWidgets import QApplication
    import moderngl as mgl

    app = QApplication.instance()
    ok = True
    # Join the presenter's own share group: QOpenGLWidget contexts all share
    # with each other, so sharing with any one of them lets the render
    # context publish scope textures the widgets can blit.
    widget_ctx = panel._gpu_wf_presenter.context()
    if widget_ctx is None:
        panel._gpu_wf_presenter.grab()
        widget_ctx = panel._gpu_wf_presenter.context()
    if widget_ctx is None:
        print("  FAIL: presenter GL context not realized")
        return False
    render_ctx = QOpenGLContext()
    render_ctx.setShareContext(widget_ctx)
    fmt = widget_ctx.format()
    fmt.setSamples(0)
    render_ctx.setFormat(fmt)
    if not render_ctx.create():
        print("  FAIL: render context creation failed")
        return False
    surface = QOffscreenSurface()
    surface.setFormat(fmt)
    surface.create()
    if not render_ctx.makeCurrent(surface):
        print("  FAIL: makeCurrent failed")
        return False

    ctx = mgl.create_context()
    from src.shared.gpu.gpu_scope_engine import GpuScopeEngine
    engine = GpuScopeEngine(ctx)
    if not engine.load_shaders():
        print("  FAIL: shaders did not compile")
        return False

    bgr = load_real_frame(VIDEO_FILES[1] if (VIDEO_DIR / VIDEO_FILES[1]).exists()
                          else VIDEO_FILES[0])
    tex = frame_to_texture(ctx, bgr)

    from src.features.color.core.scope_types import ScopeGpuFrame
    rev = panel.bump_demand_revision()
    for mode in MODES:
        panel.set_mode(mode)
        rev = panel.gpu_demand_revision
        demand = make_demand(
            mode, (512, 288), revision=rev,
        )
        outputs = engine.render_demand(demand, tex)
        ctx.finish()
        frame = ScopeGpuFrame(
            revision=rev, source_revision=1,
            waveform_texture_id=outputs.get('waveform', (None, (0, 0)))[0],
            waveform_size=outputs.get('waveform', (None, (0, 0)))[1],
            vectorscope_texture_id=outputs.get('vectorscope', (None, (0, 0)))[0],
            vectorscope_size=outputs.get('vectorscope', (None, (0, 0)))[1],
            parade_texture_id=outputs.get('parade', (None, (0, 0)))[0],
            parade_size=outputs.get('parade', (None, (0, 0)))[1],
            luma_texture_id=outputs.get('luma', (None, (0, 0)))[0],
            luma_size=outputs.get('luma', (None, (0, 0)))[1],
            histogram_texture_id=outputs.get('histogram', (None, (0, 0)))[0],
            histogram_size=outputs.get('histogram', (None, (0, 0)))[1],
            cie_texture_id=outputs.get('cie', (None, (0, 0)))[0],
            cie_size=outputs.get('cie', (None, (0, 0)))[1],
            falsecolor_texture_id=outputs.get('falsecolor', (None, (0, 0)))[0],
            falsecolor_size=outputs.get('falsecolor', (None, (0, 0)))[1],
        )
        panel.update_gpu_scope(frame)
        for _ in range(12):
            app.processEvents()
        from src.features.color.ui.scope_panel import _MODE_PRESENTER_ATTR
        if mode == 'split':
            attrs = ['_gpu_wf_presenter', '_gpu_vs_presenter']
        else:
            attrs = [_MODE_PRESENTER_ATTR[mode]]
        for attr in attrs:
            presenter = getattr(panel, attr)
            grabbed = presenter.grab()
            img = grabbed.toImage().convertToFormat(
                QImage.Format.Format_RGB32)
            w, h = img.width(), img.height()
            bpl = img.bytesPerLine()
            raw = img.constBits().asarray(bpl * h)
            arr = np.frombuffer(raw, dtype=np.uint8).reshape(h, bpl // 4, 4)
            arr = arr[:, :w, :]
            lit_ratio = float((arr[:, :, :3].max(axis=2) > 12).mean())
            ok &= report(f"presenter blit {mode}",
                         lit_ratio > 0.01, f"{w}x{h} lit={lit_ratio:.2f}")
    tex.release()
    engine.release()
    ctx.release()
    render_ctx.doneCurrent()
    return ok


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--perf', action='store_true',
                        help='只跑性能计时，跳过断言')
    parser.add_argument('--skip-perf', action='store_true',
                        help='跳过性能计时')
    parser.add_argument('--skip-qt', action='store_true',
                        help='跳过 Qt presenter blit 部分')
    parser.add_argument('--debug', action='store_true',
                        help='输出中间数据')
    args = parser.parse_args()

    print("[1/2] standalone GPU pipeline (correctness + timing)")
    ok = run_standalone(args)
    print(f"  => standalone: {'ALL PASS' if ok else 'FAILURES ABOVE'}")

    if not args.skip_qt:
        print("[2/2] Qt shared-context presenter blit")
        ok_qt = run_qt_present()
        print(f"  => presenter: {'ALL PASS' if ok_qt else 'FAILURES ABOVE'}")
    else:
        ok_qt = True

    return 0 if (ok and ok_qt) else 1


if __name__ == '__main__':
    sys.exit(main())
