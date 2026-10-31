"""P5.4 官方 workflow 模板注册表（plan §3.2 / §9.1 / §9.2 / §10.7.4）。

plan §10.7.4："先交付官方模板的查看、复制、参数编辑、运行和 debug；最后再开放
空画布、子图、循环/批处理和第三方导入。"

本模块把 plan §9.1（自动字幕）和 §9.2（字幕翻译+配音）的执行顺序建模为
:class:`WorkflowGraph` 模板。每个模板：

- 查看（``to_dict``）：UI 模板画廊展示节点结构 / 参数 / 默认 policy。
- 复制（``instantiate``）：用户"复制"得到 local_dev 可编辑副本（新 workflow_id；
  可覆盖参数）。
- 编译（``WorkflowCompiler``）：模板 graph 可直接编译为 Invocation 运行。
- debug：模板节点的 executor_id 全部受控；apply 节点连 approval_token。

红线（plan §10.7.4 / §1.4 / §6.4）：

- 官方模板 frozen（``WorkflowTemplateEntry.graph`` 不可改）；只有 instantiate 副本可改。
- 含 ``apply_approved_plan`` 的模板必须连 ``user.approval`` 的 approval_token。
- 模板节点 spec 全部来自 :class:`NodeSpecRegistry`（P5.2 M4 16 节点）。

与既有模块的关系：

- 复用 :class:`WorkflowGraph`（P5.1）/ :class:`NodeSpecRegistry`（P5.2）/
  :class:`WorkflowCompiler`（P5.3）。
- 模板的 ``skill_id`` 字段供 WorkflowCompiler 编译时路由到对应官方 Skill
  （topos.auto_caption / topos.subtitle_localization / topos.voiceover_polish /
  topos.product_ad_30s / topos.composite_render_import——后三者分别对应
  editorial/workflow 后端，见 plan §9.3-§9.5）。
"""
from __future__ import annotations

import copy
import uuid
from dataclasses import dataclass, field, replace
from typing import Any, Dict, Optional, Tuple

from src.features.ai_workflow.core.workflow_models import (
    PortRef,
    WorkflowEdge,
    WorkflowGraph,
    WorkflowNode,
    WorkflowPolicy,
)


# ---------------------------------------------------------------------- #
# WorkflowTemplateEntry
# ---------------------------------------------------------------------- #


@dataclass(frozen=True)
class WorkflowTemplateEntry:
    """官方 workflow 模板（plan §10.7.4）。

    Attributes:
        template_id: 模板稳定 ID（如 ``wf-auto-caption``）。
        display_name: 用户可见名。
        description: 模板说明。
        category: 分类（caption / localization / rough_cut / ...）。
        skill_id: 对应的官方 Skill ID（编译时路由用）。
        graph: :class:`WorkflowGraph`（frozen；官方模板不可编辑）。
        icon: 图标 key。
        default_param_overrides: 默认参数覆盖（UI 表单预填）。
    """

    template_id: str
    display_name: str
    description: str
    category: str
    skill_id: str
    graph: WorkflowGraph
    icon: str = ""
    default_param_overrides: Dict[str, Dict[str, Any]] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "template_id": self.template_id,
            "display_name": self.display_name,
            "description": self.description,
            "category": self.category,
            "skill_id": self.skill_id,
            "graph": self.graph.to_dict(),
            "icon": self.icon,
            "default_param_overrides": {
                k: dict(v) for k, v in self.default_param_overrides.items()
            },
        }


# ---------------------------------------------------------------------- #
# WorkflowTemplateRegistry
# ---------------------------------------------------------------------- #


class WorkflowTemplateRegistry:
    """官方 / 自定义 workflow 模板注册表（plan §10.7.4）。"""

    def __init__(self) -> None:
        self._templates: Dict[str, WorkflowTemplateEntry] = {}

    def register(self, entry: WorkflowTemplateEntry) -> None:
        self._templates[entry.template_id] = entry

    def get(self, template_id: str) -> Optional[WorkflowTemplateEntry]:
        return self._templates.get(template_id)

    def require(self, template_id: str) -> WorkflowTemplateEntry:
        t = self.get(template_id)
        if t is None:
            raise KeyError(f"unknown workflow template: {template_id!r}")
        return t

    def all_templates(self) -> Tuple[WorkflowTemplateEntry, ...]:
        return tuple(self._templates.values())

    def list_by_category(self, category: str) -> Tuple[WorkflowTemplateEntry, ...]:
        return tuple(t for t in self._templates.values() if t.category == category)

    def instantiate(
        self,
        template_id: str,
        *,
        new_workflow_id: Optional[str] = None,
        param_overrides: Optional[Dict[str, Dict[str, Any]]] = None,
    ) -> WorkflowGraph:
        """复制模板为可编辑的 local_dev 副本（plan §10.7.4 '复制 + 参数编辑'）。

        Args:
            template_id: 源模板 ID。
            new_workflow_id: 副本的 workflow_id（默认自动生成 ``wf-<template>-<rand>``）。
            param_overrides: ``{node_id: {param: value}}`` 覆盖节点参数。

        返回新的 :class:`WorkflowGraph`（与模板结构一致，但 workflow_id / params 变）。

        AUD-23209：副本与冻结官方模板之间不再共享任何可变容器——
        ``params`` / ``ui`` 的 dict、``default_param_overrides`` 的内层
        dict 全部 deepcopy 边界拷贝。"官方模板 frozen"红线由结构保证，
        而非仅靠"调用方不改副本"的约定维持。
        """
        t = self.require(template_id)
        wf_id = new_workflow_id or f"wf-{t.template_id}-{uuid.uuid4().hex[:8]}"
        # 合并默认参数 + 用户覆盖（内层 node dict 一并深拷贝，避免后续
        # 覆盖写穿透回冻结模板的 default_param_overrides）
        merged_overrides: Dict[str, Dict[str, Any]] = {
            nid: copy.deepcopy(params)
            for nid, params in t.default_param_overrides.items()
        }
        if param_overrides:
            for nid, params in param_overrides.items():
                base = dict(merged_overrides.get(nid, {}))
                base.update(params)
                merged_overrides[nid] = base
        # 构造新节点（应用参数覆盖；params/ui dict 均为副本）
        new_nodes = tuple(
            replace(
                n,
                params={
                    **copy.deepcopy(n.params),
                    **merged_overrides.get(n.node_id, {}),
                },
                ui=copy.deepcopy(n.ui),
            )
            for n in t.graph.nodes
        )
        return WorkflowGraph(
            workflow_id=wf_id,
            name=t.display_name,
            nodes=new_nodes,
            edges=t.graph.edges,
            ui=replace(t.graph.ui, viewport=copy.deepcopy(t.graph.ui.viewport)),
            policy=t.graph.policy,
            exposed_inputs=t.graph.exposed_inputs,
        )


# ---------------------------------------------------------------------- #
# 官方模板（plan §9.1 自动字幕 / §9.2 字幕翻译配音）
# ---------------------------------------------------------------------- #


def _auto_caption_template() -> WorkflowTemplateEntry:
    """plan §9.1 '自动字幕' 模板。

    执行顺序（plan §9.1）：
    1. Selected Assets 读取已选素材
    2. Transcribe Auto（Qwen → Demucs → WhisperX fallback）
    3. Propose Subtitle Track（EditPlan，PREVIEW）
    """
    graph = WorkflowGraph(
        workflow_id="wf-auto-caption",
        name="自动字幕（本地优先）",
        nodes=(
            WorkflowNode(
                node_id="input",
                spec_id="topos.media.selected_assets",
                params={"media_kind": "video_or_audio"},
                ui={"x": 120, "y": 200},
            ),
            WorkflowNode(
                node_id="asr",
                spec_id="topos.analysis.transcribe_auto",
                params={"language": "auto", "enable_diarization": True},
                ui={"x": 360, "y": 200},
            ),
            WorkflowNode(
                node_id="caption",
                spec_id="topos.caption.propose_track",
                params={"track_name": "AI 字幕"},
                ui={"x": 600, "y": 200},
            ),
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
        exposed_inputs=(("assets", "input", "assets"),),
        policy=WorkflowPolicy(),
    )
    return WorkflowTemplateEntry(
        template_id="wf-auto-caption",
        display_name="自动字幕",
        description="从转录证据生成可编辑的句级字幕草稿（plan §9.1）",
        category="caption",
        skill_id="topos.auto_caption",
        graph=graph,
        icon="caption",
    )


def _subtitle_localization_template() -> WorkflowTemplateEntry:
    """plan §9.2 '字幕翻译 + AI 配音' 模板。

    执行顺序（plan §9.2）：
    1. 输入现有字幕轨道或从转录生成
    2. 翻译节点输出 text_document
    3. Qwen3-TTS 合成到生成素材箱
    4. 音频放置 EditPlan
    """
    graph = WorkflowGraph(
        workflow_id="wf-subtitle-localization",
        name="字幕翻译与配音",
        nodes=(
            WorkflowNode(
                node_id="input",
                spec_id="topos.media.selected_assets",
                params={"media_kind": "video_or_audio"},
                ui={"x": 120, "y": 200},
            ),
            WorkflowNode(
                node_id="asr",
                spec_id="topos.analysis.transcribe_auto",
                ui={"x": 360, "y": 120},
            ),
            WorkflowNode(
                node_id="caption",
                spec_id="topos.caption.propose_track",
                ui={"x": 600, "y": 120},
            ),
            WorkflowNode(
                node_id="translate",
                spec_id="topos.text.translate",
                params={"target_language": "en"},
                ui={"x": 600, "y": 280},
            ),
            WorkflowNode(
                node_id="tts",
                spec_id="topos.tts.qwen3",
                ui={"x": 840, "y": 280},
            ),
            WorkflowNode(
                node_id="placement",
                spec_id="topos.timeline.propose_audio_placement",
                ui={"x": 1080, "y": 280},
            ),
            WorkflowNode(
                node_id="timeline",
                spec_id="topos.timeline.current",
                ui={"x": 120, "y": 400},
            ),
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
            WorkflowEdge(
                from_port=PortRef(node_id="caption", port="subtitle_track"),
                to_port=PortRef(node_id="translate", port="source"),
            ),
            WorkflowEdge(
                from_port=PortRef(node_id="translate", port="text"),
                to_port=PortRef(node_id="tts", port="text"),
            ),
            WorkflowEdge(
                from_port=PortRef(node_id="tts", port="audio"),
                to_port=PortRef(node_id="placement", port="audio"),
            ),
            WorkflowEdge(
                from_port=PortRef(node_id="timeline", port="timeline"),
                to_port=PortRef(node_id="placement", port="timeline"),
            ),
        ),
        exposed_inputs=(("assets", "input", "assets"),),
        policy=WorkflowPolicy(),
    )
    return WorkflowTemplateEntry(
        template_id="wf-subtitle-localization",
        display_name="字幕翻译与配音",
        description="翻译字幕并生成 TTS 配音（plan §9.2；先入生成素材箱）",
        category="localization",
        skill_id="topos.subtitle_localization",
        graph=graph,
        icon="translate",
    )


def register_official_templates(registry: WorkflowTemplateRegistry) -> None:
    """注册 plan §9.1 / §9.2 + §9.3-9.5 的官方模板。

    模板构造是纯 dataclass 组装（不 import adapters），接线错误属于代码缺陷，
    必须照常抛出（AUD-23207：不得宽泛 except 把模板构造错误吞成"模板无声
    消失"）。
    """
    registry.register(_auto_caption_template())
    registry.register(_subtitle_localization_template())
    # P10.3：3 个新用户模板
    registry.register(_talking_head_fine_cut_template())
    registry.register(_product_rough_cut_template())
    registry.register(_composite_render_import_template())


# ---------------------------------------------------------------------- #
# P10.3：3 个新用户模板（plan §9.3 口播精剪 / §9.4 产品粗剪 / §9.5 合成渲染后导入）
# ---------------------------------------------------------------------- #


def _talking_head_fine_cut_template() -> WorkflowTemplateEntry:
    """plan §9.3 '口播精剪' 模板。

    执行顺序：
    1. Selected Assets 读取已选素材（口播视频/音频）
    2. Transcribe Auto 转录（带词级时间戳）
    3. Rough Cut Plan 据 brief + transcript 产精剪 EditPlan
    4. Preview Diff 预览
    5. Approval 审批门禁
    6. Apply Approved Plan 落时间线
    """
    graph = WorkflowGraph(
        workflow_id="wf-talking-head-fine-cut",
        name="口播精剪",
        nodes=(
            WorkflowNode(
                node_id="input", spec_id="topos.media.selected_assets",
                params={"media_kind": "video_or_audio"},
                ui={"x": 100, "y": 220},
            ),
            WorkflowNode(
                node_id="asr", spec_id="topos.analysis.transcribe_auto",
                params={"language": "auto", "enable_diarization": False},
                ui={"x": 340, "y": 220},
            ),
            WorkflowNode(
                node_id="planner", spec_id="topos.agent.rough_cut_plan",
                params={"target_duration": 60},
                ui={"x": 580, "y": 220},
            ),
            WorkflowNode(
                node_id="preview", spec_id="topos.timeline.preview_diff",
                ui={"x": 820, "y": 120},
            ),
            WorkflowNode(
                node_id="timeline_in", spec_id="topos.timeline.current",
                ui={"x": 100, "y": 400},
            ),
            WorkflowNode(
                node_id="approval", spec_id="topos.user.approval",
                ui={"x": 820, "y": 320},
            ),
            WorkflowNode(
                node_id="apply", spec_id="topos.timeline.apply_approved_plan",
                ui={"x": 1060, "y": 220},
            ),
        ),
        edges=(
            WorkflowEdge(
                from_port=PortRef(node_id="input", port="assets"),
                to_port=PortRef(node_id="asr", port="assets"),
            ),
            WorkflowEdge(
                from_port=PortRef(node_id="input", port="assets"),
                to_port=PortRef(node_id="planner", port="assets"),
            ),
            WorkflowEdge(
                from_port=PortRef(node_id="asr", port="analysis"),
                to_port=PortRef(node_id="planner", port="analysis"),
            ),
            WorkflowEdge(
                from_port=PortRef(node_id="planner", port="plan"),
                to_port=PortRef(node_id="preview", port="plan"),
            ),
            WorkflowEdge(
                from_port=PortRef(node_id="timeline_in", port="timeline"),
                to_port=PortRef(node_id="preview", port="timeline"),
            ),
            WorkflowEdge(
                from_port=PortRef(node_id="planner", port="plan"),
                to_port=PortRef(node_id="approval", port="plan"),
            ),
            WorkflowEdge(
                from_port=PortRef(node_id="preview", port="report"),
                to_port=PortRef(node_id="approval", port="report"),
            ),
            WorkflowEdge(
                from_port=PortRef(node_id="planner", port="plan"),
                to_port=PortRef(node_id="apply", port="plan"),
            ),
            WorkflowEdge(
                from_port=PortRef(node_id="approval", port="token"),
                to_port=PortRef(node_id="apply", port="approval"),
            ),
        ),
        exposed_inputs=(
            ("assets", "input", "assets"),
            ("brief", "planner", "brief"),
        ),
        policy=WorkflowPolicy(),
    )
    return WorkflowTemplateEntry(
        template_id="wf-talking-head-fine-cut",
        display_name="口播精剪",
        description="据口播转录 + brief 生成精剪计划，审批后落时间线（plan §9.3）",
        category="edit",
        # plan §9.3：口播精剪 ≡ 内置 VOICEOVER_POLISH workflow（beats/策略一致），
        # 故路由到该官方 skill（与聊天入口的意图映射一致）。
        skill_id="topos.voiceover_polish",
        graph=graph,
        icon="scissors",
    )


def _product_rough_cut_template() -> WorkflowTemplateEntry:
    """plan §9.4 '产品粗剪' 模板。

    执行顺序：
    1. Selected Assets 读取产品视频素材
    2. Scene Cut 镜头切换检测 + Audio Features 音频特征（双分析并行）
    3. Rough Cut Plan 据 brief + 分析产粗剪 EditPlan
    4. Preview Diff 预览
    5. Approval 审批门禁
    """
    graph = WorkflowGraph(
        workflow_id="wf-product-rough-cut",
        name="产品粗剪",
        nodes=(
            WorkflowNode(
                node_id="input", spec_id="topos.media.selected_assets",
                params={"media_kind": "video"},
                ui={"x": 100, "y": 220},
            ),
            WorkflowNode(
                node_id="scene_cut", spec_id="topos.analysis.scene_cut",
                ui={"x": 340, "y": 120},
            ),
            WorkflowNode(
                node_id="audio_feat", spec_id="topos.analysis.audio_features",
                ui={"x": 340, "y": 320},
            ),
            WorkflowNode(
                node_id="planner", spec_id="topos.agent.rough_cut_plan",
                params={"target_duration": 90},
                ui={"x": 580, "y": 220},
            ),
            WorkflowNode(
                node_id="preview", spec_id="topos.timeline.preview_diff",
                ui={"x": 820, "y": 120},
            ),
            WorkflowNode(
                node_id="timeline_in", spec_id="topos.timeline.current",
                ui={"x": 100, "y": 400},
            ),
            WorkflowNode(
                node_id="approval", spec_id="topos.user.approval",
                ui={"x": 820, "y": 320},
            ),
        ),
        edges=(
            WorkflowEdge(
                from_port=PortRef(node_id="input", port="assets"),
                to_port=PortRef(node_id="scene_cut", port="assets"),
            ),
            WorkflowEdge(
                from_port=PortRef(node_id="input", port="assets"),
                to_port=PortRef(node_id="audio_feat", port="assets"),
            ),
            WorkflowEdge(
                from_port=PortRef(node_id="input", port="assets"),
                to_port=PortRef(node_id="planner", port="assets"),
            ),
            # 双路分析汇入 planner（analysis 端口 allow_multi=True——AUD-23206：
            # 此前 audio_feat 输出无任何消费边，音频特征白跑；plan §9.4 要求
            # "Scene Cut + Audio Features 双分析并行"共同驱动粗剪计划）。
            WorkflowEdge(
                from_port=PortRef(node_id="scene_cut", port="analysis"),
                to_port=PortRef(node_id="planner", port="analysis"),
            ),
            WorkflowEdge(
                from_port=PortRef(node_id="audio_feat", port="analysis"),
                to_port=PortRef(node_id="planner", port="analysis"),
            ),
            WorkflowEdge(
                from_port=PortRef(node_id="planner", port="plan"),
                to_port=PortRef(node_id="preview", port="plan"),
            ),
            WorkflowEdge(
                from_port=PortRef(node_id="timeline_in", port="timeline"),
                to_port=PortRef(node_id="preview", port="timeline"),
            ),
            WorkflowEdge(
                from_port=PortRef(node_id="planner", port="plan"),
                to_port=PortRef(node_id="approval", port="plan"),
            ),
            WorkflowEdge(
                from_port=PortRef(node_id="preview", port="report"),
                to_port=PortRef(node_id="approval", port="report"),
            ),
        ),
        exposed_inputs=(
            ("assets", "input", "assets"),
            ("brief", "planner", "brief"),
        ),
        policy=WorkflowPolicy(),
    )
    return WorkflowTemplateEntry(
        template_id="wf-product-rough-cut",
        display_name="产品粗剪",
        description="镜头切换 + 音频特征驱动的产品粗剪计划（plan §9.4）",
        category="edit",
        # plan §9.4：产品粗剪 ≡ 内置 PRODUCT_AD_30S workflow，故路由到该官方 skill。
        skill_id="topos.product_ad_30s",
        graph=graph,
        icon="film",
    )


def _composite_render_import_template() -> WorkflowTemplateEntry:
    """plan §9.5 '合成渲染后导入' 模板。

    执行顺序：
    1. Selected Assets 读取素材
    2. Composite Subgraph 引用已有 CompositeGraphData → render job
    3. Composite Render 渲染 → rendered_asset
    4. Composite Import Render Result 导入生成素材箱

    依赖 Composite 节点 spec（adapters.composite_adapter.register_composite_node_specs）。
    """
    graph = WorkflowGraph(
        workflow_id="wf-composite-render-import",
        name="合成渲染后导入",
        nodes=(
            WorkflowNode(
                node_id="input", spec_id="topos.media.selected_assets",
                params={"media_kind": "video_or_image"},
                ui={"x": 100, "y": 220},
            ),
            WorkflowNode(
                node_id="subgraph", spec_id="topos.composite.subgraph",
                ui={"x": 360, "y": 220},
            ),
            WorkflowNode(
                node_id="render", spec_id="topos.composite.render",
                ui={"x": 620, "y": 220},
            ),
            WorkflowNode(
                node_id="import", spec_id="topos.composite.import_render_result",
                ui={"x": 880, "y": 220},
            ),
        ),
        edges=(
            WorkflowEdge(
                from_port=PortRef(node_id="input", port="assets"),
                to_port=PortRef(node_id="subgraph", port="assets"),
            ),
            WorkflowEdge(
                from_port=PortRef(node_id="subgraph", port="render_job"),
                to_port=PortRef(node_id="render", port="render_job"),
            ),
            WorkflowEdge(
                from_port=PortRef(node_id="render", port="rendered_asset"),
                to_port=PortRef(node_id="import", port="rendered_asset"),
            ),
        ),
        exposed_inputs=(
            ("assets", "input", "assets"),
            ("composite_graph", "subgraph", "composite_graph"),
        ),
        policy=WorkflowPolicy(),
    )
    return WorkflowTemplateEntry(
        template_id="wf-composite-render-import",
        display_name="合成渲染后导入",
        description="Composite 子图渲染后导入生成素材箱（plan §9.5）",
        category="generation",
        skill_id="topos.composite_render_import",
        graph=graph,
        icon="layers",
    )


__all__ = [
    "WorkflowTemplateEntry",
    "WorkflowTemplateRegistry",
    "register_official_templates",
]
