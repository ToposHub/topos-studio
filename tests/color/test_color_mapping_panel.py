"""Tests for the combined Color Mapping panel."""

from PyQt6.QtWidgets import QWidget

from src.features.color.module import ColorModule
from src.features.color.ui.color_mapping_panel import ColorMappingPanel
from src.shared.panels.registry import PanelRegistry


def test_color_mapping_panel_uses_existing_child_panels(qapp):
    hsl_curves = QWidget()
    warper = QWidget()
    mixer = QWidget()

    panel = ColorMappingPanel(hsl_curves, warper, mixer)

    assert panel.tabs.count() == 3
    assert panel.tabs.widget(0) is hsl_curves
    assert panel.tabs.widget(1) is warper
    assert panel.tabs.widget(2) is mixer
    assert [panel.tabs.tabText(i) for i in range(3)] == [
        "HSL Curves",
        "Warper",
        "Mixer",
    ]


def test_color_mapping_panel_is_registered_as_one_color_panel():
    registry = PanelRegistry()
    ColorModule().register_panels(registry)

    mapping = registry.get("color.mapping")
    assert mapping is not None
    assert mapping.title_key == "panel.color_mapping"
    assert mapping.dock_attr == "dock_color_mapping"
    assert registry.get("color.hsl_curves") is None
    assert registry.get("color.mixer") is None
    assert registry.get("color.warper") is None
