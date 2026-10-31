from __future__ import annotations

from PyQt6.QtCore import pyqtSignal
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import QWidget

from src.features.composite.ui.project_settings_widget import ProjectSettingsWidget
from src.features.edit.ui.comp_clip_settings_dialog import CompClipSettingsDialog
from src.shared.color import color_picker as color_picker_module
from src.shared.color.color_conversion import srgb_to_linear
from src.shared.ui.widgets import color_picker_control as control_module
from src.shared.ui.widgets.color_picker_control import ColorPickerControl


def test_new_clip_and_project_settings_use_the_same_color_control(qapp) -> None:
    dialog = CompClipSettingsDialog()
    project_settings = ProjectSettingsWidget()

    assert isinstance(dialog._bg_color_control, ColorPickerControl)
    assert isinstance(project_settings._bg_color_control, ColorPickerControl)
    assert dialog._bg_color_control.swatch.size() == (
        project_settings._bg_color_control.swatch.size())
    assert dialog._bg_color_control.eyedropper_button.toolTip() == (
        project_settings._bg_color_control.eyedropper_button.toolTip()
    )

    dialog.close()
    project_settings.close()


def test_shared_color_control_does_not_commit_cancelled_preview(
    qapp, monkeypatch
) -> None:
    control = ColorPickerControl((1.0, 1.0, 1.0, 1.0))
    committed: list[QColor] = []
    control.color_changed.connect(committed.append)

    def fake_get_color(*, parent, initial, on_live):
        on_live(QColor("#123456"))
        return None

    monkeypatch.setattr(control_module, "get_color", fake_get_color)
    control._pick_color()

    assert control.rgba() == (1.0, 1.0, 1.0, 1.0)
    assert control.hex_label.text() == "#ffffffff"
    assert committed == []

    control.close()


def test_color_control_restores_outer_window_after_picker(qapp, monkeypatch) -> None:
    control = ColorPickerControl((1.0, 1.0, 1.0, 1.0))
    restored: list[bool] = []
    monkeypatch.setattr(
        control, "_restore_parent_window", lambda: restored.append(True))

    def fake_get_color(*, parent, initial, on_live):
        return QColor("#123456")

    monkeypatch.setattr(control_module, "get_color", fake_get_color)
    control._pick_color()

    assert restored
    control.close()


def test_shared_color_control_uses_screen_eyedropper(qapp, monkeypatch) -> None:
    class FakeDropper(QWidget):
        colorPicked = pyqtSignal(QColor)
        cancelled = pyqtSignal()

        def start(self) -> None:
            pass

    monkeypatch.setattr(control_module, "EyeDropperOverlay", FakeDropper)
    control = ColorPickerControl((1.0, 1.0, 1.0, 1.0))
    control._start_eyedropper()

    assert control._active_dropper is not None
    control._active_dropper.colorPicked.emit(QColor("#abcdef"))

    assert control._active_dropper is None
    assert control.hex_label.text() == "#ffabcdef"
    picked = QColor("#abcdef")
    assert control.rgba()[:3] == (
        srgb_to_linear(picked.redF()),
        srgb_to_linear(picked.greenF()),
        srgb_to_linear(picked.blueF()),
    )
    control.close()


def test_color_inspector_closes_after_confirm_mouse_event(
    qapp, monkeypatch
) -> None:
    """Confirming the picker must not click through to the outer dialog."""
    scheduled: list[object] = []

    class FakeTimer:
        @staticmethod
        def singleShot(_delay: int, callback) -> None:
            scheduled.append(callback)

    monkeypatch.setattr(color_picker_module, "QTimer", FakeTimer)
    monkeypatch.setattr(color_picker_module, "save_config", lambda *args: None)

    studio = color_picker_module.CompactColorStudio()
    studio.show()
    studio._confirm_and_close(QColor("#123456"))

    assert studio.isVisible()
    assert len(scheduled) == 1
    scheduled[0]()
    assert not studio.isVisible()
    studio.deleteLater()
