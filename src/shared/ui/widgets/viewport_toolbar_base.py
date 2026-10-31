"""
Shared base class for viewport toolbar overlays.

Provides the common QFrame shell, QWidget content container, button style
constants, and factory helpers used by all three viewport toolbars
(Composite, Color, Edit).  Each page subclasses ``ViewportToolBarBase``
and adds its own specific controls via ``self._layout``.
"""

from typing import Any, Optional

from PyQt6.QtWidgets import (
    QFrame, QWidget, QHBoxLayout, QToolButton, QLabel, QMenu,
    QComboBox,
)
from PyQt6.QtCore import Qt, QSize, QPointF, QSignalBlocker, pyqtSignal

from src.shared.i18n.i18n import I18N
from src.shared.assets.icon_manager import icon_mask, get_icon, IconColors
from src.shared.ui.widgets.chevron_combo import ChevronComboBox
from src.shared.ui.widgets.realtime_fps_indicator import RealtimeFpsIndicator


class ViewportToolBarBase(QFrame):
    """Base class for viewport toolbar overlays.

    Provides:
    - QFrame shell (28 px, border-bottom)
    - QWidget content container (solid #202126 background)
    - ``_BTN_STYLE`` constant for transparent toolbar buttons
    - Factory helpers for standard buttons (Mask, Fit, Split, Guides, menu)
    - Zoom percentage label
    - Common signals shared across all three pages
    """

    # Unified inline style for ALL toolbar buttons.
    # Applied via setStyleSheet() on each button to fully override native
    # Fusion rendering (global QSS inheritance does NOT suppress the
    # PE_IndicatorButtonDefault indicator line on macOS).
    _BTN_STYLE = """
        QToolButton {
            background: transparent;
            border: 0;
            border-radius: 4px;
            padding: 3px;
            margin: 0px;
        }
        QToolButton:hover {
            background-color: #2c2d33;
        }
        QToolButton:pressed {
            background-color: #282930;
        }
        QToolButton:checked {
            background: transparent;
        }
        QToolButton:checked:hover {
            background-color: #2c2d33;
        }
        QToolButton:disabled {
            color: #444;
        }
    """

    # ── Common signals ──────────────────────────────────────────────────
    fit_requested = pyqtSignal()
    actual_size_requested = pyqtSignal()
    zoom_in_requested = pyqtSignal()
    zoom_out_requested = pyqtSignal()
    fullscreen_requested = pyqtSignal()
    focus_mode_requested = pyqtSignal()
    mask_toggled = pyqtSignal(bool)
    split_toggled = pyqtSignal(bool)
    guide_changed = pyqtSignal(str, str, dict)  # guide_type, guide_id, params

    # ── Construction ────────────────────────────────────────────────────

    def __init__(self, object_name: str, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName(object_name)
        self.setFixedHeight(28)
        self.setMaximumHeight(28)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setAutoFillBackground(False)

        # QFrame only handles the bottom border via QSS
        self.setStyleSheet(f"""
            QFrame#{object_name} {{
                background: transparent;
                border-bottom: 1px solid #383a40;
            }}
        """)

        # Outer layout for the QFrame (contains the content container)
        outer_layout = QHBoxLayout(self)
        outer_layout.setContentsMargins(0, 0, 0, 1)  # 1 px bottom for border
        outer_layout.setSpacing(0)

        # Content container: solid background, properly filled
        self._content = QWidget()
        self._content.setStyleSheet("background-color: #202126;")
        self._layout = QHBoxLayout(self._content)
        self._layout.setContentsMargins(4, 0, 4, 0)
        self._layout.setSpacing(2)

        outer_layout.addWidget(self._content)

        # Shared by the Color, Composite, and Edit preview toolbars. Each
        # subclass places it in the right-aligned group after its own controls.
        self.fps_indicator = RealtimeFpsIndicator(self)

        # AUD-38417: 程序化 setChecked 会再次触发 toggled。经
        # _set_checked_programmatic 设置状态时该标志为 True，
        # _on_mask_toggled/_on_split_toggled 据此只同步图标、不重发信号。
        self._programmatic_toggle = False

    # ── Programmatic toggle state lock (AUD-38417) ──────────────────────

    def _set_checked_programmatic(self, button: "QToolButton", checked: bool) -> None:
        """程序化设置 toggle 按钮状态，不重发 mask_toggled/split_toggled。

        子类（如同步快捷键/会话恢复）需要以代码切换 Mask/Split 状态时，
        必须经由本辅助而非直接 setChecked，否则 toggled 处理器会把程序化
        变更当作用户操作再次广播。
        """
        if button.isChecked() == checked:
            return
        self._programmatic_toggle = True
        try:
            button.setChecked(checked)
        finally:
            self._programmatic_toggle = False

    # ── Factory helpers ─────────────────────────────────────────────────

    def _make_menu_button(self, text: str) -> QToolButton:
        """Create a text-style menu launcher button (View, Guides, etc.)."""
        btn = QToolButton()
        btn.setText(text)
        btn.setFixedHeight(22)
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        btn.setAutoRaise(True)
        btn.setStyleSheet(self._BTN_STYLE)
        return btn

    def _show_menu_at_button(self, menu: QMenu, button: QToolButton) -> None:
        """Position and show *menu* directly below *button*."""
        button.setAttribute(Qt.WidgetAttribute.WA_UnderMouse, False)
        button.clearFocus()
        pos = button.mapToGlobal(button.rect().bottomLeft())
        menu.exec(pos)
        button.setAttribute(Qt.WidgetAttribute.WA_UnderMouse, True)

    def _make_mask_btn(self) -> QToolButton:
        """Create the Mask (alpha preview) toggle button."""
        self.mask_btn = QToolButton()
        self.mask_btn.setObjectName("maskBtn")
        self.mask_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.mask_btn.setAutoRaise(True)
        self.mask_btn.setStyleSheet(self._BTN_STYLE)
        self.mask_btn.setIcon(icon_mask(IconColors.DEFAULT))
        self.mask_btn.setIconSize(QSize(15, 15))
        self.mask_btn.setCheckable(True)
        self.mask_btn.setFixedSize(26, 26)
        self.mask_btn.setToolTip(
            I18N.tr("tooltip.toggle_mask", "Toggle Alpha Mask Preview (Shift+H)"))
        self.mask_btn.toggled.connect(self._on_mask_toggled)
        return self.mask_btn

    def _make_fit_btn(self) -> QToolButton:
        """Create the Fit-to-Window button."""
        self.fit_btn = QToolButton()
        self.fit_btn.setObjectName("fitBtn")
        self.fit_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.fit_btn.setAutoRaise(True)
        self.fit_btn.setStyleSheet(self._BTN_STYLE)
        self.fit_btn.setIcon(get_icon('fa5s.expand-arrows-alt', color=IconColors.DEFAULT))
        self.fit_btn.setIconSize(QSize(15, 15))
        self.fit_btn.setFixedSize(26, 26)
        self.fit_btn.setToolTip(I18N.tr("tooltip.fit_window", "Fit to Window"))
        self.fit_btn.clicked.connect(self.fit_requested.emit)
        return self.fit_btn

    def _make_split_btn(self) -> QToolButton:
        """Create the Split-view toggle button."""
        self.split_btn = QToolButton()
        self.split_btn.setObjectName("splitBtn")
        self.split_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.split_btn.setAutoRaise(True)
        self.split_btn.setStyleSheet(self._BTN_STYLE)
        self.split_btn.setIcon(get_icon('fa5s.columns', color=IconColors.DEFAULT))
        self.split_btn.setIconSize(QSize(15, 15))
        self.split_btn.setCheckable(True)
        self.split_btn.setFixedSize(26, 26)
        self.split_btn.setToolTip(
            I18N.tr("tooltip.toggle_split", "Toggle Split View (\\\)"))
        self.split_btn.toggled.connect(self._on_split_toggled)
        return self.split_btn

    def _make_guides_btn(self) -> QToolButton:
        """Create the Guides button (opens ViewerGuidesPanel on click)."""
        self.guides_menu_btn = self._make_menu_button(
            I18N.tr("viewport.guides", "Guides"))
        self._guides_panel = None
        self.guides_menu_btn.clicked.connect(self._show_guides_panel)
        return self.guides_menu_btn

    def _show_guides_panel(self) -> None:
        """Lazily build and show the viewer guides popup anchored below the Guides button."""
        from src.features.color.ui.viewer_guides import ViewerGuidesPanel

        if self._guides_panel is None:
            self._guides_panel = ViewerGuidesPanel()
            self._guides_panel.guide_selected.connect(
                lambda gtype, gid, params: self.guide_changed.emit(gtype, gid, params))

        self.guides_menu_btn.setAttribute(Qt.WidgetAttribute.WA_UnderMouse, False)
        self.guides_menu_btn.clearFocus()
        pos = self.guides_menu_btn.mapToGlobal(
            self.guides_menu_btn.rect().bottomLeft())
        self._guides_panel.show_at(pos)

    def _make_zoom_label(self) -> QLabel:
        """Create the zoom percentage display label."""
        self.zoom_label = QLabel("100%")
        self.zoom_label.setObjectName("zoomLabel")
        self.zoom_label.setFixedWidth(50)
        self.zoom_label.setStyleSheet("color: #999; font-size: 11px; padding: 0 4px;")
        self.zoom_label.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        return self.zoom_label

    def update_zoom_label(self, zoom_level: float) -> None:
        """Update zoom percentage display."""
        if hasattr(self, 'zoom_label'):
            pct = int(round(zoom_level * 100))
            self.zoom_label.setText(f"{pct}%")

    def _add_fps_indicator(self) -> QLabel:
        """Add the shared FPS readout to this toolbar's layout."""
        self._layout.addWidget(self.fps_indicator)
        return self.fps_indicator

    def set_playback_state(self, is_playing: bool, target_fps: float = 24.0) -> None:
        """Update playback state and target FPS for the shared indicator."""
        self.fps_indicator.set_playback_state(is_playing, target_fps)

    def record_presented_frame(self, frame_token: object = None) -> None:
        """Record a frame that was actually painted by this viewer."""
        self.fps_indicator.record_presented_frame(frame_token)

    # ── Internal toggle handlers ────────────────────────────────────────

    def _on_mask_toggled(self, checked: bool) -> None:
        """Toggle mask icon color: orange when active, default when inactive."""
        color = IconColors.ORANGE if checked else IconColors.DEFAULT
        self.mask_btn.setIcon(icon_mask(color))
        # AUD-38417: 程序化 setChecked 引发的 toggled 只同步图标，不重发信号
        if getattr(self, '_programmatic_toggle', False):
            return
        self.mask_toggled.emit(checked)

    def _on_split_toggled(self, checked: bool) -> None:
        """Toggle split icon color: orange when active, default when inactive."""
        color = IconColors.ORANGE if checked else IconColors.DEFAULT
        self.split_btn.setIcon(get_icon('fa5s.columns', color=color))
        # AUD-38417: 程序化 setChecked 引发的 toggled 只同步图标，不重发信号
        if getattr(self, '_programmatic_toggle', False):
            return
        self.split_toggled.emit(checked)


class ViewportTimelineSelector(ChevronComboBox):
    """Compact, borderless timeline selector for viewport toolbars."""

    timeline_switch_requested = pyqtSignal(str)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setObjectName("viewportTimelineSelector")
        self.setFixedHeight(28)
        self.setMinimumWidth(80)
        self.setMaximumWidth(320)
        self.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToContents)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setToolTip(I18N.tr("edit.program_viewer.switch_timeline", "Switch timeline"))
        self.setStyleSheet("""
            QComboBox#viewportTimelineSelector {
                background: transparent;
                border: none;
                border-radius: 0px;
                padding: 2px 22px 2px 10px;
                color: #f0f0f0;
                font-size: 12px;
                font-weight: 600;
                min-height: 24px;
                max-height: 24px;
            }
            QComboBox#viewportTimelineSelector:hover { color: #ffffff; }
            QComboBox#viewportTimelineSelector:focus {
                background: transparent;
                border: none;
            }
            QComboBox#viewportTimelineSelector::drop-down {
                border: none;
                width: 0px;
            }
            QComboBox#viewportTimelineSelector::down-arrow {
                image: none;
                border: none;
            }
        """)
        self.currentIndexChanged.connect(self._on_index_changed)

    def set_timeline_choices(
        self,
        timelines: Any,
        active_timeline_id: Optional[str],
    ) -> None:
        """Replace choices and preserve the active timeline selection."""
        entries = list(getattr(timelines, "items", lambda: ())())
        blocker = QSignalBlocker(self)
        try:
            self.clear()
            for timeline_id, timeline_ref in entries:
                name = str(getattr(timeline_ref, "name", "") or timeline_id)
                self.addItem(name, str(timeline_id))

            if not entries:
                self.hide()
                return

            active_index = self.findData(str(active_timeline_id))
            self.setCurrentIndex(active_index if active_index >= 0 else 0)
            self.show()
        finally:
            del blocker

    def showPopup(self) -> None:
        """Keep the popup readable without widening the closed toolbar."""
        super().showPopup()
        popup = self.view().window()
        if popup is None:
            return

        popup_width = max(self.width(), self.sizeHint().width())
        popup.setMinimumWidth(popup_width)
        popup.resize(popup_width, popup.height())

        pos = self.mapToGlobal(QPointF(0, self.height()))
        x = int(pos.x())
        y = int(pos.y()) + 2
        screen = self.screen()
        if screen:
            available = screen.availableGeometry()
            x = min(x, available.right() - popup_width + 1)
            x = max(x, available.left())
        popup.move(x, y)

    def _on_index_changed(self, index: int) -> None:
        """Emit only real timeline IDs; placeholders never switch state."""
        timeline_id = self.itemData(index)
        if timeline_id:
            self.timeline_switch_requested.emit(str(timeline_id))
