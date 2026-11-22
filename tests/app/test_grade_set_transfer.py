"""The photo grade clipboard keeps the target and source histories independent."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from PyQt6.QtCore import QObject
from pytestqt.qtbot import QtBot

from src.app.controllers.grade_set_transfer import GradeSetTransfer
from src.features.color.core.layer import Layer, MaskShape
from src.shared.color.lut_resource_registry import LutResourceSession


class _Host(QObject):
    def __init__(self) -> None:
        super().__init__()
        self.status_bar = SimpleNamespace(showMessage=Mock())
        self.grade_set_panel = SimpleNamespace(
            active_path="/first.png", set_grade_clipboard_available=Mock(), invalidate_graded_thumbnail=Mock())
        self._current_edit_project = SimpleNamespace(project_id="first")
        self.engine = SimpleNamespace(
            layers=[Layer("Source")], active_layer_index=0, img_full_original=object(), source_epoch=1)
        self.color_resource_controller = None
        self.cancel_image_switch = False
        self._apply_grade_set_layers = Mock()

    def _active_grade_set_image(self) -> str:
        return self.grade_set_panel.active_path

    def _open_grade_set_image(self, path: str) -> None:
        if not self.cancel_image_switch and self.grade_set_panel.active_path != path:
            self.grade_set_panel.active_path = path
            self._current_edit_project = SimpleNamespace(project_id=path)
            self.engine.source_epoch += 1


def test_copy_keeps_live_grade_snapshot_and_skips_source_pixel_masks(qtbot: QtBot) -> None:
    host = _Host()
    layer = host.engine.layers[0]
    layer.grading_params["exposure"] = 0.65
    group = layer.add_group("Windows")
    group.add_child(MaskShape(MaskShape.BITMAP))
    circle = MaskShape(MaskShape.CIRCLE)
    group.add_child(circle)

    transfer = GradeSetTransfer(host)
    try:
        transfer.copy("/first.png")
        layer.grading_params["exposure"] = 1.25
        transfer.paste("/second.png")
        pasted, index = host._apply_grade_set_layers.call_args.args
        assert index == 0
        assert pasted[0].grading_params["exposure"] == 0.65
        assert pasted[0].id != layer.id
        assert [child.type for child in pasted[0].mask_shapes[0].children] == [MaskShape.CIRCLE]
        assert pasted[0].mask_shapes[0].children[0].id != circle.id
        host.grade_set_panel.invalidate_graded_thumbnail.assert_called_once_with("/second.png")
    finally:
        transfer.shutdown()


def test_paste_cancelling_image_switch_leaves_current_grade_untouched(qtbot: QtBot) -> None:
    host = _Host()
    transfer = GradeSetTransfer(host)
    try:
        transfer.copy("/first.png")
        host.cancel_image_switch = True
        transfer.paste("/second.png")
        host._apply_grade_set_layers.assert_not_called()
        host.grade_set_panel.invalidate_graded_thumbnail.assert_not_called()
        assert host.grade_set_panel.active_path == "/first.png"
    finally:
        transfer.shutdown()


def test_paste_reimports_lut_after_source_session_was_cleared(
    qtbot: QtBot, tmp_path: Path, monkeypatch,
) -> None:
    lut = tmp_path / "example.cube"
    lut.write_text("LUT_3D_SIZE 2\n" + "\n".join(
        f"{red} {green} {blue}"
        for blue in (0, 1) for green in (0, 1) for red in (0, 1)), encoding="utf-8")
    source_session = LutResourceSession(str(tmp_path / "source"))
    target_session = LutResourceSession(str(tmp_path / "target"))
    host = _Host()
    result = source_session.import_files([str(lut)])[0]
    assert result.entry is not None
    host.engine.layers[0].active_lut_path = result.entry.uri
    monkeypatch.setattr("src.app.controllers.grade_set_transfer.get_lut_resource_session",
                        lambda: source_session if host.grade_set_panel.active_path == "/first.png" else target_session)
    transfer = GradeSetTransfer(host)
    try:
        transfer.copy("/first.png")
        qtbot.waitUntil(lambda: transfer._grade is not None)
        source_session.reset()
        lut.unlink()
        transfer.paste("/second.png")
        qtbot.waitUntil(lambda: host._apply_grade_set_layers.called)
        pasted, _ = host._apply_grade_set_layers.call_args.args
        assert target_session.staged_path(pasted[0].active_lut_path) is not None
    finally:
        transfer.shutdown()
