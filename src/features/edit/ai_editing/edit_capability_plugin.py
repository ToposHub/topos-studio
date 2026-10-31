"""Safe bridge for edit-capability plugins.

An edit plugin may propose an :class:`EditPlan`, but it never receives an
``EditSession`` or a timeline object.  The host validates proposals through
``ToolFacade`` and applies a user-approved plan through the same facade.  The
plugin manifest intentionally advertises only a ``propose`` tool; commit is a
host-owned operation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol

from src.features.ai_harness.plugin_runtime import (
    PLUGIN_API_VERSION,
    PluginCapability,
    PluginContext,
    PluginExecutionPolicy,
    PluginHealth,
    PluginManifest,
    PluginRegistry,
    PluginSource,
)
from src.features.edit.ai_editing.models import EditPlan, OperationKind, PlanMode
from src.features.edit.ai_editing.tool_facade import ToolFacade


class EditCapabilityPlugin(Protocol):
    """Plugin-side proposal interface; no host editor object is exposed."""

    def propose(self, args: Mapping[str, Any]) -> EditPlan:
        ...


@dataclass(frozen=True)
class EditCapabilityDescriptor:
    plugin_id: str
    display_name: str
    operation_kinds: tuple[OperationKind, ...] = ()
    source: PluginSource = PluginSource.LOCAL_DEV
    version: str = "1.0.0"
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        plugin_id = str(self.plugin_id or "").strip()
        # AUD-13405：display_name 与 plugin_id 一致做 str(...) 归一化——只校验不
        # 归一会让 display_name=123 之类非字符串通过校验，随后 register() 构造
        # metadata 时在 to_dict 的 [:200] 切片处抛 TypeError 使注册路径崩溃。
        display_name = str(self.display_name or "").strip()
        if not plugin_id or not display_name:
            raise ValueError("edit capability plugin_id and display_name are required")
        source = self.source if isinstance(self.source, PluginSource) else PluginSource(str(self.source))
        kinds = tuple(dict.fromkeys(
            kind if isinstance(kind, OperationKind) else OperationKind(str(kind))
            for kind in self.operation_kinds
        ))
        object.__setattr__(self, "plugin_id", plugin_id)
        object.__setattr__(self, "display_name", display_name)
        object.__setattr__(self, "source", source)
        object.__setattr__(self, "operation_kinds", kinds)

    @property
    def proposal_tool(self) -> str:
        return f"topos.edit.{self.plugin_id}.propose"

    def to_dict(self) -> dict[str, Any]:
        return {
            "plugin_id": self.plugin_id,
            "display_name": self.display_name[:200],
            "operation_kinds": [kind.value for kind in self.operation_kinds],
            "source": self.source.value,
            "version": self.version,
            "proposal_tool": self.proposal_tool,
            "metadata": dict(self.metadata),
        }


class _EditCapabilityAdapter:
    def __init__(self, plugin: EditCapabilityPlugin, descriptor: EditCapabilityDescriptor) -> None:
        self.plugin = plugin
        self.descriptor = descriptor
        self._active = False

    def activate(self, context: PluginContext) -> None:
        self._active = True

    def deactivate(self) -> None:
        self._active = False

    def health(self) -> PluginHealth:
        return PluginHealth(
            status="healthy" if self._active else "inactive",
            message="edit proposal plugin ready" if self._active else "edit plugin is inactive",
        )

    def invoke(self, tool_name: str, args: Mapping[str, Any], *, approval: Any = None) -> Mapping[str, Any]:
        if tool_name != self.descriptor.proposal_tool:
            raise ValueError("edit plugin exposes proposal tool only")
        plan = self.plugin.propose(dict(args))
        if isinstance(plan, Mapping):
            plan = EditPlan.from_dict(dict(plan.get("plan", plan)))
        if not isinstance(plan, EditPlan):
            raise TypeError("edit plugin propose() must return EditPlan")
        return {"ok": True, "plan": plan.to_dict()}


class EditCapabilityPluginHost:
    """Host-side proposal/validation/commit boundary for edit plugins."""

    def __init__(
        self,
        facade: ToolFacade,
        *,
        plugin_registry: PluginRegistry | None = None,
    ) -> None:
        self.facade = facade
        self.plugin_registry = plugin_registry or PluginRegistry()
        self._descriptors: dict[str, EditCapabilityDescriptor] = {}

    def register(
        self,
        plugin: EditCapabilityPlugin,
        descriptor: EditCapabilityDescriptor,
        *,
        policy: PluginExecutionPolicy | None = None,
        enabled: bool = True,
    ) -> bool:
        capability = PluginCapability(
            capability_id=f"edit.capability.{descriptor.plugin_id}",
            description=descriptor.display_name,
            tool_names=(descriptor.proposal_tool,),
            permission="proposal",
            metadata={
                "edit_capability": descriptor.to_dict(),
                "thread_safe": False,
                "execution_mode": "exclusive",
            },
        )
        manifest = PluginManifest(
            plugin_id=f"edit.capability.{descriptor.plugin_id}",
            display_name=descriptor.display_name,
            version=descriptor.version,
            api_version=PLUGIN_API_VERSION,
            source=descriptor.source,
            description="edit capability proposal adapter",
            capabilities=(capability,),
            metadata={"edit_capability": descriptor.to_dict()},
        )
        accepted = self.plugin_registry.register(
            manifest,
            _EditCapabilityAdapter(plugin, descriptor),
            enabled=enabled,
            policy=policy,
        )
        if accepted:
            self._descriptors[descriptor.plugin_id] = descriptor
        return accepted

    def activate(self, plugin_id: str) -> bool:
        descriptor = self._descriptors.get(str(plugin_id))
        return descriptor is not None and self.plugin_registry.activate(self._manifest_id(plugin_id))

    def deactivate(self, plugin_id: str) -> bool:
        return self.plugin_registry.deactivate(self._manifest_id(plugin_id))

    def propose(self, plugin_id: str, args: Mapping[str, Any] | None = None) -> dict[str, Any]:
        descriptor = self._descriptors.get(str(plugin_id))
        if descriptor is None:
            return self._failure("EDIT_PLUGIN_NOT_FOUND", "edit capability plugin is not registered")
        result = self.plugin_registry.invoke_tool(
            self._manifest_id(plugin_id),
            descriptor.proposal_tool,
            dict(args or {}),
        )
        if result.get("ok") is not True or not isinstance(result.get("plan"), Mapping):
            return result
        try:
            plan = EditPlan.from_dict(dict(result["plan"]))
        except Exception as exc:  # noqa: BLE001 - proposal boundary is fail-closed
            return self._failure("EDIT_PLAN_INVALID", str(exc)[:500])
        if plan.mode is not PlanMode.PREVIEW:
            return self._failure(
                "EDIT_PLUGIN_COMMIT_FORBIDDEN",
                "edit plugins may only return preview plans",
            )
        validation = self.facade.invoke("topos.validate_edit_plan", {"plan": plan.to_dict()})
        return {
            "ok": bool(validation.get("ok")),
            "plan": plan.to_dict(),
            "validation": validation,
        }

    def apply(
        self,
        plugin_id: str,
        plan: EditPlan,
        *,
        approval: Any = None,
    ) -> dict[str, Any]:
        """Apply only through the core facade; plugins never receive this call."""

        descriptor = self._descriptors.get(str(plugin_id))
        if descriptor is None:
            return self._failure("EDIT_PLUGIN_NOT_FOUND", "edit capability plugin is not registered")
        record = self.plugin_registry.get(self._manifest_id(plugin_id))
        if record is None or record.state.value != "active":
            return self._failure("EDIT_PLUGIN_NOT_ACTIVE", "edit capability plugin is not active")
        if approval is None:
            return self._failure("PERMISSION_DENIED", "applying an edit plan requires host approval")
        validation = self.facade.invoke("topos.validate_edit_plan", {"plan": plan.to_dict()})
        if validation.get("ok") is not True:
            return {"ok": False, "validation": validation}
        return self.facade.invoke(
            "topos.apply_edit_plan",
            {"plan": plan.to_dict()},
            approval=approval,
        )

    def descriptor(self, plugin_id: str) -> EditCapabilityDescriptor | None:
        return self._descriptors.get(str(plugin_id))

    @staticmethod
    def _manifest_id(plugin_id: str) -> str:
        return f"edit.capability.{plugin_id}"

    @staticmethod
    def _failure(code: str, message: str) -> dict[str, Any]:
        return {
            "ok": False,
            "error": message[:500],
            "diagnostics": [{"code": code, "severity": "error"}],
        }


__all__ = [
    "EditCapabilityDescriptor",
    "EditCapabilityPlugin",
    "EditCapabilityPluginHost",
]
