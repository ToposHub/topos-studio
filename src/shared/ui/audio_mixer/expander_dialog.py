"""Expander Dialog — 扩展器/门限单实例编辑对话框（CW-A1 新增）。

FX 列表 / clip 适配器里的 expander 实例（以及由其无损承接的旧 noise_gate
实例）点击时弹出。薄宿主：内嵌 dynamics_dialog.ExpanderGateCard。

对接项目契约：
  - 构造：``(track_id, track_name, instance_id, initial_params, parent)``
  - 信号：``expander_preview`` / ``expander_committed`` (track_id, ExpanderParams)
"""

from __future__ import annotations

from typing import Optional

from PyQt6.QtCore import pyqtSignal
from PyQt6.QtWidgets import QDialog, QVBoxLayout, QWidget

from src.features.edit.core.timeline_v2 import ExpanderParams
from src.shared.i18n.i18n import I18N
from src.shared.ui.audio_mixer.dynamics_dialog import ExpanderGateCard
from src.shared.ui.audio_mixer.pro_widgets import PRO_DIALOG_QSS, apply_float_panel


class ExpanderDialog(QDialog):
    """扩展器/门限参数编辑对话框（ExpanderGateCard）。"""

    expander_preview = pyqtSignal(str, object)    # 拖动期实时听感
    expander_committed = pyqtSignal(str, object)  # 松手/开关/模式，进命令栈

    def __init__(
        self,
        track_id: str,
        track_name: str,
        instance_id: str,
        initial_params: Optional[ExpanderParams] = None,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._track_id = track_id
        self._instance_id = instance_id
        self._current: ExpanderParams = initial_params or ExpanderParams.default()
        self._dragging = False
        self._updating = False

        self.setWindowTitle(
            I18N.tr("audio.fx.expander_gate", "Expander / Gate")
            + f" - {track_name or track_id}")
        self.setModal(False)
        apply_float_panel(self)
        self.setMinimumSize(420, 330)
        self.setStyleSheet(PRO_DIALOG_QSS)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 12, 12, 12)

        self.card = ExpanderGateCard(self._current.to_dict())
        self.card.changed.connect(self._on_card_changed)
        self.card.interactionStarted.connect(self._on_drag_started)
        self.card.interactionEnded.connect(self._on_drag_released)
        lay.addWidget(self.card, 1)

    def _on_drag_started(self) -> None:
        self._dragging = True

    def _on_drag_released(self) -> None:
        if self._updating:
            return
        self._dragging = False
        self.expander_committed.emit(self._track_id, self._current)

    def _on_card_changed(self, data: dict) -> None:
        if self._updating:
            return
        try:
            self._current = ExpanderParams.from_dict(data)
        except ValueError:
            return
        if self._dragging:
            self.expander_preview.emit(self._track_id, self._current)
        else:
            self.expander_committed.emit(self._track_id, self._current)

    def set_current_params(self, params: ExpanderParams) -> None:
        """外部静默回填（不回发信号）。"""
        self._updating = True
        self._current = params
        self.card.syncFromData(params.to_dict())
        self._updating = False

    def reset(self) -> None:
        """重置为默认参数（保留开关状态），恰好一次 committed。"""
        was_enabled = self._current.enabled
        self._current = ExpanderParams(enabled=was_enabled)
        self.card.syncFromData(self._current.to_dict())
        self.expander_committed.emit(self._track_id, self._current)
