"""P4 edit capability plugin transaction bridge tests."""

from __future__ import annotations

from typing import Any

from src.features.ai_harness.plugin_runtime import PluginExecutionPolicy
from src.features.edit.ai_editing.edit_capability_plugin import (
    EditCapabilityDescriptor,
    EditCapabilityPluginHost,
)
from src.features.edit.ai_editing.models import EditPlan, OperationKind, PlanMode, PlanTarget


class _Facade:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any], Any]] = []

    def invoke(self, tool_name: str, args: dict[str, Any], *, approval: Any = None) -> dict[str, Any]:
        self.calls.append((tool_name, args, approval))
        if tool_name == "topos.validate_edit_plan":
            return {"ok": True, "report": {"status": "valid"}}
        if tool_name == "topos.apply_edit_plan":
            return {"ok": True, "new_revision": 1}
        raise AssertionError(f"unexpected tool: {tool_name}")


class _ProposalPlugin:
    def __init__(self, plan: EditPlan) -> None:
        self.plan = plan
        self.calls = 0

    def propose(self, args: dict[str, Any]) -> EditPlan:
        self.calls += 1
        return self.plan


def _plan(*, mode: PlanMode = PlanMode.PREVIEW) -> EditPlan:
    return EditPlan.create(
        PlanTarget(project_id="project-1", timeline_id="timeline-1"),
        base_revision=0,
        mode=mode,
        title="Plugin proposal",
    )


def test_edit_plugin_can_only_propose_and_host_validates_before_apply() -> None:
    facade = _Facade()
    host = EditCapabilityPluginHost(facade)
    plugin = _ProposalPlugin(_plan())
    descriptor = EditCapabilityDescriptor(
        plugin_id="subtitle",
        display_name="Subtitle capability",
        operation_kinds=(OperationKind.ADD_SUBTITLE,),
    )
    assert host.register(plugin, descriptor, policy=PluginExecutionPolicy(max_output_chars=16_000))
    assert host.propose(descriptor.plugin_id)["diagnostics"][0]["code"] == "PLUGIN_NOT_ACTIVE"
    assert host.activate(descriptor.plugin_id)

    proposal = host.propose(descriptor.plugin_id, {"asset_id": "asset-1"})
    assert proposal["ok"] is True
    assert proposal["validation"]["ok"] is True
    assert plugin.calls == 1
    assert [call[0] for call in facade.calls] == ["topos.validate_edit_plan"]
    capability = host.plugin_registry.plugin_capability_for_tool(descriptor.proposal_tool)
    assert capability is not None
    assert descriptor.proposal_tool in capability[1].tool_names

    denied = host.apply(descriptor.plugin_id, _plan())
    assert denied["diagnostics"][0]["code"] == "PERMISSION_DENIED"
    applied = host.apply(descriptor.plugin_id, _plan(), approval="host-grant")
    assert applied["ok"] is True
    assert [call[0] for call in facade.calls] == [
        "topos.validate_edit_plan",
        "topos.validate_edit_plan",
        "topos.apply_edit_plan",
    ]
    assert facade.calls[-1][2] == "host-grant"


def test_edit_plugin_cannot_return_commit_plan() -> None:
    facade = _Facade()
    host = EditCapabilityPluginHost(facade)
    descriptor = EditCapabilityDescriptor(
        plugin_id="unsafe-commit",
        display_name="Unsafe proposal",
    )
    assert host.register(_ProposalPlugin(_plan(mode=PlanMode.COMMIT)), descriptor)
    assert host.activate(descriptor.plugin_id)
    result = host.propose(descriptor.plugin_id)
    assert result["diagnostics"][0]["code"] == "EDIT_PLUGIN_COMMIT_FORBIDDEN"
    assert facade.calls == []
