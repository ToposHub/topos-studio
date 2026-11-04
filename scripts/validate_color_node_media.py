#!/usr/bin/env python3
"""Import real media through the app services and verify source-node previews.

Run in a dedicated application process. The script uses an isolated temporary
configuration and project; it does not open/save an existing user project.
The default validates source thumbnails; --workflow also exercises project-backed
graph edits, grading, history, project IO and native scope presentation.
With --workflow, the driver also waits for evaluated node thumbnails.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import time
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("media", type=Path)
    parser.add_argument("--output", type=Path, default=ROOT / "docs/validation/color_nodes")
    parser.add_argument("--keep-open", action="store_true")
    parser.add_argument("--workflow", action="store_true", help="Exercise authoritative graph edits, grading, undo, project IO and native scopes")
    parser.add_argument("--resources", action="store_true", help="Verify explicit Effects/LUT preview drafts, real Qt drops, layout and floating")
    parser.add_argument("--block-evaluated", action="store_true",
                        help="Negative path: suppress evaluated thumbnail demands; the run must fail non-zero")
    args = parser.parse_args()
    source = args.media.resolve(strict=True)
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    config_dir = Path(tempfile.mkdtemp(prefix="topos-node-media-"))
    config_path = config_dir / "config.json"
    config_path.write_text(json.dumps({"locale": "en_US", "work_mode": "video", "current_page": "Color",
                                       "show_welcome_on_startup": False, "auto_load_last_project": False,
                                       "check_updates_on_startup": False, "auto_save_interval": 0,
                                       # R03：验证脚本固定走显式开发 preview 模式（草稿契约断言）；
                                       # 正式命令适配器由 pytest 宿主矩阵覆盖（R03 验证清单）。
                                       "color_resource_ui_preview_mode": True}))
    os.environ["TOPOS_CONFIG_PATH"] = str(config_path)
    os.environ["TOPOS_PRODUCT_MODE"] = "studio"
    os.environ["TOPOS_TEST_MODE"] = "1"
    os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")

    import src.main as app_main
    from PyQt6.QtCore import QObject, QTimer
    from PyQt6.QtWidgets import QApplication
    from src.app.main_window import MainWindow
    from src.features.color.ui.node_editor_state import NodeKind

    class Driver(QObject):
        def __init__(self, host: Any) -> None:
            super().__init__(host)
            self.host = host
            self.phase = "start"
            self.started = time.monotonic()
            self.asset: Any = None
            self.clip: Any = None
            self.timeline: Any = None
            if args.block_evaluated:
                # 负路径：吞掉全部 demand（实例属性遮蔽方法），等待阶段必须超时失败。
                host.color_node_output_preview.request = lambda *a, **k: None
            self.report: dict[str, Any] = {
                "media": str(source.relative_to(ROOT)) if source.is_relative_to(ROOT) else source.name,
                "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                "validation": "real_app_import_and_source_thumbnail",
                "per_node_rendering": "not_checked",
            }
            self.workflow_modes = iter(("waveform", "parade", "vectorscope", "histogram", "cie", "falsecolor", "split", "quad", "all"))
            self.mode_started = 0.0
            self.timer = QTimer(self)
            self.timer.setInterval(200)
            self.timer.timeout.connect(self.tick)
            self.timer.start()

        def _assert_evaluated_pixels(self, labels: dict) -> dict:
            """像素断言：已评估缩略图与源图可区分（graded 链改变像素）。"""
            import numpy as np
            from PyQt6.QtGui import QImage

            def to_rgb(pixmap: Any) -> np.ndarray:
                image = pixmap.toImage().convertToFormat(QImage.Format.Format_RGB888)
                ptr = image.constBits()
                ptr.setsize(image.sizeInBytes())
                arr = np.frombuffer(ptr, dtype=np.uint8).reshape(
                    image.height(), image.bytesPerLine())
                return arr[:, :image.width() * 3].reshape(
                    image.height(), image.width(), 3).astype(np.int16)

            view = self.host.color_node_panel.view
            source = to_rgb(view._source_thumbnail)
            diffs = {}
            for label, node_id in labels.items():
                thumb = to_rgb(view._thumbnails[node_id])
                # 尺寸归一：缩略图与源可能有亚像素级别的尺寸差，取左上公共区域
                h = min(source.shape[0], thumb.shape[0])
                w = min(source.shape[1], thumb.shape[1])
                diff = float(np.abs(source[:h, :w] - thumb[:h, :w]).mean())
                diffs[label] = round(diff, 3)
                assert diff > 2.0, (
                    f"evaluated thumbnail for {label} is pixel-identical to the "
                    f"source (mean abs diff {diff:.3f}) — evaluation did not apply the grade")
            return diffs

        def finish(self, success: bool, error: str = "") -> None:
            self.timer.stop()
            self.report.update(status="passed" if success else "failed", error=error,
                               elapsed_seconds=round(time.monotonic() - self.started, 2))
            (output / "media_preview_report.json").write_text(json.dumps(self.report, ensure_ascii=False, indent=2) + "\n")
            print(json.dumps(self.report, ensure_ascii=False), flush=True)
            if not args.keep_open:
                app = QApplication.instance()
                app.exit(0 if success else 1)

        def tick(self) -> None:
            try:
                if time.monotonic() - self.started > 90:
                    raise TimeoutError("media import or source thumbnail did not complete in 90 seconds")
                if self.phase == "start":
                    self.host._auto_create_edit_project(name="Color Node Preview Verification")
                    self.host.media_pool_panel._import_files([str(source)])
                    self.phase = "import"
                    return
                if self.phase == "import":
                    project = self.host._current_edit_project
                    self.asset = project.get_asset_by_path(str(source))
                    if self.asset is None:
                        return
                    self.report["asset_id"] = self.asset.asset_id
                    self.report["metadata"] = {"width": self.asset.width, "height": self.asset.height,
                                               "fps": self.asset.fps, "total_frames": self.asset.total_frames}
                    tv = self.host.timeline_panel.timeline_view
                    if not tv._ensure_first_asset_timeline_settings(self.asset):
                        raise RuntimeError("could not create timeline")
                    self.timeline = self.host.edit_page._current_timeline
                    if not tv._add_asset_as_clip(self.asset, self.timeline.video_tracks[0], 0):
                        raise RuntimeError("could not insert timeline clip")
                    self.phase = "timeline"
                    return
                if self.phase == "timeline":
                    self.timeline = self.host.edit_page._current_timeline
                    self.clip = self.timeline.video_tracks[0].clips[0]
                    self.host.open_timeline_clip_in_color(self.timeline.timeline_id, self.clip.clip_id, 0)
                    self.phase = "preview"
                    return
                if self.phase == "preview":
                    panel = self.host.color_node_panel
                    bridge = self.host.color_node_source_preview
                    bridge.refresh()
                    pixmap = panel.view._source_thumbnail
                    if pixmap is None or pixmap.isNull():
                        return
                    import numpy as np
                    image = pixmap.toImage().convertToFormat(pixmap.toImage().Format.Format_RGBA8888)
                    ptr = image.constBits()
                    ptr.setsize(image.sizeInBytes())
                    pixels = np.frombuffer(ptr, dtype=np.uint8).reshape(image.height(), image.bytesPerLine())
                    pixels = pixels[:, :image.width() * 4].reshape(image.height(), image.width(), 4)[..., :3]
                    if float(pixels.std()) < 5.0:
                        raise AssertionError("source thumbnail is blank or flat")
                    if not any(node.kind == NodeKind.CORRECTOR for node in panel.snapshot().nodes.values()):
                        panel.view.apply_edit("add_serial", panel.view.state.add_serial)
                    first = next(node.id for node in panel.snapshot().nodes.values() if node.kind == NodeKind.CORRECTOR)
                    panel.view.cards[first].setSelected(True)
                    before_ids = set(panel.snapshot().nodes)
                    panel.view.apply_edit("add_serial", panel.view.state.add_serial)
                    added = set(panel.snapshot().nodes) - before_ids
                    if len(added) != 1:
                        raise AssertionError("serial UI action did not create one authoritative node")
                    second = added.pop()
                    self.first, self.second = first, second
                    for node_id in (first, second):
                        card = panel.view.cards[node_id]
                        if card._thumbnail.isNull():
                            raise AssertionError("node card did not receive source thumbnail")
                    panel.view.fit_graph()
                    pixmap.save(str(output / "source_thumbnail.png"))
                    panel.grab().save(str(output / "nodes_with_media.png"))
                    self.host.grab().save(str(output / "studio_with_media.png"))
                    self.report.update(
                        clip_id=self.clip.clip_id, timeline_id=self.timeline.timeline_id,
                        current_file_matches_source=self.host.current_file_path == str(source),
                        current_page=self.host.current_page,
                        thumbnail_size=[pixmap.width(), pixmap.height()],
                        thumbnail_rgb_std=round(float(pixels.std()), 3),
                        corrector_thumbnails_present=2,
                        source_tooltip=panel.view.cards[first].toolTip(),
                    )
                    if args.resources:
                        self.phase = "resources"
                    elif args.workflow:
                        self.phase = "workflow"
                    else:
                        self.finish(True)
                    return
                if self.phase == "resources":
                    from PyQt6.QtCore import QPointF, Qt
                    from PyQt6.QtGui import QDragEnterEvent, QDragMoveEvent, QDropEvent
                    from src.features.color.ui.node_resource_mime import resource_mime_data
                    host = self.host
                    presenter = host.color_resource_preview
                    host._migrate_color_resource_dock_layout(force=True)
                    assert host.dock_plugin.widget() is host.color_effects_browser
                    assert host.dock_lut.widget() is host.color_lut_browser
                    assert host.dock_plugin in host.tabifiedDockWidgets(host.dock_presets)
                    assert host.dock_lut in host.tabifiedDockWidgets(host.dock_presets)
                    assert host.dock_layers not in host.tabifiedDockWidgets(host.dock_plugin)
                    view = host.color_node_panel.view
                    sid = host.color_graph_host._bound_stack_id
                    baseline = host._current_edit_project.color_stacks[sid].color_graph.to_dict()
                    plugin_snapshot = [dict(item) for item in host.engine.get_active_layer().plugins]
                    effects = [entry for (kind, _), entry in presenter.catalog.items() if kind == 'effect']
                    if not effects:
                        raise AssertionError('application Effects catalog is empty')
                    # R01：目录含能力受限条目（内建未接线/模型缺失），投放
                    # 留样必须选 available 条目，否则拒绝是预期行为。
                    entry = next((e for e in effects if e.get('parameters')
                                  and e.get('available', True)), None)
                    if entry is None:
                        entry = next((e for e in effects if e.get('available', True)), effects[0])
                    presenter.drop(self.first, {'kind': 'effect', 'resource_id': entry['id']})
                    assert len(presenter._current_draft()['instances']) >= 1
                    host.dock_plugin.show()
                    host.dock_plugin.raise_()
                    self.resource_entry = entry
                    self.resource_baseline = baseline
                    self.resource_plugins = plugin_snapshot
                    self.phase = "resource_settings_capture"
                    return
                if self.phase == "resource_settings_capture":
                    from PyQt6.QtCore import QPointF, Qt
                    from PyQt6.QtGui import QDragEnterEvent, QDragMoveEvent, QDropEvent
                    from src.features.color.ui.node_resource_mime import resource_mime_data
                    host = self.host
                    host.color_effects_browser.grab().save(str(output / 'effects_settings.png'))
                    host.grab().save(str(output / 'resources_studio.png'))
                    host.color_effects_browser.tabs.setCurrentIndex(0)
                    presenter = host.color_resource_preview
                    view = host.color_node_panel.view
                    # Native event routing on the graphics viewport, same MIME as library drags.
                    card = view.cards[self.second]
                    point = view.mapFromScene(card.mapToScene(card.body_rect().center()))
                    mime = resource_mime_data(self.resource_entry)
                    for event in (
                        QDragEnterEvent(point, Qt.DropAction.CopyAction, mime, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier),
                        QDragMoveEvent(point, Qt.DropAction.CopyAction, mime, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier),
                        QDropEvent(QPointF(point), Qt.DropAction.CopyAction, mime, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier),
                    ):
                        QApplication.sendEvent(view.viewport(), event)
                        assert event.isAccepted(), 'native resource drop was rejected'
                    assert presenter._target_id == self.second
                    assert len(presenter._current_draft()['instances']) >= 1
                    luts = [entry for (kind, _), entry in presenter.catalog.items() if kind == 'lut']
                    if not luts:
                        # R02：无真实 LUT 时经正式会话导入 identity fixture
                        # （验证/去重/暂存/注册，同步完成后重投影目录）。
                        from src.shared.color.lut_resource_registry import get_lut_resource_session
                        fixture = config_dir / 'UI Identity.cube'
                        fixture.write_text('TITLE \"UI Identity\"\nLUT_3D_SIZE 2\n0 0 0\n1 0 0\n0 1 0\n1 1 0\n0 0 1\n1 0 1\n0 1 1\n1 1 1\n')
                        results = get_lut_resource_session().import_files([str(fixture)])
                        assert results and results[0].status == 'imported', \
                            f"session LUT import failed: {results[0].error}"
                        presenter._rebuild_catalog()
                        luts = [entry for (kind, _), entry in presenter.catalog.items() if kind == 'lut']
                        self.report['session_test_lut_generated'] = True
                    if luts:
                        presenter.activate(luts[0])
                        presenter.edit('set_lut_strength', {'strength': 0.42})
                        host.color_lut_browser.set_current_lut(luts[0]['id'], 0.42)
                    host.dock_plugin.setFloating(True)
                    host.dock_plugin.resize(360, 650)
                    host.color_effects_browser.tabs.setCurrentIndex(0)
                    host.dock_lut.raise_()
                    self.phase = 'resource_floating_capture'
                    return
                if self.phase == 'resource_floating_capture':
                    from PyQt6.QtCore import Qt
                    host = self.host
                    host.color_effects_browser.grab().save(str(output / 'effects_library_floating.png'))
                    host.color_lut_browser.grab().save(str(output / 'lut_browser_docked.png'))
                    host.dock_lut.setFloating(True)
                    host.dock_lut.resize(360, 650)
                    self.phase = 'resource_lut_floating_capture'
                    return
                if self.phase == 'resource_lut_floating_capture':
                    from PyQt6.QtCore import Qt
                    host = self.host
                    host.color_lut_browser.grab().save(str(output / 'lut_browser.png'))
                    host.dock_lut.setFloating(False)
                    host.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, host.dock_lut)
                    host.tabifyDockWidget(host.dock_presets, host.dock_lut)
                    host.dock_plugin.setFloating(False)
                    host.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, host.dock_plugin)
                    host.tabifyDockWidget(host.dock_presets, host.dock_plugin)
                    sid = host.color_graph_host._bound_stack_id
                    assert host._current_edit_project.color_stacks[sid].color_graph.to_dict() == self.resource_baseline, 'UI draft mutated graph'
                    assert host.engine.get_active_layer().plugins == self.resource_plugins, 'UI draft mutated legacy plugins'
                    self.report['resources'] = {'status': 'passed', 'effects_catalog_count': len([key for key in host.color_resource_preview.catalog if key[0] == 'effect']),
                        'lut_catalog_count': len([key for key in host.color_resource_preview.catalog if key[0] == 'lut']),
                        'left_library_tabs': 'passed', 'nodes_independently_visible': 'passed',
                        'qt_resource_drop': 'passed', 'per_node_draft': 'passed', 'floating': 'passed',
                        'project_and_engine_unchanged': 'passed', 'render_integration': 'preview_mode_only'}
                    if args.workflow:
                        self.phase = 'workflow'
                    else:
                        self.finish(True)
                    return
                if self.phase == "workflow":
                    panel, graph_host = self.host.color_node_panel, self.host.color_graph_host
                    sid = graph_host._bound_stack_id
                    def graph():
                        return self.host._current_edit_project.color_stacks[sid].color_graph
                    assert graph() is not None, "UI edit did not commit to project"
                    baseline = graph().to_dict()
                    # Keyboard path must reach project history after authoritative hydration.
                    panel.view.apply_edit("undo", panel.view.state.undo)
                    assert self.second not in {n.node_id for n in graph().nodes}
                    panel.view.apply_edit("redo", panel.view.state.redo)
                    assert graph().to_dict() == baseline
                    panel.view.cards[self.first].setSelected(True)
                    graph_host.begin_grade_gesture("native-verification")
                    assert graph_host.submit_grade_change(self.first, "exposure", 15.0, "native-verification")
                    assert graph_host.end_grade_gesture()
                    graded = graph().to_dict()
                    assert graded != baseline
                    assert "primary" in panel.view.cards[self.first].node.tool_indicators, "graded node has no tool marker"
                    assert self.host.grading_panel.get_current_params()["exposure"] == 15.0, "selected corrector grade is not reflected in panel"
                    panel.view.apply_edit("add_parallel", lambda: panel.view.state.add_branch(NodeKind.PARALLEL_MIXER))
                    assert len(graph().nodes) == len(graded["nodes"]) + 2
                    panel.view.apply_edit("undo", panel.view.state.undo)
                    assert graph().to_dict() == graded
                    from src.features.edit.core.edit_project_io import EditProjectIO
                    io = EditProjectIO()
                    target = output / "workflow.topos"
                    assert io.save_project(target, self.host._current_edit_project, create_backup=False, use_ai_callback=False)
                    reopened = io.load_project(target)
                    assert reopened is not None
                    assert reopened[0].color_stacks[sid].color_graph.to_dict() == graded
                    # Exercise real connection payloads through the UI/host boundary.
                    edge = next(e for e in panel.snapshot().connections if e.dst_node == self.second)
                    panel.view.apply_edit("disconnect", lambda: panel.view.state.disconnect(edge), edge)
                    assert len(graph().edges) == len(baseline["edges"]) - 1
                    panel.view.apply_edit("connect", lambda: panel.view.state.connect(edge), edge)
                    assert len(graph().edges) == len(baseline["edges"])
                    self.report["workflow"] = {"project_history": "passed", "grade_commit": "passed", "topos_save_reload": "passed", "disconnect_reconnect": "passed", "graph_render_enabled": self.host.engine.color_graph_render_enabled}
                    self.report["per_node_rendering"] = "pending_evaluated_node_thumbnails"
                    self.report["validation"] = "native_graph_workflow"
                    # V01：compound 包装前先确定性等待两张 corrector 卡片的
                    # 已评估缩略图（此前只有 --resources 的阶段耗时让缩略图
                    # 赶在包装前落地，普通路径结构性 0 张）。
                    self.thumb_wait_started = time.monotonic()
                    self.phase = "node_thumbnails_initial"
                    return
                if self.phase == "node_thumbnails_initial":
                    view = self.host.color_node_panel.view
                    have = [nid for nid in (self.first, self.second)
                            if nid in getattr(view, "_thumbnails", {})]
                    if len(have) < 2 and time.monotonic() - self.thumb_wait_started <= 12.0:
                        return
                    assert len(have) == 2, (
                        "corrector cards did not receive evaluated thumbnails "
                        f"within 12 seconds (count={len(have)})")
                    diffs = self._assert_evaluated_pixels({
                        "graded_first": self.first, "passthrough_second": self.second})
                    self.report["initial_node_thumbnails"] = {
                        "node_ids": have,
                        "mean_abs_diff_vs_source": diffs,
                    }
                    self.phase = "baseline"
                    return
                if self.phase == "baseline":
                    # R00 基线工程：2 Corrector + 1 分支已在图内；此处补 Compound、
                    # 遗留插件/LUT/未知参数留样和第二 clip，保存基线工程与图 JSON。
                    from src.features.edit.core.edit_project_io import EditProjectIO
                    panel = self.host.color_node_panel
                    graph_host = self.host.color_graph_host
                    sid = graph_host._bound_stack_id

                    def graph():
                        return self.host._current_edit_project.color_stacks[sid].color_graph

                    snap = panel.snapshot()
                    members = sorted(n.id for n in snap.nodes.values() if n.kind == NodeKind.CORRECTOR)
                    assert members, "baseline graph has no correctors to wrap"
                    panel.view.apply_edit(
                        "make_compound", lambda: panel.view.state.make_compound(members))
                    # service 为 compound 分配权威确定性 id，本地草稿 id 不用于比对。
                    compounds = [n for n in graph().nodes if n.node_type == "compound"]
                    assert compounds, "baseline compound was not committed"
                    compound = compounds[0]
                    inner = [n for n in (compound.compound_children.nodes or ())
                             if n.node_type == "corrector"]
                    assert len(inner) == len(members), "compound children lost members"
                    compound_id = compound.node_id
                    self.compound_id = compound_id
                    (output / "baseline_graph.json").write_text(json.dumps(graph().to_dict(), ensure_ascii=False, indent=2) + "\n")
                    # 遗留写入口留样（R00 审计对象，非正式命令路径）。
                    entries = [e for (kind, _), e in self.host.color_resource_preview.catalog.items()
                               if kind == "effect" and e.get("available", True)]
                    assert entries, "no effect entries for baseline plugin sample"
                    sample = entries[0]
                    layer = self.host.engine.get_active_layer()
                    layer.add_plugin(sample["id"], {p["var_name"]: p.get("default", 0.0) for p in sample.get("parameters", [])})
                    if layer.plugins:
                        layer.plugins[-1]["params"]["__unknown_future_param"] = 0.5
                    fixture = config_dir / "Baseline Identity.cube"
                    fixture.write_text('TITLE "Baseline Identity"\nLUT_3D_SIZE 2\n0 0 0\n1 0 0\n0 1 0\n1 1 0\n0 0 1\n1 0 1\n0 1 1\n1 1 1\n')
                    lut_applied = self.host.engine.apply_lut(str(fixture))
                    self.report["baseline_samples"] = {
                        "plugin_id": sample["id"],
                        "plugin_instance_order": [p.get("instance_id") for p in layer.plugins],
                        "unknown_param": "__unknown_future_param",
                        "lut_applied": bool(lut_applied),
                        "lut_path": str(fixture),
                    }
                    # 第二 clip：同一素材接在首 clip 之后，验证独立色彩栈。
                    first_clip = self.clip
                    second_frame = (first_clip.timeline_start or 0) + (first_clip.timeline_duration or 240)
                    tv = self.host.timeline_panel.timeline_view
                    track = self.host.edit_page._current_timeline.video_tracks[0]
                    assert tv._add_asset_as_clip(self.asset, track, second_frame), "second clip insert failed"
                    timeline = self.host.edit_page._current_timeline
                    second_clip = next(c for c in timeline.video_tracks[0].clips
                                       if c.clip_id != first_clip.clip_id)
                    self.host.open_timeline_clip_in_color(timeline.timeline_id, second_clip.clip_id, 0)
                    sid2 = self.host.color_graph_host._bound_stack_id
                    assert sid2 and sid2 != sid, "second clip did not bind an independent color stack"
                    project = self.host._current_edit_project
                    assert sid in project.color_stacks and sid2 in project.color_stacks
                    assert EditProjectIO().save_project(output / "baseline_project.topos", project, create_backup=False, use_ai_callback=False)
                    self.host.open_timeline_clip_in_color(timeline.timeline_id, first_clip.clip_id, 0)
                    assert self.host.color_graph_host._bound_stack_id == sid, "rebinding first clip lost its stack"
                    self.report["baseline"] = {
                        "compound_node_id": compound_id,
                        "stack_ids": [sid, sid2],
                        "clips": 2,
                        "correctors": 2,
                        "project_saved": "baseline_project.topos",
                    }
                    self.phase = "scope_modes"
                    return
                if self.phase == "scope_modes":
                    if time.monotonic() - self.mode_started < 1.0:
                        return
                    previous = getattr(self, "last_scope_mode", None)
                    if previous:
                        self.host.scope_panel.grab().save(str(output / ("scope_" + previous + ".png")))
                    try:
                        mode = next(self.workflow_modes)
                    except StopIteration:
                        self.phase = "node_thumbnails"
                        self.thumb_wait_started = time.monotonic()
                        return
                    self.host.scope_panel.set_mode(mode)
                    self.host._on_scope_demand_changed()
                    self.last_scope_mode = mode
                    self.mode_started = time.monotonic()
                    return
                if self.phase == "node_thumbnails":
                    # R08/V01：等待 compound 卡片拿到自己的已评估缩略图
                    # （生产 600ms 定时驱动 + 去抖；重映射后投递必须落在
                    # 请求方 compound id 上，而不是内部链尾节点 id）。
                    view = self.host.color_node_panel.view
                    thumbnails = getattr(view, "_thumbnails", {}) or {}
                    compound_ready = self.compound_id in thumbnails
                    if not compound_ready and time.monotonic() - self.thumb_wait_started <= 12.0:
                        return
                    evaluated = len(thumbnails)
                    self.report["per_node_rendering"] = (
                        "evaluated_node_thumbnails" if compound_ready and evaluated >= 2
                        else f"source_thumbnail_only (evaluated={evaluated}, "
                             f"compound_ready={compound_ready})")
                    self.report["node_thumbnail_count"] = evaluated
                    assert compound_ready, (
                        "compound card did not receive its evaluated thumbnail "
                        "within 12 seconds — evaluator leaked the internal "
                        "resolved node id instead of the requested card id")
                    assert evaluated >= 2, (
                        "workflow did not produce evaluated node thumbnails "
                        f"within 12 seconds (count={evaluated})"
                    )
                    diffs = self._assert_evaluated_pixels({"compound": self.compound_id})
                    self.report["compound_thumbnail_diff_vs_source"] = diffs["compound"]
                    from src.features.color.core.scope_diagnostics import scope_diagnostics
                    self.report["scope_diagnostics"] = scope_diagnostics.snapshot()
                    failures = self.report["scope_diagnostics"]["events"]
                    assert not any(failures.get("widget/" + reason, {}).get("count", 0) for reason in ("texture_not_shared", "fbo_incomplete", "blit_failed")), "native scope presentation failed"
                    assert not any(
                        name.startswith("execute/graph_") and event.get("count", 0)
                        for name, event in failures.items()
                    ), "native graph execution failed during scope validation"
                    self.host.grab().save(str(output / "workflow_final.png"))
                    self.host.color_node_panel.grab().save(str(output / "nodes_with_tool_markers.png"))
                    self.finish(True)
                    return
            except (StopIteration, KeyError, RuntimeError, ValueError, TypeError, AttributeError, IndexError, OSError, AssertionError) as exc:
                import traceback
                traceback.print_exc()
                self.finish(False, f"{type(exc).__name__}: {exc}")

    original_init = MainWindow.__init__

    def validation_init(self: Any, *init_args: Any, **kwargs: Any) -> None:
        original_init(self, *init_args, **kwargs)
        self._node_validation_driver = Driver(self)

    MainWindow.__init__ = validation_init
    sys.argv = [sys.argv[0], "--product", "studio", "--no-startup-dialog"]
    return app_main.main()


if __name__ == "__main__":
    raise SystemExit(main())
