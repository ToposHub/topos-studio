"""统一的颜色选择控件。

背景颜色在新建 Comp Clip 和 Project Settings 中必须使用同一套交互。
这里集中管理色块、屏幕吸管以及项目统一的 CompactColorStudio，避免两个
页面各自维护一份容易产生差异的实现。
"""

from __future__ import annotations

from typing import Optional

from PyQt6.QtCore import pyqtSignal, QSize, Qt
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QWidget

from src.shared.assets.icon_manager import icon_color_picker
from src.shared.color import EyeDropperOverlay, get_color
from src.shared.color.color_conversion import linear_to_srgb, srgb_to_linear
from src.shared.i18n.i18n import I18N


def linear_rgba_to_qcolor(rgba: tuple[float, float, float, float]) -> QColor:
    """将 scene-linear RGBA 转为拾色器使用的 sRGB QColor。"""
    r, g, b, a = rgba
    return QColor(
        max(0, min(255, int(round(linear_to_srgb(r) * 255)))),
        max(0, min(255, int(round(linear_to_srgb(g) * 255)))),
        max(0, min(255, int(round(linear_to_srgb(b) * 255)))),
        max(0, min(255, int(round(a * 255)))),
    )


def qcolor_to_linear_rgba(color: QColor) -> tuple[float, float, float, float]:
    """将拾色器的 sRGB QColor 转为 scene-linear RGBA。"""
    return (
        srgb_to_linear(color.redF()),
        srgb_to_linear(color.greenF()),
        srgb_to_linear(color.blueF()),
        color.alphaF(),
    )


# Compatibility aliases for callers that imported the old private helpers.
_rgba_to_qcolor = linear_rgba_to_qcolor
_qcolor_to_rgba = qcolor_to_linear_rgba


def _swatch_style(color: QColor) -> str:
    """生成两个设置入口共用的色块样式。"""
    return (
        f"background-color: rgba({color.red()},{color.green()},"
        f"{color.blue()},{color.alpha() / 255:.3f});"
        "border: 1px solid #555; border-radius: 3px;"
        "min-width: 0px; padding: 0px;"
    )


class ColorPickerControl(QWidget):
    """色块 + 屏幕吸管 + 统一拾色器。

    ``color_changed`` 只在用户确认拾色器结果或完成屏幕取色后发射，
    因而取消 CompactColorStudio 不会把预览颜色误写入项目。
    """

    color_changed = pyqtSignal(QColor)

    def __init__(
        self,
        rgba: tuple[float, float, float, float],
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._color = linear_rgba_to_qcolor(rgba)
        self._active_dropper: Optional[EyeDropperOverlay] = None

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        self.swatch = QPushButton()
        self.swatch.setFixedSize(24, 24)
        self.swatch.setCursor(Qt.CursorShape.PointingHandCursor)
        self.swatch.setToolTip(I18N.tr(
            "color_picker.open_color_inspector", "Open Color Inspector"))
        self.swatch.clicked.connect(self._pick_color)
        layout.addWidget(self.swatch)

        self.eyedropper_button = QPushButton()
        self.eyedropper_button.setFixedSize(24, 24)
        self.eyedropper_button.setIcon(icon_color_picker())
        self.eyedropper_button.setIconSize(QSize(14, 14))
        self.eyedropper_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.eyedropper_button.setToolTip(I18N.tr(
            "color_picker.pick_from_screen", "Pick color from screen"))
        self.eyedropper_button.setStyleSheet("""
            QPushButton {
                background-color: #333; border: 1px solid #444; border-radius: 3px;
                min-width: 0px; padding: 0px;
            }
            QPushButton:hover { background-color: #444; border: 1px solid #666; }
        """)
        self.eyedropper_button.clicked.connect(self._start_eyedropper)
        layout.addWidget(self.eyedropper_button)

        self.hex_label = QLabel()
        self.hex_label.setStyleSheet("color: #999; font-size: 10px;")
        layout.addWidget(self.hex_label)
        layout.addStretch(1)

        self._render(self._color)

    def rgba(self) -> tuple[float, float, float, float]:
        """返回当前已确认的 RGBA 值。"""
        return qcolor_to_linear_rgba(self._color)

    def set_rgba(self, rgba: tuple[float, float, float, float]) -> None:
        """从项目数据刷新当前颜色，不触发 ``color_changed``。"""
        self._color = linear_rgba_to_qcolor(rgba)
        self._render(self._color)

    def _render(self, color: QColor) -> None:
        """只更新显示，供拾色器实时预览使用。"""
        self.swatch.setStyleSheet(_swatch_style(color))
        self.hex_label.setText(f"#{color.rgba():08x}"[:9])

    def _pick_color(self) -> None:
        """打开与 Project Settings 完全相同的 CompactColorStudio。"""
        initial = QColor(self._color)

        def _on_live(color: QColor) -> None:
            self._render(color)

        # 使用顶层窗口作为定位参考；get_color 内部仍创建完全独立的
        # Color Inspector，因此不会继承外层 QDialog 的模态事件过滤。
        try:
            result = get_color(
                parent=self.window(), initial=initial, on_live=_on_live)
            if result is None:
                self._render(initial)
            else:
                self._commit_color(result)
        finally:
            self._restore_parent_window()

    def _start_eyedropper(self) -> None:
        """启动与 Project Settings 完全相同的全屏吸管覆盖层。"""
        self._active_dropper = EyeDropperOverlay()
        self._active_dropper.colorPicked.connect(self._commit_color)
        self._active_dropper.cancelled.connect(self._clear_dropper)
        self._active_dropper.start()

    def _commit_color(self, color: QColor) -> None:
        self._color = QColor(color)
        self._render(self._color)
        try:
            self.color_changed.emit(QColor(self._color))
        finally:
            self._clear_dropper()
            self._restore_parent_window()

    def _restore_parent_window(self) -> None:
        """把焦点和层级还给承载颜色控件的窗口。"""
        window = self.window()
        if not window.isVisible():
            return
        window.raise_()
        window.activateWindow()

    def _clear_dropper(self) -> None:
        """释放吸管覆盖层，且绝不在其自身事件处理栈内析构它。

        ``colorPicked`` / ``cancelled`` 由覆盖层自己的 mousePressEvent /
        keyPressEvent 同步发射，此刻本方法仍在该对象的事件分发栈上执行。
        ``EyeDropperOverlay`` 是无父窗口（无 WA_DeleteOnClose），若在此处
        直接把最后一个 Python 引用置 None，sip 会在事件处理器尚未返回时
        立即析构 C++ 对象——等价于事件处理器内 ``delete this``（Qt 明示的
        未定义行为，use-after-free）。改为先调用 ``deleteLater()``：此后
        sip 不再因引用计数归零而立即析构，实际销毁被推迟到事件循环的
        安全点（DeferredDelete）。
        """
        dropper, self._active_dropper = self._active_dropper, None
        if dropper is not None:
            dropper.deleteLater()
