"""CompClipSettingsDialog - 合成片段设置对话框。

用于在媒体池中新建/编辑空白 Comp Clip 的设置面板。UI 风格与合成页
参数面板（node_properties_panel.py）保持一致：暗色主题、DragValue 数值
输入、ChevronComboBox 下拉、get_color 拾色器、C.* 颜色常量。

支持两种模式：
- 新建模式（edit_mode=False）：默认值，标题 "New Comp Clip"
- 编辑模式（edit_mode=True）：预填现有值，标题 "Comp Clip Settings"
"""

from __future__ import annotations

from typing import Optional

from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout,
    QLabel, QLineEdit, QPushButton, QGroupBox, QWidget,
)

from src.shared.i18n.i18n import I18N
from src.shared.assets.icon_manager import make_link_button
from src.shared.ui.style_sheet import DIALOG_BASE_STYLE
from src.shared.ui.widgets.drag_value import DragValue
from src.shared.ui.widgets.chevron_combo import ChevronComboBox
from src.shared.ui.widgets.color_picker_control import ColorPickerControl
from src.shared.ui.widgets.color_picker_control import (
    qcolor_to_linear_rgba,
)


# =============================================================================
# 预设
# =============================================================================

# 分辨率预设: name -> (width, height)
# 在 new_timeline_dialog.RESOLUTION_PRESETS 基础上补竖屏/方形。
_RESOLUTION_PRESETS: list[tuple[str, int, int]] = [
    ("4K DCI (4096x2160)", 4096, 2160),
    ("4K UHD (3840x2160)", 3840, 2160),
    ("2K DCI (2048x1080)", 2048, 1080),
    ("2K QHD (2560x1440)", 2560, 1440),
    ("1080p (1920x1080)", 1920, 1080),
    ("720p (1280x720)", 1280, 720),
    ("Vertical 1080x1920", 1080, 1920),
    ("Vertical 4K 2160x3840", 2160, 3840),
    ("Square 1080x1080", 1080, 1080),
    ("Square 2160x2160", 2160, 2160),
]

# 帧率预设: (display_label, fps_float)
_FPS_PRESETS: list[tuple[str, float]] = [
    ("23.976", 23.976),
    ("24", 24.0),
    ("25", 25.0),
    ("29.97", 29.97),
    ("30", 30.0),
    ("50", 50.0),
    ("59.94", 59.94),
    ("60", 60.0),
]

_CUSTOM_KEY = "__custom__"

# 默认背景色：不透明白色 (R, G, B, A)
_DEFAULT_BACKGROUND_COLOR = (1.0, 1.0, 1.0, 1.0)


def _qcolor_to_rgba(color: QColor) -> tuple[float, float, float, float]:
    """将拾色器的 sRGB QColor 转为 scene-linear RGBA。"""
    return qcolor_to_linear_rgba(color)


class CompClipSettingsDialog(QDialog):
    """合成片段设置对话框。

    新建/编辑空白 Comp Clip 的名称、分辨率、帧速率、时长、背景色。

    用法（同 NewTimelineDialog 模式）::

        dlg = CompClipSettingsDialog(parent=main_window)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        settings = dlg.get_settings()
    """

    def __init__(
        self,
        parent: Optional[QWidget] = None,
        edit_mode: bool = False,
        initial_settings: Optional[dict] = None,
    ) -> None:
        super().__init__(parent)
        self._edit_mode = edit_mode

        # 初始值（编辑模式预填，新建模式用默认值）
        init = initial_settings or {}
        self._init_name = init.get("name", "Comp Clip")
        self._init_width = int(init.get("width", 1920))
        self._init_height = int(init.get("height", 1080))
        self._init_fps = float(init.get("fps", 24.0))
        # duration 统一用秒存储/交互；内部按 fps 换算成帧
        self._init_duration_seconds = float(init.get("duration_seconds", 3.0))
        self._init_background_color = tuple(
            init.get("background_color", _DEFAULT_BACKGROUND_COLOR)
        )

        # 宽高比锁状态
        self._aspect_locked = False
        self._aspect_ratio = (
            self._init_width / self._init_height if self._init_height else 16 / 9
        )

        self.setWindowTitle(
            I18N.tr(
                "comp_clip_settings.settings_title" if edit_mode
                else "comp_clip_settings.new_title",
                "Comp Clip Settings" if edit_mode else "New Comp Clip",
            )
        )
        self.setMinimumWidth(360)
        self.setStyleSheet(DIALOG_BASE_STYLE)

        self._build_ui()

    # ------------------------------------------------------------------
    # UI 构建
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)

        # 名称
        layout.addWidget(self._build_name_row())

        # 分辨率
        layout.addWidget(self._build_resolution_group())

        # 时长 + 帧率
        layout.addWidget(self._build_timing_group())

        # 背景颜色
        layout.addWidget(self._build_background_group())

        # 校验错误提示（默认隐藏）
        self._hint_label = QLabel("")
        self._hint_label.setStyleSheet("color: #ff6b6b; font-size: 10px;")
        self._hint_label.hide()
        layout.addWidget(self._hint_label)

        # 按钮
        layout.addLayout(self._build_button_row())

    def _make_label(self, text: str) -> QLabel:
        """创建与合成面板一致的参数标签（#bbb, 10px）。"""
        lbl = QLabel(text)
        lbl.setStyleSheet("color: #bbb; font-size: 10px;")
        return lbl

    def _build_name_row(self) -> QWidget:
        """名称输入行。"""
        row = QWidget()
        h = QHBoxLayout(row)
        h.setContentsMargins(0, 2, 0, 2)
        h.setSpacing(6)

        h.addWidget(self._make_label(
            I18N.tr("comp_clip_settings.name", "Name:")))
        self._name_edit = QLineEdit(self._init_name)
        self._name_edit.setPlaceholderText(
            I18N.tr("comp_clip_settings.name_placeholder", "Comp Clip"))
        h.addWidget(self._name_edit, 1)
        return row

    def _build_resolution_group(self) -> QGroupBox:
        """分辨率分组：预设下拉 + 宽高 DragValue + 宽高比锁。"""
        group = QGroupBox(
            I18N.tr("comp_clip_settings.resolution", "Resolution"))
        v = QVBoxLayout(group)
        v.setContentsMargins(8, 6, 8, 6)
        v.setSpacing(4)

        # 预设下拉
        preset_row = QWidget()
        ph = QHBoxLayout(preset_row)
        ph.setContentsMargins(0, 2, 0, 2)
        ph.setSpacing(4)
        ph.addWidget(self._make_label(
            I18N.tr("comp_clip_settings.preset", "Preset:")))
        self._resolution_combo = ChevronComboBox()
        for name, w, h in _RESOLUTION_PRESETS:
            self._resolution_combo.addItem(f"{name}  ({w}x{h})", (w, h))
        self._resolution_combo.addItem(
            I18N.tr("comp_clip_settings.custom", "Custom"), _CUSTOM_KEY)
        ph.addWidget(self._resolution_combo, 1)
        v.addWidget(preset_row)

        # 宽高 + 锁
        wh_row = QWidget()
        wh = QHBoxLayout(wh_row)
        wh.setContentsMargins(0, 2, 0, 2)
        wh.setSpacing(4)

        wh.addWidget(self._make_label("W"))
        self._width_drag = DragValue("", 64, 16384, float(self._init_width),
                                     decimals=0)
        self._width_drag.setFixedValueWidth(64)
        wh.addWidget(self._width_drag)

        # 宽高比锁按钮（统一小锁图标：锁定=橙 / 解锁=灰）
        self._aspect_lock_btn = make_link_button(
            initial_linked=False,  # 与原 QPushButton 默认未勾选一致
            tooltip=I18N.tr("comp_clip_settings.aspect_lock",
                            "Lock aspect ratio"),
        )
        self._aspect_lock_btn.toggled.connect(self._on_aspect_lock_toggled)
        wh.addWidget(self._aspect_lock_btn)

        wh.addWidget(self._make_label("H"))
        self._height_drag = DragValue("", 64, 16384, float(self._init_height),
                                      decimals=0)
        self._height_drag.setFixedValueWidth(64)
        wh.addWidget(self._height_drag)
        wh.addStretch(1)
        v.addWidget(wh_row)

        # 信号连接
        self._resolution_combo.currentIndexChanged.connect(
            self._on_resolution_preset_changed)
        self._width_drag.valueChanged.connect(self._on_width_changed)
        self._height_drag.valueChanged.connect(self._on_height_changed)

        # 初始化预设下拉选中状态
        self._sync_resolution_combo()

        return group

    def _build_timing_group(self) -> QGroupBox:
        """帧率 + 时长分组。"""
        group = QGroupBox(
            I18N.tr("comp_clip_settings.timing", "Timing"))
        v = QVBoxLayout(group)
        v.setContentsMargins(8, 6, 8, 6)
        v.setSpacing(4)

        # 帧率
        fps_row = QWidget()
        fh = QHBoxLayout(fps_row)
        fh.setContentsMargins(0, 2, 0, 2)
        fh.setSpacing(4)
        fh.addWidget(self._make_label(
            I18N.tr("comp_clip_settings.fps", "Frame Rate:")))
        self._fps_combo = ChevronComboBox()
        for label, fps in _FPS_PRESETS:
            self._fps_combo.addItem(f"{label} fps", fps)
        self._fps_combo.addItem(
            I18N.tr("comp_clip_settings.custom", "Custom"), _CUSTOM_KEY)
        fh.addWidget(self._fps_combo, 1)

        # Custom 帧率输入：仅当下拉选中 Custom 时显示（AUD-19740）。
        # DragValue 保证数值总是被 clamp 在 [1, 240]，杜绝
        # float("__custom__") 一类的解析失败。
        self._custom_fps_drag = DragValue(
            "", 1.0, 240.0, max(1.0, min(240.0, self._init_fps)),
            decimals=3, suffix=" fps")
        self._custom_fps_drag.setSingleStep(0.001)
        self._custom_fps_drag.setFixedValueWidth(72)
        fh.addWidget(self._custom_fps_drag)
        v.addWidget(fps_row)

        # 时长（秒）
        dur_row = QWidget()
        dh = QHBoxLayout(dur_row)
        dh.setContentsMargins(0, 2, 0, 2)
        dh.setSpacing(4)
        dh.addWidget(self._make_label(
            I18N.tr("comp_clip_settings.duration", "Duration:")))
        self._duration_drag = DragValue(
            "", 0.1, 3600.0, self._init_duration_seconds, decimals=1,
            suffix="s")
        self._duration_drag.setSingleStep(0.1)
        self._duration_drag.setFixedValueWidth(64)
        dh.addWidget(self._duration_drag)
        dh.addWidget(self._make_label("s"))
        dh.addStretch(1)
        v.addWidget(dur_row)

        # 初始化 fps 下拉选中
        self._sync_fps_combo()
        # 下拉切换时同步 Custom 输入框可见性
        self._fps_combo.currentIndexChanged.connect(
            self._on_fps_combo_changed)
        self._update_custom_fps_visibility()

        return group

    def _build_background_group(self) -> QGroupBox:
        """背景颜色分组。"""
        group = QGroupBox(
            I18N.tr("comp_clip_settings.background_color", "Background Color"))
        v = QVBoxLayout(group)
        v.setContentsMargins(8, 6, 8, 6)
        v.setSpacing(4)

        color_row = QWidget()
        ch = QHBoxLayout(color_row)
        ch.setContentsMargins(0, 2, 0, 2)
        ch.setSpacing(6)

        self._bg_color_value = self._init_background_color
        self._bg_color_control = ColorPickerControl(
            self._bg_color_value, parent=color_row)
        self._bg_color_control.color_changed.connect(
            self._on_background_color_changed)
        # 保留旧属性别名，避免外部面板/测试依赖色块和 HEX 标签。
        self._bg_swatch = self._bg_color_control.swatch
        self._bg_hex_label = self._bg_color_control.hex_label
        ch.addWidget(self._bg_color_control, 1)
        v.addWidget(color_row)

        return group

    def _build_button_row(self) -> QHBoxLayout:
        """确定/取消按钮行。"""
        row = QHBoxLayout()
        row.addStretch(1)

        cancel_btn = QPushButton(
            I18N.tr("common.cancel", "Cancel"))
        cancel_btn.clicked.connect(self.reject)
        row.addWidget(cancel_btn)

        ok_btn = QPushButton(
            I18N.tr("common.ok", "OK") if not self._edit_mode
            else I18N.tr("common.apply", "Apply"))
        ok_btn.clicked.connect(self._on_accept)
        row.addWidget(ok_btn)
        return row

    # ------------------------------------------------------------------
    # 交互逻辑
    # ------------------------------------------------------------------

    def _on_aspect_lock_toggled(self, checked: bool) -> None:
        self._aspect_locked = checked
        if checked and self._height_drag.value() > 0:
            self._aspect_ratio = (
                self._width_drag.value() / self._height_drag.value())

    def _on_width_changed(self, value: float) -> None:
        if self._aspect_locked and value > 0:
            new_h = max(64, min(16384, int(round(value / self._aspect_ratio))))
            self._height_drag.blockSignals(True)
            self._height_drag.setValue(new_h)
            self._height_drag.blockSignals(False)
        self._sync_resolution_combo()

    def _on_height_changed(self, value: float) -> None:
        if self._aspect_locked and value > 0:
            new_w = max(64, min(16384, int(round(value * self._aspect_ratio))))
            self._width_drag.blockSignals(True)
            self._width_drag.setValue(new_w)
            self._width_drag.blockSignals(False)
        self._sync_resolution_combo()

    def _on_resolution_preset_changed(self, index: int) -> None:
        data = self._resolution_combo.itemData(index)
        if data == _CUSTOM_KEY or data is None:
            return
        w, h = data
        self._width_drag.blockSignals(True)
        self._height_drag.blockSignals(True)
        self._width_drag.setValue(float(w))
        self._height_drag.setValue(float(h))
        self._height_drag.blockSignals(False)
        self._width_drag.blockSignals(False)
        if self._aspect_locked and h > 0:
            self._aspect_ratio = w / h

    def _sync_resolution_combo(self) -> None:
        """根据当前宽高匹配预设，无匹配则选 Custom。"""
        cur_w = int(self._width_drag.value())
        cur_h = int(self._height_drag.value())
        for i in range(self._resolution_combo.count()):
            data = self._resolution_combo.itemData(i)
            if data == _CUSTOM_KEY:
                continue
            if data == (cur_w, cur_h):
                self._resolution_combo.blockSignals(True)
                self._resolution_combo.setCurrentIndex(i)
                self._resolution_combo.blockSignals(False)
                return
        # 选 Custom（最后一项）
        self._resolution_combo.blockSignals(True)
        self._resolution_combo.setCurrentIndex(
            self._resolution_combo.count() - 1)
        self._resolution_combo.blockSignals(False)

    def _sync_fps_combo(self) -> None:
        """根据当前 fps 匹配预设，无匹配则选 Custom。"""
        cur_fps = round(self._init_fps, 3)
        for i in range(self._fps_combo.count()):
            data = self._fps_combo.itemData(i)
            if data == _CUSTOM_KEY:
                continue
            if data is not None and abs(float(data) - cur_fps) < 0.001:
                self._fps_combo.setCurrentIndex(i)
                return
        self._fps_combo.setCurrentIndex(self._fps_combo.count() - 1)

    def _is_custom_fps_selected(self) -> bool:
        """fps 下拉当前是否选中 Custom。"""
        return self._fps_combo.currentData() == _CUSTOM_KEY

    def _on_fps_combo_changed(self, _index: int) -> None:
        """fps 下拉切换：同步 Custom 输入框可见性并清除过期提示。"""
        self._update_custom_fps_visibility()
        self._set_hint("")

    def _update_custom_fps_visibility(self) -> None:
        """仅当选中 Custom 时显示自定义 fps 输入框。"""
        self._custom_fps_drag.setVisible(self._is_custom_fps_selected())

    def _set_hint(self, message: str) -> None:
        """显示/清除底部校验提示。"""
        self._hint_label.setText(message)
        self._hint_label.setVisible(bool(message))

    def _current_fps(self) -> float:
        """解析当前 fps：预设返回数值，Custom 读取输入控件。

        DragValue 保证 Custom 值被 clamp 在 [1, 240]；此处再对非数值
        currentData 防御性回退 24.0，确保永不返回无效值。
        """
        data = self._fps_combo.currentData()
        if isinstance(data, (int, float)) and not isinstance(data, bool):
            fps = float(data)
        else:
            fps = float(self._custom_fps_drag.value())
        if fps <= 0.0 or fps != fps:  # 非正数 / NaN 防御
            return 24.0
        return fps

    def _on_background_color_changed(self, color: QColor) -> None:
        """接收共享颜色控件的已确认颜色。"""
        self._bg_color_value = _qcolor_to_rgba(color)

    def _on_accept(self) -> None:
        """校验并接受；校验失败时给出可见提示且不接受。"""
        name = self._name_edit.text().strip()
        if not name:
            self._name_edit.setFocus()
            self._set_hint(I18N.tr(
                "comp_clip_settings.name_required",
                "Name cannot be empty."))
            return
        if self._is_custom_fps_selected():
            fps = self._current_fps()
            if fps <= 0.0:
                self._custom_fps_drag.setFocus()
                self._set_hint(I18N.tr(
                    "comp_clip_settings.fps_invalid",
                    "Frame rate must be a positive number (1-240)."))
                return
        self._set_hint("")
        self.accept()

    # ------------------------------------------------------------------
    # 结果
    # ------------------------------------------------------------------

    def get_settings(self) -> dict:
        """返回设置字典。

        Returns:
            dict 包含:
                - name: str
                - width: int
                - height: int
                - fps: float
                - duration_seconds: float
                - background_color: (r, g, b, a) tuple[float x4]
        """
        return {
            "name": self._name_edit.text().strip(),
            "width": int(self._width_drag.value()),
            "height": int(self._height_drag.value()),
            "fps": self._current_fps(),
            "duration_seconds": float(self._duration_drag.value()),
            "background_color": self._bg_color_value,
        }
