"""P5.5 测试：发布 workflow 为 Skill（plan §10.7.4）。

> "发布为 Skill"只是把当前 workflow 的 graph template 和声明打包到
> ``SkillDefinition``；聊天自动出现该 Skill 卡片，输入 schema 相同。

验证 :class:`WorkflowPublisher` 的核心契约：

- 把 :class:`WorkflowGraph` → :class:`SkillDraft`（复用 P4.1
  ``new_draft_from_workflow``）。
- 跑 P4 发布门禁（eval + 版本 + executor）；通过 → :class:`SkillRelease`。
- 发布产物的 graph_template == 源 workflow graph。
- 发布后聊天可见（注册进 SkillRegistry）。
- local_dev workflow 发布后仍 unverified（红线）。

红线（plan §10.7.4 / §1.4 / §5.3）：

- 发布走 P4 同一门禁（不绕过 eval / executor 安全检查）。
- workflow graph 的恶意 spec_id 不影响安全（executor_kind 受控）。
- 发布产物 skill_id 加 draft./wf 前缀（不覆盖 official）。
"""
from __future__ import annotations


from src.features.ai_workflow.core.node_specs import (
    NodeSpecRegistry,
    register_official_specs,
)
from src.features.ai_workflow.core.template_registry import (
    WorkflowTemplateRegistry,
    register_official_templates,
)
from src.features.ai_workflow.core.workflow_models import (
    PortRef,
    WorkflowEdge,
    WorkflowGraph,
    WorkflowNode,
    WorkflowPolicy,
)
from src.features.ai_workflow.core.publish_as_skill import (
    WorkflowPublisher,
)
from src.features.edit.ai_editing.skill_manifest import SkillSource
from src.features.edit.ai_editing.skill_registry import SkillRegistry


def _make_graph(workflow_id: str = "wf-my-caption") -> WorkflowGraph:
    return WorkflowGraph(
        workflow_id=workflow_id,
        name="我的字幕流程",
        nodes=(
            WorkflowNode(node_id="input", spec_id="topos.media.selected_assets"),
            WorkflowNode(node_id="asr", spec_id="topos.analysis.transcribe_auto"),
            WorkflowNode(node_id="caption", spec_id="topos.caption.propose_track"),
        ),
        edges=(
            WorkflowEdge(
                from_port=PortRef(node_id="input", port="assets"),
                to_port=PortRef(node_id="asr", port="assets"),
            ),
            WorkflowEdge(
                from_port=PortRef(node_id="asr", port="transcript"),
                to_port=PortRef(node_id="caption", port="transcript"),
            ),
        ),
        policy=WorkflowPolicy(),
    )


# ---------------------------------------------------------------------- #
# 发布成功
# ---------------------------------------------------------------------- #


class TestPublishSuccess:
    def test_publish_creates_release(self) -> None:
        node_reg = NodeSpecRegistry(); register_official_specs(node_reg)
        publisher = WorkflowPublisher(node_registry=node_reg)
        result = publisher.publish(
            graph=_make_graph(),
            author="dev-alice",
            skill_title="我的字幕 Skill",
            version="1.0.0",
        )
        assert result.success
        assert result.release is not None
        assert result.release.graph_template is not None
        assert result.release.graph_template["workflow_id"] == "wf-my-caption"

    def test_published_skill_registered_in_chat(self) -> None:
        """发布后聊天可见（注册进 SkillRegistry）。"""
        node_reg = NodeSpecRegistry(); register_official_specs(node_reg)
        publisher = WorkflowPublisher(node_registry=node_reg)
        result = publisher.publish(
            graph=_make_graph(), author="dev-alice",
            skill_title="我的字幕 Skill", version="1.0.0",
        )
        registry = SkillRegistry(register_official=False)
        publisher.register_into(registry, result.release)
        got = registry.get(result.release.skill_id)
        assert got is not None

    def test_release_carries_workflow_hash(self) -> None:
        node_reg = NodeSpecRegistry(); register_official_specs(node_reg)
        publisher = WorkflowPublisher(node_registry=node_reg)
        result = publisher.publish(
            graph=_make_graph(), author="x", skill_title="T", version="1.0.0",
        )
        assert result.release.workflow_hash  # workflow 来源记 hash


# ---------------------------------------------------------------------- #
# local_dev workflow 发布后仍 unverified
# ---------------------------------------------------------------------- #


class TestLocalDevUnverified:
    def test_published_workflow_unverified(self) -> None:
        node_reg = NodeSpecRegistry(); register_official_specs(node_reg)
        publisher = WorkflowPublisher(node_registry=node_reg)
        result = publisher.publish(
            graph=_make_graph(), author="dev-alice",
            skill_title="T", version="1.0.0",
        )
        assert result.release.unverified is True
        assert result.release.manifest.source == SkillSource.LOCAL_DEV

    def test_registry_keeps_local_dev_unverified(self) -> None:
        node_reg = NodeSpecRegistry(); register_official_specs(node_reg)
        publisher = WorkflowPublisher(node_registry=node_reg)
        result = publisher.publish(
            graph=_make_graph(), author="x", skill_title="T", version="1.0.0",
        )
        registry = SkillRegistry(register_official=False)
        publisher.register_into(registry, result.release)
        got = registry.get(result.release.skill_id)
        assert got.source == SkillSource.LOCAL_DEV
        assert got.unverified is True


# ---------------------------------------------------------------------- #
# 发布走 P4 同一门禁
# ---------------------------------------------------------------------- #


class TestReusesP4Gate:
    def test_dev_version_blocked(self) -> None:
        """版本 < 1.0.0 不得发布。"""
        node_reg = NodeSpecRegistry(); register_official_specs(node_reg)
        publisher = WorkflowPublisher(node_registry=node_reg)
        result = publisher.publish(
            graph=_make_graph(), author="x",
            skill_title="T", version="0.9.0",  # dev
        )
        assert not result.success
        # AUD-23700：精确断言版本门禁拒绝码（不得用 ``or result.deny_code``
        # 弱化为"任意非空拒绝码即通过"）；版本门禁回归时本测试必须失败。
        assert result.deny_code == "DEV_VERSION_NOT_PUBLISHABLE"
        assert result.release is None

    def test_executor_kind_safe(self) -> None:
        """红线：发布的 workflow skill executor_kind 受控（不执行任意代码）。"""
        node_reg = NodeSpecRegistry(); register_official_specs(node_reg)
        publisher = WorkflowPublisher(node_registry=node_reg)
        result = publisher.publish(
            graph=_make_graph(), author="x", skill_title="T", version="1.0.0",
        )
        # AUD-23701：先断言发布成功，安全红线断言不得包在 ``if result.success:``
        # 内静默跳过（publish 失败时测试必须失败）。
        assert result.success
        assert result.release is not None
        assert not result.release.executor_kind.is_arbitrary_code


# ---------------------------------------------------------------------- #
# 发布产物不覆盖 official skill
# ---------------------------------------------------------------------- #


class TestNoOfficialOverwrite:
    def test_published_skill_id_has_wf_prefix(self) -> None:
        node_reg = NodeSpecRegistry(); register_official_specs(node_reg)
        publisher = WorkflowPublisher(node_registry=node_reg)
        result = publisher.publish(
            graph=_make_graph(), author="x", skill_title="T", version="1.0.0",
        )
        # skill_id 加 draft./wf 前缀（不覆盖 official）
        assert result.release.skill_id.startswith("draft.")


# ---------------------------------------------------------------------- #
# 从官方模板实例化后发布
# ---------------------------------------------------------------------- #


class TestPublishFromTemplateInstance:
    def test_publish_instantiated_template_copy(self) -> None:
        """复制官方模板 → 编辑 → 发布为 local_dev Skill。"""
        node_reg = NodeSpecRegistry(); register_official_specs(node_reg)
        tr = WorkflowTemplateRegistry()
        register_official_templates(tr)
        copy = tr.instantiate(
            "wf-auto-caption",
            new_workflow_id="wf-my-caption",
            param_overrides={"caption": {"track_name": "自定义字幕"}},
        )
        publisher = WorkflowPublisher(node_registry=node_reg)
        result = publisher.publish(
            graph=copy, author="dev-alice",
            skill_title="自定义字幕 Skill", version="1.0.0",
        )
        assert result.success
        assert result.release.unverified is True
        # graph_template 是副本（含自定义参数）
        cap_node = next(
            n for n in result.release.graph_template["nodes"]
            if n["node_id"] == "caption"
        )
        assert cap_node["params"]["track_name"] == "自定义字幕"
