"""Edit monitor presentation, measured against Resolve's single/dual viewers.

Only UI chrome and focus live here. Existing monitor instances, media clocks,
frame providers and transport signals retain their original owners.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Callable

from PyQt6.QtCore import QEvent, QObject, QRectF, QSignalBlocker, QSize, Qt, pyqtSignal
from PyQt6.QtGui import QAction, QFont, QIcon, QResizeEvent
from PyQt6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QLayout, QMenu, QSizePolicy, QSlider,
    QToolButton, QVBoxLayout, QWidget,
)

from src.shared.ui.panel_splitter import PanelSplitter
from src.shared.ui.style_sheet import UI_FONT_FAMILIES, UI_MONO_FONT_FAMILIES
from src.shared.i18n.i18n import I18N
from src.shared.media.metadata import Rational
from src.shared.media.timecode import Timecode
from src.shared.ui.viewport_icons import viewer_icon, viewer_icon_size
from src.shared.ui.widgets.viewport_toolbar_base import ViewerZoomMenuButton
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
HEADER_TITLE_GAP = 8
HEADER_TITLE_RESERVE = 144
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
    """Create a flat monitor control using the shared viewer artwork."""
    button = QToolButton(parent)
    button.setIcon(viewer_icon(icon))
    button.setIconSize(viewer_icon_size(icon))
    button.setFixedSize(width, 28)
    button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
    button.setToolTip(tooltip)
    button.setAccessibleName(tooltip)
    button.setStyleSheet(_BUTTON_STYLE)
    return button


class _DragSourceButton(QToolButton):
    """I04-1：Video Only／Audio Only 图标拖出源（按下即起拖，无点击动作）。"""

    def __init__(self, icon: str, tooltip: str, intent: str,
                 starter: Callable[[str], object], parent: QWidget) -> None:
        super().__init__(parent)
        self.setIcon(viewer_icon(icon))
        self.setIconSize(QSize(20, 20))
        self.setFixedSize(32, 28)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setToolTip(tooltip)
        self.setAccessibleName(tooltip)
        self.setStyleSheet(_BUTTON_STYLE)
        self.setCursor(Qt.CursorShape.DragCopyCursor)
        self._intent = intent
        self._starter = starter

    def mousePressEvent(self, event: QEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            try:
                self._starter(self._intent)
            except Exception:
                logger.exception("source clip drag failed to start")
            event.accept()
            return
        super().mousePressEvent(event)


def _adopt(layout: QHBoxLayout, widget: QToolButton, icon: str, width: int = 32) -> None:
    """Keep a live button and its connections, replacing only its artwork."""
    widget.setText("")
    widget.setIcon(viewer_icon(icon))
    widget.setIconSize(viewer_icon_size(icon))
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
        self.mixer_mode = False
        self.optional_controls: list[QWidget] = []
        self.narrow_controls: list[QWidget] = []
        self.compact_controls: list[QWidget] = []
        self.range_buttons: tuple[QToolButton, ...] = ()
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
        if self.range_buttons:
            space = (self.width() - center_width) // 2 - 10
            button_width = max(1, min(26, space // len(self.range_buttons)))
            icon_size = min(20, button_width)
            for button in self.range_buttons:
                button.setFixedWidth(button_width)
                button.setIconSize(QSize(icon_size, icon_size))
        left_width = self.left_group.sizeHint().width()
        right_width = self.right_group.sizeHint().width()
        y = (self.height() - 28) // 2
        self.left_group.setGeometry(10, y, left_width, 28)
        self.center_group.setGeometry((self.width() - center_width) // 2, y, center_width, 28)
        self.right_group.setGeometry(self.width() - right_width - 10, y, right_width, 28)
        self.left_group.setVisible(
            not self.mixer_mode and self.left_group.geometry().right() < self.center_group.x())
        self.right_group.setVisible(
            not self.mixer_mode and self.center_group.geometry().right() < self.right_group.x())
        self.marker_group.setVisible(not self.mixer_mode and self.width() >= 700)
        self.marker_group.setGeometry(self.center_group.x() - 90, y, 54, 28)


class _MonitorHeader(QFrame):
    """Reference-aligned cells with an independently centered timeline title."""

    def __init__(self, title: QWidget, timecode: QLabel, duration: QLabel, parent: QWidget) -> None:
        super().__init__(parent)
        self.setObjectName("editMonitorHeader")
        self.setFixedHeight(HEADER_HEIGHT)
        self.setMinimumWidth(0)
        self.show_monitor_switches = True
        self.mixer_mode = False
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
        self.zoom_button = ViewerZoomMenuButton(self.left_group)
        self.zoom_button.setFixedSize(62, 28)
        self.zoom_button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.zoom_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
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
        self._style_timecode()
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
        title.setFont(QFont(UI_FONT_FAMILIES, 12, QFont.Weight.DemiBold))
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
        width = self.width()
        for button in (self.source_button, self.timeline_button):
            button.setVisible(self.show_monitor_switches)
        self.source_tape_button.setVisible(self.show_monitor_switches and width >= 560)

        # Reserve room for the centered title before showing optional cells.
        # All header cells are fixed-width, so this is independent of the
        # previous layout pass, the selected timeline name, and frame updates.
        side_budget = (width - HEADER_TITLE_RESERVE) // 2 - HEADER_TITLE_GAP
        left_tools = (self.source_button, self.source_tape_button, self.timeline_button, self.zoom_button)
        left_width = sum(tool.minimumWidth() for tool in left_tools if not tool.isHidden())
        duration_width = self.duration.minimumWidth()
        show_duration = not self.mixer_mode and width >= 560 and left_width + duration_width <= side_budget
        self.duration.setVisible(show_duration)
        right_tools = (self.timecode, self.single_button, self.dual_button, self.options_button)
        right_width = sum(tool.minimumWidth() for tool in right_tools if not tool.isHidden())
        extra_width = sum(tool.minimumWidth() for tool in self.extra_controls)
        show_extras = not self.mixer_mode and width >= 760 and right_width + extra_width <= side_budget
        for control in self.extra_controls:
            control.setVisible(show_extras)

        occupied = max(left_width + (duration_width if show_duration else 0),
                       right_width + (extra_width if show_extras else 0))
        title_width = min(320, max(0, width - 2 * (occupied + HEADER_TITLE_GAP)))
        # The combo's delayed AdjustToContents resize must retain the size
        # assigned by the header. Standalone toolbars still size to contents.
        self.title.setFixedSize(title_width, HEADER_HEIGHT)
        self.title.move((width - title_width) // 2, 0)
        self.title.setVisible(title_width >= 48)
        self.title.raise_()

    def _style_timecode(self) -> None:
        size = 15 if self.mixer_mode else 12
        self.timecode.setFont(QFont(UI_MONO_FONT_FAMILIES if self.mixer_mode
                                    else UI_FONT_FAMILIES, size, QFont.Weight.DemiBold))
        self.timecode.setFixedWidth(116 if self.mixer_mode else 90)
        self.timecode.setStyleSheet(
            f"color: #e1e1e3; background: transparent; padding: 0; font-size: {size}px; font-weight: 600;")

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
    program_record_requested = pyqtSignal()
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
        self._program_only = False
        self._mixer_picture_options: QMenu | None = None
        self._mixer_transport_layout: QHBoxLayout | None = None
        self._program_fps = 24.0
        self._dual_sizes: list[int] | None = None
        self._timeline_widget: QWidget | None = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self.splitter = PanelSplitter(Qt.Orientation.Horizontal, self)
        self.splitter.setChildrenCollapsible(False)
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
        source_viewer.range_changed.connect(self._sync_scrubber_visibility)
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
        clear_label = I18N.tr("timeline.range.clear", "Clear Range")
        clear_action = options.addAction(viewer_icon("clear_range"), clear_label, bar.btn_clear.click)
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
        # I04-1：流意图拖出源（真实 handler：按下起拖；拖拽可行性由
        # 生产端按素材流如实裁决，不虚设禁用态）。
        def _start_source_drag(intent: str) -> object:
            starter = getattr(self.source_viewer, "start_source_clip_drag",
                              None)
            return starter(intent) if callable(starter) else None

        for glyph, intent, label in (
                ("video_only", "video", "Video Only"),
                ("audio_only", "audio", "Audio Only")):
            button = _DragSourceButton(
                glyph, I18N.tr("edit.viewport." + glyph, label), intent,
                _start_source_drag, footer)
            footer.optional_controls.append(button)
            left.addWidget(button)
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
        bar.btn_clear.setToolTip(clear_label)
        _adopt(right, bar.btn_clear, "clear_range", 26)
        footer.range_buttons = (bar.btn_mark_in, bar.btn_mark_out, bar.btn_clear)
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
        self._program_options = options
        guides = toolbar.guides_menu_btn
        guides.setFixedSize(42, 28)
        guides.setStyleSheet(_BUTTON_STYLE)
        header.add_display_control(guides)
        proxy = _tool("proxy", I18N.tr("edit.viewport.proxy", "Proxy Handling"), header, 48)
        quality = _tool("resolution", I18N.tr("edit.viewport.quality", "Timeline Proxy Resolution"), header, 48)
        quality.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
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
        record_label = I18N.tr("mixer.record.transport_record", "Record")
        self.program_record_button = _tool("record", record_label, footer)
        self.program_record_button.setObjectName("mixerTransportRecord")
        self.program_record_button.setEnabled(False)
        self.program_record_button.hide()
        self.program_record_button.clicked.connect(self.program_record_requested.emit)
        self._program_record_action = options.addAction(record_label, self.program_record_button.click)
        self._program_record_action.setVisible(False)
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
        self.timeline_monitor_status = QLabel(viewer)
        self.timeline_monitor_status.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.timeline_monitor_status.hide()
        viewer.layout().addWidget(self.timeline_monitor_status)
        footer.position_groups()
        self.program_footer = footer
        return header

    def set_mixer_recording_controls(
        self, visible: bool, *, can_record: bool = False, recording: bool = False,
    ) -> None:
        """Present the Mixer transport's record and stop controls."""
        stop = self.program_stop_button
        record = self.program_record_button
        center = self._program_controls_layout()
        controls_present = center.indexOf(record) >= 0
        if visible != controls_present:
            if visible:
                play_index = center.indexOf(self._program_transport.btn_play)
                center.insertWidget(play_index + 1, stop)
                center.insertWidget(play_index + 2, record)
            else:
                center.removeWidget(stop)
                center.removeWidget(record)
            stop.setVisible(visible)
            record.setVisible(visible)
            self._program_record_action.setVisible(visible)
            self.program_footer.position_groups()

        can_start = visible and can_record and not recording
        record.setEnabled(can_start)
        self._program_record_action.setEnabled(can_start)
        record.setIcon(viewer_icon("record_active" if recording else "record"))
        if recording:
            record.setToolTip(I18N.tr("mixer.record.transport_recording", "Recording armed tracks"))
            stop.setToolTip(I18N.tr("mixer.record.stop", "Stop recording"))
        else:
            label = (I18N.tr("mixer.record.transport_start", "Record armed tracks (Shift+Space)") if can_record
                     else I18N.tr("mixer.record.transport_arm_first", "Arm a track to record"))
            record.setToolTip(label)
            stop.setToolTip(I18N.tr("edit.viewport.stop", "Stop"))
        record.setAccessibleDescription(record.toolTip())
        stop.setAccessibleName(stop.toolTip())

    def bind_mixer_transport(self, controls: QHBoxLayout) -> None:
        """Provide the timeline-side layout for the existing transport buttons."""
        self._mixer_transport_layout = controls

    def _program_controls_layout(self) -> QHBoxLayout:
        if self.program_footer.mixer_mode and self._mixer_transport_layout is not None:
            return self._mixer_transport_layout
        return self.program_footer.groups[1]

    def _place_program_transport(self, mixer: bool) -> None:
        mixer_controls = self._mixer_transport_layout
        if mixer_controls is None:
            return
        footer_controls = self.program_footer.groups[1]
        source, destination = ((footer_controls, mixer_controls) if mixer
                               else (mixer_controls, footer_controls))
        while source.count():
            button = source.takeAt(0).widget()
            if button is not None:
                destination.addWidget(button)
                button.show()

        if not mixer:
            for button in (self.program_stop_button, self.program_record_button):
                if not self.program_footer.isAncestorOf(button):
                    button.setParent(self.program_footer)
        self.program_footer.setVisible(not mixer)

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
        if self._deliver_mode or self._program_only or enabled == self._dual_view:
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
        if not self._deliver_mode and not self._program_only:
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
        if not self._deliver_mode and not self._program_only:
            self._activate("program", focus)

    def _activate(self, monitor: str, focus: bool) -> None:
        changed = self._active_monitor != monitor
        if changed:
            self._active_monitor = monitor
            self._apply_mode()
        if focus:
            viewer = self.source_viewer if monitor == "source" else self.program_viewer.get_viewer()
            viewer.setFocus(Qt.FocusReason.OtherFocusReason)
        if changed:
            self.active_monitor_changed.emit(monitor)

    def _apply_mode(self) -> None:
        self.source_pane.setVisible(not self._program_only and (self._dual_view or self._active_monitor == "source"))
        self.program_pane.setVisible(self._program_only or self._dual_view or self._active_monitor == "program")
        self._sync_scrubber_visibility()
        self._sync_buttons()

    def _sync_scrubber_visibility(self) -> None:
        """Show the timeline range alongside Source in dual-monitor mode."""
        self.source_viewer.slider.setVisible(
            not self._program_only and not self._deliver_mode
            and self.source_viewer.current_asset is not None
            and (self._dual_view or self._active_monitor == "source")
        )
        self.program_slider.setVisible(
            not self.program_header.mixer_mode
            and (self._dual_view or self._program_only or self._deliver_mode)
        )

    def _sync_buttons(self) -> None:
        switches_locked = self._deliver_mode or self._program_only
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
                button.setEnabled(not switches_locked)
            header.show_monitor_switches = (not self._dual_view or monitor == "source") and not switches_locked
            modes_visible = (not self._dual_view or monitor == "program") and not switches_locked
            header.single_button.setVisible(modes_visible and self._dual_view)
            header.dual_button.setVisible(modes_visible and not self._dual_view)
            header.set_active(monitor == self._active_monitor and self._dual_view and not self._program_only)

    def set_program_only(self, enabled: bool, *, color_monitor: bool = False) -> None:
        """Temporarily show Program without altering the Edit pane preferences."""
        if enabled == self._program_only:
            return
        previous_sizes = self.splitter.sizes()
        if enabled and self._dual_view and all(previous_sizes):
            self._dual_sizes = previous_sizes
        self._program_only = enabled
        header = self.program_header
        mixer = enabled and not color_monitor
        if mixer and self._mixer_picture_options is None:
            options = self._mixer_picture_options = QMenu(header)
            options.addMenu(self._program_toolbar.view_menu)
            options.addMenu(self._program_toolbar.quality_menu)
            options.addAction(I18N.tr("viewport.fullscreen", "Fullscreen"),
                              self.program_viewer.toggle_fullscreen)
        header.mixer_mode = mixer
        header._style_timecode()
        header.options_button.setMenu(self._mixer_picture_options if mixer else self._program_options)
        header.options_button.setToolTip(
            I18N.tr("mixer.monitor.picture_options", "Picture Options") if mixer
            else I18N.tr("edit.viewport.options", "Viewer Options"))
        header.options_button.setAccessibleName(header.options_button.toolTip())
        self.timeline_monitor_status.setVisible(enabled and color_monitor)
        self.program_footer.mixer_mode = mixer
        self._place_program_transport(mixer)
        self.program_footer.position_groups()
        self._apply_mode()
        if not enabled and self._dual_view and self._dual_sizes:
            self.splitter.setSizes(self._dual_sizes)

    def set_deliver_mode(self, enabled: bool) -> None:
        """Hide workspace-only switches while Deliver borrows Program."""
        self._deliver_mode = bool(enabled)
        self._sync_scrubber_visibility()
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

    def set_program_range(
        self, total_frames: int, fps: float | Rational = 24.0,
        start: int | None = None, end: int | None = None,
    ) -> None:
        """Present the host's timeline duration without changing its playhead."""
        self._program_fps = float(fps) if float(fps) > 0.0 else 24.0
        frames = max(0, int(total_frames))
        blocker = QSignalBlocker(self.program_slider)
        self.program_slider.setRange(0, max(0, frames - 1))
        self.program_slider.setEnabled(frames > 0)
        self.program_slider.set_in_out_points(start, end)
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
        if (event.type() in {QEvent.Type.MouseButtonPress, QEvent.Type.FocusIn}
                and not (getattr(self, "_deliver_mode", True) or self._program_only)):
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
