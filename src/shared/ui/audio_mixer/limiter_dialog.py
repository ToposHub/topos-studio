"""Limiter Dialog — 限幅器单实例编辑对话框（CW-A1 重构）。

FX 列表 / clip 适配器里的 limiter 实例点击时弹出。薄宿主：内嵌
dynamics_dialog.LimiterCard（threshold / attack / hold / release）。

对接项目契约：
  - 构造：``(track_id, track_name, instance_id, initial_params, parent)``
  - 信号：``limiter_preview`` / ``limiter_committed`` (track_id, LimiterParams)
"""

from __future__ import annotations

from typing import Optional

from PyQt6.QtCore import pyqtSignal
from PyQt6.QtWidgets import QDialog, QVBoxLayout, QWidget

from src.features.edit.core.timeline_v2 import LimiterParams
from src.shared.i18n.i18n import I18N
from src.shared.ui.audio_mixer.dynamics_dialog import LimiterCard
from src.shared.ui.audio_mixer.pro_widgets import PRO_DIALOG_QSS, apply_float_panel


class LimiterDialog(QDialog):
    """限幅器参数编辑对话框（LimiterCard）。"""

    limiter_preview = pyqtSignal(str, object)    # 拖动期实时听感
    limiter_committed = pyqtSignal(str, object)  # 松手/开关，进命令栈

    def __init__(
        self,
        track_id: str,
        track_name: str,
        instance_id: str,
        initial_params: Optional[LimiterParams] = None,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._track_id = track_id
        self._instance_id = instance_id  # 对应 TrackEffectInstance.instance_id
        self._current: LimiterParams = initial_params or LimiterParams.default()
        self._dragging = False
        self._updating = False

        self.setWindowTitle(
            I18N.tr("audio.fx.limiter", "Limiter") + f" - {track_name or track_id}")
        self.setModal(False)
        apply_float_panel(self)
        self.setMinimumSize(360, 330)
        self.setStyleSheet(PRO_DIALOG_QSS)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 12, 12, 12)

        self.card = LimiterCard(self._current.to_dict())
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
        self.limiter_committed.emit(self._track_id, self._current)

    def _on_card_changed(self, data: dict) -> None:
        if self._updating:
            return
        try:
            self._current = LimiterParams.from_dict(data)
        except ValueError:
            return
        if self._dragging:
            self.limiter_preview.emit(self._track_id, self._current)
        else:
            self.limiter_committed.emit(self._track_id, self._current)

    def set_current_params(self, params: LimiterParams) -> None:
        """外部静默回填（不回发信号）。"""
        self._updating = True
        self._current = params
        self.card.syncFromData(params.to_dict())
        self._updating = False

    def reset(self) -> None:
        """重置为默认参数（保留开关状态），恰好一次 committed。"""
        was_enabled = self._current.enabled
        self._current = LimiterParams(enabled=was_enabled)
        self.card.syncFromData(self._current.to_dict())
        self.limiter_committed.emit(self._track_id, self._current)
