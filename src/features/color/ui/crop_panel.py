"""
Crop Panel - Professional cropping tool with rotation support.

Features:
- Aspect ratio presets with constraint enforcement
- Free rotation with snap-to-zero
- Live preview with parameter change signals
- Keyboard shortcuts (C: toggle, R: reset rotation, Esc: cancel)
"""

from PyQt6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QPushButton,
                             QLabel, QApplication,
                             QCheckBox, QGroupBox, QSpinBox,
                             QComboBox, QLineEdit, QTextEdit, QPlainTextEdit,
                             QAbstractSpinBox)
from src.shared.i18n.i18n import I18N
from src.shared.ui.shortcut_scope import shortcut_owner_window_active
from src.shared.ui.widgets.chevron_combo import ChevronComboBox
from PyQt6.QtCore import pyqtSignal, Qt, QTimer, QEvent
from PyQt6.QtGui import QKeySequence, QShortcut
from src.shared.ui.widgets.param_slider_row import ParamSliderRow
from src.shared.assets.icon_manager import icon_reset


# =============================================================================
# Local Style Constants (简化版，仅保留必要的本地样式)
# =============================================================================

# 按钮样式常量
HEADER_BUTTON_STYLE = """
    QPushButton {
        background-color: #27272b;
        border: 1px solid #3a3a3a;
        border-radius: 4px;
        color: #888888;
        padding: 0 12px;
        font-weight: bold;
        font-size: 11px;
    }
    QPushButton:hover {
        background-color: #2e2e33;
        border: 1px solid #ff8800;
        color: #eeeeee;
    }
    QPushButton:checked {
        background-color: #ff8800;
        border: 1px solid #ff8800;
        color: white;
    }
"""

TOOL_BUTTON_STYLE = """
    QPushButton {
        background-color: #27272b;
        color: #888888;
        border: 1px solid #3a3a3a;
        border-radius: 4px;
        padding: 0 12px;
        font-weight: bold;
        font-size: 10px;
    }
    QPushButton:hover {
        background-color: #2e2e33;
        border: 1px solid #ff8800;
        color: #eeeeee;
    }
    QPushButton:checked {
        background-color: #ff8800;
        color: white;
        border: 1px solid #ff8800;
    }
"""

VALUE_LABEL_STYLE = """
    QLabel {
        background-color: #27272b;
        border: 1px solid #3a3a3a;
        border-radius: 4px;
        padding: 2px 6px;
        color: #eeeeee;
        font-size: 10px;
        font-weight: bold;
    }
"""


# =============================================================================
# Crop Panel Widget
# =============================================================================

class CropPanel(QWidget):
    """
    Professional crop tool panel with rotation controls.

    Signals:
        crop_requested: Emitted when crop is applied
        reset_requested: Emitted when reset is requested
        params_changed: Emitted when parameters change (for live preview)
        crop_toggled: Emitted when crop tool is activated/deactivated
        constrain_changed: Emitted when constrain to image checkbox changes
    """
    
    crop_requested = pyqtSignal(dict)
    reset_requested = pyqtSignal()
    params_changed = pyqtSignal(dict)
    crop_toggled = pyqtSignal(bool)
    constrain_changed = pyqtSignal(bool)

    # Aspect ratio presets
    ASPECT_PRESETS = [
        ("Free", None),
        ("Original", "original"),
        ("1:1", 1.0),
        ("4:3", 4.0/3.0),
        ("16:9", 16.0/9.0),
        ("16:10", 16.0/10.0),
        ("2.35:1", 2.35),
        ("3:4", 3.0/4.0),
        ("9:16", 9.0/16.0),
        ("10:16", 10.0/16.0),
        ("Custom", "custom"),
    ]

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.crop_active = False

        # Rotation state with high precision (for throttled updates)
        self._rotation_angle = 0.0  # Current rotation in degrees (-45.0 to 45.0)
        self._slider_updating = False  # Anti-recursion lock for slider/spinbox sync

        # Throttle timer for rotation changes (16ms ≈ 60 FPS)
        self._rotation_timer = QTimer(self)
        self._rotation_timer.setSingleShot(True)
        self._rotation_timer.timeout.connect(self._emit_rotation_change)

        self._setup_shortcuts()
        self._setup_ui()
        # The crop workflow keeps focus on the viewer, not in this panel, so
        # Enter/Esc must work from anywhere in the project window — but they
        # must NOT be QShortcuts: a QShortcut consumes the key before any
        # event filter runs, and on macOS a main-window shortcut still fires
        # while a parented Tool window (Motion Graphic editor) is active,
        # which used to kill the editor's own Esc handling.  Matching in an
        # application filter with an owner-window guard shares the key
        # instead of stealing it (see src/shared/ui/shortcut_scope.py).
        QApplication.instance().installEventFilter(self)

    def _setup_shortcuts(self) -> None:
        """Setup keyboard shortcuts for common actions."""
        # ⚠ Scope rules: these window-wide QShortcuts still fire on macOS
        # while the Motion Graphic editor (a parented Qt.Tool window) is
        # active, so every handler re-checks shortcut_owner_window_active()
        # before touching state.  Never widen these back to
        # ApplicationShortcut — see src/shared/ui/shortcut_scope.py.
        self.toggle_shortcut = QShortcut(QKeySequence("C"), self)
        self.toggle_shortcut.activated.connect(self._toggle_crop)
        self.toggle_shortcut.setContext(Qt.ShortcutContext.WindowShortcut)

        self.reset_rot_shortcut = QShortcut(QKeySequence("R"), self)
        self.reset_rot_shortcut.activated.connect(self._on_reset_rotation)
        self.reset_rot_shortcut.setContext(Qt.ShortcutContext.WindowShortcut)

        self.reset_all_shortcut = QShortcut(QKeySequence("Shift+R"), self)
        self.reset_all_shortcut.activated.connect(self.on_reset_all_clicked)
        self.reset_all_shortcut.setContext(Qt.ShortcutContext.WindowShortcut)

    def _setup_ui(self) -> None:
        """Initialize the UI components."""
        # 使用全局 QSS，无需本地样式表

        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(4)

        # Header with enable checkbox
        self._setup_header(layout)

        # Main content - Using QGroupBox like raw_panel.py
        self.content_widget = QWidget()
        content_layout = QVBoxLayout(self.content_widget)
        content_layout.setContentsMargins(4, 10, 4, 4)
        content_layout.setSpacing(2)

        # GroupBox for Aspect Ratio
        self._setup_aspect_ratio_section(content_layout)

        # GroupBox for Rotation
        self._setup_rotation_section(content_layout)

        content_layout.addStretch()

        layout.addWidget(self.content_widget)
        self.content_widget.setEnabled(False)

    def _setup_header(self, parent_layout) -> None:
        """Setup header with activate and reset buttons."""
        header_layout = QHBoxLayout()
        header_layout.setContentsMargins(0, 0, 0, 0)
        header_layout.setSpacing(8)
        
        # Activate button
        self.activate_btn = QPushButton(I18N.tr("crop.activate", "✂ 裁切"))
        self.activate_btn.setCheckable(True)
        self.activate_btn.setChecked(False)
        self.activate_btn.setFixedHeight(26)
        self.activate_btn.setToolTip(I18N.tr("crop.activate_tooltip", "激活/关闭裁切工具 (快捷键：C)"))
        self.activate_btn.clicked.connect(self._on_activate_clicked)
        self.activate_btn.setStyleSheet(HEADER_BUTTON_STYLE)
        header_layout.addWidget(self.activate_btn)
        
        header_layout.addStretch()
        
        # Reset all button (使用 icon_manager)
        self.reset_all_btn = QPushButton()
        self.reset_all_btn.setIcon(icon_reset())
        self.reset_all_btn.setFixedSize(22, 22)
        self.reset_all_btn.setToolTip(I18N.tr("crop.reset_tooltip", "重置所有裁切参数（裁切框、旋转）(快捷键：Shift+R)"))
        self.reset_all_btn.clicked.connect(self.on_reset_all_clicked)
        header_layout.addWidget(self.reset_all_btn)
        
        parent_layout.addLayout(header_layout)

    def _setup_aspect_ratio_section(self, layout) -> None:
        """Setup aspect ratio selection section using QGroupBox."""
        group = QGroupBox(I18N.tr("crop.aspect_ratio", "宽高比"))
        group_layout = QVBoxLayout()
        group_layout.setContentsMargins(4, 10, 4, 4)
        group_layout.setSpacing(2)

        # ComboBox
        self.aspect_combo = ChevronComboBox()
        self.aspect_combo.setMinimumHeight(18)

        for name, _ in self.ASPECT_PRESETS:
            self.aspect_combo.addItem(name)

        self.aspect_combo.currentIndexChanged.connect(self._on_aspect_combo_changed)
        group_layout.addWidget(self.aspect_combo)

        # Custom width/height inputs (shown when "Custom" is selected)
        custom_layout = QHBoxLayout()
        custom_layout.setSpacing(4)

        # Width input
        width_label = QLabel(I18N.tr("crop.width", "宽:"))
        width_label.setStyleSheet("color: #888; font-size: 10px;")
        self.width_spin = QSpinBox()
        self.width_spin.setRange(1, 9999)
        self.width_spin.setValue(1920)
        self.width_spin.setMinimumHeight(18)
        # 使用全局 QSS 样式
        self.width_spin.valueChanged.connect(self._on_custom_size_changed)

        # Height input
        height_label = QLabel(I18N.tr("crop.height", "高:"))
        height_label.setStyleSheet("color: #888; font-size: 10px;")
        self.height_spin = QSpinBox()
        self.height_spin.setRange(1, 9999)
        self.height_spin.setValue(1080)
        self.height_spin.setMinimumHeight(18)
        # 使用全局 QSS 样式
        self.height_spin.valueChanged.connect(self._on_custom_size_changed)
        
        custom_layout.addWidget(width_label)
        custom_layout.addWidget(self.width_spin)
        custom_layout.addWidget(height_label)
        custom_layout.addWidget(self.height_spin)
        custom_layout.addStretch()
        
        # Initially hide custom inputs
        self.custom_widget = QWidget()
        self.custom_widget.setLayout(custom_layout)
        self.custom_widget.setVisible(False)
        group_layout.addWidget(self.custom_widget)
        
        # Constrain to Image checkbox (画布扩展功能)
        self.constrain_check = QCheckBox(I18N.tr("tooltip.constrain_to_image", "限定在画内"))
        self.constrain_check.setChecked(True)  # 默认开启
        self.constrain_check.setToolTip(I18N.tr("crop.constrain_tooltip", "开启时裁切框限定在图像范围内；关闭时允许自由拖拽超出画布，实现画布扩展效果"))
        self.constrain_check.toggled.connect(self.constrain_changed.emit)
        group_layout.addWidget(self.constrain_check)
        
        group.setLayout(group_layout)
        layout.addWidget(group)

    def _setup_rotation_section(self, layout) -> None:
        """Setup rotation control with ParamSliderRow.
        
        Features:
        - ParamSliderRow with DragValue for sub-pixel precision (-45.0 to 45.0, step 0.1)
        - Double-click to reset to 0.0 (via DragValue)
        - Middle-click to reset to 0.0 (via DragValue)
        """
        group = QGroupBox(I18N.tr("crop.rotation", "ROTATION"))
        group_layout = QVBoxLayout()
        group_layout.setContentsMargins(4, 10, 4, 4)
        group_layout.setSpacing(2)
        
        # Rotation row: ParamSliderRow + Reset button
        rot_row_layout = QHBoxLayout()
        rot_row_layout.setSpacing(4)
        
        self.rot_row = ParamSliderRow(
            "", -45.0, 45.0, 0.0,
            step=0.1, decimals=1, suffix="°",
            label_width=0, slider_auto_hide=False)
        self.rot_row.valueChanged.connect(self._on_rot_changed)
        
        # Reset rotation button (使用 icon_manager)
        self.reset_rot_btn = QPushButton()
        self.reset_rot_btn.setIcon(icon_reset())
        self.reset_rot_btn.setFixedSize(22, 22)
        self.reset_rot_btn.setToolTip(I18N.tr(
            "crop.reset_rotation_tooltip", "重置旋转角度为 0 (快捷键：R)"))
        self.reset_rot_btn.clicked.connect(self._on_reset_rotation)
        
        rot_row_layout.addWidget(self.rot_row, 1)
        rot_row_layout.addWidget(self.reset_rot_btn)
        
        group_layout.addLayout(rot_row_layout)
        
        group.setLayout(group_layout)
        layout.addWidget(group)
    
    def _on_rot_changed(self, value: float) -> None:
        """Handle rotation change from ParamSliderRow."""
        self._rotation_angle = value
        # Schedule throttled emit (16ms delay)
        self._rotation_timer.start(16)
    
    def _emit_rotation_change(self) -> None:
        """Emit rotation change after throttle timer expires."""
        self.params_changed.emit(self.get_params())


    # ==========================================================================
    # Event Handlers - Internal
    # ==========================================================================

    def _on_activate_clicked(self, checked) -> None:
        """Handle activate button click."""
        self._set_crop_active_internal(checked)

    def _on_aspect_combo_changed(self, index) -> None:
        """Handle aspect ratio combo box change."""
        # Show/hide custom width/height inputs
        current_text = self.aspect_combo.currentText()
        if hasattr(self, 'custom_widget'):
            self.custom_widget.setVisible(current_text == "Custom")
        self._on_params_changed()

    def _on_custom_size_changed(self) -> None:
        """Handle custom width/height spinbox change."""
        self._on_params_changed()

    def _on_reset_rotation(self) -> None:
        """Reset rotation to zero."""
        if not shortcut_owner_window_active(self):
            return
        self.rot_row.blockSignals(True)
        self.rot_row.setValue(0.0)
        self.rot_row.blockSignals(False)
        self._rotation_angle = 0.0
        
        # Stop any pending timer and emit immediately
        self._rotation_timer.stop()
        self.params_changed.emit(self.get_params())

    def _on_params_changed(self) -> None:
        """Emit parameters change signal for live preview."""
        self.params_changed.emit(self.get_params())

    def _on_apply_clicked(self) -> None:
        """Handle apply crop button click (Enter key)."""
        if not self.crop_active:
            return
        # Apply crop and deactivate crop tool (same as clicking the crop button)
        self._set_crop_active_internal(False)

    def _on_cancel_clicked(self) -> None:
        """Handle cancel (Esc key) — 放弃本次裁切会话，不提交。

        AUD-25311：取消不能复用提交路径（此前 Esc 与 Enter 行为完全
        相同，都会 emit crop_requested 把裁切应用到节点状态）。取消
        应恢复进入裁切前的参数快照并静默关闭工具。
        """
        if not self.crop_active:
            return
        self._deactivate_without_commit()

    def eventFilter(self, watched, event) -> bool:  # noqa: N802 — Qt API spelling
        """Apply/cancel crop from Enter/Esc without stealing the keys.

        These keys flow through the application filter chain (they used to be
        ApplicationShortcut QShortcuts, which consume the key press before
        ANY event filter — including the Motion Graphic editor's — can see
        it; see src/shared/ui/shortcut_scope.py).  Owned only while this
        panel's window is active AND a crop session is in progress.
        """
        if (
            event.type() == QEvent.Type.KeyPress
            and not event.isAutoRepeat()
            and self.crop_active
            and shortcut_owner_window_active(self)
            and not isinstance(
                QApplication.focusWidget(),
                (QComboBox, QLineEdit, QTextEdit, QPlainTextEdit, QAbstractSpinBox),
            )
        ):
            if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                self._on_apply_clicked()
                event.accept()
                return True
            if event.key() == Qt.Key.Key_Escape:
                self._on_cancel_clicked()
                event.accept()
                return True
        return super().eventFilter(watched, event)

    def _toggle_crop(self) -> None:
        """Toggle crop tool on/off."""
        if not shortcut_owner_window_active(self):
            return
        self._set_crop_active_internal(not self.crop_active)

    def _set_crop_active_internal(self, checked) -> None:
        """Internal method to set crop active state.

        When deactivating (checked=False), automatically apply crop effect.
        """
        if self.crop_active == checked:
            return

        if checked:
            # AUD-25311：进入裁切会话时记录参数快照，供 Esc 取消恢复
            self._pre_crop_params = self.get_params()
        elif self.crop_active:
            # If deactivating, apply crop before closing (commit path)
            # Emit crop request to apply the effect
            self.crop_requested.emit(self.get_params())

        self._set_crop_active_state(checked)

    def _set_crop_active_state(self, checked: bool) -> None:
        """翻转激活状态并同步 UI（不提交、不恢复参数）。"""
        self.crop_active = checked
        self.activate_btn.setChecked(checked)
        self.content_widget.setEnabled(checked)
        self.crop_toggled.emit(checked)

        # Update button text
        self.activate_btn.setText(I18N.tr("crop.activate_active", "✂ 裁切中") if checked else I18N.tr("crop.activate", "✂ 裁切"))

    def _deactivate_without_commit(self) -> None:
        """AUD-25311：取消路径 — 恢复进入裁切前的参数快照并关闭工具。

        与提交路径（_set_crop_active_internal(False)）的差异：
        - 不 emit crop_requested（裁切效果不落到节点状态）；
        - 控件恢复快照值并广播 params_changed，使实时预览回退。
        """
        snapshot = getattr(self, '_pre_crop_params', None)
        if snapshot is not None:
            self._restore_params_from(snapshot)
            # 让实时预览回退到进入裁切前的参数
            self.params_changed.emit(dict(snapshot))
        self._rotation_timer.stop()  # 丢弃尚未发射的节流旋转更新
        self._set_crop_active_state(False)

    def _restore_params_from(self, params: dict) -> None:
        """按快照恢复裁切控件显示（blockSignals 状态锁防递归）。"""
        self.aspect_combo.blockSignals(True)
        self.rot_row.blockSignals(True)
        try:
            aspect = params.get("aspect")
            if aspect is not None:
                index = self.aspect_combo.findText(str(aspect))
                if index >= 0:
                    self.aspect_combo.setCurrentIndex(index)
            rotation = max(-45.0, min(45.0, float(params.get("rotation", 0.0))))
            self.rot_row.setValue(rotation)
            self._rotation_angle = rotation
            if aspect == "Custom":
                if "custom_width" in params:
                    self.width_spin.blockSignals(True)
                    self.width_spin.setValue(int(params["custom_width"]))
                    self.width_spin.blockSignals(False)
                if "custom_height" in params:
                    self.height_spin.blockSignals(True)
                    self.height_spin.setValue(int(params["custom_height"]))
                    self.height_spin.blockSignals(False)
        finally:
            self.aspect_combo.blockSignals(False)
            self.rot_row.blockSignals(False)
        # custom 输入区可见性跟随恢复后的 aspect 选择
        if hasattr(self, 'custom_widget'):
            self.custom_widget.setVisible(self.aspect_combo.currentText() == "Custom")

    # ==========================================================================
    # Public API
    # ==========================================================================

    def set_crop_active(self, active) -> None:
        """Set crop active state programmatically."""
        self._set_crop_active_internal(active)

    def get_params(self) -> dict:
        """
        Get current crop parameters.

        Returns:
            dict with keys: aspect, rotation, custom_width, custom_height
        """
        params = {
            "aspect": self.aspect_combo.currentText(),
            "rotation": self._rotation_angle,  # Use high-precision float value
        }

        # Add custom width/height if Custom is selected
        if self.aspect_combo.currentText() == "Custom":
            params["custom_width"] = self.width_spin.value()
            params["custom_height"] = self.height_spin.value()

        return params

    def set_params(self, rotation: float) -> None:
        """
        Set crop parameters programmatically.

        Args:
            rotation: Rotation angle in degrees (-45.0 to 45.0)
        """
        # Clamp rotation to valid range
        rotation = max(-45.0, min(45.0, rotation))

        self.rot_row.blockSignals(True)
        self.rot_row.setValue(rotation)
        self.rot_row.blockSignals(False)
        self._rotation_angle = rotation

    def set_aspect_ratio(self, aspect_name: str) -> None:
        """
        Set aspect ratio by name.
        
        Args:
            aspect_name: Name of aspect ratio preset (e.g., "16:9", "Free")
        """
        index = self.aspect_combo.findText(aspect_name)
        if index >= 0:
            self.aspect_combo.blockSignals(True)
            self.aspect_combo.setCurrentIndex(index)
            self.aspect_combo.blockSignals(False)

    def get_aspect_ratio_value(self) -> float | None:
        """
        Get the numeric aspect ratio value for current selection.
        
        Returns:
            float aspect ratio or None for Free, "original" for Original
        """
        index = self.aspect_combo.currentIndex()
        if 0 <= index < len(self.ASPECT_PRESETS):
            return self.ASPECT_PRESETS[index][1]
        return None

    # ==========================================================================
    # Reset Handlers
    # ==========================================================================

    def on_reset_all_clicked(self) -> None:
        """Reset all crop parameters including crop rect and rotation."""
        if not shortcut_owner_window_active(self):
            return
        # Block signals during reset to avoid multiple emissions
        self.aspect_combo.blockSignals(True)
        self.rot_row.blockSignals(True)
        self.constrain_check.blockSignals(True)

        try:
            self.aspect_combo.setCurrentIndex(0)  # Free
            self.rot_row.setValue(0.0)
            self._rotation_angle = 0.0
            self.constrain_check.setChecked(True)  # 重置为默认开启
        finally:
            self.aspect_combo.blockSignals(False)
            self.rot_row.blockSignals(False)
            self.constrain_check.blockSignals(False)

        # 发送 constrain_changed 信号通知 canvas 更新状态
        self.constrain_changed.emit(True)
        # 重置回 Free 后隐藏 Custom 宽高输入区（可见性只在
        # _on_aspect_combo_changed 里维护，这里漏同步会残留显示）
        if self.custom_widget.isVisible():
            self.custom_widget.setVisible(False)
        self.reset_requested.emit()

    def on_reset_crop_rect_clicked(self) -> None:
        """Reset only the crop rect to full image."""
        self.reset_requested.emit()

    def is_crop_active(self) -> bool:
        """Check if crop tool is currently active."""
        return self.crop_active

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        ParamSliderRow.apply_visibility_to_all(
            ParamSliderRow.collect_rows(self), self.width())