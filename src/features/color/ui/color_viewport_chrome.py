"""Presentation adapter for Color's viewer, without owning media playback.

The toolbar, transport, slider and timecode objects remain the originals.  Only
their layout changes; Composite's shared transport can restore its own row.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Mapping, Optional

from PyQt6.QtCore import QEvent, QObject, QPointF, QRect, QRectF, QSize, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QIcon, QIconEngine, QPaintEvent, QPainter, QPen, QPixmap
from PyQt6.QtWidgets import QHBoxLayout, QLabel, QMenu, QSlider, QToolButton, QWidget

from src.shared.i18n.i18n import I18N
from src.shared.ui.viewport_icons import viewer_icon

if TYPE_CHECKING:
    from PyQt6.QtGui import QAction

    from src.features.color.core.color_viewer_capabilities import ColorViewerCapability
    from src.features.color.ui.canvas import Canvas, VideoControlBar, ViewportMenuBar


_BUTTON_STYLE = """
QToolButton { background: transparent; border: none; border-radius: 2px;
    padding: 0; margin: 0; color: #969699; font-size: 11px; }
QToolButton:hover { background: #303035; color: #d2d2d6; }
QToolButton:checked { background: #343439; }
QToolButton:disabled { color: #555559; }
QToolButton::menu-indicator { image: none; }
"""


def _button(parent: QWidget, icon: str, label: str, enabled: bool = True) -> QToolButton:
    button = QToolButton(parent)
    button.setFixedSize(32, 28)
    button.setIconSize(QSize(20, 20))
    button.setIcon(viewer_icon(icon))
    button.setStyleSheet(_BUTTON_STYLE)
    button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
    button.setAccessibleName(label)
    button.setToolTip(label if enabled else label + " — " + I18N.tr("viewport.unavailable", "此版本暂不支持"))
    button.setEnabled(enabled)
    return button


class _ColorZoomButton(QToolButton):
    """Compact text button with the reference chevron on its right edge."""

    def paintEvent(self, event: QPaintEvent) -> None:
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(QColor("#969699" if self.isEnabled() else "#555559"), 1.1))
        x, y = self.width() - 10.0, self.height() / 2.0
        painter.drawLine(QPointF(x - 3.0, y - 1.5), QPointF(x, y + 1.5))
        painter.drawLine(QPointF(x, y + 1.5), QPointF(x + 3.0, y - 1.5))
        painter.end()


class _MenuGlyphEngine(QIconEngine):
    """Keep menu chevrons vector-sharp beside the original viewer glyphs."""

    def __init__(self, name: str) -> None:
        super().__init__()
        self.name = name

    def clone(self) -> QIconEngine:
        return _MenuGlyphEngine(self.name)

    def paint(self, painter: QPainter, rect: QRect, mode: QIcon.Mode, state: QIcon.State) -> None:
        painter.save()
        painter.translate(rect.left(), rect.top())
        painter.scale(rect.width() / 28.0, rect.height() / 20.0)
        viewer_icon(self.name).paint(painter, QRect(0, 0, 20, 20), mode=mode, state=state)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(QColor("#555559" if mode == QIcon.Mode.Disabled else "#969699"), 0.9))
        painter.drawLine(QPointF(22.0, 8.5), QPointF(24.5, 11.0))
        painter.drawLine(QPointF(24.5, 11.0), QPointF(27.0, 8.5))
        painter.restore()

    def pixmap(self, size: QSize, mode: QIcon.Mode, state: QIcon.State) -> QPixmap:
        pixmap = QPixmap(size)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        self.paint(painter, QRect(0, 0, size.width(), size.height()), mode, state)
        painter.end()
        return pixmap


def _menu_glyph(button: QToolButton, name: str) -> None:
    button.setIcon(QIcon(_MenuGlyphEngine(name)))
    button.setIconSize(QSize(28, 20))


def paint_color_scrubber(slider: QSlider) -> None:
    """Color's quiet grey ruler and white playhead, using logical pixels."""
    painter = QPainter(slider)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.fillRect(slider.rect(), QColor("#202024"))
    left, right = 15.0, max(15.0, slider.width() - 15.0)
    top = slider.height() / 2.0
    painter.setPen(QPen(QColor("#101013"), 1.0))
    painter.setBrush(QColor("#49494d"))
    painter.drawRoundedRect(QRectF(left, top - 1.5, right - left, 3.0), 1.0, 1.0)
    span = max(1, slider.maximum() - slider.minimum())

    def x_for(value: int) -> float:
        fraction = max(0.0, min(1.0, (value - slider.minimum()) / span))
        if slider.invertedAppearance():
            fraction = 1.0 - fraction
        return left + fraction * (right - left)

    in_point = getattr(slider, "_in_point", None)
    out_point = getattr(slider, "_out_point", None)
    if isinstance(in_point, int) and isinstance(out_point, int) and in_point <= out_point:
        start, end = x_for(in_point), x_for(out_point)
        painter.fillRect(QRectF(min(start, end), top - 1.0, max(1.0, abs(end - start)), 2.0), QColor("#85858a"))
    painter.setPen(QPen(QColor("#d6d6d9" if slider.isEnabled() else "#707074"), 1.0))
    x = x_for(slider.value())
    painter.drawLine(QPointF(x, top - 5.0), QPointF(x, top + 5.0))
    painter.end()


class ColorViewportChrome(QObject):
    """Install Color-specific chrome on existing presentation widgets."""

    # CV-B01：chrome 请求宿主重新评估能力（菜单打开／上下文变化时）。
    capability_refresh_requested = pyqtSignal()
    # CV-B06：监看静音请求（宿主路由到 EditPage Master 总线服务）。
    monitor_mute_toggled = pyqtSignal(bool)
    gallery_compare_requested = pyqtSignal(str)

    def __init__(self, canvas: Canvas) -> None:
        super().__init__(canvas)
        self.canvas = canvas
        self.toolbar = canvas.viewport_menu
        self.transport = canvas.video_control_bar
        self._color_transport_active = False
        self._original_row: list[tuple[QWidget, int]] = []
        self._options_stub_actions: dict[str, QAction] = {}
        self._install_header()
        self._prepare_transport()
        self.transport._color_chrome = self
        self.activate_transport()
        self.toolbar._color_chrome = self
        self.toolbar.installEventFilter(self)
        canvas.installEventFilter(self)
        self._sync_focus_capability()
        status = getattr(canvas, "color_status_bar", None)
        if status is not None:
            status.installEventFilter(self)
        self.position_title()

    def _install_header(self) -> None:
        toolbar = self.toolbar
        layout = toolbar._layout
        while layout.count():
            item = layout.takeAt(0)
            if item.widget() is not None:
                item.widget().hide()
        toolbar.setFixedHeight(30)
        toolbar.setMaximumHeight(30)
        toolbar._content.setStyleSheet("background: #202024;")
        toolbar.setStyleSheet("QFrame#viewportMenuBar { background: #202024; border-bottom: 1px solid #101013; }")
        layout.setContentsMargins(4, 0, 4, 0)
        layout.setSpacing(0)
        self.header_left = QWidget(toolbar._content)
        left = QHBoxLayout(self.header_left)
        left.setContentsMargins(0, 0, 0, 0)
        left.setSpacing(1)
        self.zoom_button = _ColorZoomButton(self.header_left)
        self.zoom_button.setText(I18N.tr("viewport.fit_short", "Fit"))
        self.zoom_button.setFixedSize(62, 28)
        self.zoom_button.setStyleSheet(_BUTTON_STYLE + "QToolButton { padding-right: 16px; padding-left: 4px; }")
        self.zoom_button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.zoom_button.setAccessibleName(I18N.tr("viewport.zoom", "Viewer Zoom"))
        self.zoom_button.setToolTip(self.zoom_button.accessibleName())
        self.zoom_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        zoom_menu = QMenu(self.zoom_button)
        for action in (toolbar.zoom_in_act, toolbar.zoom_out_act, toolbar.fit_act, toolbar.actual_size_act):
            zoom_menu.addAction(action)
        zoom_menu.addSeparator()
        for percent in (25, 50, 100, 200):
            zoom_menu.addAction(f"{percent}%", lambda checked=False, value=percent: self.set_zoom(value))
        self.zoom_button.setMenu(zoom_menu)
        left.addWidget(self.zoom_button)
        toolbar.split_btn.setIcon(viewer_icon("image_wipe"))
        toolbar.split_btn.toggled.connect(lambda checked: toolbar.split_btn.setIcon(viewer_icon("image_wipe")))
        toolbar.split_btn.setToolTip(I18N.tr("viewport.image_wipe", "Image Wipe"))
        self._adopt_header(left, toolbar.split_btn)
        self.split_screen_button = _button(self.header_left, "split_screen", I18N.tr("viewport.split_screen", "Split Screen"))
        self.split_screen_button.setToolTip(I18N.tr("gallery.split_setup", "Set Up Gallery Split Screen…"))
        self.split_screen_button.clicked.connect(
            lambda: self.gallery_compare_requested.emit("split"))
        left.addWidget(self.split_screen_button)
        toolbar.mask_btn.setIcon(viewer_icon("highlight"))
        toolbar.mask_btn.toggled.connect(lambda checked: toolbar.mask_btn.setIcon(viewer_icon("highlight")))
        toolbar.mask_btn.setToolTip(I18N.tr("viewport.highlight", "Highlight (Shift+H)"))
        self._adopt_header(left, toolbar.mask_btn)
        layout.addWidget(self.header_left)
        layout.addStretch(1)

        self.header_right = QWidget(toolbar._content)
        right = QHBoxLayout(self.header_right)
        right.setContentsMargins(0, 0, 0, 0)
        right.setSpacing(1)
        self.header_timecode = QLabel(self.transport.timecode_label.text(), self.header_right)
        self.header_timecode.setFixedWidth(84)
        self.header_timecode.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.header_timecode.setStyleSheet("color: #d6d6d9; font-size: 11px; font-weight: 600; background: transparent;")
        self.header_timecode.setAccessibleName(I18N.tr("viewport.timecode", "Current Timecode"))
        self.transport.timecode_changed.connect(self.header_timecode.setText)
        right.addWidget(self.header_timecode)
        toolbar.guides_menu_btn.setText("")
        toolbar.guides_menu_btn.setIcon(viewer_icon("guides"))
        toolbar.guides_menu_btn.setToolTip(I18N.tr("viewport.guides", "Viewer Guides"))
        self._adopt_header(right, toolbar.guides_menu_btn)
        _menu_glyph(toolbar.guides_menu_btn, "guides")
        self.proxy_button = _button(self.header_right, "proxy", I18N.tr("viewport.proxy_handling", "Proxy Handling"), False)
        self.resolution_button = _button(self.header_right, "resolution", I18N.tr("viewport.timeline_resolution", "Timeline Resolution"), False)
        _menu_glyph(self.proxy_button, "proxy")
        _menu_glyph(self.resolution_button, "resolution")
        self.bypass_button = _button(self.header_right, "bypass", I18N.tr("viewport.bypass_grades", "Bypass Color Grades and Fusion Effects"), False)
        self.enhanced_button = _button(self.header_right, "enhanced_viewer", I18N.tr("viewport.enhanced_viewer", "Enhanced Viewer"))
        self.enhanced_button.clicked.connect(toolbar.focus_act.trigger)
        self.options_button = _button(self.header_right, "more", I18N.tr("viewport.options", "Viewer Options"))
        options = QMenu(self.options_button)
        options.aboutToShow.connect(self._sync_focus_capability)
        options.aboutToShow.connect(self.capability_refresh_requested.emit)
        options.addMenu(toolbar.view_menu).setText(I18N.tr("viewport.view", "View"))
        options.addAction(I18N.tr("gallery.choose_wipe", "Choose Gallery Wipe Reference…"),
                          lambda: self.gallery_compare_requested.emit("wipe"))
        timeline_menu = options.addMenu(I18N.tr("viewport.timeline", "Timeline"))
        timeline_menu.aboutToShow.connect(lambda: self._populate_timeline_menu(timeline_menu))
        options.addSeparator()
        options.setToolTipsVisible(True)
        for button, key in (
            (self.split_screen_button, "compare.split_screen"),
            (self.proxy_button, "monitor.proxy"),
            (self.resolution_button, "monitor.resolution"),
            (self.bypass_button, "monitor.bypass"),
        ):
            action = options.addAction(button.accessibleName())
            action.setEnabled(False)
            action.setToolTip(button.toolTip())
            self._options_stub_actions[key] = action
        self.options_button.setMenu(options)
        self.options_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        for button in (self.proxy_button, self.resolution_button, self.bypass_button, self.enhanced_button, self.options_button):
            right.addWidget(button)
        layout.addWidget(self.header_right)

    @staticmethod
    def _adopt_header(layout: QHBoxLayout, button: QToolButton) -> None:
        button.setFixedSize(32, 28)
        button.setIconSize(QSize(20, 20))
        button.setStyleSheet(_BUTTON_STYLE)
        button.setAccessibleName(button.toolTip())
        layout.addWidget(button)
        button.show()

    def set_zoom(self, percent: int) -> None:
        """Use the Canvas's existing transform API, without requesting media."""
        self.canvas.resetTransform()
        self.canvas.scale(percent / 100.0, percent / 100.0)
        self.canvas._update_lod()
        self.canvas.viewport().update()
        self.zoom_button.setText(f"{percent}%")

    def update_zoom(self, factor: float) -> None:
        self.zoom_button.setText(f"{round(factor * 100.0)}%")

    def _populate_timeline_menu(self, menu: QMenu) -> None:
        menu.clear()
        selector = self.toolbar._timeline_selector
        for index in range(selector.count()):
            action = menu.addAction(selector.itemText(index))
            action.setCheckable(True)
            action.setChecked(index == selector.currentIndex())
            action.triggered.connect(lambda checked=False, value=index: selector.setCurrentIndex(value))

    def _sync_focus_capability(self) -> None:
        supported = self.toolbar._focus_mode_host() is not None
        self.enhanced_button.setEnabled(supported)
        self.toolbar.focus_act.setEnabled(supported)
        self.enhanced_button.setToolTip(
            self.enhanced_button.accessibleName() if supported else
            self.enhanced_button.accessibleName() + " — " + I18N.tr("viewport.unavailable", "此版本暂不支持"))
        self.toolbar.focus_act.setToolTip(self.enhanced_button.toolTip())

    # ── CV-B05：Power Window overlay 开关（真实后端：set_overlay_visible）──

    def _toggle_power_window_overlays(self, checked: bool) -> None:
        """显示/隐藏 Power Window 线框；只改显示，不改窗口业务状态。"""
        setter = getattr(self.canvas, "set_overlay_visible", None)
        if not callable(setter):
            return
        try:
            setter(bool(checked))
        except RuntimeError:
            return  # canvas 已销毁（浮动/关闭竞态）
        # 诚实回填：请求宿主按真实状态重评（checked 反映引擎侧事实）。
        self.capability_refresh_requested.emit()

    # ── CV-B01：能力注册表回填（enabled/checked/reason 单一来源） ──────

    @staticmethod
    def _cap_tooltip(label: str, cap: Optional[ColorViewerCapability]) -> str:
        if cap is None or cap.supported or not cap.reason:
            return label
        return f"{label} — {cap.reason}"

    def _apply_button_capability(
            self, button: QWidget,
            cap: Optional[ColorViewerCapability]) -> None:
        if cap is None:
            return
        button.setEnabled(bool(cap.supported))
        label = button.accessibleName() or button.toolTip()
        button.setToolTip(self._cap_tooltip(label, cap))
        is_checkable = getattr(button, "isCheckable", None)
        if (callable(is_checkable) and is_checkable()
                and cap.checked is not None):
            # 状态锁：程序化回填 checked 不回发 toggled。
            button.blockSignals(True)
            button.setChecked(bool(cap.checked))
            button.blockSignals(False)

    def _apply_action_capability(
            self, action: QAction,
            cap: Optional[ColorViewerCapability]) -> None:
        if cap is None:
            return
        action.setEnabled(bool(cap.supported))
        label = action.text()
        action.setToolTip(self._cap_tooltip(label, cap))
        if action.isCheckable() and cap.checked is not None:
            action.blockSignals(True)
            action.setChecked(bool(cap.checked))
            action.blockSignals(False)

    def refresh_capabilities(
            self,
            caps_by_action: Mapping[str, ColorViewerCapability]) -> None:
        """Apply ``{action: ColorViewerCapability}`` to registered controls.

        CV-B01 契约：只做回填（enabled/tooltip/checked），不发命令；
        checked 一律经 blockSignals 状态锁，旧回填不触发二次 toggle。
        """
        def cap(key: str) -> Optional[ColorViewerCapability]:
            return caps_by_action.get(key)

        for button, key in (
            (self.proxy_button, "monitor.proxy"),
            (self.resolution_button, "monitor.resolution"),
            (self.bypass_button, "monitor.bypass"),
            (self.unmix_button, "monitor.unmix"),
            (self.mute_button, "monitor.mute"),
            (self.power_window_button, "overlay.power_windows"),
        ):
            self._apply_button_capability(button, cap(key))
            action = self._options_stub_actions.get(key)
            if action is not None:
                self._apply_action_capability(action, cap(key))

        # Setup is a UI action; it does not enable an unavailable grid renderer.
        split_action = self._options_stub_actions.get("compare.split_screen")
        if split_action is not None:
            self._apply_action_capability(split_action, cap("compare.split_screen"))

        toolbar = self.toolbar
        self._apply_checkable_capability(
            cap("view.highlight"), (toolbar.mask_btn, toolbar.mask_act))
        self._apply_checkable_capability(
            cap("view.split_wipe"), (toolbar.split_btn, toolbar.split_act))
        for key, actions in (
            ("view.ab_toggle", (toolbar.ab_toggle_act,)),
            ("view.capture_reference", (toolbar.capture_reference_act,)),
            ("view.clear_reference", (toolbar.clear_reference_act,)),
            ("view.capture_still", (toolbar.capture_still_act,)),
            ("view.auto_color", (toolbar.auto_color_act,)),
            ("view.focus_mode", (toolbar.focus_act,)),
        ):
            for action in actions:
                self._apply_action_capability(action, cap(key))
        self._apply_button_capability(self.enhanced_button, cap("view.focus_mode"))

        selector_cap = cap("timeline.switch")
        if selector_cap is not None:
            toolbar._timeline_selector.setEnabled(bool(selector_cap.supported))

        bar = self.transport
        for button, key in (
            (bar.btn_first, "transport.first"),
            (bar.btn_prev, "transport.prev"),
            (bar.btn_play, "transport.play"),
            (bar.btn_next, "transport.next"),
            (bar.btn_last, "transport.last"),
        ):
            self._apply_button_capability(button, cap(key))
        self._apply_button_capability(bar.btn_loop, cap("transport.loop"))
        self._apply_button_capability(bar.slider, cap("transport.scrub"))

    def _apply_checkable_capability(
            self,
            cap: Optional[ColorViewerCapability],
            widgets) -> None:
        if cap is None:
            return
        for widget in widgets:
            if widget is None:
                continue
            widget.setEnabled(bool(cap.supported))
            if cap.checked is not None:
                widget.blockSignals(True)
                widget.setChecked(bool(cap.checked))
                widget.blockSignals(False)

    def position_title(self) -> None:
        """Keep exact centering, while protecting functional controls first."""
        if not hasattr(self, "header_right"):
            return
        width = self.toolbar.width()
        expanded = width >= 700
        for button in (self.proxy_button, self.resolution_button, self.bypass_button):
            button.setVisible(expanded)
        self.header_timecode.setVisible(width >= 520)
        self.split_screen_button.setVisible(width >= 520)
        self.toolbar._layout.activate()
        padding = max(self.header_left.sizeHint().width(), self.header_right.sizeHint().width()) + 10
        available = max(0, width - 2 * padding)
        selector = self.toolbar._timeline_selector
        selector.setStyleSheet("""
            QComboBox { color: #d6d6d9; background: transparent; border: 0;
                padding: 0 18px 0 6px; font-size: 11px; }
            QComboBox::drop-down { border: none; width: 0px; }
            QComboBox::down-arrow { image: none; border: none; }
        """)
        selector_width = min(320, max(80, selector.sizeHint().width()), available)
        selector.setVisible(selector.count() > 0 and selector_width >= 80)
        selector.setGeometry((width - max(0, selector_width)) // 2, 0, max(0, selector_width), 30)
        selector.raise_()

    def _prepare_transport(self) -> None:
        bar = self.transport
        self.row = bar.findChild(QWidget, "videoControlRow")
        row_layout = self.row.layout()
        for index in range(row_layout.count()):
            item = row_layout.itemAt(index)
            if item.widget() is not None:
                self._original_row.append((item.widget(), row_layout.stretch(index)))
        self._original_style = bar.styleSheet()
        self.left_tools = QWidget(self.row)
        self.left_tools.setObjectName("colorTransportTools")
        self.left_tools.setStyleSheet("QWidget#colorTransportTools { background: transparent; border: none; }")
        self.left_tools.setFixedWidth(110)
        tools = QHBoxLayout(self.left_tools)
        tools.setContentsMargins(4, 0, 0, 0)
        tools.setSpacing(1)
        self.power_window_button = _button(self.left_tools, "power_window", I18N.tr("viewport.power_window", "Power Window"))
        # CV-B05：Power Window 图形 overlay 的显示开关（Off/All）。复用
        # Canvas.set_overlay_visible（形状 overlay 与引擎 matte 预览是
        # 独立状态；隐藏线框不改窗口 enabled/mask 结果）。Selected-only
        # 模式未设计，按钮保持两态。
        self.power_window_button.setCheckable(True)
        self.power_window_button.toggled.connect(
            self._toggle_power_window_overlays)
        self.unmix_button = _button(self.left_tools, "unmix", I18N.tr("viewport.unmix", "Unmix"), False)
        self.mute_button = _button(self.left_tools, "mute", I18N.tr("viewport.mute", "Mute"))
        # CV-B06：监看静音（Master 总线）。请求经宿主路由到 EditPage 的
        # set_monitor_master_muted；checked 由能力回填按真实状态写入。
        self.mute_button.setCheckable(True)
        self.mute_button.toggled.connect(self.monitor_mute_toggled.emit)
        for button in (self.power_window_button, self.unmix_button, self.mute_button):
            tools.addWidget(button)
        tools.addStretch(1)
        self.transport_center = QWidget(self.row)
        self.transport_center.setObjectName("colorTransportCenter")
        self.transport_center.setStyleSheet("QWidget#colorTransportCenter { background: transparent; border: none; }")
        center = QHBoxLayout(self.transport_center)
        center.setContentsMargins(0, 0, 0, 0)
        center.setSpacing(3)
        self.right_timecode = QWidget(self.row)
        self.right_timecode.setObjectName("colorTransportTimecodeGroup")
        self.right_timecode.setStyleSheet("QWidget#colorTransportTimecodeGroup { background: transparent; border: none; }")
        self.right_timecode.setFixedWidth(110)
        right = QHBoxLayout(self.right_timecode)
        right.setContentsMargins(0, 0, 4, 0)
        self._center_layout = center
        self._right_layout = right

    def activate_transport(self) -> None:
        """Restore Color's ruler/centered legacy playback controls."""
        if self._color_transport_active:
            return
        bar = self.transport
        self._color_transport_active = True
        layout = self.row.layout()
        while layout.count():
            item = layout.takeAt(0)
            if item.widget() is not None:
                item.widget().hide()
        for button in (bar.btn_first, bar.btn_prev, bar.btn_play, bar.btn_next, bar.btn_last, bar.btn_loop):
            self._center_layout.addWidget(button)
            button.show()
            button.setAccessibleName(button.toolTip())
        self._right_layout.addWidget(bar.timecode_label)
        bar.timecode_label.setStyleSheet("color: #d6d6d9; background: transparent; border: 0; font-size: 11px; font-weight: 600; padding: 0;")
        bar.timecode_label.show()
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self.left_tools)
        layout.addStretch(1)
        layout.addWidget(self.transport_center)
        layout.addStretch(1)
        layout.addWidget(self.right_timecode)
        for group in (self.left_tools, self.transport_center, self.right_timecode):
            group.show()
        bar.layout().insertWidget(1, bar.slider)
        bar.slider.setProperty("colorMonitorScrubber", True)
        bar.slider.setFixedHeight(20)
        bar.slider.show()
        bar.ruler_slider.hide()
        bar.label_frame.hide()
        bar.setFixedHeight(56)
        bar.setMaximumHeight(56)
        bar.setStyleSheet("QFrame#videoControlBar, QWidget#videoControlRow { background: #202024; border: none; } QToolButton { background: transparent; border: 0; padding: 3px; margin: 1px; } QToolButton:hover { background: #303035; } ")
        self.canvas.update_video_control_bar_position()

    def suspend_transport(self) -> None:
        """Return the exact original controls to the shared Fusion row."""
        if not self._color_transport_active:
            return
        self._color_transport_active = False
        bar = self.transport
        bar.layout().removeWidget(bar.slider)
        bar.slider.setProperty("colorMonitorScrubber", False)
        bar.slider.setMinimumHeight(0)
        bar.slider.setMaximumHeight(16777215)
        layout = self.row.layout()
        while layout.count():
            item = layout.takeAt(0)
            if item.widget() is not None:
                item.widget().hide()
        for widget, stretch in self._original_row:
            layout.addWidget(widget, stretch)
            widget.show()
        layout.setContentsMargins(8, 2, 8, 4)
        layout.setSpacing(3)
        bar.setStyleSheet(self._original_style)

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        toolbar = getattr(self, "toolbar", None)
        canvas = getattr(self, "canvas", None)
        if toolbar is None or canvas is None:
            return False
        if watched is toolbar and event.type() in {QEvent.Type.Resize, QEvent.Type.Show}:
            self.position_title()
            self._sync_focus_capability()
        if watched is canvas and event.type() in {QEvent.Type.ParentChange, QEvent.Type.Show}:
            self._sync_focus_capability()
        if watched is getattr(canvas, "color_status_bar", None) and event.type() in {QEvent.Type.Show, QEvent.Type.Resize}:
            self.canvas.update_video_control_bar_position()
        return super().eventFilter(watched, event)
