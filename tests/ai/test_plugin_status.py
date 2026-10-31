"""P7 plugin card/Run Drawer status projection tests."""

from __future__ import annotations

from src.features.ai_harness.plugin_runtime import (
    PluginCapability,
    PluginHealth,
    PluginManifest,
    PluginRegistry,
    PluginSource,
    PluginState,
)
from src.features.ai_harness.plugin_status import (
    PluginEventJournal,
    PluginStatusProjection,
)


class _FailingPlugin:
    def activate(self, context) -> None:
        return None

    def deactivate(self) -> None:
        return None

    def health(self) -> PluginHealth:
        return PluginHealth(status="healthy")

    def invoke(self, tool_name, args, *, approval=None):
        raise RuntimeError("api_key=sk-1234567890123456")


def _manifest(*, plugin_id: str = "topos.status", api_version: str = "1.0") -> PluginManifest:
    return PluginManifest(
        plugin_id=plugin_id,
        display_name="Status plugin",
        version="2.3.4",
        api_version=api_version,
        source=PluginSource.LOCAL_DEV,
        capabilities=(PluginCapability(
            capability_id="status.inspect",
            description="Inspect status",
            tool_names=("topos.status.inspect",),
            permission="write",
        ),),
    )


def test_projection_exposes_manifest_permissions_and_unverified_source() -> None:
    registry = PluginRegistry()
    manifest = _manifest()
    assert registry.register(manifest)

    view = PluginStatusProjection.one(registry, manifest.plugin_id)

    assert view is not None
    assert view.version == "2.3.4"
    assert view.source == "local_dev"
    assert view.unverified is True
    assert view.permissions == ("write",)
    assert view.requires_approval is True
    assert view.capabilities[0].tool_names == ("topos.status.inspect",)
    assert view.can_activate is True
    assert any(item.code == "PLUGIN_UNVERIFIED_SOURCE" for item in view.diagnostics)


def test_projection_marks_incompatible_api_and_filters_disabled_plugins() -> None:
    registry = PluginRegistry()
    incompatible = _manifest(plugin_id="topos.incompatible", api_version="9.0")
    disabled = _manifest(plugin_id="topos.disabled")
    assert registry.register(incompatible)
    assert registry.register(disabled, enabled=False)

    incompatible_view = PluginStatusProjection.one(registry, incompatible.plugin_id)
    assert incompatible_view is not None
    assert incompatible_view.api_compatible is False
    assert incompatible_view.runnable is False
    assert incompatible_view.can_activate is False
    assert any(item.code == "PLUGIN_API_INCOMPATIBLE" for item in incompatible_view.diagnostics)

    active_rows = PluginStatusProjection.project(registry, include_disabled=False)
    assert [row.plugin_id for row in active_rows] == [incompatible.plugin_id]
    assert PluginStatusProjection.one(registry, "topos.missing") is None


def test_event_journal_feeds_recent_failure_into_projection_without_secrets() -> None:
    journal = PluginEventJournal(max_events=4)
    registry = PluginRegistry(event_sink=journal.record)
    manifest = _manifest()
    plugin = _FailingPlugin()
    assert registry.register(manifest, plugin)
    assert registry.activate(manifest.plugin_id)

    result = registry.invoke_tool(
        manifest.plugin_id,
        "topos.status.inspect",
        approval={"approved": True},
        run_id="run-status-1",
    )
    assert result["ok"] is False

    view = PluginStatusProjection.one(
        registry,
        manifest.plugin_id,
        event_journal=journal,
    )
    assert view is not None
    assert view.state == PluginState.ACTIVE.value
    assert view.last_failure_kind == "plugin.tool_failed"
    assert "<redacted-secret>" in view.last_failure
    assert "sk-1234567890123456" not in view.last_failure
    assert view.status_level == "warning"
    assert journal.recent(plugin_id=manifest.plugin_id, limit=1)[0].kind == "plugin.tool_failed"
    assert journal.recent(run_id="run-status-1", limit=1)[0].kind == "plugin.tool_failed"
