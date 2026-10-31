"""P7.3 测试：ComfyUI 兼容闭环验收（plan §9.4 / §1.4.2 C0–C5 / §10.3.7-9）。

> 验收：使用官方稳定 Comfy workflow fixture，可在本地 mock server 完成
> ``import → bind prompt/reference → submit → progress → output import``；
> 模型/节点缺失、网络中断、取消、输出下载失败均不会污染媒体池和 timeline。

本文件是 ComfyUI 兼容层（P6 + P7）的**端到端闭环验收**：

- 导入 → 绑定 → preflight → submit → fetch → 导出 → 转换（完整链路）
- 导入的 workflow 导出后结构等价（round-trip）
- 识别为 Topos 高层节点的 workflow 可同时走 ComfyUI 路径 + Topos 原生路径（双轨）
- C5 不承诺边界：未知 workflow 全链路保留为 opaque Comfy 子图

红线（plan §9.4 / §1.4.2 / §0）：

- 全链路不绕开 preflight / consent / hash 校验 / GenerationBin。
- 导出与转换都不执行 custom node 代码（只读）。
- 取消 / 失败 / hash 不一致 → 候选不入 bin，timeline 不变（与 P6.6 一致）。
"""
from __future__ import annotations

import json


from src.features.ai_workflow.adapters.comfyui_converter import (
    ComfyToToposConverter,
    ConversionVerdict,
)
from src.features.ai_workflow.adapters.comfyui_executor import (
    COMFYUI_LOCAL_PROVIDER_ID,
    ComfyGenerationRequest,
    ComfyWorkflowExecutor,
    FakeComfyServer,
)
from src.features.ai_workflow.adapters.comfyui_export import (
    ComfyPlaceholderKind,
    ComfyWorkflowExporter,
    NativeNodePlaceholder,
)
from src.features.ai_workflow.adapters.comfyui_importer import (
    ComfyImportReport,
    ComfyPreflightChecker,
    ComfyServerCapabilities,
    ComfyWorkflowImporter,
)
from src.features.ai_workflow.adapters.comfyui_models import (
    ComfyConnectionKind,
    ComfyConnectionProfile,
    ComfyInputBinding,
    ComfyInputSlot,
    ComfyOutputArtifact,
)
from src.features.ai_workflow.adapters.generation_node_specs import (
    register_generation_node_specs,
)
from src.features.ai_workflow.core.node_specs import NodeSpecRegistry
from src.features.edit.ai_editing.generation_models import (
    GenerationConsent,
    GenerationModality,
    GenerationRequest,
)


# ---------------------------------------------------------------------- #
# Fixtures
# ---------------------------------------------------------------------- #


def _full_consent() -> GenerationConsent:
    return GenerationConsent(
        confirmed=True, upload_acknowledged=True,
        cost_acknowledged=True, copyright_acknowledged=True,
        confirmed_at="2026-08-03T10:00:00Z",
    )


def _local_profile() -> ComfyConnectionProfile:
    return ComfyConnectionProfile(
        profile_id="local-default", kind=ComfyConnectionKind.LOCAL,
        base_url="http://127.0.0.1:8188",
    )


def _capabilities_all_present(snap) -> ComfyServerCapabilities:
    return ComfyServerCapabilities(
        profile_id="local-default",
        available_class_types=frozenset({n.class_type for n in snap.nodes}),
        available_models=frozenset({m.model_name for m in snap.required_models}),
        custom_node_repos=frozenset(),
    )


def _sdxl_t2i_workflow() -> dict:
    return {
        "3": {"class_type": "KSampler", "inputs": {
            "seed": 12345, "cfg": 7.0, "steps": 30,
            "model": ["4", 0], "positive": ["6", 0],
            "negative": ["7", 0], "latent_image": ["5", 0],
        }},
        "4": {"class_type": "CheckpointLoaderSimple", "inputs": {
            "ckpt_name": "sd_xl_base_1.0.safetensors",
        }},
        "6": {"class_type": "CLIPTextEncode", "inputs": {"text": "a cat", "clip": ["4", 1]}},
        "7": {"class_type": "CLIPTextEncode", "inputs": {"text": "", "clip": ["4", 1]}},
        "5": {"class_type": "EmptyLatentImage", "inputs": {"width": 1024, "height": 1024, "batch_size": 1}},
        "8": {"class_type": "VAEDecode", "inputs": {"samples": ["3", 0], "vae": ["4", 2]}},
        "9": {"class_type": "SaveImage", "inputs": {"images": ["8", 0]}},
    }


# ---------------------------------------------------------------------- #
# §9.4 完整链路验收：import → bind → preflight → submit → fetch
# ---------------------------------------------------------------------- #


class TestFullComfyPipeline:
    """plan §9.4 验收：import → bind prompt/reference → submit → progress → output import。"""

    def test_full_pipeline_produces_generation_bin_candidate(self) -> None:
        # 1. import
        imp = ComfyWorkflowImporter()
        snap = imp.import_from_dict("wf-t2i", _sdxl_t2i_workflow(), display_name="SDXL T2I")
        # 2. bind prompt + seed
        prompt_slot = ComfyInputSlot(
            slot_id="prompt", node_id="6", input_name="text",
            artifact_type="text_document",
        )
        seed_slot = ComfyInputSlot(
            slot_id="seed", node_id="3", input_name="seed",
            artifact_type="integer",
        )
        bindings = (
            ComfyInputBinding(slot=prompt_slot, source_artifact_id="text/doc-001"),
            ComfyInputBinding(slot=seed_slot, resolved_value="99999"),
        )
        # 3. preflight
        caps = _capabilities_all_present(snap)
        report = ComfyImportReport(
            snapshot=snap,
            required_bindings=bindings,
        )
        preflight = ComfyPreflightChecker().check(report, capabilities=caps)
        assert preflight.allowed
        # 4. submit
        server = FakeComfyServer(profile_id="local-default", available_models=frozenset({
            "sd_xl_base_1.0.safetensors",
        }))
        ex = ComfyWorkflowExecutor(server=server, server_capabilities=caps)
        req = ComfyGenerationRequest(
            request_id="req-001", snapshot=snap, profile=_local_profile(),
            consent=_full_consent(), modality=GenerationModality.IMAGE,
            prompt="a cute cat", bindings=bindings,
        )
        ref = ex.submit_job(req, import_report=report)
        # 5. progress + fetch
        server.advance_to_running(ref.external_job_id)
        server.complete_with_outputs(
            ref.external_job_id,
            outputs=(ComfyOutputArtifact(
                node_id="9", output_index=0, artifact_kind="image",
                filename="topos_00001_.png",
                content_hash="sha256:" + "a" * 64, bytes_size=4096,
            ),),
        )
        ref2 = ex.poll_progress(ref)
        result = ex.fetch_outputs(req, ref2)
        # 候选进 GenerationBin（候选，不直接进 timeline）
        assert result.succeeded
        assert len(result.candidates) == 1
        cand = result.candidates[0]
        assert cand.provenance.provider_id == COMFYUI_LOCAL_PROVIDER_ID
        # prompt hash 进 provenance（不存原文）
        assert cand.provenance.input_prompt_hash
        assert cand.provenance.input_prompt_hash != "a cute cat"


# ---------------------------------------------------------------------- #
# §1.4.2 闭环：import → export round-trip
# ---------------------------------------------------------------------- #


class TestImportExportRoundTrip:
    """plan §1.4.2 C2：导入/保存 ComfyUI API-format JSON；导出回 API-format 保证等价。"""

    def test_roundtrip_preserves_node_structure(self) -> None:
        imp = ComfyWorkflowImporter()
        snap = imp.import_from_dict("wf", _sdxl_t2i_workflow())
        exporter = ComfyWorkflowExporter()
        result = exporter.export_snapshot(snap)
        payload = json.loads(result.api_format_json)
        # 节点 class_type 完整保留
        for nid, orig in _sdxl_t2i_workflow().items():
            assert payload[nid]["class_type"] == orig["class_type"]
        # 等价性保证（纯 Comfy 子图）
        assert result.equivalence_guaranteed

    def test_roundtrip_with_bindings_applies_to_export(self) -> None:
        imp = ComfyWorkflowImporter()
        snap = imp.import_from_dict("wf", _sdxl_t2i_workflow())
        seed_slot = ComfyInputSlot(
            slot_id="seed", node_id="3", input_name="seed",
            artifact_type="integer",
        )
        binding = ComfyInputBinding(slot=seed_slot, resolved_value="424242")
        exporter = ComfyWorkflowExporter()
        result = exporter.export_snapshot(snap, bindings=(binding,))
        payload = json.loads(result.api_format_json)
        assert payload["3"]["inputs"]["seed"] == 424242

    def test_export_scrubs_credentials_even_if_present_in_payload(self) -> None:
        # 红线（§0 第四条）：即使 raw_payload 含疑似 credential，导出也清除
        raw = _sdxl_t2i_workflow()
        raw["3"]["inputs"]["api_key"] = "sk-secret-12345"  # 注入疑似 key
        imp = ComfyWorkflowImporter()
        snap = imp.import_from_dict("wf", raw)
        exporter = ComfyWorkflowExporter()
        result = exporter.export_snapshot(snap)
        low = result.api_format_json.lower()
        assert "sk-secret-12345" not in low
        assert "api_key" not in low


# ---------------------------------------------------------------------- #
# §1.4.2 C4 双轨：Comfy 子图可同时走 ComfyUI 执行 + Topos 原生节点
# ---------------------------------------------------------------------- #


class TestDualTrackComfyAndToposNative:
    """plan §1.4.2 C4：识别为 Topos 高层节点的 workflow 可同时走两条路径。"""

    def test_sdxl_t2i_recognized_as_topos_text_to_image(self) -> None:
        imp = ComfyWorkflowImporter()
        snap = imp.import_from_dict("wf", _sdxl_t2i_workflow())
        reg = NodeSpecRegistry()
        register_generation_node_specs(reg)
        converter = ComfyToToposConverter(registry=reg)
        outcome = converter.convert(snap)
        assert outcome.verdict == ConversionVerdict.CONVERTED
        assert outcome.findings[0].target_spec_id == "topos.generation.text_to_image"
        # 转换出的参数从 Comfy 字面量提取
        assert outcome.findings[0].params["seed"] == 12345

    def test_same_workflow_can_also_be_executed_via_comfy_path(self) -> None:
        # 同一个 SDXL workflow：既可识别为 Topos 节点（C4 转换），
        # 也可作为 Comfy 子图直接执行（C2 路径）
        imp = ComfyWorkflowImporter()
        snap = imp.import_from_dict("wf", _sdxl_t2i_workflow())
        caps = _capabilities_all_present(snap)
        server = FakeComfyServer(profile_id="local-default", available_models=frozenset({
            "sd_xl_base_1.0.safetensors",
        }))
        server.auto_complete_with_image = True
        ex = ComfyWorkflowExecutor(server=server, server_capabilities=caps)
        ex.bind_workflow(snap, profile=_local_profile())
        gen_req = GenerationRequest(
            request_id="gen-dual",
            modality=GenerationModality.IMAGE,
            prompt="cat",
            consent=_full_consent(),
        )
        result = ex.generate(gen_req)
        # Comfy 路径也成功
        assert result.succeeded


# ---------------------------------------------------------------------- #
# §1.4.2 C5 边界：未知 workflow 全链路保留为 opaque
# ---------------------------------------------------------------------- #


class TestOpaqueWorkflowFullChain:
    """plan §1.4.2 C5：未知 workflow 不强行转换；全链路保留为 Comfy 子图。"""

    def test_unknown_workflow_not_converted_but_still_executable_via_comfy(self) -> None:
        # 未知 workflow：converter 给 OPAQUE，但 ComfyUI 路径仍可执行（如果
        # server 支持）。AUD-23023 修复后 §13.7 红线生效：未 pin 的 custom
        # node **默认拒绝**——运行 opaque custom-node workflow 需显式 opt-out
        # （require_pinned_custom_nodes=False，即用户明确接受风险）。
        raw = {
            "1": {"class_type": "SomeCustomMagicNode", "inputs": {"x": 1}},
            "2": {"class_type": "SaveImage", "inputs": {"images": ["1", 0]}},
        }
        imp = ComfyWorkflowImporter()
        snap = imp.import_from_dict("wf-unknown", raw)
        reg = NodeSpecRegistry()
        register_generation_node_specs(reg)
        converter = ComfyToToposConverter(registry=reg)
        outcome = converter.convert(snap)
        # C5：不强行转换
        assert outcome.verdict == ConversionVerdict.OPAQUE
        # 但 ComfyUI 路径仍可执行（server 提供 SomeCustomMagicNode 时）
        caps = ComfyServerCapabilities(
            profile_id="local-default",
            available_class_types=frozenset({"SomeCustomMagicNode", "SaveImage"}),
            available_models=frozenset(),
            custom_node_repos=frozenset(),
        )
        server = FakeComfyServer(profile_id="local-default", available_models=frozenset())
        server.auto_complete_with_image = True
        # 默认策略下该 workflow 被 UNPINNED_CUSTOM_NODE 拒绝（红线）……
        ex_default = ComfyWorkflowExecutor(server=server, server_capabilities=caps)
        ex_default.bind_workflow(snap, profile=_local_profile())
        default_result = ex_default.generate(GenerationRequest(
            request_id="gen-opaque-default",
            modality=GenerationModality.IMAGE,
            prompt="x",
            consent=_full_consent(),
        ))
        assert not default_result.succeeded
        assert "unpinned_custom_node" in default_result.error_message
        # ……显式 opt-out（用户接受未 pin 风险）后仍走 Comfy 子图执行
        ex = ComfyWorkflowExecutor(
            server=server, server_capabilities=caps,
            require_pinned_custom_nodes=False,
        )
        ex.bind_workflow(snap, profile=_local_profile())
        gen_req = GenerationRequest(
            request_id="gen-opaque",
            modality=GenerationModality.IMAGE,
            prompt="x",
            consent=_full_consent(),
        )
        result = ex.generate(gen_req)
        # 仍走 Comfy 子图执行
        assert result.succeeded


# ---------------------------------------------------------------------- #
# §1.4.2 C0–C5 分层边界（与 P6.6 互补，这里聚焦导出+转换层）
# ---------------------------------------------------------------------- #


class TestLayeredCompatibilityExportConvert:
    """plan §1.4.2：C2 导出 + C4 转换 + C5 不承诺。"""

    def test_c2_export_returns_api_format_for_comfy_subgraph(self) -> None:
        imp = ComfyWorkflowImporter()
        snap = imp.import_from_dict("wf", _sdxl_t2i_workflow())
        exporter = ComfyWorkflowExporter()
        result = exporter.export_snapshot(snap)
        # 是合法 JSON
        payload = json.loads(result.api_format_json)
        assert isinstance(payload, dict)
        # 含 _topos_meta
        assert "_topos_meta" in payload

    def test_c4_converter_only_matches_stable_patterns(self) -> None:
        # 改动 golden pattern 后不再识别
        import copy
        reg = NodeSpecRegistry()
        register_generation_node_specs(reg)
        converter = ComfyToToposConverter(registry=reg)
        imp = ComfyWorkflowImporter()
        # 取 SDXL golden，改 KSampler → UnknownSampler
        raw = copy.deepcopy(_sdxl_t2i_workflow())
        raw["3"]["class_type"] = "UnknownSampler"
        snap = imp.import_from_dict("wf", raw)
        outcome = converter.convert(snap)
        assert outcome.verdict == ConversionVerdict.OPAQUE

    def test_c5_unknown_custom_node_exported_as_opaque_no_native_translation(self) -> None:
        # 红线（C5）：未知节点导出/转换都不伪装成 Topos 节点
        imp = ComfyWorkflowImporter()
        raw = _sdxl_t2i_workflow()
        raw["999"] = {"class_type": "RandomThirdPartyNode", "inputs": {"x": 1}}
        snap = imp.import_from_dict("wf", raw)
        reg = NodeSpecRegistry()
        register_generation_node_specs(reg)
        converter = ComfyToToposConverter(registry=reg)
        outcome = converter.convert(snap)
        # AUD-23670：显式锁定 verdict + findings 非空——旧版 for 循环在
        # findings 为空时整体空转通过，红线零检测力。当前实现：未知节点不在
        # SDXL required 集，模式仍匹配 → CONVERTED；若未来收紧为 OPAQUE，
        # 分支断言同样可证伪（两个分支都不允许静默通过）。
        if outcome.verdict == ConversionVerdict.CONVERTED:
            assert len(outcome.findings) >= 1, (
                "CONVERTED 却无 findings——红线断言空转"
            )
            for f in outcome.findings:
                # target 只能是受控的高层节点集合（未知节点绝不被"翻译"）
                assert f.target_spec_id in (
                    "topos.generation.text_to_image", "topos.generation.upscale",
                )
        else:
            # 收紧为保守策略：含未知节点 → OPAQUE 且不产任何 Topos 节点
            assert outcome.verdict == ConversionVerdict.OPAQUE
            assert len(outcome.findings) == 0

    def test_export_with_native_placeholders_does_not_fake_comfy_nodes(self) -> None:
        # 红线（§9.4 #8）：原生节点占位 class_type 以 _Topos 开头，不伪装成 Comfy 节点
        imp = ComfyWorkflowImporter()
        snap = imp.import_from_dict("wf", _sdxl_t2i_workflow())
        exporter = ComfyWorkflowExporter()
        placeholders = (
            NativeNodePlaceholder(
                topos_node_id="n1", topos_spec_id="topos.user.approval",
                kind=ComfyPlaceholderKind.APPROVAL, note="x",
            ),
            NativeNodePlaceholder(
                topos_node_id="n2", topos_spec_id="topos.timeline.apply_approved_plan",
                kind=ComfyPlaceholderKind.TIMELINE, note="y",
            ),
        )
        result = exporter.export_with_native_nodes(snap, placeholders)
        payload = json.loads(result.api_format_json)
        # 占位节点 class_type 以 _Topos 开头（Comfy 不执行）
        # 排除 _topos_meta（元数据，不是占位节点）
        comfy_keys = [k for k in payload
                      if k.startswith("_topos_") and k != "_topos_meta"]
        assert len(comfy_keys) == 2
        for k in comfy_keys:
            assert payload[k]["class_type"].startswith("_ToposNativePlaceholder")
        # 不保证等价
        assert not result.equivalence_guaranteed
