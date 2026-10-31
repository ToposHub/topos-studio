"""P5.2 AI Workflow 节点 spec 注册表（plan §3.3 / §4.1 M4 16 节点）。

定义 :class:`WorkflowNodeSpec` —— 节点 spec 的声明式定义（plan §3.3 必填字段）：
spec_id / 版本 / 显示名 / 分类 / 图标 / 说明 / 作者 / 许可证 / 输入输出端口
（artifact type + 必填 + 多连接 + 流式）/ 参数 schema / 所需 capability / 资源
lane / 预估成本 / 网络/写权限级别 / executor_id / 可取消性 / retry policy /
失败策略。

设计边界（红线，遵循 plan §3.3 / §1.4 / §5.3）：

- spec **不拥有裸权限，只声明所需能力**（plan §5.3 第一条）。``required_capabilities``
  在编译期由 :class:`WorkflowCompiler` 检查 host 是否具备；不具备 → 拒绝执行。
- spec 的 ``executor_id`` **受控枚举**，不含 ``exec_python`` / ``shell`` / ``fs`` /
  ``ui``（红线：第三方不得执行任意代码）。
- 端口 artifact type 来自 plan §3.1 的 ``ArtifactType`` 表。

与既有模块的关系：

- :class:`WorkflowValidator`（本文件下半部分）用注册表检查 graph 的端口兼容性。
- :class:`WorkflowCompiler`（P5.3）用 spec 的 ``executor_id`` / ``required_capabilities``
  决定编译目标（taskgraph / workflow_runner / editorial_builder）。
- 不复用 Composite 的 ``node_capabilities.py``（那是渲染节点能力，两套东西）。
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Tuple

from src.features.ai_workflow.core.workflow_models import JsonValue

_LOG = logging.getLogger(__name__)

# 版本号里的数字段（"1.2.3-beta" → (1, 2, 3)）。
_VERSION_SEGMENT_RE = re.compile(r"^\d+")


# ---------------------------------------------------------------------- #
# NodeCategory（plan §4.1 分类）
# ---------------------------------------------------------------------- #


class NodeCategory:
    """节点分类（plan §4.1）。"""

    INPUT = "input"            # 输入节点（只读项目状态）
    ANALYSIS = "analysis"      # 分析（ASR/OCR/scene cut/audio）
    EDIT = "edit"              # 编辑（字幕/音频放置/粗剪）
    TEXT = "text"              # 文本（翻译/脚本）
    GENERATION = "generation"  # 生成（TTS/视频/音乐/图片）
    AGENT = "agent"            # 子智能体（TaskGraph）
    APPROVAL = "approval"      # 审批（diff/approval/apply）
    OUTPUT = "output"          # 输出（report/导出）
    CONTROL = "control"        # 控制（条件/合并；M6+）

    ALL: Tuple[str, ...] = (
        INPUT, ANALYSIS, EDIT, TEXT, GENERATION, AGENT, APPROVAL, OUTPUT, CONTROL,
    )


# ---------------------------------------------------------------------- #
# NodePortSpec —— 端口声明
# ---------------------------------------------------------------------- #


@dataclass(frozen=True)
class NodePortSpec:
    """节点端口声明（plan §3.3 输入/输出端口）。

    Attributes:
        name: 端口名（如 ``assets`` / ``transcript``）。
        artifact_type: plan §3.1 的 ArtifactType（如 ``asset_set`` / ``transcript_ref``）。
        direction: ``input`` / ``output``。
        required: 是否必填（输入端口）。
        allow_multi: 是否允许多连接（输入端口）。
        streaming: 是否流式（批量处理时，如 For Each Asset）。
        description: 端口说明。
    """

    name: str
    artifact_type: str
    direction: str
    required: bool = True
    allow_multi: bool = False
    streaming: bool = False
    description: str = ""

    def __post_init__(self) -> None:
        if self.direction not in ("input", "output"):
            raise ValueError(f"invalid port direction: {self.direction!r}")
        if not self.name:
            raise ValueError("port name cannot be empty")
        if not self.artifact_type:
            raise ValueError("port artifact_type cannot be empty")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name, "artifact_type": self.artifact_type,
            "direction": self.direction, "required": self.required,
            "allow_multi": self.allow_multi, "streaming": self.streaming,
            "description": self.description,
        }


# ---------------------------------------------------------------------- #
# 受控 executor_id（红线：不含 exec_python/shell/fs/ui）
# ---------------------------------------------------------------------- #


class NodeExecutorId:
    """节点 executor 的受控枚举（plan §3.3 executor_id）。

    红线：不含任意代码执行后端（``exec_python`` / ``shell`` / ``fs`` / ``ui``）。
    第三方节点只能用这些受控 executor。
    """

    ANALYSIS_PROVIDER = "analysis_provider"      # ASR/OCR/scene cut/audio provider
    TTS_PROVIDER = "tts_provider"
    TRANSLATE_PROVIDER = "translate_provider"
    LLM_ROLE = "llm_role"                        # 子智能体角色（TaskGraph）
    TASKGRAPH = "taskgraph"                      # 多角色 TaskGraph
    WORKFLOW_RUNNER = "workflow_runner"          # M7 WorkflowRunner
    EDITORIAL_BUILDER = "editorial_builder"      # 纯 Python 构造器
    READ_ONLY = "read_only"                      # 只读（输入/检索）
    CONTROL = "control"                          # 控制流（条件/合并）
    RENDER = "render"                            # 合成渲染（M6+）
    FFMPEG = "ffmpeg"                            # 音轨提取等 ffmpeg Job

    ALL: Tuple[str, ...] = (
        ANALYSIS_PROVIDER, TTS_PROVIDER, TRANSLATE_PROVIDER, LLM_ROLE,
        TASKGRAPH, WORKFLOW_RUNNER, EDITORIAL_BUILDER, READ_ONLY,
        CONTROL, RENDER, FFMPEG,
    )


# ---------------------------------------------------------------------- #
# WorkflowNodeSpec —— 节点 spec
# ---------------------------------------------------------------------- #


@dataclass(frozen=True)
class WorkflowNodeSpec:
    """节点 spec 的声明式定义（plan §3.3 必填字段）。

    frozen + 版本化。注册到 :class:`NodeSpecRegistry` 后供 validator / compiler 查询。
    """

    spec_id: str                              # 如 "topos.analysis.transcribe_auto"
    version: str = "1.0.0"
    display_name: str = ""
    category: str = NodeCategory.CONTROL
    icon: str = ""
    description: str = ""
    author: str = "Topos"
    license: str = "Topos-proprietary"

    # 端口
    inputs: Tuple[NodePortSpec, ...] = ()
    outputs: Tuple[NodePortSpec, ...] = ()

    # 参数 schema（JSON Schema 片段）
    params_schema: Dict[str, JsonValue] = field(default_factory=dict)
    default_params: Dict[str, JsonValue] = field(default_factory=dict)

    # 能力 / 资源 / 权限
    required_capabilities: Tuple[str, ...] = ()  # asr/ocr/tts/llm/translate/...
    resource_lane: str = "analysis_local"        # plan §6.2 lane
    estimated_cost: float = 0.0
    network_required: bool = False
    timeline_write_required: bool = False

    # 执行
    executor_id: str = NodeExecutorId.READ_ONLY
    cancelable: bool = True
    retry_policy: str = "fail"  # fail / skip / fallback / ask_user（plan §3.3）
    failure_strategy: str = "fail"

    def __post_init__(self) -> None:
        if not self.spec_id:
            raise ValueError("spec_id cannot be empty")
        parts = self.spec_id.split(".")
        if len(parts) < 3 or parts[0] != "topos":
            raise ValueError(
                f"spec_id must follow 'topos.<domain>.<capability>': {self.spec_id!r}"
            )
        if self.category not in NodeCategory.ALL:
            raise ValueError(f"invalid category: {self.category!r}")
        if self.executor_id not in NodeExecutorId.ALL:
            raise ValueError(f"invalid executor_id: {self.executor_id!r}")
        if self.retry_policy not in ("fail", "skip", "fallback", "ask_user"):
            raise ValueError(f"invalid retry_policy: {self.retry_policy!r}")

    @property
    def input_ports(self) -> Tuple[NodePortSpec, ...]:
        return tuple(p for p in self.inputs if p.direction == "input")

    @property
    def output_ports(self) -> Tuple[NodePortSpec, ...]:
        return tuple(p for p in self.outputs if p.direction == "output")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "spec_id": self.spec_id,
            "version": self.version,
            "display_name": self.display_name,
            "category": self.category,
            "icon": self.icon,
            "description": self.description,
            "author": self.author,
            "license": self.license,
            "inputs": [p.to_dict() for p in self.inputs],
            "outputs": [p.to_dict() for p in self.outputs],
            "params_schema": dict(self.params_schema),
            "default_params": dict(self.default_params),
            "required_capabilities": list(self.required_capabilities),
            "resource_lane": self.resource_lane,
            "estimated_cost": self.estimated_cost,
            "network_required": self.network_required,
            "timeline_write_required": self.timeline_write_required,
            "executor_id": self.executor_id,
            "cancelable": self.cancelable,
            "retry_policy": self.retry_policy,
            "failure_strategy": self.failure_strategy,
        }


# ---------------------------------------------------------------------- #
# NodeSpecRegistry
# ---------------------------------------------------------------------- #


def _version_key(version: str) -> Tuple[int, ...]:
    """把语义化版本字符串解析为可比较的整数元组（AUD-23200）。

    ``"1.2.3"`` → ``(1, 2, 3)``；``"2.0"`` → ``(2, 0)``；无法解析的段按 ``0``
    处理。返回值保证同长度比较时语义正确（调用方用元组比较，缺失位视作 0）。
    """
    segments: List[int] = []
    for part in str(version).split("."):
        m = _VERSION_SEGMENT_RE.match(part.strip())
        segments.append(int(m.group(0)) if m else 0)
    return tuple(segments) or (0,)


class NodeSpecRegistry:
    """节点 spec 注册表（plan §4.1）。

    in-memory；``register_official_specs`` 装配 plan §4.1 的 16 个 M4 节点。
    """

    def __init__(self) -> None:
        self._specs: Dict[str, WorkflowNodeSpec] = {}

    def register(self, spec: WorkflowNodeSpec) -> None:
        """注册 spec；同版本幂等，**拒绝版本降级**（AUD-23200）。

        - 同 ``spec_id`` 同 ``version`` → 幂等 no-op。
        - 新版本 **高于** 已注册版本 → 覆盖（版本升级）。
        - 新版本 **不高于** 已注册版本（降级或乱序注册）→ 保留已注册的较新
          版本并记录 warning（注册顺序不再决定最终版本）。
        """
        existing = self._specs.get(spec.spec_id)
        if existing is not None:
            if _version_key(existing.version) == _version_key(spec.version):
                return  # 幂等（同版本）
            if _version_key(spec.version) > _version_key(existing.version):
                _LOG.info(
                    "node spec upgraded: %s %s -> %s",
                    spec.spec_id, existing.version, spec.version,
                )
            else:
                _LOG.warning(
                    "ignoring node spec downgrade: %s registered=%s incoming=%s",
                    spec.spec_id, existing.version, spec.version,
                )
                return
        self._specs[spec.spec_id] = spec

    def get(self, spec_id: str) -> WorkflowNodeSpec | None:
        return self._specs.get(spec_id)

    def require(self, spec_id: str) -> WorkflowNodeSpec:
        s = self.get(spec_id)
        if s is None:
            raise KeyError(f"unknown node spec: {spec_id!r}")
        return s

    def all_specs(self) -> Tuple[WorkflowNodeSpec, ...]:
        return tuple(self._specs.values())

    def list_by_category(self, category: str) -> Tuple[WorkflowNodeSpec, ...]:
        return tuple(s for s in self._specs.values() if s.category == category)

    @property
    def count(self) -> int:
        return len(self._specs)


# ---------------------------------------------------------------------- #
# M4 16 个官方节点 spec（plan §4.1）
# ---------------------------------------------------------------------- #


def _input_port(name: str, atype: str, **kw: Any) -> NodePortSpec:
    return NodePortSpec(name=name, artifact_type=atype, direction="input", **kw)


def _output_port(name: str, atype: str, **kw: Any) -> NodePortSpec:
    return NodePortSpec(name=name, artifact_type=atype, direction="output", **kw)


def _official_m4_specs() -> Tuple[WorkflowNodeSpec, ...]:
    return (
        # ---- 输入 ----
        WorkflowNodeSpec(
            spec_id="topos.media.selected_assets",
            display_name="Selected Assets", category=NodeCategory.INPUT,
            description="读取用户已选择的素材 ID（只读当前选择）",
            inputs=(),
            outputs=(_output_port("assets", "asset_set", description="已选素材集合"),),
            params_schema={"type": "object", "properties": {
                # 官方模板实际使用的参数（AUD-23219：schema 与模板声明对齐）
                "media_kind": {"type": "string"},
            }},
            executor_id=NodeExecutorId.READ_ONLY, resource_lane="control",
            cancelable=False,
        ),
        WorkflowNodeSpec(
            spec_id="topos.timeline.current",
            display_name="Current Timeline", category=NodeCategory.INPUT,
            description="读取当前 timeline ID + revision（只读）",
            inputs=(),
            outputs=(_output_port("timeline", "timeline_ref"),),
            executor_id=NodeExecutorId.READ_ONLY, resource_lane="control",
            cancelable=False,
        ),
        # ---- 分析 ----
        WorkflowNodeSpec(
            spec_id="topos.media.extract_audio",
            display_name="Extract Audio", category=NodeCategory.ANALYSIS,
            description="ffmpeg 提取音轨（不改工程）",
            inputs=(_input_port("asset", "asset_ref"),),
            outputs=(_output_port("audio", "media_generation_ref"),),
            required_capabilities=("ffmpeg",),
            executor_id=NodeExecutorId.FFMPEG, resource_lane="analysis_local",
            estimated_cost=0.01,
        ),
        WorkflowNodeSpec(
            spec_id="topos.analysis.transcribe_auto",
            display_name="Transcribe (Auto)", category=NodeCategory.ANALYSIS,
            description="复用 Qwen → Demucs → WhisperX 路由转录",
            inputs=(_input_port("assets", "asset_set"),),
            outputs=(
                _output_port("transcript", "transcript_ref"),
                _output_port("analysis", "analysis_ref"),
            ),
            required_capabilities=("asr",),
            executor_id=NodeExecutorId.ANALYSIS_PROVIDER,
            resource_lane="analysis_local", estimated_cost=0.05,
            params_schema={"type": "object", "properties": {
                "language": {"type": "string"},
                "enable_diarization": {"type": "boolean"},
            }},
        ),
        WorkflowNodeSpec(
            spec_id="topos.analysis.scene_cut",
            display_name="Scene Cut", category=NodeCategory.ANALYSIS,
            description="镜头切换检测",
            inputs=(_input_port("assets", "asset_set"),),
            outputs=(_output_port("analysis", "analysis_ref"),),
            required_capabilities=("scene_cut",),
            executor_id=NodeExecutorId.ANALYSIS_PROVIDER, resource_lane="analysis_local",
        ),
        WorkflowNodeSpec(
            spec_id="topos.analysis.ocr",
            display_name="OCR", category=NodeCategory.ANALYSIS,
            description="RapidOCR 文字识别（untrusted）",
            inputs=(_input_port("assets", "asset_set"),),
            outputs=(_output_port("analysis", "analysis_ref"),),
            required_capabilities=("ocr",),
            executor_id=NodeExecutorId.ANALYSIS_PROVIDER, resource_lane="analysis_local",
        ),
        WorkflowNodeSpec(
            spec_id="topos.analysis.audio_features",
            display_name="Audio Features", category=NodeCategory.ANALYSIS,
            description="BPM / loudness / speech ranges",
            inputs=(_input_port("assets", "asset_set"),),
            outputs=(_output_port("analysis", "analysis_ref"),),
            required_capabilities=("audio_features",),
            executor_id=NodeExecutorId.ANALYSIS_PROVIDER, resource_lane="analysis_local",
        ),
        # ---- 编辑 / 文本 / 生成 ----
        WorkflowNodeSpec(
            spec_id="topos.caption.propose_track",
            display_name="Propose Subtitle Track", category=NodeCategory.EDIT,
            description="把时间戳 transcript 转 EditPlan（不立即创建轨道）",
            inputs=(_input_port("transcript", "transcript_ref", required=True),),
            outputs=(
                _output_port("plan", "edit_plan_ref"),
                _output_port("subtitle_track", "subtitle_track_ref"),
            ),
            params_schema={"type": "object", "properties": {
                # 官方模板实际使用的参数（AUD-23219：schema 与模板声明对齐）
                "track_name": {"type": "string"},
            }},
            executor_id=NodeExecutorId.EDITORIAL_BUILDER, resource_lane="control",
            cancelable=False,
        ),
        WorkflowNodeSpec(
            spec_id="topos.text.translate",
            display_name="Translate", category=NodeCategory.TEXT,
            description="翻译字幕/文本（local/cloud profile 明示）",
            inputs=(_input_port("source", "subtitle_track_ref", required=False),
                    _input_port("text", "text_document", required=False),),
            outputs=(_output_port("text", "text_document"),),
            required_capabilities=("translate",),
            executor_id=NodeExecutorId.TRANSLATE_PROVIDER, resource_lane="network",
            network_required=True, estimated_cost=0.02,
            params_schema={"type": "object", "properties": {
                "target_language": {"type": "string"},
            }, "required": ["target_language"]},
        ),
        WorkflowNodeSpec(
            spec_id="topos.tts.qwen3",
            display_name="Qwen3 TTS", category=NodeCategory.GENERATION,
            description="Qwen3-TTS 合成（输出先入生成素材箱）",
            inputs=(_input_port("subtitle", "subtitle_track_ref", required=False),
                    _input_port("text", "text_document", required=False),),
            outputs=(_output_port("audio", "media_generation_ref"),),
            required_capabilities=("tts",),
            executor_id=NodeExecutorId.TTS_PROVIDER, resource_lane="generation_local",
            estimated_cost=0.1,
        ),
        WorkflowNodeSpec(
            spec_id="topos.timeline.propose_audio_placement",
            display_name="Propose Audio Placement", category=NodeCategory.EDIT,
            description="生成音轨插入 EditPlan（不直接插音轨）",
            inputs=(_input_port("audio", "media_generation_ref"),
                    _input_port("timeline", "timeline_ref"),),
            outputs=(_output_port("plan", "edit_plan_ref"),),
            executor_id=NodeExecutorId.EDITORIAL_BUILDER, resource_lane="control",
            cancelable=False,
        ),
        WorkflowNodeSpec(
            spec_id="topos.agent.rough_cut_plan",
            display_name="Rough Cut Plan", category=NodeCategory.AGENT,
            description="复用 TaskGraph / WorkflowRunner 产粗剪 EditPlan",
            inputs=(_input_port("assets", "asset_set"),
                    # analysis 允许多路连入（scene_cut + audio_features 双分析并行，
                    # plan §9.4；AUD-23206/AUD-23218）
                    _input_port("analysis", "analysis_ref", required=False,
                                allow_multi=True),
                    _input_port("brief", "text_document", required=False),),
            outputs=(_output_port("plan", "edit_plan_ref"),
                     _output_port("report", "report"),),
            required_capabilities=("llm",),
            executor_id=NodeExecutorId.TASKGRAPH, resource_lane="analysis_local",
            estimated_cost=0.1,
            params_schema={"type": "object", "properties": {
                # 官方模板实际使用的参数（AUD-23219：schema 与模板声明对齐；
                # 单位：秒）
                "target_duration": {"type": "integer"},
            }},
        ),
        # ---- 审批 / 输出 ----
        WorkflowNodeSpec(
            spec_id="topos.timeline.preview_diff",
            display_name="Preview Diff", category=NodeCategory.APPROVAL,
            description="revision/hash 校验，显示 diff",
            inputs=(_input_port("plan", "edit_plan_ref"),
                    _input_port("timeline", "timeline_ref"),),
            outputs=(_output_port("report", "report"),),
            executor_id=NodeExecutorId.READ_ONLY, resource_lane="control",
            cancelable=False,
        ),
        WorkflowNodeSpec(
            spec_id="topos.user.approval",
            display_name="User Approval", category=NodeCategory.APPROVAL,
            description="UI 明确确认；不允许自动批准（红线）",
            inputs=(_input_port("plan", "edit_plan_ref"),
                    _input_port("report", "report", required=False),),
            outputs=(_output_port("token", "approval_token"),),
            executor_id=NodeExecutorId.READ_ONLY, resource_lane="control",
            cancelable=False, retry_policy="ask_user",
        ),
        WorkflowNodeSpec(
            spec_id="topos.timeline.apply_approved_plan",
            display_name="Apply Approved Plan", category=NodeCategory.OUTPUT,
            description="CommandStack 唯一写入口（需 approval_token）",
            inputs=(_input_port("plan", "edit_plan_ref"),
                    _input_port("approval", "approval_token"),),
            outputs=(_output_port("timeline", "timeline_ref"),
                     _output_port("report", "report"),),
            executor_id=NodeExecutorId.WORKFLOW_RUNNER, resource_lane="render",
            timeline_write_required=True, cancelable=False,
        ),
        WorkflowNodeSpec(
            spec_id="topos.report.run_summary",
            display_name="Run Summary", category=NodeCategory.OUTPUT,
            description="汇总 provenance / 成本 / 失败",
            inputs=(_input_port("any", "report", allow_multi=True),),
            outputs=(_output_port("summary", "report"),),
            executor_id=NodeExecutorId.READ_ONLY, resource_lane="control",
            cancelable=False,
        ),
    )


def register_official_specs(registry: NodeSpecRegistry) -> None:
    """把官方节点 spec 注册进 registry。

    包含：

    - plan §4.1 的 16 个 M4 核心节点 spec。
    - plan §10.8 P6 的 Composite Subgraph/Render/Import spec（合成渲染后导入模板用）。
    - plan §10.8 P6 的 ComfyUI Workflow spec（外部子图节点）。
    - plan §4.3 P6.5 的 6 个生成节点 spec（文生图/图生图/重绘/文生视频/
      图生视频/放大；AUD-23113——此前 ``register_generation_node_specs``
      仅被测试调用，生产装配不可达）。

    Composite/ComfyUI/Generation spec 是可选依赖（adapters 包），注册失败只
    记日志不阻断（无 adapters 包时画布 Tab 搜索仍可见 16 个核心 spec）。
    """
    for spec in _official_m4_specs():
        registry.register(spec)
    # P10.3：Composite + ComfyUI spec 接进画布（Tab 搜索可见）。
    # adapters 是可选依赖：只豁免 ImportError（包缺失）；适配器自身的
    # TypeError/ValueError 等真实缺陷必须照常抛出（不得宽泛 except 吞掉，
    # AUD-23201），否则 spec 无声消失且无诊断。
    try:
        from src.features.ai_workflow.adapters.composite_adapter import (
            register_composite_node_specs,
        )
    except ImportError as exc:
        _LOG.warning(
            "composite node specs unavailable (adapters missing): %s", exc,
        )
    else:
        register_composite_node_specs(registry)
    try:
        from src.features.ai_workflow.adapters.comfyui_node_specs import (
            register_comfyui_node_specs,
        )
    except ImportError as exc:
        _LOG.warning(
            "comfyui node specs unavailable (adapters missing): %s", exc,
        )
    else:
        register_comfyui_node_specs(registry)
    try:
        from src.features.ai_workflow.adapters.generation_node_specs import (
            register_generation_node_specs,
        )
    except ImportError as exc:
        _LOG.warning(
            "generation node specs unavailable (adapters missing): %s", exc,
        )
    else:
        register_generation_node_specs(registry)


__all__ = [
    "NodeCategory",
    "NodePortSpec",
    "NodeExecutorId",
    "WorkflowNodeSpec",
    "NodeSpecRegistry",
    "register_official_specs",
]
