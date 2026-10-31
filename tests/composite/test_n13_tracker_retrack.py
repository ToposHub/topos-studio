"""N13 · Tracker 真实重跟引擎 + 异步面板 + Undo 接线测试。

审计句：工厂没有 retrack_fn/on_changed；手工修正直接改 node 无 Undo/
revision/持久化刷新链；同步重跟移可取消 worker。
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

from PyQt6.QtWidgets import QApplication  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

pytestmark = pytest.mark.composite_core

from src.features.composite.core.tracker_corrections import (  # noqa: E402
    RetrackSegment, retrack_plan_async, retrack_translation_segment)
from src.features.composite.nodes.tracker_node import TrackerNode  # noqa: E402
from src.features.color.ui.tracker_correction_panel import (  # noqa: E402
    TrackerCorrectionPanel)


def _shifted_frames(shifts, size=(160, 200)):
    """合成帧序列：同一纹理图案按 shifts 平移（BGR uint8）。"""
    rng = np.random.default_rng(7)
    texture = rng.integers(0, 255, size=(60, 80, 3), dtype=np.uint8)
    frames = []
    for dx, dy in shifts:
        canvas = np.zeros((size[0], size[1], 3), dtype=np.uint8)
        canvas[:, :, 1] = 30  # 背景可分
        x, y = 60 + int(dx), 50 + int(dy)
        canvas[y:y + 60, x:x + 80] = texture
        frames.append(canvas)
    return frames


class TestRetrackEngine:
    def test_recovers_known_shifts(self):
        shifts = [(0, 0), (6, 0), (12, 4), (18, 4)]
        frames = _shifted_frames(shifts)
        segment = RetrackSegment(
            start_frame=1, end_frame=3, anchor_frame=0,
            anchor_transform={"tx": 0.0, "ty": 0.0})
        results, error = retrack_translation_segment(
            lambda f: frames[f], segment, (60, 50, 80, 60))
        assert error == ""
        for i, (dx, dy) in enumerate(shifts[1:], start=1):
            assert results[str(i)]["tx"] == pytest.approx(
                float(dx), abs=1.5)
            assert results[str(i)]["ty"] == pytest.approx(
                float(dy), abs=1.5)
            assert results[str(i)]["status"] == "valid"

    def test_missing_frame_returns_partial_with_error(self):
        frames = _shifted_frames([(0, 0), (5, 0), (10, 0)])
        segment = RetrackSegment(
            start_frame=1, end_frame=2, anchor_frame=0,
            anchor_transform={"tx": 0.0, "ty": 0.0})

        def getter(f):
            return frames[f] if f < 2 else None

        results, error = retrack_translation_segment(
            getter, segment, (60, 50, 80, 60))
        assert "frame 2 unavailable" in error
        assert "1" in results and "2" not in results  # 部分结果不冒充

    def test_cooperative_cancel(self):
        frames = _shifted_frames([(0, 0)] * 8)
        segment = RetrackSegment(
            start_frame=1, end_frame=6, anchor_frame=0,
            anchor_transform={})
        results, error = retrack_translation_segment(
            lambda f: frames[f], segment, (60, 50, 80, 60),
            is_cancelled=lambda: True)
        assert error == "cancelled"
        assert results == {}

    def test_anchor_missing_fails_closed(self):
        segment = RetrackSegment(1, 2, 0, {})
        results, error = retrack_translation_segment(
            lambda f: None, segment, (0, 0, 40, 40))
        assert results == {} and "anchor" in error

    def test_batch_plan_stops_on_first_failed_segment(self):
        node = TrackerNode("t1")
        node.set_track_data({
            "version": 1, "transforms": {
                "0": {"tx": 0.0, "ty": 0.0, "status": "valid"},
                "1": {"tx": 0.0, "ty": 0.0, "status": "failed"},
                "2": {"tx": 0.0, "ty": 0.0, "status": "failed"},
            }})
        frames = {0: _shifted_frames([(0, 0)])[0],
                  1: _shifted_frames([(4, 0)])[0]}  # 帧 2 缺失
        merged, error = retrack_plan_async(
            lambda f: frames.get(f), node.get_track_data(), (60, 50, 80, 60))
        assert "frame 2 unavailable" in error
        assert "1" in merged and "2" not in merged


class TestPanelUndoAndAsync:
    def _node(self):
        node = TrackerNode("t1")
        node.set_track_data({
            "version": 1, "transforms": {
                "0": {"tx": 0.0, "ty": 0.0, "status": "valid"},
                "1": {"tx": 1.0, "ty": 0.0, "status": "failed"},
            }})
        return node

    def test_manual_fix_registers_undo(self, qtbot, monkeypatch):
        from PyQt6.QtWidgets import QInputDialog
        node = self._node()
        registered = []

        def sink(desc, undo_fn, redo_fn):
            registered.append((desc, undo_fn, redo_fn))

        panel = TrackerCorrectionPanel(node, undo_sink=sink)
        qtbot.addWidget(panel)
        answers = iter([(1, True), (7.0, True), (2.0, True)])
        monkeypatch.setattr(QInputDialog, "getInt",
                            staticmethod(lambda *a, **k: next(answers)))
        monkeypatch.setattr(QInputDialog, "getDouble",
                            staticmethod(lambda *a, **k: next(answers)))
        panel._on_manual_fix()
        assert len(registered) == 1
        desc, undo_fn, redo_fn = registered[0]
        undo_fn()  # 撤销 → 修正消失（失败段回归）
        assert node.get_manual_corrections() == {}
        assert len(node.plan_incremental_retrack().segments) == 1
        redo_fn()
        assert node.get_manual_corrections()["1"] == {"tx": 7.0, "ty": 2.0}

    def test_retrack_merge_registers_undo(self, qtbot):
        node = self._node()
        registered = []

        def sink(desc, undo_fn, redo_fn):
            registered.append((desc, undo_fn, redo_fn))

        def engine(segment, is_cancelled=None):
            return {1: {"tx": 9.0, "ty": 0.0}}

        panel = TrackerCorrectionPanel(
            node, retrack_fn=engine, undo_sink=sink)
        qtbot.addWidget(panel)
        panel._segments.setCurrentRow(0)
        panel._on_retrack()
        assert panel._worker.wait(5000)
        for _ in range(10):
            QApplication.processEvents()
        assert node.get_track_data()["transforms"]["1"]["tx"] == \
            pytest.approx(9.0)
        assert len(registered) == 1
        _, undo_fn, _redo = registered[0]
        undo_fn()
        assert node.get_track_data()["transforms"]["1"]["status"] == "failed"

    def test_no_undo_sink_still_works(self, qtbot, monkeypatch):
        from PyQt6.QtWidgets import QInputDialog
        node = self._node()
        panel = TrackerCorrectionPanel(node)  # 无 undo_sink
        qtbot.addWidget(panel)
        answers = iter([(1, True), (3.0, True), (4.0, True)])
        monkeypatch.setattr(QInputDialog, "getInt",
                            staticmethod(lambda *a, **k: next(answers)))
        monkeypatch.setattr(QInputDialog, "getDouble",
                            staticmethod(lambda *a, **k: next(answers)))
        panel._on_manual_fix()  # 不注册 undo 但功能可用
        assert node.get_manual_corrections()["1"]["tx"] == 3.0


class TestControllerWiring:
    def _host(self):
        import inspect
        from src.app.controllers.menu_toolbar_controller import (
            MenuToolbarMixin,
        )

        class _Host:
            pass

        host = _Host()
        for name, fn in inspect.getmembers(
                MenuToolbarMixin, predicate=inspect.isfunction):
            if name in ("_composite_host", "_tracker_undo_sink",
                        "_tracker_view_refresh", "_build_tracker_retrack_fn"):
                setattr(host, name, fn.__get__(host))
        return host

    def test_retrack_fn_none_without_frame_getter(self):
        host = self._host()
        host.composite_controller = type("C", (), {})()  # 无逐帧源 API
        assert host._build_tracker_retrack_fn(
            type("N", (), {"node_id": "n", "parameters": {}})()) is None

    def test_retrack_fn_real_engine_with_frame_getter(self):
        host = self._host()
        frames = _shifted_frames([(0, 0), (5, 0)])

        class _Comp:
            def get_node_input_frame(self, node_id, frame):
                return frames[frame]

        host.composite_controller = _Comp()
        fn = host._build_tracker_retrack_fn(
            type("N", (), {"node_id": "n", "parameters": {
                "center_x": 0.5, "center_y": 0.5,
                "size_x": 0.4, "size_y": 0.375}})())
        assert fn is not None
        seg = RetrackSegment(1, 1, 0, {"tx": 0.0, "ty": 0.0})
        results, error = fn(seg)
        assert error == ""
        assert results["1"]["tx"] == pytest.approx(5.0, abs=1.5)

    def test_undo_sink_pushes_to_page_stack(self):
        host = self._host()
        pushed = []

        class _Stack:
            def push(self, action):
                pushed.append(action)

        host.composite_controller = type("C", (), {"_undo_stack": _Stack()})()
        sink = host._tracker_undo_sink()
        assert sink is not None
        sink("修正", lambda: None, lambda: None)
        assert len(pushed) == 1
        assert pushed[0].description == "修正"

    def test_undo_sink_none_without_stack(self):
        host = self._host()
        host.composite_controller = type("C", (), {})()
        assert host._tracker_undo_sink() is None
