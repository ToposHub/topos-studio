from src.features.edit.ui.scene_cut_dialog import _format_cut_confirmation_message


def test_add_cuts_confirmation_formats_count() -> None:
    message = _format_cut_confirmation_message(7)

    assert "7" in message
    assert "{0}" not in message
