"""DragValue — 数值输入框（达芬奇 Fusion / Nuke 风格）

这是全局共用的原子数值控件，放在 ``src/shared/ui/widgets`` 下统一管理。

交互方式：
- 数值区域按住鼠标左键左右拖拽调整值（超过 3px 阈值才进入拖拽，避免误判双击）
- 拖拽按固定 200px 参考宽度换算（与控件实际宽度、DPR、平台无关）：
  - 默认拖拽 = 细调手感（拖满 10 个参考宽度 = 完整值域）
  - Shift + 拖拽 → 大范围 / 粗调（拖满 1 个参考宽度 = 完整值域）
  - Ctrl（Mac 为 ⌘ Command）+ 拖拽 → 微调（带 0.1/px 下限，小值域保持比例）
- 双击数值 → 进入内联输入模式（直接在数值格内键入；Enter / 失焦提交，Esc 取消）
- 鼠标滚轮 → 按 single step 步进（Shift=大范围 ×10，Ctrl=微调 ×0.1）
- 鼠标中键点击 → 重置为默认值

构造参数 ``label`` 可为空：为空时不创建标签控件，渲染成纯粹的"数值格"，
方便调用方（如节点属性面板）把 X / Y 两个数值格摆在同一个自定义标签旁边。
带标签的旧用法（mask_panel）保持不变。

样式常量统一引用自 src/shared/ui/style_sheet.py
"""
from PyQt6.QtWidgets import (
    QWidget, QHBoxLayout, QLabel, QLineEdit, QSizePolicy, QApplication,
)
from PyQt6.QtCore import Qt, QEvent, QLocale, pyqtSignal
from PyQt6.QtGui import QMouseEvent, QDoubleValidator

from src.shared.ui.style_sheet import (
    DRAG_VALUE_LABEL_STYLE,
    DRAG_VALUE_NORMAL_STYLE,
    DRAG_VALUE_ACTIVE_STYLE,
)

# 进入拖拽前需要移动的最小像素数；小于此值视为点击，不改变数值，
# 从而保证"干净的双击"不会因为微小抖动被误判为拖拽。
_DRAG_THRESHOLD = 3

# ── 拖拽 / 滚轮速度档位 ──────────────────────────────────────────────────────
# 固定 200px 参考宽度：base = (max - min) / 200
#   换算与控件实际宽度、DPR、平台无关 —— 同样的拖拽距离在任何机器上得到同样的数值
#   默认（无修饰键）= base × 0.1  ← 拖满 10 个参考宽度 = 完整值域（跟手但不跳变）
#   Shift          = base × 1.0  ← 拖满全程 = 完整范围（大范围 / 粗调）
#   Ctrl / ⌘       = base × 0.01 ← 微调；当 base ≥ 0.1 时下限 0.1/px，
#                    避免亚 0.1 的抖动；base < 0.1（小值域）时保持比例，
#                    否则一像素就会跨过整个值域
_DRAG_REFERENCE_WIDTH = 200.0
_DEFAULT_SPEED = 0.1    # 默认 = base × 0.1（原 Shift 细调手感）
_LARGE_SPEED = 1.0      # Shift = base × 1.0（大范围 / 粗调）
_FINE_SPEED = 0.01      # Ctrl / ⌘ = base × 0.01（微调）
_FINE_FLOOR = 0.1       # 微调档的每像素下限（仅当 base ≥ 0.1 时生效）


def drag_speed_per_pixel(span: float, modifiers) -> float:
    """拖拽时每像素的数值变化量（纯函数，便于跨平台断言）。

    ``span`` 是值域宽度 (max - min)；``modifiers`` 是 Qt KeyboardModifiers。
    档位与下限规则见模块顶部的常量注释。
    """
    base = abs(float(span)) / _DRAG_REFERENCE_WIDTH
    if modifiers & Qt.KeyboardModifier.ShiftModifier:
        return base * _LARGE_SPEED
    if modifiers & (Qt.KeyboardModifier.ControlModifier
                    | Qt.KeyboardModifier.MetaModifier):
        speed = base * _FINE_SPEED
        if base >= _FINE_FLOOR:
            speed = max(speed, _FINE_FLOOR)
        return speed
    return base * _DEFAULT_SPEED


class DragValue(QWidget):
    """可拖拽 / 可键入的数值输入框。

    Signals:
        valueChanged(float): 数值变化时发射（已 clamp 到范围内）。
        interaction_started: Emitted once a pointer drag crosses the threshold.
        interaction_ended: Emitted after an engaged pointer drag is released.
    """

    valueChanged = pyqtSignal(float)
    interaction_started = pyqtSignal()
    interaction_ended = pyqtSignal()

    def __init__(self, label: str, min_v: float, max_v: float, default: float,
                 decimals: int = 0, suffix: str = "", compact: bool = False,
                 stretchable: bool = False, parent=None) -> None:
        super().__init__(parent)
        self._min = float(min_v)
        self._max = float(max_v)
        self._default = float(default)
        self._decimals = int(decimals)
        self._suffix = suffix
        self._value = self._default
        self._mixed_value = False
        self._single_step = 10.0 ** (-self._decimals) if self._decimals > 0 else 1.0
        self._block_signals = False
        self._stretchable = stretchable

        # 拖拽状态
        self._dragging = False           # 鼠标左键已按下（潜在的拖拽）
        self._drag_engaged = False       # 已越过阈值，进入真正的数值调整
        self._drag_start_x = 0
        self._drag_start_value = 0.0

        # 内联编辑状态
        self._editing = False

        # Match the global combo/input height so the inline editor has enough
        # vertical room for its text and selection highlight.
        self.setFixedHeight(24)
        self.setCursor(Qt.CursorShape.SizeHorCursor)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)

        # 可选标签
        self._label: QLabel | None = None
        if label:
            self._label = QLabel(label)
            self._label.setMinimumWidth(50 if compact else 60)
            self._label.setAlignment(
                Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            self._label.setStyleSheet(DRAG_VALUE_LABEL_STYLE)
            layout.addWidget(self._label)

        # 数值格：QLabel（显示）与 QLineEdit（内联输入）叠放在同一格内，
        # 通过 QStackedLayout 切换；非编辑态下 line edit 被隐藏，不会拦截鼠标事件。
        self._value_label = QLabel()
        self._value_label.setAlignment(
            Qt.AlignmentFlag.AlignCenter | Qt.AlignmentFlag.AlignVCenter)
        self._value_label.setStyleSheet(DRAG_VALUE_NORMAL_STYLE)
        self._value_label.setMinimumWidth(50 if compact else 60)
        if self._stretchable:
            self._value_label.setSizePolicy(
                QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        else:
            self._value_label.setSizePolicy(
                QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)

        self._edit = QLineEdit()
        self._edit.setObjectName("DragValueInlineEdit")
        self._edit.setAlignment(
            Qt.AlignmentFlag.AlignCenter | Qt.AlignmentFlag.AlignVCenter)
        self._edit.setStyleSheet("""
            QLineEdit {
                color: #fff; font-size: 11px; font-weight: bold;
                background: #3a3a40; border: 1px solid #ff8800;
                border-radius: 3px; padding: 0px 3px;
                min-height: 0px; max-height: 24px;
            }
        """)
        self._edit.installEventFilter(self)
        self._edit.returnPressed.connect(self._commit_edit)
        self._edit.editingFinished.connect(self._commit_edit)
        self._edit.hide()

        # _edit 始终用 Maximum 策略，防止双击进入编辑模式时布局变形
        self._edit.setSizePolicy(
            QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
        self._edit.setFixedHeight(24)
        if not self._stretchable:
            self._edit.setMinimumWidth(50 if compact else 60)

        layout.addWidget(self._value_label)
        layout.addWidget(self._edit)

        self._update_display()

    # ---- 格式化 / 显示 ----

    def _format_value(self, v: float) -> str:
        if self._decimals == 0:
            return f"{int(round(v))}{self._suffix}"
        return f"{v:.{self._decimals}f}{self._suffix}"

    def _update_display(self):
        self._value_label.setText(
            "—" if self._mixed_value else self._format_value(self._value))

    def setMixedValue(self, mixed: bool) -> None:
        """Display a multi-selection value without emitting an edit."""
        self._mixed_value = bool(mixed)
        self.setProperty("mixedValue", self._mixed_value)
        self._update_display()

    def isMixedValue(self) -> bool:
        return self._mixed_value

    # ---- 取值 ----

    def value(self) -> float:
        return self._value

    def defaultValue(self) -> float:
        return self._default

    def setDefaultValue(self, v: float):
        self._default = float(v)

    def setValue(self, v: float):
        v = max(self._min, min(self._max, float(v)))
        was_mixed = self._mixed_value
        self._mixed_value = False
        self.setProperty("mixedValue", False)
        if abs(v - self._value) < 1e-10 and not was_mixed:
            return
        self._value = v
        self._update_display()
        if not self._block_signals:
            self.valueChanged.emit(self._value)

    def blockSignals(self, block: bool) -> bool:
        old = self._block_signals
        self._block_signals = block
        return old

    # ---- QDoubleSpinBox 兼容 API（供面板配置）----

    def setRange(self, min_v: float, max_v: float):
        self._min = float(min_v)
        self._max = float(max_v)
        self.setValue(self._value)  # 重新 clamp
        self._rebuild_validator()

    def setSingleStep(self, step: float):
        self._single_step = float(step)

    def setFixedValueWidth(self, w: int):
        """Lock the value cell to a fixed pixel width so it never stretches with
        the panel width (the slider takes the variable space instead)."""
        self._stretchable = False
        self._value_label.setFixedWidth(w)
        self._value_label.setSizePolicy(
            QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self._edit.setFixedWidth(w)
        self._edit.setSizePolicy(
            QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)

    def setDecimals(self, d: int):
        self._decimals = int(d)
        self._single_step = 10.0 ** (-self._decimals) if self._decimals > 0 else 1.0
        self._update_display()
        self._rebuild_validator()

    def setSuffix(self, suffix: str):
        """Set the display suffix while keeping the shared input behavior."""
        self._suffix = str(suffix)
        self._update_display()

    def _rebuild_validator(self):
        v = QDoubleValidator(self._min, self._max, self._decimals, self._edit)
        v.setLocale(QLocale(QLocale.Language.C))  # 固定用 '.' 作小数点
        self._edit.setValidator(v)

    # ---- 内联编辑 ----

    def isEditing(self) -> bool:
        return self._editing

    def _begin_edit(self):
        if self._editing:
            return
        self._dragging = False
        self._drag_engaged = False
        self._editing = True
        self._value_label.setStyleSheet(DRAG_VALUE_ACTIVE_STYLE)
        self._value_label.hide()
        self._edit.setText(f"{self._value:.{self._decimals}f}")
        self._rebuild_validator()
        self._edit.show()
        self._edit.setFocus(Qt.FocusReason.OtherFocusReason)
        self._edit.selectAll()
        # 安装全局事件过滤器，检测 _edit 外部的鼠标点击以主动提交编辑
        QApplication.instance().installEventFilter(self)

    def _end_edit(self):
        self._editing = False
        self._edit.hide()
        self._value_label.show()
        self._value_label.setStyleSheet(DRAG_VALUE_NORMAL_STYLE)
        # 移除全局事件过滤器
        app = QApplication.instance()
        if app:
            app.removeEventFilter(self)

    def _commit_edit(self):
        if not self._editing:
            return
        text = self._edit.text().strip().replace(",", ".")
        try:
            val = float(text)
        except ValueError:
            self._end_edit()
            self._update_display()
            return
        self._end_edit()
        clamped_val = max(self._min, min(self._max, val))
        changed = self._mixed_value or abs(clamped_val - self._value) >= 1e-10
        if changed:
            self.interaction_started.emit()
        self.setValue(clamped_val)
        if changed:
            self.interaction_ended.emit()

    def _cancel_edit(self):
        if not self._editing:
            return
        self._end_edit()
        self._update_display()

    def eventFilter(self, obj, event: QEvent) -> bool:
        if obj is self._edit and event.type() == QEvent.Type.KeyPress:
            key = event.key()
            if key == Qt.Key.Key_Escape:
                self._cancel_edit()
                return True
            if key in (Qt.Key.Key_Enter, Qt.Key.Key_Return):
                self._commit_edit()
                return True
        # 编辑态下，检测 _edit 外部的鼠标点击 → 主动提交
        if self._editing and obj is not self._edit:
            if event.type() in (QEvent.Type.MouseButtonPress,
                                QEvent.Type.MouseButtonDblClick):
                # 判断点击是否发生在 _edit 内部
                if isinstance(obj, QWidget):
                    global_pos = event.globalPosition().toPoint() \
                        if hasattr(event, 'globalPosition') else event.globalPos()
                    if not self._edit.geometry().contains(
                            self._edit.mapFromGlobal(global_pos)):
                        self._commit_edit()
                        return False  # 不吞掉事件，让目标控件正常处理
        return super().eventFilter(obj, event)

    # ---- 鼠标事件 ----

    def _drag_sensitivity(self, mods) -> float:
        """拖拽时每像素的数值变化量，按修饰键分三档。

        委托给纯函数 ``drag_speed_per_pixel``：固定 200px 参考宽度，
        换算不受控件实际宽度、DPR 或 offscreen 坐标影响。
        """
        return drag_speed_per_pixel(self._max - self._min, mods)

    def mousePressEvent(self, event: QMouseEvent):
        if self._editing:
            # 编辑态下由 line edit 自己处理，这里不拦截
            super().mousePressEvent(event)
            return
        if event.button() == Qt.MouseButton.MiddleButton:
            self.setValue(self._default)
            return
        if event.button() == Qt.MouseButton.LeftButton:
            self._dragging = True
            self._drag_engaged = False
            self._drag_start_x = event.position().x()
            self._drag_start_value = self._value
            self._value_label.setStyleSheet(DRAG_VALUE_ACTIVE_STYLE)
            event.accept()

    def mouseMoveEvent(self, event: QMouseEvent):
        if not self._dragging:
            return
        dx = event.position().x() - self._drag_start_x
        if not self._drag_engaged:
            if abs(dx) < _DRAG_THRESHOLD:
                return  # 未越过阈值，视为点击，暂不改变数值
            self._drag_engaged = True
            self.interaction_started.emit()
        sensitivity = self._drag_sensitivity(event.modifiers())
        new_value = self._drag_start_value + dx * sensitivity
        self.setValue(new_value)
        event.accept()

    def mouseReleaseEvent(self, event: QMouseEvent):
        if event.button() == Qt.MouseButton.LeftButton and self._dragging:
            was_engaged = self._drag_engaged
            self._dragging = False
            self._drag_engaged = False
            if not self._editing:
                self._value_label.setStyleSheet(DRAG_VALUE_NORMAL_STYLE)
            if was_engaged:
                self.interaction_ended.emit()
            event.accept()

    def mouseDoubleClickEvent(self, event: QMouseEvent):
        if event.button() == Qt.MouseButton.LeftButton:
            self._begin_edit()
            event.accept()

    def wheelEvent(self, event):
        step = self._single_step
        mods = event.modifiers()
        if mods & Qt.KeyboardModifier.ShiftModifier:
            step *= _LARGE_SPEED / _DEFAULT_SPEED   # Shift = 大范围 / 粗调（×10）
        elif mods & (Qt.KeyboardModifier.ControlModifier
                     | Qt.KeyboardModifier.MetaModifier):
            step *= _FINE_SPEED / _DEFAULT_SPEED    # Ctrl / ⌘ = 微调（×0.1）
        delta = event.angleDelta().y()
        before = self._value
        if delta > 0:
            self.setValue(self._value + step)
        elif delta < 0:
            self.setValue(self._value - step)
        if self._value != before:
            # A wheel notch is one complete interaction, just like committing
            # an inline edit.  Consumers can therefore create one undo item
            # rather than one for every low-level valueChanged signal.
            self.interaction_started.emit()
            self.interaction_ended.emit()
        event.accept()
