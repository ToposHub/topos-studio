"""Focused tests for the shared realtime preview FPS indicator."""

from __future__ import annotations


def test_realtime_fps_indicator_is_green_at_target_rate(qapp):
    from src.shared.ui.widgets.realtime_fps_indicator import RealtimeFpsIndicator

    clock = [0.0]
    indicator = RealtimeFpsIndicator(now_fn=lambda: clock[0])
    indicator.set_playback_state(True, 24.0)

    for frame in range(25):
        clock[0] = frame / 24.0
        indicator.record_presented_frame(frame)
    indicator._refresh_display()

    assert indicator.text() == "24 FPS"
    assert indicator.is_realtime is True
    assert "#7fd18b" in indicator.styleSheet()


def test_realtime_fps_indicator_is_red_when_display_lags(qapp):
    from src.shared.ui.widgets.realtime_fps_indicator import RealtimeFpsIndicator

    clock = [0.0]
    indicator = RealtimeFpsIndicator(now_fn=lambda: clock[0])
    indicator.set_playback_state(True, 24.0)

    for frame in range(13):
        clock[0] = frame / 12.0
        indicator.record_presented_frame(frame)
    indicator._refresh_display()

    assert indicator.text() == "12 FPS"
    assert indicator.is_realtime is False
    assert "#ff6b6b" in indicator.styleSheet()


def test_realtime_fps_indicator_does_not_count_duplicate_frame_tokens(qapp):
    from src.shared.ui.widgets.realtime_fps_indicator import RealtimeFpsIndicator

    clock = [0.0]
    indicator = RealtimeFpsIndicator(now_fn=lambda: clock[0])
    indicator.set_playback_state(True, 24.0)

    indicator.record_presented_frame(7)
    clock[0] = 0.5
    indicator.record_presented_frame(7)

    assert indicator.measured_fps is None


def test_realtime_fps_indicator_is_neutral_when_paused(qapp):
    from src.shared.ui.widgets.realtime_fps_indicator import RealtimeFpsIndicator

    indicator = RealtimeFpsIndicator()
    indicator.set_playback_state(False, 30.0)

    assert indicator.text() == "FPS —"
    assert "#9a9a9a" in indicator.styleSheet()


def test_realtime_fps_indicator_is_neutral_before_first_valid_sample(qapp):
    from src.shared.ui.widgets.realtime_fps_indicator import RealtimeFpsIndicator

    clock = [0.0]
    indicator = RealtimeFpsIndicator(now_fn=lambda: clock[0])
    indicator.set_playback_state(True, 24.0)
    indicator.record_presented_frame(1)
    clock[0] = 1.0
    indicator._refresh_display()

    assert indicator.text() == "FPS --"
    assert "#9a9a9a" in indicator.styleSheet()


def test_realtime_fps_indicator_keeps_last_value_when_frames_stop_arriving(qapp):
    from src.shared.ui.widgets.realtime_fps_indicator import RealtimeFpsIndicator

    clock = [0.0]
    indicator = RealtimeFpsIndicator(now_fn=lambda: clock[0])
    indicator.set_playback_state(True, 24.0)
    for frame in range(25):
        clock[0] = frame / 24.0
        indicator.record_presented_frame(frame)

    indicator._refresh_display()
    clock[0] = 2.0
    indicator._refresh_display()

    assert indicator.text() == "24 FPS"
    assert "#ff6b6b" in indicator.styleSheet()

    indicator.set_playback_state(False, 24.0)
    assert indicator.text() == "24 FPS"
    assert "#9a9a9a" in indicator.styleSheet()


def test_realtime_fps_indicator_refresh_is_throttled_to_playback(qapp):
    from src.shared.ui.widgets.realtime_fps_indicator import RealtimeFpsIndicator

    indicator = RealtimeFpsIndicator()

    assert indicator._refresh_timer.interval() == 500
    assert indicator._refresh_timer.isActive() is False

    indicator.set_playback_state(True, 24.0)
    assert indicator._refresh_timer.isActive() is True

    indicator.set_playback_state(False, 24.0)
    assert indicator._refresh_timer.isActive() is False
