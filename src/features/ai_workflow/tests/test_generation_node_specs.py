"""P6.5 测试：生成节点 spec 扩充 + ComfyUI Workflow 节点 spec（plan §4.3 / §1.4.2 C2）。

验证生成相关节点 spec 的注册与红线：

- 文生图 / 图生图 / 局部重绘 / 文生视频 / 图生视频 / 视频后处理（plan §4.3）。
- ``GenerationControls`` schema：每个能力组有 JSON Schema + 单位/范围 + 是否影响 cache +
  是否上传参考素材 + 是否需模型 pack + 是否增加成本（plan §4.3）。
- ComfyUI Workflow 节点（C2 工作流执行级）：外部子图节点，输入绑定受控。
- LoRA / Control / Reference 等"未知 provider 字段"只存于 ``bound_inputs``，不混入 Topos
  核心数据模型（plan §4.3）。

红线（plan §4.3 / §4.4 / §1.4.2 / §0）：

- 生成节点 ``executor_id`` 受控（TTS_PROVIDER / GENERATION / FFMPEG；无任意代码执行）。
- 生成节点不直接写 timeline（``timeline_write_required=False``）。
- 输出 ``media_generation_ref`` / ``asset_ref``（候选进 GenerationBin，不直接进 timeline）。
- ComfyUI Workflow 节点不暴露任意 ``class_type`` 改写；只暴露声明槽位（C2）。
- 不存在"完全访问" / "跳过审批" / "忽略许可证"节点（§4.4）。
- LoRA / Control / Reference 参数只进 ``bound_inputs``（外部子图绑定），不进 Topos 核心
  schema（plan §4.3）。
"""
from __future__ import annotations


from src.features.ai_workflow.adapters.generation_node_specs import (
    GenerationControlsSchema,
    register_generation_node_specs,
)
from src.features.ai_workflow.core.node_specs import (
    NodeCategory,
    NodeExecutorId,
    NodeSpecRegistry,
)


# ---------------------------------------------------------------------- #
# 生成节点 spec 注册
# ---------------------------------------------------------------------- #


class TestGenerationNodeSpecs:
    def test_all_generation_specs_registered(self) -> None:
        reg = NodeSpecRegistry()
        register_generation_node_specs(reg)
        expected = {
            "topos.generation.text_to_image",
            "topos.generation.image_to_image",
            "topos.generation.inpaint",
            "topos.generation.text_to_video",
            "topos.generation.image_to_video",
            "topos.generation.upscale",
        }
        for sid in expected:
            assert reg.get(sid) is not None, f"missing spec: {sid}"

    def test_text_to_image_spec_basics(self) -> None:
        reg = NodeSpecRegistry()
        register_generation_node_specs(reg)
        s = reg.require("topos.generation.text_to_image")
        assert s.category == NodeCategory.GENERATION
        # 输入是 text_document（prompt），输出是 media_generation_ref
        in_types = {p.artifact_type for p in s.input_ports}
        assert "text_document" in in_types
        out_types = {p.artifact_type for p in s.output_ports}
        assert "media_generation_ref" in out_types
        # 红线：不直接写 timeline
        assert s.timeline_write_required is False
        # 红线：executor 受控
        assert s.executor_id == NodeExecutorId.TTS_PROVIDER  # 复用 generation provider
        # 红线：不输出 texture
        for t in out_types:
            assert "texture" not in t.lower()

    def test_image_to_image_takes_reference_image(self) -> None:
        reg = NodeSpecRegistry()
        register_generation_node_specs(reg)
        s = reg.require("topos.generation.image_to_image")
        # 输入应有 asset_ref（源图）+ text_document（prompt）
        in_types = {p.artifact_type for p in s.input_ports}
        assert "asset_ref" in in_types
        assert "text_document" in in_types

    def test_inpaint_takes_mask(self) -> None:
        reg = NodeSpecRegistry()
        register_generation_node_specs(reg)
        s = reg.require("topos.generation.inpaint")
        in_names = {p.name for p in s.input_ports}
        # inpaint 必须有原图 + mask
        assert "source_image" in in_names
        assert "mask" in in_names

    def test_text_to_video_takes_duration(self) -> None:
        reg = NodeSpecRegistry()
        register_generation_node_specs(reg)
        s = reg.require("topos.generation.text_to_video")
        # params_schema 应有 duration_seconds
        assert "duration_seconds" in s.params_schema.get("properties", {})

    def test_image_to_video_takes_reference_image(self) -> None:
        reg = NodeSpecRegistry()
        register_generation_node_specs(reg)
        s = reg.require("topos.generation.image_to_video")
        in_names = {p.name for p in s.input_ports}
        assert "reference_image" in in_names

    def test_upscale_is_post_processing(self) -> None:
        reg = NodeSpecRegistry()
        register_generation_node_specs(reg)
        s = reg.require("topos.generation.upscale")
        in_types = {p.artifact_type for p in s.input_ports}
        assert "asset_ref" in in_types or "media_generation_ref" in in_types
        out_types = {p.artifact_type for p in s.output_ports}
        assert "media_generation_ref" in out_types

    def test_all_generation_specs_have_consent_param(self) -> None:
        # 红线（§0 #1）：生成节点必须暴露 consent 相关参数（云上传 / 成本确认）
        reg = NodeSpecRegistry()
        register_generation_node_specs(reg)
        for sid in (
            "topos.generation.text_to_image", "topos.generation.image_to_image",
            "topos.generation.inpaint", "topos.generation.text_to_video",
            "topos.generation.image_to_video", "topos.generation.upscale",
        ):
            s = reg.require(sid)
            # 网络必需（生成需 provider）+ estimated_cost > 0（成本）
            assert s.network_required is True, f"{sid} should require network"
            assert s.estimated_cost > 0, f"{sid} should have non-zero cost"

    def test_no_full_access_or_skip_approval_node(self) -> None:
        # 红线（§4.4）：不暴露"完全访问"/"跳过审批"/"忽略许可证"
        reg = NodeSpecRegistry()
        register_generation_node_specs(reg)
        for s in reg.all_specs():
            assert "full_access" not in s.spec_id
            assert "skip_approval" not in s.spec_id
            assert "ignore_license" not in s.spec_id
            assert s.executor_id in NodeExecutorId.ALL


# ---------------------------------------------------------------------- #
# GenerationControlsSchema —— plan §4.3
# ---------------------------------------------------------------------- #


class TestGenerationControlsSchema:
    def test_text_to_image_controls(self) -> None:
        ctrl = GenerationControlsSchema.text_to_image()
        schema = ctrl.to_json_schema()
        props = schema["properties"]
        # 文生图必填参数（plan §4.3）
        for k in ("prompt", "negative_prompt", "width", "height", "seed",
                  "batch_size", "steps", "guidance"):
            assert k in props, f"text_to_image missing control: {k}"
        # 每个字段有 unit / range / cache_impact / upload_impact / cost_impact 标注
        for k, v in props.items():
            assert isinstance(v, dict)
            # 至少有 type
            assert "type" in v

    def test_text_to_video_controls(self) -> None:
        ctrl = GenerationControlsSchema.text_to_video()
        props = ctrl.to_json_schema()["properties"]
        # 文生视频：duration / resolution / fps / seed / first_frame
        for k in ("prompt", "duration_seconds", "resolution", "fps", "seed"):
            assert k in props, f"text_to_video missing control: {k}"

    def test_image_to_image_controls(self) -> None:
        ctrl = GenerationControlsSchema.image_to_image()
        props = ctrl.to_json_schema()["properties"]
        # 图生图：denoise_strength / prompt / seed
        assert "denoise_strength" in props

    def test_lora_control_field_goes_to_bound_inputs_only(self) -> None:
        # 红线（plan §4.3）："未知 provider 字段只保存于外部 Comfy 子图的 bound_inputs 中，
        # 不混入 Topos 的核心数据模型"
        ctrl = GenerationControlsSchema.text_to_image()
        schema = ctrl.to_json_schema()
        # LoRA / ControlNet / IP-Adapter 等"未知 provider 字段"**不**应出现在核心 schema
        for k in ("lora_list", "controlnet", "ip_adapter"):
            assert k not in schema["properties"], (
                f"{k} must NOT be in Topos core schema; "
                "it belongs to Comfy workflow bound_inputs only"
            )

    def test_each_field_has_cache_impact_annotation(self) -> None:
        # plan §4.3："是否影响 cache" 必须标注
        ctrl = GenerationControlsSchema.text_to_image()
        meta = ctrl.field_metadata
        for fname, m in meta.items():
            assert "cache_impact" in m, f"{fname} missing cache_impact"
            assert isinstance(m["cache_impact"], bool)

    def test_each_field_has_upload_impact_annotation(self) -> None:
        # plan §4.3："是否会上传参考素材" 必须标注
        ctrl = GenerationControlsSchema.text_to_image()
        meta = ctrl.field_metadata
        for fname, m in meta.items():
            assert "upload_impact" in m, f"{fname} missing upload_impact"
            assert isinstance(m["upload_impact"], bool)

    def test_each_field_has_cost_impact_annotation(self) -> None:
        ctrl = GenerationControlsSchema.text_to_image()
        meta = ctrl.field_metadata
        for fname, m in meta.items():
            assert "cost_impact" in m, f"{fname} missing cost_impact"

    def test_each_field_has_unit_or_range(self) -> None:
        # plan §4.3："单位/范围"
        ctrl = GenerationControlsSchema.text_to_image()
        meta = ctrl.field_metadata
        for fname, m in meta.items():
            # 数字字段必须有 unit 或 range
            assert "unit" in m or "range" in m, (
                f"{fname} missing unit/range annotation"
            )


# ---------------------------------------------------------------------- #
# ComfyUI Workflow 节点 spec（C2 工作流执行级）
# ---------------------------------------------------------------------- #


class TestComfyWorkflowNodeSpec:
    def test_comfy_workflow_spec_registered(self) -> None:
        from src.features.ai_workflow.adapters.comfyui_node_specs import (
            register_comfyui_node_specs,
        )
        reg = NodeSpecRegistry()
        register_comfyui_node_specs(reg)
        s = reg.get("topos.comfy.workflow")
        assert s is not None
        assert s.category == NodeCategory.GENERATION

    def test_comfy_workflow_does_not_expose_arbitrary_class_type(self) -> None:
        # 红线（§9.4 #2）：ComfyUI Workflow 节点不暴露任意 class_type 改写
        from src.features.ai_workflow.adapters.comfyui_node_specs import (
            register_comfyui_node_specs,
        )
        reg = NodeSpecRegistry()
        register_comfyui_node_specs(reg)
        s = reg.require("topos.comfy.workflow")
        # params_schema 不应有 "class_type" / "node_type" 改写参数
        props = s.params_schema.get("properties", {})
        for k in props:
            assert "class_type" not in k.lower()
            assert "node_type" not in k.lower()
        # 只暴露安全绑定槽位（prompt / seed / 参考素材 / 输出节点）
        allowed = {"prompt", "negative_prompt", "seed", "width", "height",
                   "reference_assets", "output_node_id", "workflow_snapshot_id"}
        for k in props:
            # 字段名应在白名单或 bound_inputs 命名空间
            assert k in allowed or k.startswith("bound_"), (
                f"comfy workflow exposes non-allowed param: {k}"
            )

    def test_comfy_workflow_inputs_use_topos_artifacts(self) -> None:
        # 输入是 text_document / asset_ref / selection_set（Topos artifact），不传 JSON
        from src.features.ai_workflow.adapters.comfyui_node_specs import (
            register_comfyui_node_specs,
        )
        reg = NodeSpecRegistry()
        register_comfyui_node_specs(reg)
        s = reg.require("topos.comfy.workflow")
        in_types = {p.artifact_type for p in s.input_ports}
        # 必须有 prompt 输入（text_document）
        assert "text_document" in in_types
        # 红线：不传 raw JSON / texture
        for t in in_types:
            assert "json" not in t.lower()
            assert "texture" not in t.lower()

    def test_comfy_workflow_output_is_generation_ref(self) -> None:
        # 输出 media_generation_ref（候选进 GenerationBin，不直接进 timeline）
        from src.features.ai_workflow.adapters.comfyui_node_specs import (
            register_comfyui_node_specs,
        )
        reg = NodeSpecRegistry()
        register_comfyui_node_specs(reg)
        s = reg.require("topos.comfy.workflow")
        out_types = {p.artifact_type for p in s.output_ports}
        assert "media_generation_ref" in out_types
        # 红线：不直接 timeline_write
        assert s.timeline_write_required is False
