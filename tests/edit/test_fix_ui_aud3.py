"""Batch-A Critical fixes regression tests (AUD-19740/19800/20000/20400).

Covers four audit findings from docs/audit/2026-09-28/batches/:

- AUD-19740: CompClipSettingsDialog fps "Custom" item must expose a real
  numeric input and ``get_settings()`` must never attempt
  ``float("__custom__")``.
- AUD-19800: double-clicking an MG generator card must not raise NameError
  (``template_display_name`` was only imported inside another function).
- AUD-20000: the properties-panel keyframe diamond must write/delete
  keyframes at the *playhead* frame, not the stale cached frame 0.
- AUD-20400: the CPU texture-upload path must respect QImage row stride
  (``bytesPerLine`` 4-byte alignment) instead of assuming a compact buffer.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from PyQt6.QtCore import QObject, pyqtSignal
from PyQt6.QtGui import QImage, QColor
from PyQt6.QtWidgets import QWidget

from src.features.edit.ui.comp_clip_settings_dialog import (
    _CUSTOM_KEY,
    CompClipSettingsDialog,
)
from src.features.edit.ui.program_viewer import _rgb888_qimage_to_rgba


# ================================================================
# AUD-19740 — Custom fps input in CompClipSettingsDialog
# ================================================================

def _select_custom_fps(dialog: CompClipSettingsDialog) -> None:
    combo = dialog._fps_combo
    for i in range(combo.count()):
        if combo.itemData(i) == _CUSTOM_KEY:
            combo.setCurrentIndex(i)
            return
    raise AssertionError("fps combo has no Custom item")


def test_custom_fps_returns_drag_value_without_value_error(qapp) -> None:
    """Selecting Custom must yield a numeric fps (no float('__custom__'))."""
    dialog = CompClipSettingsDialog()
    _select_custom_fps(dialog)
    dialog._custom_fps_drag.setValue(47.953)

    settings = dialog.get_settings()

    assert settings["fps"] == pytest.approx(47.953)
    dialog.close()


def test_non_preset_initial_fps_auto_falls_back_to_custom_input(qapp) -> None:
    """Edit mode with a non-preset fps must prefill the Custom input."""
    dialog = CompClipSettingsDialog(
        edit_mode=True, initial_settings={"fps": 12.5})

    assert dialog._fps_combo.currentData() == _CUSTOM_KEY
    assert not dialog._custom_fps_drag.isHidden()
    assert dialog.get_settings()["fps"] == pytest.approx(12.5)
    dialog.close()


def test_custom_fps_input_visibility_follows_combo(qapp) -> None:
    """The numeric input is hidden on presets and shown on Custom."""
    dialog = CompClipSettingsDialog()  # default fps 24 → preset selected
    assert dialog._custom_fps_drag.isHidden()

    _select_custom_fps(dialog)
    assert not dialog._custom_fps_drag.isHidden()

    # Back to a preset (index 2 == "25 fps")
    dialog._fps_combo.setCurrentIndex(2)
    assert dialog._custom_fps_drag.isHidden()
    assert dialog.get_settings()["fps"] == pytest.approx(25.0)
    dialog.close()


def test_empty_name_shows_visible_hint(qapp) -> None:
    """Accept must be refused with a user-visible message, not silently."""
    from PyQt6.QtWidgets import QDialog

    dialog = CompClipSettingsDialog()
    dialog._name_edit.setText("   ")
    dialog._on_accept()

    assert dialog.result() != int(QDialog.DialogCode.Accepted)
    assert not dialog._hint_label.isHidden()
    assert dialog._hint_label.text()
    dialog.close()


# ================================================================
# AUD-19800 — MG generator double-click NameError
# ================================================================

def test_mg_generator_double_click_does_not_raise_name_error(qapp) -> None:
    """Double-click must resolve template_display_name at module scope."""
    from src.features.edit.ui.edit_effects_panel import EditEffectsPanel

    panel = EditEffectsPanel()
    template = MagicMock()
    template.display_name = "My Fancy Generator"
    template.metadata = None
    registry = MagicMock()
    registry.get.return_value = template
    panel._motion_graphic_registry = registry

    # Before the fix this raised NameError inside a Qt slot (fatal).
    panel._on_motion_graphic_generator_double_clicked_by_id(
        "motion_graphic_template:t1")

    assert "My Fancy Generator" in panel.info_label.text()


# ================================================================
# AUD-20000 — keyframe diamond operates at the playhead frame
# ================================================================

def _keyframe_spy_panel():
    from src.features.edit.ui.edit_properties_panel import EditPropertiesPanel

    panel = EditPropertiesPanel()
    clip = MagicMock()
    clip.clip_id = "clip-1"
    clip.get_param_at_frame = MagicMock(return_value=0.75)
    panel._current_clips = [clip]
    emitted: list[tuple] = []
    panel.keyframe_toggled.connect(lambda *args: emitted.append(args))
    effect_emitted: list[tuple] = []
    panel.effect_keyframe_toggled.connect(lambda *args: effect_emitted.append(args))
    return panel, clip, emitted, effect_emitted


def test_clip_keyframe_toggled_emits_playhead_frame(qapp) -> None:
    """◆ must emit the resolved playhead frame + value at that frame."""
    panel, clip, emitted, _ = _keyframe_spy_panel()
    panel.set_frame_provider(lambda: 42)

    # Button would have emitted its stale cache (frame=0, value=0.0).
    panel._on_keyframe_button_toggled("opacity", 0, 0.0)

    assert emitted, "keyframe_toggled must be emitted"
    clip_id, param_name, frame, value = emitted[-1]
    assert (clip_id, param_name) == ("clip-1", "opacity")
    assert frame == 42, "frame must come from the playhead, not the cache"
    assert value == pytest.approx(0.75)
    clip.get_param_at_frame.assert_called_with("opacity", 42)


def test_effect_keyframe_toggled_emits_playhead_frame(qapp) -> None:
    """Effect ◆ must emit the resolved playhead frame + animated value."""
    panel, clip, _, effect_emitted = _keyframe_spy_panel()
    panel.set_frame_provider(lambda: 130)
    effect = MagicMock()
    effect.get_param_at_frame = MagicMock(return_value=5.5)
    clip.get_effect_by_id = MagicMock(return_value=effect)

    panel._on_effect_keyframe_button_toggled(
        "effect-1", "radius", "clip-1", 0, 0.0)

    assert effect_emitted, "effect_keyframe_toggled must be emitted"
    clip_id, effect_id, param_name, frame, value = effect_emitted[-1]
    assert (clip_id, effect_id, param_name) == ("clip-1", "effect-1", "radius")
    assert frame == 130
    assert value == pytest.approx(5.5)
    effect.get_param_at_frame.assert_called_with("radius", 130)


def test_frame_resolved_from_timeline_panel_and_subscription(qapp) -> None:
    """Without a provider the panel pulls the frame from timeline_panel and
    subscribes to its playback controller's frame_changed signal."""

    class _FakePlaybackController(QObject):
        frame_changed = pyqtSignal(int)

        def __init__(self) -> None:
            super().__init__()
            self.frame = 7

    class _FakeTimelinePanel(QWidget):
        def __init__(self) -> None:
            super().__init__()
            self._controller = _FakePlaybackController()

        def get_current_frame(self) -> int:
            return self._controller.frame

        def get_playback_controller(self) -> _FakePlaybackController:
            return self._controller

    from src.features.edit.ui.edit_properties_panel import EditPropertiesPanel

    host = QWidget()
    timeline_panel = _FakeTimelinePanel()
    host.timeline_panel = timeline_panel  # attribute main window exposes
    panel = EditPropertiesPanel(parent=host)

    assert panel._resolve_current_frame() == 7

    panel._ensure_frame_source_connected()
    timeline_panel._controller.frame = 99
    timeline_panel._controller.frame_changed.emit(99)
    assert panel._current_frame == 99

    # Keyframe emission follows the live playhead without any explicit push.
    emitted: list[tuple] = []
    panel.keyframe_toggled.connect(lambda *args: emitted.append(args))
    clip = MagicMock()
    clip.clip_id = "clip-2"
    clip.get_param_at_frame = MagicMock(return_value=0.25)
    panel._current_clips = [clip]
    panel._on_keyframe_button_toggled("scale_x", 0, 0.0)
    assert emitted[-1][2] == 99


def test_set_clip_refreshes_keyframe_buttons_at_playhead(qapp) -> None:
    """set_clip must refresh ◆ state with the resolved frame (AUD-20000)."""
    from src.features.edit.ui.edit_properties_panel import EditPropertiesPanel
    from src.features.edit.core.clip_params import TransformParams

    panel = EditPropertiesPanel()
    panel.set_frame_provider(lambda: 55)
    clip = _make_mock_clip()
    clip.transform = TransformParams()
    panel.set_clip(clip)

    assert panel._current_frame == 55


def _make_mock_clip() -> MagicMock:
    """Minimal clip mock driving set_clip section building."""
    clip = MagicMock()
    clip.clip_id = "test-clip"
    clip.transform = MagicMock()
    clip.crop = MagicMock()
    clip.crop.left = clip.crop.right = clip.crop.top = clip.crop.bottom = 0.0
    clip.crop.feather = 0.0
    clip.opacity = 1.0
    clip.speed = MagicMock()
    clip.speed.speed = 1.0
    clip.speed.method = "linear"
    clip.effects = []
    clip.asset_id = "asset-1"
    clip.name = "Test"
    clip.timeline_start = 0
    clip.timeline_end = 100
    return clip


# ================================================================
# AUD-20400 — QImage row stride in the CPU upload path
# ================================================================

def _make_rgb888_image(width: int, height: int) -> QImage:
    """QImage whose rows carry a per-row distinct color (skew detector)."""
    image = QImage(width, height, QImage.Format.Format_RGB888)
    for y in range(height):
        color = QColor((y * 40 + 10) % 256, (y * 40 + 20) % 256,
                       (y * 40 + 30) % 256)
        for x in range(width):
            image.setPixel(x, y, color.rgb())
    return image


def test_rgb888_extraction_respects_padded_row_stride() -> None:
    """width*3 % 4 != 0 → rows must be de-padded, not reshaped blindly."""
    width, height = 5, 3  # 5*3=15 bytes → Qt pads stride to 16
    image = _make_rgb888_image(width, height)
    assert image.bytesPerLine() == 16, "precondition: padded scanlines"

    rgba = _rgb888_qimage_to_rgba(image)

    assert rgba is not None
    assert rgba.shape == (height, width, 4)
    for y in range(height):
        assert tuple(rgba[y, 0, :3]) == (
            (y * 40 + 10) % 256, (y * 40 + 20) % 256, (y * 40 + 30) % 256), (
            f"row {y} skewed — stride padding leaked into pixel data")
    assert (rgba[..., 3] == 255).all()


def test_rgb888_extraction_compact_stride_path() -> None:
    """width*3 % 4 == 0 → compact fast path returns identical pixels."""
    width, height = 4, 2  # 12 bytes per row → stride == row_bytes
    image = _make_rgb888_image(width, height)
    assert image.bytesPerLine() == width * 3

    rgba = _rgb888_qimage_to_rgba(image)

    assert rgba is not None
    assert rgba.shape == (height, width, 4)
    for y in range(height):
        assert tuple(rgba[y, 0, :3]) == (
            (y * 40 + 10) % 256, (y * 40 + 20) % 256, (y * 40 + 30) % 256)


def test_rgb888_extraction_rejects_invalid_dimensions() -> None:
    assert _rgb888_qimage_to_rgba(QImage()) is None
