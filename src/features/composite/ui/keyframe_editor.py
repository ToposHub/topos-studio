"""
Keyframe Editor Widget - UI for editing parameter animation keyframes.

Provides a timeline-based keyframe editor with:
- Keyframe diamond markers on timeline
- Curve view for value visualization
- Add/remove/edit keyframes
- Interpolation type selection

Integration:
- Used by NodePropertiesPanel for animated parameters
- Emits keyframe_changed signal when keyframes are modified
- Syncs with current frame from video control panel
"""

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel,     QComboBox, QSpinBox,
    QDoubleSpinBox, QCheckBox, )
from PyQt6.QtCore import Qt, pyqtSignal, QPointF
from PyQt6.QtGui import (
    QColor, QPainter, QPen, QBrush, QPainterPath, QFont,
    QPolygonF, QMouseEvent, )
from contextlib import contextmanager
from typing import Any, Iterator, Optional, List
from dataclasses import dataclass, field

from src.features.composite.utils.keyframe import Keyframe, KeyframeTrack, InterpolationType
from src.shared.i18n.i18n import I18N


# ── Constants ──

KEYFRAME_SIZE = 8  # Diamond size in pixels
HIT_RADIUS = 12    # Hit test radius
TIMELINE_HEIGHT = 40  # Timeline row height
_PLAYHEAD_HIT = 6  # P1: grab radius (px) around the playhead for seeking
CURVE_HEIGHT = 120  # Curve editor height
MIN_FRAME_WIDTH = 4  # Minimum pixels per frame


@dataclass
class KeyframeEditorTheme:
    """Theme colors for keyframe editor."""
    background: QColor = field(default_factory=lambda: QColor("#2a2a2a"))
    timeline_bg: QColor = field(default_factory=lambda: QColor("#1e1e1e"))
    grid_line: QColor = field(default_factory=lambda: QColor("#444444"))
    playhead: QColor = field(default_factory=lambda: QColor("#ff4444"))
    keyframe_normal: QColor = field(default_factory=lambda: QColor("#cc6600"))
    keyframe_selected: QColor = field(default_factory=lambda: QColor("#ff8800"))
    keyframe_hover: QColor = field(default_factory=lambda: QColor("#ffaa44"))
    curve_line: QColor = field(default_factory=lambda: QColor("#cc6600"))
    curve_area: QColor = field(default_factory=lambda: QColor("#3a3a3a"))
    text: QColor = field(default_factory=lambda: QColor("#888888"))
    text_highlight: QColor = field(default_factory=lambda: QColor("#ffffff"))


class KeyframeTimelineWidget(QWidget):
    """
    Timeline widget showing keyframes as diamond markers.

    Features:
    - Horizontal timeline with frame markers
    - Diamond-shaped keyframe icons
    - Click to add, double-click to remove
    - Drag to move keyframes
    - Current frame (playhead) indicator
    """

    # Signals
    keyframe_added = pyqtSignal(int, float)  # frame, value
    keyframe_removed = pyqtSignal(int)  # frame
    keyframe_moved = pyqtSignal(int, int, float)  # old_frame, new_frame, value
    keyframe_selected = pyqtSignal(int)  # frame
    frame_clicked = pyqtSignal(int)  # frame (for seeking)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._track: Optional[KeyframeTrack] = None
        self._total_frames: int = 100
        self._current_frame: int = 0
        self._selected_frame: int = -1  # No selection
        self._hover_frame: int = -1
        self._dragging: bool = False
        self._drag_start_frame: int = -1
        self._drag_start_pos: QPointF = QPointF()
        # P1: playhead (current-frame indicator) drag state. The playhead used
        # to be a read-only marker that could only move via set_current_frame;
        # grabbing and dragging it now seeks the host viewport live.
        self._playhead_dragging: bool = False

        self._theme = KeyframeEditorTheme()
        self.setMinimumHeight(TIMELINE_HEIGHT)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setCursor(Qt.CursorShape.CrossCursor)

    def set_track(self, track: Optional[KeyframeTrack]) -> None:
        """Set the keyframe track to display."""
        self._track = track
        self._selected_frame = -1
        self.update()

    def set_total_frames(self, total: int) -> None:
        """Set total number of frames in timeline."""
        self._total_frames = max(1, total)
        self.update()

    def set_current_frame(self, frame: int) -> None:
        """Set current frame (playhead position)."""
        self._current_frame = max(0, min(frame, self._total_frames - 1))
        self.update()

    def _seek_to_x(self, x: float) -> int:
        """P1: move the playhead to the frame under pixel ``x`` and emit it.

        Used by playhead drag + click-to-seek so the host viewport follows.
        Returns the clamped integer frame the playhead moved to.
        """
        new_frame = self._x_to_frame(x)
        if new_frame != self._current_frame:
            self._current_frame = new_frame
            self.update()
        self.frame_clicked.emit(new_frame)
        return new_frame

    def selected_keyframe(self) -> Optional[Keyframe]:
        """Get the currently selected keyframe."""
        if self._track and self._selected_frame >= 0:
            return self._track.get_keyframe_at(self._selected_frame)
        return None

    def _frame_to_x(self, frame: int) -> float:
        """Convert frame number to x coordinate."""
        w = self.width()
        if self._total_frames <= 1:
            return w / 2
        return (frame / (self._total_frames - 1)) * w

    def _x_to_frame(self, x: float) -> int:
        """Convert x coordinate to frame number."""
        w = self.width()
        if w <= 0:
            return 0
        frame = int((x / w) * (self._total_frames - 1))
        return max(0, min(frame, self._total_frames - 1))

    def _find_keyframe_at(self, pos: QPointF) -> int:
        """Find keyframe at given position, return frame number or -1."""
        if not self._track:
            return -1
        for kf in self._track.keyframes:
            x = self._frame_to_x(kf.frame)
            y = self.height() / 2
            # Diamond hit test
            if abs(pos.x() - x) < HIT_RADIUS and abs(pos.y() - y) < HIT_RADIUS:
                return kf.frame
        return -1

    def paintEvent(self, event) -> None:
        """Paint the timeline."""
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        rect = self.rect()
        h = rect.height()

        # Background
        painter.fillRect(rect, self._theme.timeline_bg)

        # Grid lines (frame markers)
        painter.setPen(QPen(self._theme.grid_line, 1))
        frame_step = max(1, self._total_frames // 10)
        for frame in range(0, self._total_frames, frame_step):
            x = self._frame_to_x(frame)
            painter.drawLine(int(x), 0, int(x), h)

        # Draw keyframes as diamonds
        if self._track:
            for kf in self._track.keyframes:
                x = self._frame_to_x(kf.frame)
                y = h / 2

                # Determine color based on state
                if kf.frame == self._selected_frame:
                    color = self._theme.keyframe_selected
                elif kf.frame == self._hover_frame:
                    color = self._theme.keyframe_hover
                else:
                    color = self._theme.keyframe_normal

                # Draw diamond shape
                diamond = QPolygonF([
                    QPointF(x, y - KEYFRAME_SIZE),
                    QPointF(x + KEYFRAME_SIZE, y),
                    QPointF(x, y + KEYFRAME_SIZE),
                    QPointF(x - KEYFRAME_SIZE, y),
                ])
                painter.setBrush(QBrush(color))
                painter.setPen(QPen(self._theme.text_highlight, 1))
                painter.drawPolygon(diamond)

        # Draw playhead (current frame indicator)
        playhead_x = self._frame_to_x(self._current_frame)
        painter.setPen(QPen(self._theme.playhead, 2))
        painter.drawLine(int(playhead_x), 0, int(playhead_x), h)

        # Draw frame number labels
        painter.setPen(QPen(self._theme.text, 1))
        painter.setFont(QFont("monospace", 8))
        for frame in range(0, self._total_frames, max(1, self._total_frames // 5)):
            x = self._frame_to_x(frame)
            painter.drawText(int(x - 15), h - 5, f"F{frame}")

    def mousePressEvent(self, event: QMouseEvent) -> None:
        """Handle mouse press - select/add keyframe, or grab the playhead."""
        pos = event.position()
        frame = self._find_keyframe_at(pos)

        if frame >= 0:
            # Select existing keyframe
            self._selected_frame = frame
            self._dragging = True
            self._drag_start_frame = frame
            self._drag_start_pos = pos
            self.keyframe_selected.emit(frame)
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
        else:
            # P1: if the press lands on/near the playhead, grab it for seeking
            # instead of adding a keyframe. Detect before the add-keyframe
            # branch so clicking the playhead never spawns a stray keyframe.
            playhead_x = self._frame_to_x(self._current_frame)
            if abs(pos.x() - playhead_x) <= _PLAYHEAD_HIT:
                self._playhead_dragging = True
                self._seek_to_x(pos.x())
                self.update()
                return
            # Add new keyframe at clicked position
            new_frame = self._x_to_frame(pos.x())
            if self._track:
                # Get value at this frame (use current value or interpolated)
                existing_kf = self._track.get_keyframe_at(new_frame)
                if existing_kf is None:
                    # Calculate value from interpolation
                    default_val = 0.0
                    if self._track.keyframes:
                        default_val = self._track.get_value(new_frame)
                    self.keyframe_added.emit(new_frame, default_val)
            self.frame_clicked.emit(new_frame)

        self.update()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        """Handle mouse move - drag keyframe, drag playhead, or update hover."""
        pos = event.position()

        # P1: playhead drag takes precedence — seek the host viewport live.
        if self._playhead_dragging:
            self._seek_to_x(pos.x())
            return

        if self._dragging and self._selected_frame >= 0:
            # Drag selected keyframe
            new_frame = self._x_to_frame(pos.x())
            # Clamp to valid range, avoid collision with other keyframes
            if self._track:
                # Find nearest non-colliding frame
                new_frame = self._find_free_frame(new_frame, self._selected_frame)
                if new_frame != self._selected_frame:
                    # Move keyframe
                    kf = self._track.get_keyframe_at(self._selected_frame)
                    if kf:
                        self.keyframe_moved.emit(self._selected_frame, new_frame, kf.value)
                        self._selected_frame = new_frame
        else:
            # Update hover state
            hover_frame = self._find_keyframe_at(pos)
            if hover_frame != self._hover_frame:
                self._hover_frame = hover_frame
                self.setCursor(
                    Qt.CursorShape.ClosedHandCursor if hover_frame >= 0
                    else Qt.CursorShape.CrossCursor
                )
                self.update()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        """Handle mouse release - finish drag."""
        self._dragging = False
        self._playhead_dragging = False
        self.setCursor(Qt.CursorShape.CrossCursor)

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:
        """Handle double click - remove keyframe."""
        pos = event.position()
        frame = self._find_keyframe_at(pos)

        if frame >= 0 and self._track:
            # Remove keyframe (but keep at least 2 for valid animation)
            if len(self._track.keyframes) > 2:
                self.keyframe_removed.emit(frame)
                self._selected_frame = -1
                self.update()

    def find_free_frame(self, target_frame: int, current_frame: int) -> int:
        """Public collision-aware frame lookup for editor-side moves.

        AUD-10003: the frame spinbox moves a keyframe programmatically and
        must obey the same "one keyframe per frame per track" invariant as a
        timeline drag (which uses :meth:`_find_free_frame` internally).
        """
        return self._find_free_frame(int(target_frame), int(current_frame))

    def _find_free_frame(self, target_frame: int, current_frame: int) -> int:
        """Find nearest frame that doesn't collide with existing keyframes."""
        if not self._track:
            return target_frame

        # Get frames of existing keyframes (excluding current)
        existing_frames = set(kf.frame for kf in self._track.keyframes)
        existing_frames.discard(current_frame)

        # If target is free, use it
        if target_frame not in existing_frames:
            return target_frame

        # Find nearest free frame
        direction = 1 if target_frame > current_frame else -1
        for offset in range(1, self._total_frames):
            candidate = target_frame + offset * direction
            if 0 <= candidate < self._total_frames and candidate not in existing_frames:
                return candidate
            # Also check opposite direction
            candidate = target_frame - offset * direction
            if 0 <= candidate < self._total_frames and candidate not in existing_frames:
                return candidate

        return current_frame  # Can't find free frame, stay at current


class KeyframeCurveWidget(QWidget):
    """
    Curve editor widget showing keyframe values over time.

    Features:
    - Graph of parameter value vs frame
    - Shows interpolation curve
    - Can edit keyframe values by dragging vertically
    """

    # Signals
    keyframe_value_changed = pyqtSignal(int, float)  # frame, new_value

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._track: Optional[KeyframeTrack] = None
        self._total_frames: int = 100
        self._current_frame: int = 0
        self._selected_frame: int = -1
        self._hover_frame: int = -1
        self._dragging: bool = False
        self._drag_start_value: float = 0.0

        self._theme = KeyframeEditorTheme()
        self.setMinimumHeight(CURVE_HEIGHT)
        self.setMouseTracking(True)
        self.setCursor(Qt.CursorShape.CrossCursor)

    def set_track(self, track: Optional[KeyframeTrack]) -> None:
        """Set the keyframe track to display."""
        self._track = track
        self.update()

    def set_total_frames(self, total: int) -> None:
        """Set total number of frames."""
        self._total_frames = max(1, total)
        self.update()

    def set_current_frame(self, frame: int) -> None:
        """Set current frame."""
        self._current_frame = max(0, min(frame, self._total_frames - 1))
        self.update()

    def set_selected_frame(self, frame: int) -> None:
        """Set selected keyframe frame (synced from timeline)."""
        self._selected_frame = frame
        self.update()

    def _frame_to_x(self, frame: int) -> float:
        """Convert frame to x coordinate."""
        w = self.width()
        if self._total_frames <= 1:
            return w / 2
        return (frame / (self._total_frames - 1)) * w

    def _x_to_frame(self, x: float) -> int:
        """Convert x to frame."""
        w = self.width()
        if w <= 0:
            return 0
        return int((x / w) * (self._total_frames - 1))

    def _value_to_y(self, value: float) -> float:
        """Convert parameter value to y coordinate."""
        h = self.height()
        if not self._track:
            return h / 2
        # Normalize value to 0-1 range
        range_val = self._track.max_val - self._track.min_val
        if range_val <= 0:
            return h / 2
        normalized = (value - self._track.min_val) / range_val
        # Flip Y (0 at bottom, 1 at top)
        return h * (1.0 - normalized)

    def _y_to_value(self, y: float) -> float:
        """Convert y coordinate to parameter value."""
        h = self.height()
        if not self._track or h <= 0:
            return 0.0
        # Flip Y and normalize
        normalized = 1.0 - (y / h)
        range_val = self._track.max_val - self._track.min_val
        return self._track.min_val + normalized * range_val

    def paintEvent(self, event) -> None:
        """Paint the curve editor."""
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        rect = self.rect()

        # Background
        painter.fillRect(rect, self._theme.curve_area)

        # Grid lines
        painter.setPen(QPen(self._theme.grid_line, 1, Qt.PenStyle.DotLine))
        # Vertical (frame markers)
        for frame in range(0, self._total_frames, max(1, self._total_frames // 10)):
            x = self._frame_to_x(frame)
            painter.drawLine(int(x), 0, int(x), rect.height())
        # Horizontal (value markers)
        if self._track:
            range_val = self._track.max_val - self._track.min_val
            for i in range(5):
                val = self._track.min_val + i * range_val / 4
                y = self._value_to_y(val)
                painter.drawLine(0, int(y), rect.width(), int(y))
                painter.drawText(5, int(y + 4), f"{val:.1f}")

        # Draw interpolation curve
        if self._track and self._track.keyframes:
            path = QPainterPath()

            # Start from first keyframe
            first_kf = self._track.keyframes[0]
            x = self._frame_to_x(first_kf.frame)
            y = self._value_to_y(first_kf.value)
            path.moveTo(x, y)

            # Draw interpolated curve segments
            for i in range(len(self._track.keyframes) - 1):
                kf1 = self._track.keyframes[i]
                kf2 = self._track.keyframes[i + 1]

                # Draw interpolated values between keyframes
                frame_range = kf2.frame - kf1.frame
                steps = min(100, max(10, frame_range))
                for step in range(1, steps + 1):
                    frame = kf1.frame + int(step * frame_range / steps)
                    value = self._track.get_value(frame)
                    x = self._frame_to_x(frame)
                    y = self._value_to_y(value)
                    path.lineTo(x, y)

            painter.setPen(QPen(self._theme.curve_line, 2))
            painter.drawPath(path)

        # Draw keyframe points
        if self._track:
            for kf in self._track.keyframes:
                x = self._frame_to_x(kf.frame)
                y = self._value_to_y(kf.value)

                # Determine color
                if kf.frame == self._selected_frame:
                    color = self._theme.keyframe_selected
                elif kf.frame == self._hover_frame:
                    color = self._theme.keyframe_hover
                else:
                    color = self._theme.keyframe_normal

                # Draw circle point
                painter.setBrush(QBrush(color))
                painter.setPen(QPen(self._theme.text_highlight, 1))
                painter.drawEllipse(QPointF(x, y), KEYFRAME_SIZE, KEYFRAME_SIZE)

                # Draw value label for selected/hovered
                if kf.frame == self._selected_frame or kf.frame == self._hover_frame:
                    painter.setPen(QPen(self._theme.text_highlight))
                    painter.setFont(QFont("monospace", 8))
                    painter.drawText(int(x + 10), int(y - 5), f"{kf.value:.2f}")

        # Draw current frame indicator
        playhead_x = self._frame_to_x(self._current_frame)
        painter.setPen(QPen(self._theme.playhead, 1, Qt.PenStyle.DashLine))
        painter.drawLine(int(playhead_x), 0, int(playhead_x), rect.height())

        # Draw current value on playhead
        if self._track:
            current_val = self._track.get_value(self._current_frame)
            y = self._value_to_y(current_val)
            painter.setBrush(QBrush(self._theme.playhead))
            painter.setPen(QPen(self._theme.playhead))
            painter.drawEllipse(QPointF(playhead_x, y), 4, 4)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        """Handle mouse press."""
        pos = event.position()

        # Find keyframe at position
        if self._track:
            for kf in self._track.keyframes:
                x = self._frame_to_x(kf.frame)
                y = self._value_to_y(kf.value)
                if (pos - QPointF(x, y)).manhattanLength() < HIT_RADIUS:
                    self._selected_frame = kf.frame
                    self._dragging = True
                    self._drag_start_value = kf.value
                    self.setCursor(Qt.CursorShape.SizeVerCursor)
                    self.update()
                    return

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        """Handle mouse move."""
        pos = event.position()

        if self._dragging and self._selected_frame >= 0:
            # Drag to change value
            new_value = self._y_to_value(pos.y())
            # Clamp to valid range
            if self._track:
                new_value = max(self._track.min_val, min(self._track.max_val, new_value))
                self.keyframe_value_changed.emit(self._selected_frame, new_value)
                self.update()
        else:
            # Update hover
            hover_frame = -1
            if self._track:
                for kf in self._track.keyframes:
                    x = self._frame_to_x(kf.frame)
                    y = self._value_to_y(kf.value)
                    if (pos - QPointF(x, y)).manhattanLength() < HIT_RADIUS:
                        hover_frame = kf.frame
                        break
            if hover_frame != self._hover_frame:
                self._hover_frame = hover_frame
                self.update()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        """Handle mouse release."""
        self._dragging = False
        self.setCursor(Qt.CursorShape.CrossCursor)


class KeyframeEditorWidget(QWidget):
    """
    Complete keyframe editor with timeline and curve views.

    Provides a full UI for editing keyframe animations:
    - Timeline view with diamond keyframe markers
    - Curve view showing interpolation
    - Interpolation type selector
    - Add/remove keyframe buttons
    - Value editor for selected keyframe
    """

    # Signals
    keyframes_changed = pyqtSignal(str, list)  # param_name, keyframes_list
    # P1: emitted when the user clicks/drags the playhead inside this dialog,
    # so the host can seek its viewport to the same frame for live preview.
    current_frame_changed = pyqtSignal(int)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._param_name: str = ""
        self._param_def: Optional[Any] = None  # ParamDef reference
        self._track: Optional[KeyframeTrack] = None
        self._total_frames: int = 100
        self._current_frame: int = 0
        self._is_animated: bool = False
        # S5: snapshot of keyframes captured when the user toggles Animated OFF,
        # so toggling it back ON restores the curve instead of reseeding two
        # default endpoints.
        self._disabled_backup: Optional[List[Keyframe]] = None
        # AUD-10001/AUD-10002: state lock guarding programmatic widget updates.
        # setValue/setCurrentIndex/setChecked fire their changed-signals
        # synchronously; without the lock those re-enter the edit handlers and
        # emit redundant ``keyframes_changed`` commits (or, during
        # set_parameter, snapshot + commit using a stale track/param mix).
        self._updating_ui: bool = False

        self._init_ui()

    @contextmanager
    def _programmatic_ui_update(self) -> Iterator[None]:
        """Hold the anti-recursion lock around programmatic widget updates.

        Signal handlers connected to valueChanged/currentIndexChanged/
        stateChanged check :attr:`_updating_ui` first, so updates applied from
        code never re-enter the user-edit commit path.
        """
        self._updating_ui = True
        try:
            yield
        finally:
            self._updating_ui = False

    def _init_ui(self) -> None:
        """Initialize UI layout."""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)

        # ── Animation toggle ──
        header = QWidget()
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(4, 4, 4, 4)

        self._anim_checkbox = QCheckBox(I18N.tr("keyframe_editor.animated", "Animated"))
        self._anim_checkbox.setToolTip(
            I18N.tr(
                "keyframe_editor.animated_tooltip",
                "Enable keyframe animation for this parameter"))
        self._anim_checkbox.setStyleSheet("color: #ccc;")
        self._anim_checkbox.stateChanged.connect(self._on_animation_toggled)
        header_layout.addWidget(self._anim_checkbox)

        # Interpolation type selector
        self._interp_combo = QComboBox()
        self._interp_combo.addItems(["Linear", "Bezier", "Hold", "Ease In", "Ease Out", "Ease In-Out"])
        self._interp_combo.setToolTip(
            I18N.tr(
                "keyframe_editor.interp_tooltip",
                "Interpolation type for selected keyframe"))
        self._interp_combo.setFixedWidth(100)
        # Use global DARK_THEME QComboBox style
        self._interp_combo.currentIndexChanged.connect(self._on_interpolation_changed)
        header_layout.addWidget(self._interp_combo)

        header_layout.addStretch()
        layout.addWidget(header)

        # ── Curve editor ──
        self._curve_widget = KeyframeCurveWidget()
        self._curve_widget.keyframe_value_changed.connect(self._on_value_changed)
        layout.addWidget(self._curve_widget, stretch=2)

        # ── Timeline ──
        self._timeline_widget = KeyframeTimelineWidget()
        self._timeline_widget.keyframe_added.connect(self._on_keyframe_added)
        self._timeline_widget.keyframe_removed.connect(self._on_keyframe_removed)
        self._timeline_widget.keyframe_moved.connect(self._on_keyframe_moved)
        self._timeline_widget.keyframe_selected.connect(self._on_keyframe_selected)
        self._timeline_widget.frame_clicked.connect(self._on_frame_clicked)
        layout.addWidget(self._timeline_widget, stretch=1)

        # ── Selected keyframe editor ──
        editor = QWidget()
        editor_layout = QHBoxLayout(editor)
        editor_layout.setContentsMargins(4, 4, 4, 4)

        self._frame_spin = QSpinBox()
        self._frame_spin.setRange(0, 100000)
        self._frame_spin.setToolTip(I18N.tr("keyframe_editor.frame_tooltip", "Keyframe frame number"))
        self._frame_spin.setFixedWidth(60)
        self._frame_spin.valueChanged.connect(self._on_frame_spin_changed)
        editor_layout.addWidget(QLabel(I18N.tr("keyframe_editor.frame_label", "Frame:")))
        editor_layout.addWidget(self._frame_spin)

        self._value_spin = QDoubleSpinBox()
        self._value_spin.setRange(0.0, 1.0)
        self._value_spin.setDecimals(3)
        self._value_spin.setSingleStep(0.01)
        self._value_spin.setToolTip(I18N.tr("keyframe_editor.value_tooltip", "Keyframe value"))
        self._value_spin.setFixedWidth(80)
        self._value_spin.valueChanged.connect(self._on_value_spin_changed)
        editor_layout.addWidget(QLabel(I18N.tr("keyframe_editor.value_label", "Value:")))
        editor_layout.addWidget(self._value_spin)

        editor_layout.addStretch()
        layout.addWidget(editor)

        # Set initial visibility
        self._update_editor_state()

    def set_parameter(self, param_name: str, param_def: Any,
                      total_frames: int = 100, current_frame: int = 0) -> None:
        """Set the parameter to edit.

        Args:
            param_name: Parameter name.
            param_def: ParamDef instance with animation data.
            total_frames: Total frames in timeline.
            current_frame: Current frame position.
        """
        self._param_name = param_name
        self._param_def = param_def
        self._total_frames = total_frames
        self._current_frame = current_frame
        # S5: reset the OFF-snapshot for the new parameter so a stale backup
        # from a previously edited param is never restored by accident.
        self._disabled_backup = None

        # Load existing keyframe track
        self._is_animated = param_def.animated if param_def else False
        # AUD-10002: setChecked fires stateChanged synchronously. At this
        # point ``self._track`` still belongs to the previously edited
        # parameter while ``_param_name``/``_param_def`` already point at the
        # new one — an unguarded toggle handler would snapshot and commit
        # that mixed state. Hold the lock so the programmatic set is silent.
        with self._programmatic_ui_update():
            self._anim_checkbox.setChecked(self._is_animated)

        if param_def and param_def.keyframes:
            self._track = KeyframeTrack.from_dict({
                "param_name": param_name,
                "keyframes": param_def.keyframes,
                "min_val": param_def.min_val,
                "max_val": param_def.max_val,
            })
        else:
            # Create default track with two keyframes (start and end)
            default_val = param_def.default if param_def and param_def.default else 0.0
            self._track = KeyframeTrack(
                param_name=param_name,
                min_val=param_def.min_val if param_def else 0.0,
                max_val=param_def.max_val if param_def else 1.0,
            )
            self._track.add_keyframe(Keyframe(0, default_val))
            self._track.add_keyframe(Keyframe(total_frames - 1, default_val))

        # Update widgets
        self._timeline_widget.set_track(self._track)
        self._timeline_widget.set_total_frames(total_frames)
        self._timeline_widget.set_current_frame(current_frame)

        self._curve_widget.set_track(self._track)
        self._curve_widget.set_total_frames(total_frames)
        self._curve_widget.set_current_frame(current_frame)

        # Update value spinbox range
        if param_def:
            self._value_spin.setRange(param_def.min_val, param_def.max_val)
            self._value_spin.setDecimals(param_def.decimals)
        # AUD-10003: keep the frame spinbox inside the real timeline range so
        # a typed frame can never move a keyframe outside the composition.
        self._frame_spin.setRange(0, max(0, int(total_frames) - 1))

        self._update_editor_state()

    def set_current_frame(self, frame: int) -> None:
        """Update current frame position."""
        self._current_frame = frame
        self._timeline_widget.set_current_frame(frame)
        self._curve_widget.set_current_frame(frame)

    def set_total_frames(self, total: int) -> None:
        """Update total frames."""
        self._total_frames = total
        self._timeline_widget.set_total_frames(total)
        self._curve_widget.set_total_frames(total)
        # AUD-10003: keep the frame spinbox clamped to the timeline range.
        self._frame_spin.setRange(0, max(0, int(total) - 1))

    def get_track(self) -> Optional[KeyframeTrack]:
        """Get the current keyframe track."""
        return self._track

    def _update_editor_state(self) -> None:
        """Update editor widgets based on animation state."""
        enabled = self._is_animated
        self._interp_combo.setEnabled(enabled)
        self._frame_spin.setEnabled(enabled)
        self._value_spin.setEnabled(enabled)
        self._curve_widget.setVisible(enabled)
        self._timeline_widget.setVisible(enabled)

    def _on_animation_toggled(self, state: int) -> None:
        """Handle animation toggle.

        A-6 fix: turning Animated OFF must emit an empty keyframe list so the
        owning node-level track is deleted — it must NOT keep emitting the
        default two-point track that was created just to seed the editor UI.

        S5: OFF now snapshots the current keyframes into ``_disabled_backup``
        instead of discarding them, so toggling back ON restores the curve the
        user had built (rather than reseeding two default endpoints and losing
        all the edits). A fresh enable with no backup still seeds endpoints.
        """
        # AUD-10002: programmatic setChecked (set_parameter) must not run the
        # snapshot/commit logic — only a real user click lands here.
        if self._updating_ui:
            return
        self._is_animated = (state == Qt.CheckState.Checked.value)
        self._update_editor_state()

        if self._is_animated:
            # Enabling: restore the snapshot if we have one, otherwise seed two
            # default keyframes (only when the working track is empty).
            if self._track and not self._track.keyframes and self._disabled_backup:
                for kf in self._disabled_backup:
                    self._track.add_keyframe(kf)
                self._disabled_backup = None
                self._timeline_widget.set_track(self._track)
                self._curve_widget.set_track(self._track)
            elif self._track and len(self._track.keyframes) < 2 and not self._disabled_backup:
                default_val = self._param_def.default if self._param_def else 0.0
                self._track.keyframes.clear()
                self._track.add_keyframe(Keyframe(0, default_val))
                self._track.add_keyframe(Keyframe(self._total_frames - 1, default_val))
                self._timeline_widget.set_track(self._track)
                self._curve_widget.set_track(self._track)
        else:
            # Disabling: snapshot the keyframes before clearing so ON can
            # restore them, then emit an empty list (A-6: deletes node track).
            if self._track and self._track.keyframes:
                import copy
                self._disabled_backup = copy.deepcopy(self._track.keyframes)
                self._track.keyframes.clear()
                self._timeline_widget.set_track(self._track)
                self._curve_widget.set_track(self._track)

        # Emit change
        self._emit_keyframes_changed()

    def _on_keyframe_added(self, frame: int, value: float) -> None:
        """Handle keyframe addition."""
        if self._track:
            self._track.add_keyframe(Keyframe(frame, value))
            self._timeline_widget.set_track(self._track)
            self._curve_widget.set_track(self._track)
            self._emit_keyframes_changed()

    def _on_keyframe_removed(self, frame: int) -> None:
        """Handle keyframe removal."""
        if self._track:
            self._track.remove_keyframe(frame)
            self._timeline_widget.set_track(self._track)
            self._curve_widget.set_track(self._track)
            self._emit_keyframes_changed()

    def _on_keyframe_moved(self, old_frame: int, new_frame: int, value: float) -> None:
        """Handle keyframe move.

        F5: move the existing Keyframe object in place (mutate its ``frame``
        field then re-sort) instead of remove+new. The old implementation
        rebuilt a bare ``Keyframe(new_frame, value)`` which dropped the
        interpolation type, bezier handles and tangent mode, silently
        flattening any BEZIER curve back to LINEAR on every drag.
        """
        if self._track:
            kf = self._track.get_keyframe_at(old_frame)
            if kf is None:
                return
            kf.frame = new_frame
            self._track.keyframes.sort()
            self._timeline_widget.set_track(self._track)
            self._curve_widget.set_track(self._track)
            self._emit_keyframes_changed()

    def _on_keyframe_selected(self, frame: int) -> None:
        """Handle keyframe selection.

        AUD-10001: the spin/combo updates are programmatic reflections of the
        selection. Hold the anti-recursion lock so their changed-signals do
        not re-enter the edit handlers and emit redundant
        ``keyframes_changed`` commits for an unmodified keyframe.
        """
        kf = self._track.get_keyframe_at(frame) if self._track else None
        if kf:
            with self._programmatic_ui_update():
                self._frame_spin.setValue(kf.frame)
                self._value_spin.setValue(kf.value)
                # Update interpolation combo
                interp_idx = {
                    InterpolationType.LINEAR: 0,
                    InterpolationType.BEZIER: 1,
                    InterpolationType.HOLD: 2,
                    InterpolationType.EASE_IN: 3,
                    InterpolationType.EASE_OUT: 4,
                    InterpolationType.EASE_IN_OUT: 5,
                }.get(kf.interpolation, 0)
                self._interp_combo.setCurrentIndex(interp_idx)
            self._curve_widget.set_selected_frame(frame)

    def _on_frame_clicked(self, frame: int) -> None:
        """Handle playhead click/drag in the timeline — seek the host viewport.

        P1: previously a no-op ``pass``, which is why dragging the playhead
        inside this dialog never moved the main Viewport. Now we sync our own
        playhead (so the curve widget's indicator follows) and forward the
        frame via ``current_frame_changed`` so NodePropertiesPanel relays it to
        CompositePage.set_composite_current_frame → re-evaluate.
        """
        self._current_frame = max(0, min(frame, max(0, self._total_frames - 1)))
        self._timeline_widget.set_current_frame(self._current_frame)
        self._curve_widget.set_current_frame(self._current_frame)
        self.current_frame_changed.emit(self._current_frame)

    def _on_value_changed(self, frame: int, new_value: float) -> None:
        """Handle value change from curve editor drag."""
        if self._track:
            kf = self._track.get_keyframe_at(frame)
            if kf:
                kf.value = new_value
                self._curve_widget.set_track(self._track)
                self._emit_keyframes_changed()

    def _on_frame_spin_changed(self, frame: int) -> None:
        """Handle frame spinbox change.

        AUD-10003: a typed frame is clamped to the timeline range and resolved
        against same-frame collisions (one keyframe per frame per track) with
        the timeline's free-frame search — the same rule a timeline drag
        follows. The selection and the spinbox are re-synced to the actual
        destination, which may differ from the typed value.
        """
        if self._updating_ui:
            return
        if self._track and self._timeline_widget.selected_keyframe():
            selected = self._timeline_widget._selected_frame
            kf = self._track.get_keyframe_at(selected)
            if kf and frame != selected:
                total = max(1, self._total_frames)
                target = max(0, min(int(frame), total - 1))
                target = self._timeline_widget.find_free_frame(target, selected)
                if target != selected:
                    self._on_keyframe_moved(selected, target, kf.value)
                    # Keep selection (tree + curve + spinbox) pointing at the
                    # keyframe's real destination frame.
                    self._timeline_widget._selected_frame = target
                    self._curve_widget.set_selected_frame(target)
                    with self._programmatic_ui_update():
                        self._frame_spin.setValue(target)

    def _on_value_spin_changed(self, value: float) -> None:
        """Handle value spinbox change."""
        if self._updating_ui:
            return
        if self._track and self._timeline_widget.selected_keyframe():
            selected = self._timeline_widget._selected_frame
            kf = self._track.get_keyframe_at(selected)
            if kf:
                kf.value = value
                self._curve_widget.set_track(self._track)
                self._emit_keyframes_changed()

    def _on_interpolation_changed(self, index: int) -> None:
        """Handle interpolation type change."""
        if self._updating_ui:
            return
        if self._track and self._timeline_widget.selected_keyframe():
            selected = self._timeline_widget._selected_frame
            kf = self._track.get_keyframe_at(selected)
            if kf:
                interp_types = [
                    InterpolationType.LINEAR,
                    InterpolationType.BEZIER,
                    InterpolationType.HOLD,
                    InterpolationType.EASE_IN,
                    InterpolationType.EASE_OUT,
                    InterpolationType.EASE_IN_OUT,
                ]
                kf.interpolation = interp_types[index]
                self._curve_widget.set_track(self._track)
                self._emit_keyframes_changed()

    def _emit_keyframes_changed(self) -> None:
        """Emit keyframes_changed signal.

        A-6 fix: when animation is disabled, emit an empty list (not the
        default seed track). Consumers treat an empty list as "remove the
        node-level track".
        """
        if not self._is_animated:
            self.keyframes_changed.emit(self._param_name, [])
            return
        if self._track:
            keyframes_list = [kf.to_dict() for kf in self._track.keyframes]
            self.keyframes_changed.emit(self._param_name, keyframes_list)