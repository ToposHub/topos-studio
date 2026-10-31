"""Focused tests for the timeline clip color treatment."""

from __future__ import annotations

from PyQt6.QtGui import QColor

from src.features.edit.ui.timeline_items import (
    TIMELINE_LABEL_COLORS,
    _derive_label_palette,
)


def test_label_palette_has_broad_bright_but_controlled_choices() -> None:
    assert len(TIMELINE_LABEL_COLORS) >= 18
    assert len({name for name, _color in TIMELINE_LABEL_COLORS}) == len(TIMELINE_LABEL_COLORS)

    for _name, value in TIMELINE_LABEL_COLORS:
        color = QColor(value)
        assert color.isValid()
        assert color.getHslF()[1] <= 0.65


def test_custom_label_color_is_toned_down_for_clip_body() -> None:
    palette = _derive_label_palette("#4488FF")

    assert palette is not None
    body_start, body_end, label_bar, waveform = palette
    assert body_start.getHslF()[1] <= 0.76
    assert body_end.lightnessF() < body_start.lightnessF()
    assert label_bar.lightnessF() < body_end.lightnessF()
    assert waveform.lightnessF() > body_start.lightnessF()


def test_neutral_label_stays_neutral() -> None:
    palette = _derive_label_palette("#888888")

    assert palette is not None
    assert palette[0].getHslF()[1] == 0.0
    assert palette[3].getHslF()[1] == 0.0


def test_invalid_label_color_uses_default_palette_path() -> None:
    assert _derive_label_palette("not-a-color") is None
    assert _derive_label_palette(None) is None
