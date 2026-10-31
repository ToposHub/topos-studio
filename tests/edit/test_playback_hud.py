"""P1-23 — 播放性能 HUD 测试。

覆盖：
- PlaybackHud 轮询渲染（状态/帧/目标 fps/丢帧）
- 实测 fps 差分计算（注入 now_fn，两次刷新）
- 代理就绪度统计行（stats_getter）
- 无控制器时的占位文案
- TimelinePanel.toggle_playback_hud 开关生命周期
- _collect_proxy_hud_stats 对 video/audio 混合素材的统计
"""

from __future__ import annotations

import os
import sys

import pytest

pytestmark = pytest.mark.edit_ui

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

from src.features.edit.core.edit_playback_controller import PlaybackState
from src.features.edit.core.project import EditProject, MediaAsset
from src.features.edit.ui.playback_hud import PlaybackHud


class _FakeController:
    def __init__(self, frame: int = 0, playing: bool = True, dropped: int = 3) -> None:
        self.state = PlaybackState.PLAYING if playing else PlaybackState.STOPPED
        self.current_frame = frame
        self.fps = 24.0
        self.playback_speed = 1.0
        self.dropped_frames = dropped
        self.is_playing = playing


class _FakeClock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


class TestPlaybackHud:
    def test_renders_state_frame_and_target(self, qapp) -> None:
        clock = _FakeClock()
        controller = _FakeController(frame=120, dropped=5)
        hud = PlaybackHud(
            controller_getter=lambda: controller, now_fn=clock, parent=None
        )
        try:
            text = hud._label.text()
            assert "PLAYING" in text
            assert "120" in text
            assert "24" in text  # 目标 fps
            assert "5" in text  # 丢帧
        finally:
            hud.deleteLater()

    def test_measured_fps_from_frame_delta(self, qapp) -> None:
        clock = _FakeClock()
        controller = _FakeController(frame=0)
        hud = PlaybackHud(
            controller_getter=lambda: controller, now_fn=clock, parent=None
        )
        try:
            # 10 秒内前进 240 帧 → 24.0 fps
            controller.current_frame = 240
            clock.now += 10.0
            hud._refresh()
            assert hud._measured_fps == pytest.approx(24.0)
            assert "24.0" in hud._label.text()
        finally:
            hud.deleteLater()

    def test_paused_resets_measured_fps(self, qapp) -> None:
        clock = _FakeClock()
        controller = _FakeController(frame=0)
        hud = PlaybackHud(
            controller_getter=lambda: controller, now_fn=clock, parent=None
        )
        try:
            controller.current_frame = 240
            clock.now += 10.0
            hud._refresh()
            assert hud._measured_fps > 0
            controller.is_playing = False
            controller.state = PlaybackState.PAUSED
            hud._refresh()
            assert hud._measured_fps == 0.0
        finally:
            hud.deleteLater()

    def test_stats_line_rendered(self, qapp) -> None:
        clock = _FakeClock()
        controller = _FakeController()
        hud = PlaybackHud(
            controller_getter=lambda: controller,
            stats_getter=lambda: {"ready": 2, "generating": 1, "missing": 3},
            now_fn=clock,
            parent=None,
        )
        try:
            text = hud._label.text()
            assert "2" in text and "1" in text and "3" in text
            assert "R2/G1/M3" in text
        finally:
            hud.deleteLater()

    def test_no_controller_placeholder(self, qapp) -> None:
        hud = PlaybackHud(controller_getter=lambda: None, parent=None)
        try:
            assert hud._label.text()  # 占位文案非空
        finally:
            hud.deleteLater()


class TestTimelinePanelHudToggle:
    def test_toggle_creates_and_destroys(self, qapp) -> None:
        from src.features.edit.ui.timeline_panel import TimelinePanel

        panel = TimelinePanel()
        try:
            assert panel._playback_hud is None
            panel.toggle_playback_hud()
            assert panel._playback_hud is not None
            panel.toggle_playback_hud()
            assert panel._playback_hud is None
        finally:
            panel.deleteLater()

    def test_collect_proxy_stats_counts_video_only(self, qapp, monkeypatch) -> None:
        import src.shared.media.disk_proxy_service as dps
        from src.features.edit.ui.timeline_panel import TimelinePanel

        class _FakeInfo:
            def __init__(self, status: str) -> None:
                class _S:
                    value = status
                self.status = _S()
                self.is_ready = status == "ready"

        class _FakeService:
            def get_disk_proxy_info(self, path: str):
                return _FakeInfo(path.rsplit("_", 1)[-1])

        monkeypatch.setattr(dps, "get_disk_proxy_service", lambda: _FakeService())

        def _asset(aid: str, media_type: str = "video") -> MediaAsset:
            return MediaAsset(
                asset_id=aid,
                path=f"/tmp/clip_{aid}",
                media_type=media_type,
                display_name=aid,
                metadata={},
            )

        panel = TimelinePanel()
        try:
            project = EditProject(
                project_id="p",
                name="n",
                assets={
                    "ready": _asset("ready"),
                    "generating": _asset("generating"),
                    "missing": _asset("missing"),
                    "missing2": _asset("missing2"),
                    "a1": _asset("a1", media_type="audio"),  # 不计
                },
                root_bin_id="root",
            )
            panel.set_project(project)
            stats = panel._collect_proxy_hud_stats()
            assert stats == {"ready": 1, "generating": 1, "missing": 2}
        finally:
            panel.deleteLater()


def test_menu_action_callback_resolves():
    """View/Timeline 的 HUD 菜单项回调必须存在于 MainWindow mixin（防静默跳过）。"""
    from src.app.controllers.edit_controller import EditControllerMixin
    from src.features.edit.module import EditModule
    from src.shared.actions import ActionRegistry

    registry = ActionRegistry()
    EditModule().register_menu_actions(registry)
    actions = {
        a.action_id: a
        for a in registry.get_actions(enabled_features={"edit"})
    }
    assert "edit.playback.hud" in actions
    assert actions["edit.playback.hud"].menu_path == "View/Timeline"
    assert callable(
        getattr(EditControllerMixin, actions["edit.playback.hud"].callback_name, None)
    )
