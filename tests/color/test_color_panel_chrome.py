"""The Color workspace title rows share the wheel's geometry and icon states."""

import pytest
from PyQt6.QtCore import QEvent, QPointF, QRect, QSize, Qt
from PyQt6.QtGui import QEnterEvent
from PyQt6.QtWidgets import QApplication, QWidget

from src.features.color.ui.blur_sharpen_panel import BlurSharpenPanel
from src.features.color.ui.color_wheel import GradingPanel
from src.features.color.ui.crop_panel import CropPanel
from src.features.color.ui.curves_panel import CurvesPanel
from src.features.color.ui.denoise_panel import DenoisePanel
from src.features.color.ui.layer_panel import LayerPanel
from src.features.color.ui.node_editor_panel import ColorNodePanel
from src.features.color.ui.power_window_panel import PowerWindowPanel
from src.features.color.ui.qualifier_panel import QualifierPanel
from src.features.color.ui.scope_panel import ScopePanel
from src.features.color.ui.tracker_panel import TrackerPanel
from src.features.color.ui.warper_panel import WarperPanel
from src.shared.ui.style_sheet import DARK_THEME
from src.shared.ui.widgets.curves_widget import CurvesWidget


@pytest.mark.parametrize(("factory", "name", "control"), [
    (GradingPanel, "colorToolHeader", "reset_all_btn"),
    (lambda: CurvesPanel(CurvesWidget()), "colorToolHeader", "reset_curves_btn"),
    (QualifierPanel, "qualifierHeader", "reset_all_btn"),
    (WarperPanel, "colorToolHeader", "reset_btn"),
    (BlurSharpenPanel, "colorToolHeader", "reset_btn"),
    (DenoisePanel, "colorToolHeader", "reset_btn"),
    (lambda: DenoisePanel(image_mode=True), "colorToolHeader", "reset_btn"),
    (CropPanel, "colorToolHeader", "activate_btn"),
    (PowerWindowPanel, "window_title", "reset_button"),
    (TrackerPanel, "tracker_title_bar", "btn_clear"),
    (LayerPanel, "colorLayersHeader", "settings_btn"),
    (ScopePanel, "scopeHeader", "settings_btn"),
    (ColorNodePanel, "ColorNodeToolbar", "pointer_button"),
])
def test_title_rows_have_identical_height_and_icon_size(qtbot, factory, name, control) -> None:
    panel = factory()
    qtbot.addWidget(panel)
    panel.setStyleSheet(DARK_THEME + panel.styleSheet())
    panel.resize(700, 392)
    panel.show()
    button = getattr(panel, control)
    header = panel.findChild(QWidget, name)
    assert header.isAncestorOf(button)
    assert header.height() == 30
    assert button.iconSize() == QSize(14, 14)


def test_wheel_icons_render_bright_when_checked_without_a_background_box(qtbot) -> None:
    panel = GradingPanel()
    qtbot.addWidget(panel)
    panel.setStyleSheet(DARK_THEME)
    panel.resize(700, 392)
    panel.show()

    # Check a painter-drawn glyph and a QIcon-backed toggle in the same title row.
    for button in (panel._mode_buttons["bars"], panel.keyframe_mode_btn):
        header = button.parentWidget()
        rect = button.geometry()
        button.setChecked(False)
        idle = header.grab().toImage()
        ratio = idle.devicePixelRatio()
        bounds = QRect(round(rect.x() * ratio), round(rect.y() * ratio),
                       round(rect.width() * ratio), round(rect.height() * ratio))
        idle = idle.copy(bounds)
        button.setChecked(True)
        active = header.grab().toImage().copy(bounds)
        assert idle.pixelColor(2, 2).name() == active.pixelColor(2, 2).name() == "#202024"
        assert max(active.pixelColor(x, y).lightness()
                   for y in range(active.height()) for x in range(active.width())) >= 220
        assert max(idle.pixelColor(x, y).lightness()
                   for y in range(idle.height()) for x in range(idle.width())) < 195


def test_qualifier_renders_the_three_color_surface_levels(qtbot) -> None:
    panel = QualifierPanel()
    qtbot.addWidget(panel)
    panel.setStyleSheet(DARK_THEME + panel.styleSheet())
    panel.resize(700, 392)
    panel.show()

    for name, expected in (
        ("qualifierHeader", "#202024"),
        ("qualifierTools", "#2c2d31"),
        ("qualifierRanges", "#27272b"),
        ("qualifierFinesse", "#27272b"),
    ):
        surface = panel.findChild(QWidget, name)
        image = surface.grab().toImage()
        scale = image.devicePixelRatio()
        point = (round((surface.width() - 3) * scale), round(3 * scale))
        assert image.pixelColor(*point).name() == expected


def test_reset_buttons_share_dimensions_and_pointer_feedback(qtbot) -> None:
    panel = GradingPanel()
    qtbot.addWidget(panel)
    panel.setStyleSheet(DARK_THEME)
    panel.resize(700, 392)
    panel.show()

    for button, size, glyph_size, background in (
        (panel.reset_all_btn, QSize(24, 26), QSize(14, 14), "#202024"),
        (panel.lift_wheel.reset_btn, QSize(16, 16), QSize(11, 11), "#27272b"),
    ):
        assert button.size() == size
        assert button.iconSize() == glyph_size

        def glyph_lightness() -> int:
            parent_image = button.parentWidget().grab().toImage()
            ratio = parent_image.devicePixelRatio()
            bounds = QRect(round(button.x() * ratio), round(button.y() * ratio),
                           round(button.width() * ratio), round(button.height() * ratio))
            image = parent_image.copy(bounds)
            assert image.pixelColor(1, 1).name() == background
            return max(image.pixelColor(x, y).lightness()
                       for y in range(image.height()) for x in range(image.width()))

        QApplication.sendEvent(button, QEvent(QEvent.Type.Leave))
        idle = glyph_lightness()
        center = button.rect().center()
        QApplication.sendEvent(button, QEnterEvent(QPointF(center), QPointF(button.mapTo(panel, center)),
                                                   QPointF(button.mapToGlobal(center))))
        hover = glyph_lightness()
        qtbot.mousePress(button, Qt.MouseButton.LeftButton, pos=center)
        pressed = glyph_lightness()
        qtbot.mouseRelease(button, Qt.MouseButton.LeftButton, pos=center)
        assert idle < hover < pressed
        assert glyph_lightness() == hover
        QApplication.sendEvent(button, QEvent(QEvent.Type.Leave))
        assert glyph_lightness() == idle
