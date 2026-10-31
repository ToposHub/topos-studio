"""P1-01 多机位角度面板（网格监看 + 波形同步入口）UI 测试。

- set_group 构建：网格 tile / 参考角度下拉 / 同步按钮可用性 / 高亮
- request_angle_frames 去抖合并 → ThumbnailProvider 取帧 → 回填 pixmap
- tile 点击 / 同步按钮 → 信号（面板只上报意图）
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from src.features.edit.core.multicam import (
    build_multicam_group_from_assets,
    group_with_sync_offsets,
)


class _FakeProviderSignal:
    """记录请求并允许手动回放 frame_thumbnail_ready。"""

    def __init__(self):
        from PyQt6.QtCore import QObject, pyqtSignal

        class _Inner(QObject):
            frame_thumbnail_ready = pyqtSignal(str, int, object)

        self._obj = _Inner()
        self.signal = self._obj.frame_thumbnail_ready
        self.requests = []  # (asset_id, path, frame, total, height)

    def request_frame_pixmap_async(self, asset_id, path, frame, total, target_height=48):
        self.requests.append((asset_id, path, frame, total, target_height))


@pytest.fixture()
def fake_provider(monkeypatch):
    import src.features.edit.ui.multicam_panel as panel_mod

    fake_signal = _FakeProviderSignal()
    provider = MagicMock()
    provider.frame_thumbnail_ready = fake_signal.signal
    provider.request_frame_pixmap_async.side_effect = (
        fake_signal.request_frame_pixmap_async)

    class _FakeThumbnailProvider:
        @staticmethod
        def instance():
            return provider

    monkeypatch.setattr(panel_mod, "ThumbnailProvider", _FakeThumbnailProvider)
    return provider, fake_signal


def _group(offsets=None):
    group = build_multicam_group_from_assets(
        ["a1", "a2", "a3"],
        {"a1": "Wide", "a2": "Close", "a3": "Top"},
    )
    if offsets:
        group = group_with_sync_offsets(group, offsets)
    return group


@pytest.mark.edit_ui
class TestMulticamPanelGrid:

    def _panel(self):
        from src.features.edit.ui.multicam_panel import MulticamPanel
        return MulticamPanel()

    def test_set_group_builds_tiles_and_combo(self, qapp, fake_provider):
        panel = self._panel()
        group = _group()
        panel.set_group(group, "a2", {
            "a1": {"path": "/m/a1.mp4", "total_frames": 400},
            "a2": {"path": "/m/a2.mp4", "total_frames": 400},
            "a3": {"path": "/m/a3.mp4", "total_frames": 400},
        })
        assert set(panel._tiles) == {"a1", "a2", "a3"}
        # 参考下拉默认 = 当前角度
        assert panel._reference_combo.currentData() == "a2"
        assert panel._sync_button.isEnabled()
        # 当前角度 tile 高亮
        assert panel._tiles["a2"]._active is True
        assert panel._tiles["a1"]._active is False

    def test_tile_click_emits_switch(self, qapp, fake_provider):
        panel = self._panel()
        group = _group()
        panel.set_group(group, "a1")
        emitted = []
        panel.switch_angle_requested.connect(
            lambda gid, aid, off: emitted.append((gid, aid, off)))
        panel._tiles["a3"].clicked.emit()
        assert len(emitted) == 1
        assert emitted[0][1] == "a3"
        assert emitted[0][0] == group.group_id

    def test_offset_badge_in_caption(self, qapp, fake_provider):
        panel = self._panel()
        group = _group(offsets={"a2": 30})
        panel.set_group(group, "a1")
        assert "[+30f]" in panel._tiles["a2"]._caption.text()
        assert "]" not in panel._tiles["a1"]._caption.text()

    def test_clear_group_disables_sync(self, qapp, fake_provider):
        panel = self._panel()
        panel.set_group(_group(), "a1")
        panel.set_group(None)
        assert not panel._sync_button.isEnabled()
        assert not panel._reference_combo.isEnabled()
        assert panel._tiles == {}

    def test_sync_button_emits_reference(self, qapp, fake_provider):
        panel = self._panel()
        panel.set_group(_group(), "a1")
        emitted = []
        panel.sync_requested.connect(emitted.append)
        panel._sync_button.click()
        assert emitted == ["a1"]

    def test_sync_running_toggles_button(self, qapp, fake_provider):
        panel = self._panel()
        panel.set_group(_group(), "a1")
        panel.set_sync_running(True)
        assert not panel._sync_button.isEnabled()
        panel.set_sync_running(False)
        assert panel._sync_button.isEnabled()


@pytest.mark.edit_ui
class TestPanelFrameRequests:

    def _panel_with_group(self):
        from src.features.edit.ui.multicam_panel import MulticamPanel
        panel = MulticamPanel()
        panel.set_group(_group(), "a1", {
            "a1": {"path": "/m/a1.mp4", "total_frames": 400},
            "a2": {"path": "/m/a2.mp4", "total_frames": 400},
            "a3": {"path": "/m/a3.mp4", "total_frames": 400},
        })
        return panel

    def test_frames_coalesce_until_timer_fires(self, qapp, fake_provider):
        provider, recorder = fake_provider
        panel = self._panel_with_group()

        panel.request_angle_frames({"a1": 10, "a2": 20, "a3": 30})
        panel.request_angle_frames({"a1": 11, "a2": 21, "a3": 31})  # 合并
        assert provider.request_frame_pixmap_async.call_count == 0
        assert panel._frame_timer.isActive()

        panel._frame_timer.stop()
        panel._apply_angle_frames()
        called = {req[0]: req[2] for req in recorder.requests}
        assert called == {"a1": 11, "a2": 21, "a3": 31}

    def test_duplicate_frame_not_rerequested(self, qapp, fake_provider):
        provider, recorder = fake_provider
        panel = self._panel_with_group()

        panel.request_angle_frames({"a1": 10})
        panel._frame_timer.stop()
        panel._apply_angle_frames()
        assert len(recorder.requests) == 1

        panel.request_angle_frames({"a1": 10})  # 同帧不重复请求
        panel._frame_timer.stop()
        panel._apply_angle_frames()
        assert len(recorder.requests) == 1

    def test_frame_clamped_to_total(self, qapp, fake_provider):
        provider, recorder = fake_provider
        panel = self._panel_with_group()
        panel.request_angle_frames({"a1": 99999})
        panel._frame_timer.stop()
        panel._apply_angle_frames()
        assert recorder.requests[0][2] == 399  # total-1

    def test_thumbnail_ready_updates_tile(self, qapp, fake_provider):
        from PyQt6.QtGui import QImage, QPixmap

        provider, recorder = fake_provider
        panel = self._panel_with_group()
        panel.request_angle_frames({"a1": 10})
        panel._frame_timer.stop()
        panel._apply_angle_frames()

        image = QImage(64, 48, QImage.Format.Format_RGB32)
        image.fill(0xFFFFFFFF)
        pixmap = QPixmap.fromImage(image)
        provider.frame_thumbnail_ready.emit("/m/a1.mp4", 10, pixmap)

        tile = panel._tiles["a1"]
        assert not tile._thumb.pixmap().isNull()
        assert not panel._pending_by_key  # pending 清空

    def test_thumbnail_ready_unknown_key_ignored(self, qapp, fake_provider):
        provider, _recorder = fake_provider
        panel = self._panel_with_group()
        from PyQt6.QtGui import QPixmap
        provider.frame_thumbnail_ready.emit("/other.mp4", 99, QPixmap())
        assert panel._tiles["a1"]._thumb.pixmap().isNull()


# =====================================================================
# F2.5 · 实时路径（播放会话 → MulticamGridStream；暂停回落静帧路径）
# =====================================================================

class _FakeGridStream:
    """MulticamGridStream 测试替身：记录 push/会话/门控调用。"""

    def __init__(self):
        from src.features.edit.ui.multicam_grid_stream import (
            MulticamGridSignals,
        )
        self._signals = MulticamGridSignals()
        self.pushed: list[tuple] = []
        self.sessions: list[str] = []
        self.active_calls: list[bool] = []
        self.shutdown_called = False

    @property
    def signals(self) -> MulticamGridSignals:
        return self._signals

    def start(self) -> None:
        pass

    def begin_session(self) -> int:
        self.sessions.append("begin")
        return len(self.sessions)

    def end_session(self) -> None:
        self.sessions.append("end")

    def set_active(self, active: bool) -> None:
        self.active_calls.append(bool(active))

    def push(self, frames, infos, timeline_frame, generation=0) -> None:
        self.pushed.append(
            (dict(frames), dict(infos), timeline_frame, generation))

    def stats(self) -> dict:
        return getattr(self, "_stats_snapshot", {})

    def shutdown(self) -> None:
        self.shutdown_called = True


@pytest.mark.edit_ui
class TestPanelRealtimeRouting:

    def _panel(self, fake_stream):
        from src.features.edit.ui.multicam_panel import MulticamPanel
        panel = MulticamPanel(grid_stream_factory=lambda: fake_stream)
        panel.set_group(_group(), "a1", {
            "a1": {"path": "/m/a1.mp4", "total_frames": 400},
            "a2": {"path": "/m/a2.mp4", "total_frames": 400},
            "a3": {"path": "/m/a3.mp4", "total_frames": 400},
        })
        panel.show()
        return panel

    def test_playback_routes_to_stream_bypassing_debounce(
            self, qapp, fake_provider):
        """播放中：推帧直达实时流（携带同一时间线帧号+generation），
        不启动 250ms 去抖、不经 ThumbnailProvider。"""
        provider, _recorder = fake_provider
        stream = _FakeGridStream()
        panel = self._panel(stream)
        panel.set_playback_active(True)
        assert stream.sessions == ["begin"]

        panel.request_angle_frames(
            {"a1": 10, "a2": 20, "a3": 30},
            {"a1": {"path": "/m/a1.mp4", "total_frames": 400}},
            timeline_frame=100)
        assert len(stream.pushed) == 1
        frames, infos, timeline_frame, generation = stream.pushed[0]
        assert frames == {"a1": 10, "a2": 20, "a3": 30}
        assert timeline_frame == 100
        assert generation == panel._grid_generation
        assert not panel._frame_timer.isActive()      # 绕开去抖
        assert provider.request_frame_pixmap_async.call_count == 0

    def test_pause_falls_back_to_static_path(self, qapp, fake_provider):
        """暂停：会话结束、徽标回落"静帧"，下一推帧走 ThumbnailProvider
        去抖路径。"""
        provider, recorder = fake_provider
        stream = _FakeGridStream()
        panel = self._panel(stream)
        panel.set_playback_active(True)
        panel.set_playback_active(False)
        assert stream.sessions == ["begin", "end"]
        assert panel._tiles["a1"]._mode_badge.text() == "静帧"

        panel.request_angle_frames({"a1": 10})
        assert stream.pushed == []                     # 不再走实时流
        assert panel._frame_timer.isActive()           # 回落去抖路径
        panel._frame_timer.stop()
        panel._apply_angle_frames()
        assert provider.request_frame_pixmap_async.call_count == 1

    def test_hidden_panel_gates_stream_push(self, qapp, fake_provider):
        """可见性门控：面板隐藏（dock 收起/最小化等效）停发取帧。"""
        _provider, _recorder = fake_provider
        stream = _FakeGridStream()
        panel = self._panel(stream)
        panel.set_playback_active(True)
        panel.hide()
        qapp.processEvents()
        panel.request_angle_frames({"a1": 5}, None, timeline_frame=5)
        assert stream.pushed == []
        assert not panel._frame_timer.isActive()       # 隐藏时静帧路径也停发

    def test_grid_frame_ready_sets_tile_and_badge(
            self, qapp, fake_provider):
        """实时帧回填：同 generation 帧写入 tile，徽标 LIVE；旧会话帧丢弃。"""
        _provider, _recorder = fake_provider
        stream = _FakeGridStream()
        panel = self._panel(stream)
        panel.set_playback_active(True)
        from PyQt6.QtGui import QImage
        image = QImage(64, 48, QImage.Format.Format_RGB32)
        image.fill(0xFF00AAFF)
        stream.signals.frame_ready.emit("a2", 100, image,
                                        panel._grid_generation)
        qapp.processEvents()
        assert not panel._tiles["a2"]._thumb.pixmap().isNull()
        assert panel._tiles["a2"]._mode_badge.text() == "LIVE"
        before = panel._tiles["a2"]._thumb.pixmap().toImage()
        stale = QImage(64, 48, QImage.Format.Format_RGB32)
        stale.fill(0xFF000000)
        stream.signals.frame_ready.emit("a2", 100, stale,
                                        panel._grid_generation + 7)
        qapp.processEvents()
        after = panel._tiles["a2"]._thumb.pixmap().toImage()
        assert before == after                          # 旧会话帧不回显

    def test_angle_state_marks_missing(self, qapp, fake_provider):
        """缺路徽标：angle_state(missing) → "缺路"。"""
        _provider, _recorder = fake_provider
        stream = _FakeGridStream()
        panel = self._panel(stream)
        panel.set_playback_active(True)
        stream.signals.angle_state.emit(
            "a3", "missing", panel._grid_generation)
        qapp.processEvents()
        assert panel._tiles["a3"]._mode_badge.text() == "缺路"

    def test_degrade_level_marks_badge(self, qapp, fake_provider):
        """降质档徽标：统计 level>0 的角度帧到达 → "LIVE·L2"。"""
        _provider, _recorder = fake_provider
        stream = _FakeGridStream()
        panel = self._panel(stream)
        panel.set_playback_active(True)
        from PyQt6.QtGui import QImage
        image = QImage(64, 48, QImage.Format.Format_RGB32)
        stream._stats_snapshot = {"a1": {"level": 2}}
        stream.signals.frame_ready.emit("a1", 10, image,
                                        panel._grid_generation)
        qapp.processEvents()
        assert panel._tiles["a1"]._mode_badge.text() == "LIVE·L2"
        # 恢复到 level 0 → 回纯 LIVE
        stream._stats_snapshot = {"a1": {"level": 0}}
        stream.signals.frame_ready.emit("a1", 11, image,
                                        panel._grid_generation)
        qapp.processEvents()
        assert panel._tiles["a1"]._mode_badge.text() == "LIVE"
