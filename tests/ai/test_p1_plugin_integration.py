"""P1 integration: real chat routing passes through the shared plugin gate."""

from __future__ import annotations

from unittest.mock import MagicMock

from src.app.controllers.ai_editing_mixin import build_p1_dependencies
from src.features.ai_harness.runtime import HarnessRuntime
from src.features.ai_harness.plugin_persistence import PluginSnapshotStore


def test_p1_dependencies_project_skills_into_plugin_registry_and_route_gate() -> None:
    deps = build_p1_dependencies(
        service=MagicMock(),
        runtime=HarnessRuntime(),
    )
    registry = deps["plugin_registry"]
    host = deps["skill_plugin_host"]
    router = deps["router"]

    assert registry.list()
    assert all(record.state.value == "active" for record in registry.list())
    routed = router.route("自动字幕")
    assert routed.matched is True
    assert routed.skill_id == "topos.auto_caption"

    assert host.deactivate("topos.auto_caption")
    blocked = router.route("自动字幕")
    assert blocked.matched is False
    assert "未安装或未启用" in blocked.clarify_question


def test_skill_project_restore_syncs_skill_registry_and_requires_activation() -> None:
    deps = build_p1_dependencies(
        service=MagicMock(),
        runtime=HarnessRuntime(),
    )
    registry = deps["plugin_registry"]
    host = deps["skill_plugin_host"]
    router = deps["router"]
    snapshot = PluginSnapshotStore.export(registry)
    saved_row = next(
        row for row in snapshot["plugins"]
        if row["manifest"]["plugin_id"] == "composition.skill.topos.auto_caption"
    )

    assert saved_row["state"] == "active"
    assert host.restore_project_state("topos.auto_caption", saved_row["state"])
    assert registry.get("composition.skill.topos.auto_caption").state.value == "enabled"
    assert host.skill_registry.is_enabled("topos.auto_caption") is False
    assert router.route("自动字幕").matched is False

    assert host.activate("topos.auto_caption")
    assert router.route("自动字幕").matched is True
