"""P1 · 时间线包络编辑（2026-11-19 计划第二阶段）。

验收来自 docs/plans/2026-11-19-film-post-audio-audit-and-roadmap.md：

- ClipItem 新增节点命中、Option/Alt 添加、节点拖动与选择，采用局部
  采样时间和 dB；拖动只更新临时 overlay，松手提交一次
  UpdateClipAudioEditCommand（一条节点操作只占一步撤销）；
- 零点及 0→−12 dB 中点准确；独立编辑模式处理 Delete、Esc 和吸附；
- 覆盖锁轨与超过 1000 节点的帧时间。
"""
from __future__ import annotations

import sys
import time
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from PyQt6.QtCore import QEvent, QPointF, Qt
from PyQt6.QtGui import QKeyEvent

RATE = 48000


class _FakeEvent:
    """mousePressEvent 兼容的最小事件（pos/modifiers/accept）。"""

    def __init__(self, pos, modifiers=Qt.KeyboardModifier.NoModifier):
        self._pos = QPointF(pos)
        self._modifiers = modifiers
        self.accepted = False

    def pos(self):
        return QPointF(self._pos)

    def scenePos(self):
        return QPointF(self._pos)

    def modifiers(self):
        return self._modifiers

    def button(self):
        return Qt.MouseButton.LeftButton

    def accept(self):
        self.accepted = True


def _clip_item(qapp, nodes, *, locked=False, width=480.0, height=72.0, node_editing=True):
    """真实 ClipItem（媒体类型 audio → 包络可用），装载包络节点。"""
    from src.features.edit.ui.timeline_items import ClipItem

    item = ClipItem(
        "c1", "a1", "Clip", 0, 100, width / 100.0, 0,
        source_start=0, source_duration=100, max_source_frames=100,
        track_height=int(height), is_track_locked=locked,
        media_type="audio",
    )
    item.set_volume_envelope(list(nodes), node_editing=node_editing)
    return item


def _recorder(item):
    box = SimpleNamespace(finished=[])
    item.envelope_edit_finished.connect(
        lambda *args: box.finished.append(args))
    return box


class TestGeometryAndHit:
    def test_db_y_round_trip_zero_and_midpoint(self, qapp) -> None:
        nodes = [(0.0, 0.0), (0.5, -12.0)]
        item = _clip_item(qapp, nodes)
        geo = item._envelope_geometry()
        assert geo is not None
        y_zero = item._envelope_db_to_y(0.0, geo)
        y_m12 = item._envelope_db_to_y(-12.0, geo)
        waveform = item._waveform_rect()
        assert y_zero == pytest.approx(waveform.top() + waveform.height() / 6.0)
        assert item._envelope_y_to_db(y_zero, geo) == pytest.approx(0.0, abs=0.01)
        assert item._envelope_y_to_db(y_m12, geo) == pytest.approx(-12.0, abs=0.01)
        # 中点：0 与 −12 的 y 中点反解 ≈ −6 dB。
        mid_db = item._envelope_y_to_db((y_zero + y_m12) / 2, geo)
        assert mid_db == pytest.approx(-6.0, abs=0.05)

    def test_node_hit_radius(self, qapp) -> None:
        nodes = [(0.25, -6.0), (0.6, -3.0)]
        item = _clip_item(qapp, nodes)
        geo = item._envelope_geometry()
        x, y = 0.25 * geo["width"], item._envelope_db_to_y(-6.0, geo)
        assert item._envelope_node_at(QPointF(x, y)) == 0
        assert item._envelope_node_at(QPointF(x + 3, y + 3)) == 0
        assert item._envelope_node_at(QPointF(x + 40, y)) is None

    def test_hit_performance_with_1200_nodes(self, qapp) -> None:
        rng = np.random.default_rng(20261119)
        fractions = sorted(rng.random(1200))
        nodes = [(float(f), float(-6.0 + 6.0 * r)) for f, r
                 in zip(fractions, rng.random(1200))]
        item = _clip_item(qapp, nodes)
        geo = item._envelope_geometry()
        start = time.perf_counter()
        for i in range(200):
            item._envelope_node_at(
                QPointF((i / 200.0) * geo["width"], geo["zero_y"] + 10))
        elapsed = (time.perf_counter() - start) * 1000
        assert elapsed < 250  # 200 次命中 < 250 ms（>1000 节点帧时间）


class TestNodeDragCommitsOnce:
    def test_drag_overlay_only_then_single_commit(self, qapp) -> None:
        nodes = [(0.0, 0.0), (0.5, 0.0), (1.0, 0.0)]
        item = _clip_item(qapp, nodes)
        recorder = _recorder(item)
        geo = item._envelope_geometry()
        node_pos = QPointF(0.5 * geo["width"], item._envelope_db_to_y(0.0, geo))

        assert item._handle_envelope_press(_FakeEvent(node_pos)) is True
        assert item._drag_mode == "envelope_node"
        target_y = item._envelope_db_to_y(-12.0, geo)
        item._update_envelope_node_drag(QPointF(node_pos.x(), target_y), False)
        # 拖动期间：临时 overlay 更新，原始折线不变（模型未提交）。
        assert item._envelope_nodes == [(0.0, 0.0), (0.5, 0.0), (1.0, 0.0)]
        assert item._envelope_drag_nodes[1][1] == pytest.approx(-12.0, abs=0.1)
        item._finish_envelope_drag()
        assert len(recorder.finished) == 1
        clip_id, track_id, emitted = recorder.finished[0]
        assert clip_id == "c1" and track_id == "a1"
        assert emitted[1][1] == pytest.approx(-12.0, abs=0.1)

    def test_zero_snap_during_drag(self, qapp) -> None:
        nodes = [(0.2, -4.0), (0.5, -4.0)]
        item = _clip_item(qapp, nodes)
        geo = item._envelope_geometry()
        pos = QPointF(0.5 * geo["width"],
                      item._envelope_db_to_y(-4.0, geo))
        assert item._handle_envelope_press(_FakeEvent(pos)) is True
        near_zero = item._envelope_db_to_y(-0.3, geo)
        item._update_envelope_node_drag(QPointF(pos.x(), near_zero), False)
        assert item._envelope_drag_nodes[1][1] == 0.0  # 磁吸 0 dB
        # Alt 细调不做磁吸。
        item._update_envelope_node_drag(QPointF(pos.x(), near_zero), True)
        assert item._envelope_drag_nodes[1][1] != 0.0

    def test_time_clamped_between_neighbors(self, qapp) -> None:
        nodes = [(0.2, 0.0), (0.5, 0.0), (0.8, 0.0)]
        item = _clip_item(qapp, nodes)
        geo = item._envelope_geometry()
        pos = QPointF(0.5 * geo["width"], item._envelope_db_to_y(0.0, geo))
        assert item._handle_envelope_press(_FakeEvent(pos)) is True
        item._update_envelope_node_drag(
            QPointF(0.05 * geo["width"], item._envelope_db_to_y(-6.0, geo)),
            False)
        assert item._envelope_drag_nodes[1][0] == pytest.approx(0.2, abs=1e-6)

    def test_cancel_restores_overlay_no_command(self, qapp) -> None:
        nodes = [(0.0, 0.0), (0.5, 0.0), (1.0, 0.0)]
        item = _clip_item(qapp, nodes)
        recorder = _recorder(item)
        geo = item._envelope_geometry()
        pos = QPointF(0.5 * geo["width"], item._envelope_db_to_y(0.0, geo))
        assert item._handle_envelope_press(_FakeEvent(pos)) is True
        item._update_envelope_node_drag(
            QPointF(pos.x(), item._envelope_db_to_y(-12.0, geo)), False)
        item._cancel_envelope_drag()
        assert item._envelope_drag_nodes is None
        assert item._envelope_nodes == [(0.0, 0.0), (0.5, 0.0), (1.0, 0.0)]
        assert recorder.finished == []


class TestEnvelopeClicks:
    def test_left_click_on_empty_envelope_adds_node(self, qapp) -> None:
        nodes = []
        item = _clip_item(qapp, nodes)
        recorder = _recorder(item)
        geo = item._envelope_geometry()
        pos = QPointF(0.5 * geo["width"], item._envelope_db_to_y(0.0, geo))
        assert item._handle_envelope_press(_FakeEvent(pos)) is True
        assert item._drag_mode == "envelope_node"
        assert item._envelope_nodes == []
        assert item._envelope_live_nodes() == [(0.5, 0.0)]
        item._finish_envelope_drag()
        assert len(recorder.finished) == 1
        assert len(recorder.finished[0][2]) == 1

    def test_remove_node_commits_the_remaining_points(self, qapp) -> None:
        nodes = [(0.0, 0.0), (0.5, -6.0), (1.0, 0.0)]
        item = _clip_item(qapp, nodes)
        recorder = _recorder(item)
        item._remove_envelope_point(1)
        assert recorder.finished and len(recorder.finished[0][2]) == 2

    def test_locked_track_not_editable(self, qapp) -> None:
        nodes = [(0.5, -6.0)]
        item = _clip_item(qapp, nodes, locked=True)
        geo = item._envelope_geometry()
        pos = QPointF(0.5 * geo["width"], item._envelope_db_to_y(-6.0, geo))
        assert item._handle_envelope_press(_FakeEvent(pos)) is False
        assert item._envelope_editable() is False


def test_default_envelope_paints_on_a_clip_without_knots_at_narrow_zoom(qapp) -> None:
    from PyQt6.QtGui import QImage, QPainter
    from PyQt6.QtWidgets import QStyleOptionGraphicsItem

    item = _clip_item(qapp, [], width=20.0, node_editing=False)
    image = QImage(20, 72, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(0)
    painter = QPainter(image)
    try:
        item.paint(painter, QStyleOptionGraphicsItem())
    finally:
        painter.end()
    zero_y = round(item._envelope_geometry()["zero_y"])
    assert max(image.pixelColor(10, y).green() for y in range(zero_y - 1, zero_y + 2)) > 175
    assert item._envelope_nodes == []


def test_edit_timeline_shifts_the_whole_envelope_without_editing_knots(qapp) -> None:
    nodes = [(0.25, 3.0), (0.75, -12.0)]
    item = _clip_item(qapp, nodes, node_editing=False)
    node_commits = _recorder(item)
    shifts: list[float] = []
    item.envelope_shift_finished.connect(lambda _clip, _track, db: shifts.append(db))
    geo = item._envelope_geometry()
    point = QPointF(geo["width"] / 2.0, item._envelope_db_to_y(-4.5, geo))

    assert not item._handle_envelope_press(_FakeEvent(point, Qt.KeyboardModifier.AltModifier))
    assert item._handle_envelope_press(_FakeEvent(point))
    item._update_envelope_shift(6 * geo["pixels_per_db"])
    assert item._envelope_live_nodes() == [(0.25, -3.0), (0.75, -18.0)]
    item._finish_envelope_drag()

    assert shifts == [-6.0]
    assert node_commits.finished == []


class TestKeyboard:
    def test_delete_selected_node_emits_without_node(self, qapp) -> None:
        nodes = [(0.0, 0.0), (0.5, -6.0), (1.0, 0.0)]
        item = _clip_item(qapp, nodes)
        recorder = _recorder(item)
        item._envelope_selected = 1
        event = QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Delete,
                          Qt.KeyboardModifier.NoModifier)
        item.keyPressEvent(event)
        assert recorder.finished and len(recorder.finished[0][2]) == 2
        assert item._envelope_selected is None


class TestViewCommit:
    def test_handler_builds_model_nodes_single_command(self, qapp, monkeypatch) -> None:
        """视图处理器：分数 → 模型节点（48k 局部样本），单命令执行。"""
        from src.features.edit.core.timeline_v2 import (
            ClipInstance,
            EditTrack,
            TimelineSequence,
        )
        from src.features.edit.ui.timeline_view import TimelineView

        view = TimelineView()
        clip = ClipInstance("c1", "a1", "a1", 0, 48, 0, 48)
        track = EditTrack("a1", "A1", "audio", 0, clips=(clip,), volume=1.0)
        seq = TimelineSequence(timeline_id="s", name="S",
                               audio_tracks=(track,), fps=24.0)
        view.load_timeline(seq)

        executed = []
        monkeypatch.setattr(view, "_execute_command",
                            lambda cmd: executed.append(cmd))
        monkeypatch.setattr(view, "_is_track_locked", lambda tid: False)

        # 2 s clip：0 dB→−12 dB 中点在 1 s（48000 样本）。
        view._audio_only = True
        view._mixer_envelope_editing = True
        view._on_envelope_edit_finished("c1", "a1", [
            (0.0, 0.0), (0.5, -12.0), (1.0, 0.0)])
        assert len(executed) == 1
        new_edit = executed[0]._after.audio_edit
        nodes = new_edit["envelope"]
        assert [n["gain_db"] for n in nodes] == [0.0, -12.0, 0.0]
        assert nodes[1]["local_sample"] == 48000  # 1 s @ 48k
        # 模型应用第一条命令后，再次提交（拖动后邻近）沿用既有 id。
        seq, _ = executed[0].apply(seq)
        view._timeline = seq  # 真实路径由 _execute_command 更新
        clip = seq.get_clip_by_id("c1")
        old_ids = [n["id"] for n in clip.audio_edit["envelope"]]
        view._on_envelope_edit_finished("c1", "a1", [
            (0.0, 0.0), (0.51, -12.0), (1.0, 0.0)])
        new_nodes = executed[1]._after.audio_edit["envelope"]
        assert [n["id"] for n in new_nodes] == old_ids
        assert new_nodes[1]["local_sample"] == 48960  # 0.51 × 96000
