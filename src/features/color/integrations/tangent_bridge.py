"""Map Tangent Ripple controls onto Topos' existing grading pipeline."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import logging
import math
import time
from typing import Any

from PyQt6.QtCore import QObject, QTimer, Qt, pyqtSignal

from src.shared.control_surfaces.tangent import PanelInfo, PanelType, TangentHubClient
from src.shared.control_surfaces.tangent.settings import (
    COLOR_WHEEL_ROTATION_CONFIG_KEY,
    CONTROL_SURFACE_AUTO,
    CONTROL_SURFACE_CONFIG_KEY,
    CONTROL_SURFACE_DISABLED,
    CONTROL_SURFACE_ELEMENT,
    CONTROL_SURFACE_RIPPLE,
    DEFAULT_COLOR_WHEEL_ROTATION_DEGREES,
    DEFAULT_CONTROL_SURFACE,
    DEFAULT_TRACKERBALL_SMOOTHING,
    DEFAULT_TANGENT_SENSITIVITY,
    HUE_DIRECTION_INVERTED_CONFIG_KEY,
    TRACKERBALL_SMOOTHING_CONFIG_KEY,
    TANGENT_SENSITIVITY_MAX,
    TANGENT_SENSITIVITY_CONFIG_KEYS,
    normalize_control_surface,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _WheelParameter:
    wheel: str
    component: int
    minimum: float
    maximum: float


@dataclass
class _TrackerballPoint:
    """Current and requested Cartesian trackerball positions."""

    target_x: float = 0.0
    target_y: float = 0.0
    output_x: float = 0.0
    output_y: float = 0.0
    # Keep raw X/Y increments together until the next input frame. Tangent Hub
    # sends the two axes as separate packets, so applying each packet as a
    # color-space edit would make a diagonal gesture zig-zag through Hue/Sat.
    pending_x: float = 0.0
    pending_y: float = 0.0
    pending_axes: int = 0
    pending_started_ns: int = 0
    pending_latest_input_ns: int = 0
    last_vector_input_ns: int = 0
    # Only Hub events carrying a monotonic timestamp can use the velocity
    # response curve.  Older/direct callers remain on the legacy unity gain.
    velocity_tracking_available: bool = False
    last_flush_ns: int = 0
    last_hue: float = 0.0


# Tangent sends a trackerball's X/Y movement as independent parameter events,
# unlike a mouse HID report which carries dx/dy atomically.  Keep an incoming
# vector open for 12 ms (less than one 60 Hz display frame), then integrate all
# received axes together.  Every direction uses the same window: a cardinal
# fast path would make behavior depend on the previous gesture and split a
# diagonal into visible horizontal/vertical steps.
TRACKERBALL_VECTOR_BATCH_NS = 12_000_000
# The sensitivity preference is the maximum gain.  Start a deliberate roll at
# 35% of it (about 3.5x with the default 1000% maximum), then ease toward a
# 2x overspeed ceiling as the trackerball's physical speed reaches this
# normalized-units-per-second rate.  The higher floor keeps the control
# responsive while the curve still prevents every slow step from jumping at
# the full 20x fast-roll gain.
TRACKERBALL_MIN_SPEED_SCALE = 0.35
TRACKERBALL_FAST_SPEED_SCALE = 2.0
TRACKERBALL_SPEED_REFERENCE = 0.18
TRACKERBALL_SPEED_GAP_NS = 250_000_000
# At the current 1000% internal X/Y gain, one smallest Hub packet is about
# 0.01 normalized wheel units. A 0.02 dead zone would hide a valid first
# diagonal vector and make it look like a short cardinal move.
TRACKERBALL_HUE_HOLD_RADIUS = 0.001
TRACKERBALL_EXTRA_FILTER_SECONDS = 0.100
TANGENT_DISPLAY_DECIMALS = 2


class TangentColorBridge(QObject):
    """Connect Ripple's relative controls to ``MainWindow.on_grading_changed``.

    The bridge owns no color state.  It reads and writes the existing
    ``GradingPanel`` values and routes updates through the controller callback,
    preserving render invalidation, undo history, keyframes and persistence.
    """

    status_changed = pyqtSignal(str)

    RESET_ALL_ACTION_ID = 0x00020001

    _PARAMETERS: dict[int, _WheelParameter] = {
        0x00310021: _WheelParameter("lift", 0, -1.0, 1.0),
        0x00310023: _WheelParameter("lift", 1, -1.0, 1.0),
        0x00310024: _WheelParameter("lift", 2, -1.0, 1.0),
        0x00310031: _WheelParameter("gamma", 0, -1.0, 1.0),
        0x00310033: _WheelParameter("gamma", 1, -1.0, 1.0),
        0x00310034: _WheelParameter("gamma", 2, -1.0, 1.0),
        0x00310041: _WheelParameter("gain", 0, -1.0, 1.0),
        0x00310043: _WheelParameter("gain", 1, -1.0, 1.0),
        0x00310044: _WheelParameter("gain", 2, -1.0, 1.0),
    }

    def __init__(self, main_window: Any, parent: QObject | None = None) -> None:
        super().__init__(parent or main_window)
        self._main_window = main_window
        self._panel = main_window.grading_panel
        self._gesture_open = False
        self._active_surface = self._read_surface()
        self._sensitivity: dict[str, float] = {}
        self._last_sent_display_values: dict[int, tuple[float, bool]] = {}
        self._hue_direction = 1.0
        self._wheel_rotation_degrees = DEFAULT_COLOR_WHEEL_ROTATION_DEGREES
        self._trackerball_smoothing = DEFAULT_TRACKERBALL_SMOOTHING / 100.0
        self._trackerball_points = {
            wheel: _TrackerballPoint()
            for wheel in ("lift", "gamma", "gain")
        }
        self._flushing_trackerball = False
        self._reload_settings()

        self._assets_dir = (
            Path(__file__).resolve().parents[3]
            / "shared"
            / "control_surfaces"
            / "tangent"
            / "assets"
        )
        self.client = TangentHubClient(
            application_name="Topos Color",
            system_path=self._system_path_for_surface(self._active_surface),
            parent=self,
        )

        self._gesture_timer = QTimer(self)
        self._gesture_timer.setSingleShot(True)
        self._gesture_timer.setInterval(250)
        self._gesture_timer.timeout.connect(self._finish_gesture)

        # Hub may accept the application definition before the panel map is
        # fully loaded.  A delayed second sync makes startup deterministic and
        # removes the need for the user to jog a control once to wake it up.
        self._sync_timer = QTimer(self)
        self._sync_timer.setSingleShot(True)
        self._sync_timer.setInterval(350)
        self._sync_timer.timeout.connect(self.sync_from_panel)

        # Trackerball events can arrive as interleaved X/Y packets. Coalesce
        # active diagonal pairs and move the puck toward one Cartesian target
        # so diagonal gestures do not zig-zag between two independent updates.
        self._trackerball_timer = QTimer(self)
        self._trackerball_timer.setInterval(8)
        self._trackerball_timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._trackerball_timer.timeout.connect(self._flush_trackerball_motion)
        self._reset_trackerball_points_from_params(self._panel.get_current_params())

        self.client.connected.connect(self._on_connected)
        self.client.disconnected.connect(self._on_disconnected)
        self.client.error.connect(self._on_error)
        self.client.unavailable.connect(self._on_unavailable)
        self.client.panels_received.connect(self._on_panels_received)
        self.client.parameter_change_received_at.connect(self._on_parameter_change)
        self.client.parameter_reset_received.connect(self._on_parameter_reset)
        self.client.parameter_value_requested.connect(
            self._on_parameter_value_requested
        )
        self.client.action_on_received.connect(self._on_action_on)
        self._panel.grading_changed.connect(self._on_panel_changed)

    def start(self) -> None:
        """Start the optional Hub connection in the background."""

        if self._active_surface == CONTROL_SURFACE_DISABLED:
            self.status_changed.emit("Tangent Hub 集成已禁用")
            return
        self.client.start()
        self._schedule_sync()

    def reconfigure(self, surface: str | None = None) -> None:
        """Apply the saved surface selection without restarting Topos."""

        # Preferences can change calibration without changing the selected
        # Hub profile.  Reload these first so a dialog Apply takes effect
        # immediately and does not need a connection restart.
        self._reload_settings()
        selected_surface = normalize_control_surface(
            self._read_surface() if surface is None else surface
        )
        system_path = self._system_path_for_surface(selected_surface)
        if (
            selected_surface == self._active_surface
            and system_path == self.client.system_path
        ):
            # An unchanged Apply still counts as an explicit retry request:
            # pull a dormant background probe forward so freshly installed
            # Hub software is picked up immediately instead of within 30s.
            self.client.wake()
            self._schedule_sync()
            return

        self.client.stop()
        self._active_surface = selected_surface
        self.client.system_path = system_path
        self._last_sent_display_values.clear()
        if selected_surface == CONTROL_SURFACE_DISABLED:
            self.status_changed.emit("Tangent Hub 集成已禁用")
            return
        self.status_changed.emit("正在切换 Tangent 控制台配置…")
        self.client.start()

    def shutdown(self) -> None:
        """Stop the Hub connection and close any synthetic gesture."""

        if self._sync_timer.isActive():
            self._sync_timer.stop()
        if self._trackerball_timer.isActive():
            self._trackerball_timer.stop()
        if self._gesture_timer.isActive():
            self._gesture_timer.stop()
        if self._gesture_open:
            self._finish_gesture()
        self.client.stop()

    def sync_from_panel(self) -> None:
        """Push the current color-wheel values to Hub displays."""

        if self._active_surface == CONTROL_SURFACE_DISABLED:
            return
        params = self._panel.get_current_params()
        if not self._trackerball_timer.isActive() and not self._flushing_trackerball:
            self._reset_trackerball_points_from_params(params)
        for parameter_id, spec in self._PARAMETERS.items():
            value = self._parameter_value(params, spec)
            if value is None:
                continue
            self._send_parameter_value(
                parameter_id,
                value,
                self._is_default(value, spec),
                force=True,
            )

    def _on_connected(self) -> None:
        self.status_changed.emit("Tangent Hub 已连接，等待控制台配置")
        self._schedule_sync()

    def _on_disconnected(self) -> None:
        self.status_changed.emit("Tangent Hub 已断开，正在自动重连…")

    def _on_error(self, message: str) -> None:
        # Hub is optional: one transient notice per failure streak is enough.
        # The client keeps retrying quietly and reports ``unavailable`` if the
        # grace period expires, so this must not log at warning level.
        logger.info("Tangent Hub connection error: %s", message)
        self.status_changed.emit("Tangent Hub 未响应，正在自动重试…")

    def _on_unavailable(self) -> None:
        logger.info(
            "Tangent Hub 未安装或未运行，调色台集成进入待机（后台保持探测）"
        )
        self.status_changed.emit("未检测到 Tangent Hub，调色台集成已待机")

    def _on_panels_received(self, panels: object) -> None:
        if not isinstance(panels, tuple):
            return
        has_ripple = any(
            isinstance(panel, PanelInfo) and panel.panel_type == PanelType.RIPPLE
            for panel in panels
        )
        element_types = {
            int(PanelType.ELEMENT_TK),
            int(PanelType.ELEMENT_MF),
            int(PanelType.ELEMENT_KB),
            int(PanelType.ELEMENT_BT),
        }
        element_types_seen = {
            int(panel.panel_type)
            for panel in panels
            if isinstance(panel, PanelInfo)
        }
        has_element = element_types.issubset(element_types_seen)

        if self._active_surface == CONTROL_SURFACE_RIPPLE:
            selected_surface = CONTROL_SURFACE_RIPPLE if has_ripple else None
        elif self._active_surface == CONTROL_SURFACE_ELEMENT:
            selected_surface = CONTROL_SURFACE_ELEMENT if has_element else None
        elif has_ripple:
            # Hub reports configured panels, not physical presence. Prefer the
            # existing Ripple path for auto mode to preserve current behavior.
            selected_surface = CONTROL_SURFACE_RIPPLE
        elif has_element:
            selected_surface = CONTROL_SURFACE_ELEMENT
        else:
            selected_surface = None

        # Auto mode starts with the lightweight Ripple profile because Hub's
        # initiate message is only a configured-panel inventory. If it reports
        # an Element-only setup, switch to the Element profile and perform the
        # managed handshake again with the four-panel map set.
        if (
            self._active_surface == CONTROL_SURFACE_AUTO
            and selected_surface == CONTROL_SURFACE_ELEMENT
            and self.client.system_path != self._system_path_for_surface(
                CONTROL_SURFACE_ELEMENT
            )
        ):
            self.reconfigure(CONTROL_SURFACE_ELEMENT)
            return

        if selected_surface == CONTROL_SURFACE_RIPPLE:
            self.status_changed.emit("Tangent Ripple 已接入 Topos Color")
            self.sync_from_panel()
            self._schedule_sync()
        elif selected_surface == CONTROL_SURFACE_ELEMENT:
            self.status_changed.emit("Tangent Element 已接入 Topos Color")
            self.sync_from_panel()
            self._schedule_sync()
        else:
            label = (
                "Tangent Ripple"
                if self._active_surface == CONTROL_SURFACE_RIPPLE
                else "Tangent Element"
                if self._active_surface == CONTROL_SURFACE_ELEMENT
                else "支持的 Tangent 控制台"
            )
            self.status_changed.emit(f"Tangent Hub 已连接，未检测到{label}")

    def _read_surface(self) -> str:
        config_manager = getattr(self._main_window, "config_manager", None)
        value = (
            config_manager.get(CONTROL_SURFACE_CONFIG_KEY)
            if config_manager is not None
            else DEFAULT_CONTROL_SURFACE
        )
        return normalize_control_surface(value)

    def _reload_settings(self) -> None:
        """Reload live Tangent calibration values from the preferences."""

        config_manager = getattr(self._main_window, "config_manager", None)

        def _get(key: str, default: object) -> object:
            if config_manager is None:
                return default
            value = config_manager.get(key)
            return default if value is None else value

        for key in TANGENT_SENSITIVITY_CONFIG_KEYS:
            raw_value = _get(key, DEFAULT_TANGENT_SENSITIVITY[key])
            try:
                self._sensitivity[key] = max(
                    1.0, min(float(TANGENT_SENSITIVITY_MAX), float(raw_value))
                )
            except (TypeError, ValueError):
                self._sensitivity[key] = float(DEFAULT_TANGENT_SENSITIVITY[key])

        try:
            self._wheel_rotation_degrees = float(
                _get(
                    COLOR_WHEEL_ROTATION_CONFIG_KEY,
                    DEFAULT_COLOR_WHEEL_ROTATION_DEGREES,
                )
            ) % 360.0
        except (TypeError, ValueError):
            self._wheel_rotation_degrees = DEFAULT_COLOR_WHEEL_ROTATION_DEGREES

        try:
            self._trackerball_smoothing = max(
                0.0,
                min(
                    1.0,
                    float(
                        _get(
                            TRACKERBALL_SMOOTHING_CONFIG_KEY,
                            DEFAULT_TRACKERBALL_SMOOTHING,
                        )
                    )
                    / 100.0,
                ),
            )
        except (TypeError, ValueError):
            self._trackerball_smoothing = DEFAULT_TRACKERBALL_SMOOTHING / 100.0

        self._hue_direction = -1.0 if bool(
            _get(HUE_DIRECTION_INVERTED_CONFIG_KEY, False)
        ) else 1.0

        set_rotation = getattr(self._panel, "set_wheel_rotation", None)
        if callable(set_rotation):
            set_rotation(self._wheel_rotation_degrees)
        if hasattr(self, "_trackerball_points"):
            # Rotation changes the Cartesian basis.  Rebase queued/output
            # coordinates immediately so the next hardware packet uses the
            # new physical orientation instead of the previous calibration.
            self._reset_trackerball_points_from_params(
                self._panel.get_current_params()
            )

    def _schedule_sync(self) -> None:
        """Refresh Hub displays after its map has had time to load."""

        if self._active_surface == CONTROL_SURFACE_DISABLED:
            return
        self._sync_timer.start()

    def _system_path_for_surface(self, surface: str) -> str:
        if surface == CONTROL_SURFACE_ELEMENT:
            element_path = self._assets_dir / "element"
            if element_path.is_dir():
                return str(element_path.resolve())
        return str(self._assets_dir.resolve())

    def _on_panel_changed(self, *args: object) -> None:
        """Mirror UI changes to Hub without feeding them back into the panel."""

        if len(args) not in (12, 13):
            logger.warning("Unexpected grading signal length from panel: %d", len(args))
            return
        values = self._values_from_grading_args(args)
        if values is None:
            return
        if self._flushing_trackerball:
            # ``_flush_trackerball_motion`` mirrors only the active wheel and
            # performs a narrow three-parameter Hub sync itself.  Re-running
            # this all-wheels loop here turns one ball movement into nine
            # socket writes and can starve the next incoming motion packet.
            return
        # A normal UI edit is the new source of truth.  Do not let an old
        # queued hardware target overwrite it on the next timer tick.
        self._reset_trackerball_points_from_params(values)
        for parameter_id, spec in self._PARAMETERS.items():
            value = self._parameter_value(values, spec)
            if value is None:
                continue
            self._send_parameter_value(
                parameter_id,
                value,
                self._is_default(value, spec),
            )

    def _on_parameter_change(
        self,
        parameter_id: int,
        increment: float,
        timestamp_ns: int | None = None,
    ) -> None:
        spec = self._PARAMETERS.get(parameter_id)
        if spec is None:
            logger.debug("Ignoring unsupported Tangent parameter 0x%08X", parameter_id)
            return

        params = self._panel.get_current_params()
        if spec.component in (0, 1):
            # Hub sends X and Y in separate packets.  Accumulate the packets
            # into a short display-frame vector before applying them, rather
            # than making two successive polar edits.
            if (
                not self._trackerball_timer.isActive()
                and not self._flushing_trackerball
                and not self._trackerball_state_matches_params(params)
            ):
                # A project/layer can load the grading panel with emit=False.
                # Rebase before the first hardware delta so an old project's
                # cached origin cannot reset the newly opened wheel.
                self._reset_trackerball_points_from_params(params)
            state = self._trackerball_points[spec.wheel]
            # Use the UI-thread arrival time for batching.  Queued Qt signals
            # can reach this slot in a burst after the UI has been busy
            # rendering; batching from delivery preserves that burst as one
            # vector instead of flushing its first stale axis immediately.
            arrival_ns = time.monotonic_ns()
            if state.pending_started_ns == 0:
                state.pending_started_ns = arrival_ns
            raw_delta = self._normalize_axis_increment(float(increment))
            if self._hue_direction < 0:
                raw_delta *= -1.0
            self._record_trackerball_input_time(
                state,
                timestamp_ns=timestamp_ns,
            )
            if spec.component == 0:
                state.pending_x += raw_delta
                state.pending_axes |= 1
            else:
                state.pending_y += raw_delta
                state.pending_axes |= 2
            self._begin_gesture()
            if not self._trackerball_timer.isActive():
                self._trackerball_timer.start()
            return
        else:
            # AUD-24501: 环/亮度分量此前直接 float(params[...][...])，面板
            # 数据契约被破坏时异常会从 Hub 驱动 slot 逃逸进 Qt 事件循环
            # （PyQt6 默认 qFatal 终止进程）。与其他分量一致做契约校验，
            # 坏包记 warning 后丢弃。
            current_wheel = self._wheel_value_or_none(params, spec.wheel)
            if current_wheel is None:
                logger.warning(
                    "Dropping Tangent parameter change for %s: invalid wheel "
                    "value %r", spec.wheel, params.get(spec.wheel))
                return
            current = current_wheel[spec.component]
            delta = float(increment) * self._sensitivity_for(spec)
            value = max(spec.minimum, min(spec.maximum, current + delta))
            params[spec.wheel] = self._updated_wheel(
                current_wheel, spec.component, value
            )

        self._begin_gesture()
        # AUD-24500: 环/亮度调节经 _apply_params(emit=True) 同步触发
        # grading_changed → _on_panel_changed，其重置会无条件清空在途
        # trackerball 批处理与平滑状态（Ripple 上"转环的同时滚球"是常规
        # 手势，每个环包至多丢一批球增量并打断速度曲线）。与球路径的
        # _flush_trackerball_motion 对齐：发布期间置 _flushing_trackerball
        # 让 _on_panel_changed 跳过重置；Hub 显示由紧随其后的
        # _send_parameter_value 窄同步完成（本路径只改了这一个参数）。
        self._flushing_trackerball = True
        try:
            self._apply_params(params)
        finally:
            self._flushing_trackerball = False
        self._send_parameter_value(
            parameter_id,
            value,
            self._is_default(value, spec),
        )

    def _reset_trackerball_points_from_params(self, params: dict[str, Any]) -> None:
        """Align smoothing state with the panel's current HSV wheel values."""

        for wheel_name, state in self._trackerball_points.items():
            wheel = params.get(wheel_name)
            if not isinstance(wheel, (tuple, list)) or len(wheel) != 3:
                continue
            try:
                hue = float(wheel[0])
                saturation = max(0.0, min(1.0, float(wheel[1])))
            except (TypeError, ValueError):
                continue
            angle = math.radians((hue + self._wheel_rotation_degrees) % 360.0)
            state.target_x = state.output_x = saturation * math.cos(angle)
            state.target_y = state.output_y = saturation * math.sin(angle)
            state.pending_x = 0.0
            state.pending_y = 0.0
            state.pending_axes = 0
            state.pending_started_ns = 0
            state.pending_latest_input_ns = 0
            state.last_vector_input_ns = 0
            state.velocity_tracking_available = False
            state.last_flush_ns = 0
            state.last_hue = hue % 360.0

    def _trackerball_state_matches_params(self, params: dict[str, Any]) -> bool:
        """Return whether cached Cartesian points reflect the visible wheels."""

        for wheel_name, state in self._trackerball_points.items():
            wheel = params.get(wheel_name)
            if not isinstance(wheel, (tuple, list)) or len(wheel) != 3:
                continue
            try:
                hue = float(wheel[0])
                saturation = max(0.0, min(1.0, float(wheel[1])))
            except (TypeError, ValueError):
                continue
            angle = math.radians((hue + self._wheel_rotation_degrees) % 360.0)
            expected_x = saturation * math.cos(angle)
            expected_y = saturation * math.sin(angle)
            if (
                abs(state.target_x - expected_x) > 1e-5
                or abs(state.target_y - expected_y) > 1e-5
                or abs(state.output_x - expected_x) > 1e-5
                or abs(state.output_y - expected_y) > 1e-5
            ):
                return False
        return True

    @staticmethod
    def _clamp_trackerball_target(state: _TrackerballPoint) -> None:
        radius = math.hypot(state.target_x, state.target_y)
        if radius <= 1.0:
            return
        scale = 1.0 / radius
        state.target_x *= scale
        state.target_y *= scale

    @staticmethod
    def _record_trackerball_input_time(
        state: _TrackerballPoint,
        *,
        timestamp_ns: int | None,
    ) -> None:
        """Record source timing for one direction-neutral vector batch."""

        if timestamp_ns is None:
            # The real Hub client always supplies its receive timestamp. Keep
            # direct callers and older integrations on the legacy unity gain
            # instead of guessing a physical speed from an un-timestamped
            # delta.
            state.velocity_tracking_available = False
            return

        try:
            event_ns = int(timestamp_ns)
        except (TypeError, ValueError):
            event_ns = 0
        if event_ns <= 0:
            state.velocity_tracking_available = False
            return

        state.velocity_tracking_available = True
        state.pending_latest_input_ns = max(
            state.pending_latest_input_ns,
            event_ns,
        )

    @staticmethod
    def _consume_trackerball_vector_speed(
        state: _TrackerballPoint,
    ) -> float | None:
        """Measure one batch radially so gain is invariant to direction."""

        if not state.velocity_tracking_available:
            return None
        event_ns = state.pending_latest_input_ns
        previous_ns = state.last_vector_input_ns
        state.last_vector_input_ns = max(previous_ns, event_ns)
        state.pending_latest_input_ns = 0
        if previous_ns <= 0 or event_ns <= previous_ns:
            return 0.0
        elapsed_ns = event_ns - previous_ns
        if elapsed_ns > TRACKERBALL_SPEED_GAP_NS:
            return 0.0
        return math.hypot(state.pending_x, state.pending_y) / (
            elapsed_ns / 1_000_000_000.0
        )

    @staticmethod
    def _trackerball_velocity_scale(raw_speed: float) -> float:
        """Map physical trackerball speed to a smooth 35%-to-200% gain."""

        normalized_speed = max(
            0.0,
            min(1.0, float(raw_speed) / TRACKERBALL_SPEED_REFERENCE),
        )
        # Cubic smoothstep has a flat, precise low-speed region and reaches
        # the configured maximum without a sudden acceleration knee.
        eased_speed = normalized_speed * normalized_speed * (
            3.0 - 2.0 * normalized_speed
        )
        return TRACKERBALL_MIN_SPEED_SCALE + (
            (TRACKERBALL_FAST_SPEED_SCALE - TRACKERBALL_MIN_SPEED_SCALE)
            * eased_speed
        )

    def _trackerball_axis_sensitivity(self, wheel_name: str, axis: int) -> float:
        """Return the configured maximum X or Y trackerball gain."""

        component = ("hue", "saturation")[axis]
        key = f"tangent.{wheel_name}_{component}_sensitivity"
        return self._sensitivity.get(
            key,
            float(DEFAULT_TANGENT_SENSITIVITY.get(key, 100)),
        ) / 100.0

    def _wheel_from_trackerball_point(
        self,
        wheel: object,
        x: float,
        y_up: float,
        state: _TrackerballPoint | None = None,
    ) -> tuple[float, float, float]:
        if not isinstance(wheel, (tuple, list)) or len(wheel) != 3:
            raise ValueError("invalid grading wheel value")
        current_hue, _, brightness = (float(item) for item in wheel)
        radius = min(1.0, math.hypot(x, y_up))
        if radius <= 1e-9:
            return current_hue, 0.0, brightness
        # Hue is mathematically undefined at the center. Holding the last
        # valid angle through this tiny radius prevents atan2() from turning
        # sub-pixel X/Y noise into a visible color jump.
        if radius < TRACKERBALL_HUE_HOLD_RADIUS and state is not None:
            return state.last_hue, radius, brightness
        hue = (
            math.degrees(math.atan2(y_up, x))
            - self._wheel_rotation_degrees
        ) % 360.0
        if state is not None:
            state.last_hue = hue
        return hue, radius, brightness

    @staticmethod
    def _filter_alpha(
        elapsed_seconds: float,
        smoothing: float,
    ) -> float:
        """Return a frame-rate-independent Cartesian filter coefficient."""

        # Cartesian packet pairing is handled by the pending X/Y hold above.
        # Do not add a hidden diagonal-only filter: it makes diagonal travel
        # slower than straight travel even when the preference is 0%.
        time_constant = TRACKERBALL_EXTRA_FILTER_SECONDS * smoothing
        if time_constant <= 0.0:
            return 1.0
        return 1.0 - math.exp(-elapsed_seconds / time_constant)

    def _flush_trackerball_motion(self) -> None:
        """Apply one smoothed Cartesian trackerball frame to the panel."""

        params = self._panel.get_current_params()
        now_ns = time.monotonic_ns()
        any_change = False
        pending = False
        changed_wheels: set[str] = set()

        for wheel_name, state in self._trackerball_points.items():
            pending_age_ns = (
                now_ns - state.pending_started_ns
                if state.pending_started_ns
                else 0
            )
            # Every timestamped vector uses the same fixed input window. This
            # caps UI/render publication rate equally for cardinal and
            # diagonal motion while reconstructing interleaved X/Y packets as
            # one Cartesian sample.
            hold_vector_batch = (
                state.pending_axes != 0
                and state.velocity_tracking_available
                and pending_age_ns < TRACKERBALL_VECTOR_BATCH_NS
            )
            if state.pending_axes and not hold_vector_batch:
                raw_speed = self._consume_trackerball_vector_speed(state)
                speed_scale = (
                    self._trackerball_velocity_scale(raw_speed)
                    if raw_speed is not None
                    else 1.0
                )
                state.target_x += (
                    state.pending_x
                    * self._trackerball_axis_sensitivity(wheel_name, 0)
                    * speed_scale
                )
                state.target_y += (
                    state.pending_y
                    * self._trackerball_axis_sensitivity(wheel_name, 1)
                    * speed_scale
                )
                state.pending_x = 0.0
                state.pending_y = 0.0
                state.pending_axes = 0
                state.pending_started_ns = 0
                self._clamp_trackerball_target(state)
            elif hold_vector_batch:
                pending = True

            if state.last_flush_ns:
                elapsed_seconds = min(
                    0.1,
                    max(0.001, (now_ns - state.last_flush_ns) / 1_000_000_000.0),
                )
            else:
                elapsed_seconds = 0.016
            state.last_flush_ns = now_ns

            dx = state.target_x - state.output_x
            dy = state.target_y - state.output_y
            distance = math.hypot(dx, dy)
            alpha = self._filter_alpha(
                elapsed_seconds,
                self._trackerball_smoothing,
            )
            if distance <= 1e-4:
                # Snapping a tiny but real delta still changes the published
                # grading value.  Without this flag the timer stopped before
                # the panel saw it, and a later rebase discarded the motion.
                any_change = any_change or distance > 0.0
                state.output_x = state.target_x
                state.output_y = state.target_y
            else:
                state.output_x += dx * alpha
                state.output_y += dy * alpha
                any_change = True

            if math.hypot(
                state.target_x - state.output_x,
                state.target_y - state.output_y,
            ) > 1e-4:
                pending = True

            wheel = params.get(wheel_name)
            try:
                updated = self._wheel_from_trackerball_point(
                    wheel,
                    state.output_x,
                    state.output_y,
                    state,
                )
            except (TypeError, ValueError, IndexError):
                # AUD-24501: 8ms timer slot 内不允许异常逃逸（qFatal）；
                # 坏轮值只跳过该轮的发布，保底保留面板原值。
                logger.warning(
                    "Skipping trackerball frame for wheel %r: invalid panel "
                    "value %r", wheel_name, wheel)
                continue
            if wheel != updated:
                params[wheel_name] = updated
                changed_wheels.add(wheel_name)

        if any_change and changed_wheels:
            self._flushing_trackerball = True
            try:
                self._apply_trackerball_params(params, changed_wheels)
            finally:
                self._flushing_trackerball = False
            self._sync_trackerball_wheels(params, changed_wheels)

        if pending:
            # Keep the undo gesture open until the filtered tail settles.
            self._gesture_timer.start()
        else:
            self._trackerball_timer.stop()

    def _on_parameter_reset(self, parameter_id: int) -> None:
        spec = self._PARAMETERS.get(parameter_id)
        if spec is None:
            return
        params = self._panel.get_current_params()
        # AUD-24501: slot 内不再 raise——异常逃逸进 Qt 事件循环会按
        # PyQt6 默认行为 qFatal 终止整个应用；坏数据降级为 warning +
        # 丢弃该事件包（与 _values_from_grading_args 的降级风格一致）。
        current_wheel = self._wheel_value_or_none(params, spec.wheel)
        if current_wheel is None:
            logger.warning(
                "Dropping Tangent parameter reset for %s: invalid wheel "
                "value %r", spec.wheel, params.get(spec.wheel))
            return
        if spec.component in (0, 1):
            # Resetting either ball axis resets the whole 2D color differential
            # while preserving the independent master/ring brightness value.
            params[spec.wheel] = (current_wheel[0], 0.0, current_wheel[2])
        else:
            params[spec.wheel] = self._updated_wheel(
                current_wheel, spec.component, 0.0
            )
        self._begin_gesture()
        self._apply_params(params)
        self._send_parameter_value(
            parameter_id,
            0.0,
            at_default=True,
            force=True,
        )

    def _on_parameter_value_requested(self, parameter_id: int) -> None:
        spec = self._PARAMETERS.get(parameter_id)
        if spec is None:
            return
        params = self._panel.get_current_params()
        value = self._parameter_value(params, spec)
        if value is None:
            return
        self._send_parameter_value(
            parameter_id,
            value,
            self._is_default(value, spec),
            force=True,
        )

    def _on_action_on(self, action_id: int) -> None:
        if action_id != self.RESET_ALL_ACTION_ID:
            return
        self._panel.reset_all()

    def _begin_gesture(self) -> None:
        if not self._gesture_open:
            self._gesture_open = True
            self._main_window.on_interaction_started()
        self._gesture_timer.start()

    def _finish_gesture(self) -> None:
        if not self._gesture_open:
            return
        self._gesture_open = False
        self._main_window.on_interaction_ended()

    def _apply_params(self, params: dict[str, Any]) -> None:
        self._panel.set_params(
            params["lift"],
            params["gamma"],
            params["gain"],
            params["exposure"],
            params["temp"],
            params["tint"],
            params["contrast"],
            params["shadow"],
            params["saturation"],
            params.get("vibrance", 0.0),
            params.get("mid_detail", 0.0),
            params.get("hue_shift", 0.0),
            # Reuse the existing signal fan-out so the normal controller,
            # ColorStack persistence and undo integration all observe the
            # hardware edit exactly like a UI edit.
            emit=True,
            **({"highlights": params["highlights"]} if "highlights" in params else {}),
        )

    def _apply_trackerball_params(
        self,
        params: dict[str, Any],
        changed_wheels: set[str],
    ) -> None:
        """Publish hardware motion without rewriting unrelated wheel widgets."""

        if len(changed_wheels) == 1:
            wheel_name = next(iter(changed_wheels))
            set_wheel_params = getattr(self._panel, "set_wheel_params", None)
            if callable(set_wheel_params):
                set_wheel_params(wheel_name, params[wheel_name], emit=True)
                return
        self._apply_params(params)

    def _sync_trackerball_wheels(
        self,
        params: dict[str, Any],
        changed_wheels: set[str],
    ) -> None:
        """Mirror only the actively moved wheels back to Tangent Hub."""

        for parameter_id, spec in self._PARAMETERS.items():
            if spec.wheel not in changed_wheels:
                continue
            value = self._parameter_value(params, spec)
            if value is None:
                continue
            self._send_parameter_value(
                parameter_id,
                value,
                self._is_default(value, spec),
            )

    def _sensitivity_for(self, spec: _WheelParameter) -> float:
        component_name = ("hue", "saturation", "brightness")[spec.component]
        key = f"tangent.{spec.wheel}_{component_name}_sensitivity"
        return self._sensitivity.get(
            key,
            float(DEFAULT_TANGENT_SENSITIVITY.get(key, 100)),
        ) / 100.0

    def _send_parameter_value(
        self,
        parameter_id: int,
        value: float,
        at_default: bool,
        *,
        force: bool = False,
    ) -> bool:
        """Send a two-decimal display value without quantizing app state."""

        display_value = round(float(value), TANGENT_DISPLAY_DECIMALS)
        cache_key = (display_value, bool(at_default))
        if (
            not force
            and self._last_sent_display_values.get(parameter_id) == cache_key
        ):
            return True

        sent = self.client.send_parameter_value(
            parameter_id,
            display_value,
            bool(at_default),
        )
        if sent:
            self._last_sent_display_values[parameter_id] = cache_key
        return bool(sent)

    @staticmethod
    def _normalize_axis_increment(increment: float) -> float:
        """Convert Hub movement counts to normalized trackerball travel.

        Tangent's test mode reports the movement amount as a count-like value,
        while the application mapping uses a -1.0..1.0 normalized coordinate.
        Keep already-normalized fractional values intact for compatibility,
        but treat an exact 1-count packet as a count too.  The old strict
        ``> 1.0`` check turned that very common packet into a full-screen jump.
        """

        value = float(increment)
        if abs(value) >= 1.0:
            return value / 100.0
        return value

    def _parameter_value(
        self,
        params: dict[str, Any],
        spec: _WheelParameter,
    ) -> float | None:
        """Return the Hub display value for ``spec``; ``None`` on bad data.

        AUD-24501: 调用方均为 Hub 驱动 slot（面板信号 / 定时器 / Hub 请求），
        数据契约被破坏时不再 raise（qFatal 风险），改为返回 None 由调用
        方跳过该参数的 Hub 同步。
        """
        wheel = self._wheel_value_or_none(params, spec.wheel)
        if wheel is None:
            logger.warning(
                "Skipping Hub sync for %s: invalid wheel value %r",
                spec.wheel, params.get(spec.wheel))
            return None
        if spec.component == 2:
            return wheel[2]

        hue, saturation = wheel[0], wheel[1]
        angle = math.radians((hue + self._wheel_rotation_degrees) % 360.0)
        if spec.component == 0:
            value = saturation * math.cos(angle)
        else:
            value = saturation * math.sin(angle)
        return value * self._hue_direction

    @staticmethod
    def _updated_wheel(
        wheel: object,
        component: int,
        value: float,
    ) -> tuple[float, float, float]:
        if not isinstance(wheel, (tuple, list)) or len(wheel) != 3:
            raise ValueError("invalid grading wheel value")
        updated = [float(item) for item in wheel]
        updated[component] = value
        return tuple(updated)  # type: ignore[return-value]

    @staticmethod
    def _wheel_value_or_none(
        params: dict[str, Any],
        wheel_name: str,
    ) -> tuple[float, float, float] | None:
        """Validate a grading wheel triple against the panel contract.

        AUD-24501: Hub 驱动 slot 的统一入口校验——坏值（长度错、非数值）
        返回 None，由调用方记 warning 并丢弃事件包，替代原先的 raise。
        """
        wheel = params.get(wheel_name)
        if not isinstance(wheel, (tuple, list)) or len(wheel) != 3:
            return None
        try:
            return (float(wheel[0]), float(wheel[1]), float(wheel[2]))
        except (TypeError, ValueError):
            return None

    @classmethod
    def _values_from_grading_args(
        cls,
        args: tuple[object, ...],
    ) -> dict[str, Any] | None:
        try:
            wheels = [args[0], args[1], args[2]]
            if not all(isinstance(wheel, (tuple, list)) and len(wheel) == 3 for wheel in wheels):
                return None
            return {
                "lift": tuple(float(value) for value in wheels[0]),
                "gamma": tuple(float(value) for value in wheels[1]),
                "gain": tuple(float(value) for value in wheels[2]),
                "exposure": float(args[3]),
                "temp": float(args[4]),
                "tint": float(args[5]),
                "contrast": float(args[6]),
                "shadow": float(args[7]),
                "saturation": float(args[8]),
                "vibrance": float(args[9]),
                "mid_detail": float(args[10]),
                "hue_shift": float(args[11]),
            }
        except (TypeError, ValueError, IndexError):
            logger.exception("Invalid grading values received from panel")
            return None

    @staticmethod
    def _is_default(value: float, spec: _WheelParameter) -> bool:
        return abs(value) < 1e-6 or (
            spec.component == 0 and abs(value) < 1e-6
        )
