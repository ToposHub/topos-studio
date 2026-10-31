"""P6.5 生成节点 spec 扩充（plan §4.3 图片/视频生成能力模型 + §4.4）。

> 图片和视频生成不能只有一个 "Prompt → 文件" 的过小接口。Workflow 节点需要以
> provider-neutral 的 ``GenerationRequest`` 为核心，同时把复杂控制项收进可验证的
> ``GenerationControls`` schema，而不是任意 ``dict``。

提供两件事（plan §4.3 / §4.4）：

1. :func:`register_generation_node_specs` — 注册 6 个生成节点 spec：
   - ``topos.generation.text_to_image``（文生图）
   - ``topos.generation.image_to_image``（图生图）
   - ``topos.generation.inpaint``（局部重绘）
   - ``topos.generation.text_to_video``（文生视频）
   - ``topos.generation.image_to_video``（图生视频）
   - ``topos.generation.upscale``（后处理：放大/补帧/抠像）
2. :class:`GenerationControlsSchema` — provider-neutral 的生成控制 schema
   （plan §4.3）。每个字段标注：JSON Schema 类型 / 单位 / 范围 / 是否影响 cache /
   是否上传参考素材 / 是否需模型 pack / 是否增加成本。

红线（plan §4.3 / §4.4 / §0 / §1.4.2）：

- **executor_id 受控**：生成节点用 ``TTS_PROVIDER``（复用 generation provider lane；
  无任意代码执行后端）。
- **不直接写 timeline**：所有生成节点 ``timeline_write_required=False``；输出
  ``media_generation_ref``（候选进 :class:`GenerationBin`，进 timeline 走另一条
  approval 链）。
- **不输出 texture**：输出是 ``media_generation_ref`` / ``asset_ref``，不是 GPU texture。
- **LoRA / ControlNet / IP-Adapter 等"未知 provider 字段"只进 Comfy workflow 的
  ``bound_inputs``**，不进 Topos 核心 schema（plan §4.3 末段）。
- **不暴露"完全访问"/"跳过审批"/"忽略许可证"** 节点（§4.4）。
- 每个字段必须有 JSON Schema + 单位/范围 + cache/upload/cost 影响标注（plan §4.3）。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Tuple

from src.features.ai_workflow.core.node_specs import (
    NodeCategory,
    NodeExecutorId,
    NodePortSpec,
    NodeSpecRegistry,
    WorkflowNodeSpec,
)


# ---------------------------------------------------------------------- #
# helpers
# ---------------------------------------------------------------------- #


def _input_port(name: str, atype: str, **kw: Any) -> NodePortSpec:
    return NodePortSpec(name=name, artifact_type=atype, direction="input", **kw)


def _output_port(name: str, atype: str, **kw: Any) -> NodePortSpec:
    return NodePortSpec(name=name, artifact_type=atype, direction="output", **kw)


# ---------------------------------------------------------------------- #
# 6 个生成节点 spec
# ---------------------------------------------------------------------- #


def _generation_specs() -> Tuple[WorkflowNodeSpec, ...]:
    """plan §4.3 的 6 个生成节点 spec（在 GenerationControlsSchema 定义后调用）。

    红线：
    - 所有生成节点 ``network_required=True`` + ``estimated_cost > 0``（云 provider 必需）。
    - ``executor_id=TTS_PROVIDER``（复用 generation provider lane；受控）。
    - ``timeline_write_required=False``（候选进 GenerationBin，不直接进 timeline）。
    """
    return (
        # 文生图
        WorkflowNodeSpec(
            spec_id="topos.generation.text_to_image",
            display_name="Text to Image", category=NodeCategory.GENERATION,
            description="文生图（prompt + 宽高 + seed → media_generation_ref）",
            inputs=(_input_port("prompt", "text_document"),
                    _input_port("negative", "text_document", required=False),),
            outputs=(_output_port("image", "media_generation_ref"),),
            required_capabilities=("image_gen",),
            executor_id=NodeExecutorId.TTS_PROVIDER, resource_lane="generation_local",
            estimated_cost=0.2, network_required=True,
            params_schema=GenerationControlsSchema.text_to_image().to_json_schema(),
        ),
        # 图生图
        WorkflowNodeSpec(
            spec_id="topos.generation.image_to_image",
            display_name="Image to Image", category=NodeCategory.GENERATION,
            description="图生图（源图 + prompt + denoise → media_generation_ref）",
            inputs=(_input_port("source_image", "asset_ref"),
                    _input_port("prompt", "text_document"),
                    _input_port("negative", "text_document", required=False),),
            outputs=(_output_port("image", "media_generation_ref"),),
            required_capabilities=("image_gen",),
            executor_id=NodeExecutorId.TTS_PROVIDER, resource_lane="generation_local",
            estimated_cost=0.25, network_required=True,
            params_schema=GenerationControlsSchema.image_to_image().to_json_schema(),
        ),
        # 局部重绘
        WorkflowNodeSpec(
            spec_id="topos.generation.inpaint",
            display_name="Inpaint", category=NodeCategory.GENERATION,
            description="局部重绘（原图 + mask + prompt → media_generation_ref）",
            inputs=(_input_port("source_image", "asset_ref"),
                    _input_port("mask", "asset_ref"),
                    _input_port("prompt", "text_document"),),
            outputs=(_output_port("image", "media_generation_ref"),),
            required_capabilities=("image_gen",),
            executor_id=NodeExecutorId.TTS_PROVIDER, resource_lane="generation_local",
            estimated_cost=0.3, network_required=True,
            params_schema=GenerationControlsSchema.inpaint().to_json_schema(),
        ),
        # 文生视频
        WorkflowNodeSpec(
            spec_id="topos.generation.text_to_video",
            display_name="Text to Video", category=NodeCategory.GENERATION,
            description="文生视频（prompt + duration + resolution → media_generation_ref）",
            inputs=(_input_port("prompt", "text_document"),
                    _input_port("first_frame", "asset_ref", required=False),
                    _input_port("last_frame", "asset_ref", required=False),),
            outputs=(_output_port("video", "media_generation_ref"),),
            required_capabilities=("video_gen",),
            executor_id=NodeExecutorId.TTS_PROVIDER, resource_lane="generation_local",
            estimated_cost=1.0, network_required=True,
            params_schema=GenerationControlsSchema.text_to_video().to_json_schema(),
        ),
        # 图生视频
        WorkflowNodeSpec(
            spec_id="topos.generation.image_to_video",
            display_name="Image to Video", category=NodeCategory.GENERATION,
            description="图生视频（参考图 + motion → media_generation_ref）",
            inputs=(_input_port("reference_image", "asset_ref"),
                    _input_port("prompt", "text_document", required=False),),
            outputs=(_output_port("video", "media_generation_ref"),),
            required_capabilities=("video_gen",),
            executor_id=NodeExecutorId.TTS_PROVIDER, resource_lane="generation_local",
            estimated_cost=1.2, network_required=True,
            params_schema=GenerationControlsSchema.image_to_video().to_json_schema(),
        ),
        # 后处理：放大（可扩展为补帧/抠像）
        WorkflowNodeSpec(
            spec_id="topos.generation.upscale",
            display_name="Upscale", category=NodeCategory.GENERATION,
            description="后处理：放大 / 补帧 / 去闪烁 / 抠像（plan §4.3 后处理）",
            inputs=(_input_port("source", "media_generation_ref"),
                    _input_port("source_asset", "asset_ref", required=False),),
            outputs=(_output_port("result", "media_generation_ref"),),
            required_capabilities=("image_gen",),
            executor_id=NodeExecutorId.TTS_PROVIDER, resource_lane="generation_local",
            estimated_cost=0.15, network_required=True,
            params_schema=GenerationControlsSchema.upscale().to_json_schema(),
        ),
    )


# GENERATION_SPECS 在文件末尾赋值（依赖 GenerationControlsSchema）
# 见 GENERATION_SPECS 定义。


# ---------------------------------------------------------------------- #
# GenerationControlsSchema —— plan §4.3
# ---------------------------------------------------------------------- #


# 单个字段的元数据（plan §4.3："有 JSON Schema、单位/范围、是否影响 cache、是否会上传
# 参考素材、是否需要模型 pack、是否增加成本"）
@dataclass(frozen=True)
class _FieldMeta:
    """生成控制字段的元数据。"""

    type: str                       # JSON Schema type
    unit: str = ""                  # 单位（如 "pixel" / "second" / "step"）
    range: Tuple[float, float] = (0.0, 0.0)   # 范围（min, max）；空 = 无限制
    default: Any = None
    cache_impact: bool = False      # 是否影响 cache（plan §4.3）
    upload_impact: bool = False     # 是否上传参考素材
    model_pack_required: bool = False  # 是否需模型 pack
    cost_impact: bool = False       # 是否增加成本
    description: str = ""

    def to_json_schema(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {"type": self.type, "description": self.description}
        if self.default is not None:
            d["default"] = self.default
        if self.range != (0.0, 0.0):
            d["minimum"] = self.range[0]
            d["maximum"] = self.range[1]
        return d


@dataclass(frozen=True)
class GenerationControlsSchema:
    """provider-neutral 的生成控制 schema（plan §4.3）。

    每个能力组（文生图 / 图生图 / 文生视频 ...）有独立的字段集合。每个字段标注：
    JSON Schema 类型 / 单位 / 范围 / 是否影响 cache / 是否上传参考素材 / 是否需模型 pack /
    是否增加成本。

    红线（plan §4.3 末段）：LoRA / ControlNet / IP-Adapter 等"未知 provider 字段"
    **不**进 Topos 核心 schema；它们只存于 Comfy workflow 的 ``bound_inputs``。
    """

    fields: Tuple[Tuple[str, _FieldMeta], ...] = ()

    @property
    def field_metadata(self) -> Dict[str, Dict[str, Any]]:
        """字段 → 元数据 dict（含 unit / range / cache_impact 等）。"""
        out: Dict[str, Dict[str, Any]] = {}
        for fname, m in self.fields:
            out[fname] = {
                "type": m.type,
                "unit": m.unit,
                "range": list(m.range),
                "default": m.default,
                "cache_impact": m.cache_impact,
                "upload_impact": m.upload_impact,
                "model_pack_required": m.model_pack_required,
                "cost_impact": m.cost_impact,
                "description": m.description,
            }
        return out

    def to_json_schema(self) -> Dict[str, Any]:
        """转 JSON Schema 片段（用于 WorkflowNodeSpec.params_schema）。"""
        properties: Dict[str, Any] = {}
        required: list[str] = []
        for fname, m in self.fields:
            properties[fname] = m.to_json_schema()
            # prompt / 基本尺寸字段为 required
            if fname in ("prompt",) and m.type == "string":
                required.append(fname)
        return {
            "type": "object",
            "properties": properties,
            "required": required,
            "additionalProperties": False,  # 红线：未知字段不进核心 schema
        }

    # ---------------- 能力组工厂 ---------------- #

    @classmethod
    def text_to_image(cls) -> "GenerationControlsSchema":
        """文生图（plan §4.3）：prompt / negative / width / height / seed / batch /
        steps / guidance。"""
        return cls(fields=(
            ("prompt", _FieldMeta(
                type="string", unit="", default="",
                cache_impact=True, cost_impact=True,
                description="positive prompt",
            )),
            ("negative_prompt", _FieldMeta(
                type="string", unit="", default="",
                cache_impact=True,
                description="negative prompt",
            )),
            ("width", _FieldMeta(
                type="integer", unit="pixel", range=(64, 8192), default=1024,
                cache_impact=True, cost_impact=True,
                description="output image width",
            )),
            ("height", _FieldMeta(
                type="integer", unit="pixel", range=(64, 8192), default=1024,
                cache_impact=True, cost_impact=True,
                description="output image height",
            )),
            ("seed", _FieldMeta(
                type="integer", unit="", range=(0, 2**32 - 1), default=0,
                cache_impact=True,
                description="reproducibility seed",
            )),
            ("batch_size", _FieldMeta(
                type="integer", unit="count", range=(1, 16), default=1,
                cache_impact=False, cost_impact=True,
                description="number of candidates",
            )),
            ("steps", _FieldMeta(
                type="integer", unit="step", range=(1, 200), default=30,
                cache_impact=False, cost_impact=True,
                description="diffusion sampling steps",
            )),
            ("guidance", _FieldMeta(
                type="number", unit="", range=(0.0, 30.0), default=7.0,
                cache_impact=False, cost_impact=False,
                description="classifier-free guidance scale",
            )),
        ))

    @classmethod
    def image_to_image(cls) -> "GenerationControlsSchema":
        """图生图（plan §4.3）：denoise_strength / prompt / seed。"""
        return cls(fields=(
            ("prompt", _FieldMeta(
                type="string", unit="", default="",
                cache_impact=True, cost_impact=True,
                description="positive prompt",
            )),
            ("denoise_strength", _FieldMeta(
                type="number", unit="", range=(0.0, 1.0), default=0.6,
                cache_impact=True, cost_impact=True,
                description="how much to deviate from source image",
            )),
            ("seed", _FieldMeta(
                type="integer", unit="", range=(0, 2**32 - 1), default=0,
                cache_impact=True,
                description="reproducibility seed",
            )),
            ("steps", _FieldMeta(
                type="integer", unit="step", range=(1, 200), default=30,
                cache_impact=False, cost_impact=True,
                description="diffusion sampling steps",
            )),
        ))

    @classmethod
    def text_to_video(cls) -> "GenerationControlsSchema":
        """文生视频（plan §4.3）：duration / resolution / fps / seed / first_frame。"""
        return cls(fields=(
            ("prompt", _FieldMeta(
                type="string", unit="", default="",
                cache_impact=True, cost_impact=True,
                description="positive prompt",
            )),
            ("duration_seconds", _FieldMeta(
                type="number", unit="second", range=(0.5, 60.0), default=4.0,
                cache_impact=True, cost_impact=True,
                description="target video duration",
            )),
            ("resolution", _FieldMeta(
                type="string", unit="", default="1280x720",
                cache_impact=True, cost_impact=True,
                description="output resolution (WxH)",
            )),
            ("fps", _FieldMeta(
                type="integer", unit="fps", range=(1, 60), default=24,
                cache_impact=False, cost_impact=True,
                description="output frame rate",
            )),
            ("seed", _FieldMeta(
                type="integer", unit="", range=(0, 2**32 - 1), default=0,
                cache_impact=True,
                description="reproducibility seed",
            )),
        ))

    @classmethod
    def inpaint(cls) -> "GenerationControlsSchema":
        """局部重绘（plan §4.3 / AUD-23114）：prompt / denoise_strength /
        mask_blur / seed / steps（每个字段带 JSON Schema + 单位/范围标注）。"""
        return cls(fields=(
            ("prompt", _FieldMeta(
                type="string", unit="", default="",
                cache_impact=True, cost_impact=True,
                description="positive prompt (what to repaint inside the mask)",
            )),
            ("denoise_strength", _FieldMeta(
                type="number", unit="", range=(0.0, 1.0), default=0.8,
                cache_impact=True, cost_impact=True,
                description="how much to deviate from the masked source region",
            )),
            ("mask_blur", _FieldMeta(
                type="integer", unit="pixel", range=(0, 64), default=8,
                cache_impact=False, cost_impact=False,
                description="mask edge blur radius (feathering)",
            )),
            ("seed", _FieldMeta(
                type="integer", unit="", range=(0, 2**32 - 1), default=0,
                cache_impact=True,
                description="reproducibility seed",
            )),
            ("steps", _FieldMeta(
                type="integer", unit="step", range=(1, 200), default=30,
                cache_impact=False, cost_impact=True,
                description="diffusion sampling steps",
            )),
        ))

    @classmethod
    def image_to_video(cls) -> "GenerationControlsSchema":
        """图生视频（plan §4.3 / AUD-23114）：prompt / motion_strength /
        duration_seconds / resolution / fps / seed。"""
        return cls(fields=(
            ("prompt", _FieldMeta(
                type="string", unit="", default="",
                cache_impact=True, cost_impact=False,
                description="optional motion/scene prompt",
            )),
            ("motion_strength", _FieldMeta(
                type="number", unit="", range=(0.0, 1.0), default=0.5,
                cache_impact=True, cost_impact=True,
                description="camera/subject motion intensity",
            )),
            ("duration_seconds", _FieldMeta(
                type="number", unit="second", range=(0.5, 60.0), default=4.0,
                cache_impact=True, cost_impact=True,
                description="target video duration",
            )),
            ("resolution", _FieldMeta(
                type="string", unit="", default="1280x720",
                cache_impact=True, cost_impact=True,
                description="output resolution (WxH)",
            )),
            ("fps", _FieldMeta(
                type="integer", unit="fps", range=(1, 60), default=24,
                cache_impact=False, cost_impact=True,
                description="output frame rate",
            )),
            ("seed", _FieldMeta(
                type="integer", unit="", range=(0, 2**32 - 1), default=0,
                cache_impact=True,
                description="reproducibility seed",
            )),
        ))

    @classmethod
    def upscale(cls) -> "GenerationControlsSchema":
        """后处理放大（plan §4.3 / AUD-23114）：scale_factor / sharpen
        （scale 依赖 upscale 模型 pack → model_pack_required）。"""
        return cls(fields=(
            ("scale_factor", _FieldMeta(
                type="number", unit="x", range=(1.0, 8.0), default=2.0,
                cache_impact=True, cost_impact=True, model_pack_required=True,
                description="upscale multiplier (may require an upscale model pack)",
            )),
            ("sharpen", _FieldMeta(
                type="number", unit="", range=(0.0, 1.0), default=0.0,
                cache_impact=False, cost_impact=False,
                description="post-upscale sharpening amount",
            )),
        ))


# ---------------------------------------------------------------------- #
# 模块级 GENERATION_SPECS + register（必须在 GenerationControlsSchema 定义后）
# ---------------------------------------------------------------------- #


GENERATION_SPECS: Tuple[WorkflowNodeSpec, ...] = _generation_specs()


def register_generation_node_specs(registry: NodeSpecRegistry) -> None:
    """注册 6 个生成节点 spec 到 :class:`NodeSpecRegistry`。"""
    for spec in GENERATION_SPECS:
        registry.register(spec)


__all__ = [
    "GENERATION_SPECS",
    "GenerationControlsSchema",
    "register_generation_node_specs",
]
