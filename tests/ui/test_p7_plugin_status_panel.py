"""P7 UI integration: the assistant panel renders plugin view models only."""

from __future__ import annotations

import pytest

from src.features.ai_harness.plugin_runtime import PluginManifest, PluginRegistry, PluginSource
from src.features.ai_harness.plugin_status import PluginStatusProjection
from src.features.edit.ui.ai_edit_assistant_panel import AiEditAssistantPanel


pytestmark = pytest.mark.edit_core


def test_assistant_panel_renders_and_clears_plugin_status(qapp) -> None:
    panel = AiEditAssistantPanel()
    registry = PluginRegistry()
    manifest = PluginManifest(
        plugin_id="topos.ui.status",
        display_name="UI status plugin",
        source=PluginSource.LOCAL_DEV,
    )
    assert registry.register(manifest)

    panel.set_plugin_status(PluginStatusProjection.project(registry))
    assert not panel._plugin_status_strip.isHidden()
    # P1-UI-02 规格裁决：状态条文案经 _trf("ai_assistant.plugin_status") 渲染，
    # 测试锚定 i18n 默认格式（Plugins {total} / unverified {n}），
    # 不依赖某个 locale 的翻译文案。
    assert "Plugins 1" in panel._plugin_status_label.text()
    assert "unverified 1" in panel._plugin_status_label.text()

    panel.set_plugin_status(())
    assert panel._plugin_status_strip.isHidden()
