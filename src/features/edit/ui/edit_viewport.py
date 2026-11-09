"""Edit monitor presentation, measured against Resolve's single/dual viewers.

Only UI chrome and focus live here. Existing monitor instances, media clocks,
frame providers and transport signals retain their original owners.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Callable

from PyQt6.QtCore import QEvent, QObject, QPointF, QRectF, QSignalBlocker, QSize, Qt, pyqtSignal
from PyQt6.QtGui import QAction, QColor, QFont, QIcon, QPainter, QPen, QResizeEvent
from PyQt6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QLayout, QMenu, QSizePolicy, QSlider, QSplitter,
    QToolButton, QVBoxLayout, QWidget,
)

from src.shared.i18n.i18n import I18N
from src.shared.media.metadata import Rational
from src.shared.media.timecode import Timecode
from src.shared.ui.viewport_icons import viewer_icon
from src.shared.assets.icon_manager import (
    IconColors, icon_jump_start, icon_step_back, icon_play, icon_pause,
    icon_step_forward, icon_jump_end, icon_loop,
)
from src.features.edit.ui.viewport_scrubber import MonitorScrubber

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from src.features.edit.ui.program_viewer import ProgramViewerWidget
    from src.features.edit.ui.source_viewer import SourceViewerWidget

HEADER_HEIGHT = 30
TRANSPORT_HEIGHT = 34
MONITOR_BACKGROUND = "#191919"

# I03：本容器的动作键 → viewer_capabilities 注册表动作名（单一事实源）。
_CAPABILITY_REGISTRY_KEYS = {
    "source.match_frame": "match_frame.source_to_timeline",
    "timeline.match_frame": "match_frame.timeline_to_source",
    "source.reverse": "source.reverse_play",
    "source.loop": "source.loop",
}
_BUTTON_STYLE = """
QToolButton { background: transparent; border: none; border-radius: 0;
    color: #b9b9bd; padding: 0; margin: 0; font-size: 11px; }
QToolButton:hover { background: #303035; color: #e5e5e7; }
QToolButton:pressed { background: #3a3a3f; }
QToolButton:checked { background: #343439; color: #e0e0e2; }
QToolButton:disabled { color: #555559; background: transparent; }
QToolButton::menu-indicator { image: none; width: 0; }
"""
_SLIDER_STYLE = """
QSlider { background: #202024; border: none; margin: 0; }
QSlider::groove:horizontal { margin: 0 20px; height: 4px; background: #111113; }
QSlider::handle:horizontal { width: 8px; margin: -6px 0; background: transparent; }
"""


def _tool(icon: str, tooltip: str, parent: QWidget, width: int = 32) -> QToolButton:
    """Create a flat monitor control on a consistent 20px vector grid."""
    button = QToolButton(parent)
    button.setIcon(viewer_icon(icon))
    button.setIconSize(QSize(20, 20))
    button.setFixedSize(width, 28)
    button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
    button.setToolTip(tooltip)
    button.setAccessibleName(tooltip)
    button.setStyleSheet(_BUTTON_STYLE)
    return button


class _DropdownTool(QToolButton):
    """A platform-independent icon/text menu with a fine, inset chevron."""

    def __init__(self, parent: QWidget, icon: str | None = None) -> None:
        super().__init__(parent)
        self.setFixedSize(48, 28)
        self.setIconSize(QSize(20, 20))
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setStyleSheet(_BUTTON_STYLE + "QToolButton { padding-right: 12px; }")
        if icon is not None:
            self.setIcon(viewer_icon(icon))
        self.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)

    def paintEvent(self, event: QEvent) -> None:
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(QColor("#96969c" if self.isEnabled() else "#555559"), 1.0))
        x, y = self.width() - 10.0, self.height() / 2.0
        painter.drawLine(QPointF(x - 3.0, y - 1.0), QPointF(x, y + 2.0))
        painter.drawLine(QPointF(x, y + 2.0), QPointF(x + 3.0, y - 1.0))
        painter.end()


def _adopt(layout: QHBoxLayout, widget: QToolButton, icon: str, width: int = 32) -> None:
    """Keep a live button and its connections, replacing only its artwork."""
    widget.setText("")
    widget.setIcon(viewer_icon(icon))
    widget.setIconSize(QSize(20, 20))
    widget.setFixedSize(width, 28)
    widget.setStyleSheet(_BUTTON_STYLE)
    widget.setFocusPolicy(Qt.FocusPolicy.NoFocus)
    widget.setAccessibleName(widget.toolTip())
    layout.addWidget(widget)
    widget.show()


def _legacy_transport_icon(name: str, loop_enabled: bool = False) -> QIcon:
    """Retain the original Color transport's full-sized orange play artwork."""
    factories = {
        "first": icon_jump_start, "previous": icon_step_back,
        "play": icon_play, "pause": icon_pause,
        "next": icon_step_forward, "last": icon_jump_end, "loop": icon_loop,
    }
    color = IconColors.ORANGE if name in {"play", "pause"} or (name == "loop" and loop_enabled) else IconColors.DEFAULT
    return factories[name](color)


def _adopt_transport(layout: QHBoxLayout, button: QToolButton, name: str) -> None:
    """Preserve legacy glyph size, hit target and existing signal ownership."""
    button.setText("")
    button.setIcon(_legacy_transport_icon(name))
    button.setIconSize(QSize(18, 18) if name == "play" else QSize(15, 15))
    button.setFixedSize(32 if name == "play" else 28, 28)
    button.setStyleSheet(_BUTTON_STYLE + "QToolButton { border-radius: 4px; padding: 3px; margin: 1px; }")
    button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
    button.setAccessibleName(button.toolTip())
    layout.addWidget(button)
    button.show()


class _MonitorFooter(QFrame):
    """Anchor edge tools and transport independently to the monitor center."""

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.setObjectName("editMonitorTransport")
        self.setFixedHeight(TRANSPORT_HEIGHT)
        self.setMinimumWidth(280)
        self.setStyleSheet("QFrame#editMonitorTransport { background: #202024; border-top: 1px solid #151518; }" + _BUTTON_STYLE)
        self.left_group = QWidget(self)
        self.center_group = QWidget(self)
        self.right_group = QWidget(self)
        self.marker_group = QWidget(self)
        self.optional_controls: list[QWidget] = []
        self.narrow_controls: list[QWidget] = []
        self.compact_controls: list[QWidget] = []
        self.groups: list[QHBoxLayout] = []
        for group in (self.left_group, self.center_group, self.right_group, self.marker_group):
            group.setObjectName("monitorControlGroup")
            group.setStyleSheet("QWidget#monitorControlGroup { background: transparent; border: none; }")
            row = QHBoxLayout(group)
            row.setContentsMargins(0, 0, 0, 0)
            row.setSpacing(0)
            row.setSizeConstraint(QLayout.SizeConstraint.SetNoConstraint)
            self.groups.append(row)
        for name, label in (("previous_marker", "Previous Marker"), ("marker", "Add Marker"), ("next_marker", "Next Marker")):
            button = _tool(name, I18N.tr("edit.viewport." + name, label), self.marker_group, 18)
            button.setEnabled(False)
            button.setToolTip(label + " — " + I18N.tr("edit.viewport.backend_pending", "Not available in this version"))
            self.groups[3].addWidget(button)

    def minimumSizeHint(self) -> QSize:
        return QSize(280, TRANSPORT_HEIGHT)

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self.position_groups()

    def position_groups(self) -> None:
        """Use full-row centering, including when the edge groups differ."""
        expanded = self.width() >= 460
        for control in self.optional_controls:
            control.setVisible(expanded)
        for control in self.narrow_controls:
            control.setVisible(self.width() >= 340)
        for control in self.compact_controls:
            control.setVisible(self.width() >= 300)
        center_width = self.center_group.sizeHint().width()
        left_width = self.left_group.sizeHint().width()
        right_width = self.right_group.sizeHint().width()
        y = (self.height() - 28) // 2
        self.left_group.setGeometry(10, y, left_width, 28)
        self.center_group.setGeometry((self.width() - center_width) // 2, y, center_width, 28)
        self.right_group.setGeometry(self.width() - right_width - 10, y, right_width, 28)
        self.marker_group.setVisible(self.width() >= 700)
        self.marker_group.setGeometry(self.center_group.x() - 90, y, 54, 28)


class _MonitorHeader(QFrame):
    """Reference-aligned cells with an independently centered timeline title."""

    def __init__(self, title: QWidget, timecode: QLabel, duration: QLabel, parent: QWidget) -> None:
        super().__init__(parent)
        self.setObjectName("editMonitorHeader")
        self.setFixedHeight(HEADER_HEIGHT)
        self.setMinimumWidth(0)
        self.show_monitor_switches = True
        self.extra_controls: list[QWidget] = []
        self.setStyleSheet("""
QFrame#editMonitorHeader { background: #202024; border-bottom: 1px solid #101013; }
QLabel { color: #dedee1; background: transparent; border: none; font-size: 11px;
    font-weight: 600; padding: 0; }
""")
        self.left_group = QWidget(self)
        self.right_group = QWidget(self)
        for group in (self.left_group, self.right_group):
            group.setObjectName("monitorHeaderGroup")
            group.setStyleSheet("QWidget#monitorHeaderGroup { background: transparent; border: none; }")
        self.left = QHBoxLayout(self.left_group)
        self.right = QHBoxLayout(self.right_group)
        for row in (self.left, self.right):
            row.setContentsMargins(0, 0, 0, 0)
            row.setSpacing(0)
        outer = QHBoxLayout(self)
        outer.setSizeConstraint(QLayout.SizeConstraint.SetNoConstraint)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        outer.addWidget(self.left_group)
        outer.addStretch(1)
        outer.addWidget(self.right_group)
        self.left_group.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Preferred)
        self.right_group.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Preferred)
        self.source_button = _tool("source_clip", I18N.tr("edit.viewport.source", "Source Clip"), self.left_group)
        self.source_tape_button = _tool("source_tape", I18N.tr("edit.viewport.source_tape", "Source Tape"), self.left_group)
        self.timeline_button = _tool("timeline", I18N.tr("edit.viewport.timeline", "Timeline"), self.left_group)
        for button in (self.source_button, self.source_tape_button, self.timeline_button):
            button.setCheckable(True)
            self.left.addWidget(button)
        self.source_tape_button.setEnabled(False)
        self.source_tape_button.setToolTip(I18N.tr("edit.viewport.source_tape", "Source Tape") + " — " + I18N.tr("edit.viewport.backend_pending", "Not available in this version"))
        self.zoom_button = _DropdownTool(self.left_group)
        self.zoom_button.setFixedWidth(62)
        self.zoom_button.setText(I18N.tr("viewport.fit", "Fit"))
        self.zoom_button.setToolTip(I18N.tr("edit.viewport.zoom", "Viewer Zoom"))
        self.zoom_button.setAccessibleName(self.zoom_button.toolTip())
        self.zoom_button.setStyleSheet(_BUTTON_STYLE + "QToolButton { padding-right: 12px; border-left: 1px solid #131316; border-right: 1px solid #131316; }")
        self.left.addWidget(self.zoom_button)
        self.duration = duration
        duration.setParent(self.left_group)
        duration.setFixedWidth(84)
        duration.setAlignment(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft)
        duration.setStyleSheet("color: #d1d1d4; font-size: 11px; font-weight: 600; padding-left: 6px; background: transparent;")
        duration.setToolTip(I18N.tr("edit.viewport.duration", "Duration"))
        self.left.addWidget(duration)
        self.timecode = timecode
        timecode.setParent(self.right_group)
        timecode.setFixedWidth(90)
        timecode.setAlignment(Qt.AlignmentFlag.AlignCenter)
        timecode.setStyleSheet("color: #e1e1e3; background: transparent; padding: 0; font-size: 12px; font-weight: 600;")
        timecode.setToolTip(I18N.tr("edit.viewport.timecode", "Current Timecode"))
        timecode.setAccessibleName(timecode.toolTip())
        self.right.addWidget(timecode)
        timecode.show()
        self.single_button = _tool("single_view", I18N.tr("edit.viewport.single", "Single Viewer Mode"), self.right_group, 36)
        self.dual_button = _tool("dual_view", I18N.tr("edit.viewport.dual", "Dual Viewer Mode"), self.right_group, 36)
        for button in (self.single_button, self.dual_button):
            button.setStyleSheet(_BUTTON_STYLE + "QToolButton { border-left: 1px solid #131316; }")
            self.right.addWidget(button)
        self.options_button = _tool("more", I18N.tr("edit.viewport.options", "Viewer Options"), self.right_group, 32)
        self.options_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.right.addWidget(self.options_button)
        self.title = title
        title.setParent(self)
        title.setMinimumWidth(0)
        title.setMaximumWidth(320)
        title.setFixedHeight(HEADER_HEIGHT)
        title.setProperty("resolveMonitorTitle", True)
        title.setFont(QFont("Helvetica Neue", 12, QFont.Weight.DemiBold))
        title.show()
        self.position_title()

    def minimumSizeHint(self) -> QSize:
        """Optional cells must not constrain the splitter's saved proportions."""
        return QSize(220, HEADER_HEIGHT)

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self.position_title()

    def add_display_control(self, control: QWidget) -> None:
        """Insert a reference display cell between timecode and mode switch."""
        self.right.insertWidget(self.right.indexOf(self.single_button), control)
        self.extra_controls.append(control)
        self.position_title()

    def position_title(self) -> None:
        """Keep the title at the exact monitor center without covering tools."""
        for button in (self.source_button, self.timeline_button):
            button.setVisible(self.show_monitor_switches)
        self.source_tape_button.setVisible(self.show_monitor_switches and self.width() >= 560)
        self.duration.setVisible(self.width() >= 560)
        for control in self.extra_controls:
            control.setVisible(self.width() >= 760)
        self.layout().activate()
        center = self.width() / 2.0
        available = max(0, int(2 * min(center - self.left_group.geometry().right() - 8,
                                      self.right_group.x() - center - 8)))
        width = min(320, available)
        self.title.setGeometry((self.width() - width) // 2, 0, width, HEADER_HEIGHT)
        self.title.setVisible(width >= 48)
        self.title.raise_()

    def set_active(self, active: bool) -> None:
        color = "#e26458" if active else "#dedee1"
        self.title.setProperty("monitorTitleColor", color)
        self.title.setStyleSheet("color: " + color + "; background: transparent; border: none; padding: 0; font-size: 12px; font-weight: 600;")
        self.title.update()
        self.position_title()


class EditViewport(QWidget):
    """One dockable workspace containing the original Source/Program viewers.

    ``active_monitor`` uses ``source`` / ``program`` for controller compatibility.
    Existing commands still target the original viewer objects.  New program
    scrub gestures are emitted for the host's existing timeline seek entry.
    """

    mode_changed = pyqtSignal(bool)
    active_monitor_changed = pyqtSignal(str)
    program_seek_requested = pyqtSignal(int)
    program_stop_requested = pyqtSignal()
    # I03：能力宿主协议。capability_action_requested(key) —— 有真实
    # handler 的动作经宿主执行（Match Frame 等）；本地动作（reverse）
    # 在容器内消化。capabilities_context_requested —— 菜单即将展开/状
    # 态可能过期，宿主应重评注册表并调 refresh_capabilities 回填。
    capability_action_requested = pyqtSignal(str)
    capabilities_context_requested = pyqtSignal()

    def __init__(
        self, program_viewer: ProgramViewerWidget,
        source_viewer: SourceViewerWidget, parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("EditViewport")
        self.setMinimumSize(320, 200)
        self.program_viewer = program_viewer
        self.source_viewer = source_viewer
        self._capability_actions: dict[str, list] = {}
        self._capability_labels: dict[str, str] = {}
        self._dual_view = False
        self._active_monitor = "program"
        self._deliver_mode = False
        self._program_fps = 24.0
        self._dual_sizes: list[int] | None = None
        self._timeline_widget: QWidget | None = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self.splitter = QSplitter(Qt.Orientation.Horizontal, self)
        self.splitter.setChildrenCollapsible(False)
        self.splitter.setHandleWidth(1)
        self.splitter.setStyleSheet("QSplitter::handle { background: #0d0d10; }")
        layout.addWidget(self.splitter)
        self.source_pane = QWidget(self.splitter)
        self.program_pane = QWidget(self.splitter)
        for pane, viewer in ((self.source_pane, source_viewer), (self.program_pane, program_viewer)):
            pane_layout = QVBoxLayout(pane)
            pane_layout.setContentsMargins(0, 0, 0, 0)
            pane_layout.setSpacing(0)
            pane_layout.addWidget(viewer)
            self.splitter.addWidget(pane)
        self.source_header = self._install_source_chrome()
        self.program_header = self._install_program_chrome()
        for header in (self.source_header, self.program_header):
            header.source_button.clicked.connect(lambda: self.show_source())
            header.timeline_button.clicked.connect(lambda: self.show_program())
            header.single_button.clicked.connect(lambda: self.set_dual_view(False))
            header.dual_button.clicked.connect(lambda: self.set_dual_view(True))
        # Install once.  Show/hide switches never migrate the canvas or rebuild
        # the controls; marks, zoom/pan and pending frames retain their owners.
        for viewer in (program_viewer, source_viewer):
            viewer.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
            viewer.installEventFilter(self)
            for child in viewer.findChildren(QWidget):
                child.installEventFilter(self)
        self._apply_mode()

    @property
    def dual_view(self) -> bool:
        """Whether both monitors are visible."""
        return self._dual_view

    @property
    def active_monitor(self) -> str:
        """The monitor that receives transport keyboard focus."""
        return self._active_monitor

    def _install_source_chrome(self) -> _MonitorHeader:
        viewer = self.source_viewer
        bar = viewer.toolbar
        layout = viewer.layout()
        bar.hide()
        bar.set_presentation_icon_provider(_legacy_transport_icon)
        bar._monitor_mark_active_style = _BUTTON_STYLE + "QToolButton { background: #37373c; }"
        bar.source_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        if not bar.source_label.text():
            bar.source_label.setText(I18N.tr("edit.viewport.source", "Source"))
        header = _MonitorHeader(bar.source_label, bar.timecode_label, bar.duration_label, viewer)
        layout.insertWidget(0, header)
        zoom_menu = QMenu(header)
        zoom_menu.addAction(I18N.tr("viewport.fit", "Fit to Window"), viewer.viewer.fit_to_window)
        zoom_menu.addAction(I18N.tr("viewport.actual_size", "Actual Size"), viewer.viewer.actual_size)
        header.zoom_button.setMenu(zoom_menu)
        viewer.viewer.zoom_changed.connect(lambda value: header.zoom_button.setText(f"{value * 100.0:.0f}%"))
        options = QMenu(header)
        clear_action = options.addAction(bar.btn_clear.text(), bar.btn_clear.click)
        options.aboutToShow.connect(lambda: clear_action.setEnabled(bar.btn_clear.isEnabled()))
        options.addAction(I18N.tr("edit.viewport.source_patch", "Source Patch"), bar.btn_source_patch.click)
        options.addAction(bar.btn_prev.toolTip(), bar.btn_prev.click)
        options.addAction(bar.btn_next.toolTip(), bar.btn_next.click)
        options.addSeparator()
        for key, label in (("source_tape", "Source Tape"),
                           ("previous_marker", "Previous Marker"), ("marker", "Add Marker"),
                           ("next_marker", "Next Marker")):
            self._unavailable(options, I18N.tr("edit.viewport." + key, label))
        self._capability_menu_action(
            options, "source.match_frame",
            I18N.tr("edit.viewport.match_frame", "Match Frame"))
        self._capability_menu_action(
            options, "source.reverse",
            I18N.tr("edit.viewport.reverse", "Play Reverse"))
        self.source_loop_action = options.addAction(
            I18N.tr("edit.viewport.loop", "Loop Source Playback"))
        self.source_loop_action.setCheckable(True)
        self.source_loop_action.triggered.connect(viewer.set_loop)
        self._register_capability_action("source.loop", self.source_loop_action)
        options.aboutToShow.connect(self._sync_source_loop_action)
        options.aboutToShow.connect(self.capabilities_context_requested.emit)
        header.options_button.setMenu(options)
        footer, left, center, right = self._footer(viewer)
        _adopt(left, bar.btn_source_patch, "source_patch", 40)
        for glyph, label in (("video_only", "Video Only"), ("audio_only", "Audio Only")):
            left.addWidget(self._pending_button(footer, glyph, I18N.tr("edit.viewport." + glyph, label)))
        self.source_stop_button = self._stop_button(viewer.stop_in_place, footer)
        self.source_stop_button.hide()
        options.addAction(I18N.tr("edit.viewport.stop", "Stop"), self.source_stop_button.click)
        for button, name in ((bar.btn_jump_start, "first"), (bar.btn_prev, "previous"),
                             (bar.btn_play, "play"), (bar.btn_next, "next"), (bar.btn_jump_end, "last")):
            _adopt_transport(center, button, name)
        center.setSpacing(3)
        bar.update_play_state(bar._is_playing)
        right.addWidget(self._capability_footer_button(
            footer, "match_frame", I18N.tr("edit.viewport.match_frame", "Match Frame"),
            "source.match_frame"))
        _adopt(right, bar.btn_mark_in, "mark_in", 26)
        _adopt(right, bar.btn_mark_out, "mark_out", 26)
        viewer.slider.setProperty("resolveMonitorScrubber", True)
        viewer.slider.setAccessibleName(I18N.tr("edit.viewport.source_scrub", "Scrub Source Clip"))
        viewer.slider.setStyleSheet(_SLIDER_STYLE)
        viewer.slider.setFixedHeight(24)
        layout.addWidget(footer)
        viewer.viewer.setMinimumSize(160, 90)
        footer.position_groups()
        self.source_footer = footer
        return header

    def _install_program_chrome(self) -> _MonitorHeader:
        viewer = self.program_viewer
        toolbar = viewer.get_toolbar()
        transport = viewer.get_transport_bar()
        toolbar.hide()
        transport.hide()
        transport.set_presentation_icon_provider(
            lambda name: _legacy_transport_icon(name, transport._loop_enabled))
        self._program_toolbar = toolbar
        self._program_transport = transport
        header = _MonitorHeader(toolbar._timeline_selector, transport.timecode_label, QLabel("00:00:00:00"), viewer)
        viewer.layout().insertWidget(0, header)
        header.zoom_button.setMenu(toolbar.view_menu)
        toolbar.view_menu.addSeparator()
        for zoom in (0.125, 0.25, 0.5, 0.75, 1.0, 1.5, 2.0, 3.0):
            toolbar.view_menu.addAction(f"{zoom * 100.0:g}%", lambda checked=False, value=zoom: viewer.get_viewer().set_zoom(value))
        viewer.get_viewer().zoom_changed.connect(lambda value: header.zoom_button.setText(f"{value * 100.0:.0f}%"))
        options = QMenu(header)
        options.addMenu(toolbar.view_menu).setText(I18N.tr("viewport.view", "View"))
        options.addMenu(toolbar.quality_menu).setText(I18N.tr("edit.viewport.quality", "Timeline Proxy Resolution"))
        options.addAction(I18N.tr("edit.viewport.guides", "Viewer Guides"), toolbar.guides_menu_btn.click)
        options.addAction(transport.btn_prev_frame.toolTip(), transport.btn_prev_frame.click)
        options.addAction(transport.btn_next_frame.toolTip(), transport.btn_next_frame.click)
        options.addSeparator()
        for key, label in (("proxy", "Proxy Handling"), ("bypass", "Bypass Color Grades and Fusion Effects"),
                           ("reverse", "Play Reverse"),
                           ("timeline_mark_in", "Mark Timeline In"), ("timeline_mark_out", "Mark Timeline Out"),
                           ("previous_marker", "Previous Marker"), ("marker", "Add Marker"),
                           ("next_marker", "Next Marker")):
            self._unavailable(options, I18N.tr("edit.viewport." + key, label))
        self._capability_menu_action(
            options, "timeline.match_frame",
            I18N.tr("edit.viewport.match_frame", "Match Frame"))
        options.aboutToShow.connect(self.capabilities_context_requested.emit)
        header.options_button.setMenu(options)
        guides_group = QWidget(header)
        guides_group.setObjectName("monitorGuidesGroup")
        guides_group.setStyleSheet("QWidget#monitorGuidesGroup { background: transparent; border: none; }")
        guides_row = QHBoxLayout(guides_group)
        guides_row.setContentsMargins(0, 0, 0, 0)
        guides_row.setSpacing(0)
        _adopt(guides_row, toolbar.guides_menu_btn, "guides", 28)
        guides_arrow = _tool("chevron_down", I18N.tr("edit.viewport.guides", "Viewer Guides"), guides_group, 14)
        guides_arrow.clicked.connect(toolbar.guides_menu_btn.click)
        guides_row.addWidget(guides_arrow)
        guides_group.setFixedWidth(42)
        header.add_display_control(guides_group)
        proxy = _DropdownTool(header, "proxy")
        proxy.setToolTip(I18N.tr("edit.viewport.proxy", "Proxy Handling"))
        quality = _DropdownTool(header, "resolution")
        quality.setToolTip(I18N.tr("edit.viewport.quality", "Timeline Proxy Resolution"))
        quality.setMenu(toolbar.quality_menu)
        bypass = _tool("bypass", I18N.tr("edit.viewport.bypass", "Bypass Color Grades and Fusion Effects"), header, 36)
        for control in (proxy, bypass):
            control.setEnabled(False)
            control.setToolTip(control.toolTip() + " — " + I18N.tr("edit.viewport.backend_pending", "Not available in this version"))
        for control in (proxy, quality, bypass):
            control.setAccessibleName(control.toolTip())
            header.add_display_control(control)
        footer, left, center, right = self._footer(viewer)
        _adopt(left, toolbar.transform_btn, "transform", 26)
        arrow = _tool("chevron_down", I18N.tr("edit.viewport.overlay_tools", "Viewer Overlay Tools"), footer, 14)
        arrow.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        overlay_menu = QMenu(arrow)
        transform = overlay_menu.addAction(I18N.tr("edit.viewport.transform", "Transform"), toolbar.transform_btn.click)
        transform.setCheckable(True)
        overlay_menu.aboutToShow.connect(lambda: transform.setChecked(toolbar.transform_btn.isChecked()))
        overlay_menu.addAction(I18N.tr("edit.viewport.guides", "Viewer Guides"), toolbar.guides_menu_btn.click)
        arrow.setMenu(overlay_menu)
        left.addWidget(arrow)
        footer.compact_controls.append(arrow)
        self.program_stop_button = self._stop_button(self.program_stop_requested.emit, footer)
        self.program_stop_button.hide()
        options.addAction(I18N.tr("edit.viewport.stop", "Stop"), self.program_stop_button.click)
        for button, name in ((transport.btn_jump_start, "first"), (transport.btn_prev_frame, "previous"),
                             (transport.btn_play, "play"), (transport.btn_next_frame, "next"),
                             (transport.btn_jump_end, "last"), (transport.btn_loop, "loop")):
            _adopt_transport(center, button, name)
        transport.set_loop_checked(transport._loop_enabled)
        transport.update_play_button(transport._is_playing)
        center.setSpacing(3)
        right.addWidget(self._capability_footer_button(
            footer, "match_frame", I18N.tr("edit.viewport.match_frame", "Match Frame"),
            "timeline.match_frame"))
        for glyph, key, label in (("mark_in", "timeline_mark_in", "Mark Timeline In"),
                                  ("mark_out", "timeline_mark_out", "Mark Timeline Out")):
            mark = _tool(glyph, I18N.tr("edit.viewport." + key, label), footer)
            mark.setEnabled(False)
            mark.setToolTip(label + " — " + I18N.tr("edit.viewport.backend_pending", "Not available in this version"))
            right.addWidget(mark)
            footer.narrow_controls.append(mark)
        self.program_slider = MonitorScrubber(viewer)
        self.program_slider.setObjectName("programMonitorScrubber")
        self.program_slider.setRange(0, 0)
        self.program_slider.setEnabled(False)
        self.program_slider.setToolTip(I18N.tr("edit.viewport.scrub", "Scrub Timeline"))
        self.program_slider.setAccessibleName(self.program_slider.toolTip())
        self.program_slider.valueChanged.connect(self.program_seek_requested.emit)
        viewer.layout().addWidget(self.program_slider)
        viewer.layout().addWidget(footer)
        footer.position_groups()
        self.program_footer = footer
        return header

    @staticmethod
    def _unavailable(menu: QMenu, text: str) -> None:
        """Keep backend capability honest while matching the reference menus."""
        menu.setToolTipsVisible(True)
        action = menu.addAction(text)
        action.setEnabled(False)
        action.setToolTip(I18N.tr("edit.viewport.backend_pending", "Not available in this version"))

    @staticmethod
    def _footer(viewer: QWidget) -> tuple[_MonitorFooter, QHBoxLayout, QHBoxLayout, QHBoxLayout]:
        footer = _MonitorFooter(viewer)
        return footer, footer.groups[0], footer.groups[1], footer.groups[2]

    @staticmethod
    def _pending_button(footer: _MonitorFooter, icon: str, label: str) -> QToolButton:
        button = _tool(icon, label, footer)
        button.setEnabled(False)
        button.setToolTip(label + " — " + I18N.tr("edit.viewport.backend_pending", "Not available in this version"))
        footer.optional_controls.append(button)
        return button

    # ── I03：capability 动作（注册表单一事实源驱动 enabled/reason） ──

    def _register_capability_action(self, key: str, action: Any) -> None:
        """同键可注册多个控件（菜单项 + 底栏按钮），回填共享。"""
        self._capability_actions.setdefault(key, []).append(action)
        self._capability_labels.setdefault(
            key, action.text() if hasattr(action, "text") else "")

    def _capability_menu_action(self, menu: QMenu, key: str, label: str) -> QAction:
        """有真实 handler 的菜单项：宿主触发；未回填前默认禁用。"""
        action = menu.addAction(label)
        action.setEnabled(False)
        action.setToolTip(label + " — " + I18N.tr("edit.viewport.backend_pending", "Not available in this version"))
        action.triggered.connect(lambda: self._dispatch_capability_action(key))
        self._register_capability_action(key, action)
        self._capability_labels[key] = label
        return action

    def _capability_footer_button(self, footer: _MonitorFooter, icon: str,
                                  label: str, key: str) -> QToolButton:
        """监视器底栏的能力按钮（与菜单同键，回填共享）。"""
        button = _tool(icon, label, footer)
        button.setEnabled(False)
        button.setToolTip(label + " — " + I18N.tr("edit.viewport.backend_pending", "Not available in this version"))
        button.clicked.connect(lambda: self._dispatch_capability_action(key))
        footer.optional_controls.append(button)
        self._register_capability_action(key, button)
        self._capability_labels[key] = label
        return button

    def _dispatch_capability_action(self, key: str) -> None:
        """本地可消化的动作在容器内执行，其余交给宿主。"""
        if key == "source.reverse":
            reverse = getattr(self.source_viewer, "play_reverse", None)
            if callable(reverse):
                reverse()
            return
        self.capability_action_requested.emit(key)

    def refresh_capabilities(self, caps_by_action: dict) -> None:
        """I03：按注册表回填 enabled/tooltip（宿主在状态变化与菜单展开时调用）。"""
        for key, actions in self._capability_actions.items():
            cap = caps_by_action.get(_CAPABILITY_REGISTRY_KEYS.get(key, ""))
            if cap is None:
                continue
            label = self._capability_labels.get(key, "")
            for action in actions:
                blocker = QSignalBlocker(action)
                action.setEnabled(bool(cap.supported))
                action.setToolTip(
                    label if cap.supported
                    else f"{label} — {cap.reason}"
                    if cap.reason else label)
                del blocker

    @staticmethod
    def _stop_button(stop: Callable[[], None], parent: QWidget) -> QToolButton:
        button = _tool("stop", I18N.tr("edit.viewport.stop", "Stop"), parent)
        button.clicked.connect(stop)
        return button

    def _sync_source_loop_action(self) -> None:
        """Mirror the existing Source loop policy without starting playback."""
        action = self.source_loop_action
        blocker = QSignalBlocker(action)
        action.setChecked(self.source_viewer.loop_enabled)
        action.setEnabled(self.source_viewer.current_asset is not None)
        del blocker

    def set_dual_view(self, enabled: bool) -> None:
        """Switch monitor presentation while retaining both viewer instances."""
        enabled = bool(enabled)
        if self._deliver_mode or enabled == self._dual_view:
            self._sync_buttons()
            return
        if self._dual_view:
            self._dual_sizes = self.splitter.sizes()
        self._dual_view = enabled
        self._apply_mode()
        if enabled:
            self.splitter.setSizes(self._dual_sizes or [max(1, self.width() // 2)] * 2)
        self.mode_changed.emit(enabled)

    def show_source(self, focus: bool = True) -> None:
        """Reveal an opened asset without changing the single/dual preference."""
        if not self._deliver_mode:
            self._activate("source", focus)

    def restore_splitter_sizes(self, sizes: list[int]) -> None:
        """Restore saved dual proportions even when Source is currently hidden."""
        if len(sizes) != 2 or any(
            isinstance(value, bool) or not isinstance(value, int) or value <= 0
            for value in sizes
        ):
            raise ValueError("Viewer splitter sizes must contain two positive integers")
        self._dual_sizes = list(sizes)
        if self._dual_view:
            self.splitter.setSizes(self._dual_sizes)

    def show_program(self, focus: bool = True) -> None:
        """Reveal the timeline monitor and optionally transfer keyboard focus."""
        if not self._deliver_mode:
            self._activate("program", focus)

    def _activate(self, monitor: str, focus: bool) -> None:
        changed = self._active_monitor != monitor
        self._active_monitor = monitor
        self._apply_mode()
        if focus:
            viewer = self.source_viewer if monitor == "source" else self.program_viewer.get_viewer()
            viewer.setFocus(Qt.FocusReason.OtherFocusReason)
        if changed:
            self.active_monitor_changed.emit(monitor)

    def _apply_mode(self) -> None:
        self.source_pane.setVisible(self._dual_view or self._active_monitor == "source")
        self.program_pane.setVisible(self._dual_view or self._active_monitor == "program")
        self._sync_buttons()

    def _sync_buttons(self) -> None:
        for monitor, header in (("source", self.source_header), ("program", self.program_header)):
            for button, checked in (
                (header.source_button, self._active_monitor == "source"),
                (header.timeline_button, self._active_monitor == "program"),
                (header.single_button, not self._dual_view),
                (header.dual_button, self._dual_view),
            ):
                blocker = QSignalBlocker(button)
                button.setChecked(checked)
                del blocker
                button.setEnabled(not self._deliver_mode)
            header.show_monitor_switches = (not self._dual_view or monitor == "source") and not self._deliver_mode
            modes_visible = (not self._dual_view or monitor == "program") and not self._deliver_mode
            header.single_button.setVisible(modes_visible and self._dual_view)
            header.dual_button.setVisible(modes_visible and not self._dual_view)
            header.set_active(monitor == self._active_monitor and self._dual_view)

    def set_deliver_mode(self, enabled: bool) -> None:
        """Hide workspace-only switches while Deliver borrows Program."""
        self._deliver_mode = bool(enabled)
        self._sync_buttons()

    def prepare_for_deliver(self) -> None:
        """Keep monitor controls live while removing Edit workspace switches."""
        self.set_deliver_mode(True)

    def restore_program_viewer(self) -> None:
        """Return a borrowed complete Program widget to its permanent slot."""
        self.program_pane.layout().addWidget(self.program_viewer)
        self.program_viewer.show()
        self.set_deliver_mode(False)
        self._apply_mode()

    def bind_timeline_widget(self, widget: QWidget) -> None:
        """Let a timeline click/focus reveal Program, without per-frame switching."""
        if self._timeline_widget is not None:
            self._timeline_widget.removeEventFilter(self)
            for child in self._timeline_widget.findChildren(QWidget):
                child.removeEventFilter(self)
        self._timeline_widget = widget
        widget.installEventFilter(self)
        for child in widget.findChildren(QWidget):
            child.installEventFilter(self)

    def set_program_range(self, total_frames: int, fps: float | Rational = 24.0) -> None:
        """Present the host's timeline duration without changing its playhead."""
        self._program_fps = float(fps) if float(fps) > 0.0 else 24.0
        frames = max(0, int(total_frames))
        blocker = QSignalBlocker(self.program_slider)
        self.program_slider.setRange(0, max(0, frames - 1))
        self.program_slider.setEnabled(frames > 0)
        del blocker
        rate = fps if isinstance(fps, Rational) and float(fps) > 0.0 else Rational.from_float(self._program_fps)
        try:
            text = Timecode.from_frame_number(frames, rate, Timecode.auto_detect_type(rate)).to_string()
        except ValueError as exc:
            # Durations can exceed SMPTE's 24-hour clock; keep these readable
            # without changing the timeline's existing timecode semantics.
            logger.debug("Monitor duration exceeds timecode domain: %s", exc)
            seconds, frame = divmod(frames, max(1, round(self._program_fps)))
            minutes, second = divmod(seconds, 60)
            hour, minute = divmod(minutes, 60)
            text = f"{hour:02d}:{minute:02d}:{second:02d}:{frame:02d}"
        self.program_header.duration.setText(text)

    def set_program_position(self, frame: int) -> None:
        """Follow presentation updates without feeding them back into seeks."""
        if self.program_slider.isSliderDown():
            return
        blocker = QSignalBlocker(self.program_slider)
        self.program_slider.setValue(int(frame))
        del blocker

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        """Activate the clicked/focused monitor without swallowing its input."""
        if not self._deliver_mode and event.type() in {QEvent.Type.MouseButtonPress, QEvent.Type.FocusIn}:
            if isinstance(watched, QWidget):
                if watched is self.source_viewer or self.source_viewer.isAncestorOf(watched):
                    self._activate("source", False)
                elif watched is self.program_viewer or self.program_viewer.isAncestorOf(watched):
                    self._activate("program", False)
                elif self._timeline_widget is not None and (
                    watched is self._timeline_widget or self._timeline_widget.isAncestorOf(watched)
                ):
                    self._activate("program", False)
        return super().eventFilter(watched, event)
