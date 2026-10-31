"""
Audio Fader Widget - 完全复刻 Kdenlive AudioSlider

实现专业的音频推子控件，包含：
- 垂直/水平方向支持
- 窄/宽滑块样式
- dB 刻度标签
- 中性位置标记（0dB）
- 自定义标签格式化
- 鼠标滚轮支持
- 右键重置到中性位置
"""

from typing import Optional, Callable
from PyQt6.QtWidgets import QWidget, QAbstractSlider
from PyQt6.QtCore import Qt, pyqtSignal, QSize, QRectF, QPointF, QSizeF, QEvent, QTimer
from PyQt6.QtGui import (
    QColor, QPainter, QPen, QLinearGradient,
    QFont, QFontMetrics, QPixmap, QCursor
)


class AudioFader(QAbstractSlider):
    """专业音频推子，复刻 Kdenlive AudioSlider"""

    slider_pressed = pyqtSignal()
    slider_released = pyqtSignal()

    # 常量
    KNOB_WIDTH = 14
    NARROW_KNOB_HEIGHT = 7
    WIDE_KNOB_HEIGHT = 31
    TICK_WIDTH = 8
    TICK_HEIGHT = 3
    TICK_LABELS_GROOVE_MARGIN = 0
    DIVIDER_LINE_GAP = 2
    # AUD-37602: 滚轮/键盘合成会话的释放去抖窗口（ms）。窗口内的连续增量
    # 合并为一次 slider_pressed → valueChanged* → slider_released 会话，
    # 消费端据此折叠为单条提交命令。
    SYNTH_RELEASE_MS = 350

    def __init__(
        self,
        orientation: Qt.Orientation = Qt.Orientation.Vertical,
        parent: Optional[QWidget] = None,
        narrow_knob: bool = False,
        neutral_position: int = 8000  # 默认 0dB 位置（0-10000 范围，IEC 对齐映射下 0dB=8000）
    ) -> None:
        super().__init__(parent)
        self._narrow_knob = narrow_knob
        self._neutral_position = neutral_position
        self._tick_positions: list[float] = []
        self._ticks_visible = True
        self._tick_labels_visible = True
        self._max_label_width = 0
        self._knob_hovered = False
        self._knob_focused = False
        self._label_formatter: Optional[Callable[[float], str]] = None
        self._value_to_slider: Optional[Callable[[float], int]] = None
        self._drag_start_pos: Optional[QPointF] = None

        self.setOrientation(orientation)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        self.setMouseTracking(True)

        # 设置字体 - 使用系统最小可读字体
        font = QFont()
        # 使用明确像素字号，避免不同平台的默认点大小过小而丢失字形下半部。
        # 注意：widget 字体参与宽度/高度计算（_max_label_width、_label_height），
        # 改它会连锁影响推子总宽度 → 破坏 strip 布局。绘制标签时改用 _label_font。
        font.setPixelSize(9)
        self.setFont(font)
        self._label_height = QFontMetrics(font).height()
        # 仅用于绘制刻度数字的小字体，不影响宽度/高度计算，避免布局连锁变动。
        # 当前 9px：与 widget 字体同号（用户要求"再大两号"从 7px），但布局仍按
        # widget 字体度量算，推子宽度不变。
        self._label_font = QFont(font)
        self._label_font.setPixelSize(9)

        # 默认范围 0-10000，默认值 8000（0dB，IEC 对齐映射下 unity=8000）
        # B1: 步进专业化 —— 72dB 跨度 / 10000 单位 ≈ 0.0072 dB/unit。
        #   singleStep=40 ≈ 0.3 dB（普通滚轮一格，符合专业混音器手感）
        #   pageStep=160 ≈ 1.2 dB（Shift+滚轮，跨档适中）
        #   Ctrl+滚轮在 wheelEvent 内动态降到 ~7 ≈ 0.05 dB（精细微调）
        self.setRange(0, 10000)
        self.setValue(8000)
        self.setSingleStep(40)
        self.setPageStep(160)

        self._update_size()
        self._background_cache: Optional[QPixmap] = None
        self._background_cache_logical_size: QSize = QSize(0, 0)
        # B2: 拖动时鼠标相对 knob 中心的偏移，用于精确跟随
        self._drag_grab_offset: float = 0.0
        # B7: 左键按下中标志（供 focusOutEvent 兜底补发 slider_released）
        self._left_button_pressed: bool = False
        # AUD-37602: 提交通道去污染状态机。
        #   _press_pending    —— 左键已按下但值尚未变化（惰性 press：值不变
        #                        则整轮按下/释放不发任何信号，消费端不会
        #                        因"原地点击 knob"提交一条无效果命令）
        #   _mouse_interaction —— 本轮按下已实际发射 slider_pressed（首次值
        #                        变化时物化），释放时据此补发 slider_released
        #   _synth_session    —— 滚轮/键盘合成会话进行中（去抖窗口内合并
        #                        多次增量为一次提交）
        self._press_pending: bool = False
        self._mouse_interaction: bool = False
        self._synth_session: bool = False
        self._kbd_arming: bool = False
        self._synth_timer: Optional[QTimer] = None
        # 内部 valueChanged 钩子：惰性物化 press / 合成滚轮键盘会话
        self.valueChanged.connect(self._on_internal_value_changed)

    # ========== 公共接口 ==========

    def set_tick_positions(self, tick_positions: list[float]) -> None:
        """设置刻度位置（dB 值）"""
        self._tick_positions = tick_positions
        if tick_positions:
            self._max_label_width = self._calculate_max_label_width(tick_positions)
            if self._tick_labels_visible and self.orientation() == Qt.Orientation.Vertical:
                width = self.KNOB_WIDTH + self._max_label_width + self.TICK_LABELS_GROOVE_MARGIN
                self.setMinimumWidth(width)
                self.setFixedWidth(width)
        self._update_background_cache()
        self.update()

    def set_label_formatter(self, formatter: Callable[[float], str]) -> None:
        """设置标签格式化函数"""
        self._label_formatter = formatter
        self._update_background_cache()
        self.update()

    def set_value_to_slider_function(self, func: Callable[[float], int]) -> None:
        """设置 dB 值到滑块位置的转换函数"""
        self._value_to_slider = func
        self._update_background_cache()
        self.update()

    def set_ticks_visible(self, visible: bool) -> None:
        self._ticks_visible = visible
        self._update_background_cache()
        self.update()

    def set_tick_labels_visible(self, visible: bool) -> None:
        self._tick_labels_visible = visible
        self._update_size()
        self._update_background_cache()
        self.update()

    def set_neutral_position(self, value: int) -> None:
        self._neutral_position = value
        self.update()

    def set_narrow_knob(self, narrow: bool) -> None:
        self._narrow_knob = narrow
        self._update_background_cache()
        self.update()

    # ========== 事件处理 ==========

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        # 绘制背景缓存
        if self._background_cache is None or self._background_cache_logical_size != self.size():
            self._update_background_cache()
        if self._background_cache:
            painter.drawPixmap(0, 0, self._background_cache)

        # 绘制滑块
        knob_pos = self._position_from_value(self.value())
        knob = self._get_knob_rect(knob_pos)
        self._draw_knob(painter, knob)

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            event.accept()
            if not self.isEnabled():
                return
            # AUD-37602: 鼠标接管前冲刷滚轮/键盘合成会话的未决提交
            self._end_synth_session()
            # B7: 标记左键按下中（供 focusOutEvent 兜底补发 slider_released）
            self._left_button_pressed = True
            # AUD-37602: 惰性 press —— 暂不发射 slider_pressed，待本轮按下
            # 首次发生值变化时由 _on_internal_value_changed 物化。值不变的
            # 点击（原地点击 knob）整轮不发信号，不再污染提交/撤销栈。
            self._press_pending = True
            # B3: 若该 press 是双击的第一击（即将引发 doubleClick），跳过跳值，
            # 避免双击重置前产生中间值变更（value 抖动 + preview/commit 抖动）。
            # Qt 在双击的两次 press 上都会设置 MouseEventCreatedDoubleClick 标志。
            if event.flags() & Qt.MouseEventFlag.MouseEventCreatedDoubleClick:
                self._drag_start_pos = event.position()
                return
            self._drag_start_pos = event.position()
            # B2: 点击落在 knob 上时只进入拖动模式，不跳值（专业混音器约定）。
            # 点击 knob 外的槽道空白才跳到点击位置（QSlider 行为）。
            knob_pos = self._position_from_value(self.value())
            knob_rect = self._get_knob_rect(knob_pos)
            if not knob_rect.contains(event.position()):
                pos = event.position().y() if self.orientation() == Qt.Orientation.Vertical else event.position().x()
                self.setValue(self._value_from_position(pos))
            # 记录拖动起始时的鼠标位置与 knob 中心的偏移，供 mouseMoveEvent 精确跟随
            if self.orientation() == Qt.Orientation.Vertical:
                self._drag_grab_offset = event.position().y() - knob_pos
            else:
                self._drag_grab_offset = event.position().x() - knob_pos
        elif event.button() in (Qt.MouseButton.RightButton, Qt.MouseButton.MiddleButton):
            event.accept()
            if not self.isEnabled():
                return
            # 值未处于中性位时 setValue 才发 valueChanged（非拖动态 → 直接提交
            # 一次）；已在中性位时无信号，不产生空命令。
            self.setValue(self._neutral_position)
        else:
            super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        if event.buttons() & Qt.MouseButton.LeftButton:
            event.accept()
            if not self.isEnabled():
                return
            # B2: 用 grab offset 让 knob 精确跟随鼠标（保持按下时的相对偏移），
            # 而非鼠标坐标直接映射到 knob 中心。拖动手感更自然，无突跳。
            raw = event.position().y() if self.orientation() == Qt.Orientation.Vertical else event.position().x()
            pos = raw - self._drag_grab_offset
            self.setValue(self._value_from_position(pos))
            self.sliderMoved.emit(self.value())
        else:
            super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._left_button_pressed = False
            # AUD-37602: 仅当本轮按下实际发射过 slider_pressed（即值发生过
            # 变化）才补发 slider_released；未变化的点击不发信号，消费端
            # 的拖动状态从未激活（其 active 守卫天然 no-op），不产生空命令。
            if self._mouse_interaction:
                self._mouse_interaction = False
                self.slider_released.emit()
            self._press_pending = False
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event) -> None:
        """双击重置到中性位置（0 dB / 中间 balance）。

        专业混音器标准交互：双击推子立即回到默认值。
        AUD-37602: 仅在值实际跳变时才补 press/release 信号对；已处于
        中性位时无双击命令（此前会发射一对空 press/release → 空提交）。
        """
        if event.button() == Qt.MouseButton.LeftButton:
            event.accept()
            if not self.isEnabled():
                return
            if self.value() == self._neutral_position:
                # 已在中性位：无值变化，不发任何信号（不产生空提交）
                return
            # AUD-37602: 先进入交互态再跳值 —— 值变化走消费端的拖动/预览
            # 路径，release 时单次提交；无前置 press 的程序化触发场景
            # 在此补一对完整 press/release。
            self._press_pending = False
            if not self._mouse_interaction:
                self._mouse_interaction = True
                self.slider_pressed.emit()
            self.setValue(self._neutral_position)
            self._mouse_interaction = False
            self.slider_released.emit()
        else:
            super().mouseDoubleClickEvent(event)

    def focusInEvent(self, event) -> None:
        super().focusInEvent(event)
        if not self._knob_focused:
            self._knob_focused = True
            self.update()

    def focusOutEvent(self, event) -> None:
        super().focusOutEvent(event)
        if self._knob_focused:
            self._knob_focused = False
            self.update()
        # B7: 失焦兜底 —— 拖动中若焦点被抢（切窗口/弹 modal），mouseReleaseEvent
        # 可能不触发，导致上层 _volume_dragging 泄漏、后续 valueChanged 一直走 preview。
        # 这里在失焦时补发 slider_released，上层 _on_volume_released 有 active 守卫，
        # 正常 release 后再触发是 no-op。
        if getattr(self, '_left_button_pressed', False):
            self._left_button_pressed = False
            self._press_pending = False
            # AUD-37602: 未物化的 press（值未变化）无需补发 release ——
            # 消费端拖动状态从未激活。
            if self._mouse_interaction:
                self._mouse_interaction = False
                self.slider_released.emit()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._update_background_cache()

    def event(self, event) -> bool:
        if self.isEnabled() and event.type() in (
            QEvent.Type.HoverMove,
            QEvent.Type.HoverEnter,
            QEvent.Type.HoverLeave
        ):
            mouse_pos = QPointF(self.mapFromGlobal(QCursor.pos()))
            knob_pos = self._position_from_value(self.value())
            knob_rect = self._get_knob_rect(knob_pos)
            knob_hovered = knob_rect.contains(mouse_pos)
            if knob_hovered != self._knob_hovered:
                self._knob_hovered = knob_hovered
                self.update()
        return super().event(event)

    def sliderChange(self, change: QAbstractSlider.SliderChange) -> None:
        if change == QAbstractSlider.SliderChange.SliderValueChange:
            min_val = self.minimum()
            max_val = self.maximum()
            if not self.isVisible() or self.width() <= 0 or self.height() <= 0:
                super().sliderChange(change)
                return
            current_value = self.value()
            new_value = max(min_val, min(current_value, max_val))
            if new_value != current_value:
                self.blockSignals(True)
                super().sliderChange(change)
                self.setValue(new_value)
                self.blockSignals(False)
                return
        super().sliderChange(change)

    def changeEvent(self, event) -> None:
        if event.type() in (QEvent.Type.EnabledChange, QEvent.Type.PaletteChange):
            self._update_background_cache()
        super().changeEvent(event)

    def keyPressEvent(self, event) -> None:
        """B5: 推子键盘 nudge —— 方向键微调，Ctrl 精细，Shift 大步，Home/End 跳极值。

        专业混音器约定：
          Left/Down  → -singleStep（≈-0.3 dB）
          Right/Up   → +singleStep（≈+0.3 dB）
          PgDown/PgUp → ∓pageStep（≈∓1.2 dB）
          Ctrl+方向键 → 精细（≈0.05 dB）
          Home       → 最大值（+6 dB）
          End        → 最小值（-60 dB）
        """
        if not self.isEnabled():
            super().keyPressEvent(event)
            return

        key = event.key()
        mods = event.modifiers()

        # Home/End 跳极值
        if key == Qt.Key.Key_Home:
            self._apply_discrete_value(self.maximum(), event)
            return
        if key == Qt.Key.Key_End:
            self._apply_discrete_value(self.minimum(), event)
            return

        # 方向键 / PgUp PgDown（QAbstractSlider 默认已处理，这里加 Ctrl 精细覆盖）
        if key in (Qt.Key.Key_Left, Qt.Key.Key_Right, Qt.Key.Key_Up, Qt.Key.Key_Down,
                   Qt.Key.Key_PageUp, Qt.Key.Key_PageDown):
            if mods & Qt.KeyboardModifier.ControlModifier:
                # Ctrl 精细：覆盖默认步进为 ~0.05 dB
                fine_step = 7
                if key in (Qt.Key.Key_Left, Qt.Key.Key_Down):
                    self._apply_discrete_value(self.value() - fine_step, event)
                elif key in (Qt.Key.Key_Right, Qt.Key.Key_Up):
                    self._apply_discrete_value(self.value() + fine_step, event)
                elif key == Qt.Key.Key_PageDown:
                    self._apply_discrete_value(self.value() - fine_step * 4, event)
                else:  # Ctrl+PageUp
                    self._apply_discrete_value(self.value() + fine_step * 4, event)
                return
            # 非 Ctrl：交给 QAbstractSlider 默认处理（用 singleStep/pageStep）。
            # AUD-37602: 委托窗口内发生的 valueChanged 由内部钩子合成会话，
            # 键按住不放的连续 repeat 合并为一次提交（每次续期去抖窗口）。
            self._kbd_arming = True
            try:
                super().keyPressEvent(event)
            finally:
                self._kbd_arming = False
            return
        super().keyPressEvent(event)

    def wheelEvent(self, event) -> None:
        if not self.isEnabled():
            return
        delta = event.angleDelta().y()
        mods = event.modifiers()
        # B1: 步进专业化
        #   Ctrl → 精细微调（~0.05 dB/格），优先级最高
        #   Shift → 大步进（pageStep ≈ 1.2 dB/格）
        #   默认 → singleStep ≈ 0.3 dB/格
        if mods & Qt.KeyboardModifier.ControlModifier:
            step = 7   # ≈ 0.05 dB
        elif mods & Qt.KeyboardModifier.ShiftModifier:
            step = self.pageStep()
        else:
            step = self.singleStep()
        raw_value = self.value() + step if delta > 0 else self.value() - step
        # AUD-37602: 离散增量统一走合成会话 —— 去抖窗口内的连续滚轮合并为
        # 一次 press→(preview*)→release（单条提交命令）；边界处无实际
        # 变化的滚轮不产生任何信号（旧实现每次 valueChanged 都直接提交，
        # 滚 20 格 = 20 条撤销命令）。
        self._apply_discrete_value(raw_value, event)

    # ========== 私有方法 ==========

    # ── AUD-37602: 提交去污染状态机 ──

    def _on_internal_value_changed(self, _value: int) -> None:
        """内部 valueChanged 钩子：惰性物化按下 / 合成滚轮键盘会话。"""
        if self._press_pending:
            # 本轮按下内首次值变化 → 物化 slider_pressed（此前未发射）
            self._press_pending = False
            self._mouse_interaction = True
            self.slider_pressed.emit()
            return
        if self._kbd_arming:
            # super().keyPressEvent 默认步进（方向键/PgUp/PgDn）触发的变更
            # → 纳入合成会话（键 repeat 持续续期去抖窗口）
            if not self._synth_session:
                self._start_synth_session()
            else:
                self._restart_synth_timer()

    def _apply_discrete_value(self, new_value: int, event) -> None:
        """离散增量（滚轮一格 / Home/End / Ctrl+方向键）统一入口。

        无实际变化时不产生任何信号；有变化时纳入合成会话（去抖窗口内的
        连续增量合并为一次 press→(valueChanged*)→release 提交）。
        """
        new_value = max(self.minimum(), min(int(new_value), self.maximum()))
        if new_value == self.value():
            event.accept()
            return
        if not self._synth_session:
            self._start_synth_session()
        else:
            self._restart_synth_timer()
        self.setValue(new_value)
        event.accept()

    def _start_synth_session(self) -> None:
        """开启滚轮/键盘合成会话（等效一次按下，消费端进入预览态）。"""
        self._synth_session = True
        self._restart_synth_timer()
        self.slider_pressed.emit()

    def _restart_synth_timer(self) -> None:
        """（重新）启动合成会话的释放去抖定时器。"""
        if self._synth_timer is None:
            timer = QTimer(self)
            timer.setSingleShot(True)
            timer.timeout.connect(self._on_synth_timer)
            self._synth_timer = timer
        self._synth_timer.start(self.SYNTH_RELEASE_MS)

    def _on_synth_timer(self) -> None:
        """去抖窗口到期：结束合成会话，补发一次 slider_released（单次提交）。"""
        if self._synth_session:
            self._synth_session = False
            self.slider_released.emit()

    def _end_synth_session(self) -> None:
        """立即结束合成会话（鼠标接管前冲刷未决提交）。"""
        if self._synth_timer is not None:
            self._synth_timer.stop()
        if self._synth_session:
            self._synth_session = False
            self.slider_released.emit()

    def _update_size(self) -> None:
        knob_size = self._calculate_knob_size()
        self.setMinimumWidth(round(knob_size.width()))
        self.setMinimumHeight(round(knob_size.height()))
        if self.orientation() == Qt.Orientation.Vertical:
            self.setFixedWidth(round(knob_size.width()))
        else:
            self.setFixedHeight(round(knob_size.height()))

    def _update_background_cache(self) -> None:
        size = self.size()
        if not size.isValid() or size.width() <= 0 or size.height() <= 0:
            return
        self._background_cache_logical_size = size

        # HiDPI 支持
        scaling_factor = self.devicePixelRatioF()
        scaled_size = QSize(round(size.width() * scaling_factor), round(size.height() * scaling_factor))

        self._background_cache = QPixmap(scaled_size)
        self._background_cache.setDevicePixelRatio(scaling_factor)
        self._background_cache.fill(Qt.GlobalColor.transparent)

        painter = QPainter(self._background_cache)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        groove = self._get_groove_rect(QRectF(self.rect()))
        ticks = self._get_tick_rect(groove)
        knob_pos = self._position_from_value(self.value())
        knob = self._get_knob_rect(knob_pos)
        labels = self._get_tick_label_rect(ticks, knob)

        self._draw_groove(painter, groove)
        if self._ticks_visible:
            self._draw_ticks(painter, ticks)
        if self._tick_labels_visible:
            self._draw_labels(painter, labels, ticks)

    def _draw_groove(self, painter: QPainter, groove_rect: QRectF) -> None:
        # 达芬奇式 2px 极窄轨线
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        if self.orientation() == Qt.Orientation.Vertical:
            cx = groove_rect.center().x()
            track_rect = QRectF(cx - 1.0, groove_rect.top(), 2.0, groove_rect.height())
            painter.fillRect(track_rect, QColor("#1a1a1e"))
        else:
            cy = groove_rect.center().y()
            track_rect = QRectF(groove_rect.left(), cy - 1.0, groove_rect.width(), 2.0)
            painter.fillRect(track_rect, QColor("#1a1a1e"))
        painter.restore()

    def _draw_ticks(self, painter: QPainter, tick_rect: QRectF) -> None:
        if not self._tick_positions:
            return

        text_color = QColor("#555560")
        tick_size = self._calculate_tick_size()

        min_val = self.minimum()
        max_val = self.maximum()
        range_val = max_val - min_val
        if range_val <= 0:
            return

        value_to_slider = self._value_to_slider or (lambda v: int(v))

        if self.orientation() == Qt.Orientation.Vertical:
            tick_start_x = tick_rect.left() + (tick_rect.width() - tick_size.width()) / 2.0
            for tick_value in self._tick_positions:
                slider_value = value_to_slider(tick_value)
                ratio = (slider_value - min_val) / range_val
                y = tick_rect.bottom() - (tick_rect.height() - 1.0) * ratio

                painter.save()
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(text_color)
                painter.drawRect(QRectF(tick_start_x, y - 0.5, tick_size.width(), 1.0))
                painter.restore()

    def _draw_labels(self, painter: QPainter, label_rect: QRectF, tick_rect: QRectF) -> None:
        if not self._tick_labels_visible or not self._tick_positions:
            return

        # 用 _label_font 绘制（比 widget 字体小两号），但度量仍用 widget 字体
        # 保持原有布局间距和推子总宽度不变，仅数字字形变小。
        draw_font = self._label_font if self._label_font is not None else self.font()
        painter.setFont(draw_font)
        text_color = QColor("#888890")
        painter.setOpacity(0.7)
        painter.setPen(text_color if self.isEnabled() else QColor("#555560"))

        min_val = self.minimum()
        max_val = self.maximum()
        range_val = max_val - min_val
        if range_val <= 0:
            return

        value_to_slider = self._value_to_slider or (lambda v: int(v))
        label_formatter = self._label_formatter or (lambda v: str(v))
        label_margin = 2.0

        if self.orientation() == Qt.Orientation.Vertical:
            prev_y = -1.0
            for tick_value in self._tick_positions:
                slider_value = value_to_slider(tick_value)
                ratio = (slider_value - min_val) / range_val
                y = tick_rect.bottom() - (tick_rect.height() - 1.0) * ratio
                y -= self._label_height / 2.0

                if y + self._label_height > self.height():
                    y = self.height() - self._label_height
                elif y < 0:
                    y = 0

                if prev_y < 0 or (prev_y - self._label_height) - y > label_margin:
                    label = label_formatter(tick_value)
                    # 用 QRectF + AlignRight|AlignVCenter 绘制：直接用 label_rect
                    # 做水平右对齐范围，用 _label_height 高度的槽位做垂直居中。
                    # 完全避免手动算 baseline 出错，小字稳定落在原标签列内。
                    text_rect = QRectF(
                        label_rect.left(),
                        y,
                        label_rect.width(),
                        self._label_height,
                    )
                    painter.drawText(
                        text_rect,
                        Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                        label,
                    )
                    prev_y = y

    def _draw_knob(self, painter: QPainter, rect: QRectF) -> None:
        highlight = self.value() != self._neutral_position or self._knob_hovered or self._knob_focused

        if self.orientation() == Qt.Orientation.Horizontal:
            painter.save()
            center = rect.center()
            painter.translate(center)
            painter.rotate(90)
            painter.translate(-center)

            rotated_size = QSizeF(round(rect.height()), round(rect.width()))
            rotated_top_left = QPointF(center.x() - rotated_size.width() / 2.0, center.y() - rotated_size.height() / 2.0)
            rotated_rect = QRectF(rotated_top_left, rotated_size)

            if self._narrow_knob:
                self._draw_narrow_knob(painter, rotated_rect, highlight)
            else:
                self._draw_wide_knob(painter, rotated_rect, highlight)
            painter.restore()
        else:
            if self._narrow_knob:
                self._draw_narrow_knob(painter, rect, highlight)
            else:
                self._draw_wide_knob(painter, rect, highlight)

    def _draw_narrow_knob(self, painter: QPainter, knob_rect: QRectF, highlight: bool) -> None:
        main_color = QColor("#3a3a42") if self.isEnabled() else QColor("#282830")
        outline_color = QColor("#5a5a66") if highlight else QColor("#282830")
        divider_color = QColor("#e0e0e0") if highlight else QColor("#888890")

        border_pen = QPen(outline_color)
        border_pen.setWidthF(1.0)

        painter.setPen(border_pen)
        painter.setBrush(main_color)
        painter.drawRoundedRect(knob_rect, 1.5, 1.5)

        # 中心指示线
        divider_y = knob_rect.top() + self.NARROW_KNOB_HEIGHT / 2.0
        divider_pen = QPen(divider_color)
        divider_pen.setWidthF(1.0)
        painter.setPen(divider_pen)
        painter.drawLine(
            int(knob_rect.left() + 2), int(divider_y),
            int(knob_rect.right() - 2), int(divider_y)
        )

    def _draw_wide_knob(self, painter: QPainter, knob_rect: QRectF, highlight: bool) -> None:
        # 达芬奇精致暗灰拉丝金属推子帽
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        # 背景与渐变
        grad = QLinearGradient(knob_rect.left(), knob_rect.top(), knob_rect.right(), knob_rect.top())
        if highlight:
            grad.setColorAt(0.0, QColor("#4c4c56"))
            grad.setColorAt(0.5, QColor("#383842"))
            grad.setColorAt(1.0, QColor("#4c4c56"))
            border_color = QColor("#00d2c1") if self._knob_focused else QColor("#666675")
        else:
            grad.setColorAt(0.0, QColor("#3c3c44"))
            grad.setColorAt(0.5, QColor("#2c2c34"))
            grad.setColorAt(1.0, QColor("#3c3c44"))
            border_color = QColor("#24242a")

        painter.setPen(QPen(border_color, 1.0))
        painter.setBrush(grad)
        painter.drawRoundedRect(knob_rect, 2.0, 2.0)

        # 中心白色刻度指示线
        mid_y = knob_rect.center().y()
        painter.setPen(QPen(QColor("#ffffff"), 1.5))
        painter.drawLine(
            int(knob_rect.left() + 2), int(mid_y),
            int(knob_rect.right() - 2), int(mid_y)
        )
        painter.restore()

    def _calculate_knob_size(self) -> QSizeF:
        if self.orientation() == Qt.Orientation.Vertical:
            return QSizeF(self.KNOB_WIDTH, self.NARROW_KNOB_HEIGHT if self._narrow_knob else self.WIDE_KNOB_HEIGHT)
        else:
            return QSizeF(self.NARROW_KNOB_HEIGHT if self._narrow_knob else self.WIDE_KNOB_HEIGHT, self.KNOB_WIDTH)

    def _calculate_tick_size(self) -> QSizeF:
        if self.orientation() == Qt.Orientation.Vertical:
            return QSizeF(self.TICK_WIDTH, self.TICK_HEIGHT)
        else:
            return QSizeF(self.TICK_HEIGHT, self.TICK_WIDTH)

    def _get_knob_rect(self, pos: float) -> QRectF:
        knob_size = self._calculate_knob_size()
        groove = self._get_groove_rect(QRectF(self.rect()))
        if self.orientation() == Qt.Orientation.Vertical:
            x = groove.left() + (groove.width() - knob_size.width()) / 2.0
            y = pos - knob_size.height() / 2.0
            return QRectF(x, y, knob_size.width(), knob_size.height())
        else:
            y = groove.top() + (groove.height() - knob_size.height()) / 2.0
            x = pos - knob_size.width() / 2.0
            return QRectF(x, y, knob_size.width(), knob_size.height())

    def _get_groove_rect(self, widget_rect: QRectF) -> QRectF:
        groove_width = 6.0
        if self.orientation() == Qt.Orientation.Vertical:
            right_margin = (max(self._calculate_knob_size().width(), self.TICK_WIDTH if self._ticks_visible else 0.0) - groove_width) / 2.0
            left_margin = widget_rect.right() - right_margin - groove_width - 5.0  # 整体左移 5px 靠近波形图
            return QRectF(left_margin, 0, groove_width, widget_rect.height())
        else:
            top_margin = (max(self._calculate_knob_size().height(), self.TICK_WIDTH if self._ticks_visible else 0.0) - groove_width) / 2.0
            return QRectF(0, top_margin, widget_rect.width(), groove_width)

    def _get_tick_rect(self, groove_rect: QRectF) -> QRectF:
        knob_size = self._calculate_knob_size()
        if self.orientation() == Qt.Orientation.Vertical:
            x = groove_rect.left() + (groove_rect.width() - self.TICK_WIDTH) / 2.0
            y = groove_rect.top() + knob_size.height() / 2.0
            height = groove_rect.height() - knob_size.height()
            return QRectF(x, y, self.TICK_WIDTH, height)
        else:
            y = groove_rect.top() + (groove_rect.height() - self.TICK_WIDTH) / 2.0
            x = groove_rect.left() + knob_size.width() / 2.0
            width = groove_rect.width() - knob_size.width()
            return QRectF(x, y, width, self.TICK_WIDTH)

    def _get_tick_label_rect(self, tick_rect: QRectF, knob_rect: QRectF) -> QRectF:
        if not self._tick_labels_visible:
            return QRectF()
        if self.orientation() == Qt.Orientation.Vertical:
            left_of_ticks = tick_rect.left()
            left_of_knob = knob_rect.left()
            left = min(left_of_ticks, left_of_knob) - self.TICK_LABELS_GROOVE_MARGIN - self._max_label_width
            return QRectF(left, tick_rect.top(), self._max_label_width, tick_rect.height())
        else:
            top = max(tick_rect.bottom(), knob_rect.bottom()) + self.TICK_LABELS_GROOVE_MARGIN
            return QRectF(tick_rect.left(), top, tick_rect.width(), self._label_height)

    def _position_from_value(self, value: int) -> float:
        min_val = self.minimum()
        max_val = self.maximum()
        range_val = max_val - min_val
        if range_val <= 0:
            return 0.0

        groove = self._get_groove_rect(QRectF(self.rect()))
        if self.orientation() == Qt.Orientation.Vertical:
            top = groove.top() + self._calculate_knob_size().height() / 2.0
            bottom = groove.bottom() - self._calculate_knob_size().height() / 2.0
            return bottom - (bottom - top) * (value - min_val) / range_val
        else:
            left = groove.left() + self._calculate_knob_size().width() / 2.0
            right = groove.right() - self._calculate_knob_size().width() / 2.0
            return left + (right - left) * (value - min_val) / range_val

    def _value_from_position(self, pos: float) -> int:
        min_val = self.minimum()
        max_val = self.maximum()
        range_val = max_val - min_val
        if range_val <= 0:
            return min_val

        groove = self._get_groove_rect(QRectF(self.rect()))
        if self.orientation() == Qt.Orientation.Vertical:
            top = groove.top() + self._calculate_knob_size().height() / 2.0
            bottom = groove.bottom() - self._calculate_knob_size().height() / 2.0
            # AUD-37601: widget 高度被压到恰好等于 knob 高度时滑轨零长度，
            # 除零异常会从 mousePress/mouseMove 事件处理器抛出（PyQt6 按
            # qFatal 处理可致进程退出）。与 range 守卫同风格直接返回 min_val。
            if bottom <= top:
                return min_val
            ratio = (bottom - pos) / (bottom - top)
        else:
            left = groove.left() + self._calculate_knob_size().width() / 2.0
            right = groove.right() - self._calculate_knob_size().width() / 2.0
            # AUD-37601: 同上，横向滑轨零长度守卫
            if right <= left:
                return min_val
            ratio = (pos - left) / (right - left)

        ratio = max(0.0, min(1.0, ratio))
        return min_val + round(ratio * range_val)

    def _calculate_max_label_width(self, tick_positions: list[float]) -> int:
        if not tick_positions:
            return 0
        fm = QFontMetrics(self.font())
        max_w = 0
        formatter = self._label_formatter or (lambda v: str(v))
        for val in tick_positions:
            label = formatter(val)
            max_w = max(max_w, fm.horizontalAdvance(label))
        return max_w
