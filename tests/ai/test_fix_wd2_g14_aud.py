"""批 D 修复测试（wd2-g14）：ComfyUI 适配器 16 条 Major 级审计条目。

对应证据报告：
- docs/audit/2026-09-28/batches/batch-230-features-ai_workflow-adapters.md
- docs/audit/2026-09-28/batches/batch-231-features-ai_workflow-adapters.md

覆盖条目：AUD-23001/23002/23003/23011/23012/23013/23020/23023/
AUD-23100/23101/23102/23106/23108/23113/23114（AUD-23004 的行为用例在
src/features/ai_workflow/tests/test_comfyui_client.py::TestCancel 中就地更新）。
"""
from __future__ import annotations

import dataclasses
import inspect
import io
import json
import logging
from typing import Any, Dict, Tuple

import pytest

from src.features.ai_workflow.adapters.comfyui_client import (
    _ComfyHttpError,
    RealComfyClient,
)
from src.features.ai_workflow.adapters.comfyui_executor import (
    ComfyExecutorErrorCode,
    ComfyWorkflowExecutor,
    FakeComfyServer,
)
from src.features.ai_workflow.adapters.comfyui_export import ComfyWorkflowExporter
from src.features.ai_workflow.adapters.comfyui_importer import (
    ComfyPreflightChecker,
    ComfyServerCapabilities,
    ComfyWorkflowImporter,
    PreflightDenyCode,
)
from src.features.ai_workflow.adapters.comfyui_models import (
    _BUILTIN_CLASS_TYPES,
    ComfyConnectionProfile,
    ComfyInputBinding,
    ComfyInputSlot,
    ComfyWorkflowSnapshot,
)
from src.features.ai_workflow.adapters.comfyui_profiles import ComfyProfileRegistry
from src.features.ai_workflow.adapters.composite_adapter import (
    CompositeRenderJobRef,
    CompositeRenderJobState,
    CompositeSubgraphAdapter,
    CompositeSubgraphRequest,
    CompositeGraphRef,
    FakeCompositeRenderService,
)
from src.features.ai_workflow.adapters.generation_node_specs import (
    register_generation_node_specs,
)
from src.features.ai_workflow.core.node_specs import (
    NodeSpecRegistry,
    register_official_specs,
)
from src.features.edit.ai_editing.generation_models import (
    GenerationConsent,
    GenerationModality,
    GenerationRequest,
)


# ---------------------------------------------------------------------- #
# 公共 fixture / 辅助
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
        "5": {"class_type": "EmptyLatentImage", "inputs": {
            "width": 1024, "height": 1024, "batch_size": 1,
        }},
        "8": {"class_type": "VAEDecode", "inputs": {"samples": ["3", 0], "vae": ["4", 2]}},
        "9": {"class_type": "SaveImage", "inputs": {
            "images": ["8", 0], "filename_prefix": "topos",
        }},
    }


def _local_profile() -> ComfyConnectionProfile:
    return ComfyConnectionProfile(
        profile_id="local-default", kind="local",
        base_url="http://127.0.0.1:8188",
    )


def _full_consent() -> GenerationConsent:
    return GenerationConsent(
        confirmed=True,
        upload_acknowledged=True,
        cost_acknowledged=True,
        copyright_acknowledged=True,
        confirmed_at="2026-09-29T10:00:00Z",
    )


def _imported_snapshot() -> ComfyWorkflowSnapshot:
    return ComfyWorkflowImporter().import_from_dict(
        "wf-t2i", _sdxl_t2i_workflow(), display_name="SDXL T2I",
    )


def _caps_all_present(snap: ComfyWorkflowSnapshot) -> ComfyServerCapabilities:
    return ComfyServerCapabilities(
        profile_id="local-default",
        available_class_types=frozenset({n.class_type for n in snap.nodes}),
        available_models=frozenset({m.model_name for m in snap.required_models}),
        custom_node_repos=frozenset(),
    )


class _FakeResponse:
    def __init__(self, body: bytes | str) -> None:
        if isinstance(body, str):
            body = body.encode("utf-8")
        self._stream = io.BytesIO(body)

    def read(self, limit: int = -1) -> bytes:
        return self._stream.read(limit if limit > 0 else -1)

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *args: Any) -> None:
        self._stream.close()


class _ScriptedOpener:
    """按 (method, path) 返回预设响应的 fake opener（路由 dict 可中途修改）。"""

    def __init__(self, routes: Dict[Tuple[str, str], Any]) -> None:
        self.routes = routes
        self.calls: list[Tuple[str, str]] = []

    def __call__(self, request: Any, timeout: float = 0.0) -> Any:
        from urllib.parse import urlsplit
        split = urlsplit(request.full_url)
        key = (request.get_method(), split.path)
        self.calls.append(key)
        body = self.routes.get(key)
        if isinstance(body, Exception):
            raise body
        if isinstance(body, (_FakeResponse, _FlakyDownloadStream)):
            return body  # 已是流式响应（含故障注入流）
        if callable(body):
            body = body(request)
            if isinstance(body, (_FakeResponse, _FlakyDownloadStream)):
                return body
        if isinstance(body, str):
            body = body.encode("utf-8")
        return _FakeResponse(body or b"{}")


class _FlakyDownloadStream:
    """先返回一块数据、再断流的下载流（制造半成品文件）。"""

    def __init__(self) -> None:
        self._reads = 0

    def read(self, limit: int = -1) -> bytes:
        self._reads += 1
        if self._reads == 1:
            return b"partial-bytes"
        raise OSError("connection reset mid-download")

    def __enter__(self) -> "_FlakyDownloadStream":
        return self

    def __exit__(self, *args: Any) -> None:
        return None


def _client(
    routes: Dict[Tuple[str, str], Any],
    *,
    download_dir: Any = None,
) -> Tuple[RealComfyClient, _ScriptedOpener]:
    opener = _ScriptedOpener(routes)
    client = RealComfyClient(
        _local_profile(), None,
        opener=opener, sleep=lambda _s: None,
        download_dir=download_dir,
    )
    return client, opener


def _inject_job(client: RealComfyClient, job_id: str, state: str) -> Any:
    from src.features.ai_workflow.adapters.comfyui_client import _JobRecord

    record = _JobRecord(
        external_job_id=job_id,
        profile_id=client.profile_id,
        workflow_snapshot_hash="sha256:wf",
        state=state,
        submit_time_iso="2026-09-29T00:00:00Z",
    )
    client._jobs[job_id] = record
    return record


def _history_success(filename: str) -> str:
    return json.dumps({
        "abc": {"status": {"status_str": "success"},
                "outputs": {"9": {"images": [
                    {"filename": filename, "subfolder": "", "type": "output"},
                ]}}},
    })


class _FakeConfig:
    def __init__(self) -> None:
        self.values: Dict[str, Any] = {}

    def get(self, key: str, default: Any = None) -> Any:
        return self.values.get(key, default)

    def set(self, key: str, value: Any, auto_save: bool = True) -> None:
        self.values[key] = value


# ---------------------------------------------------------------------- #
# AUD-23001：capabilities() 按真实 /object_info 结构解析模型候选
# ---------------------------------------------------------------------- #


class TestAud23001CapabilitiesParsing:
    def _object_info(self) -> dict:
        # 真实 ComfyUI /object_info 结构：input -> {"required"/"optional"} ->
        # {输入名: [候选列表|类型名, 配置]}；顶层键 = class_type（覆盖
        # _sdxl_t2i_workflow 用到的全部节点类型）
        return {
            "KSampler": {
                "input": {"required": {
                    "seed": ["INT", {"default": 0}],
                    "steps": ["INT", {"default": 20}],
                }},
                "output": ["LATENT"],
            },
            "CheckpointLoaderSimple": {
                "input": {"required": {
                    "ckpt_name": [
                        ["sd_xl_base_1.0.safetensors", "flux-dev.safetensors"],
                        {},
                    ],
                }},
                "output": ["MODEL", "CLIP", "VAE"],
            },
            "VAELoader": {
                "input": {"optional": {
                    "vae_name": [["vae_ft_mse_840000.safetensors"], {}],
                }},
            },
            "CLIPTextEncode": {
                "input": {"required": {"text": ["STRING", {"default": ""}]}},
            },
            "EmptyLatentImage": {
                "input": {"required": {
                    "width": ["INT", {"default": 512}],
                    "height": ["INT", {"default": 512}],
                    "batch_size": ["INT", {"default": 1}],
                }},
            },
            "VAEDecode": {
                "input": {"required": {"samples": ["LATENT", {}]}},
            },
            "SaveImage": {
                "input": {"required": {
                    "images": ["IMAGE", {}],
                    "filename_prefix": ["STRING", {"default": "ComfyUI"}],
                }},
            },
        }

    def test_models_collected_from_required_and_optional(self) -> None:
        client, _ = _client({
            ("GET", "/object_info"): json.dumps(self._object_info()),
        })
        caps = client.capabilities()
        assert caps.available_models == frozenset({
            "sd_xl_base_1.0.safetensors",
            "flux-dev.safetensors",
            "vae_ft_mse_840000.safetensors",
        })
        assert "CheckpointLoaderSimple" in caps.available_class_types

    def test_real_path_preflight_no_longer_denies_model(self) -> None:
        # Real 路径闭环：capabilities() 的模型集合能通过 preflight（此前恒空集
        # → MISSING_MODEL 必拒）。
        client, _ = _client({
            ("GET", "/object_info"): json.dumps(self._object_info()),
        })
        caps = client.capabilities()
        report = ComfyWorkflowImporter().import_and_report("wf", _sdxl_t2i_workflow())
        result = ComfyPreflightChecker().check(report, capabilities=caps)
        assert result.allowed is True


# ---------------------------------------------------------------------- #
# AUD-23002：job_outputs 下载失败不固化 outputs_fetched / 清理半成品
# ---------------------------------------------------------------------- #


class TestAud23002OutputsFetchRetry:
    def test_download_failure_raises_and_keeps_retryable(self, tmp_path) -> None:
        import urllib.error
        routes: Dict[Tuple[str, str], Any] = {
            ("GET", "/history/abc"): _history_success("out.png"),
            ("GET", "/view"): urllib.error.URLError("boom"),
        }
        client, _ = _client(routes, download_dir=tmp_path)
        _inject_job(client, "abc", "queued")
        with pytest.raises(_ComfyHttpError):
            client.job_outputs("abc")
        record = client._jobs["abc"]
        assert record.outputs_fetched is False  # 未固化 → 可重试
        assert record.actual_output_hashes == {}
        # 无半成品文件残留
        assert list(tmp_path.iterdir()) == []

    def test_partial_download_file_cleaned_up(self, tmp_path) -> None:
        routes: Dict[Tuple[str, str], Any] = {
            ("GET", "/history/abc"): _history_success("out.png"),
            ("GET", "/view"): _FlakyDownloadStream(),
        }
        client, _ = _client(routes, download_dir=tmp_path)
        _inject_job(client, "abc", "queued")
        with pytest.raises(_ComfyHttpError):
            client.job_outputs("abc")
        assert list(tmp_path.iterdir()) == []
        assert client._jobs["abc"].outputs_fetched is False

    def test_retry_after_transient_failure_succeeds(self, tmp_path) -> None:
        import hashlib
        import urllib.error
        routes: Dict[Tuple[str, str], Any] = {
            ("GET", "/history/abc"): _history_success("out.png"),
            ("GET", "/view"): urllib.error.URLError("transient"),
        }
        opener = _ScriptedOpener(routes)
        client = RealComfyClient(
            _local_profile(), None, opener=opener, sleep=lambda _s: None,
            download_dir=tmp_path,
        )
        _inject_job(client, "abc", "queued")
        with pytest.raises(_ComfyHttpError):
            client.job_outputs("abc")
        # 网络恢复后同一 job 重试成功（outputs_fetched 未被失败固化）
        routes[("GET", "/view")] = b"png-bytes"
        outputs, hashes, _ = client.job_outputs("abc")
        assert len(outputs) == 1
        assert hashes["out.png"] == "sha256:" + hashlib.sha256(b"png-bytes").hexdigest()
        assert client._jobs["abc"].outputs_fetched is True

    def test_executor_maps_fetch_failure_to_server_failed(self) -> None:
        # executor 边界：job_outputs 抛错 → SERVER_FAILED 诊断（不固化"无产物"）
        from src.features.ai_workflow.adapters.comfyui_executor import (
            ComfyGenerationRequest,
        )
        snap = _imported_snapshot()
        server = _FailingOutputsServer(profile_id="local-default")
        ex = ComfyWorkflowExecutor(
            server=server, server_capabilities=_caps_all_present(snap),
            poll_timeout_s=5.0, poll_interval_s=0.0,
        )
        req = ComfyGenerationRequest(
            request_id="fetch-fail", snapshot=snap, profile=_local_profile(),
            consent=_full_consent(), modality=GenerationModality.IMAGE, prompt="cat",
        )
        ref = ex.submit_job(
            req,
            import_report=ComfyWorkflowImporter().import_and_report(
                "wf", _sdxl_t2i_workflow(),
            ),
        )
        server.complete(ref.external_job_id)
        result = ex.fetch_outputs(req, server.get_job(ref.external_job_id))
        assert result.error_code == ComfyExecutorErrorCode.SERVER_FAILED


class _FailingOutputsServer(FakeComfyServer):
    """/view 下载失败的 Fake server（job_outputs 抛错）。"""

    def job_outputs(
        self, external_job_id: str,
    ) -> Tuple[list, Dict[str, str], float]:
        raise RuntimeError("download failed (network)")

    def complete(self, external_job_id: str) -> None:
        self._jobs[external_job_id].state = "success"


# ---------------------------------------------------------------------- #
# AUD-23003：下载文件名路径穿越防护
# ---------------------------------------------------------------------- #


class TestAud23003PathTraversalGuard:
    def test_traversal_filename_sanitized_to_basename(self, tmp_path) -> None:
        client, _ = _client({
            ("GET", "/history/abc"): _history_success("../../../../evil.png"),
            ("GET", "/view"): b"png-data",
        }, download_dir=tmp_path)
        _inject_job(client, "abc", "queued")
        outputs, hashes, _ = client.job_outputs("abc")
        # 落盘文件只在下载目录内，且文件名是纯 basename
        files = sorted(p.name for p in tmp_path.iterdir())
        assert files == ["abc_evil.png"]
        assert (tmp_path / "abc_evil.png").exists()
        # 穿越目标（下载目录之外）不存在
        assert not (tmp_path.parent / "evil.png").exists()
        assert outputs[0].local_path.endswith("abc_evil.png")


# ---------------------------------------------------------------------- #
# AUD-23011：默认轮询超时放宽到分钟级
# ---------------------------------------------------------------------- #


class TestAud23011PollTimeoutDefault:
    def test_default_poll_timeout_is_minutes_scale(self) -> None:
        sig = inspect.signature(ComfyWorkflowExecutor.__init__)
        default = sig.parameters["poll_timeout_s"].default
        assert isinstance(default, float)
        assert default >= 300.0, (
            "默认 poll_timeout_s 必须放宽到分钟级（真实 SDXL 生成普遍 >30s）"
        )

    def test_explicit_small_timeout_still_supported(self) -> None:
        # 测试/Fake 路径仍可显式传小值
        ex = ComfyWorkflowExecutor(
            server=FakeComfyServer(profile_id="p"),
            server_capabilities=ComfyServerCapabilities(
                profile_id="p",
                available_class_types=frozenset(),
                available_models=frozenset(),
            ),
            poll_timeout_s=0.01, poll_interval_s=0.0,
        )
        assert ex._poll_timeout_s == 0.01


# ---------------------------------------------------------------------- #
# AUD-23012：generate 协议路径携带输入绑定 + MISSING_BINDING 门禁生效
# ---------------------------------------------------------------------- #


class TestAud23012BindingsInGeneratePath:
    def test_bindings_flowed_into_submitted_payload(self) -> None:
        snap = _imported_snapshot()
        server = FakeComfyServer(profile_id="local-default")
        server.auto_complete_with_image = True
        ex = ComfyWorkflowExecutor(
            server=server, server_capabilities=_caps_all_present(snap),
            poll_timeout_s=5.0, poll_interval_s=0.0,
        )
        binding = ComfyInputBinding(
            slot=ComfyInputSlot(
                slot_id="seed", node_id="3", input_name="seed",
                artifact_type="integer", required=True,
            ),
            resolved_value="777",
        )
        ex.bind_workflow(snap, profile=_local_profile(), bindings=(binding,))
        result = ex.generate(GenerationRequest(
            request_id="bind-001", modality=GenerationModality.IMAGE,
            prompt="cat", consent=_full_consent(),
        ))
        assert result.succeeded, result.error_message
        job = next(iter(server._jobs.values()))
        assert job.payload["3"]["inputs"]["seed"] == 777

    def test_unbound_required_slot_blocks_generate(self) -> None:
        snap = _imported_snapshot()
        # 注入一个"声明必填"的 slot（from_api_format 产物均为 required=False，
        # 必填槽位由宿主声明——如重新绑定的 prompt 槽）
        must_bind = ComfyInputSlot(
            slot_id="must_bind", node_id="6", input_name="text",
            artifact_type="text_document", required=True,
        )
        snap_with_required = dataclasses.replace(
            snap, input_slots=snap.input_slots + (must_bind,),
        )
        server = FakeComfyServer(profile_id="local-default")
        ex = ComfyWorkflowExecutor(
            server=server, server_capabilities=_caps_all_present(snap),
            poll_timeout_s=5.0, poll_interval_s=0.0,
        )
        ex.bind_workflow(snap_with_required, profile=_local_profile())
        result = ex.generate(GenerationRequest(
            request_id="bind-002", modality=GenerationModality.IMAGE,
            prompt="cat", consent=_full_consent(),
        ))
        # MISSING_BINDING 门禁生效（此前恒空转）
        assert not result.succeeded
        assert result.error_code == ComfyExecutorErrorCode.PREFLIGHT_FAILED
        assert "missing_binding" in result.error_message
        assert server.job_count == 0

    def test_binding_required_slot_unblocks_generate(self) -> None:
        snap = _imported_snapshot()
        slot = ComfyInputSlot(
            slot_id="must_bind", node_id="6", input_name="text",
            artifact_type="text_document", required=True,
        )
        snap_with_required = dataclasses.replace(
            snap, input_slots=snap.input_slots + (slot,),
        )
        server = FakeComfyServer(profile_id="local-default")
        server.auto_complete_with_image = True
        ex = ComfyWorkflowExecutor(
            server=server, server_capabilities=_caps_all_present(snap),
            poll_timeout_s=5.0, poll_interval_s=0.0,
        )
        ex.bind_workflow(
            snap_with_required, profile=_local_profile(),
            bindings=(ComfyInputBinding(slot=slot, resolved_value="bound text"),),
        )
        result = ex.generate(GenerationRequest(
            request_id="bind-003", modality=GenerationModality.IMAGE,
            prompt="cat", consent=_full_consent(),
        ))
        assert result.succeeded, result.error_message


# ---------------------------------------------------------------------- #
# AUD-23013：实际 hash 缺失 → HASH_MISMATCH（不回退期望值）
# ---------------------------------------------------------------------- #


class TestAud23013MissingActualHash:
    def test_missing_actual_hash_rejected(self) -> None:
        from src.features.ai_workflow.adapters.comfyui_executor import (
            ComfyGenerationRequest,
        )
        from src.features.ai_workflow.adapters.comfyui_models import (
            ComfyOutputArtifact,
        )
        snap = _imported_snapshot()
        server = FakeComfyServer(profile_id="local-default")
        ex = ComfyWorkflowExecutor(
            server=server, server_capabilities=_caps_all_present(snap),
            poll_timeout_s=5.0, poll_interval_s=0.0,
        )
        req = ComfyGenerationRequest(
            request_id="hash-miss", snapshot=snap, profile=_local_profile(),
            consent=_full_consent(), modality=GenerationModality.IMAGE, prompt="cat",
        )
        ref = ex.submit_job(
            req,
            import_report=ComfyWorkflowImporter().import_and_report("wf", _sdxl_t2i_workflow()),
        )
        server.complete_with_outputs(
            ref.external_job_id,
            outputs=(ComfyOutputArtifact(
                node_id="9", output_index=0, artifact_kind="image",
                filename="out.png", content_hash="sha256:" + "d" * 64,
                bytes_size=10,
            ),),
        )
        # 模拟"实际 hash 缺失"（如下载失败后的 pending 占位路径）
        server._jobs[ref.external_job_id].actual_output_hashes = {}
        result = ex.fetch_outputs(req, server.get_job(ref.external_job_id))
        assert not result.succeeded
        assert result.error_code == ComfyExecutorErrorCode.HASH_MISMATCH
        assert result.candidates == ()


# ---------------------------------------------------------------------- #
# AUD-23020：导出（含 _topos_meta）→ 导入往返可用
# ---------------------------------------------------------------------- #


class TestAud23020ExportImportRoundtrip:
    def test_pure_export_roundtrips_through_importer(self) -> None:
        snap = _imported_snapshot()
        result = ComfyWorkflowExporter().export_snapshot(snap)
        assert result.equivalence_guaranteed is True
        # 导出 JSON 仍含 _topos_meta（可追溯红线）
        payload = json.loads(result.api_format_json)
        assert "_topos_meta" in payload
        # importer 显式跳过元数据键——此前直接抛 "missing string class_type"
        snap2 = ComfyWorkflowImporter().import_from_json_string(
            "roundtrip", result.api_format_json,
        )
        assert snap2.node_count == snap.node_count
        assert {n.class_type for n in snap2.nodes} == {n.class_type for n in snap.nodes}
        # raw_payload（提交给 Comfy server 的 payload）不含元数据键
        assert "_topos_meta" not in snap2.raw_payload
        assert json.dumps(snap2.raw_payload, sort_keys=True)

    def test_placeholder_nodes_survive_reimport(self) -> None:
        from src.features.ai_workflow.adapters.comfyui_export import (
            NativeNodePlaceholder,
        )
        snap = _imported_snapshot()
        result = ComfyWorkflowExporter().export_with_native_nodes(
            snap,
            (NativeNodePlaceholder(
                topos_node_id="agent-1", topos_spec_id="topos.agent.review",
                kind="agent",
            ),),
        )
        assert result.equivalence_guaranteed is False
        snap2 = ComfyWorkflowImporter().import_from_json_string(
            "mixed", result.api_format_json,
        )
        # 占位节点（带 class_type 的 "_" 前缀键）保留，只有元数据键被跳过
        class_types = {n.class_type for n in snap2.nodes}
        assert "_ToposNativePlaceholder_agent" in class_types
        assert snap2.node_count == snap.node_count + 1

    def test_meta_only_payload_rejected_as_empty(self) -> None:
        with pytest.raises(ValueError, match="empty"):
            ComfyWorkflowImporter().import_from_json_string(
                "meta-only", json.dumps({"_topos_meta": {"workflow_id": "x"}}),
            )


# ---------------------------------------------------------------------- #
# AUD-23023：UNPINNED_CUSTOM_NODE 门禁可达（未 pin 默认拒绝）
# ---------------------------------------------------------------------- #


class TestAud23023UnpinnedGateReachable:
    def test_unknown_custom_node_without_repo_info_denied(self) -> None:
        # server 已装该 class_type（MISSING_NODE 不触发），但 repo 信息不可得
        # → 无法验证 pin → §13.7 默认拒绝（此前静默放行）
        raw = _sdxl_t2i_workflow()
        raw["999"] = {"class_type": "SomeCustom", "inputs": {}}
        report = ComfyWorkflowImporter().import_and_report("wf", raw)
        caps = ComfyServerCapabilities(
            profile_id="local",
            available_class_types=frozenset(
                {n.class_type for n in report.snapshot.nodes}
            ),
            available_models=frozenset(
                {m.model_name for m in report.required_models}
            ),
            custom_node_repos=frozenset(),  # RealComfyClient.capabilities() 现状
        )
        result = ComfyPreflightChecker().check(report, capabilities=caps)
        assert result.allowed is False
        assert result.deny_code == PreflightDenyCode.UNPINNED_CUSTOM_NODE

    def test_gate_opt_out_still_allows(self) -> None:
        raw = _sdxl_t2i_workflow()
        raw["999"] = {"class_type": "SomeCustom", "inputs": {}}
        report = ComfyWorkflowImporter().import_and_report("wf", raw)
        caps = ComfyServerCapabilities(
            profile_id="local",
            available_class_types=frozenset(
                {n.class_type for n in report.snapshot.nodes}
            ),
            available_models=frozenset(
                {m.model_name for m in report.required_models}
            ),
        )
        checker = ComfyPreflightChecker(require_pinned_custom_nodes=False)
        assert checker.check(report, capabilities=caps).allowed is True

    def test_builtin_only_workflow_still_allowed(self) -> None:
        report = ComfyWorkflowImporter().import_and_report("wf", _sdxl_t2i_workflow())
        caps = ComfyServerCapabilities(
            profile_id="local",
            available_class_types=frozenset(
                {n.class_type for n in report.snapshot.nodes}
            ),
            available_models=frozenset(
                {m.model_name for m in report.required_models}
            ),
        )
        assert ComfyPreflightChecker().check(report, capabilities=caps).allowed is True


# ---------------------------------------------------------------------- #
# AUD-23100：内置白名单条目无首尾空格
# ---------------------------------------------------------------------- #


class TestAud23100BuiltinWhitelist:
    def test_no_entry_has_surrounding_whitespace(self) -> None:
        for ct in _BUILTIN_CLASS_TYPES:
            assert ct == ct.strip(), f"whitelist entry {ct!r} has stray whitespace"

    def test_conditioning_concat_recognized_as_builtin(self) -> None:
        raw = {
            "1": {"class_type": "ConditioningConcat", "inputs": {
                "conditioning_1": ["2", 0], "conditioning_2": ["3", 0],
            }},
            "2": {"class_type": "CLIPTextEncode", "inputs": {
                "text": "a", "clip": ["4", 1],
            }},
            "3": {"class_type": "CLIPTextEncode", "inputs": {
                "text": "b", "clip": ["4", 1],
            }},
            "4": {"class_type": "CLIPLoader", "inputs": {}},
        }
        snap = ComfyWorkflowSnapshot.from_api_format("wf", raw)
        concat = [n for n in snap.nodes if n.class_type == "ConditioningConcat"]
        assert len(concat) == 1
        # 修复前：" ConditioningConcat " 永不匹配 → 被误标 unknown
        assert concat[0].is_unknown_custom_node is False
        assert snap.required_custom_nodes == ()


# ---------------------------------------------------------------------- #
# AUD-23101：同名输入第 3 个起 slot_id 不再重复
# ---------------------------------------------------------------------- #


class TestAud23101SlotIdUniqueness:
    def test_third_duplicate_input_gets_unique_slot_id(self) -> None:
        raw = {}
        for i, node_id in enumerate(("10", "11", "12")):
            raw[node_id] = {"class_type": "EmptyLatentImage", "inputs": {
                "width": 512, "height": 512, "batch_size": 1,
            }}
        snap = ComfyWorkflowSnapshot.from_api_format("wf", raw)
        width_ids = [
            s.slot_id for s in snap.input_slots if s.input_name == "width"
        ]
        assert width_ids == ["width", "width_2", "width_3"]
        # 全图 slot_id 唯一
        all_ids = [s.slot_id for s in snap.input_slots]
        assert len(all_ids) == len(set(all_ids))

    def test_mixed_canonical_bases_share_suffix_counter(self) -> None:
        # "text" 与 "positive" 都规范化到 "prompt" 基准名 → prompt/prompt_2/prompt_3
        raw = {
            "6": {"class_type": "CLIPTextEncode", "inputs": {"text": "a"}},
            "7": {"class_type": "CLIPTextEncode", "inputs": {"text": "b"}},
            "8": {"class_type": "CLIPTextEncode", "inputs": {"text": "c"}},
        }
        snap = ComfyWorkflowSnapshot.from_api_format("wf", raw)
        prompt_ids = sorted(
            s.slot_id for s in snap.input_slots if s.slot_id.startswith("prompt")
        )
        assert prompt_ids == ["prompt", "prompt_2", "prompt_3"]


# ---------------------------------------------------------------------- #
# AUD-23102：raw_payload 深拷贝隔离 + content_hash 防篡改
# ---------------------------------------------------------------------- #


class TestAud23102RawPayloadIsolation:
    def test_caller_mutation_does_not_leak_into_snapshot(self) -> None:
        raw = _sdxl_t2i_workflow()
        snap = ComfyWorkflowSnapshot.from_api_format("wf", raw)
        hash_before = snap.content_hash
        # 调用方在导入后改写原始 dict（旧实现共享嵌套引用 → 快照内容被改而
        # content_hash 不变，防篡改失效）
        raw["3"]["inputs"]["seed"] = 999999
        raw["6"]["inputs"]["text"] = "TAMPERED"
        assert snap.raw_payload["3"]["inputs"]["seed"] == 12345
        assert snap.raw_payload["6"]["inputs"]["text"] == "a cat"
        assert snap.content_hash == hash_before

    def test_two_snapshots_do_not_share_nested_payload(self) -> None:
        raw = {"6": {"class_type": "CLIPTextEncode", "inputs": {"text": "keep"}}}
        s1 = ComfyWorkflowSnapshot.from_api_format("wf", raw)
        s2 = ComfyWorkflowSnapshot.from_api_format("wf", raw)
        # 每个快照持有自己的深拷贝：改其一不影响另一个与调用方原始 dict
        s1.raw_payload["6"]["inputs"]["text"] = "changed"
        assert s2.raw_payload["6"]["inputs"]["text"] == "keep"
        assert raw["6"]["inputs"]["text"] == "keep"


# ---------------------------------------------------------------------- #
# AUD-23106：load_from_config 收窄异常 + 记日志
# ---------------------------------------------------------------------- #


class TestAud23106ProfileLoadDiagnostics:
    def test_bad_entry_logged_and_good_entries_kept(self, caplog) -> None:
        config = _FakeConfig()
        config.values[ComfyProfileRegistry.CONFIG_KEY_PROFILES] = [
            {  # 坏条目：cloud profile 缺 credential_ref → ValueError
                "profile_id": "cloud-broken", "kind": "cloud",
                "base_url": "https://comfy.example.com",
            },
            {  # 好条目
                "profile_id": "local-ok", "kind": "local",
                "base_url": "http://127.0.0.1:8188",
            },
        ]
        registry = ComfyProfileRegistry()
        with caplog.at_level(
            logging.WARNING,
            logger="src.features.ai_workflow.adapters.comfyui_profiles",
        ):
            registry.load_from_config(config)
        assert [p.profile_id for p in registry.list()] == ["local-ok"]
        assert "dropping invalid ComfyUI profile entry" in caplog.text
        assert "cloud-broken" in caplog.text, (
            "坏条目必须记 warning（含 profile_id），不得静默丢弃"
        )


# ---------------------------------------------------------------------- #
# AUD-23108：fetch_result 只走服务公开 get_output
# ---------------------------------------------------------------------- #


class TestAud23108PublicGetOutput:
    def test_fake_service_exposes_public_get_output(self) -> None:
        svc = FakeCompositeRenderService(
            available_graph_hashes=frozenset({"sha256:h"}),
        )
        req = CompositeSubgraphRequest(
            request_id="r1",
            graph_ref=CompositeGraphRef(
                composite_graph_id="g1", content_hash="sha256:h",
            ),
            range_spec="whole",
        )
        ref = svc.submit(req)
        svc.complete_with_output(ref.render_job_id, output_asset_id="asset-1")
        assert svc.get_output(ref.render_job_id) == "asset-1"

    def test_adapter_works_with_service_lacking_private_jobs(self) -> None:
        # 只实现公开面的最小服务（无 _jobs 私有属性）——替换契约成立
        adapter = CompositeSubgraphAdapter(render_service=_MinimalPublicService())
        req = CompositeSubgraphRequest(
            request_id="r2",
            graph_ref=CompositeGraphRef(
                composite_graph_id="g1", content_hash="sha256:h",
            ),
            range_spec="whole",
        )
        ref = adapter.submit_render(req)
        adapter._svc.complete(ref.render_job_id)  # noqa: SLF001 — 测试驱动
        result = adapter.fetch_result(req, ref)
        assert result.succeeded is True
        assert result.output_asset_id == "public-output"


class _MinimalPublicService:
    """只实现 adapter 依赖的公开面（无 ``_jobs`` 私有字典）。"""

    def __init__(self) -> None:
        self._outputs: Dict[str, str] = {}
        self._states: Dict[str, str] = {}

    def submit(self, req: CompositeSubgraphRequest) -> CompositeRenderJobRef:
        jid = f"render-{len(self._states)}"
        self._states[jid] = CompositeRenderJobState.QUEUED
        return CompositeRenderJobRef(
            render_job_id=jid,
            composite_graph_id=req.graph_ref.composite_graph_id,
            graph_content_hash=req.graph_ref.content_hash,
        )

    def get_job(self, render_job_id: str) -> CompositeRenderJobRef:
        return CompositeRenderJobRef(
            render_job_id=render_job_id,
            composite_graph_id="g1",
            graph_content_hash="sha256:h",
            state=self._states[render_job_id],
        )

    def cancel(self, render_job_id: str) -> CompositeRenderJobRef:
        self._states[render_job_id] = CompositeRenderJobState.CANCELLED
        return self.get_job(render_job_id)

    def get_output(self, render_job_id: str) -> str:
        return self._outputs[render_job_id]

    def complete(self, render_job_id: str) -> None:
        self._states[render_job_id] = CompositeRenderJobState.SUCCESS
        self._outputs[render_job_id] = "public-output"


# ---------------------------------------------------------------------- #
# AUD-23113：register_official_specs 接入生成节点（生产装配可达）
# ---------------------------------------------------------------------- #


class TestAud23113GenerationSpecsWired:
    def test_official_registry_contains_generation_specs(self) -> None:
        reg = NodeSpecRegistry()
        register_official_specs(reg)
        for sid in (
            "topos.generation.text_to_image",
            "topos.generation.image_to_image",
            "topos.generation.inpaint",
            "topos.generation.text_to_video",
            "topos.generation.image_to_video",
            "topos.generation.upscale",
        ):
            assert reg.get(sid) is not None, (
                f"{sid} 未接入 register_official_specs（画布 Tab 搜索不可达）"
            )


# ---------------------------------------------------------------------- #
# AUD-23114：inpaint / image_to_video / upscale 补 params_schema
# ---------------------------------------------------------------------- #


class TestAud23114MissingParamsSchemas:
    def test_three_specs_have_json_schema(self) -> None:
        reg = NodeSpecRegistry()
        register_generation_node_specs(reg)
        for sid in (
            "topos.generation.inpaint",
            "topos.generation.image_to_video",
            "topos.generation.upscale",
        ):
            spec = reg.require(sid)
            assert spec.params_schema.get("type") == "object", sid
            props = spec.params_schema.get("properties")
            assert isinstance(props, dict) and props, (
                f"{sid} params_schema 缺字段（Inspector 渲染不出任何控件）"
            )
            assert spec.params_schema.get("additionalProperties") is False, sid

    def test_schema_fields_content(self) -> None:
        reg = NodeSpecRegistry()
        register_generation_node_specs(reg)
        inpaint = reg.require("topos.generation.inpaint").params_schema["properties"]
        assert "denoise_strength" in inpaint
        assert "mask_blur" in inpaint
        i2v = reg.require("topos.generation.image_to_video").params_schema["properties"]
        assert "motion_strength" in i2v
        assert "duration_seconds" in i2v
        upscale = reg.require("topos.generation.upscale").params_schema["properties"]
        assert "scale_factor" in upscale
        assert upscale["scale_factor"]["minimum"] == 1.0

    def test_all_six_generation_specs_declare_schema(self) -> None:
        reg = NodeSpecRegistry()
        register_generation_node_specs(reg)
        for spec in reg.all_specs():
            assert spec.params_schema.get("properties"), spec.spec_id
