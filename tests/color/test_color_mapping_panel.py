"""The retired Color Mapping container is replaced by two dock descriptors."""

import pytest

from src.features.color.module import ColorModule
from src.shared.panels.registry import PanelRegistry


def test_warper_and_mixer_are_registered_as_independent_panels() -> None:
    registry = PanelRegistry()
    ColorModule().register_panels(registry)

    assert registry.get("color.mapping") is None
    warper = registry.get("color.warper")
    mixer = registry.get("color.mixer")
    assert warper is not None and mixer is not None
    assert (warper.title_key, warper.dock_attr) == ("panel.warper", "dock_warper")
    assert (mixer.title_key, mixer.dock_attr) == ("panel.mixer", "dock_mixer")
    assert warper.page_ids == mixer.page_ids == ("Color",)
    assert registry.get("color.hsl_curves") is None


@pytest.mark.parametrize("work_mode", ["all", "image", "video"])
def test_independent_color_tools_remain_available_in_both_work_modes(work_mode: str) -> None:
    registry = PanelRegistry()
    ColorModule().register_panels(registry)
    panels = registry.get_panels_by_category(enabled_features={"color"}, work_mode=work_mode)
    ids = {panel.panel_id for panel in panels["Color"]}
    assert {"color.warper", "color.mixer"} <= ids
    assert "color.mapping" not in ids
