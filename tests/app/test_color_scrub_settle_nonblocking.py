"""
Tests for the non-blocking Color scrub-release settle seek.

松手精确定位（scrub_finished → seek_requested → on_video_seek）此前按
"暂停态 seek" 推断为阻塞，在 UI 线程同步等待 master 解码；现在改为非阻塞
seek + 就绪轮询上屏精确帧。
"""

from unittest.mock import MagicMock, call, patch

import pytest

from src.app.controllers.file_controller import FileOpsMixin
from src.features.edit.core.clip_params import SpeedParams
from src.features.edit.core.timeline_v2 import ClipInstance

pytestmark = pytest.mark.edit_core


def _make_controller(page: str = "Color"):
    fc = FileOpsMixin.__new__(FileOpsMixin)
    fc.video_controller = MagicMock()
    fc.video_controller.supports_async_scrub_seek = True
    fc.video_controller.is_playing = False
    fc.current_page = page
    fc._color_timeline_playback_controller = lambda: None
    fc._get_timeline_composite_playback_controller = lambda: None
    fc._sync_color_timeline_playhead = MagicMock()
    fc._color_local_frame_to_source_frame = lambda local: int(local) + 100
    fc._viewport_scrub_active = False
    fc._viewport_scrub_preview_token = 0
    fc._viewport_scrub_settle_pending = False
    return fc


class TestScrubSettleNonBlocking:
    @pytest.mark.parametrize("timeline_preview", [False, True])
    def test_color_pauses_background_work_before_seek_and_releases_after_page_change(
        self, qapp, timeline_preview: bool,
    ) -> None:
        from src.features.edit.ui.timeline_panel import TimelinePanel

        fc = _make_controller()
        panel = fc.timeline_panel = TimelinePanel()
        observed: list[bool] = []
        view = panel.get_timeline_view()
        on_seek = lambda *a, **kw: observed.append(view._thumb_provider.is_thumbnail_work_suspended)
        if timeline_preview:
            fc._color_timeline_playback_controller = panel.get_playback_controller
            fc.on_video_seek = on_seek
        else:
            fc.video_controller.seek_to_frame.side_effect = on_seek
        try:
            fc.on_video_scrub_started(30)
            assert observed == [True]
            fc.current_page = "Edit"
            fc.on_video_scrub_cancelled()
            timer = panel._background_media_timers["color_scrub"]
            assert timer.isActive()
            # A new press before the idle interval expires cancels that release.
            fc.current_page = "Color"
            fc.on_video_scrub_started(28)
            assert not timer.isActive()
            fc.on_video_scrub_finished(24)
            assert timer.isActive()
            timer.timeout.emit()
            assert not view._thumb_provider.is_thumbnail_work_suspended
        finally:
            panel.set_background_media_priority("color_scrub", False)
            panel.deleteLater()

    def test_scope_freeze_precedes_timeline_pause_and_unfreezes_on_release(self):
        fc = _make_controller("Color")
        timeline = MagicMock()
        timeline.is_playing = True
        fc._color_timeline_playback_controller = lambda: timeline
        fc._on_scope_scrub_state_changed = MagicMock()
        fc.on_video_seek = MagicMock()
        calls = MagicMock()
        calls.attach_mock(fc._on_scope_scrub_state_changed, 'scope')
        calls.attach_mock(timeline.pause, 'pause')
        fc.on_video_scrub_started(5)
        assert calls.mock_calls[0].args == (True,)
        assert calls.mock_calls[1] == call.pause()
        fc.on_video_scrub_finished(10)
        fc._on_scope_scrub_state_changed.assert_called_with(False)

    def test_settle_seek_is_non_blocking_with_color_precision(self):
        fc = _make_controller("Color")
        fc.on_video_scrub_started(50)
        fc.on_video_scrub_moved(60)
        fc.on_video_scrub_finished(60)
        assert fc._viewport_scrub_settle_pending is True

        fc.on_video_seek(60)
        kwargs = fc.video_controller.seek_to_frame.call_args.kwargs
        assert kwargs.get("blocking") is False
        assert kwargs.get("allow_proxy_fallback") is False
        assert fc._viewport_scrub_settle_pending is False

    def test_settle_seek_non_color_keeps_proxy_fallback(self):
        fc = _make_controller("Edit")
        fc.on_video_scrub_started(10)
        fc.on_video_scrub_finished(10)
        fc.on_video_seek(10)
        kwargs = fc.video_controller.seek_to_frame.call_args.kwargs
        assert kwargs.get("blocking") is False
        assert "allow_proxy_fallback" not in kwargs

    def test_interrupted_drag_retires_preview_without_seeking_new_page(self) -> None:
        fc = _make_controller()
        fc.on_video_scrub_started(5)
        generation = fc._viewport_scrub_drag_generation
        fc.video_controller.reset_mock()
        fc.current_page = "Composite"
        fc.on_video_scrub_cancelled()
        assert not fc._viewport_scrub_active
        assert not fc._viewport_scrub_settle_pending
        assert fc._viewport_scrub_drag_generation > generation
        fc.video_controller.seek_to_frame.assert_not_called()

    def test_new_drag_clears_stale_settle_flag(self):
        fc = _make_controller()
        fc.on_video_scrub_started(5)
        fc.on_video_scrub_finished(5)
        assert fc._viewport_scrub_settle_pending is True
        fc.on_video_scrub_started(6)
        assert fc._viewport_scrub_settle_pending is False
        # 随后的 seek 不再走 settle 分支（无 blocking=False）
        fc.on_video_seek(6)
        kwargs = fc.video_controller.seek_to_frame.call_args.kwargs
        assert "blocking" not in kwargs

    def test_settle_poll_presents_exact_frame_and_stops(self, qapp):
        fc = _make_controller()
        present = fc.video_controller.present_ready_scrub_frame
        present.return_value = True
        fc.on_video_scrub_started(7)
        fc.on_video_scrub_finished(7)
        fc.on_video_seek(7)
        # 轮询回调在事件循环里执行
        for _ in range(20):
            qapp.processEvents()
        present.assert_called_once_with(107)  # local 7 -> source 107

    def test_settle_poll_cancelled_by_next_drag(self, qapp):
        fc = _make_controller()
        present = fc.video_controller.present_ready_scrub_frame
        present.return_value = False  # 永远不就绪 → 持续轮询
        fc.on_video_scrub_started(7)
        fc.on_video_scrub_finished(7)
        fc.on_video_seek(7)
        for _ in range(5):
            qapp.processEvents()
        assert present.call_count >= 1

        fc.on_video_scrub_started(9)  # token bump → settle 轮询作废
        count_after_cancel = present.call_count
        for _ in range(10):
            qapp.processEvents()
        # 轮询停止（只可能有拖拽启动路径自身的一次调用，不应持续增长）
        assert present.call_count <= count_after_cancel + 1

    def test_held_fast_scrub_refines_without_releasing_mouse(self, qapp) -> None:
        fc = _make_controller()
        fc.on_video_scrub_started(16)
        fc._viewport_scrub_pending_frame = 23

        with (
            patch("src.shared.video.scrub_stride.display_stride", return_value=8),
            patch("src.app.controllers.file_controller.QTimer.singleShot") as schedule,
        ):
            fc._emit_pending_viewport_scrub()
            timer = schedule.call_args.args[1]
            fc._viewport_scrub_last_emit_ms -= 150
            timer()

        assert fc._viewport_scrub_active
        assert fc._viewport_scrub_hold_exact
        assert fc._viewport_scrub_latest_source_target == 123
        fc.video_controller.seek_to_frame.assert_called_with(
            123, blocking=False, allow_proxy_fallback=False, scrub_reset=True,
        )
        fc.on_video_scrub_finished(23)
        fc.video_controller.seek_to_frame.reset_mock()
        timer()
        fc.video_controller.seek_to_frame.assert_not_called()

    @pytest.mark.parametrize("before_exact_seek", [True, False])
    def test_held_fast_scrub_cannot_seek_or_present_after_clip_change(
        self, qapp, before_exact_seek: bool,
    ) -> None:
        fc = _make_controller()
        fc._color_timeline_clip_playback_context = {"clip": object()}
        fc.on_video_scrub_started(16)
        fc._viewport_scrub_pending_frame = 23

        with (
            patch("src.shared.video.scrub_stride.display_stride", return_value=8),
            patch("src.app.controllers.file_controller.QTimer.singleShot") as schedule,
        ):
            fc._emit_pending_viewport_scrub()
            timer = schedule.call_args.args[1]
            fc._viewport_scrub_last_emit_ms -= 150
            fc.video_controller.seek_to_frame.reset_mock()
            if before_exact_seek:
                fc._color_timeline_clip_playback_context = {"clip": object()}
                timer()
                fc.video_controller.seek_to_frame.assert_not_called()
            else:
                timer()
                fc.video_controller.seek_to_frame.assert_called_once()
                poll = schedule.call_args.args[1]
                fc._color_timeline_clip_playback_context = {"clip": object()}
                poll()
                fc.video_controller.present_ready_scrub_frame.assert_not_called()
        fc.on_video_scrub_finished(23)

    def test_held_fast_scrub_leaves_synchronous_backend_alone(self) -> None:
        fc = _make_controller()
        fc.video_controller.supports_async_scrub_seek = False
        with patch("src.app.controllers.file_controller.QTimer.singleShot") as schedule:
            fc._arm_color_scrub_hold_timer()
        schedule.assert_not_called()


class TestScrubCoalesceWatchdog:
    """拖拽合并闩锁自愈：singleShot 回调丢失不再永久冻结调色页拖拽。"""

    def test_stuck_coalesce_timer_recovers_within_watchdog(self):
        import time

        fc = _make_controller()
        fc._viewport_scrub_timer_pending = True
        # 模拟回调丢失：调度时间戳停留在 1s 前
        fc._viewport_scrub_timer_scheduled_ms = time.monotonic() * 1000.0 - 1000.0
        fc._viewport_scrub_pending_frame = 33
        fc._viewport_scrub_last_emit_ms = time.monotonic() * 1000.0 - 1000.0
        fc._viewport_scrub_interval_ms = 8

        emit_calls = []
        fc._emit_pending_viewport_scrub = lambda: emit_calls.append(1)
        from unittest.mock import patch

        with patch("src.app.controllers.file_controller.QTimer"):
            fc.on_video_scrub_moved(33)

        # 看门狗复位闩锁并立即发出（elapsed >= interval）
        assert fc._viewport_scrub_timer_pending is False
        assert emit_calls == [1]

    def test_fresh_coalesce_timer_still_defers(self):
        import time
        from unittest.mock import patch

        fc = _make_controller()
        now_ms = time.monotonic() * 1000.0
        fc._viewport_scrub_timer_pending = True
        fc._viewport_scrub_timer_scheduled_ms = now_ms - 5.0  # 窗口内，未超时
        fc._viewport_scrub_last_emit_ms = now_ms  # 刚 emit 过，elapsed < interval
        fc._viewport_scrub_interval_ms = 8

        emit_calls = []
        fc._emit_pending_viewport_scrub = lambda: emit_calls.append(1)
        with patch("src.app.controllers.file_controller.QTimer"):
            fc.on_video_scrub_moved(40)

        assert emit_calls == []  # 窗口内仍正常延后


class TestScrubReadyFramePump:
    """Only the current drag pumps ready frames until it ends."""

    @pytest.mark.parametrize(
        ("reverse", "freeze", "expected"),
        [
            (False, None, [(120, 120, 0), (123, 120, 1), (126, 120, 1), (124, 126, -1)]),
            (True, None, [(179, 179, 0), (176, 179, -1), (173, 179, -1), (175, 173, 1)]),
            (False, 140, [(140, 140, 0)]),
        ],
        ids=["normal", "reverse", "freeze"],
    )
    def test_color_retime_seek_and_pump_use_source_direction(self, reverse, freeze, expected) -> None:
        fc = _make_controller()
        del fc._color_local_frame_to_source_frame
        fc._color_timeline_clip_playback_context = {"clip": ClipInstance(
            clip_id="retime", asset_id="source", track_id="v1", timeline_start=10,
            timeline_duration=100, source_start=100, source_duration=100,
            speed=SpeedParams(reverse=reverse), freeze_source_frame=freeze,
        )}
        with (
            patch("src.app.controllers.file_controller.QTimer.singleShot") as schedule,
            patch("src.shared.video.scrub_stride.display_stride", return_value=1),
        ):
            fc.on_video_scrub_started(20)
            pump = schedule.call_args.args[1]
            for frame in (23, 26, 24):
                fc._viewport_scrub_pending_frame = frame
                fc._emit_pending_viewport_scrub()
            pump()

        seeks = fc.video_controller.seek_to_frame.call_args_list
        assert [(seek.args[0], seek.kwargs["scrub_origin"], seek.kwargs["scrub_direction"]) for seek in seeks] == expected
        assert [seek.kwargs["scrub_reset"] for seek in seeks] == ([True] if freeze else [True, False, False, True])
        target, origin, direction = expected[-1]
        fc.video_controller.present_latest_ready_scrub_frame.assert_called_once_with(
            target, scrub_origin=origin, scrub_direction=direction,
        )

    def test_color_reversal_keeps_a_monotonic_target_when_stride_changes(self) -> None:
        fc = _make_controller()
        fc._color_static_preview_token = 9
        with patch("src.app.controllers.file_controller.QTimer.singleShot"):
            fc.on_video_scrub_started(20)
            for frame, stride in ((51, 16), (49, 16), (45, 8), (43, 16), (41, 4)):
                fc._viewport_scrub_pending_frame = frame
                with patch("src.shared.video.scrub_stride.display_stride", return_value=stride):
                    fc._emit_pending_viewport_scrub()

        assert fc._color_static_preview_token == 10
        calls = fc.video_controller.seek_to_frame.call_args_list
        assert [c.args[0] for c in calls] == [120, 148, 151, 148, 144]
        assert [c.kwargs["scrub_origin"] for c in calls] == [120, 120, 151, 151, 151]
        assert [c.kwargs["scrub_direction"] for c in calls] == [0, 1, -1, -1, -1]
        assert [c.kwargs["scrub_reset"] for c in calls] == [True, False, True, False, False]

    def test_pump_presents_latest_target(self, qapp):
        fc = _make_controller()
        present_latest = fc.video_controller.present_latest_ready_scrub_frame
        present_latest.return_value = True
        fc.on_video_scrub_started(20)
        for _ in range(10):
            qapp.processEvents()
        # local 20 -> source 120（映射 +100）
        present_latest.assert_called_with(120, scrub_origin=120, scrub_direction=0)
        assert fc._viewport_scrub_pump_armed is True

    def test_pump_single_instance_across_moves(self, qapp):
        fc = _make_controller()
        fc.on_video_scrub_started(20)
        assert fc._viewport_scrub_pump_armed is True
        fc._viewport_scrub_pump_armed = True  # 已武装
        fc._poll_ready_viewport_scrub_frame(30)
        # 不重复武装：drag_generation 不变即同一条泵
        gen = fc._viewport_scrub_drag_generation
        fc._poll_ready_viewport_scrub_frame(31)
        assert fc._viewport_scrub_drag_generation == gen

    def test_previous_drag_pump_cannot_disarm_the_new_pump(self, qapp):
        fc = _make_controller()
        fc.on_video_scrub_started(20)
        fc.on_video_scrub_finished(25)
        fc.on_video_scrub_started(30)
        qapp.processEvents()
        assert fc._viewport_scrub_pump_armed
        fc.video_controller.present_latest_ready_scrub_frame.assert_called_once_with(
            130, scrub_origin=130, scrub_direction=0,
        )
        fc.on_video_scrub_finished(30)

    def test_color_skips_a_repeated_master_preview_target(self):
        fc = _make_controller("Color")
        fc.on_video_scrub_started(20)
        fc._viewport_scrub_pending_frame = 20
        fc._emit_pending_viewport_scrub()
        fc.video_controller.seek_to_frame.assert_called_once_with(
            120, blocking=False, allow_proxy_fallback=False, fast_scrub=True,
            scrub_origin=120, scrub_direction=0, scrub_reset=True,
        )
        fc.on_video_scrub_finished(20)

    def test_pump_stops_after_release(self, qapp):
        import time

        fc = _make_controller()
        present_latest = fc.video_controller.present_latest_ready_scrub_frame
        present_latest.return_value = False
        fc.on_video_scrub_started(20)
        for _ in range(5):
            qapp.processEvents()
        fc.on_video_scrub_finished(25)
        count = present_latest.call_count
        deadline = time.monotonic() + 0.08
        while time.monotonic() < deadline:
            qapp.processEvents()
            time.sleep(0.005)
        # 泵已终止：80ms（≥6 个 12ms tick）内不再有新调用
        assert present_latest.call_count == count

    def test_legacy_fallback_without_latest_api(self, qapp):
        fc = _make_controller()
        del fc.video_controller.present_latest_ready_scrub_frame
        present = fc.video_controller.present_ready_scrub_frame
        present.return_value = True
        fc.on_video_scrub_started(7)
        fc._viewport_scrub_pending_frame = 7
        fc._emit_pending_viewport_scrub()
        for _ in range(10):
            qapp.processEvents()
        present.assert_called_once_with(107)
