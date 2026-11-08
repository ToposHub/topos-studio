"""Vector monitor icon coverage and native QIcon state rendering."""

from __future__ import annotations

import pytest
from PyQt6.QtCore import QSize
from PyQt6.QtGui import QIcon

from src.features.edit.ui.viewport_icons import VIEWER_ICON_NAMES, viewer_icon


@pytest.mark.parametrize("name", sorted(VIEWER_ICON_NAMES))
def test_each_viewer_glyph_draws_visible_pixels(qapp, name: str) -> None:
    image = viewer_icon(name).pixmap(QSize(20, 20)).toImage()
    assert any(image.pixelColor(x, y).alpha() > 0 for x in range(20) for y in range(20))


def test_viewer_icon_modes_and_checked_state_use_consistent_neutral_color(qapp) -> None:
    icon = viewer_icon("stop")
    normal = icon.pixmap(QSize(20, 20), QIcon.Mode.Normal, QIcon.State.Off).toImage()
    hover = icon.pixmap(QSize(20, 20), QIcon.Mode.Active, QIcon.State.Off).toImage()
    disabled = icon.pixmap(QSize(20, 20), QIcon.Mode.Disabled, QIcon.State.Off).toImage()
    checked = icon.pixmap(QSize(20, 20), QIcon.Mode.Normal, QIcon.State.On).toImage()
    assert normal.pixelColor(10, 10).name() == "#969699"
    assert hover.pixelColor(10, 10).name() == "#d2d2d6"
    assert disabled.pixelColor(10, 10).name() == "#555559"
    assert checked.pixelColor(10, 10).name() == "#d2d2d6"


def test_high_dpi_viewer_icon_keeps_logical_size(qapp) -> None:
    pixmap = viewer_icon("dual_view").pixmap(QSize(20, 20), 2.0)
    assert pixmap.width() == 40
    assert pixmap.devicePixelRatio() == 2.0


def test_unknown_viewer_icon_reports_wiring_error(qapp) -> None:
    with pytest.raises(ValueError, match="Unknown viewer icon"):
        viewer_icon("does_not_exist")
