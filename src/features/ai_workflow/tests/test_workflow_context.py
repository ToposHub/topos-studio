"""Regression tests for live workflow context and deterministic preview metadata."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.features.ai_workflow.core.node_specs import NodeSpecRegistry, register_official_specs
from src.features.ai_workflow.core.template_registry import (
    WorkflowTemplateRegistry,
    register_official_templates,
)
from src.features.ai_workflow.core.workflow_compiler import WorkflowCompiler
from src.features.ai_workflow.runtime.workflow_context import (
    WorkflowContextError,
    resolve_workflow_context,
)


class _Analysis:
    def get_analysis(self, asset_id: str):
        return SimpleNamespace(to_dict=lambda: {"asset_id": asset_id, "version": 1})

    def get_segments(self, asset_id: str):
        return (SimpleNamespace(to_dict=lambda: {"asset_id": asset_id, "kind": "shot"}),)

    def get_status(self, asset_id: str) -> str:
        return "ready"


class _Bridge:
    _analysis_service = _Analysis()

    def get_project_id(self) -> str:
        return "project-1"

    def get_active_timeline_id(self) -> str:
        return "timeline-1"

    def get_selected_asset_ids(self) -> tuple[str, ...]:
        return ("asset-a", "asset-b")

    def get_project_sequences(self):
        seq = SimpleNamespace(revision=7)
        return SimpleNamespace(sequences={"timeline-1": seq})


class _Service:
    active_brief_id = "brief-1"
    active_profile_id = "profile-local"

    def get_brief(self, brief_id: str):
        return SimpleNamespace(revision=3)


def test_context_contains_live_scope_revision_and_evidence_hash() -> None:
    context = resolve_workflow_context(_Bridge(), _Service())
    assert context.project_id == "project-1"
    assert context.timeline_id == "timeline-1"
    assert context.selected_asset_ids == ("asset-a", "asset-b")
    assert context.base_revision == 7
    assert context.brief_id == "brief-1"
    assert context.brief_revision == 3
    assert len(context.analysis_snapshot_hash) == 64
    assert len(context.context_hash) == 64


def test_context_fails_closed_without_asset_scope() -> None:
    bridge = _Bridge()
    bridge.get_selected_asset_ids = lambda: ()  # type: ignore[method-assign]
    with pytest.raises(WorkflowContextError, match="没有选中的素材") as exc_info:
        resolve_workflow_context(bridge, _Service())
    assert exc_info.value.code == "NO_ASSETS"


def test_same_compiler_input_has_stable_plan_hash() -> None:
    node_registry = NodeSpecRegistry()
    register_official_specs(node_registry)
    templates = WorkflowTemplateRegistry()
    register_official_templates(templates)
    graph = templates.instantiate("wf-auto-caption", new_workflow_id="wf-test")
    compiler = WorkflowCompiler(node_registry)
    kwargs = {
        "skill_id": "topos.auto_caption",
        "project_id": "project-1",
        "timeline_id": "timeline-1",
        "selected_asset_ids": ("asset-a",),
        "brief_id": "brief-1",
        "brief_revision": 3,
        "base_revision": 7,
        "analysis_snapshot_hash": "evidence-hash",
        "profile_id": "profile-local",
        "available_capabilities": {"asr"},
        "now": 100.0,
    }
    first = compiler.compile(graph, **kwargs)
    second = compiler.compile(graph, **kwargs)
    assert first.success and second.success
    assert first.plan_hash == second.plan_hash
    assert first.plan_preview["execution"] == "preview_only_until_approval"
    assert first.invocation is not None
    assert first.invocation.scope.base_revision == 7
    assert first.invocation.inputs["analysis_snapshot_hash"] == "evidence-hash"


def test_plan_hash_changes_when_revision_changes() -> None:
    node_registry = NodeSpecRegistry()
    register_official_specs(node_registry)
    templates = WorkflowTemplateRegistry()
    register_official_templates(templates)
    graph = templates.instantiate("wf-auto-caption", new_workflow_id="wf-test")
    compiler = WorkflowCompiler(node_registry)
    base = compiler.compile(
        graph,
        skill_id="topos.auto_caption",
        project_id="project-1",
        timeline_id="timeline-1",
        selected_asset_ids=("asset-a",),
        available_capabilities={"asr"},
        base_revision=7,
        now=100.0,
    )
    changed = compiler.compile(
        graph,
        skill_id="topos.auto_caption",
        project_id="project-1",
        timeline_id="timeline-1",
        selected_asset_ids=("asset-a",),
        available_capabilities={"asr"},
        base_revision=8,
        now=100.0,
    )
    assert base.plan_hash != changed.plan_hash


# ---------------------------------------------------------------------- #
# ComfyUI 能力广告诚实性（AUD-23403）
# ---------------------------------------------------------------------- #


class _Caps:
    def __init__(self, class_types: tuple[str, ...]) -> None:
        self.available_class_types = frozenset(class_types)


class _Executor:
    """模拟 ComfyWorkflowExecutor 的绑定/探活状态。"""

    def __init__(
        self, *, bound: bool = False, class_types: tuple[str, ...] = (),
    ) -> None:
        self._bound_snapshot = object() if bound else None
        self._caps = _Caps(class_types)


class TestComfyCapabilityAdvertising:
    def _bridge_with_executors(self, executors: dict) -> _Bridge:
        bridge = _Bridge()
        bridge._comfy_generation_executors = executors
        return bridge

    def test_configured_but_unbound_executor_does_not_advertise(self) -> None:
        """AUD-23403：仅装配（未绑定/未探活）的 executor 不广告 image_gen/video_gen。"""
        bridge = self._bridge_with_executors({"local": _Executor()})
        context = resolve_workflow_context(bridge, _Service())
        assert "image_gen" not in context.available_capabilities
        assert "video_gen" not in context.available_capabilities

    def test_bound_without_probe_does_not_advertise(self) -> None:
        """绑定了 snapshot 但从未探活（capabilities 空）→ 仍不广告。"""
        bridge = self._bridge_with_executors({"local": _Executor(bound=True)})
        context = resolve_workflow_context(bridge, _Service())
        assert "image_gen" not in context.available_capabilities

    def test_bound_and_probed_advertises(self) -> None:
        bridge = self._bridge_with_executors({
            "local": _Executor(bound=True, class_types=("CheckpointLoaderSimple",)),
        })
        context = resolve_workflow_context(bridge, _Service())
        assert "image_gen" in context.available_capabilities
        assert "video_gen" in context.available_capabilities

    def test_one_ready_executor_suffices_among_unready(self) -> None:
        bridge = self._bridge_with_executors({
            "unbound": _Executor(),
            "ready": _Executor(bound=True, class_types=("KSampler",)),
        })
        context = resolve_workflow_context(bridge, _Service())
        assert "image_gen" in context.available_capabilities
