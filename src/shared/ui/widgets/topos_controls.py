"""
Topos Color - 统一的基础 UI 组件
Unified base UI components for Topos Color

此模块包含所有面板共用的自定义控件，确保 UI 风格一致性。
"""

from typing import Optional, List, Tuple, Union
from PyQt6.QtWidgets import QSlider, QWidget, QStyleOptionSlider, QStyle
from PyQt6.QtCore import Qt, pyqtSignal, QRect
from PyQt6.QtGui import (
    QColor,
    QMouseEvent,
)

# 滑杆把手的三档速度常数（宽度自适应：拖满控件宽度 = 完整值域，把手跟手）。
# 注意与 DragValue 数值格的契约不同：数值格用固定 200px 参考宽度 +
# 更细的默认档（见 drag_value.drag_speed_per_pixel），把手拖拽则要求
# 把手贴着指针走，因此 base 取控件实际宽度且默认档为 ×1。两者各自独立。
_SLIDER_DEFAULT_SPEED = 1.0   # 默认 = base × 1（拖满全程 = 完整范围）
_SLIDER_LARGE_SPEED = 10.0    # Shift = base × 10（大范围 / 粗调）
_SLIDER_FINE_SPEED = 0.1      # Ctrl / ⌘ = base × 0.1（微调）

from src.shared.ui.style_sheet import gradient_slider_groove_style


class ToposSlider(QSlider):
    """
    统一的滑块组件，支持双击重置到默认值。
    
    Features:
        - 双击重置到默认值
        - 发射 double_clicked 信号
        - 可选的渐变背景支持
        - 统一的视觉样式
    
    Signals:
        double_clicked: 双击时发射
        reset_to_default: 重置到默认值时发射，携带默认值
    """
    
    double_clicked = pyqtSignal()
    reset_to_default = pyqtSignal(int)
    
    def __init__(
        self, 
        orientation: Qt.Orientation = Qt.Orientation.Horizontal,
        default_value: int = 0,
        parent: Optional[QWidget] = None
    ) -> None:
        """
        初始化 ToposSlider。
        
        Args:
            orientation: 滑块方向（水平或垂直）
            default_value: 默认值，双击时重置到此值
            parent: 父控件
        """
        super().__init__(orientation, parent)
        self._default_value: int = default_value
        self._gradient_stops: Optional[List[Tuple[float, Union[str, QColor]]]] = None
        self._show_progress: bool = True  # 是否显示进度填充
        
    def set_default_value(self, value: int) -> None:
        """设置默认值"""
        self._default_value = value
        
    def get_default_value(self) -> int:
        """获取默认值"""
        return self._default_value
    
    def set_show_progress(self, show: bool) -> None:
        """设置是否显示进度填充（sub-page）"""
        self._show_progress = show
        
    def mouseDoubleClickEvent(self, event) -> None:
        """双击事件处理：重置到默认值"""
        if event.button() == Qt.MouseButton.LeftButton:
            self.setValue(self._default_value)
            self.double_clicked.emit()
            self.reset_to_default.emit(self._default_value)
        super().mouseDoubleClickEvent(event)
    
    def set_gradient_stops(self, stops: List[Tuple[float, Union[str, QColor]]]) -> None:
        """
        设置滑槽的渐变背景。
        
        注意：使用此方法会覆盖全局 QSS 样式，仅用于特殊需求的滑块
        （如 mixer_panel.py 中的色相/饱和度滑块）。
        
        Args:
            stops: 渐变停止点列表，格式为 [(position, color), ...]
                   position: 0.0 到 1.0 之间的位置
                   color: QColor 对象或十六进制颜色字符串
        """
        self._gradient_stops = stops
        self._apply_gradient_style()
    
    def _apply_gradient_style(self) -> None:
        """应用渐变样式到滑块"""
        if not self._gradient_stops:
            return
            
        # 构建 QSS 渐变字符串
        grad_str = "qlineargradient(x1:0, y1:0, x2:1, y2:0, "
        for pos, color in self._gradient_stops:
            if isinstance(color, QColor):
                color_str = f"rgba({color.red()}, {color.green()}, {color.blue()}, {color.alpha()})"
            else:
                color_str = color
            grad_str += f"stop:{pos} {color_str}, "
        grad_str = grad_str.rstrip(", ") + ")"
        
        # 应用样式（使用集中模板，保留手柄样式统一）
        self.setStyleSheet(gradient_slider_groove_style(grad_str))
    
    def clear_gradient(self) -> None:
        """清除渐变样式，恢复到全局 QSS 样式"""
        self._gradient_stops = None
        self.setStyleSheet("")


class ToposClickableSlider(ToposSlider):
    """
    带有可点击跳转功能的滑块。
    
    继承自 ToposSlider，增加点击滑槽直接跳转到该位置的功能。
    这是对原有 ClickableSlider 的替代实现。

    可选的 ``enable_velocity_drag`` 开关启用「三档速度手柄拖拽」
    （默认关闭以保持 color 页等已发布行为不变）：
        默认拖拽手柄 = base × 1.0
        Shift       = base × 10   （大范围 / 粗调）
        Ctrl / ⌘    = base × 0.1  （微调）
    其中 ``base = (maximum − minimum) / widget_width``，与 DragValue 完全一致，
    拖满控件宽度 = 覆盖完整值域。

    注意：只有「抓住手柄拖拽」走速度档位；点击滑槽空白处仍瞬间跳转
    （保留现有快速定位行为）。
    """

    def __init__(
        self,
        orientation: Qt.Orientation = Qt.Orientation.Horizontal,
        default_value: int = 0,
        parent: Optional[QWidget] = None
    ) -> None:
        super().__init__(orientation, default_value, parent)
        # 三档速度拖拽（opt-in）。color 页十几个面板默认不启用，行为不变。
        self._velocity_drag = False
        # 当前拖拽会话状态
        self._vdrag_active = False
        self._vdrag_start_pos = 0.0      # 拖拽起点（水平=x，垂直=y）
        self._vdrag_start_value = 0      # 拖拽起点的 value（int 域）
        self._vdrag_accum = 0.0          # 浮点累积，避免 int 来回拖丢精度

    # ── opt-in 开关 ───────────────────────────────────────────

    @property
    def enable_velocity_drag(self) -> bool:
        return self._velocity_drag

    @enable_velocity_drag.setter
    def enable_velocity_drag(self, v: bool) -> None:
        self._velocity_drag = bool(v)

    # ── 三档速度 ──────────────────────────────────────────────

    def _drag_sensitivity(self, mods: Qt.KeyboardModifier) -> float:
        """拖拽时每像素的数值变化量，按修饰键分三档。

        宽度自适应：base = (max − min) / widget_width，
        拖满控件宽度 = 覆盖完整值域，跟手感与控件尺寸无关。

          默认（无修饰键）= base × 1.0   ← 拖满全程 = 完整范围
          Shift          = base × 10.0  ← 拖满全程 = 10 倍范围
          Ctrl / ⌘       = base × 0.1   ← 拖满全程 = 1/10 范围

        int 域最终 setValue 会按整数四舍五入。
        """
        w = max(self.width(), 1)
        base = (self.maximum() - self.minimum()) / w
        if mods & Qt.KeyboardModifier.ShiftModifier:
            return base * _SLIDER_LARGE_SPEED
        if mods & (Qt.KeyboardModifier.ControlModifier
                   | Qt.KeyboardModifier.MetaModifier):
            return base * _SLIDER_FINE_SPEED
        return base * _SLIDER_DEFAULT_SPEED

    def _handle_rect(self) -> QRect:
        """当前手柄在控件内的矩形（用 Qt 官方 subControlRect，跨平台可靠）。"""
        opt = QStyleOptionSlider()
        self.initStyleOption(opt)
        return self.style().subControlRect(
            QStyle.ComplexControl.CC_Slider, opt,
            QStyle.SubControl.SC_SliderHandle, self,
        )

    def _press_pos(self, event: QMouseEvent) -> float:
        return (event.position().x() if self.orientation() == Qt.Orientation.Horizontal
                else event.position().y())

    # ── 鼠标事件 ──────────────────────────────────────────────

    def mousePressEvent(self, event: QMouseEvent) -> None:
        """左键：
        - 开启三档速度 + 点击落在手柄上 → 进入速度拖拽会话，自己接管；
        - 否则 → 原有「点击滑槽跳转」+ super。
        """
        if (event.button() == Qt.MouseButton.LeftButton
                and self._velocity_drag
                and self._handle_rect().contains(event.position().toPoint())):
            self._vdrag_active = True
            self._vdrag_start_pos = self._press_pos(event)
            self._vdrag_start_value = self.value()
            self._vdrag_accum = 0.0
            event.accept()
            return
        # 原有行为：点击滑槽直接跳转
        if event.button() == Qt.MouseButton.LeftButton:
            if self.orientation() == Qt.Orientation.Horizontal:
                value = self._pos_to_value_horizontal(event.position().x())
            else:
                value = self._pos_to_value_vertical(event.position().y())
            self.setValue(value)
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if not self._vdrag_active:
            super().mouseMoveEvent(event)
            return
        dx = self._press_pos(event) - self._vdrag_start_pos
        # 垂直滑块：向上拖增大（与手柄方向一致），故取负
        if self.orientation() == Qt.Orientation.Vertical:
            dx = -dx
        sensitivity = self._drag_sensitivity(event.modifiers())
        # 累积浮点增量，再四舍五入成 int，保证小幅拖拽也能逐渐变化。
        self._vdrag_accum = dx * sensitivity
        new_value = self._vdrag_start_value + int(round(self._vdrag_accum))
        self.setValue(new_value)
        event.accept()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self._vdrag_active:
            self._vdrag_active = False
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def _pos_to_value_horizontal(self, x: float) -> int:
        """将水平位置转换为滑块值"""
        rect = self.rect()
        handle_width = 14
        groove_start = handle_width // 2
        groove_end = rect.width() - handle_width // 2
        groove_width = groove_end - groove_start
        
        if groove_width <= 0:
            return self.value()
            
        ratio = (x - groove_start) / groove_width
        ratio = max(0.0, min(1.0, ratio))
        
        range_min = self.minimum()
        range_max = self.maximum()
        value = range_min + ratio * (range_max - range_min)
        
        return int(round(value))
    
    def _pos_to_value_vertical(self, y: float) -> int:
        """将垂直位置转换为滑块值"""
        rect = self.rect()
        handle_height = 14
        groove_start = handle_height // 2
        groove_end = rect.height() - handle_height // 2
        groove_height = groove_end - groove_start
        
        if groove_height <= 0:
            return self.value()
            
        # 垂直滑块：y 从上到下，值从大到小
        ratio = (groove_end - y) / groove_height
        ratio = max(0.0, min(1.0, ratio))
        
        range_min = self.minimum()
        range_max = self.maximum()
        value = range_min + ratio * (range_max - range_min)
        
        return int(round(value))


# 为了向后兼容，提供别名
ClickableSlider = ToposClickableSlider