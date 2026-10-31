"""色调区间饱和度面板 - Lift/Gamma/Gain 各区间的饱和度偏置。

Tonal Saturation Panel - per-range saturation offsets for the
Lift/Gamma/Gain wheels.

与移动端 mega.frag 的 ``tonal_saturation_adjust_g`` 对齐的自研扩展
参数（不在 ASC CDL / 达芬奇 LGG 语义内）。每个区间 -100..100，
0 = 无效果。

布局上刻意做成独立 dock（默认与 Color Wheel 同组标签页），不挤占
GradingPanel 的达芬奇式排版；未来 Log 轮盘页签也挂在同组。

信号:
    tonal_saturation_changed: (lift_sat, gamma_sat, gain_sat) 改变时发射
    interaction_started / interaction_ended: 手势边界（撤销粒度对齐
    GradingPanel 的一次手势一次 history push）
"""

from typing import Optional

from PyQt6.QtCore import QSize, pyqtSignal
from PyQt6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout,
                             QGridLayout, QPushButton)

from src.shared.assets.icon_manager import icon_reset
from src.shared.ui.widgets.param_slider_row import ParamSliderRow
from src.shared.i18n.i18n import I18N


class TonalSaturationPanel(QWidget):
    """三个色调区间的饱和度偏置滑块（-100..100）。"""

    tonal_saturation_changed = pyqtSignal(float, float, float)
    interaction_started = pyqtSignal()
    interaction_ended = pyqtSignal()

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._syncing_ui = False

        layout = QVBoxLayout()
        layout.setContentsMargins(8, 2, 8, 2)
        layout.setSpacing(1)
        self.setLayout(layout)

        header_layout = QHBoxLayout()
        self.reset_all_btn = QPushButton()
        self.reset_all_btn.setObjectName("reset_btn")
        self.reset_all_btn.setIcon(icon_reset())
        self.reset_all_btn.setIconSize(QSize(12, 12))
        self.reset_all_btn.setFixedSize(16, 16)
        self.reset_all_btn.setToolTip(I18N.tr(
            "grading.tonal_saturation_reset", "重置全部色调区间饱和度"))
        self.reset_all_btn.clicked.connect(self.reset_all)
        header_layout.addWidget(self.reset_all_btn)
        header_layout.addStretch()
        layout.addLayout(header_layout)

        # 标签宽度按当前语言字体度量自适应（三列统一取最长者），
        # 简写标签通常落在 50-80px；极端语言封顶 170，tooltip 兜底。
        labels = [
            I18N.tr("grading.lift_sat", "暗部饱和"),
            I18N.tr("grading.gamma_sat", "中间饱和"),
            I18N.tr("grading.gain_sat", "亮部饱和"),
        ]
        metrics = self.fontMetrics()
        widest = max(metrics.horizontalAdvance(text) for text in labels)
        label_width = max(65, min(170, widest + 14))

        self.lift_slider = self._create_slider(labels[0], label_width)
        self.gamma_slider = self._create_slider(labels[1], label_width)
        self.gain_slider = self._create_slider(labels[2], label_width)

        # 一行三列（与色轮页主参数网格同款）；下方留白供未来追加
        # 更多功能参数。
        grid = QGridLayout()
        grid.setSpacing(1)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.addWidget(self.lift_slider, 0, 0)
        grid.addWidget(self.gamma_slider, 0, 1)
        grid.addWidget(self.gain_slider, 0, 2)
        layout.addLayout(grid)

        layout.addStretch(1)

        for s in (self.lift_slider, self.gamma_slider, self.gain_slider):
            s.valueChanged.connect(self._on_value_changed)
            s.interactionStarted.connect(self.interaction_started.emit)
            s.interactionEnded.connect(self._on_interaction_ended)

    @staticmethod
    def _create_slider(label: str, label_width: int) -> ParamSliderRow:
        row = ParamSliderRow(
            label, -100.0, 100.0, 0.0,
            step=0.5, decimals=1,
            label_width=label_width, slider_auto_hide=False)
        # 极端语言封顶后仍可能省略，tooltip 兜底全名
        # （与 GradingPanel 的 vibrance 同款处理）。
        row.label.setToolTip(label)
        return row

    # ------------------------------------------------------------------
    # 状态同步（引擎 → UI，单向）
    # ------------------------------------------------------------------

    def set_params(self, lift_sat: float, gamma_sat: float,
                   gain_sat: float) -> None:
        """回写引擎/工程值到滑块（不触发 tonal_saturation_changed）。

        blockSignals 双保险：除 _syncing_ui 软守卫外，把 row 内部
        DragValue/QSlider 的信号也物理静默——程序化 setValue 在某些
        状态下仍可能从组件内部路径触发 valueChanged/interactionEnded，
        不能依赖回调侧守卫的时序。
        """
        rows = (self.lift_slider, self.gamma_slider, self.gain_slider)
        self._syncing_ui = True
        for row in rows:
            row.blockSignals(True)
        try:
            self.lift_slider.setValue(float(lift_sat))
            self.gamma_slider.setValue(float(gamma_sat))
            self.gain_slider.setValue(float(gain_sat))
        finally:
            for row in rows:
                row.blockSignals(False)
            self._syncing_ui = False

    def reset_all(self) -> None:
        self.set_params(0.0, 0.0, 0.0)
        self._emit_changed()

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    def _on_value_changed(self, _value: float) -> None:
        if self._syncing_ui:
            return
        self._emit_changed()

    def _on_interaction_ended(self) -> None:
        # set_params 程序化同步期间 DragValue 也可能发 interaction_ended，
        # 与 _on_value_changed 一样须被 _syncing_ui 挡住，否则会把同步值
        # 当作用户输入回发（GradingPanel 滑块同款守卫）。
        if self._syncing_ui:
            return
        self._emit_changed()
        self.interaction_ended.emit()

    def _emit_changed(self) -> None:
        self.tonal_saturation_changed.emit(
            self.lift_slider.value(),
            self.gamma_slider.value(),
            self.gain_slider.value(),
        )
