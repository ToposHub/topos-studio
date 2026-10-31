"""Local scene timeline with deterministic frame scrubbing and keyframe hooks."""

from __future__ import annotations

from PyQt6.QtCore import QSize, QTimer, Qt, pyqtSignal
from PyQt6.QtGui import QAction, QColor, QMouseEvent, QPainter
from PyQt6.QtWidgets import QHBoxLayout, QLabel, QMenu, QSizePolicy, QToolButton, QVBoxLayout, QWidget

from src.shared.assets.icon_manager import (
    IconColors,
    icon_loop,
    icon_pause,
    icon_play,
    icon_step_back,
    icon_step_forward,
)
from src.shared.i18n.i18n import I18N
from src.shared.ui.widgets import DragValue
from src.shared.ui.widgets.topos_controls import ToposClickableSlider

from ..core.model import TeachingSceneDocument


class _MarkerTimelineSlider(ToposClickableSlider):
    """Scrubber with visible, directly clickable scene-marker ticks."""

    markerActivated = pyqtSignal(int)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(Qt.Orientation.Horizontal, parent=parent)
        self._markers: tuple[tuple[int, str], ...] = ()

    def set_markers(self, markers: tuple[tuple[int, str], ...]) -> None:
        self._markers = tuple(markers)
        self.update()

    def _marker_x(self, frame: int) -> int:
        span = max(1, self.maximum() - self.minimum())
        return round((frame - self.minimum()) / span * max(0, self.width() - 1))

    def paintEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setPen(QColor("#ff8800"))
        for frame, _label in self._markers:
            if self.minimum() <= frame <= self.maximum():
                x = self._marker_x(frame)
                painter.drawLine(x, 1, x, max(2, self.height() - 2))
        painter.end()

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            for frame, _label in self._markers:
                if abs(event.position().x() - self._marker_x(frame)) <= 4:
                    self.setValue(frame)
                    self.markerActivated.emit(frame)
                    event.accept()
                    return
        super().mousePressEvent(event)


class SceneTimeline(QWidget):
    frameChanged = pyqtSignal(int)
    durationChangeRequested = pyqtSignal(int)
    playStateChanged = pyqtSignal(bool)
    markerRequested = pyqtSignal(int)
    markerJumpRequested = pyqtSignal(int)
    markerRemoveRequested = pyqtSignal(str)
    markersClearRequested = pyqtSignal()
    markerRenameRequested = pyqtSignal(str, str)
    pageStepRequested = pyqtSignal(int)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("mgTimelineWidget")
        self.slider = _MarkerTimelineSlider(self)
        self.slider.setObjectName("timelineSlider")
        self.slider.setOrientation(Qt.Orientation.Horizontal)
        self.slider.setFixedHeight(16)
        # Motion Graphic users generally reason in seconds; frames remain the
        # internal/editing unit and the ruler still snaps to exact frames.
        self.timecode_label = QLabel("0.00 s")
        self.timecode_label.setObjectName("mgTimelineTimecode")
        self.timecode_label.setFixedWidth(82)
        self.timecode_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        # Kept as an alias for callers that used the old frame-label surface.
        self.frame_label = self.timecode_label
        # A timeline duration is still a numeric parameter.  Use the same
        # drag-to-adjust/value-cell control as Edit > Properties rather than
        # an isolated native spin box.
        self.duration_spin = DragValue("", 1 / 24.0, 600.0, 1.0, decimals=2, suffix=" s", parent=self)
        self.duration_spin.setObjectName("timelineDuration")
        self.duration_spin.setFixedValueWidth(72)
        self.previous_page_button = QToolButton(self)
        self.previous_page_button.setObjectName("mgTimelinePageStepButton")
        self.previous_page_button.setToolTip(I18N.tr("motion_graphic.transport.previous_page", "Previous Page"))
        self.previous_page_button.setFixedSize(28, 28)
        self.previous_page_button.setIconSize(QSize(15, 15))
        self.previous_page_button.setIcon(icon_step_back(IconColors.DEFAULT))
        self.play_button = QToolButton(self)
        self.play_button.setObjectName("mgTimelinePlayButton")
        self.play_button.setToolTip(I18N.tr("motion_graphic.transport.play_stop", "Play / Stop"))
        self.play_button.setFixedSize(32, 28)
        self.play_button.setIconSize(QSize(18, 18))
        self.play_button.setIcon(icon_play(IconColors.ORANGE))
        self.next_page_button = QToolButton(self)
        self.next_page_button.setObjectName("mgTimelinePageStepButton")
        self.next_page_button.setToolTip(I18N.tr("motion_graphic.transport.next_page", "Next Page"))
        self.next_page_button.setFixedSize(28, 28)
        self.next_page_button.setIconSize(QSize(15, 15))
        self.next_page_button.setIcon(icon_step_forward(IconColors.DEFAULT))
        self.marker_button = QToolButton(self)
        self.marker_button.setObjectName("mgTimelineMarkerButton")
        self.marker_button.setText(I18N.tr("motion_graphic.transport.marker", "Marker"))
        self.marker_button.setToolTip(I18N.tr("motion_graphic.transport.add_marker", "Add a marker at the current frame"))
        # The label includes the marker count (for example, "Marker (4)").
        # Let the layout use the button's size hint instead of eliding it.
        self.marker_button.setMinimumWidth(76)
        self.marker_button.setSizePolicy(QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Fixed)
        self.loop_button = QToolButton(self)
        self.loop_button.setObjectName("mgTimelineLoopButton")
        self.loop_button.setIcon(icon_loop(IconColors.ORANGE))
        self.loop_button.setIconSize(QSize(16, 16))
        self.loop_button.setToolTip(I18N.tr("motion_graphic.transport.loop", "Loop Playback"))
        self.loop_button.setCheckable(True)
        self.loop_button.setChecked(True)
        self.loop_button.setFixedSize(28, 28)
        self.loop_button.toggled.connect(self._update_loop_icon)
        self._timer = QTimer(self)
        self._fps = 24.0
        self._pending_duration_seconds: float | None = None
        self._timer.timeout.connect(self._advance)
        self.slider.valueChanged.connect(self._frame_changed)
        # AUD-31009: a duration drag passes through dozens of valueChanged
        # ticks; each one used to emit durationChangeRequested, and the host
        # executed a full-scene SetDurationCommand per tick (one undo entry
        # each).  Stage the value and emit exactly once per interaction —
        # DragValue wraps pointer drags, inline edits and wheel notches in
        # interaction_started/interaction_ended.
        self.duration_spin.valueChanged.connect(self._duration_value_changed)
        self.duration_spin.interaction_ended.connect(self._commit_duration_change)
        self.play_button.clicked.connect(self.toggle_play)
        self.previous_page_button.clicked.connect(lambda: self.pageStepRequested.emit(-1))
        self.next_page_button.clicked.connect(lambda: self.pageStepRequested.emit(1))
        self.marker_button.clicked.connect(lambda: self.markerRequested.emit(self.slider.value()))
        self.slider.markerActivated.connect(self.markerJumpRequested)
        self._marker_menu = QMenu(self.marker_button)
        self.marker_button.setMenu(self._marker_menu)
        self.marker_button.setPopupMode(QToolButton.ToolButtonPopupMode.MenuButtonPopup)
        # Keep transport as one compact widget.  It can be hosted in the
        # keyframes header or remain below the scrubber as a standalone row.
        self._transport_bar = QWidget(self)
        self._transport_bar.setObjectName("mgTimelineTransport")
        self._transport_bar.setSizePolicy(
            QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Preferred,
        )
        self._transport_layout = QHBoxLayout(self._transport_bar)
        self._transport_layout.setContentsMargins(0, 0, 0, 0)
        self._transport_layout.setSpacing(6)
        self._transport_layout.addWidget(self.previous_page_button)
        self._transport_layout.addWidget(self.play_button)
        self._transport_layout.addWidget(self.next_page_button)
        self._transport_layout.addWidget(self.timecode_label)
        self._transport_layout.addWidget(self.duration_spin)
        self._transport_layout.addWidget(self.loop_button)
        self._transport_layout.addWidget(self.marker_button)
        self._transport_host: QHBoxLayout | None = None

        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(6, 0, 6, 1)
        self._layout.setSpacing(0)
        self._layout.addWidget(self.slider)
        self._layout.addWidget(self._transport_bar)
        self.setFixedHeight(18)

    def set_transport_host(
        self, host: QHBoxLayout | None, *, include_timecode: bool = True,
    ) -> None:
        """Place compact transport controls in a host row, or below the slider."""
        if host is self._transport_host:
            self.timecode_label.setVisible(include_timecode)
            if host is None:
                self.setFixedHeight(46)
            return
        if self._transport_host is not None:
            self._transport_host.removeWidget(self._transport_bar)
        else:
            self._layout.removeWidget(self._transport_bar)

        self._transport_host = host
        if host is None:
            self._transport_bar.setParent(self)
            self.timecode_label.setVisible(True)
            self._layout.addWidget(self._transport_bar)
            self.setFixedHeight(46)
            return
        self.timecode_label.setVisible(include_timecode)
        host.addWidget(self._transport_bar, 0, Qt.AlignmentFlag.AlignVCenter)
        self.setFixedHeight(18)

    def set_scene(self, scene: TeachingSceneDocument) -> None:
        self._fps = scene.fps
        duration_seconds = scene.duration_frames / max(self._fps, 0.001)
        self.slider.blockSignals(True)
        previous_block = self.duration_spin.blockSignals(True)
        try:
            self.slider.setRange(0, max(0, scene.duration_frames - 1))
            self.duration_spin.setRange(1 / max(self._fps, 1.0), max(600.0, duration_seconds * 2.0))
            self.duration_spin.setValue(duration_seconds)
        finally:
            self.duration_spin.blockSignals(previous_block)
            self.slider.blockSignals(False)
        self.slider.set_markers(tuple((marker.frame, marker.label) for marker in scene.markers))
        self._rebuild_marker_menu(scene)
        self.set_frame(min(self.slider.value(), scene.duration_frames - 1))

    def _rebuild_marker_menu(self, scene: TeachingSceneDocument) -> None:
        self._marker_menu.clear()
        self._marker_menu.addAction(
            I18N.tr("motion_graphic.transport.add_marker", "Add Marker at the current frame"),
            lambda: self.markerRequested.emit(self.slider.value()),
        )
        if not scene.markers:
            self.marker_button.setText(I18N.tr("motion_graphic.transport.marker", "Marker"))
            return
        self.marker_button.setText(f"{I18N.tr('motion_graphic.transport.marker', 'Marker')} ({len(scene.markers)})")
        self._marker_menu.addSeparator()
        remove_menu = self._marker_menu.addMenu(I18N.tr("motion_graphic.transport.remove_marker", "Remove Marker"))
        clear_action = QAction(I18N.tr("motion_graphic.transport.clear_markers", "Remove All Markers"), self._marker_menu)
        clear_action.triggered.connect(self.markersClearRequested.emit)
        self._marker_menu.addAction(clear_action)
        rename_menu = self._marker_menu.addMenu(I18N.tr("motion_graphic.transport.rename_marker", "Rename Marker"))
        for marker in sorted(scene.markers, key=lambda item: item.frame):
            timecode = self._frame_to_display(marker.frame)
            title = f"{timecode} — {marker.label}"
            jump_action = QAction(title, self._marker_menu)
            jump_action.triggered.connect(lambda _checked=False, frame=marker.frame: self.markerJumpRequested.emit(frame))
            self._marker_menu.addAction(jump_action)
            remove_action = QAction(title, remove_menu)
            remove_action.triggered.connect(
                lambda _checked=False, marker_id=marker.marker_id: self.markerRemoveRequested.emit(marker_id),
            )
            remove_menu.addAction(remove_action)
            rename_action = QAction(title, rename_menu)
            rename_action.triggered.connect(
                lambda _checked=False, marker_id=marker.marker_id, label=marker.label:
                self.markerRenameRequested.emit(marker_id, label),
            )
            rename_menu.addAction(rename_action)

    def _update_loop_icon(self, enabled: bool) -> None:
        """Keep the loop affordance legible in both toggle states."""
        color = IconColors.ORANGE if enabled else IconColors.DEFAULT
        self.loop_button.setIcon(icon_loop(color))

    def set_frame(self, frame: int) -> None:
        self.slider.blockSignals(True)
        self.slider.setValue(max(self.slider.minimum(), min(frame, self.slider.maximum())))
        self.slider.blockSignals(False)
        self.timecode_label.setText(self._frame_to_display(self.slider.value()))

    def toggle_play(self) -> None:
        if self._timer.isActive():
            self._timer.stop()
            self.play_button.setIcon(icon_play(IconColors.ORANGE))
            self.playStateChanged.emit(False)
        else:
            self._timer.start(max(1, round(1000 / self._fps)))
            self.play_button.setIcon(icon_pause(IconColors.ORANGE))
            self.playStateChanged.emit(True)

    def stop(self) -> None:
        if self._timer.isActive():
            self.toggle_play()

    def _advance(self) -> None:
        next_frame = self.slider.value() + 1
        if next_frame > self.slider.maximum():
            if self.loop_button.isChecked():
                self.slider.setValue(self.slider.minimum())
            else:
                self.slider.setValue(self.slider.maximum())
                self.stop()
            return
        self.slider.setValue(next_frame)

    def _frame_changed(self, frame: int) -> None:
        self.timecode_label.setText(self._frame_to_display(frame))
        self.frameChanged.emit(frame)

    def _duration_value_changed(self, seconds: float) -> None:
        """Stage one duration tick (see AUD-31009 note in __init__)."""
        self._pending_duration_seconds = float(seconds)

    def _commit_duration_change(self) -> None:
        """Emit the staged duration exactly once per completed interaction."""
        seconds = self._pending_duration_seconds
        self._pending_duration_seconds = None
        if seconds is None:
            return
        self.durationChangeRequested.emit(
            max(1, int(round(seconds * self._fps))),
        )

    def _frame_to_display(self, frame: int) -> str:
        """Format the playhead in seconds while preserving frame precision."""

        seconds = max(0.0, float(frame) / max(self._fps, 0.001))
        return f"{seconds:.2f} s"
