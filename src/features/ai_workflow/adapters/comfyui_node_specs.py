"""P6.5 ComfyUI Workflow 节点 spec（plan §1.4.2 C2 工作流执行级 / §9.4）。

> ``ComfyUI Workflow`` 节点连接 ``text_document`` / ``asset_ref`` / ``selection_set``
> 等 Topos artifact；只允许填充该 workflow 预先声明的绑定槽位。

注册 ``topos.comfy.workflow`` 节点 spec（plan §1.4.2 C2 / §9.4 #2）。

红线（plan §9.4 #2 / §4.4 / §0 / §1.4.2）：

- **不暴露任意 ``class_type`` 改写**：``params_schema`` 只暴露安全绑定槽位
  （prompt / negative_prompt / seed / width / height / reference_assets /
  output_node_id / workflow_snapshot_id）。
- **输入用 Topos artifact**（text_document / asset_ref / selection_set），不传 raw JSON /
  texture。
- **输出 ``media_generation_ref``**（候选进 GenerationBin，不直接进 timeline）。
- ``executor_id`` 受控（TTS_PROVIDER 复用 generation provider lane；无任意代码执行）。
- ``network_required=True`` + ``estimated_cost > 0``（云 / 本机 Comfy server 都需确认）。
- ``additionalProperties=False``（避免任意字段进核心 schema）。
"""
from __future__ import annotations

from typing import Any, Tuple

from src.features.ai_workflow.core.node_specs import (
    NodeCategory,
    NodeExecutorId,
    NodePortSpec,
    NodeSpecRegistry,
    WorkflowNodeSpec,
)


def _input_port(name: str, atype: str, **kw: Any) -> NodePortSpec:
    return NodePortSpec(name=name, artifact_type=atype, direction="input", **kw)


def _output_port(name: str, atype: str, **kw: Any) -> NodePortSpec:
    return NodePortSpec(name=name, artifact_type=atype, direction="output", **kw)


def _comfy_node_specs() -> Tuple[WorkflowNodeSpec, ...]:
    """ComfyUI 相关节点 spec（plan §4.2 M9）。"""
    return (
        WorkflowNodeSpec(
            spec_id="topos.comfy.workflow",
            display_name="ComfyUI Workflow", category=NodeCategory.GENERATION,
            description=(
                "外部 ComfyUI workflow（API-format snapshot）；只允许填充预先声明的"
                "绑定槽位，不暴露任意 class_type 改写（plan §9.4 #2）"
            ),
            inputs=(
                _input_port("prompt", "text_document"),
                _input_port("negative_prompt", "text_document", required=False),
                _input_port("reference_assets", "asset_set", required=False),
                _input_port("selection", "selection_set", required=False),
            ),
            outputs=(_output_port("result", "media_generation_ref"),),
            required_capabilities=("image_gen",),  # 至少需要某种生成能力
            executor_id=NodeExecutorId.TTS_PROVIDER, resource_lane="generation_local",
            estimated_cost=0.3, network_required=True,
            params_schema={
                "type": "object",
                "properties": {
                    # 只暴露安全绑定槽位（plan §9.4 #2）
                    "prompt": {"type": "string", "description": "bind to workflow prompt slot"},
                    "negative_prompt": {"type": "string"},
                    "seed": {"type": "integer", "minimum": 0},
                    "width": {"type": "integer", "minimum": 64, "maximum": 8192},
                    "height": {"type": "integer", "minimum": 64, "maximum": 8192},
                    "reference_assets": {
                        "type": "array", "items": {"type": "string"},
                        "description": "Topos asset IDs to bind as reference inputs",
                    },
                    "output_node_id": {
                        "type": "string",
                        "description": "which SaveImage/SaveVideo node to harvest",
                    },
                    "workflow_snapshot_id": {
                        "type": "string",
                        "description": "Topos-side ComfyWorkflowSnapshot.workflow_id",
                    },
                    # bound_inputs 是命名空间：LoRA / ControlNet 等"未知 provider 字段"
                    # 只进这里，不进核心 schema（plan §4.3 末段）
                    "bound_inputs": {
                        "type": "object",
                        "description": (
                            "opaque per-workflow input bindings (e.g. LoRA weights, "
                            "ControlNet strength); NOT validated by Topos core schema"
                        ),
                        "additionalProperties": True,
                    },
                },
                "required": ["workflow_snapshot_id"],
                "additionalProperties": False,
            },
            default_params={
                "seed": 0, "width": 1024, "height": 1024,
            },
            cancelable=True, retry_policy="fail",
        ),
    )


def register_comfyui_node_specs(registry: NodeSpecRegistry) -> None:
    """注册 ComfyUI 相关节点 spec 到 :class:`NodeSpecRegistry`。"""
    for spec in _comfy_node_specs():
        registry.register(spec)
