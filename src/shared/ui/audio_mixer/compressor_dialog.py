"""Compressor Dialog — 压缩器单实例编辑对话框（CW-A1 重构）。

FX 列表 / clip 适配器里的 compressor 实例点击时弹出。薄宿主：内嵌
dynamics_dialog.CompressorCard（7 旋钮 + Sidechain）+ Make Up 旋钮。

对接项目契约：
  - 构造：``(track_id, track_name, instance_id, initial_params, parent)``
  - 信号：``compressor_preview`` / ``compressor_committed`` (track_id, CompressorParams)
"""

from __future__ import annotations

from typing import Optional

from PyQt6.QtCore import pyqtSignal, Qt
from PyQt6.QtWidgets import QDialog, QHBoxLayout, QLabel, QVBoxLayout, QWidget

from src.features.edit.core.timeline_v2 import CompressorParams
from src.shared.i18n.i18n import I18N
from src.shared.ui.audio_mixer.dynamics_dialog import COLORS, CompressorCard
from src.shared.ui.audio_mixer.pro_widgets import (
    KnobParameterCell,
    apply_float_panel,
    ProVerticalMeter,
    PRO_DIALOG_QSS,
    parse_float_clean,
)


class CompressorDialog(QDialog):
    """压缩器参数编辑对话框（CompressorCard + Make Up）。"""

    compressor_preview = pyqtSignal(str, object)    # 拖动期实时听感
    compressor_committed = pyqtSignal(str, object)  # 松手/开关，进命令栈

    def __init__(
        self,
        track_id: str,
        track_name: str,
        instance_id: str,
        initial_params: Optional[CompressorParams] = None,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._track_id = track_id
        self._instance_id = instance_id
        self._current: CompressorParams = initial_params or CompressorParams.default()
        self._dragging = False
        self._updating = False

        self.setWindowTitle(
            I18N.tr("audio.fx.compressor", "Compressor") + f" - {track_name or track_id}")
        self.setModal(False)
        apply_float_panel(self)
        self.setMinimumSize(560, 330)
        self.setStyleSheet(PRO_DIALOG_QSS)

        lay = QHBoxLayout(self)
        lay.setContentsMargins(12, 12, 12, 12)
        lay.setSpacing(10)

        self.card = CompressorCard(self._current.to_dict())
        self.card.changed.connect(self._on_card_changed)
        self.card.interactionStarted.connect(self._on_drag_started)
        self.card.interactionEnded.connect(self._on_drag_released)
        lay.addWidget(self.card, 1)

        side = QVBoxLayout()
        side.addStretch()
        self.c_makeup = KnobParameterCell(
            I18N.tr("audio.fx.makeup", "Make Up"), 0.0, 24.0,
            self._current.makeup_gain, COLORS["cyan"], "0", "dB", "+24",
            lambda v: f"{v:.1f}", parse_float_clean, knob_size=42)
        self.c_makeup.valueChanged.connect(self._on_makeup_changed)
        self.c_makeup.interactionStarted.connect(self._on_drag_started)
        self.c_makeup.interactionEnded.connect(self._on_drag_released)
        side.addWidget(self.c_makeup)
        # 侧链检源电平表（引擎 sc 遥测驱动；sc_on 关闭时置灰）
        sc_lbl = QLabel(I18N.tr("audio.fx.sidechain", "Sidechain"))
        sc_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        sc_lbl.setStyleSheet("color: #9AA0B0; font-size: 10px; font-weight: 600;")
        side.addWidget(sc_lbl)
        self.m_sidechain = ProVerticalMeter(mode="level", show_ticks=False)
        self.m_sidechain.setFixedHeight(96)
        self.m_sidechain.setActive(self._current.sc_on)
        side.addWidget(self.m_sidechain, 0, Qt.AlignmentFlag.AlignHCenter)
        side.addStretch()
        lay.addLayout(side)

    def _emit(self) -> None:
        if self._updating:
            return
        if self._dragging:
            self.compressor_preview.emit(self._track_id, self._current)
        else:
            self.compressor_committed.emit(self._track_id, self._current)

    def _on_drag_started(self) -> None:
        self._dragging = True

    def _on_drag_released(self) -> None:
        if self._updating:
            return
        self._dragging = False
        self.compressor_committed.emit(self._track_id, self._current)

    def _on_card_changed(self, data: dict) -> None:
        data = dict(data)
        data["makeup_gain"] = self._current.makeup_gain
        try:
            self._current = CompressorParams.from_dict(data)
        except ValueError:
            return
        self.m_sidechain.setActive(self._current.sc_on)
        self._emit()

    def _on_makeup_changed(self, val: float) -> None:
        data = self._current.to_dict()
        data["makeup_gain"] = round(val, 1)
        self._current = CompressorParams.from_dict(data)
        self._emit()

    def set_sidechain_sources(self, sources: list) -> None:
        """更新侧链源列表 [(track_id, name), ...]。"""
        self.card.set_sidechain_sources(sources)

    def set_sidechain_level(self, level_db: float) -> None:
        """引擎侧链检源电平遥测回填（update_track_telemetry 调用）。"""
        self.m_sidechain.setValue(float(level_db))

    def set_current_params(self, params: CompressorParams) -> None:
        """外部静默回填（不回发信号）。"""
        self._updating = True
        self._current = params
        self.card.syncFromData(params.to_dict())
        self.c_makeup.setValue(params.makeup_gain)
        self.m_sidechain.setActive(params.sc_on)
        self._updating = False
