"""P6.3 测试：ComfyUI Workflow executor（plan §14.1 step 2/4）。

> 用本地 mock HTTP/WebSocket server 固化官方 API-format workflow fixture；测试 submit、
> progress、cancel、history/output 映射，不依赖真实 GPU。
> 实现 ``ComfyUI Workflow`` executor 的 ``GenerationProvider`` adapter，结果必须进
> ``GenerationBin``，不得直接导入 timeline。

验证 :class:`ComfyWorkflowExecutor`（实现 :class:`GenerationProvider` 协议）的契约：

- ``submit_job`` → :class:`ComfyJobRef`（外部 job ID；不直接执行）。
- ``poll_progress`` → 进度回调 → 更新 :class:`ComfyJobRef` 状态。
- ``fetch_outputs`` → 下载输出 → :class:`GenerationResult`（候选进 GenerationBin）。
- ``cancel_job`` → 取消传播到 server。
- 用 :class:`FakeComfyServer`（mock HTTP/WS）固化 fixture；不依赖真实 GPU/网络。

红线（plan §14.1 step 2/4 / §9.4 / §0）：

- 不联网执行未确认 workflow（``submit_job`` 前必须 preflight 通过）。
- 生成结果进 GenerationBin（候选，不直接进 timeline）。
- consent gate：未确认不提交（``ComfyGenerationRequest.consent`` 必须 complete）。
- 取消传播：``cancel_job`` → server 取消 + :class:`ComfyJobRef` 状态 → CANCELLED。
- 输出 hash 校验：下载结果 content_hash 必须与 server 报告一致；不一致 → 拒绝导入。
- provider_id 受控（``comfyui-local`` / ``comfyui-cloud``）。
- ``GenerationProvenance`` 必填 + 完整（model_id / workflow_hash / provider_id）。
"""
from __future__ import annotations

from typing import Any

import pytest

from src.features.ai_workflow.adapters.comfyui_executor import (
    COMFYUI_CLOUD_PROVIDER_ID,
    COMFYUI_LOCAL_PROVIDER_ID,
    ComfyExecutorError,
    ComfyExecutorErrorCode,
    ComfyGenerationRequest,
    ComfyWorkflowExecutor,
    FakeComfyServer,
)
from src.features.ai_workflow.adapters.comfyui_importer import (
    ComfyImportReport,
    ComfyServerCapabilities,
    ComfyWorkflowImporter,
)
from src.features.ai_workflow.adapters.comfyui_models import (
    ComfyConnectionKind,
    ComfyConnectionProfile,
    ComfyInputBinding,
    ComfyInputSlot,
    ComfyJobRef,
    ComfyJobState,
    ComfyOutputArtifact,
    ComfyWorkflowSnapshot,
)
from src.features.edit.ai_editing.generation_models import (
    GeneratedAsset,
    GenerationBudget,
    GenerationConsent,
    GenerationModality,
    GenerationRequest,
    GenerationResult,
)


# ---------------------------------------------------------------------- #
# Fixtures
# ---------------------------------------------------------------------- #


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


def _local_profile() -> ComfyConnectionProfile:
    return ComfyConnectionProfile(
        profile_id="local-default",
        kind=ComfyConnectionKind.LOCAL,
        base_url="http://127.0.0.1:8188",
    )


def _full_consent() -> GenerationConsent:
    return GenerationConsent(
        confirmed=True,
        upload_acknowledged=True,
        cost_acknowledged=True,
        copyright_acknowledged=True,
        confirmed_at="2026-08-02T10:00:00Z",
    )


def _imported_t2i_snapshot() -> ComfyWorkflowSnapshot:
    return ComfyWorkflowImporter().import_from_dict(
        "wf-t2i", _sdxl_t2i_workflow(), display_name="SDXL T2I",
    )


def _capabilities_all_present(snap: ComfyWorkflowSnapshot) -> ComfyServerCapabilities:
    return ComfyServerCapabilities(
        profile_id="local-default",
        available_class_types=frozenset({n.class_type for n in snap.nodes}),
        available_models=frozenset({m.model_name for m in snap.required_models}),
        custom_node_repos=frozenset(),
    )


def _track_submitted_job_ids(server: FakeComfyServer) -> list[str]:
    """经公共协议捕获 server 上被提交的 job id（AUD-23521）。

    包装实例的 ``submit`` 记录返回的 ``external_job_id``——测试随后用
    ``server.get_job(id)``（公共面）断言状态，**不穿透** ``_jobs`` 私有结构
    （与本文件 ``test_fetch_outputs_uses_public_job_outputs_protocol`` 声明的
    协议约束一致；``next(iter(server._jobs.values()))`` 既脆弱又自相矛盾）。
    """
    submitted: list[str] = []
    original_submit = server.submit

    def _capturing_submit(**kwargs: Any) -> ComfyJobRef:
        ref = original_submit(**kwargs)
        submitted.append(ref.external_job_id)
        return ref

    server.submit = _capturing_submit  # type: ignore[method-assign]
    return submitted


# ---------------------------------------------------------------------- #
# ComfyGenerationRequest —— executor 入参
# ---------------------------------------------------------------------- #


class TestComfyGenerationRequest:
    def test_construction_with_snapshot_and_consent(self) -> None:
        snap = _imported_t2i_snapshot()
        req = ComfyGenerationRequest(
            request_id="req-001",
            snapshot=snap,
            profile=_local_profile(),
            consent=_full_consent(),
            modality=GenerationModality.IMAGE,
            prompt="a cute cat",
        )
        assert req.request_id == "req-001"
        assert req.workflow_snapshot_hash == snap.content_hash
        assert req.consent.confirmed is True

    def test_consent_must_be_complete(self) -> None:
        snap = _imported_t2i_snapshot()
        # 不完整 consent（未确认）
        bad_consent = GenerationConsent(confirmed=False)
        with pytest.raises(ValueError, match="consent"):
            ComfyGenerationRequest(
                request_id="req-001", snapshot=snap,
                profile=_local_profile(), consent=bad_consent,
                modality=GenerationModality.IMAGE, prompt="x",
            )

    def test_bindings_applied_to_payload(self) -> None:
        snap = _imported_t2i_snapshot()
        slot = ComfyInputSlot(
            slot_id="seed", node_id="3", input_name="seed",
            artifact_type="integer",
        )
        binding = ComfyInputBinding(slot=slot, resolved_value="99999")
        req = ComfyGenerationRequest(
            request_id="req-001", snapshot=snap,
            profile=_local_profile(), consent=_full_consent(),
            modality=GenerationModality.IMAGE, prompt="x",
            bindings=(binding,),
        )
        payload = req.submitted_payload()
        # seed 应被改写
        assert payload["3"]["inputs"]["seed"] == 99999

    def test_submitted_payload_includes_original_class_types(self) -> None:
        # 红线（§9.4 #4）：提交完整 API-format snapshot；不删节点
        snap = _imported_t2i_snapshot()
        req = ComfyGenerationRequest(
            request_id="r", snapshot=snap, profile=_local_profile(),
            consent=_full_consent(), modality=GenerationModality.IMAGE, prompt="x",
        )
        payload = req.submitted_payload()
        # 7 个节点都在
        assert len(payload) == 7
        assert payload["9"]["class_type"] == "SaveImage"


# ---------------------------------------------------------------------- #
# FakeComfyServer —— mock HTTP/WS（§14.1 step 2）
# ---------------------------------------------------------------------- #


class TestFakeComfyServer:
    def test_server_starts_empty(self) -> None:
        server = FakeComfyServer(profile_id="local-default")
        assert server.profile_id == "local-default"
        assert server.job_count == 0

    def test_submit_returns_job_ref(self) -> None:
        server = FakeComfyServer(profile_id="local-default", available_models=frozenset({
            "sd_xl_base_1.0.safetensors",
        }))
        snap = _imported_t2i_snapshot()
        payload = dict(snap.raw_payload)
        ref = server.submit(
            payload=payload,
            workflow_snapshot_hash=snap.content_hash,
        )
        assert isinstance(ref, ComfyJobRef)
        assert ref.profile_id == "local-default"
        assert ref.state == ComfyJobState.QUEUED
        assert ref.workflow_snapshot_hash == snap.content_hash
        assert server.job_count == 1

    def test_server_progress_drives_running_then_success(self) -> None:
        server = FakeComfyServer(profile_id="local-default", available_models=frozenset({
            "sd_xl_base_1.0.safetensors",
        }))
        snap = _imported_t2i_snapshot()
        ref = server.submit(payload=dict(snap.raw_payload), workflow_snapshot_hash=snap.content_hash)
        # 推进到 RUNNING
        ref2 = server.advance_to_running(ref.external_job_id)
        assert ref2.state == ComfyJobState.RUNNING
        # 推进到 SUCCESS 并产出 outputs
        ref3, outputs = server.complete_with_outputs(
            ref.external_job_id,
            outputs=(
                ComfyOutputArtifact(
                    node_id="9", output_index=0, artifact_kind="image",
                    filename="topos_00001_.png",
                    content_hash="sha256:abcd" + "0" * 60,
                    bytes_size=1024,
                ),
            ),
        )
        assert ref3.state == ComfyJobState.SUCCESS
        assert len(outputs) == 1
        assert outputs[0].filename == "topos_00001_.png"

    def test_server_cancel_marks_cancelled(self) -> None:
        server = FakeComfyServer(profile_id="local", available_models=frozenset({
            "sd_xl_base_1.0.safetensors",
        }))
        snap = _imported_t2i_snapshot()
        ref = server.submit(payload=dict(snap.raw_payload), workflow_snapshot_hash=snap.content_hash)
        cancelled = server.cancel(ref.external_job_id)
        assert cancelled.state == ComfyJobState.CANCELLED
        assert server.get_job(ref.external_job_id).state == ComfyJobState.CANCELLED

    def test_server_tamper_marks_success_but_reports_mismatched_actual_hashes(self) -> None:
        """红线（§9.4 #5）：server 侧篡改 → SUCCESS 但实际 hash 与报告不一致
        （由 executor 在 fetch_outputs 时校验拒绝）。

        AUD-23520：旧版测试名宣称 ``returns_no_outputs`` 但 ``outputs`` 从未
        被断言，tamper 行为零覆盖——若 ``tamper_hash`` 回归（不再篡改实际
        hash），:ref:`test_hash_mismatch_blocks_import` 会误报到错误层面而本
        测试仍绿色。现补齐：报告 hash 与实际 hash 表必须可观测地不一致。
        """
        server = FakeComfyServer(profile_id="local", available_models=frozenset({
            "sd_xl_base_1.0.safetensors",
        }))
        snap = _imported_t2i_snapshot()
        ref = server.submit(payload=dict(snap.raw_payload), workflow_snapshot_hash=snap.content_hash)
        server.advance_to_running(ref.external_job_id)
        # server 内部搞坏 hash → complete 时报告不一致
        reported = ComfyOutputArtifact(
            node_id="9", output_index=0, artifact_kind="image",
            filename="bad.png", content_hash="sha256:deadbeef",
            bytes_size=10,
        )
        ref3, outputs = server.complete_with_outputs(
            ref.external_job_id,
            outputs=(reported,),
            tamper_hash=True,
        )
        # server 侧标记 SUCCESS（篡改对 server 不可见，由 executor 校验）
        assert ref3.state == ComfyJobState.SUCCESS
        # 报告的 outputs 原样返回
        assert len(outputs) == 1
        assert outputs[0].filename == "bad.png"
        assert outputs[0].content_hash == "sha256:deadbeef"
        # 红线：tamper 模式下实际 hash 表与报告 hash 可观测地不一致
        _, actual_hashes, _ = server.job_outputs(ref.external_job_id)
        assert actual_hashes == {"bad.png": "sha256:tampered-content"}
        assert actual_hashes["bad.png"] != outputs[0].content_hash


# ---------------------------------------------------------------------- #
# ComfyWorkflowExecutor —— GenerationProvider adapter
# ---------------------------------------------------------------------- #


class TestComfyWorkflowExecutor:
    def test_provider_id_for_local_profile(self) -> None:
        server = FakeComfyServer(profile_id="local-default")
        ex = ComfyWorkflowExecutor(
            server=server,
            server_capabilities=ComfyServerCapabilities(
                profile_id="local-default",
                available_class_types=frozenset(),
                available_models=frozenset(),
            ),
        )
        p = _local_profile()
        assert ex.provider_id_for(p) == COMFYUI_LOCAL_PROVIDER_ID

    def test_provider_id_for_cloud_profile(self) -> None:
        server = FakeComfyServer(profile_id="comfy-cloud")
        ex = ComfyWorkflowExecutor(
            server=server,
            server_capabilities=ComfyServerCapabilities(
                profile_id="comfy-cloud",
                available_class_types=frozenset(),
                available_models=frozenset(),
            ),
        )
        cloud = ComfyConnectionProfile(
            profile_id="comfy-cloud", kind=ComfyConnectionKind.CLOUD,
            base_url="https://api.comfy.cloud", credential_ref="cred/cloud/0",
        )
        assert ex.provider_id_for(cloud) == COMFYUI_CLOUD_PROVIDER_ID

    def test_submit_requires_preflight_pass(self) -> None:
        # 红线：submit 前必须 preflight 通过
        server = FakeComfyServer(profile_id="local-default", available_models=frozenset({
            "sd_xl_base_1.0.safetensors",
        }))
        server_caps = ComfyServerCapabilities(
            profile_id="local-default",
            available_class_types=frozenset(),  # 空 → preflight 失败
            available_models=frozenset({"sd_xl_base_1.0.safetensors"}),
            custom_node_repos=frozenset(),
        )
        ex = ComfyWorkflowExecutor(server=server, server_capabilities=server_caps)
        snap = _imported_t2i_snapshot()
        report = ComfyImportReport(snapshot=snap)
        req = ComfyGenerationRequest(
            request_id="r", snapshot=snap, profile=_local_profile(),
            consent=_full_consent(), modality=GenerationModality.IMAGE, prompt="cat",
        )
        with pytest.raises(ComfyExecutorError) as ei:
            ex.submit_job(req, import_report=report)
        assert ei.value.code == ComfyExecutorErrorCode.PREFLIGHT_FAILED

    def test_submit_job_returns_jobref(self) -> None:
        snap = _imported_t2i_snapshot()
        caps = _capabilities_all_present(snap)
        server = FakeComfyServer(profile_id="local-default", available_models=frozenset({
            "sd_xl_base_1.0.safetensors",
        }))
        ex = ComfyWorkflowExecutor(server=server, server_capabilities=caps)
        req = ComfyGenerationRequest(
            request_id="r", snapshot=snap, profile=_local_profile(),
            consent=_full_consent(), modality=GenerationModality.IMAGE, prompt="cat",
        )
        report = ComfyImportReport(snapshot=snap)
        ref = ex.submit_job(req, import_report=report)
        assert ref.state in (ComfyJobState.QUEUED, ComfyJobState.RUNNING)

    def test_submit_is_idempotent_and_conflict_is_rejected(self) -> None:
        snap = _imported_t2i_snapshot()
        caps = _capabilities_all_present(snap)
        server = FakeComfyServer(profile_id="local-default")
        ex = ComfyWorkflowExecutor(server=server, server_capabilities=caps)
        report = ComfyImportReport(snapshot=snap)
        req = ComfyGenerationRequest(
            request_id="same-request", snapshot=snap, profile=_local_profile(),
            consent=_full_consent(), modality=GenerationModality.IMAGE, prompt="cat",
        )
        first = ex.submit_job(req, import_report=report)
        second = ex.submit_job(req, import_report=report)
        assert second.external_job_id == first.external_job_id
        assert server.job_count == 1
        conflict = ComfyGenerationRequest(
            request_id="same-request", snapshot=snap, profile=_local_profile(),
            consent=_full_consent(), modality=GenerationModality.IMAGE, prompt="dog",
        )
        with pytest.raises(ComfyExecutorError) as ei:
            ex.submit_job(conflict, import_report=report)
        assert ei.value.code == ComfyExecutorErrorCode.IDEMPOTENCY_CONFLICT

    def test_poll_progress_updates_state(self) -> None:
        snap = _imported_t2i_snapshot()
        caps = _capabilities_all_present(snap)
        server = FakeComfyServer(profile_id="local-default", available_models=frozenset({
            "sd_xl_base_1.0.safetensors",
        }))
        ex = ComfyWorkflowExecutor(server=server, server_capabilities=caps)
        req = ComfyGenerationRequest(
            request_id="r", snapshot=snap, profile=_local_profile(),
            consent=_full_consent(), modality=GenerationModality.IMAGE, prompt="cat",
        )
        report = ComfyImportReport(snapshot=snap)
        ref = ex.submit_job(req, import_report=report)
        # server 推进
        server.advance_to_running(ref.external_job_id)
        ref2 = ex.poll_progress(ref)
        assert ref2.state == ComfyJobState.RUNNING

    def test_fetch_outputs_returns_generation_result(self) -> None:
        snap = _imported_t2i_snapshot()
        caps = _capabilities_all_present(snap)
        server = FakeComfyServer(profile_id="local-default", available_models=frozenset({
            "sd_xl_base_1.0.safetensors",
        }))
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
                filename="topos_00001_.png",
                content_hash="sha256:" + "a" * 64,
                bytes_size=2048,
            ),),
        )
        ref2 = ex.poll_progress(ref)
        result = ex.fetch_outputs(req, ref2)
        assert isinstance(result, GenerationResult)
        assert result.succeeded
        assert len(result.candidates) == 1
        cand = result.candidates[0]
        assert isinstance(cand, GeneratedAsset)
        assert cand.modality == GenerationModality.IMAGE

    def test_outputs_have_full_provenance(self) -> None:
        snap = _imported_t2i_snapshot()
        caps = _capabilities_all_present(snap)
        server = FakeComfyServer(profile_id="local-default", available_models=frozenset({
            "sd_xl_base_1.0.safetensors",
        }))
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
                filename="out.png", content_hash="sha256:" + "b" * 64, bytes_size=10,
            ),),
        )
        ref2 = ex.poll_progress(ref)
        result = ex.fetch_outputs(req, ref2)
        cand = result.candidates[0]
        # provenance 必须完整（红线）
        assert cand.provenance.provider_id == COMFYUI_LOCAL_PROVIDER_ID
        assert cand.provenance.model_id  # 非空
        # workflow snapshot hash 进 model_version（可追溯）：检查前缀匹配
        assert cand.provenance.model_version.startswith("comfy-workflow@sha256:")
        assert cand.provenance.model_version[len("comfy-workflow@"):] in snap.content_hash
        assert cand.provenance.input_prompt_hash  # prompt hash 非空

    def test_cancel_job_propagates_to_server(self) -> None:
        snap = _imported_t2i_snapshot()
        caps = _capabilities_all_present(snap)
        server = FakeComfyServer(profile_id="local-default", available_models=frozenset({
            "sd_xl_base_1.0.safetensors",
        }))
        ex = ComfyWorkflowExecutor(server=server, server_capabilities=caps)
        req = ComfyGenerationRequest(
            request_id="r", snapshot=snap, profile=_local_profile(),
            consent=_full_consent(), modality=GenerationModality.IMAGE, prompt="cat",
        )
        report = ComfyImportReport(snapshot=snap)
        ref = ex.submit_job(req, import_report=report)
        cancelled = ex.cancel_job(ref)
        assert cancelled.state == ComfyJobState.CANCELLED
        assert server.get_job(ref.external_job_id).state == ComfyJobState.CANCELLED

    def test_hash_mismatch_blocks_import(self) -> None:
        # 红线（§9.4 #5 / §9.4 #9）：输出 hash 不一致 → 拒绝导入
        snap = _imported_t2i_snapshot()
        caps = _capabilities_all_present(snap)
        server = FakeComfyServer(profile_id="local-default", available_models=frozenset({
            "sd_xl_base_1.0.safetensors",
        }))
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
            tamper_hash=True,  # 实际下载内容 hash ≠ 报告的
        )
        ref2 = ex.poll_progress(ref)
        result = ex.fetch_outputs(req, ref2)
        # hash 不一致 → 候选为空 + error
        assert not result.succeeded
        assert result.error_code == ComfyExecutorErrorCode.HASH_MISMATCH

    def test_failed_job_produces_error_result(self) -> None:
        snap = _imported_t2i_snapshot()
        caps = _capabilities_all_present(snap)
        server = FakeComfyServer(profile_id="local-default", available_models=frozenset({
            "sd_xl_base_1.0.safetensors",
        }))
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
        assert result.error_code == ComfyExecutorErrorCode.SERVER_FAILED

    def test_cancelled_job_produces_cancelled_result(self) -> None:
        snap = _imported_t2i_snapshot()
        caps = _capabilities_all_present(snap)
        server = FakeComfyServer(profile_id="local-default", available_models=frozenset({
            "sd_xl_base_1.0.safetensors",
        }))
        ex = ComfyWorkflowExecutor(server=server, server_capabilities=caps)
        req = ComfyGenerationRequest(
            request_id="r", snapshot=snap, profile=_local_profile(),
            consent=_full_consent(), modality=GenerationModality.IMAGE, prompt="cat",
        )
        report = ComfyImportReport(snapshot=snap)
        ref = ex.submit_job(req, import_report=report)
        server.cancel(ref.external_job_id)
        ref2 = ex.poll_progress(ref)
        result = ex.fetch_outputs(req, ref2)
        assert not result.succeeded
        assert result.error_code == ComfyExecutorErrorCode.CANCELLED

    def test_generate_timeout_requests_provider_cancel(self) -> None:
        snap = _imported_t2i_snapshot()
        caps = _capabilities_all_present(snap)
        server = FakeComfyServer(profile_id="local-default")
        submitted = _track_submitted_job_ids(server)
        ex = ComfyWorkflowExecutor(
            server=server, server_capabilities=caps,
            poll_timeout_s=0.001, poll_interval_s=0.0,
        )
        ex.bind_workflow(snap, profile=_local_profile())
        result = ex.generate(GenerationRequest(
            request_id="timeout-request", modality=GenerationModality.IMAGE,
            prompt="cat", consent=_full_consent(),
        ))
        assert result.error_code == ComfyExecutorErrorCode.TIMEOUT
        # 公共协议断言：被超时打断的 job 必须已被 provider cancel（AUD-23521）
        assert len(submitted) == 1
        assert server.get_job(submitted[0]).state == ComfyJobState.CANCELLED

    def test_generate_cooperative_cancel_propagates(self) -> None:
        """is_cancelled 注入（GenerationService 按签名自动注入）：轮询中
        检查到取消 → 传播 server 取消 → CANCELLED 诊断，不等到超时。"""
        snap = _imported_t2i_snapshot()
        caps = _capabilities_all_present(snap)
        server = FakeComfyServer(profile_id="local-default")
        submitted = _track_submitted_job_ids(server)
        ex = ComfyWorkflowExecutor(
            server=server, server_capabilities=caps,
            poll_timeout_s=30.0, poll_interval_s=0.0,
        )
        ex.bind_workflow(snap, profile=_local_profile())
        result = ex.generate(
            GenerationRequest(
                request_id="coop-cancel-request", modality=GenerationModality.IMAGE,
                prompt="cat", consent=_full_consent(),
            ),
            is_cancelled=lambda: True,
        )
        assert result.error_code == ComfyExecutorErrorCode.CANCELLED
        # 公共协议断言（AUD-23521）：不穿透 server._jobs 私有结构
        assert len(submitted) == 1
        assert server.get_job(submitted[0]).state == ComfyJobState.CANCELLED

    def test_fetch_outputs_uses_public_job_outputs_protocol(self) -> None:
        """executor 不穿透 _jobs 私有结构（Fake/Real 同 job_outputs 协议）。"""
        snap = _imported_t2i_snapshot()
        caps = _capabilities_all_present(snap)
        server = FakeComfyServer(profile_id="local-default", cost_units=1.5)
        server.auto_complete_with_image = True
        ex = ComfyWorkflowExecutor(server=server, server_capabilities=caps)
        req = ComfyGenerationRequest(
            request_id="r-protocol", snapshot=snap, profile=_local_profile(),
            consent=_full_consent(), modality=GenerationModality.IMAGE, prompt="cat",
        )
        ref = ex.submit_job(req, import_report=ComfyImportReport(snapshot=snap))
        ref = ex.poll_until_terminal(ref, timeout_s=1.0)
        outputs, hashes, cost = server.job_outputs(ref.external_job_id)
        assert outputs and cost == 1.5
        assert set(hashes) == {o.filename for o in outputs}

    def test_generate_enforces_provider_cost_budget(self) -> None:
        snap = _imported_t2i_snapshot()
        caps = _capabilities_all_present(snap)
        server = FakeComfyServer(profile_id="local-default", cost_units=2.0)
        server.auto_complete_with_image = True
        ex = ComfyWorkflowExecutor(server=server, server_capabilities=caps)
        ex.bind_workflow(snap, profile=_local_profile())
        result = ex.generate(GenerationRequest(
            request_id="cost-request", modality=GenerationModality.IMAGE,
            prompt="cat", consent=_full_consent(),
            budget=GenerationBudget(max_cost_units=1.0),
        ))
        assert result.error_code == ComfyExecutorErrorCode.COST_LIMIT_EXCEEDED
        assert result.actual_cost_units == 2.0

    def test_generate_rejects_mismatched_provider_consent(self) -> None:
        snap = _imported_t2i_snapshot()
        caps = _capabilities_all_present(snap)
        server = FakeComfyServer(profile_id="local-default")
        ex = ComfyWorkflowExecutor(server=server, server_capabilities=caps)
        ex.bind_workflow(snap, profile=_local_profile())
        consent = GenerationConsent(
            confirmed=True, provider_id=COMFYUI_CLOUD_PROVIDER_ID,
            upload_acknowledged=True, cost_acknowledged=True,
            copyright_acknowledged=True,
        )
        result = ex.generate(GenerationRequest(
            request_id="provider-mismatch", modality=GenerationModality.IMAGE,
            prompt="cat", consent=consent,
        ))
        assert result.error_code == ComfyExecutorErrorCode.CONSENT_PROVIDER_MISMATCH


# ---------------------------------------------------------------------- #
# GenerationProvider 协议适配（标准 generate 路径）
# ---------------------------------------------------------------------- #


class TestComfyExecutorAsGenerationProvider:
    def test_implements_generation_provider_protocol(self) -> None:
        snap = _imported_t2i_snapshot()
        caps = _capabilities_all_present(snap)
        server = FakeComfyServer(profile_id="local-default", available_models=frozenset({
            "sd_xl_base_1.0.safetensors",
        }))
        ex = ComfyWorkflowExecutor(server=server, server_capabilities=caps)
        # ComfyWorkflowExecutor 满足 GenerationProvider 协议（duck typing：provider_id +
        # supported_modalities + generate）；GenerationProvider 未声明 runtime_checkable，
        # 用属性检查代替 isinstance
        assert hasattr(ex, "provider_id")
        assert hasattr(ex, "supported_modalities")
        assert callable(getattr(ex, "generate", None))

    def test_generate_drives_full_pipeline(self) -> None:
        snap = _imported_t2i_snapshot()
        caps = _capabilities_all_present(snap)
        server = FakeComfyServer(profile_id="local-default", available_models=frozenset({
            "sd_xl_base_1.0.safetensors",
        }))
        # server 在 submit 后自动完成（auto_complete=True）
        server.auto_complete_with_image = True
        ex = ComfyWorkflowExecutor(server=server, server_capabilities=caps)

        # 标准 GenerationRequest（provider-neutral）
        gen_req = GenerationRequest(
            request_id="gen-req-001",
            modality=GenerationModality.IMAGE,
            prompt="a cute cat",
            consent=_full_consent(),
        )
        # 给 executor 注入 workflow + profile
        ex.bind_workflow(snap, profile=_local_profile())
        result = ex.generate(gen_req)
        assert isinstance(result, GenerationResult)
        assert result.succeeded
        assert len(result.candidates) >= 1

    def test_generate_with_unconsented_request_errors(self) -> None:
        snap = _imported_t2i_snapshot()
        caps = _capabilities_all_present(snap)
        server = FakeComfyServer(profile_id="local-default", available_models=frozenset({
            "sd_xl_base_1.0.safetensors",
        }))
        ex = ComfyWorkflowExecutor(server=server, server_capabilities=caps)
        ex.bind_workflow(snap, profile=_local_profile())
        # 不完整 consent
        bad = GenerationRequest(
            request_id="gen-x",
            modality=GenerationModality.IMAGE,
            prompt="x",
            consent=GenerationConsent(confirmed=False),
        )
        result = ex.generate(bad)
        assert not result.succeeded
        assert result.error_code == ComfyExecutorErrorCode.CONSENT_INCOMPLETE
