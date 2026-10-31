"""ParamSliderRow -- 全局统一参数行控件

即插即用的 "标签 + 滑动条 + 数值拖拽框" 三件套，封装了 composite 页面
Properties 面板的全部交互特性，供 color / composite / edit 等所有页面共用。

交互方式（继承自 DragValue + ToposClickableSlider）：
- 数值框：左键拖拽调值、中键重置、Shift 粗调、Ctrl 微调、双击内联编辑
- 滑动条：点击滑槽跳转；手柄拖拽三档速度（默认 / Shift 粗调 / Ctrl 微调）
- 面板宽度不足时自动隐藏滑动条

向后兼容：
- 暴露 ``spin`` / ``slider`` / ``scale`` 属性，与旧
  ``create_double_slider`` 返回的 dict 接口一致，可渐进迁移。
- 暴露 ``blockSignals()`` 方法，批量屏蔽内部子控件信号。
"""
from typing import Optional
from PyQt6.QtWidgets import QWidget, QHBoxLayout, QLabel
from PyQt6.QtCore import Qt, pyqtSignal

from src.shared.ui.widgets.drag_value import DragValue
from src.shared.ui.widgets.topos_controls import ToposClickableSlider
from src.shared.ui.style_sheet import (
    PARAM_LABEL_STYLE,
    PARAM_LABEL_DISABLED_STYLE,
)

# 面板宽度低于此值时自动隐藏滑动条
# 固定开销：label(65) + drag(64) + spacing(8) + margins(~6) ≈ 143px
# 低于此阈值时 slider 剩余空间 < ~100px，不值得显示
_DEFAULT_SLIDER_SHOW_WIDTH = 240


def _to_scale_int(value: float, scale: int) -> int:
    """浮点 -> slider int 域，四舍五入而非截断。

    ``int(0.29 * 100) == 28`` 这类浮点噪声截断会让滑条上限/回显比真实
    约束值少一格（如动态 range 的 Soft Clip 膝宽）；先 round 再取整，
    已精确落在 int 域上的值行为不变。
    """
    return int(round(value * scale))


class ParamSliderRow(QWidget):
    """Label + Slider + DragValue 三合一参数行。

    Signals:
        valueChanged(float): 数值变化（slider 或 drag 任一触发）。
        interactionStarted: 用户开始交互（slider 按下或 drag 开始拖拽）。
        interactionEnded: 用户结束交互（slider 释放或 drag 结束拖拽）。
    """

    valueChanged = pyqtSignal(float)
    interactionStarted = pyqtSignal()
    interactionEnded = pyqtSignal()

    def __init__(
        self,
        label: str,
        min_val: float,
        max_val: float,
        default_val: float,
        step: float = 0.01,
        decimals: int = 2,
        suffix: str = "",
        label_width: int = 65,
        slider_auto_hide: bool = True,
        slider_show_width: int = _DEFAULT_SLIDER_SHOW_WIDTH,
        value_fixed_width: int = 64,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._updating = False          # 防递归锁
        self._default_val = float(default_val)
        self._slider_auto_hide = slider_auto_hide
        self._slider_show_width = slider_show_width

        # -- scale：将浮点映射到 slider 的 int 域 --
        self._scale = 100 if step < 1 else 1
        self._step = step

        # ---------- 子控件 ----------
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)

        # 1) 标签
        self._label = QLabel(label)
        self._label.setFixedWidth(label_width)
        self._label.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self._label.setStyleSheet(PARAM_LABEL_STYLE)
        layout.addWidget(self._label)

        # 2) 滑动条
        self._slider = ToposClickableSlider(
            Qt.Orientation.Horizontal,
            default_value=int(default_val * self._scale))
        self._slider.setRange(_to_scale_int(min_val, self._scale),
                              _to_scale_int(max_val, self._scale))
        self._slider.setValue(_to_scale_int(default_val, self._scale))
        self._slider.setMinimumHeight(18)
        self._slider.enable_velocity_drag = True
        layout.addWidget(self._slider)

        # 3) 数值拖拽框（无标签，纯粹的数值格）
        self._drag = DragValue(
            "", min_v=min_val, max_v=max_val, default=default_val,
            decimals=decimals, suffix=suffix)
        self._drag.setFixedValueWidth(value_fixed_width)
        layout.addWidget(self._drag)

        # ---------- 信号连接 ----------
        self._slider.valueChanged.connect(self._on_slider_changed)
        self._slider.sliderPressed.connect(self.interactionStarted.emit)
        self._slider.sliderReleased.connect(self.interactionEnded.emit)
        self._drag.valueChanged.connect(self._on_drag_changed)
        self._drag.interaction_started.connect(self.interactionStarted.emit)
        self._drag.interaction_ended.connect(self.interactionEnded.emit)

    # ================================================================
    # 内部同步
    # ================================================================

    def _on_slider_changed(self, int_val: int):
        """Slider int -> DragValue float."""
        if self._updating:
            return
        self._updating = True
        float_val = int_val / self._scale
        self._drag.setValue(float_val)
        self._updating = False
        self.valueChanged.emit(float_val)

    def _on_drag_changed(self, float_val: float):
        """DragValue float -> Slider int."""
        if self._updating:
            return
        self._updating = True
        self._slider.setValue(_to_scale_int(float_val, self._scale))
        self._updating = False
        self.valueChanged.emit(float_val)

    # ================================================================
    # 公开 API
    # ================================================================

    def value(self) -> float:
        """获取当前浮点值。"""
        return self._drag.value()

    def setValue(self, v: float):
        """设置值（同时更新 slider 和 drag）。"""
        if self._updating:
            return
        self._updating = True
        self._drag.setValue(v)
        self._slider.setValue(_to_scale_int(v, self._scale))
        self._updating = False

    def reset(self):
        """重置为默认值。"""
        self.setValue(self._default_val)

    def setEnabled(self, enabled: bool):
        self._slider.setEnabled(enabled)
        self._drag.setEnabled(enabled)
        # 标签同步变暗/恢复（达芬奇式整列变灰；QSS 固定色不会自动置灰）
        self._label.setStyleSheet(
            PARAM_LABEL_STYLE if enabled else PARAM_LABEL_DISABLED_STYLE)
        super().setEnabled(enabled)

    def setDecimals(self, d: int):
        self._drag.setDecimals(d)

    def setRange(self, min_val: float, max_val: float):
        self._drag.setRange(min_val, max_val)
        self._slider.setRange(_to_scale_int(min_val, self._scale),
                              _to_scale_int(max_val, self._scale))

    def setDefaultValue(self, v: float):
        self._default_val = float(v)
        self._drag.setDefaultValue(v)
        self._slider.set_default_value(int(v * self._scale))

    def blockSignals(self, block: bool) -> bool:
        """批量屏蔽/恢复内部子控件信号，用于外部批量更新。"""
        self._drag.blockSignals(block)
        self._slider.blockSignals(block)
        return super().blockSignals(block)

    # ================================================================
    # 向后兼容属性（等价于旧 create_double_slider 返回的 dict key）
    # ================================================================

    @property
    def spin(self) -> DragValue:
        """兼容旧 ``control['spin']``。"""
        return self._drag

    @property
    def slider(self) -> ToposClickableSlider:
        """兼容旧 ``control['slider']``。"""
        return self._slider

    @property
    def scale(self) -> int:
        """兼容旧 ``control['scale']``。"""
        return self._scale

    @property
    def label(self) -> QLabel:
        """兼容旧 ``control['label']``，用于设置 tooltip 等。"""
        return self._label

    # ================================================================
    # Slider 自适应显隐
    # ================================================================

    def apply_slider_visibility(self, panel_width: int):
        """根据面板宽度决定 slider 是否显示。

        由父面板的 resizeEvent 调用，或自身 resizeEvent 触发。
        """
        if self._slider_auto_hide:
            self._slider.setVisible(panel_width >= self._slider_show_width)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self._slider_auto_hide:
            w = self.width()
            if w > 50:  # 已布局，使用自身宽度（最可靠）
                self.apply_slider_visibility(w)
            else:
                panel = self._find_resizable_parent()
                if panel is not None:
                    self.apply_slider_visibility(panel.width())

    def _find_resizable_parent(self) -> Optional[QWidget]:
        """向上查找可调节大小的父面板（通常是有 resizeEvent 的那一层）。"""
        w = self.parent()
        depth = 0
        while w is not None and depth < 6:
            if isinstance(w, QWidget) and w.width() >= 150:
                return w
            w = w.parent()
            depth += 1
        return None

    # ================================================================
    # 收集所有 ParamSliderRow 实例（供面板批量 slider 显隐）
    # ================================================================

    @staticmethod
    def collect_rows(parent: QWidget) -> list:
        """收集 parent 下所有 ParamSliderRow 实例。"""
        return parent.findChildren(ParamSliderRow)

    @staticmethod
    def apply_visibility_to_all(rows: list, panel_width: int):
        """批量设置所有行的 slider 显隐。"""
        for row in rows:
            row.apply_slider_visibility(panel_width)
