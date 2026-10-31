"""P1 smoke test: an active plugin contributes one tool to the Facade."""

from __future__ import annotations

from src.features.ai_harness.plugin_runtime import (
    PluginCapability,
    PluginHealth,
    PluginManifest,
    PluginRegistry,
)
from src.features.edit.ai_editing.tool_facade import ToolFacade, ToolPermission


class _EchoPlugin:
    def activate(self, context) -> None:
        return None

    def deactivate(self) -> None:
        return None

    def health(self) -> PluginHealth:
        return PluginHealth("healthy")

    def invoke(self, tool_name, args, *, approval=None):
        return {"ok": True, "data": {"tool_name": tool_name, "args": dict(args)}}


def test_active_plugin_tool_is_advertised_and_routed() -> None:
    registry = PluginRegistry()
    manifest = PluginManifest(
        plugin_id="topos.echo",
        display_name="Echo",
        capabilities=(PluginCapability(
            capability_id="echo.read",
            description="return a structured echo",
            tool_names=("topos.plugin.echo",),
            permission="read",
            input_schema={"type": "object"},
            metadata={"domain": "system"},
        ),),
    )
    assert registry.register(manifest, _EchoPlugin())
    assert registry.activate(manifest.plugin_id)

    facade = ToolFacade(None, plugin_registry=registry)
    definition = next(
        tool for tool in facade.list_tools(max_permission=ToolPermission.READ)
        if tool.name == "topos.plugin.echo"
    )
    result = facade.invoke("topos.plugin.echo", {"value": "ok"})

    assert definition.permission == ToolPermission.READ
    assert result["ok"] is True
    assert result["data"]["args"]["value"] == "ok"
    assert facade.capability_catalog.get("topos.plugin.echo") is not None

    assert registry.deactivate(manifest.plugin_id)
    assert "topos.plugin.echo" not in {
        tool.name for tool in facade.list_tools(max_permission=ToolPermission.READ)
    }
    assert facade.capability_catalog.get("topos.plugin.echo") is None
