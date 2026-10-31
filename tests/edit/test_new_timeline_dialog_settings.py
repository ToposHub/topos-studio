from __future__ import annotations

from src.features.edit.ui.new_timeline_dialog import NewTimelineDialog


def test_existing_timeline_settings_populate_the_shared_dialog(qapp) -> None:
    dialog = NewTimelineDialog(
        initial_settings={
            "name": "Sequence A",
            "fps_num": 30000,
            "fps_den": 1001,
            "width": 2560,
            "height": 1440,
            "sample_rate": 96000,
            "pixel_aspect_ratio": 1.0,
            "field_order": "progressive",
            "display_format": "frames",
            "audio_track_type": "stereo",
            "audio_display_format": "ms",
            "start_timecode": "01:00:00:00",
        },
        edit_mode=True,
    )

    settings = dialog.get_settings()

    assert dialog.windowTitle() == "Timeline Settings"
    assert settings["name"] == "Sequence A"
    assert (settings["fps_num"], settings["fps_den"]) == (30000, 1001)
    assert (settings["width"], settings["height"]) == (2560, 1440)
    assert settings["sample_rate"] == 96000
    assert settings["start_timecode"] == "01:00:00:00"
