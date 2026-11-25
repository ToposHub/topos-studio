"""Chroma Warp plot paint and gesture regressions without a decoded frame."""
from __future__ import annotations

import pytest
from PyQt6.QtCore import QPoint, Qt
from PyQt6.QtGui import QImage
from PyQt6.QtTest import QTest
from pytestqt.qtbot import QtBot

from src.shared.ui.widgets.chroma_warp_canvas import ChromaWarpCanvas


@pytest.fixture
def canvas(qtbot: QtBot) -> ChromaWarpCanvas:
    widget = ChromaWarpCanvas()
    qtbot.addWidget(widget)
    widget.resize(400, 400)
    widget.show()
    return widget


def _samples_changed(before: QImage, after: QImage, x: int, y: int) -> bool:
    scale = before.devicePixelRatio()
    for py in range(y - 2, y + 3):
        for px in range(x - 2, x + 3):
            u, v = round(px * scale), round(py * scale)
            a, b = before.pixelColor(u, v), after.pixelColor(u, v)
            if abs(a.red() - b.red()) + abs(a.green() - b.green()) + abs(a.blue() - b.blue()) > 24:
                return True
    return False


def test_normal_outline_expands_and_point_mode_uses_the_source(canvas: ChromaWarpCanvas) -> None:
    blank = canvas.grab().toImage()
    start, end = QPoint(120, 160), QPoint(280, 160)
    QTest.mousePress(canvas, Qt.MouseButton.LeftButton, pos=start)
    QTest.mouseMove(canvas, end)
    during = canvas.grab().toImage()
    assert _samples_changed(blank, during, 200, 118)
    QTest.mouseRelease(canvas, Qt.MouseButton.LeftButton, pos=end)
    assert canvas.density_image is None

    canvas.set_parameter("chroma_range", 0.08)
    wider = canvas.grab().toImage()
    assert _samples_changed(blank, wider, 200, 80)
    assert not _samples_changed(blank, wider, 200, 118)

    canvas.set_parameter("chroma_range", 0.04)
    canvas.set_parameter("stroke_mode", "point_to_point")
    point = canvas.grab().toImage()
    assert _samples_changed(blank, point, 120, 118)
    assert not _samples_changed(blank, point, 200, 118)


def test_pin_has_its_own_range_ring(canvas: ChromaWarpCanvas) -> None:
    blank = canvas.grab().toImage()
    canvas.tool = "add_pin"
    QTest.mouseClick(canvas, Qt.MouseButton.LeftButton, pos=QPoint(180, 180))
    pin = canvas.grab().toImage()
    assert _samples_changed(blank, pin, 180, 164)
    assert not _samples_changed(blank, pin, 180, 138)


@pytest.mark.parametrize("preview", [False, True])
def test_returning_a_draft_to_its_start_creates_no_stroke(canvas: ChromaWarpCanvas, preview: bool) -> None:
    phases: list[str] = []
    canvas.edit_requested.connect(phases.append)
    start = [0.3, 0.4]
    canvas._begin_plot_edit(start)
    canvas._move_plot_edit([0.5, 0.6])
    if preview:
        canvas._publish_preview()
    canvas._move_plot_edit(start)
    assert canvas.snapshot(preview=True)["strokes"] == []
    canvas._finish_plot_edit()
    assert phases == (["update", "cancel"] if preview else [])
    assert canvas.strokes == []


def test_preview_and_release_keep_one_stroke_id(canvas: ChromaWarpCanvas) -> None:
    records: list[tuple[str, str]] = []

    def record(phase: str) -> None:
        records.append((phase, canvas.snapshot(preview=phase == "update")["strokes"][0]["id"]))

    canvas.edit_requested.connect(record)
    canvas._begin_plot_edit([0.3, 0.4])
    canvas._move_plot_edit([0.5, 0.6])
    canvas._publish_preview()
    canvas._move_plot_edit([0.6, 0.33])
    canvas._finish_plot_edit()
    assert [phase for phase, _ in records] == ["update", "update", "commit"]
    assert len({stroke_id for _, stroke_id in records}) == 1
    assert canvas.strokes[0]["path"] == [[0.3, 0.4], [0.6, 0.33]]


@pytest.mark.parametrize("tool", ["stroke", "point_stroke"])
def test_two_clicks_add_independent_exposure_points(canvas: ChromaWarpCanvas, tool: str) -> None:
    phases: list[str] = []
    canvas.edit_requested.connect(phases.append)
    canvas.tool = tool
    QTest.mouseClick(canvas, Qt.MouseButton.LeftButton, pos=QPoint(120, 160))
    canvas.set_parameter("exposure", 0.75)
    QTest.mousePress(canvas, Qt.MouseButton.LeftButton, pos=QPoint(260, 190))
    assert len(canvas.strokes) == 1
    QTest.mouseRelease(canvas, Qt.MouseButton.LeftButton, pos=QPoint(260, 190))
    assert phases == ["atomic", "atomic"]
    assert [stroke["mode"] for stroke in canvas.strokes] == [tool, tool]
    assert [stroke["parameters"]["exposure"] for stroke in canvas.strokes] == [0.75, 0.0]
    assert all(stroke["path"][0] == stroke["path"][1] for stroke in canvas.strokes)


def test_moving_an_exposure_point_preserves_its_single_position(canvas: ChromaWarpCanvas) -> None:
    start, target = QPoint(120, 160), QPoint(210, 200)
    QTest.mouseClick(canvas, Qt.MouseButton.LeftButton, pos=start)
    canvas.tool = "select"
    QTest.mousePress(canvas, Qt.MouseButton.LeftButton, pos=start)
    QTest.mouseRelease(canvas, Qt.MouseButton.LeftButton, pos=target)
    assert canvas.strokes[0]["path"] == [[0.525, 0.5], [0.525, 0.5]]
