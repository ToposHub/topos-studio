#!/usr/bin/env python3
"""Audit every usable composite-page node: validity + render cost.

Boots the REAL app, builds a comp clip, then for each node type under test:

  1. wires a minimal graph (video_frame → node → output, or +constant for
     two-input merges, or node → output for generators)
  2. waits for the first valid viewer emission (cold eval: includes shader
     compile / clip open)
  3. performs N warm evals (seek to fresh frames so upstream texture changes
     force a real re-eval; generators vary a natural parameter instead)
  4. collects the node's own eval time from PerfMonitor.record_node_eval
  5. reads back the presented result once to verify the node actually
     produces non-black pixels

Reports a table + JSON: valid flag, cold ms, warm median/p90/max ms,
pixel stats, errors. Incremental JSON dump after each node so a crash
mid-run still yields data.

Usage:
    .venv/bin/python tests/benchmark/audit_composite_nodes.py [video_path]
    TOPOS_NODE_FILTER=blur,glow ...  # re-run a subset
"""

from __future__ import annotations

import ctypes
import json
import logging
import os
import sys
import threading
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

if not os.environ.get("TOPOS_CONFIG_PATH"):
    os.environ["TOPOS_CONFIG_PATH"] = str(REPO_ROOT / ".topos" / "config.json")

os.environ.setdefault("TOPOS_PRODUCT_MODE", "studio")
os.environ["TOPOS_TEST_MODE"] = "1"
os.environ["TOPOS_FEATURE_EDIT"] = "1"

VIDEO = Path(sys.argv[1] if len(sys.argv) > 1 else REPO_ROOT / "tests/test_videos/2K_H265.mov").resolve()

WARM_EVALS = 6
RESULTS_DIR = REPO_ROOT / "tests/benchmark/results"


def log(msg: str) -> None:
    print(f"[AUDIT {time.strftime('%H:%M:%S')}] {msg}", flush=True)


def install_watchdog() -> None:
    """Dump all-thread Python tracebacks if the main loop stalls.

    Some node sequences deterministically hang the UI thread in a pure
    Python loop; without py-spy root access this is the only way to see
    which function spins.
    """
    import faulthandler
    import threading

    state = {"heartbeat": time.time()}

    def beat():
        state["heartbeat"] = time.time()

    def watch():
        while True:
            time.sleep(20.0)
            stall = time.time() - state["heartbeat"]
            if stall > 25.0:
                print(f"\n[WATCHDOG] main loop stalled {stall:.0f}s — "
                      f"dumping tracebacks\n", flush=True)
                faulthandler.dump_traceback(file=sys.stdout)
                os._exit(99)

    t = threading.Thread(target=watch, daemon=True)
    t.start()
    globals()["_heartbeat"] = beat


_heartbeat = lambda: None  # replaced by install_watchdog


# ────────────────────────── temp test assets ──────────────────────────

def make_identity_lut(path: Path) -> None:
    lines = ["TITLE \"audit_identity\"", "LUT_3D_SIZE 2"]
    for b in (0.0, 1.0):
        for g in (0.0, 1.0):
            for r in (0.0, 1.0):
                lines.append(f"{r:.6f} {g:.6f} {b:.6f}")
    path.write_text("\n".join(lines) + "\n")


def make_test_png(path: Path) -> None:
    import numpy as np
    from PyQt6.QtGui import QImage

    h, w = 1080, 1920
    yy, xx = np.mgrid[0:h, 0:w]
    arr = np.zeros((h, w, 3), dtype=np.uint8)
    arr[..., 0] = (xx * 255 // w).astype(np.uint8)
    arr[..., 1] = (yy * 255 // h).astype(np.uint8)
    arr[..., 2] = ((xx + yy) * 255 // (w + h)).astype(np.uint8)
    img = QImage(arr.data, w, h, w * 3, QImage.Format.Format_RGB888)
    if not img.save(str(path)):
        raise RuntimeError("failed to save test png")


# ────────────────────────── node test matrix ──────────────────────────
# wiring: "single" video→node→out | "dual" video+const→node→out | "gen" node→out
# vary: (param, step) for generators whose cache would otherwise hit

LUT_PATH = Path("/tmp/audit_identity.cube")
PNG_PATH = Path("/tmp/audit_gradient.png")

NODE_SPECS = [
    # Filter
    ("blur", "filter", "single", {}, None),
    ("glow", "filter", "single", {}, None),
    ("sharpen", "filter", "single", {}, None),
    ("denoise", "filter", "single", {}, None),
    ("erode_dilate", "filter", "single", {}, None),
    ("defocus", "filter", "single", {}, None),
    ("edge_detect", "filter", "single", {}, None),
    # Color
    ("grade", "color", "single", {}, None),
    ("color_correct", "color", "single", {}, None),
    ("cs_transform", "color", "single", {}, None),
    ("huecorrect", "color", "single", {}, None),
    ("lut", "color", "single", {"lut_path": str(LUT_PATH)}, None),
    # Transform
    ("transform", "transform", "single", {}, None),
    ("reformat", "transform", "single", {}, None),
    ("crop", "transform", "single", {}, None),
    ("cornerpin", "transform", "single", {}, None),
    # Channel
    ("shuffle", "channel", "single", {}, None),
    ("copy_channel", "channel", "single", {}, None),
    ("channel_merge", "channel", "single", {}, None),
    ("set_alpha", "channel", "single", {}, None),
    ("remove_channels", "channel", "single", {}, None),
    # Keyer
    ("keyer", "keyer", "single", {}, None),
    ("luma_keyer", "keyer", "single", {}, None),
    ("chroma_keyer", "keyer", "single", {}, None),
    ("difference_keyer", "keyer", "single", {}, None),
    ("matte_cleanup", "keyer", "single", {}, None),
    # Mask
    ("mask", "mask", "dual", {}, None),
    ("mask_merge", "mask", "dual", {}, None),
    ("roto_shape", "mask", "gen", {}, ("center_x", 0.01)),
    # Utility
    ("invert", "utility", "single", {}, None),
    ("clamp", "utility", "single", {}, None),
    ("normalize", "utility", "single", {}, None),
    ("compare", "utility", "dual", {}, None),
    ("dot", "utility", "single", {}, None),
    ("noop", "utility", "single", {}, None),
    # Merge
    ("merge", "merge", "dual", {}, None),
    ("blend", "merge", "dual", {}, None),
    ("deep_merge", "merge", "dual", {}, None),
    ("matte", "merge", "dual", {}, None),
    ("dissolve", "merge", "dual", {}, None),
    ("switch", "merge", "dual", {}, None),
    ("premult", "merge", "single", {}, None),
    ("unpremult", "merge", "single", {}, None),
    # Time
    ("time_offset", "time", "single", {}, None),
    ("frame_hold", "time", "single", {}, None),
    ("retime", "time", "single", {}, None),
    # Generators
    ("constant", "input", "gen",
     {"color_r": 30000.0, "color_g": 12000.0, "color_b": 22000.0}, ("color_r", 800)),
    ("text", "input", "gen", {"text": "Node Audit"}, ("font_size", 2)),
    ("noise", "input", "gen", {}, ("seed", 1)),
    ("ramp", "input", "gen", {}, ("end_x", 0.01)),
    # Special sources
    ("image_input", "input", "gen", {"file_path": str(PNG_PATH)},
     ("premultiplied", "toggle")),
    # 3D (standalone empty scene)
    ("scene3d", "3d", "gen", {}, None),
]

SKIPPED = [
    ("output", "terminal node (used as graph sink everywhere)"),
    ("file_output", "side-effect node: writes files to disk"),
    ("video_frame", "source node: measured as the pipeline baseline"),
    ("constant(backdrop)", "source node: used as B input in dual wiring"),
    ("timeline_clip_input", "needs timeline runtime context (edit page)"),
    ("topos_color", "needs color page pipeline attached"),
    ("motion_graphic", "None placeholder unless teaching feature enabled"),
    ("color_input", "deprecated legacy, auto-migrated on load"),
    ("ai_generate/ai_inpaint/ai_expand/ai_remove/ai_denoise",
     "require external AI service"),
    ("group/precomp", "structural containers, no pixel op"),
    ("camera3d/light3d/geo3d/model3d/deformer3d",
     "3D child nodes: only meaningful wired inside a scene3d"),
]


# ────────────────────────── instrumentation ──────────────────────────

class Collector:
    """Collects per-node eval samples + errors across the render thread.

    ``real`` holds non-cached eval times (the actual GPU work); cached hits
    are counted separately. Warm waits block until a *real* eval lands —
    otherwise a seek can be satisfied instantly by a stale-frame re-present
    (video node falls back to its last decoded texture while the new frame
    is still decoding, which cache-hits every downstream node).
    """

    def __init__(self):
        self.lock = threading.Lock()
        self.node_id: str | None = None
        self.active = False
        self.samples: list[float] = []
        self.real: list[float] = []
        self.cached = 0
        self.errors: list[str] = []
        self.capture = False
        self.readback: dict | None = None

    def reset(self, node_id: str):
        with self.lock:
            self.node_id = node_id
            self.active = True
            self.samples = []
            self.real = []
            self.cached = 0
            self.errors = []
            self.capture = False
            self.readback = None


def install_instrumentation(renderer, collector: Collector):
    from src.features.composite.utils.perf_monitor import PerfMonitor
    from src.features.composite.render import composite_renderer as cr_mod

    orig_record = PerfMonitor.record_node_eval

    def traced_record(self, node_id, node_type, eval_time_ms,
                      was_cached, was_dirty, error=None):
        orig_record(self, node_id, node_type, eval_time_ms,
                    was_cached, was_dirty, error)
        with collector.lock:
            if collector.active and node_id == collector.node_id:
                collector.samples.append(float(eval_time_ms))
                if was_cached:
                    collector.cached += 1
                else:
                    collector.real.append(float(eval_time_ms))
                if error:
                    collector.errors.append(str(error))

    PerfMonitor.record_node_eval = traced_record

    orig_render = cr_mod.CompositeRenderer._render_to_output

    def traced_render(self, result, request_id=0):
        r = orig_render(self, result, request_id)
        with collector.lock:
            do_read = (collector.active and collector.capture
                       and collector.readback is None)
        if do_read and result is not None and getattr(result, "is_valid", False):
            try:
                collector.readback = _read_result_pixels(result)
            except Exception as e:  # noqa: BLE001
                collector.readback = {"error": repr(e)}
        return r

    cr_mod.CompositeRenderer._render_to_output = traced_render


def _read_result_pixels(result) -> dict:
    import numpy as np
    from OpenGL.GL import (GL_FLOAT, GL_RGBA, GL_TEXTURE_2D,
                           glBindTexture, glGetTexImage)

    tex_id = int(result.texture_id)
    w, h = int(result.width), int(result.height)
    if tex_id <= 0 or w <= 0 or h <= 0 or w * h > 8192 * 8192:
        return {"error": f"bad result tex={tex_id} {w}x{h}"}
    glBindTexture(GL_TEXTURE_2D, tex_id)
    n = w * h * 4
    buf = (ctypes.c_float * n)()
    glGetTexImage(GL_TEXTURE_2D, 0, GL_RGBA, GL_FLOAT, buf)
    arr = np.frombuffer(buf, dtype=np.float32).reshape(h, w, 4)
    rgb = arr[:, :, :3]
    return {
        "w": w, "h": h,
        "mean": round(float(rgb.mean()), 5),
        "std": round(float(rgb.std()), 5),
        "nonblack": round(float((rgb.max(axis=2) > 0.004).mean()), 4),
    }


class RenderLogCatcher(logging.Handler):
    """Catch 'no valid output' warnings from the renderer for the report."""

    def __init__(self, collector: Collector):
        super().__init__()
        self.collector = collector
        self.records: list[str] = []

    def emit(self, record: logging.LogRecord):
        try:
            msg = record.getMessage()
        except Exception:  # noqa: BLE001
            return
        if "no valid output" in msg or record.levelno >= logging.ERROR:
            with self.collector.lock:
                if self.collector.active and len(self.collector.errors) < 8:
                    self.collector.errors.append(msg[:300])


# ────────────────────────── helpers ──────────────────────────

def port_names(node) -> list[str]:
    return [p.name for p in node.inputs]


def out_port_name(node) -> str:
    return node.outputs[0].name if node.outputs else "result"


def stats(values: list[float]) -> dict:
    if not values:
        return {"n": 0}
    sv = sorted(values)
    n = len(sv)
    return {
        "n": n,
        "median": round(sv[n // 2], 2),
        "p90": round(sv[min(n - 1, int(n * 0.9))], 2),
        "max": round(sv[-1], 2),
    }


def main() -> int:
    from PyQt6.QtCore import Qt, QTimer
    from PyQt6.QtGui import QSurfaceFormat
    from PyQt6.QtWidgets import QApplication, QDialog

    make_identity_lut(LUT_PATH)
    make_test_png(PNG_PATH)
    install_watchdog()
    log(f"temp assets: {LUT_PATH}, {PNG_PATH}")

    os.environ["QSG_RHI_BACKEND"] = "opengl"
    fmt = QSurfaceFormat()
    fmt.setVersion(4, 5)
    fmt.setProfile(QSurfaceFormat.OpenGLContextProfile.CoreProfile)
    fmt.setRenderableType(QSurfaceFormat.RenderableType.OpenGL)
    fmt.setRedBufferSize(8); fmt.setGreenBufferSize(8); fmt.setBlueBufferSize(8); fmt.setAlphaBufferSize(8)
    fmt.setDepthBufferSize(24); fmt.setStencilBufferSize(8)
    if sys.platform != "win32":
        fmt.setSamples(4)
    fmt.setSwapBehavior(QSurfaceFormat.SwapBehavior.DoubleBuffer)
    QSurfaceFormat.setDefaultFormat(fmt)
    QApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts, True)
    app = QApplication(sys.argv)

    def _auto_dismiss_dialogs():
        for w in QApplication.topLevelWidgets():
            if isinstance(w, QDialog) and w.isVisible():
                w.reject()

    _dialog_killer = QTimer()
    _dialog_killer.timeout.connect(_auto_dismiss_dialogs)
    _dialog_killer.start(200)

    from src.shared.config.config_manager import ConfigManager
    from src.shared.feature_flags import load_feature_flags
    from src.app.feature_registry import FeatureRegistry
    from src.app.app_context import AppContext
    from src.shared.i18n.i18n import I18N
    from src.app.main_window import MainWindow

    config_manager = ConfigManager()
    feature_flags = load_feature_flags(config_manager)
    I18N.load_locale("zh_CN")
    registry = FeatureRegistry()
    registry.initialize(AppContext(config_manager=config_manager, feature_flags=feature_flags, i18n=I18N, logger=None))
    from src.features.color.module import ColorModule
    registry.register_module(ColorModule())
    if feature_flags.is_enabled("composite"):
        from src.features.composite.module import CompositeModule
        m = CompositeModule(); registry.register_module(m); m.discover_and_register_plugins()
    if feature_flags.is_enabled("edit"):
        from src.features.edit.module import EditModule
        registry.register_module(EditModule())

    mw = MainWindow(feature_flags=feature_flags, feature_registry=registry, test_mode=False)
    mw.show()
    for _ in range(90):
        app.processEvents()
    settle_end = time.time() + 4.0
    while time.time() < settle_end:
        app.processEvents()
        time.sleep(0.02)

    # import media (creates the project)
    mw.media_pool_panel._import_files([str(VIDEO)])
    project = None
    deadline = time.time() + 60
    while time.time() < deadline:
        app.processEvents()
        project = getattr(mw, "_current_edit_project", None)
        if project is not None and project.get_asset_by_path(str(VIDEO)) is not None:
            break
        time.sleep(0.02)
    assert project is not None, "project not created"
    asset = project.get_asset_by_path(str(VIDEO))
    total_frames = int(asset.metadata.get("total_frames") or 240)
    log(f"asset imported fps={asset.metadata.get('fps')} frames={total_frames}")

    # fresh frames: a global pool sliced per test node so every node's evals
    # hit frames never decoded before (otherwise the whole downstream chain
    # cache-hits and measures nothing)
    POOL_STEP = 2
    frame_pool = list(range(3, max(4, total_frames - 3), POOL_STEP))
    frame_cursor = 0

    def next_frames(n: int) -> list[int]:
        nonlocal frame_cursor
        if not frame_pool:
            return [1] * n
        out = [frame_pool[(frame_cursor + i) % len(frame_pool)]
               for i in range(n)]
        frame_cursor += n
        return out

    # empty comp clip → Composite page
    new_project, comp_clip_id, comp_asset_id, graph_id = project.create_empty_comp_clip(
        name="Node Audit", width=1920, height=1080, fps=24.0,
        duration_frames=max(total_frames + 48, 720),
        background_color=(0.0, 0.0, 0.0, 1.0))
    mw._current_edit_project = new_project
    for attempt in range(3):
        mw._switch_to_composite_page(graph_id)
        deadline = time.time() + 10
        while time.time() < deadline:
            app.processEvents()
            if getattr(mw, "_current_composite_id", None) == graph_id:
                break
            time.sleep(0.05)
        if getattr(mw, "_current_composite_id", None) == graph_id:
            break
    for _ in range(30):
        app.processEvents()

    page = mw.composite_page
    assert page is not None, "composite page missing"
    deadline = time.time() + 30
    while time.time() < deadline and getattr(page, "_renderer", None) is None:
        app.processEvents()
        time.sleep(0.02)
    renderer = page._renderer
    assert renderer is not None, "composite renderer not initialized"

    deadline = time.time() + 15
    while time.time() < deadline:
        graph = page._node_graph
        outs = graph.get_nodes_by_type("output") if graph is not None else []
        if outs and getattr(mw, "_current_composite_id", None) == graph_id:
            break
        app.processEvents()
        time.sleep(0.05)
    else:
        raise AssertionError("our comp graph never loaded")
    out_node = outs[0]

    actions = page._node_editor._actions
    consts = graph.get_nodes_by_type("constant")
    const_node = consts[0] if consts else actions.add_node("constant", -1200, 500)
    video_node = actions.add_node("video_frame", -1100, 300,
                                  parameters={"file_path": str(VIDEO)})
    assert video_node is not None, "video node creation failed"

    # emissions tracking: (t, rid, tex, w, h) — VALID presents only; the
    # renderer also emits on invalid graphs (viewer cleared) which would
    # otherwise satisfy wait loops instantly and starve real evals.
    emissions: list[tuple[float, int, int, int, int]] = []

    def _on_frame_ready(rid, tex, w, h):
        if int(tex) > 0 and int(w) > 0 and int(h) > 0:
            emissions.append((time.perf_counter(), int(rid), int(tex), int(w), int(h)))

    page.composite_frame_ready.connect(_on_frame_ready)

    def wait_emissions(n_target: int, timeout: float = 30.0) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline and len(emissions) < n_target:
            app.processEvents()
            time.sleep(0.005)
        return len(emissions) >= n_target

    # instrumentation
    collector = Collector()
    install_instrumentation(renderer, collector)
    catcher = RenderLogCatcher(collector)
    logging.getLogger("src.features.composite.render.composite_renderer").addHandler(catcher)

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    results_path = RESULTS_DIR / "node_audit.json"

    def dump(results: dict):
        results_path.write_text(json.dumps(results, ensure_ascii=False, indent=1))

    results: dict = {"video": str(VIDEO), "total_frames": total_frames,
                     "warm_evals": WARM_EVALS, "nodes": {}, "skipped": SKIPPED}

    def wait_real_evals(n_target: int, timeout: float = 30.0) -> bool:
        """Block until n_target non-cached evals of the current node landed."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            _heartbeat()
            app.processEvents()
            with collector.lock:
                if len(collector.real) >= n_target:
                    return True
            time.sleep(0.005)
        with collector.lock:
            return len(collector.real) >= n_target

    def seek_and_wait(frame: int, n_target: int, timeout: float = 30.0) -> bool:
        page.comp_seek(frame)
        return wait_real_evals(n_target, timeout)

    # ── baseline: video → output ──
    log("baseline: video_frame → output")
    actions.disconnect_by_dst(out_node.id, "image")
    actions.connect(video_node.id, out_port_name(video_node), out_node.id, "image")
    base_frames = next_frames(WARM_EVALS + 1)
    collector.reset(video_node.id)
    page.comp_seek(base_frames[0])
    wait_real_evals(1, timeout=45.0)
    for f in base_frames[1:]:
        seek_and_wait(f, len(collector.real) + 1)
    with collector.lock:
        baseline_samples = list(collector.real)
        collector.active = False
    results["baseline_video_eval_ms"] = stats(baseline_samples)
    log(f"baseline video_frame eval: {results['baseline_video_eval_ms']}")

    # ── per-node audit ──
    only = os.environ.get("TOPOS_NODE_FILTER")
    wanted = {s.strip() for s in only.split(",")} if only else None
    prev = None  # (node_obj, conns)

    for spec in NODE_SPECS:
        ntype, cat, wiring, params, vary = spec
        if wanted and ntype not in wanted:
            continue
        _heartbeat()
        log(f"── testing {ntype} ({cat}, {wiring})")
        rec: dict = {"category": cat, "wiring": wiring}

        # teardown previous test node
        if prev is not None:
            prev_node, prev_conns = prev
            for (s, sp, d, dp) in prev_conns:
                try:
                    actions.disconnect(s, sp, d, dp)
                except Exception:  # noqa: BLE001
                    pass
            actions.delete_node(prev_node.id)
            prev = None
            for _ in range(10):
                app.processEvents()

        node = actions.add_node(ntype, -500, 300, parameters=params or None)
        if node is None:
            rec["error"] = "add_node returned None"
            results["nodes"][ntype] = rec
            dump(results)
            log(f"   ✗ {ntype}: add_node failed")
            continue

        conns: list[tuple[str, str, str, str]] = []
        try:
            actions.disconnect_by_dst(out_node.id, "image")
            inps = port_names(node)
            if wiring in ("single", "dual") and inps:
                if actions.connect(video_node.id, out_port_name(video_node), node.id, inps[0]):
                    conns.append((video_node.id, out_port_name(video_node), node.id, inps[0]))
                else:
                    rec["error"] = f"connect video→{inps[0]} failed"
            if wiring == "dual" and len(inps) > 1:
                if actions.connect(const_node.id, out_port_name(const_node), node.id, inps[1]):
                    conns.append((const_node.id, out_port_name(const_node), node.id, inps[1]))
            if actions.connect(node.id, out_port_name(node), out_node.id, "image"):
                conns.append((node.id, out_port_name(node), out_node.id, "image"))
            else:
                rec["error"] = (rec.get("error") or "") + " connect→output failed"

            # cold eval on a frame this pipeline has never decoded
            collector.reset(node.id)
            node_frames = next_frames(WARM_EVALS + 1) if wiring != "gen" else None
            base0 = node.parameters.get(vary[0]) if vary else None

            def _apply_vary(k: int):
                key, step = vary or ("font_size", 1)
                try:
                    if step == "toggle":
                        val = bool(base0) if k % 2 == 0 else (not bool(base0))
                    else:
                        val = (float(base0) if base0 is not None else 1.0) + (k + 1) * step
                        if isinstance(base0, int) and isinstance(step, int):
                            val = int(val)
                    actions.set_parameter(node.id, key, val)
                except Exception:  # noqa: BLE001
                    pass
                # graph_changed only drives the minimap; parameter evaluation
                # normally goes through the properties panel. Drive it
                # explicitly or generator varies never re-evaluate.
                page._evaluate_graph_now()

            t_cold0 = time.perf_counter()
            if node_frames:
                page.comp_seek(node_frames[0])
            else:
                _apply_vary(0)
            got_cold = wait_real_evals(1, timeout=45.0)
            rec["cold_s"] = round(time.perf_counter() - t_cold0, 3)
            rec["cold_eval_ms"] = round(collector.real[0], 2) if collector.real else None

            for k in range(WARM_EVALS):
                with collector.lock:
                    collector.capture = (k == WARM_EVALS - 1)
                    target = len(collector.real) + 1
                if node_frames:
                    page.comp_seek(node_frames[k + 1])
                    wait_real_evals(target, timeout=15.0)
                else:
                    _apply_vary(k + 1)
                    wait_real_evals(target, timeout=4.0 if vary else 2.0)
                app.processEvents()

            with collector.lock:
                real_evals = list(collector.real)
                n_cached = collector.cached
                errs = list(collector.errors)
                readback = collector.readback
                collector.active = False
            warm = real_evals[1:] if len(real_evals) > 1 else []
            rec["warm_eval_ms"] = stats(warm)
            rec["cache_hits"] = n_cached
            rec["errors"] = errs[:4]
            rec["readback"] = readback
            last = emissions[-1] if emissions else None
            rec["last_emission"] = (last[1], last[3], last[4]) if last else None
            # Readback is best-effort: the last warm eval can be superseded
            # before presenting, missing the one-shot capture. Node-level
            # eval errors are the reliable failure signal.
            node_errors = [e for e in errs if "no valid output" not in e]
            rec["valid"] = bool(
                len(real_evals) >= 2 and last and last[2] > 0 and last[3] > 0
                and last[4] > 0 and not node_errors
                and (readback is None or readback.get("nonblack", 1.0) > 0.001
                     or ntype == "edge_detect"))
        except Exception as e:  # noqa: BLE001
            rec["error"] = f"{type(e).__name__}: {e}"
            rec["valid"] = False

        prev = (node, conns)
        results["nodes"][ntype] = rec
        dump(results)
        warm_s = rec.get("warm_eval_ms") or {}
        rb = rec.get("readback") or {}
        log(f"   {'✓' if rec.get('valid') else '✗'} {ntype}: "
            f"cold={rec.get('cold_eval_ms')}ms warm_med={warm_s.get('median')}ms "
            f"p90={warm_s.get('p90')} max={warm_s.get('max')}ms "
            f"nonblack={rb.get('nonblack')}"
            + (f" ERR={rec['errors'][:1]}" if rec.get("errors") else ""))

    # ── summary table ──
    log("\n══════════════════ NODE AUDIT SUMMARY ══════════════════")
    rows = []
    for ntype, rec in results["nodes"].items():
        warm = rec.get("warm_eval_ms") or {}
        rows.append((warm.get("median") or 9999, ntype, rec))
    rows.sort(key=lambda r: -r[0])
    log(f"{'node':<18} {'cat':<9} {'ok':<3} {'cold':>7} {'median':>7} {'p90':>7} {'max':>7}  nonblack")
    for _, ntype, rec in rows:
        warm = rec.get("warm_eval_ms") or {}
        rb = rec.get("readback") or {}
        log(f"{ntype:<18} {rec.get('category', ''):<9} "
            f"{'Y' if rec.get('valid') else 'N':<3} "
            f"{str(rec.get('cold_eval_ms')):>7} {str(warm.get('median')):>7} "
            f"{str(warm.get('p90')):>7} {str(warm.get('max')):>7}  {rb.get('nonblack')}")
    log(f"\nbaseline video_frame eval: {results.get('baseline_video_eval_ms')}")
    log(f"results → {results_path}")
    dump(results)

    # skip Qt/GL teardown — headless CGL teardown can segfault after the
    # results are already safely on disk
    os._exit(0)
    return 0


if __name__ == "__main__":
    sys.exit(main())
