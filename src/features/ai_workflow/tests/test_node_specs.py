"""P5.2 测试：节点 spec 注册表（plan §4.1 M4 16 个节点 + §3.3 spec 必填字段）。

验证 :class:`NodeSpecRegistry` 与 :class:`WorkflowNodeSpec` 的核心契约：

- plan §4.1 的 16 个 M4 节点 spec 全部注册（输入/分析/编辑/文本/生成/审批/输出）。
- 每个 spec 至少声明（plan §3.3）：spec_id / 版本 / 显示名 / 分类 / 输入输出端口
  （artifact type + 必填 + 多连接）/ 参数 schema / 所需 capability / 风险。
- 端口 artifact type 来自 plan §3.1 的 ``ArtifactType`` 表（asset_ref/asset_set/
  transcript_ref/edit_plan_ref/...）。
- spec_id 命名规范 ``<domain>.<capability>[.<variant>]``。
- 注册表查询 ``get(spec_id)`` / ``list_by_category()`` / 端口查找。

红线（plan §3.3 / §1.4）：

- spec 不声明 FORBIDDEN_CAPABILITIES（exec_python/shell/fs/ui）。
- spec 的 ``executor_id`` 受控（不执行任意代码）。
- spec 不可变（frozen）；版本号语义化。
"""
from __future__ import annotations

import pytest

from src.features.ai_workflow.core.node_specs import (
    NodeCategory,
    NodePortSpec,
    NodeSpecRegistry,
    WorkflowNodeSpec,
    register_official_specs,
)


class TestNodePortSpec:
    def test_construction(self) -> None:
        p = NodePortSpec(
            name="assets", artifact_type="asset_set",
            direction="input", required=True, allow_multi=False,
        )
        assert p.name == "assets"
        assert p.artifact_type == "asset_set"
        assert p.direction == "input"
        assert p.required is True

    def test_invalid_direction_rejected(self) -> None:
        with pytest.raises(ValueError):
            NodePortSpec(name="x", artifact_type="y", direction="bogus")


# ---------------------------------------------------------------------- #
# 16 个 M4 节点（plan §4.1）
# ---------------------------------------------------------------------- #


REQUIRED_M4_SPEC_IDS = {
    # 输入
    "topos.media.selected_assets",
    "topos.timeline.current",
    # 分析
    "topos.media.extract_audio",
    "topos.analysis.transcribe_auto",
    "topos.analysis.scene_cut",
    "topos.analysis.ocr",
    "topos.analysis.audio_features",
    # 编辑 / 文本 / 生成
    "topos.caption.propose_track",
    "topos.text.translate",
    "topos.tts.qwen3",
    "topos.timeline.propose_audio_placement",
    "topos.agent.rough_cut_plan",
    # 审批 / 输出
    "topos.timeline.preview_diff",
    "topos.user.approval",
    "topos.timeline.apply_approved_plan",
    "topos.report.run_summary",
}


class TestM4NodeSpecs:
    def test_all_16_m4_specs_registered(self) -> None:
        registry = NodeSpecRegistry()
        register_official_specs(registry)
        registered = {s.spec_id for s in registry.all_specs()}
        missing = REQUIRED_M4_SPEC_IDS - registered
        assert not missing, f"missing M4 specs: {missing}"

    def test_each_spec_has_required_fields(self) -> None:
        registry = NodeSpecRegistry()
        register_official_specs(registry)
        for spec in registry.all_specs():
            assert spec.spec_id
            assert spec.version
            assert spec.display_name
            assert spec.category in NodeCategory.ALL
            # 至少有端口声明（输入节点可以无输入端口，但必须有输出）
            assert len(spec.outputs) >= 1 or spec.category == NodeCategory.CONTROL
            # executor_id 受控
            assert spec.executor_id in (
                "analysis_provider", "tts_provider", "translate_provider",
                "llm_role", "taskgraph", "workflow_runner", "editorial_builder",
                "read_only", "control", "render", "ffmpeg",
            )

    def test_spec_naming_convention(self) -> None:
        """spec_id 遵循 <domain>.<capability>[.<variant>]。"""
        registry = NodeSpecRegistry()
        register_official_specs(registry)
        for spec in registry.all_specs():
            parts = spec.spec_id.split(".")
            assert len(parts) >= 3, f"spec_id {spec.spec_id} violates naming"
            assert parts[0] == "topos"

    def test_get_spec_by_id(self) -> None:
        registry = NodeSpecRegistry()
        register_official_specs(registry)
        spec = registry.get("topos.analysis.transcribe_auto")
        assert spec is not None
        assert "asr" in spec.required_capabilities

    def test_list_by_category(self) -> None:
        registry = NodeSpecRegistry()
        register_official_specs(registry)
        analysis = registry.list_by_category(NodeCategory.ANALYSIS)
        assert len(analysis) >= 5
        assert all(s.category == NodeCategory.ANALYSIS for s in analysis)

    def test_unknown_spec_id_returns_none(self) -> None:
        registry = NodeSpecRegistry()
        register_official_specs(registry)
        assert registry.get("topos.bogus") is None


# ---------------------------------------------------------------------- #
# 注册版本语义（AUD-23200：拒绝降级）
# ---------------------------------------------------------------------- #


def _spec_with_version(spec_id: str, version: str) -> WorkflowNodeSpec:
    """最小合法 spec（version 可变）。"""
    return WorkflowNodeSpec(
        spec_id=spec_id, version=version,
        display_name=spec_id, category=NodeCategory.INPUT,
        executor_id="read_only",
        outputs=(
            NodePortSpec(name="out", artifact_type="report", direction="output"),
        ),
    )


class TestRegistryVersioning:
    def test_same_version_is_idempotent(self) -> None:
        registry = NodeSpecRegistry()
        first = _spec_with_version("topos.test.node", "1.0.0")
        second = _spec_with_version("topos.test.node", "1.0.0")
        registry.register(first)
        registry.register(second)
        assert registry.count == 1
        assert registry.get("topos.test.node") is first

    def test_upgrade_replaces_with_newer(self) -> None:
        registry = NodeSpecRegistry()
        registry.register(_spec_with_version("topos.test.node", "1.0.0"))
        newer = _spec_with_version("topos.test.node", "1.1.0")
        registry.register(newer)
        assert registry.get("topos.test.node") is newer

    def test_downgrade_is_rejected(self) -> None:
        """AUD-23200：旧版本注册不得静默覆盖已注册的新版本。"""
        registry = NodeSpecRegistry()
        registry.register(_spec_with_version("topos.test.node", "2.0.0"))
        older = _spec_with_version("topos.test.node", "1.9.9")
        registry.register(older)
        assert registry.get("topos.test.node").version == "2.0.0"

    def test_registration_order_does_not_decide_version(self) -> None:
        """后注册的 v1.0.0 不能覆盖先注册的 v2.0.0（乱序装配场景）。"""
        registry = NodeSpecRegistry()
        registry.register(_spec_with_version("topos.test.node", "2.0.0"))
        registry.register(_spec_with_version("topos.test.node", "1.0.0"))
        registry.register(_spec_with_version("topos.test.node", "2.1.0"))
        registry.register(_spec_with_version("topos.test.node", "2.0.5"))
        assert registry.get("topos.test.node").version == "2.1.0"


# ---------------------------------------------------------------------- #
# 端口 artifact type 来自 plan §3.1 表
# ---------------------------------------------------------------------- #


class TestArtifactTypes:
    def test_known_artifact_types_used(self) -> None:
        registry = NodeSpecRegistry()
        register_official_specs(registry)
        all_types: set[str] = set()
        for spec in registry.all_specs():
            for p in spec.inputs + spec.outputs:
                all_types.add(p.artifact_type)
        # plan §3.1 的首批 ArtifactType 中 M4 节点使用的（selection_set 留给 M6+）
        expected = {
            "asset_ref", "asset_set", "timeline_ref", "transcript_ref",
            "analysis_ref", "subtitle_track_ref", "text_document",
            "edit_plan_ref", "media_generation_ref", "report",
            "approval_token",
        }
        assert expected.issubset(all_types), f"missing types: {expected - all_types}"


# ---------------------------------------------------------------------- #
# 输入端口必填校验
# ---------------------------------------------------------------------- #


class TestPortContracts:
    def test_transcribe_auto_inputs_outputs(self) -> None:
        registry = NodeSpecRegistry()
        register_official_specs(registry)
        spec = registry.require("topos.analysis.transcribe_auto")
        # 输入：asset_set；输出：transcript_ref + analysis_ref
        in_types = {p.artifact_type for p in spec.inputs}
        out_types = {p.artifact_type for p in spec.outputs}
        assert "asset_set" in in_types
        assert "transcript_ref" in out_types
        assert "analysis_ref" in out_types

    def test_apply_plan_requires_approval_token(self) -> None:
        """红线：apply_approved_plan 必须输入 approval_token（plan §3.1）。"""
        registry = NodeSpecRegistry()
        register_official_specs(registry)
        spec = registry.require("topos.timeline.apply_approved_plan")
        in_types = {p.artifact_type for p in spec.inputs}
        assert "approval_token" in in_types
        assert "edit_plan_ref" in in_types

    def test_approval_node_outputs_token(self) -> None:
        registry = NodeSpecRegistry()
        register_official_specs(registry)
        spec = registry.require("topos.user.approval")
        out_types = {p.artifact_type for p in spec.outputs}
        assert "approval_token" in out_types
