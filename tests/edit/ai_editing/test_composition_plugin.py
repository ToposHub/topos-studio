"""P5 Agent Preset plugin gate tests."""

from __future__ import annotations

from types import SimpleNamespace

from src.features.ai_harness.plugin_runtime import PluginSource
from src.features.ai_harness.subagent_runtime import (
    SubagentDefinition,
    SubagentError,
    SubagentRegistry,
    SubagentSupervisor,
)
from src.features.edit.ai_editing.agent_presets import AgentPreset, AgentPresetRegistry
from src.features.edit.ai_editing.composition_plugin import (
    AgentPresetPluginHost,
    SkillPluginHost,
    SubagentPluginHost,
)
from src.features.edit.ai_editing.skill_manifest import SkillManifest, SkillSource, ToolPolicy
from src.features.edit.ai_editing.skill_registry import SkillRegistry


def _preset(preset_id: str = "editorial") -> AgentPreset:
    return AgentPreset(
        preset_id=preset_id,
        display_name="Editorial preset",
        tool_allowlist=("topos.get_project_snapshot",),
        max_iterations=2,
        max_tool_calls=3,
        max_parallel_tool_calls=1,
    )


def test_preset_is_visible_only_when_its_plugin_is_active() -> None:
    registry = AgentPresetRegistry((_preset(),))
    host = AgentPresetPluginHost(registry)
    assert host.register_existing() == 1
    assert host.get("editorial") == _preset()
    assert host.list() == (_preset(),)

    assert host.deactivate("editorial")
    assert host.get("editorial") is None
    assert host.list() == ()
    assert host.activate("editorial")
    assert host.get("editorial") == _preset()

    snapshot = host.snapshot()
    assert snapshot["presets"][0]["preset_id"] == "editorial"
    assert snapshot["plugin_registry"]["plugins"][0]["state"] == "active"


def test_local_preset_is_marked_unverified_and_disabled_by_default() -> None:
    registry = AgentPresetRegistry()
    host = AgentPresetPluginHost(registry)
    preset = _preset("local-preset")
    assert host.register(preset, source=PluginSource.LOCAL_DEV, enabled=False)
    assert host.get(preset.preset_id) is None
    record = host.plugin_registry.get(host.plugin_id_for(preset.preset_id))
    assert record is not None
    assert record.manifest.unverified
    assert host.enable(preset.preset_id)
    assert host.activate(preset.preset_id)
    assert host.get(preset.preset_id) == preset


def test_skill_plugin_gate_controls_tool_discovery() -> None:
    skill_registry = SkillRegistry(register_official=False)
    host = SkillPluginHost(skill_registry)
    manifest = SkillManifest(
        skill_id="custom.skill",
        title="Custom skill",
        source=SkillSource.LOCAL_DEV,
        tool_policy=ToolPolicy(allowed_tools=("topos.search_assets",)),
    )
    assert host.register(manifest)
    tool = SimpleNamespace(name="topos.search_assets", permission="read")
    assert host.tools_for(manifest.skill_id, [tool]) == [tool]
    assert host.deactivate(manifest.skill_id)
    assert host.tools_for(manifest.skill_id, [tool]) == []
    assert host.activate(manifest.skill_id)
    assert host.tools_for(manifest.skill_id, [tool]) == [tool]


def test_subagent_plugin_gate_preserves_supervisor_lineage_limits() -> None:
    supervisor = SubagentSupervisor(SubagentRegistry())
    host = SubagentPluginHost(supervisor)
    definition = SubagentDefinition(
        agent_id="researcher",
        display_name="Researcher",
        runner=lambda request: "evidence",
        continuable=True,
    )
    assert host.register(definition)
    result = host.run(
        parent_run_id="parent-1",
        session_id="session-1",
        agent_id="researcher",
        prompt="find evidence",
    )
    assert result.status == "succeeded"
    assert result.lineage == ("parent-1",)
    assert host.deactivate("researcher")
    try:
        host.continue_child(result.child_run_id, "continue")
    except SubagentError as exc:
        assert "not active" in str(exc)
    else:
        raise AssertionError("inactive subagent plugin must be rejected")
