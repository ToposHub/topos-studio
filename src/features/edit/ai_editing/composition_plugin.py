"""Plugin lifecycle gate for declarative Agent Presets."""

from __future__ import annotations

from typing import Any

from src.features.ai_harness.plugin_runtime import (
    PLUGIN_API_VERSION,
    PluginCapability,
    PluginManifest,
    PluginRegistry,
    PluginSource,
)
from src.features.edit.ai_editing.agent_presets import AgentPreset, AgentPresetRegistry
from src.features.edit.ai_editing.skill_manifest import SkillManifest, SkillSource
from src.features.edit.ai_editing.skill_registry import SkillRegistry
from src.features.ai_harness.subagent_runtime import (
    SubagentDefinition,
    SubagentError,
    SubagentResult,
    SubagentSupervisor,
)


class AgentPresetPluginHost:
    """Expose presets through the plugin kernel without duplicating their policy."""

    def __init__(
        self,
        preset_registry: AgentPresetRegistry,
        *,
        plugin_registry: PluginRegistry | None = None,
    ) -> None:
        self.preset_registry = preset_registry
        self.plugin_registry = plugin_registry or PluginRegistry()
        self._presets: dict[str, AgentPreset] = {}

    def register(
        self,
        preset: AgentPreset,
        *,
        source: PluginSource = PluginSource.BUILTIN,
        version: str = "1.0.0",
        enabled: bool = True,
    ) -> bool:
        plugin_id = self.plugin_id_for(preset.preset_id)
        manifest = PluginManifest(
            plugin_id=plugin_id,
            display_name=preset.display_name,
            version=version,
            api_version=PLUGIN_API_VERSION,
            source=source,
            description="declarative Agent Preset policy bundle",
            capabilities=(PluginCapability(
                capability_id=f"composition.preset.{preset.preset_id}",
                description=preset.display_name,
                permission="read",
                metadata={"preset": preset.to_dict()},
            ),),
            metadata={"preset": preset.to_dict()},
        )
        accepted = self.plugin_registry.register(manifest, enabled=enabled)
        if not accepted:
            return False
        self._presets[preset.preset_id] = preset
        if enabled and not self.plugin_registry.activate(plugin_id):
            self._presets.pop(preset.preset_id, None)
            return False
        return True

    def register_existing(self, *, source: PluginSource = PluginSource.BUILTIN) -> int:
        """Project an existing preset registry into active plugin records."""

        count = 0
        for preset in self.preset_registry.list():
            if self.register(preset, source=source):
                count += 1
        return count

    def activate(self, preset_id: str) -> bool:
        return self.plugin_registry.activate(self.plugin_id_for(preset_id))

    def enable(self, preset_id: str) -> bool:
        return self.plugin_registry.enable(self.plugin_id_for(preset_id))

    def deactivate(self, preset_id: str) -> bool:
        return self.plugin_registry.deactivate(self.plugin_id_for(preset_id))

    def is_active(self, preset_id: str) -> bool:
        record = self.plugin_registry.get(self.plugin_id_for(preset_id))
        return record is not None and record.state.value == "active"

    def get(self, preset_id: str) -> AgentPreset | None:
        if not self.is_active(preset_id):
            return None
        return self._presets.get(str(preset_id))

    def list(self) -> tuple[AgentPreset, ...]:
        return tuple(
            self._presets[preset_id]
            for preset_id in sorted(self._presets)
            if self.is_active(preset_id)
        )

    def snapshot(self) -> dict[str, Any]:
        return {
            "schema_version": "topos-agent-preset-plugin-host/1.0",
            "presets": [preset.to_dict() for preset in self.list()],
            "plugin_registry": self.plugin_registry.snapshot(),
        }

    @staticmethod
    def plugin_id_for(preset_id: str) -> str:
        return f"composition.preset.{preset_id}"


class SkillPluginHost:
    """Gate Skill discovery and tool filtering with plugin lifecycle state."""

    def __init__(
        self,
        skill_registry: SkillRegistry,
        *,
        plugin_registry: PluginRegistry | None = None,
    ) -> None:
        self.skill_registry = skill_registry
        self.plugin_registry = plugin_registry or PluginRegistry()
        self._skills: dict[str, SkillManifest] = {}

    def register(self, manifest: SkillManifest, *, enabled: bool = True) -> bool:
        if self.skill_registry.get(manifest.skill_id) is None:
            diagnostics = self.skill_registry.register(manifest, enabled=False)
            if diagnostics:
                return False
        plugin_manifest = PluginManifest(
            plugin_id=self.plugin_id_for(manifest.skill_id),
            display_name=manifest.title or manifest.skill_id,
            version=manifest.version,
            api_version=PLUGIN_API_VERSION,
            source=self._source(manifest.source),
            description=manifest.description,
            capabilities=(PluginCapability(
                capability_id=f"composition.skill.{manifest.skill_id}",
                description=manifest.title or manifest.skill_id,
                permission="read",
                metadata={"skill": manifest.to_dict()},
            ),),
            metadata={"skill": manifest.to_dict()},
        )
        accepted = self.plugin_registry.register(
            plugin_manifest,
            enabled=enabled,
        )
        if not accepted:
            return False
        self._skills[manifest.skill_id] = manifest
        if enabled:
            self.skill_registry.enable(manifest.skill_id)
            if not self.plugin_registry.activate(self.plugin_id_for(manifest.skill_id)):
                self._skills.pop(manifest.skill_id, None)
                self.skill_registry.disable(manifest.skill_id)
                return False
        return True

    def activate(self, skill_id: str) -> bool:
        skill_id = str(skill_id)
        if self.skill_registry.get(skill_id) is None:
            return False
        self.skill_registry.enable(skill_id)
        return self.plugin_registry.activate(self.plugin_id_for(skill_id))

    def deactivate(self, skill_id: str) -> bool:
        changed = self.plugin_registry.deactivate(self.plugin_id_for(skill_id))
        if changed:
            self.skill_registry.disable(str(skill_id))
        return changed

    def restore_project_state(self, skill_id: str, persisted_state: str) -> bool:
        """Restore a Skill lifecycle fact without implicitly reactivating it."""

        skill_id = str(skill_id)
        changed = self.plugin_registry.restore_project_state(
            self.plugin_id_for(skill_id), persisted_state,
        )
        if not changed:
            return False
        # Router and tool filtering must remain closed until the user or host
        # explicitly calls activate() after a project restore.
        self.skill_registry.disable(skill_id)
        return True

    def is_active(self, skill_id: str) -> bool:
        record = self.plugin_registry.get(self.plugin_id_for(skill_id))
        return record is not None and record.state.value == "active"

    def tools_for(self, skill_id: str, all_tools: list[Any]) -> list[Any]:
        if not self.is_active(skill_id):
            return []
        return self.skill_registry.tools_for(str(skill_id), all_tools)

    def validate_tool_call(self, skill_id: str, tool_name: str, tool_permission: str) -> list[Any]:
        if not self.is_active(skill_id):
            return []
        return self.skill_registry.validate_tool_call(skill_id, tool_name, tool_permission)

    @staticmethod
    def plugin_id_for(skill_id: str) -> str:
        return f"composition.skill.{skill_id}"

    @staticmethod
    def _source(source: SkillSource) -> PluginSource:
        if source is SkillSource.OFFICIAL:
            return PluginSource.BUILTIN
        if source is SkillSource.VERIFIED_THIRD_PARTY:
            return PluginSource.VERIFIED_THIRD_PARTY
        return PluginSource.LOCAL_DEV


class SubagentPluginHost:
    """Gate subagent admission while preserving supervisor lineage controls."""

    # AUD-13221：child_run_id → agent_id 映射的保留上限。超过后按插入序淘汰
    # 最旧的映射（对照同目录 ContextSpillStore 的 max_items+trim 设计），
    # 防止编辑器长会话高频派生子代理时字典无界增长。
    DEFAULT_MAX_CHILD_AGENTS = 256

    def __init__(
        self,
        supervisor: SubagentSupervisor,
        *,
        plugin_registry: PluginRegistry | None = None,
        max_child_agents: int = DEFAULT_MAX_CHILD_AGENTS,
    ) -> None:
        self.supervisor = supervisor
        self.plugin_registry = plugin_registry or PluginRegistry()
        self._definitions: dict[str, SubagentDefinition] = {}
        self._child_agents: dict[str, str] = {}
        if int(max_child_agents) <= 0:
            raise ValueError("max_child_agents must be positive")
        self._max_child_agents = int(max_child_agents)

    def register(self, definition: SubagentDefinition, *, enabled: bool = True) -> bool:
        manifest = PluginManifest(
            plugin_id=self.plugin_id_for(definition.agent_id),
            display_name=definition.display_name,
            api_version=PLUGIN_API_VERSION,
            source=PluginSource.LOCAL_DEV,
            description="bounded subagent definition",
            capabilities=(PluginCapability(
                capability_id=f"composition.subagent.{definition.agent_id}",
                description=definition.display_name,
                permission="read",
                metadata={"continuable": definition.continuable},
            ),),
            metadata={"agent_id": definition.agent_id, "continuable": definition.continuable},
        )
        if not self.supervisor.registry.register(definition):
            return False
        accepted = self.plugin_registry.register(manifest, enabled=enabled)
        if not accepted:
            self.supervisor.registry.remove(definition.agent_id)
            return False
        self._definitions[definition.agent_id] = definition
        if enabled and not self.plugin_registry.activate(self.plugin_id_for(definition.agent_id)):
            self._definitions.pop(definition.agent_id, None)
            self.supervisor.registry.remove(definition.agent_id)
            return False
        return True

    def activate(self, agent_id: str) -> bool:
        return self.plugin_registry.activate(self.plugin_id_for(agent_id))

    def deactivate(self, agent_id: str) -> bool:
        return self.plugin_registry.deactivate(self.plugin_id_for(agent_id))

    def is_active(self, agent_id: str) -> bool:
        record = self.plugin_registry.get(self.plugin_id_for(agent_id))
        return record is not None and record.state.value == "active"

    def run(self, **kwargs: Any) -> SubagentResult:
        agent_id = str(kwargs.get("agent_id", ""))
        if not self.is_active(agent_id):
            raise SubagentError(f"subagent plugin is not active: {agent_id}")
        result = self.supervisor.run(**kwargs)
        self._child_agents[result.child_run_id] = agent_id
        # AUD-13221：超出上限时按插入序淘汰最旧映射，字典不再无界增长。
        excess = len(self._child_agents) - self._max_child_agents
        if excess > 0:
            for child_run_id in list(self._child_agents)[:excess]:
                self._child_agents.pop(child_run_id, None)
        return result

    def continue_child(self, child_run_id: str, prompt: str) -> SubagentResult:
        agent_id = self._child_agents.get(str(child_run_id), "")
        if not self.is_active(agent_id):
            raise SubagentError("subagent plugin is not active")
        return self.supervisor.continue_child(child_run_id, prompt)

    @staticmethod
    def plugin_id_for(agent_id: str) -> str:
        return f"composition.subagent.{agent_id}"


__all__ = ["AgentPresetPluginHost", "SkillPluginHost", "SubagentPluginHost"]
