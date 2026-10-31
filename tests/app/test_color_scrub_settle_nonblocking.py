"""
Tests for the non-blocking Color scrub-release settle seek.

松手精确定位（scrub_finished → seek_requested → on_video_seek）此前按
"暂停态 seek" 推断为阻塞，在 UI 线程同步等待 master 解码；现在改为非阻塞
seek + 就绪轮询上屏精确帧。
"""

from unittest.mock import MagicMock

import pytest

from src.app.controllers.file_controller import FileOpsMixin

pytestmark = pytest.mark.edit_core


def _make_controller(page: str = "Color"):
    fc = FileOpsMixin.__new__(FileOpsMixin)
    fc.video_controller = MagicMock()
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
    """拖拽期就绪帧泵：单一 12ms 循环取最新完成帧，松手即停。"""

    def test_pump_presents_latest_target(self, qapp):
        fc = _make_controller()
        present_latest = fc.video_controller.present_latest_ready_scrub_frame
        present_latest.return_value = True
        fc.on_video_scrub_started(20)
        for _ in range(10):
            qapp.processEvents()
        # local 20 -> source 120（映射 +100）
        present_latest.assert_called_with(120)
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
        for _ in range(10):
            qapp.processEvents()
        present.assert_called_once_with(107)
