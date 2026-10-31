"""F2.3 · 真双画面 Trim Viewer 与 JKL 动态修剪。

三层覆盖：
- ``TrimDualFrameProvider``：latest-wins 合并请求、generation 随帧投递、
  stop/close_decoders 生命周期（注入假解码器，无真实媒体依赖）。
- ``TrimMonitorDialog``：双帧画面（占位/更新/单边 trim 清右半）。
- ``TimelineView`` 接线：roll 会话请求左右片真实保留边界帧（与
  with_*_edge_source_values 同源）、hide/切源 bump generation、ROLLING 态
  J/K/L/Enter 键盘步进与单步提交、Esc 取消零 revision。
"""

from __future__ import annotations

import time
from typing import Any, List, Optional

import numpy as np
import pytest
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QKeyEvent
from PyQt6.QtWidgets import QApplication

from src.features.edit.core.timeline_v2 import (
    ClipInstance,
    EditTrack,
    TimelineSequence,
)
from src.features.edit.ui.timeline_interaction import InteractionState

pytestmark = pytest.mark.edit_core


def _clip(clip_id: str, track_id: str, start: int, duration: int,
          **kw) -> ClipInstance:
    return ClipInstance(
        clip_id=clip_id,
        asset_id=f"asset-{clip_id}",
        track_id=track_id,
        timeline_start=start,
        timeline_duration=duration,
        source_start=kw.pop("source_start", 0),
        source_duration=kw.pop("source_duration", 500),
        **kw,
    )


def _roll_timeline() -> TimelineSequence:
    timeline = TimelineSequence(timeline_id="t1", name="T", fps=24.0)
    timeline = timeline.with_track_added(EditTrack(
        track_id="v1", name="V1", track_type="video", index=0))
    timeline = timeline.with_clip_added("v1", _clip("a", "v1", 0, 100))
    timeline = timeline.with_clip_added("v1", _clip("b", "v1", 100, 100))
    return timeline


def _wait_until(condition, timeout: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        QApplication.processEvents()
        if condition():
            return True
        time.sleep(0.01)
    QApplication.processEvents()
    return condition()


# ── Provider（假解码器注入） ────────────────────────────────────────────


class _FakeDecoder:
    def __init__(self, path: str) -> None:
        self.path = path
        self.reads: List[int] = []
        self.closed = False

    def read_frame(self, frame: int):
        self.reads.append(int(frame))
        time.sleep(0.08)  # 模拟忙碌解码：请求在解码期间到达才会被合并
        return np.full((4, 6, 3), 128, dtype=np.uint8)

    def close(self) -> None:
        self.closed = True


def _make_provider():
    from src.features.edit.ui.trim_frame_provider import TrimDualFrameProvider

    decoders: dict[str, _FakeDecoder] = {}

    def factory(path: str) -> _FakeDecoder:
        decoders[path] = _FakeDecoder(path)
        return decoders[path]

    provider = TrimDualFrameProvider(decoder_factory=factory)
    return provider, decoders


class TestTrimDualFrameProvider:
    def test_latest_wins_merges_requests(self, qapp) -> None:
        """连发 3 个请求只解码最后一条（快速拖动不堆积）。"""
        provider, decoders = _make_provider()
        provider.start()
        try:
            for frame in (10, 20, 30):
                provider.request("/x.mp4", frame, "/x.mp4", frame + 1, 1)
            assert _wait_until(
                lambda: decoders["/x.mp4"].reads[-1:] == [31])
            # 三连发在忙碌期间到达：只有最后一条（left 30 / right 31）被解码
            assert decoders["/x.mp4"].reads == [30, 31], decoders["/x.mp4"].reads
        finally:
            provider.stop()

    def test_generation_tagged_on_delivery(self, qapp) -> None:
        provider, decoders = _make_provider()
        delivered: List[tuple] = []
        provider.signals.frame_ready.connect(
            lambda side, frame, img, gen: delivered.append((side, frame, gen)))
        provider.start()
        try:
            provider.request("/x.mp4", 5, "/y.mp4", 7, 42)
            assert _wait_until(lambda: len(delivered) >= 2)
            assert ("left", 5, 42) in delivered
            assert ("right", 7, 42) in delivered
        finally:
            provider.stop()

    def test_stop_ends_worker_and_closes_decoders(self, qapp) -> None:
        provider, decoders = _make_provider()
        provider.start()
        provider.request("/x.mp4", 1, None, None, 1)
        assert _wait_until(lambda: "/x.mp4" in decoders)
        provider.stop()
        assert _wait_until(lambda: decoders["/x.mp4"].closed, timeout=3.0)

    def test_close_decoders_disposes_cached(self, qapp) -> None:
        provider, decoders = _make_provider()
        provider.start()
        provider.request("/x.mp4", 1, "/y.mp4", 2, 1)
        assert _wait_until(lambda: len(decoders) == 2)
        provider.close_decoders()
        assert _wait_until(
            lambda: all(d.closed for d in decoders.values()))
        provider.stop()


# ── TrimMonitorDialog 双帧画面 ─────────────────────────────────────────


class TestTrimMonitorDualFrames:
    def _monitor(self, qapp):
        from src.features.edit.ui.trim_numeric_dialog import TrimMonitorDialog

        monitor = TrimMonitorDialog()
        return monitor

    def test_pending_shows_placeholder(self, qapp) -> None:
        monitor = self._monitor(qapp)
        monitor.set_frames_pending()
        assert monitor._frame_a.text() == "…"
        assert monitor._frame_b.text() == "…"

    def test_update_frame_sets_pixmap_per_side(self, qapp) -> None:
        from PyQt6.QtGui import QImage

        monitor = self._monitor(qapp)
        monitor.resize(640, 480)
        image = QImage(60, 40, QImage.Format.Format_RGB888)
        image.fill(0xFF00FF00)
        monitor.update_frame("left", image)
        assert monitor._frame_a.pixmap() is not None
        assert not monitor._frame_a.pixmap().isNull()
        assert monitor._frame_b.pixmap() is None or \
            monitor._frame_b.pixmap().isNull()
        monitor.update_frame("right", image)
        assert not monitor._frame_b.pixmap().isNull()

    def test_update_trim_clears_right_panel(self, qapp) -> None:
        from PyQt6.QtGui import QImage

        monitor = self._monitor(qapp)
        image = QImage(8, 8, QImage.Format.Format_RGB888)
        monitor.update_frame("right", image)
        assert not monitor._frame_b.pixmap().isNull()
        monitor.update_trim("00:00:04:00", 5)
        assert monitor._frame_b.pixmap() is None or \
            monitor._frame_b.pixmap().isNull()
        assert monitor._label_a.text() == "00:00:04:00"


# ── TimelineView 接线（假 provider 注入） ──────────────────────────────


class _FakeProviderHandle:
    """记录请求的假 provider（signals 用真实 TrimDualFrameSignals）。"""

    def __init__(self) -> None:
        from src.features.edit.ui.trim_frame_provider import TrimDualFrameSignals

        self.signals = TrimDualFrameSignals()
        self.requests: List[tuple] = []
        self.invalidated = 0
        self.closed_decoders = 0
        self.stopped = False

    def request(self, left_path, left_frame, right_path, right_frame,
                generation) -> None:
        self.requests.append(
            (left_path, left_frame, right_path, right_frame, generation))

    def invalidate(self) -> None:
        self.invalidated += 1

    def close_decoders(self) -> None:
        self.closed_decoders += 1

    def stop(self) -> None:
        self.stopped = True


def _roll_view(qapp, qtbot):
    from src.features.edit.ui.timeline_view import TimelineView

    view = TimelineView()
    qtbot.addWidget(view)
    view.load_timeline(_roll_timeline())
    fake = _FakeProviderHandle()
    view._trim_frame_provider_instance = fake
    view._trim_frame_generation = 1
    return view, fake


class TestRollDualFrameWiring:
    def test_roll_requests_real_boundary_frames(self, qapp, qtbot) -> None:
        """左右帧与拖拽预览的权威源窗同源（保留边界帧）。"""
        view, fake = _roll_view(qapp, qtbot)
        view._on_roll_trim_started("a", "b", 100)
        view._on_roll_trim_dragged(10)

        assert len(fake.requests) >= 2  # started + dragged
        left_path, left_frame, right_path, right_frame, gen = fake.requests[-1]
        assert gen == 1
        # 无工程（素材缺失）→ 路径为 None，帧值仍按权威源窗计算
        from src.features.edit.core.timeline_v2 import ClipInstance  # noqa: F401
        assert left_frame == 109  # 新左片源窗末帧（source_start 0 + 110 - 1）
        assert right_frame == 10  # 右片新源窗首帧（0 + delta）
        assert left_path is None and right_path is None

    def test_hide_monitor_bumps_generation(self, qapp, qtbot) -> None:
        view, fake = _roll_view(qapp, qtbot)
        view._on_roll_trim_started("a", "b", 100)
        gen_before = view._trim_frame_generation
        view._hide_trim_monitor()
        assert view._trim_frame_generation == gen_before + 1
        assert fake.invalidated == 1

    def test_stale_generation_frame_not_displayed(self, qapp, qtbot) -> None:
        view, fake = _roll_view(qapp, qtbot)
        view._on_roll_trim_started("a", "b", 100)
        view._hide_trim_monitor()  # bump generation

        from PyQt6.QtGui import QImage

        stale = QImage(8, 8, QImage.Format.Format_RGB888)
        view._on_trim_frame_ready("left", 99, stale, 1)  # 旧 generation
        monitor = view._trim_monitor_dialog
        assert monitor._frame_a.pixmap() is None or \
            monitor._frame_a.pixmap().isNull()

    def test_load_timeline_closes_decoders(self, qapp, qtbot) -> None:
        view, fake = _roll_view(qapp, qtbot)
        view.load_timeline(_roll_timeline())
        assert fake.closed_decoders == 1
        assert fake.invalidated == 1


class TestRollJklKeyboard:
    def _press(self, view, key, modifiers=Qt.KeyboardModifier.NoModifier):
        from PyQt6.QtCore import QEvent

        event = QKeyEvent(
            QEvent.Type.KeyPress, key, modifiers)
        view.keyPressEvent(event)
        return event

    def test_jkl_steps_cut_and_enter_commits_once(self, qapp, qtbot) -> None:
        view, fake = _roll_view(qapp, qtbot)
        view._on_roll_trim_started("a", "b", 100)

        # L +1、L +1、Shift+L +10、J −1 → delta = 11
        self._press(view, Qt.Key.Key_L)
        self._press(view, Qt.Key.Key_L)
        self._press(view, Qt.Key.Key_L, Qt.KeyboardModifier.ShiftModifier)
        self._press(view, Qt.Key.Key_J)
        assert view._roll_preview_state["delta"] == 11

        # K 暂停不改变 delta
        self._press(view, Qt.Key.Key_K)
        assert view._roll_preview_state["delta"] == 11

        # Enter 提交：单步 Undo，双片按 delta 落库
        self._press(view, Qt.Key.Key_Return)
        assert view._timeline.get_clip_by_id("a").timeline_duration == 111
        assert view._timeline.get_clip_by_id("b").timeline_start == 111
        assert view._timeline.get_clip_by_id("b").timeline_duration == 89
        assert len(view._command_stack._undo_stack) == 1
        assert view._interaction.is_idle

    def test_esc_cancels_without_revision(self, qapp, qtbot) -> None:
        view, fake = _roll_view(qapp, qtbot)
        view._on_roll_trim_started("a", "b", 100)
        self._press(view, Qt.Key.Key_L)
        assert view._roll_preview_state["delta"] == 1

        from PyQt6.QtCore import QEvent

        view.keyPressEvent(QKeyEvent(
            QEvent.Type.KeyPress, Qt.Key.Key_Escape,
            Qt.KeyboardModifier.NoModifier))
        assert view._interaction.is_idle
        assert len(view._command_stack._undo_stack) == 0
        assert view._timeline.get_clip_by_id("a").timeline_duration == 100

    def test_jkl_outside_session_not_consumed(self, qapp, qtbot) -> None:
        """会话外 J/K/L 不被本分支消费（透传输层 JKL）。"""
        view, fake = _roll_view(qapp, qtbot)
        event = self._press(view, Qt.Key.Key_L)
        assert not event.isAccepted() or view._roll_preview_state is not None
        assert view._roll_preview_state is None


class TestTrimFinishSignal:
    def test_hide_emits_finish_with_last_cut_frame(self, qapp, qtbot) -> None:
        """F2.3：会话结束发 trim_frame_preview_finished（scrub 管线收尾）。"""
        view, fake = _roll_view(qapp, qtbot)
        finished: List[int] = []
        view.trim_frame_preview_finished.connect(finished.append)
        view._on_roll_trim_started("a", "b", 100)
        view._on_roll_trim_dragged(10)
        view._hide_trim_monitor()
        assert finished == [110]  # cut 100 + delta 10
