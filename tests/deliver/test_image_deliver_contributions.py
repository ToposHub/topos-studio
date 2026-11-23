"""Deliver contributes only image export surfaces to the image workspace."""
from __future__ import annotations

from unittest.mock import Mock

import pytest

from src.app.app_context import AppContext
from src.features.deliver.module import DeliverModule
from src.shared.actions import ActionRegistry
from src.shared.panels import PanelRegistry


def _module(monkeypatch: pytest.MonkeyPatch, product: str, mode: str) -> DeliverModule:
    monkeypatch.setenv("TOPOS_PRODUCT_MODE", product)
    module = DeliverModule()
    module._available = True
    module.register_pages(AppContext(config_manager={"work_mode": mode}))
    return module


@pytest.mark.parametrize("product,mode,image", [
    ("studio", "image", True), ("studio", "video", False),
    ("color", "video", True), ("edit", "image", False),
])
def test_deliver_menu_and_shortcut_contributions_follow_the_active_workspace(
    monkeypatch: pytest.MonkeyPatch, product: str, mode: str, image: bool,
) -> None:
    module = _module(monkeypatch, product, mode)
    actions = ActionRegistry()
    module.register_menu_actions(actions)
    shortcuts = Mock()
    module.register_shortcuts(shortcuts)
    menu_ids = {action.action_id for action in actions.get_actions()}
    shortcut_ids = {call.args[0] for call in shortcuts.register.call_args_list}
    if image:
        assert menu_ids == {"deliver.export", "deliver.export.batch"}
        assert shortcut_ids == menu_ids
        batch = actions.get_action("deliver.export.batch")
        assert batch.menu_path == "Deliver"
        assert batch.callback_name == "on_export_settings"
    else:
        assert menu_ids == {
            "deliver.export", "deliver.export.current_source", "deliver.export.settings",
            "deliver.export.timeline", "deliver.export.composite", "deliver.export.srt",
            "deliver.queue.start", "deliver.queue.stop",
        }
        assert shortcut_ids == menu_ids - {"deliver.export.settings", "deliver.export.srt"}


def test_deliver_image_choice_stays_frozen_until_restart(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TOPOS_PRODUCT_MODE", "studio")
    config = {"work_mode": "image"}
    module = DeliverModule()
    module._available = True
    module.register_pages(AppContext(config_manager=config))
    config["work_mode"] = "video"
    actions = ActionRegistry()
    module.register_menu_actions(actions)
    assert {action.action_id for action in actions.get_actions()} == {
        "deliver.export", "deliver.export.batch"}


def test_image_and_video_export_panels_have_separate_mode_scopes(monkeypatch: pytest.MonkeyPatch) -> None:
    module = _module(monkeypatch, "studio", "image")
    registry = PanelRegistry()
    module.register_panels(registry)
    image = registry.get_panels_by_category({"deliver"}, "image")["Deliver"]
    video = registry.get_panels_by_category({"deliver"}, "video")["Deliver"]
    assert [(panel.panel_id, panel.dock_attr) for panel in image] == [("deliver.export", "dock_export")]
    assert [(panel.panel_id, panel.dock_attr) for panel in video] == [("deliver.batch_export", "dock_deliver")]


def test_unavailable_deliver_registers_no_export_surfaces(monkeypatch: pytest.MonkeyPatch) -> None:
    module = _module(monkeypatch, "studio", "image")
    module._available = False
    actions = ActionRegistry()
    panels = PanelRegistry()
    shortcuts = Mock()
    module.register_menu_actions(actions)
    module.register_panels(panels)
    module.register_shortcuts(shortcuts)
    assert actions.get_actions() == []
    assert panels.get_all() == []
    shortcuts.register.assert_not_called()
