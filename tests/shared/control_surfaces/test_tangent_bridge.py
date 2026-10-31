from __future__ import annotations

import math
import time

import pytest

from PyQt6.QtCore import QObject, pyqtSignal

from src.features.color.integrations.tangent_bridge import (
    TRACKERBALL_FAST_SPEED_SCALE,
    TRACKERBALL_MIN_SPEED_SCALE,
    TRACKERBALL_SPEED_REFERENCE,
    TangentColorBridge,
    _TrackerballPoint,
)
from src.features.color.ui.color_wheel import ColorWheel


class _FakePanel(QObject):
    grading_changed = pyqtSignal(object, object, object, object, object, object,
                                  object, object, object, object, object, object)

    def __init__(self) -> None:
        super().__init__()
        self.params = {
            "lift": (0.0, 0.0, 0.0),
            "gamma": (0.0, 0.0, 0.0),
            "gain": (0.0, 0.0, 0.0),
            "exposure": 0.0,
            "temp": 0.0,
            "tint": 0.0,
            "contrast": 0.0,
            "shadow": 0.0,
            "saturation": 0.0,
            "vibrance": 0.0,
            "mid_detail": 0.0,
            "hue_shift": 0.0,
        }
        self.last_set_params = None
        self.wheel_set_calls: list[tuple[str, tuple[float, float, float]]] = []

    def get_current_params(self):
        return dict(self.params)

    def set_params(self, lift, gamma, gain, exposure, temp, tint, contrast,
                   shadow, saturation, vibrance, mid_detail, hue_shift,
                   emit=False):
        self.params.update({
            "lift": tuple(lift),
            "gamma": tuple(gamma),
            "gain": tuple(gain),
            "exposure": exposure,
            "temp": temp,
            "tint": tint,
            "contrast": contrast,
            "shadow": shadow,
            "saturation": saturation,
            "vibrance": vibrance,
            "mid_detail": mid_detail,
            "hue_shift": hue_shift,
        })
        self.last_set_params = self.params.copy()
        if emit:
            values = self.params
            self.grading_changed.emit(
                values["lift"], values["gamma"], values["gain"],
                values["exposure"], values["temp"], values["tint"],
                values["contrast"], values["shadow"], values["saturation"],
                values["vibrance"], values["mid_detail"], values["hue_shift"],
            )

    def set_wheel_params(self, wheel_name, values, *, emit=False):
        wheel_values = tuple(float(value) for value in values)
        self.params[wheel_name] = wheel_values
        self.wheel_set_calls.append((wheel_name, wheel_values))
        self.last_set_params = self.params.copy()
        if emit:
            params = self.params
            self.grading_changed.emit(
                params["lift"], params["gamma"], params["gain"],
                params["exposure"], params["temp"], params["tint"],
                params["contrast"], params["shadow"], params["saturation"],
                params["vibrance"], params["mid_detail"], params["hue_shift"],
            )

    def reset_all(self):
        pass


class _FakeMainWindow(QObject):
    def __init__(self) -> None:
        super().__init__()
        self.grading_panel = _FakePanel()
        self.config_manager = None
        self.interactions = []

    def on_interaction_started(self):
        self.interactions.append("start")

    def on_interaction_ended(self):
        self.interactions.append("end")


class _FakeConfig:
    def __init__(self, values):
        self.values = values

    def get(self, key):
        return self.values.get(key)


def test_relative_parameter_change_updates_color_wheel(qapp):
    window = _FakeMainWindow()
    window.config_manager = _FakeConfig({
        "tangent.trackerball_smoothing": 0,
    })
    bridge = TangentColorBridge(window, parent=window)
    sent = []
    bridge.client.send_parameter_value = lambda *args: sent.append(args) or True

    bridge._on_parameter_change(0x00310023, 0.025)  # Lift saturation
    bridge._flush_trackerball_motion()

    assert window.grading_panel.last_set_params["lift"] == pytest.approx(
        (330.0, 0.25, 0.0)
    )
    assert any(
        item[0] == 0x00310023 and item[1] == pytest.approx(0.25)
        for item in sent
    )
    assert window.interactions == ["start"]
    bridge.shutdown()


def test_trackerball_fast_path_updates_only_the_active_wheel(qapp):
    window = _FakeMainWindow()
    window.config_manager = _FakeConfig({"tangent.trackerball_smoothing": 0})
    bridge = TangentColorBridge(window, parent=window)
    bridge.client.send_parameter_value = lambda *args: True

    bridge._on_parameter_change(0x00310031, 0.01)
    bridge._flush_trackerball_motion()

    assert len(window.grading_panel.wheel_set_calls) == 1
    wheel_name, values = window.grading_panel.wheel_set_calls[0]
    assert wheel_name == "gamma"
    assert values == pytest.approx((240.0, 0.1, 0.0))
    bridge.shutdown()


def test_one_count_axis_increment_is_not_a_full_normalized_move(qapp):
    assert TangentColorBridge._normalize_axis_increment(1.0) == pytest.approx(0.01)
    assert TangentColorBridge._normalize_axis_increment(-3.0) == pytest.approx(-0.03)
    assert TangentColorBridge._normalize_axis_increment(0.25) == pytest.approx(0.25)


def test_tangent_sensitivity_and_direction_are_configurable(qapp):
    window = _FakeMainWindow()
    window.grading_panel.params["gamma"] = (100.0, 0.0, 0.0)
    window.config_manager = _FakeConfig({
        "tangent.gamma_hue_sensitivity": 200,
        "tangent.hue_direction_inverted": True,
        "tangent.trackerball_smoothing": 0,
    })
    bridge = TangentColorBridge(window, parent=window)
    bridge.client.send_parameter_value = lambda *args: True

    bridge._on_parameter_change(0x00310031, 5.0)
    bridge._flush_trackerball_motion()

    assert window.grading_panel.last_set_params["gamma"] == (60.0, 0.1, 0.0)
    bridge.shutdown()


def test_trackerball_axes_move_the_puck_in_cartesian_directions(qapp):
    window = _FakeMainWindow()
    window.config_manager = _FakeConfig({
        "tangent.trackerball_smoothing": 0,
    })
    bridge = TangentColorBridge(window, parent=window)
    bridge.client.send_parameter_value = lambda *args: True

    # X+ is screen-right; Y+ is physical up and therefore screen-up.  Both
    # axes use the same normalized travel and sensitivity.
    bridge._on_parameter_change(0x00310021, 0.01)
    bridge._flush_trackerball_motion()
    assert window.grading_panel.last_set_params["lift"] == pytest.approx(
        (240.0, 0.1, 0.0)
    )

    window.grading_panel.params["lift"] = (0.0, 0.0, 0.0)
    bridge._reset_trackerball_points_from_params(window.grading_panel.params)
    bridge._on_parameter_change(0x00310023, 0.01)
    bridge._flush_trackerball_motion()
    assert window.grading_panel.last_set_params["lift"] == pytest.approx(
        (330.0, 0.1, 0.0)
    )
    bridge.shutdown()


def test_trackerball_smoothing_moves_toward_target_without_jumping(qapp):
    window = _FakeMainWindow()
    window.config_manager = _FakeConfig({
        "tangent.trackerball_smoothing": 50,
    })
    bridge = TangentColorBridge(window, parent=window)
    bridge.client.send_parameter_value = lambda *args: True

    bridge._on_parameter_change(0x00310021, 0.01)
    bridge._flush_trackerball_motion()

    # 1000% sensitivity makes the final X target .10; 50% smoothing should
    # apply only the first half of that distance in the first frame.
    assert 0.0 < window.grading_panel.last_set_params["lift"][1] < 0.1
    assert bridge._trackerball_timer.isActive()
    bridge.shutdown()


def test_diagonal_trackerball_motion_is_one_cartesian_move(qapp):
    window = _FakeMainWindow()
    window.config_manager = _FakeConfig({"tangent.trackerball_smoothing": 0})
    bridge = TangentColorBridge(window, parent=window)
    bridge.client.send_parameter_value = lambda *args: True

    # Both packets arrive before the frame is flushed. They must be combined
    # in X/Y space before converting the point to Hue/Sat.
    bridge._on_parameter_change(0x00310021, 0.01)
    bridge._on_parameter_change(0x00310023, 0.01)
    bridge._flush_trackerball_motion()

    lift = window.grading_panel.last_set_params["lift"]
    state = bridge._trackerball_points["lift"]
    assert lift[0] == pytest.approx(285.0)
    assert lift[1] == pytest.approx((0.1**2 + 0.1**2) ** 0.5)
    assert state.output_x == pytest.approx(state.output_y)
    bridge.shutdown()


def test_trackerball_batch_uses_ui_arrival_time(qapp, monkeypatch):
    window = _FakeMainWindow()
    window.config_manager = _FakeConfig({"tangent.trackerball_smoothing": 0})
    bridge = TangentColorBridge(window, parent=window)
    bridge.client.send_parameter_value = lambda *args: True

    start_ns = time.monotonic_ns()
    monkeypatch.setattr(time, "monotonic_ns", lambda: start_ns)
    # The worker timestamp can be old when Qt delivers several queued Hub
    # events together.  The vector window starts on UI delivery so the first
    # event is not flushed before its paired event reaches this slot.
    bridge._on_parameter_change(0x00310021, 0.01, start_ns - 1_000_000_000)

    state = bridge._trackerball_points["lift"]
    assert state.pending_started_ns == start_ns
    monkeypatch.setattr(time, "monotonic_ns", lambda: start_ns + 8_000_000)
    bridge._flush_trackerball_motion()
    assert window.grading_panel.last_set_params is None

    monkeypatch.setattr(time, "monotonic_ns", lambda: start_ns + 12_000_000)
    bridge._flush_trackerball_motion()
    assert window.grading_panel.last_set_params is not None
    bridge.shutdown()


def test_first_axis_waits_for_interleaved_diagonal_packet(qapp, monkeypatch):
    window = _FakeMainWindow()
    window.config_manager = _FakeConfig({"tangent.trackerball_smoothing": 0})
    bridge = TangentColorBridge(window, parent=window)
    bridge.client.send_parameter_value = lambda *args: True

    start_ns = time.monotonic_ns()
    monkeypatch.setattr(time, "monotonic_ns", lambda: start_ns)
    bridge._on_parameter_change(0x00310021, 0.01, start_ns)

    # The first X packet waits for a sub-frame vector batch rather than being
    # committed as a visible horizontal step.
    monkeypatch.setattr(time, "monotonic_ns", lambda: start_ns + 8_000_000)
    bridge._flush_trackerball_motion()
    assert window.grading_panel.last_set_params is None

    # The interleaved Y packet arrives within the same 12 ms input frame.
    monkeypatch.setattr(time, "monotonic_ns", lambda: start_ns + 9_000_000)
    bridge._on_parameter_change(
        0x00310023,
        0.01,
        start_ns + 9_000_000,
    )
    bridge._flush_trackerball_motion()
    assert window.grading_panel.last_set_params is None

    monkeypatch.setattr(time, "monotonic_ns", lambda: start_ns + 12_000_000)
    bridge._flush_trackerball_motion()

    lift = window.grading_panel.last_set_params["lift"]
    state = bridge._trackerball_points["lift"]
    assert lift[0] == pytest.approx(285.0)
    # The first deliberate movement starts at the fine-control floor. Later
    # events use their measured per-axis cadence and accelerate smoothly.
    assert lift[1] == pytest.approx(
        (0.1**2 + 0.1**2) ** 0.5 * TRACKERBALL_MIN_SPEED_SCALE
    )
    bridge.shutdown()


def test_diagonal_vector_batch_limits_unpaired_input_latency(
    qapp,
    monkeypatch,
):
    window = _FakeMainWindow()
    window.config_manager = _FakeConfig({"tangent.trackerball_smoothing": 0})
    bridge = TangentColorBridge(window, parent=window)
    bridge.client.send_parameter_value = lambda *args: True

    state = bridge._trackerball_points["lift"]
    start_ns = time.monotonic_ns()
    state.pending_x = 0.1
    state.pending_axes = 1
    state.pending_started_ns = start_ns
    state.velocity_tracking_available = True

    # A missing partner is held only for the 12ms vector batch, not for the
    # former adaptive window that could reach 120ms under load.
    monkeypatch.setattr(time, "monotonic_ns", lambda: start_ns + 8_000_000)
    bridge._flush_trackerball_motion()
    assert window.grading_panel.last_set_params is None
    assert state.pending_axes == 1

    # Once the batch interval expires, the unmatched input is committed so
    # a genuine cardinal move cannot fall behind.
    monkeypatch.setattr(time, "monotonic_ns", lambda: start_ns + 12_000_000)
    bridge._flush_trackerball_motion()
    assert window.grading_panel.last_set_params is not None
    assert state.pending_axes == 0
    bridge.shutdown()


def test_trackerball_preserves_subpixel_cartesian_increment(qapp):
    window = _FakeMainWindow()
    window.config_manager = _FakeConfig({"tangent.trackerball_smoothing": 0})
    bridge = TangentColorBridge(window, parent=window)
    bridge.client.send_parameter_value = lambda *args: True

    state = bridge._trackerball_points["lift"]
    increment = 0.000123456789
    bridge._on_parameter_change(0x00310021, increment)
    bridge._flush_trackerball_motion()

    # The configured 1000% internal gain is applied without rounding the raw
    # fractional Hub increment to two or three decimal places.
    assert state.target_x == pytest.approx(increment * 10.0)
    assert state.output_x == pytest.approx(increment * 10.0)
    bridge.shutdown()


def test_previous_cardinal_gesture_cannot_split_next_diagonal(
    qapp,
    monkeypatch,
):
    window = _FakeMainWindow()
    window.config_manager = _FakeConfig({"tangent.trackerball_smoothing": 0})
    bridge = TangentColorBridge(window, parent=window)
    bridge.client.send_parameter_value = lambda *args: True

    start_ns = time.monotonic_ns()
    monkeypatch.setattr(time, "monotonic_ns", lambda: start_ns)
    bridge._on_parameter_change(0x00310021, 0.01, start_ns)
    monkeypatch.setattr(time, "monotonic_ns", lambda: start_ns + 12_000_000)
    bridge._flush_trackerball_motion()
    bridge._finish_gesture()
    window.grading_panel.wheel_set_calls.clear()

    # A new diagonal starts with X, then Y inside the vector window. The old
    # cardinal implementation remembered the prior X gesture and published
    # this first X packet alone at the 8 ms timer tick.
    second_ns = start_ns + 1_000_000_000
    monkeypatch.setattr(time, "monotonic_ns", lambda: second_ns)
    bridge._on_parameter_change(0x00310021, 0.01, second_ns)
    monkeypatch.setattr(time, "monotonic_ns", lambda: second_ns + 8_000_000)
    bridge._flush_trackerball_motion()
    assert window.grading_panel.wheel_set_calls == []

    monkeypatch.setattr(time, "monotonic_ns", lambda: second_ns + 9_000_000)
    bridge._on_parameter_change(
        0x00310023,
        0.01,
        second_ns + 9_000_000,
    )
    bridge._flush_trackerball_motion()
    assert window.grading_panel.wheel_set_calls == []

    monkeypatch.setattr(time, "monotonic_ns", lambda: second_ns + 12_000_000)
    bridge._flush_trackerball_motion()

    state = bridge._trackerball_points["lift"]
    assert len(window.grading_panel.wheel_set_calls) == 1
    assert state.output_x - 0.035 == pytest.approx(0.035)
    assert state.output_y == pytest.approx(0.035)
    bridge.shutdown()


def test_trackerball_vector_speed_is_direction_invariant():
    cardinal = _trackerball_state_for_speed(0.01, 0.0)
    diagonal_component = 0.01 / math.sqrt(2.0)
    diagonal = _trackerball_state_for_speed(
        diagonal_component,
        diagonal_component,
    )

    cardinal_speed = TangentColorBridge._consume_trackerball_vector_speed(cardinal)
    diagonal_speed = TangentColorBridge._consume_trackerball_vector_speed(diagonal)

    assert cardinal_speed is not None
    assert diagonal_speed is not None
    assert cardinal_speed == pytest.approx(0.5)
    assert diagonal_speed == pytest.approx(cardinal_speed)
    assert TangentColorBridge._trackerball_velocity_scale(
        diagonal_speed,
    ) == pytest.approx(
        TangentColorBridge._trackerball_velocity_scale(cardinal_speed)
    )


@pytest.mark.parametrize(
    ("x_increment", "y_increment"),
    [
        (0.001, 0.0),
        (0.001 / math.sqrt(2.0), 0.001 / math.sqrt(2.0)),
    ],
)
def test_trackerball_directions_share_one_qt_publication_cadence(
    qapp,
    qtbot,
    x_increment: float,
    y_increment: float,
):
    window = _FakeMainWindow()
    window.config_manager = _FakeConfig({"tangent.trackerball_smoothing": 0})
    bridge = TangentColorBridge(window, parent=window)
    bridge.client.send_parameter_value = lambda *args: True

    start_ns = time.monotonic_ns()
    for index in range(10):
        timestamp_ns = start_ns + index * 1_000_000
        bridge._on_parameter_change(
            0x00310021,
            x_increment,
            timestamp_ns,
        )
        if y_increment:
            bridge._on_parameter_change(
                0x00310023,
                y_increment,
                timestamp_ns,
            )

    # Input callbacks only accumulate motion. Both cardinal and diagonal
    # streams publish once from the same fixed Qt timer cadence.
    assert window.grading_panel.wheel_set_calls == []
    qtbot.waitUntil(
        lambda: len(window.grading_panel.wheel_set_calls) == 1,
        timeout=100,
    )
    qtbot.wait(20)
    assert len(window.grading_panel.wheel_set_calls) == 1
    state = bridge._trackerball_points["lift"]
    assert math.hypot(state.output_x, state.output_y) == pytest.approx(0.035)
    bridge.shutdown()


def _trackerball_state_for_speed(x: float, y: float) -> _TrackerballPoint:
    return _TrackerballPoint(
        pending_x=x,
        pending_y=y,
        pending_latest_input_ns=1_020_000_000,
        last_vector_input_ns=1_000_000_000,
        velocity_tracking_available=True,
    )


def test_trackerball_publishes_motion_below_snap_threshold(qapp):
    window = _FakeMainWindow()
    window.config_manager = _FakeConfig({"tangent.trackerball_smoothing": 0})
    bridge = TangentColorBridge(window, parent=window)
    bridge.client.send_parameter_value = lambda *args: True

    increment = 0.000009
    bridge._on_parameter_change(0x00310021, increment)
    bridge._flush_trackerball_motion()

    state = bridge._trackerball_points["lift"]
    assert state.output_x == pytest.approx(increment * 10.0)
    assert window.grading_panel.wheel_set_calls
    assert window.grading_panel.params["lift"][1] == pytest.approx(
        increment * 10.0
    )
    bridge.shutdown()


def test_trackerball_velocity_curve_keeps_slow_rolls_fine_and_fast_rolls_fast():
    slow_scale = TangentColorBridge._trackerball_velocity_scale(0.0125)
    medium_scale = TangentColorBridge._trackerball_velocity_scale(0.125)
    fast_scale = TangentColorBridge._trackerball_velocity_scale(
        TRACKERBALL_SPEED_REFERENCE
    )

    assert slow_scale > TRACKERBALL_MIN_SPEED_SCALE
    assert slow_scale < 0.38
    assert medium_scale > slow_scale
    assert medium_scale < fast_scale
    assert fast_scale == pytest.approx(TRACKERBALL_FAST_SPEED_SCALE)


def test_small_diagonal_vector_keeps_its_actual_hue(qapp):
    window = _FakeMainWindow()
    window.config_manager = _FakeConfig({"tangent.trackerball_smoothing": 0})
    bridge = TangentColorBridge(window, parent=window)
    state = bridge._trackerball_points["lift"]
    state.last_hue = 0.0

    # A real diagonal vector must not be swallowed by a center dead zone.
    hue, saturation, _ = bridge._wheel_from_trackerball_point(
        (0.0, 0.0, 0.0),
        0.01,
        0.01,
        state,
    )
    assert hue == pytest.approx(285.0)
    assert saturation == pytest.approx(math.sqrt(0.0002))
    bridge.shutdown()


def test_tangent_display_rounds_and_deduplicates_without_changing_internal_value(qapp):
    window = _FakeMainWindow()
    bridge = TangentColorBridge(window, parent=window)
    sent = []
    bridge.client.send_parameter_value = (
        lambda *args: sent.append(args) or True
    )

    bridge._send_parameter_value(0x00310021, 0.1234, False)
    bridge._send_parameter_value(0x00310021, 0.1241, False)
    bridge._send_parameter_value(0x00310021, 0.1261, False)

    assert sent == [
        (0x00310021, 0.12, False),
        (0x00310021, 0.13, False),
    ]
    bridge.shutdown()


def test_first_trackerball_delta_rebases_after_project_load(qapp, monkeypatch):
    window = _FakeMainWindow()
    window.config_manager = _FakeConfig({"tangent.trackerball_smoothing": 0})
    bridge = TangentColorBridge(window, parent=window)
    bridge.client.send_parameter_value = lambda *args: True

    # Simulate a project loader updating the panel with emit=False. The bridge
    # still has its startup origin at the neutral wheel center.
    window.grading_panel.params["lift"] = (120.0, 0.5, 0.0)
    start_ns = time.monotonic_ns()
    bridge._on_parameter_change(0x00310021, 0.01, start_ns)
    monkeypatch.setattr(time, "monotonic_ns", lambda: start_ns + 110_000_000)
    bridge._flush_trackerball_motion()

    expected_angle = math.radians(120.0 + 120.0)
    expected_x = (
        0.5 * math.cos(expected_angle)
        + 0.1 * TRACKERBALL_MIN_SPEED_SCALE
    )
    expected_y = 0.5 * math.sin(expected_angle)
    state = bridge._trackerball_points["lift"]
    assert state.output_x == pytest.approx(expected_x)
    assert state.output_y == pytest.approx(expected_y)
    assert window.grading_panel.last_set_params["lift"][1] > 0.4
    bridge.shutdown()


def test_missing_hub_shows_one_transient_notice_then_idle_status(qapp):
    window = _FakeMainWindow()
    bridge = TangentColorBridge(window, parent=window)
    statuses: list[str] = []
    bridge.status_changed.connect(statuses.append)

    # First failure of a streak: one transient notice, not a warning loop.
    bridge._on_error("timed out")
    # Grace period expired: the integration goes idle instead of retrying.
    bridge._on_unavailable()

    assert statuses == [
        "Tangent Hub 未响应，正在自动重试…",
        "未检测到 Tangent Hub，调色台集成已待机",
    ]
    bridge.shutdown()


def test_reconfigure_with_unchanged_surface_wakes_dormant_client(qapp):
    window = _FakeMainWindow()
    bridge = TangentColorBridge(window, parent=window)
    woken = []
    bridge.client.wake = lambda: woken.append(1)

    bridge.reconfigure(bridge._active_surface)

    assert woken == [1]
    bridge.shutdown()


def test_color_wheel_rotation_updates_pointer_calibration(qapp):
    wheel = ColorWheel()
    wheel.set_hsv(0.0, 1.0, 0.0)

    wheel.set_rotation_degrees(0.0)
    assert wheel.knob_pos.x() == 1.0
    assert abs(wheel.knob_pos.y()) < 1e-6

    wheel.set_rotation_degrees(90.0)
    assert abs(wheel.knob_pos.x()) < 1e-6
    assert wheel.knob_pos.y() == -1.0
    wheel.deleteLater()


def test_color_wheel_reset_cancels_queued_drag_update(qapp):
    wheel = ColorWheel()
    wheel.set_hsv(120.0, 0.8, 0.5)
    wheel._pending_hue = 120.0
    wheel._pending_sat = 0.8
    wheel._pending_val = 0.5
    wheel._update_timer.start()

    wheel._wheel_canvas.reset_btn.click()
    wheel._emit_changed_signal()

    assert not wheel._update_timer.isActive()
    assert wheel._pending_hue is None
    assert (wheel.hue, wheel.sat, wheel.brightness) == (0.0, 0.0, 0.0)
    wheel.deleteLater()


def test_one_gesture_one_undo_entry(qapp):
    """F1.4：一次连续硬件手势 = 一条 undo；手势结束后的新调整开新条目。"""
    window = _FakeMainWindow()
    window.config_manager = _FakeConfig({
        "tangent.trackerball_smoothing": 0,
    })
    bridge = TangentColorBridge(window, parent=window)
    bridge.client.send_parameter_value = lambda *args: True

    # 同一手势内连续多次调整——只应有一次 interaction start
    for _ in range(5):
        bridge._on_parameter_change(0x00310023, 0.025)
        bridge._flush_trackerball_motion()
    assert window.interactions == ["start"]

    # 手势防抖到期 → 结束当前 undo 边界
    bridge._gesture_timer.timeout.emit()
    assert window.interactions == ["start", "end"]

    # 新手势 → 新的 undo 条目
    bridge._on_parameter_change(0x00310023, 0.05)
    bridge._flush_trackerball_motion()
    assert window.interactions == ["start", "end", "start"]
    bridge.shutdown()


def test_sync_from_panel_rebases_display_after_layer_switch(qapp):
    """F1.4：切层/切镜后 sync_from_panel 重推当前 grade（反馈侧 rebase）。"""
    window = _FakeMainWindow()
    window.config_manager = _FakeConfig({
        "tangent.trackerball_smoothing": 0,
    })
    bridge = TangentColorBridge(window, parent=window)
    sent = []
    bridge.client.send_parameter_value = lambda *a: sent.append(a) or True

    # 预热显示缓存（制造去重基线）
    bridge._on_parameter_change(0x00310023, 0.025)
    bridge._flush_trackerball_motion()
    sent.clear()

    # 切层后的 rebase（layer_controller 切层时调用同入口）
    bridge.sync_from_panel()
    assert len(sent) >= 1, "sync_from_panel 应重推参数到 Hub"
    bridge.shutdown()
