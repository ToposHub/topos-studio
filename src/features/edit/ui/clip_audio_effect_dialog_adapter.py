"""Route timeline audio-effect windows to the project command and preview paths."""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from PyQt6.QtCore import QObject, pyqtSignal
from PyQt6.QtWidgets import QWidget

from src.features.mixer.effects.production_dialog import PRODUCTION_BINDINGS, ProductionEffectDialog
from src.shared.ui.audio_mixer.pro_widgets import present_effect_window

if TYPE_CHECKING:
    from src.features.edit.core.timeline_v2 import TimelineSequence


class ClipAudioEffectDialogManager(QObject):
    """Keep at most one effect window for each clip and production effect instance."""

    clip_effect_params_committed = pyqtSignal(str, str, dict, object)
    clip_effect_preview = pyqtSignal(str, str, dict, object)
    clip_effect_preview_finished = pyqtSignal(str, str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._dialogs: dict[tuple[str, str], ProductionEffectDialog] = {}
        self._timeline_id: str | None = None

    def open_dialog(
        self,
        effect_type: str,
        clip_id: str,
        effect_id: str,
        params_dict: dict[str, Any],
        clip_name: str = "",
        parent: QWidget | None = None,
        *,
        enabled: bool | None = None,
        algorithm_version: int | None = None,
    ) -> ProductionEffectDialog | None:
        if effect_type not in PRODUCTION_BINDINGS or effect_type == "spatial_panner":
            return None
        key = clip_id, effect_id
        dialog = self._dialogs.get(key)
        if dialog is not None and dialog.fx_effect_type != effect_type:
            dialog.close()
            dialog = None
        if dialog is None:
            dialog = ProductionEffectDialog(
                effect_type, clip_name or clip_id, params_dict, parent or self.parent(),
                enabled=enabled, algorithm_version=algorithm_version,
            )
            dialog.committed.connect(
                lambda params: self.clip_effect_params_committed.emit(
                    clip_id, effect_id, params, dialog.power_override))
            dialog.preview.connect(
                lambda params: self.clip_effect_preview.emit(
                    clip_id, effect_id, params, dialog.power_override))
            dialog.preview_cleared.connect(lambda: self.clip_effect_preview_finished.emit(clip_id, effect_id))
            dialog.finished.connect(lambda _result: self._finished(key, dialog))
            self._dialogs[key] = dialog
        else:
            dialog.set_algorithm_version(algorithm_version)
            dialog.set_current_params(params_dict, enabled=enabled)
        present_effect_window(dialog)
        return dialog

    def _finished(self, key: tuple[str, str], dialog: ProductionEffectDialog) -> None:
        if self._dialogs.get(key) is dialog:
            self._dialogs.pop(key)
            self.clip_effect_preview_finished.emit(*key)

    def previews_for(self, clip_id: str) -> dict[str, tuple[dict[str, Any], bool | None]]:
        return {
            effect_id: preview
            for (owner, effect_id), dialog in self._dialogs.items()
            if owner == clip_id and (preview := dialog.active_preview) is not None
        }

    def sync_timeline(self, timeline: TimelineSequence | None) -> None:
        if timeline is None:
            self.close_all()
            self._timeline_id = None
            return
        if self._timeline_id is not None and self._timeline_id != timeline.timeline_id:
            self.close_all()
        self._timeline_id = timeline.timeline_id
        for (clip_id, effect_id), dialog in tuple(self._dialogs.items()):
            clip = timeline.get_clip_by_id(clip_id)
            effect = clip.get_effect_by_id(effect_id) if clip else None
            track = timeline.get_track_by_id(clip.track_id) if clip else None
            if effect is None or effect.effect_type != dialog.fx_effect_type or track is None or track.is_locked:
                dialog.close()
            else:
                dialog.set_algorithm_version(effect.algorithm_version)
                dialog.set_current_params(dict(effect.params), enabled=effect.is_enabled)

    def close_all(self) -> None:
        for dialog in tuple(self._dialogs.values()):
            dialog.close()
        self._dialogs.clear()
