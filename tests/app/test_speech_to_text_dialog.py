from __future__ import annotations

from src.features.edit.ui.speech_to_text_dialog import SpeechToTextSettingsDialog
from src.shared.i18n.i18n import I18N
from src.shared.ui.widgets import ChevronComboBox


def test_speech_to_text_dialog_has_safe_long_audio_defaults(qapp) -> None:
    dialog = SpeechToTextSettingsDialog()

    settings = dialog.settings()

    assert settings == {
        "model_size": "auto",
        "background_music": "keep",
        "language": "auto",
        "timestamps": "auto",
        "speakers": True,
        "max_new_tokens": 8192,
    }


def test_speech_to_text_dialog_uses_current_locale(qapp) -> None:
    I18N.initialize()
    previous = I18N.get_current_locale()
    try:
        I18N.load_locale("en_US")
        dialog = SpeechToTextSettingsDialog()
        assert isinstance(dialog._model_combo, ChevronComboBox)
        assert dialog.windowTitle() == "Speech-to-Text Settings"
        assert dialog._model_combo.itemText(0).startswith("Auto")
        assert dialog._music_combo.itemText(1).startswith("Prioritize vocals")
    finally:
        I18N.load_locale(previous)
