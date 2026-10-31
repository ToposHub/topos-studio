"""Speech-to-text options dialog.

The dialog deliberately returns a plain dictionary.  The controller can pass
the same options through the analysis service without making the provider or
the timeline depend on Qt widgets.
"""

from __future__ import annotations

from typing import Any

from PyQt6.QtWidgets import (
    QCheckBox,
    QDialog,
    QFormLayout,
    QLabel,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QHBoxLayout,
)

from src.shared.export.export_panel import _apply_compact_button_style, _make_param_label
from src.shared.i18n.i18n import I18N
from src.shared.ui.widgets import ChevronComboBox
from src.features.edit.ui.edit_style import input_style


class SpeechToTextSettingsDialog(QDialog):
    """Collect transcription settings for one audio clip."""

    def __init__(self, parent: Any = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(I18N.tr("speech_to_text.dialog.title", "Speech-to-Text Settings"))
        self.setModal(True)
        self.setMinimumWidth(430)
        self.setStyleSheet(input_style())

        intro = QLabel(
            I18N.tr(
                "speech_to_text.dialog.intro",
                "Choose recognition quality, audio preprocessing, and timestamp mode.\n"
                "The first model load or vocal separation may take some time.",
            )
        )
        intro.setWordWrap(True)

        self._model_combo = ChevronComboBox()
        self._model_combo.addItem(
            I18N.tr("speech_to_text.model.auto", "Auto (prefer the best installed model)"),
            "auto",
        )
        self._model_combo.addItem(
            I18N.tr("speech_to_text.model.small", "Lightweight 0.6B (faster, lower memory)"),
            "small",
        )
        self._model_combo.addItem(
            I18N.tr("speech_to_text.model.large", "High quality 1.7B (more memory)"),
            "large",
        )

        self._music_combo = ChevronComboBox()
        self._music_combo.addItem(
            I18N.tr("speech_to_text.music.keep", "Keep original audio (fastest)"), "keep"
        )
        self._music_combo.addItem(
            I18N.tr(
                "speech_to_text.music.remove",
                "Prioritize vocals (remove background music with Demucs)",
            ),
            "remove",
        )

        self._language_combo = ChevronComboBox()
        self._language_combo.addItem(I18N.tr("speech_to_text.language.auto", "Auto detect"), "auto")
        self._language_combo.addItem(I18N.tr("speech_to_text.language.zh", "Chinese"), "zh")
        self._language_combo.addItem(I18N.tr("speech_to_text.language.en", "English"), "en")
        self._language_combo.addItem(I18N.tr("speech_to_text.language.ja", "Japanese"), "ja")
        self._language_combo.addItem(I18N.tr("speech_to_text.language.ko", "Korean"), "ko")

        self._timestamp_combo = ChevronComboBox()
        self._timestamp_combo.addItem(
            I18N.tr("speech_to_text.timestamps.auto", "Auto (word-level when alignment is available)"),
            "auto",
        )
        self._timestamp_combo.addItem(
            I18N.tr("speech_to_text.timestamps.segment", "Segment-level (faster and more stable)"),
            "segment",
        )

        self._speaker_check = QCheckBox(
            I18N.tr("speech_to_text.speakers", "Try speaker identification (requires Sortformer)")
        )
        self._speaker_check.setChecked(True)

        self._max_tokens = QSpinBox()
        self._max_tokens.setRange(2048, 32768)
        self._max_tokens.setSingleStep(1024)
        self._max_tokens.setValue(8192)
        self._max_tokens.setSuffix(f" {I18N.tr('speech_to_text.tokens_suffix', 'tokens')}")
        self._max_tokens.setToolTip(
            I18N.tr("speech_to_text.max_tokens_hint", "Prevents long audio from being truncated")
        )

        form = QFormLayout()
        form.setSpacing(10)
        form.addRow(_make_param_label(I18N.tr("speech_to_text.model.label", "Model")), self._model_combo)
        form.addRow(_make_param_label(I18N.tr("speech_to_text.music.label", "Background music")), self._music_combo)
        form.addRow(_make_param_label(I18N.tr("speech_to_text.language.label", "Language")), self._language_combo)
        form.addRow(_make_param_label(I18N.tr("speech_to_text.timestamps.label", "Timestamps")), self._timestamp_combo)
        form.addRow(_make_param_label(I18N.tr("speech_to_text.max_tokens.label", "Output limit")), self._max_tokens)
        form.addRow("", self._speaker_check)

        buttons = QHBoxLayout()
        buttons.addStretch()
        cancel_button = QPushButton(I18N.tr("common.cancel", "Cancel"))
        ok_button = QPushButton(I18N.tr("common.ok", "OK"))
        ok_button.setDefault(True)
        _apply_compact_button_style(cancel_button)
        _apply_compact_button_style(ok_button)
        cancel_button.clicked.connect(self.reject)
        ok_button.clicked.connect(self.accept)
        buttons.addWidget(cancel_button)
        buttons.addWidget(ok_button)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)
        layout.addWidget(intro)
        layout.addLayout(form)
        layout.addLayout(buttons)

    def settings(self) -> dict[str, Any]:
        """Return provider-neutral options selected by the user."""
        return {
            "model_size": str(self._model_combo.currentData() or "auto"),
            "background_music": str(self._music_combo.currentData() or "keep"),
            "language": str(self._language_combo.currentData() or "auto"),
            "timestamps": str(self._timestamp_combo.currentData() or "auto"),
            "speakers": bool(self._speaker_check.isChecked()),
            "max_new_tokens": int(self._max_tokens.value()),
        }


__all__ = ["SpeechToTextSettingsDialog"]
