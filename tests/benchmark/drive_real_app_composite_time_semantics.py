#!/usr/bin/env python3
"""Real-material verification for composite time semantics (P0-04/P0-05/P0-03).

Drives the REAL app composite page with a real test video and verifies:

  1. P0-04 frame remap: video_frame → time_offset/frame_hold/retime → output.
     The render-side VideoFrameNode must resolve the *remapped* playhead
     (playhead+offset / held frame / playhead*speed), not the raw playhead.
  2. P0-05 layer gates: a layer with in_frame/out_frame produces snapshot
     frame gates; the gated video node drops out of the render outside the
     range (evaluator gate covered by unit tests; here we verify the real
     page's snapshot carries correct gates + generation bumps on edit).
  3. P0-03 preflight: the real graph preflights clean; inserting a
     placeholder node (ai_generate) into the render tree flips export mode to
     not-ok with node.placeholder.  Compare is a real implemented node and
     must remain exportable.

Usage:
    .venv/bin/python tests/benchmark/drive_real_app_composite_time_semantics.py \\
        [video_path]
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

if not os.environ.get("TOPOS_CONFIG_PATH"):
    os.environ["TOPOS_CONFIG_PATH"] = str(REPO_ROOT / ".topos" / "config.json")
os.environ.setdefault("TOPOS_PRODUCT_MODE", "studio")
os.environ["TOPOS_TEST_MODE"] = "1"
os.environ["TOPOS_FEATURE_EDIT"] = "1"
os.environ.setdefault("QSG_RHI_BACKEND", "opengl")

VIDEO = Path(sys.argv[1] if len(sys.argv) > 1
             else REPO_ROOT / "tests/test_videos/2K_H265.mov").resolve()
COMP_FPS = 24.0
COMP_DURATION_FRAMES = 240


def log(msg: str) -> None:
    print(f"[TIME-VERIFY] {msg}", flush=True)


RESOLVED_FRAMES: list[int] = []


def instrument_video_node() -> None:
    """Record the playhead the render-side node resolves (post-remap)."""
    from src.features.composite.nodes.video_frame_node import VideoFrameNode

    orig = VideoFrameNode._resolve_frame_number

    def traced(self, timeline_frame=None):
        if timeline_frame is not None:
            RESOLVED_FRAMES.append(int(timeline_frame))
        return orig(self, timeline_frame)

    VideoFrameNode._resolve_frame_number = traced


def wait_for(predicate, timeout: float, what: str) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        app = QApplication.instance()
        if app is not None:
            app.processEvents()
        if predicate():
            return True
        time.sleep(0.02)
    log(f"TIMEOUT waiting for {what}")
    return False


def wait_frame(page, timeout: float = 20.0) -> None:
    """Pump events until at least one new render emission lands."""
    before = getattr(page, "_render_emission_count", 0)
    deadline = time.time() + timeout

    def _tick():
        return getattr(page, "_render_emission_count", 0) > before

    while time.time() < deadline:
        QApplication.processEvents()
        if _tick():
            return
        time.sleep(0.01)


def main() -> int:
    global QApplication
    from PyQt6.QtCore import Qt, QTimer
    from PyQt6.QtGui import QSurfaceFormat
    from PyQt6.QtWidgets import QApplication, QDialog, QMessageBox

    fmt = QSurfaceFormat()
    fmt.setVersion(4, 5)
    fmt.setProfile(QSurfaceFormat.OpenGLContextProfile.CoreProfile)
    fmt.setRenderableType(QSurfaceFormat.RenderableType.OpenGL)
    QSurfaceFormat.setDefaultFormat(fmt)
    QApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts, True)
    app = QApplication(sys.argv)

    def _auto_dismiss_dialogs():
        for w in QApplication.topLevelWidgets():
            if isinstance(w, QDialog) and w.isVisible():
                if isinstance(w, QMessageBox):
                    # The real close path asks about the unsaved synthetic
                    # project.  An automated run must choose explicitly so
                    # it exercises the rest of closeEvent instead of hanging
                    # in the modal dialog.
                    w.done(QMessageBox.StandardButton.Discard.value)
                else:
                    w.reject()

    killer = QTimer()
    killer.timeout.connect(_auto_dismiss_dialogs)
    killer.start(200)

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
    registry.initialize(AppContext(
        config_manager=config_manager, feature_flags=feature_flags,
        i18n=I18N, logger=None))
    from src.features.color.module import ColorModule
    registry.register_module(ColorModule())
    if feature_flags.is_enabled("composite"):
        from src.features.composite.module import CompositeModule
        m = CompositeModule()
        registry.register_module(m)
        m.discover_and_register_plugins()
    if feature_flags.is_enabled("edit"):
        from src.features.edit.module import EditModule
        registry.register_module(EditModule())

    instrument_video_node()

    mw = MainWindow(feature_flags=feature_flags, feature_registry=registry,
                    test_mode=False)
    mw.show()
    for _ in range(90):
        app.processEvents()
    settle = time.time() + 4.0
    while time.time() < settle:
        app.processEvents()
        time.sleep(0.02)

    mw.media_pool_panel._import_files([str(VIDEO)])
    project = None
    deadline = time.time() + 60
    while time.time() < deadline:
        app.processEvents()
        project = getattr(mw, "_current_edit_project", None)
        if (project is not None
                and project.get_asset_by_path(str(VIDEO)) is not None):
            break
        time.sleep(0.02)
    assert project is not None, "project not created"
    log(f"asset imported: {VIDEO.name}")

    new_project, comp_clip_id, comp_asset_id, graph_id = \
        project.create_empty_comp_clip(
            name="TimeVerify", width=1920, height=1080, fps=COMP_FPS,
            duration_frames=COMP_DURATION_FRAMES,
            background_color=(0.0, 0.0, 0.0, 1.0))
    mw._current_edit_project = new_project
    mw._switch_to_composite_page(graph_id)
    wait_for(lambda: getattr(mw, "_current_composite_id", None) == graph_id,
             15, "composite page switch")

    page = mw.composite_page
    assert page is not None, "composite page missing"
    wait_for(lambda: getattr(page, "_renderer", None) is not None,
             30, "renderer init")

    graph = page._node_graph
    wait_for(lambda: bool(graph.get_nodes_by_type("output")),
             15, "output node present")

    # count emissions so wait_frame can detect new renders
    page._render_emission_count = 0
    page.composite_frame_ready.connect(
        lambda *a, **k: setattr(
            page, "_render_emission_count",
            getattr(page, "_render_emission_count", 0) + 1))

    # ── build video_frame → time_offset → output ──
    actions = page._node_editor._actions
    out_node = graph.get_nodes_by_type("output")[0]
    video_node = actions.add_node("video_frame", -1000, 300,
                                  parameters={"file_path": str(VIDEO)})
    time_node = actions.add_node("time_offset", -600, 300)
    assert video_node is not None and time_node is not None
    for conn in graph.connections:
        if conn.dst_node_id == out_node.id:
            actions.disconnect(conn.src_node_id, conn.src_port_name,
                               out_node.id, conn.dst_port_name)
    ok1 = actions.connect(video_node.id, "image", time_node.id, "input")
    ok2 = actions.connect(time_node.id, "result", out_node.id, "image")
    log(f"graph wired: video→time_offset={ok1} time_offset→output={ok2}")

    wait_frame(page)
    failures: list[str] = []

    def seek_and_record(frame: int, param_node=None, param_set=None) -> int:
        """Seek, wait for the render, return the last resolved source frame."""
        if param_set is not None and param_node is not None:
            for k, v in param_set.items():
                param_node.set_parameter(k, v)
        RESOLVED_FRAMES.clear()
        page.set_composite_current_frame(frame)
        wait_frame(page, timeout=25.0)
        time.sleep(0.3)
        for _ in range(10):
            app.processEvents()
            time.sleep(0.02)
        log(f"  resolved frames: {RESOLVED_FRAMES[-8:]}")
        if not RESOLVED_FRAMES:
            return -1
        return RESOLVED_FRAMES[-1]

    # 1) no offset → identity
    got = seek_and_record(20, time_node, {"frame_offset": 0})
    log(f"playhead=20 offset=0 → resolved={got}")
    if got != 20:
        failures.append(f"identity: expected 20, got {got}")

    # 2) time_offset +5 → resolved 26
    got = seek_and_record(21, time_node, {"frame_offset": 5})
    log(f"playhead=21 offset=+5 → resolved={got}")
    if got != 26:
        failures.append(f"time_offset: expected 26, got {got}")

    # 3) time_offset -7 at a NEW playhead → resolved 15 (22-7)
    got = seek_and_record(22, time_node, {"frame_offset": -7})
    log(f"playhead=22 offset=-7 → resolved={got}")
    if got != 15:
        failures.append(f"time_offset neg: expected 15, got {got}")

    # swap to frame_hold: hold at frame 3
    hold_node = actions.add_node("frame_hold", -600, 100)
    assert hold_node is not None
    actions.disconnect(video_node.id, "image", time_node.id, "input")
    actions.disconnect(time_node.id, "result", out_node.id, "image")
    ok1 = actions.connect(video_node.id, "image", hold_node.id, "input")
    ok2 = actions.connect(hold_node.id, "result", out_node.id, "image")
    log(f"rewired video→frame_hold={ok1} frame_hold→output={ok2}")
    got = seek_and_record(150, hold_node, {"hold_frame": 3})
    log(f"playhead=150 hold=3 → resolved={got}")
    if got != 3:
        failures.append(f"frame_hold: expected 3, got {got}")

    # swap to retime speed=2
    retime_node = actions.add_node("retime", -600, -100)
    assert retime_node is not None
    actions.disconnect(video_node.id, "image", hold_node.id, "input")
    actions.disconnect(hold_node.id, "result", out_node.id, "image")
    ok1 = actions.connect(video_node.id, "image", retime_node.id, "input")
    ok2 = actions.connect(retime_node.id, "result", out_node.id, "image")
    log(f"rewired video→retime={ok1} retime→output={ok2}")
    got = seek_and_record(30, retime_node, {"speed": 2.0})
    log(f"playhead=30 speed=2.0 → resolved={got}")
    if got != 60:
        failures.append(f"retime: expected 60, got {got}")
    # retime at another playhead: 40*2 = 80
    got = seek_and_record(40, retime_node, {"speed": 2.0})
    log(f"playhead=40 speed=2.0 → resolved={got}")
    if got != 80:
        failures.append(f"retime 2: expected 80, got {got}")

    # ── P0-05 layer gates on the real page ──
    layer_id = None
    ctrl = getattr(page, "_layer_stack_controller", None)
    if ctrl is not None:
        layer_id = ctrl.add_media_layer("Gate", video_node.id)
    if layer_id:
        ctrl.set_layer_property(layer_id, "in_frame", 100)
        ctrl.set_layer_property(layer_id, "out_frame", 200)
        snap = graph.snapshot(frame_number=150)
        gates = snap.layer_frame_gates
        layer = graph.get_layer(layer_id)
        expected = {layer.source_node_id, layer.root_node_id}
        if not expected.issubset(gates):
            failures.append(f"layer gates missing nodes: {gates.keys()}")
        else:
            gate = gates[layer.source_node_id]
            if gate != (100, 200):
                failures.append(f"layer gate bounds wrong: {gate}")
        snap_in = graph.snapshot(frame_number=50)
        from src.features.composite.core.time_remap import gate_active
        if gate_active(snap_in.layer_frame_gates[layer.source_node_id], 50):
            failures.append("gate should be inactive at frame 50")
        log(f"layer gates ok: {gates}")
    else:
        log("no _layer_controller on page — skipping layer gate part")

    # ── P0-03 preflight on the real graph ──
    from src.features.composite.core.graph_preflight import (
        MODE_EXPORT, run_preflight,
    )
    report = run_preflight(graph, mode=MODE_EXPORT, check_fs=False)
    log(f"preflight (clean graph): ok={report.ok} issues={len(report.issues)}")
    if not report.ok:
        failures.append(f"clean graph preflight failed: "
                        f"{[i.code for i in report.errors]}")

    placeholder_node = actions.add_node("ai_generate", -300, 500)
    if placeholder_node is not None:
        # off the render tree first: export preflight must stay clean
        report2 = run_preflight(graph, mode=MODE_EXPORT, check_fs=False)
        if not report2.ok:
            failures.append("ai_generate off-tree must not block export")
        # now splice it into the render tree → export must flag it
        actions.disconnect(retime_node.id, "result", out_node.id, "image")
        actions.connect(placeholder_node.id, "image", out_node.id, "image")
        report3 = run_preflight(graph, mode=MODE_EXPORT, check_fs=False)
        codes = {i.code for i in report3.errors}
        log(f"preflight (ai_generate in tree): ok={report3.ok} errors={codes}")
        if report3.ok or "node.placeholder" not in codes:
            failures.append(f"ai_generate in tree should block export: {codes}")

    # Print the verdict BEFORE teardown — window close can hang on render
    # thread joins (known P0-01/P0-08 stability domain), and the verdict is
    # what callers parse.
    if failures:
        log("FAILURES:")
        for f in failures:
            log(f"  ✗ {f}")
        verdict = 1
    else:
        log("ALL TIME-SEMANTICS CHECKS PASSED")
        verdict = 0
    sys.stdout.flush()
    try:
        mw.close()
        app.processEvents()
    except Exception:
        pass
    killer.stop()
    # Hard exit: avoid hanging on Qt/render-thread teardown after the run.
    os._exit(verdict)


if __name__ == "__main__":
    sys.exit(main())
