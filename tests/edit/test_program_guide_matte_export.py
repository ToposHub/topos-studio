"""Program Viewer cinema-matte export tests."""

from __future__ import annotations

from types import SimpleNamespace

import numpy as np

from src.features.edit.core.timeline_export import (
    TimelineExporter,
    apply_program_guide_matte_bgr,
)
from src.features.edit.core.timeline_v2 import TimelineSequence


def test_235_matte_adds_opaque_cinema_bars() -> None:
    """2.35:1 on a 16:9 output should create top and bottom black bars."""
    frame = np.full((1080, 1920, 3), 255, dtype=np.uint8)

    matted = apply_program_guide_matte_bgr(frame, 2.35)

    assert np.all(matted[0] == 0)
    assert np.all(matted[-1] == 0)
    assert np.all(matted[540] == 255)
    assert np.all(frame == 255)


def test_exporter_only_burns_enabled_black_matte() -> None:
    """Outline-only guides must never alter the exported pixels."""
    frame = np.full((100, 200, 3), 255, dtype=np.uint8)
    exporter = TimelineExporter.__new__(TimelineExporter)
    exporter._sequence = SimpleNamespace(format_settings={
        "program_guide": {
            "type": "aspect_ratio",
            "id": "2.35",
            "params": {"ratio": 2.35, "fill_black": True, "burn_in": True},
        }
    })

    assert np.all(exporter._apply_program_guide_matte(frame)[0] == 0)

    exporter._sequence.format_settings["program_guide"]["params"]["burn_in"] = False

    assert exporter._apply_program_guide_matte(frame) is frame


def test_program_guide_is_persisted_in_timeline_format_settings() -> None:
    """The program selection survives through the normal timeline save path."""
    from src.app.controllers.edit_controller import EditControllerMixin

    timeline = TimelineSequence(name="Guide test")
    captured: list[TimelineSequence] = []
    controller = SimpleNamespace(
        edit_page=SimpleNamespace(_current_timeline=timeline),
        edit_project_dirty=False,
        _on_edit_timeline_changed=captured.append,
    )

    EditControllerMixin.on_program_guide_changed(
        controller,
        "aspect_ratio",
        "2.35",
        {"ratio": 2.35, "fill_black": True, "burn_in": True},
    )

    assert controller.edit_project_dirty
    assert captured[0].format_settings["program_guide"] == {
        "type": "aspect_ratio",
        "id": "2.35",
        "params": {"ratio": 2.35, "fill_black": True, "burn_in": True},
    }
