"""The Color workspace title rows share the wheel's geometry and icon states."""

import pytest
from PyQt6.QtCore import QEvent, QPointF, QRect, QSize, Qt
from PyQt6.QtGui import QColor, QEnterEvent, QFont, QPalette, QRegion
from PyQt6.QtWidgets import QApplication, QGroupBox, QLabel, QWidget

from src.features.color.core.layer import MaskShape
from src.features.color.ui.blur_sharpen_panel import BlurSharpenPanel
from src.features.color.ui.color_wheel import GradingPanel
from src.features.color.ui.crop_panel import CropPanel
from src.features.color.ui.curves_panel import CurvesPanel
from src.features.color.ui.denoise_panel import DenoisePanel
from src.features.color.ui.film_grain_panel import FilmGrainPanel
from src.features.color.ui.layer_panel import LayerPanel
from src.features.color.ui.mixer_panel import MixerPanel
from src.features.color.ui.node_editor_panel import ColorNodePanel
from src.features.color.ui.panel_chrome import ColorParameterRow, PARAMETER_ROW_HEIGHT
from src.features.color.ui.power_window_panel import PowerWindowPanel
from src.features.color.ui.qualifier_panel import QualifierPanel
from src.features.color.ui.raw_panel import RawPanel
from src.features.color.ui.scope_panel import ScopePanel
from src.features.color.ui.tracker_panel import TrackerPanel
from src.features.color.ui.warper_panel import WarperPanel
from src.shared.ui.style_sheet import C, DARK_THEME
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


def test_raw_has_continuous_dividers_and_even_parameter_spacing(qtbot) -> None:
    panel = RawPanel()
    qtbot.addWidget(panel)
    panel.setStyleSheet(DARK_THEME + panel.styleSheet())
    panel.set_params({"enabled": True, "wb_type": "custom"})
    panel.resize(658, panel.minimumSizeHint().height())
    panel.show()

    assert not panel.findChildren(QGroupBox)
    titles = panel.findChildren(QLabel, "colorParameterSectionTitle")
    dividers = panel.findChildren(QWidget, "colorParameterDivider")
    header = panel.findChild(QWidget, "colorToolHeader")
    assert header.height() == 30
    assert panel.reset_btn.iconSize() == QSize(14, 14)
    assert len(titles) == 3
    assert len(dividers) == 2
    for title in titles:
        layout = title.parentWidget().layout()
        # The final layout item is the stretch below the parameter rows.
        for index in range(1, layout.count() - 1):
            previous = layout.itemAt(index - 1).geometry()
            current = layout.itemAt(index).geometry()
            assert current.top() - previous.bottom() - 1 == 6
    for divider in dividers:
        assert divider.size() == QSize(1, panel.height() - header.height())
        assert divider.grab().toImage().pixelColor(0, 0) == QColor(C.PANEL_DIVIDER)
    for row in (panel.temp_control, panel.contrast_control):
        assert row.height() == PARAMETER_ROW_HEIGHT
        assert row.label.font().pixelSize() < titles[0].font().pixelSize()
        role = QPalette.ColorRole.WindowText
        assert row.label.palette().color(role).lightness() < titles[0].palette().color(role).lightness()
        assert panel.rect().contains(QRect(row.mapTo(panel, row.rect().topLeft()), row.size()))


def test_grain_orders_options_before_collapsible_parameter_sections(qtbot) -> None:
    panel = FilmGrainPanel()
    qtbot.addWidget(panel)
    panel.setStyleSheet(DARK_THEME + panel.styleSheet())
    panel.resize(390, 280)
    panel.show()

    assert not panel.findChildren(QGroupBox)
    assert panel.reset_btn.iconSize() == QSize(14, 14)
    dividers = panel.findChildren(QWidget, "colorParameterDivider")
    assert len(dividers) == 2
    for divider in dividers:
        assert divider.size() == QSize(panel.width(), 1)
        assert divider.grab().toImage().pixelColor(0, 0) == QColor(C.PANEL_DIVIDER)

    def y(widget: QWidget) -> int:
        return widget.mapTo(panel, widget.rect().topLeft()).y()

    assert y(panel.animated_check) < y(panel.grain_section_toggle)
    assert y(panel.grain_section_toggle) < y(panel.size_control) < y(panel.intensity_control)
    assert panel.grain_section_toggle.isChecked()
    assert panel.size_control.isVisible()
    assert panel.size_control.height() == PARAMETER_ROW_HEIGHT
    assert not panel.shadows_control.isVisible()

    panel.set_params({"shadows": 0.35})
    panel.response_section_toggle.click()
    assert panel.shadows_control.isVisible()
    assert panel.shadows_control.value() == 0.35
    assert y(panel.shadows_control) < y(panel.midtones_control) < y(panel.highlights_control)
    assert panel.highlights_control.height() == PARAMETER_ROW_HEIGHT
    panel.response_section_toggle.click()
    assert not panel.shadows_control.isVisible()
    assert panel._build_params()["shadows"] == 0.35


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


def test_mixer_switches_and_resets_modes_from_one_compact_header(qtbot) -> None:
    panel = MixerPanel()
    qtbot.addWidget(panel)
    panel.setStyleSheet(DARK_THEME)
    panel.set_params({"red": [10, 20, 30]})
    panel.resize(320, 270)
    panel.show()

    header = panel.findChild(QWidget, "colorToolHeader")
    assert header.height() == 30
    assert panel.findChildren(QWidget, "colorToolHeader") == [header]
    assert panel.reset_btn.parentWidget() is header
    assert panel.reset_btn.iconSize() == QSize(14, 14)
    assert panel._mode_group.checkedId() == 0

    changed = []
    panel.mixer_changed.connect(changed.append)
    for index, page in enumerate((panel.hue_tab, panel.sat_tab, panel.lum_tab)):
        button = panel._mode_group.button(index)
        assert button.parentWidget() is header
        assert button.iconSize() == QSize(20, 20)
        pixmap = button.icon().pixmap(button.iconSize(), 2.0)
        assert pixmap.size() == QSize(40, 40)
        assert QRegion(pixmap.mask()).boundingRect().height() >= 30
        qtbot.mouseClick(button, Qt.MouseButton.LeftButton)
        assert panel._mode_group.checkedId() == index
        assert panel._pages.currentWidget() is page
        assert panel._mode_title.text() == button.accessibleName()
        assert button.accessibleName() in panel.reset_btn.toolTip()

        red = page.findChildren(ColorParameterRow)[0]
        yellow = page.findChildren(ColorParameterRow)[2]
        assert red.mapTo(panel, QPointF()).y() == header.height() + 16
        assert yellow.y() - red.y() == PARAMETER_ROW_HEIGHT + 7
        assert red.value() == (index + 1) * 10

    assert changed == []
    qtbot.mouseClick(panel.reset_btn, Qt.MouseButton.LeftButton)
    assert [params["red"] for params in changed] == [[10, 20, 0]]


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


def _numeric_field_background(field: QWidget) -> str:
    image = field.grab().toImage()
    scale = image.devicePixelRatio()
    return image.pixelColor(round(6 * scale), round(2 * scale)).name()


def test_qualifier_and_window_use_the_wheel_numeric_field(qtbot) -> None:
    wheel = GradingPanel()
    qualifier = QualifierPanel()
    window = PowerWindowPanel()
    shape = MaskShape("circle")
    window.set_shapes([shape])
    window.select_shape(shape.id)
    values = (
        (wheel, wheel.temp_slider.spin),
        (qualifier, qualifier.hue_center_ctrl.spin),
        (window, window.controls["size"]),
    )
    for panel, value in values:
        qtbot.addWidget(panel)
        panel.setStyleSheet(DARK_THEME + panel.styleSheet())
        panel.resize(700, 392)
        panel.show()
        assert value.isEnabled()
        display = value._value_label
        assert display.height() == 16
        assert display.font().weight() == QFont.Weight.Medium
        assert _numeric_field_background(display) == "#232325"

        qtbot.mouseDClick(value, Qt.MouseButton.LeftButton)
        editor = value._edit
        assert editor.isVisible()
        assert editor.height() == display.height()
        assert editor.font().weight() == display.font().weight()
        editor.deselect()
        assert _numeric_field_background(editor) == "#3a3a40"
        qtbot.keyClick(editor, Qt.Key.Key_Escape)

        value.setDisabled(True)
        assert _numeric_field_background(display) == "#1c1c1e"


def test_crop_native_number_input_renders_the_same_numeric_field(qtbot) -> None:
    panel = CropPanel()
    qtbot.addWidget(panel)
    panel.setStyleSheet(DARK_THEME + panel.styleSheet())
    panel.set_crop_active(True)
    panel.aspect_combo.setCurrentIndex(panel.aspect_combo.findText("Custom"))
    panel.show()
    panel.activateWindow()
    field = panel.width_spin
    panel.aspect_combo.setFocus()
    assert field.isVisible()
    assert field.height() == 16
    assert field.lineEdit().font().weight() == QFont.Weight.Medium
    assert _numeric_field_background(field) == "#232325"

    field.setFocus()
    qtbot.waitUntil(field.hasFocus)
    field.lineEdit().deselect()
    assert _numeric_field_background(field) == "#3a3a40"
    field.setDisabled(True)
    assert _numeric_field_background(field) == "#1c1c1e"


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
