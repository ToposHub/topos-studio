"""批 D 修复回归测试（wd2-g11）：ai_harness 上半部 Major 16 条。

覆盖（与 docs/plans/2026-09-28-multi-machine-program/fixes/reports/wd2-g11.md
对应）：

- AUD-21901：转录路由追踪不再把终态质量冒充中间步骤质量。
- AUD-21905/21906/21907：生成 run 的取消传播 / job 取消终态 / 参考素材
  id 形状校验。
- AUD-22005/22006/22007/22008：legacy adapter 的 DRAFT 终态化 / developer
  路由契约 / 归档异常边界 / 转录追踪 run 级隔离。
- AUD-22014：角色超时预算真实生效（看门狗降级）。
- AUD-22017：HTTP transport 拒绝重定向，Bearer 凭据不出构造期端点。
- AUD-22021：discovery 失败 fail-closed（error_discovery_failed）。
- AUD-22101：不可信 JSON Schema 的复杂度/分支数/pattern 长度上限。
- AUD-22102/22103/22104：stdio transport 重启队列隔离 / close 不等请求锁 /
  stdout 队列有界丢弃。
- AUD-22105：进程隔离插件 invoke 的本地看门狗。
"""

from __future__ import annotations

import concurrent.futures
import json
import queue
import sys
import threading
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from typing import Any

import pytest

pytestmark = pytest.mark.edit_core

from src.features.ai_harness.analysis_skills import collect_transcript_trace
from src.features.ai_harness.generation_adapter import (
    GenerationHarnessAdapter,
    invocation_to_generation_request,
)
from src.features.ai_harness.invocation_models import (
    Invocation,
    InvocationScope,
    PermissionLevel,
    RunPolicy,
    RunStatus,
)
from src.features.ai_harness.legacy_adapters import LegacyHarnessAdapter
from src.features.ai_harness.llm_role_model_client import LlmRoleModelClient
from src.features.ai_harness.mcp_http_transport import (
    HttpMcpTransport,
    McpHttpTransportError,
)
from src.features.ai_harness.mcp_outbound import (
    McpConnectionProfile,
    McpOutboundClient,
    McpServerRegistry,
)
from src.features.ai_harness.mcp_protocol import validate_json_schema
from src.features.ai_harness.mcp_stdio_transport import (
    _STDOUT_QUEUE_MAX,
    SubprocessStdioTransport,
)
from src.features.ai_harness.runtime import HarnessRuntime
from src.features.edit.ai_editing.generation_models import GenerationConsent
from src.features.edit.ai_editing.jobs import JobStatus


# ================================================================
# AUD-21901：转录路由追踪的诚实质量
# ================================================================


class _FakeConfidence:
    confidence = 0.8
    words = (("w", 0, 10, "h", 0.9),)  # 有词级对齐 → _analysis_quality 不扣 0.05


def _analysis_with_router(selection: str) -> Any:
    return SimpleNamespace(
        transcript=(_FakeConfidence(),),
        provenance=SimpleNamespace(extra={"router_selection": selection}),
    )


class TestAud21901TranscriptTraceQuality:
    def test_demucs_selected_qwen_step_quality_unknown(self):
        trace = collect_transcript_trace("a1", _analysis_with_router("qwen3_asr_demucs_v4"))
        qwen = next(s for s in trace.steps if s.provider == "qwen3_asr")
        demucs = next(s for s in trace.steps if s.provider == "demucs_v4")
        # Demucs 胜出：终态质量只属于 Demucs 步
        assert demucs.selected is True
        assert demucs.quality == pytest.approx(0.8)
        # Qwen 原音频步质量未持久化——不得冒用终态值
        assert qwen.selected is False
        assert qwen.quality is None
        assert "未持久化" in qwen.note

    def test_whisperx_fallback_qwen_step_quality_unknown(self):
        trace = collect_transcript_trace("a1", _analysis_with_router("whisperx_fallback"))
        qwen = next(s for s in trace.steps if s.provider == "qwen3_asr")
        whisper = next(s for s in trace.steps if s.provider == "whisperx")
        assert whisper.selected is True
        assert whisper.quality == pytest.approx(0.8)
        assert qwen.quality is None

    def test_low_confidence_demucs_attempt_quality_not_fabricated(self):
        trace = collect_transcript_trace(
            "a1", _analysis_with_router("qwen3_asr_original_low_confidence"),
        )
        qwen = next(s for s in trace.steps if s.provider == "qwen3_asr")
        demucs = next(s for s in trace.steps if s.provider == "demucs_v4")
        # Qwen 被选中（低置信）→ 终态质量属于 Qwen 步
        assert qwen.selected is True
        assert qwen.quality == pytest.approx(0.8)
        # Demucs 尝试过但未胜出 → 质量未知，绝不编造 0.0
        assert demucs.selected is False
        assert demucs.quality is None
        assert demucs.note != ""

    def test_qwen_selected_step_keeps_quality(self):
        trace = collect_transcript_trace("a1", _analysis_with_router("qwen3_asr_original"))
        qwen = next(s for s in trace.steps if s.provider == "qwen3_asr")
        assert qwen.selected is True
        assert qwen.quality == pytest.approx(0.8)
        assert trace.final_quality == pytest.approx(0.8)

    def test_public_dict_serializes_unknown_quality_as_none(self):
        trace = collect_transcript_trace("a1", _analysis_with_router("qwen3_asr_demucs_v4"))
        payload = trace.to_public_dict()
        qwen = next(s for s in payload["steps"] if s["provider"] == "qwen3_asr")
        assert qwen["quality"] is None


# ================================================================
# AUD-21905 / 21906 / 21907：generation_adapter
# ================================================================


def _full_consent() -> GenerationConsent:
    return GenerationConsent(
        confirmed=True, provider_id="stub",
        cost_acknowledged=True, upload_acknowledged=True,
        copyright_acknowledged=True,
    )


def _gen_invocation(**extra_inputs: Any) -> Invocation:
    inputs: dict[str, Any] = {
        "modality": "image", "prompt": "a cat",
        "consent": _full_consent(),
    }
    inputs.update(extra_inputs)
    return Invocation(
        source="chat", skill_id="topos.generate",
        scope=InvocationScope(project_id="p1"),
        policy=RunPolicy(
            permission=PermissionLevel.PROPOSE, allow_generation=True,
        ),
        inputs=inputs,
        parent_session_id="sess1",
    )


@dataclass
class _FakeJobHandle:
    job_id: str = "job-1"
    status: Any = JobStatus.QUEUED
    error_message: str = ""
    result_manifest: dict[str, Any] | None = None
    cancel_requested: bool = False

    def request_cancel(self) -> None:
        self.cancel_requested = True
        self.status = JobStatus.CANCELLING


class _FakeRegistry:
    def __init__(self, handle: _FakeJobHandle) -> None:
        self._handle = handle

    def get(self, job_id: str) -> _FakeJobHandle | None:
        return self._handle if job_id == self._handle.job_id else None


class _ControllablePool:
    def __init__(self) -> None:
        self.future_obj: concurrent.futures.Future = concurrent.futures.Future()

    def future(self, job_id: str) -> concurrent.futures.Future:
        return self.future_obj


class _FakeAsyncGenService:
    """真实 provider 形状：async 路径 + 可控 future（不联网）。"""

    def __init__(self, handle: _FakeJobHandle) -> None:
        self.job_registry = _FakeRegistry(handle)
        self.worker_pool = _ControllablePool()
        self.handle = handle

    def is_demo_request(self, request: Any) -> bool:
        return False

    def request_generation_async(self, request: Any) -> tuple[_FakeJobHandle, list]:
        return self.handle, []


class _FakeSyncGenService:
    """demo provider 形状：同步完成（invoke 内直接收尾）。"""

    def __init__(self, handle: _FakeJobHandle) -> None:
        self.job_registry = _FakeRegistry(handle)
        self.handle = handle

    def is_demo_request(self, request: Any) -> bool:
        return True

    def request_generation(self, request: Any) -> tuple[_FakeJobHandle, list]:
        return self.handle, []


class TestAud21905CancelPropagation:
    def test_cancel_after_invoke_propagates_to_job_and_terminal_cancelled(self):
        handle = _FakeJobHandle()
        svc = _FakeAsyncGenService(handle)
        rt = HarnessRuntime()
        adapter = GenerationHarnessAdapter(svc, rt)
        rid = adapter.invoke(_gen_invocation())
        # 真实 provider：invoke 返回时 run 在 worker future 上等待
        assert rt.get_run(rid).status == RunStatus.RUNNING
        assert handle.cancel_requested is False
        # 用户取消（invoke 已返回——唯一传播点是事件订阅，AUD-21905）
        rt.cancel(rid)
        assert handle.cancel_requested is True
        # job 仍跑完（succeeded）：收尾回调必须让位——终态 CANCELLED 而非 SUCCEEDED
        handle.status = JobStatus.SUCCEEDED
        svc.worker_pool.future_obj.set_result(None)
        rec = rt.get_run(rid)
        assert rec.status == RunStatus.CANCELLED
        adapter.close()

    def test_close_unsubscribes_runtime_events(self):
        handle = _FakeJobHandle()
        svc = _FakeAsyncGenService(handle)
        rt = HarnessRuntime()
        adapter = GenerationHarnessAdapter(svc, rt)
        rid = adapter.invoke(_gen_invocation())
        adapter.close()
        handle2 = _FakeJobHandle(job_id="job-2")
        svc.handle = handle2
        svc.job_registry = _FakeRegistry(handle2)
        rid2 = adapter.invoke(_gen_invocation())
        rt.cancel(rid2)
        # 退订后不再传播（close 语义）
        assert handle2.cancel_requested is False
        svc.worker_pool.future_obj.set_result(None)
        assert rt.get_run(rid).run_id == rid  # sanity


class TestAud21906JobCancelledTerminal:
    def test_job_cancelled_maps_run_to_cancelled_not_failed(self):
        handle = _FakeJobHandle(status=JobStatus.CANCELLED)
        svc = _FakeSyncGenService(handle)
        rt = HarnessRuntime()
        adapter = GenerationHarnessAdapter(svc, rt)
        rid = adapter.invoke(_gen_invocation())
        rec = rt.get_run(rid)
        assert rec.status == RunStatus.CANCELLED
        assert rec.error_code != "GENERATION_JOB_FAILED"
        assert any(d.get("code") == "GENERATION_JOB_CANCELLED" for d in rec.diagnostics)
        adapter.close()

    def test_job_failed_still_maps_to_failed(self):
        handle = _FakeJobHandle(status=JobStatus.FAILED, error_message="boom")
        svc = _FakeSyncGenService(handle)
        rt = HarnessRuntime()
        adapter = GenerationHarnessAdapter(svc, rt)
        rid = adapter.invoke(_gen_invocation())
        rec = rt.get_run(rid)
        assert rec.status == RunStatus.FAILED
        assert rec.error_code == "GENERATION_JOB_FAILED"
        adapter.close()


class TestAud21907ReferenceAssetIds:
    def test_string_input_rejected(self):
        inv = _gen_invocation(reference_asset_ids="asset-abc")
        with pytest.raises(ValueError):
            invocation_to_generation_request(inv)

    def test_non_string_item_rejected(self):
        inv = _gen_invocation(reference_asset_ids=["a1", 123])
        with pytest.raises(ValueError):
            invocation_to_generation_request(inv)

    def test_valid_list_normalized_to_tuple(self):
        inv = _gen_invocation(reference_asset_ids=["a1", "a2"])
        req = invocation_to_generation_request(inv)
        assert req.reference_asset_ids == ("a1", "a2")

    def test_invoke_records_request_build_failed(self):
        from src.features.edit.ai_editing.generation_provider import (
            DeterministicGenerationProvider, ProviderRegistry,
        )
        from src.features.edit.ai_editing.media_generation_service import GenerationService

        reg = ProviderRegistry()
        reg.register(DeterministicGenerationProvider(), preferred=True)
        svc = GenerationService(reg)
        rt = HarnessRuntime()
        adapter = GenerationHarnessAdapter(svc, rt)
        rid = adapter.invoke(_gen_invocation(reference_asset_ids="asset-abc"))
        rec = rt.get_run(rid)
        assert rec.status == RunStatus.FAILED
        assert rec.error_code == "REQUEST_BUILD_FAILED"
        assert any(d.get("code") == "REQUEST_BUILD_FAILED" for d in rec.diagnostics)
        adapter.close()


# ================================================================
# AUD-22005 / 22006 / 22007 / 22008：legacy_adapters
# ================================================================


def _empty_workflow_result() -> Any:
    return SimpleNamespace(
        diagnostics=(), degraded=False, artifact_id=None, variants=(),
        missing_evidence=(), degradation="",
    )


def _empty_skill_result() -> Any:
    return SimpleNamespace(
        diagnostics=(), degraded=False, artifact_id=None, artifacts=(),
        variants=(), cancelled_tasks=(), failed_tasks=(), degradation="",
    )


class _RecordingService:
    """最小 AssistantService 假体：只记录 run_workflow / run_skill 调用。"""

    def __init__(self) -> None:
        self.workflow_calls: list[Any] = []
        self.skill_calls: list[Any] = []
        self.plan_repository: Any = None

    def run_workflow(self, wf_input: Any, **kwargs: Any) -> Any:
        self.workflow_calls.append(wf_input)
        return _empty_workflow_result()

    def run_skill(self, sk_input: Any, *, cancel_requested: Any = None) -> Any:
        self.skill_calls.append(sk_input)
        return _empty_skill_result()


def _legacy_invocation(
    skill_id: str = "topos.subtitle_draft", source: str = "chat",
    base_revision: Any = 0,
) -> Invocation:
    return Invocation(
        source=source, skill_id=skill_id,
        scope=InvocationScope(
            project_id="p1", timeline_id="t1", selected_asset_ids=("a1",),
        ),
        policy=RunPolicy(permission=PermissionLevel.PROPOSE),
        inputs={"brief_id": "b1", "target_duration": 720, "base_revision": base_revision},
        parent_session_id="sess1",
    )


class TestAud22005DraftTerminal:
    def test_skill_input_failure_reaches_failed_terminal(self):
        svc = _RecordingService()
        rt = HarnessRuntime()
        adapter = LegacyHarnessAdapter(svc, rt)
        rid = adapter.invoke(_legacy_invocation(skill_id="topos.not_a_workflow",
                                                base_revision="abc"))
        rec = rt.get_run(rid)
        assert rec.status == RunStatus.FAILED  # 不再永久停在 draft
        assert rec.error_code == "SKILL_INPUT_FAILED"
        assert rec.finished_at is not None
        assert svc.skill_calls == []  # 未触达 provider

    def test_workflow_mapping_failure_reaches_failed_terminal(self):
        svc = _RecordingService()
        rt = HarnessRuntime()
        adapter = LegacyHarnessAdapter(svc, rt)
        # 直接走 _run_workflow_path（invoke 会先经 _should_route_workflow 预探
        # 测，未知 skill 不会进该分支——audit 已注明该路径多为 invoke_compiled
        # 以外的兜底；此处验证分支本身的终态化）。
        rid = rt.start(_legacy_invocation(skill_id="totally.unknown"))
        adapter._run_workflow_path(rid, _legacy_invocation(skill_id="totally.unknown"))
        rec = rt.get_run(rid)
        assert rec.status == RunStatus.FAILED  # 不再永久停在 draft
        assert rec.error_code == "WORKFLOW_MAPPING_FAILED"
        assert rec.finished_at is not None


class TestAud22006DeveloperRouting:
    def test_developer_source_routes_to_skill_not_workflow(self):
        svc = _RecordingService()
        rt = HarnessRuntime()
        adapter = LegacyHarnessAdapter(svc, rt)
        rid = adapter.invoke(_legacy_invocation(source="developer"))
        assert len(svc.skill_calls) == 1
        assert svc.workflow_calls == []
        assert rt.get_run(rid).status in (
            RunStatus.SUCCEEDED, RunStatus.SUCCEEDED_WITH_WARNINGS,
            RunStatus.FAILED,
        )

    def test_non_developer_source_still_routes_to_workflow(self):
        svc = _RecordingService()
        rt = HarnessRuntime()
        adapter = LegacyHarnessAdapter(svc, rt)
        adapter.invoke(_legacy_invocation(source="chat"))
        assert len(svc.workflow_calls) == 1
        assert svc.skill_calls == []


class _ExplodingRepo:
    def get_plan(self, plan_id: str) -> Any:
        raise RuntimeError("plan store corrupted")

    def store(self, plan: Any) -> None:
        raise RuntimeError("plan store corrupted")


class TestAud22007ArchiveFailureBoundary:
    def test_workflow_archive_failure_gets_terminal(self):
        svc = _RecordingService()
        svc.plan_repository = _ExplodingRepo()

        def _with_variants(inp: Any, **kwargs: Any) -> Any:
            result = _empty_workflow_result()
            result.variants = (SimpleNamespace(edit_plan_ref="plan-1"),)
            return result

        svc.run_workflow = _with_variants  # type: ignore[method-assign]
        rt = HarnessRuntime()
        adapter = LegacyHarnessAdapter(svc, rt)
        rid = adapter.invoke(_legacy_invocation())
        rec = rt.get_run(rid)
        assert rec.status == RunStatus.FAILED  # 不再卡死 RUNNING
        assert rec.error_code == "RECORD_FAILED"
        assert any(d.get("code") == "RECORD_FAILED" for d in rec.diagnostics)

    def test_editorial_archive_failure_gets_terminal(self):
        svc = _RecordingService()
        svc.plan_repository = _ExplodingRepo()
        rt = HarnessRuntime()
        adapter = LegacyHarnessAdapter(svc, rt)

        def _outcome_with_plan(inv: Any, cancel_requested: Any = None,
                               run_ctx: Any = None) -> Any:
            return SimpleNamespace(
                diagnostics=(), degraded=False, artifacts=(),
                plan=SimpleNamespace(plan_id="plan-9"),
            )

        adapter._build_editorial_outcome = _outcome_with_plan  # type: ignore[method-assign]
        compiled = SimpleNamespace(
            runnable=True, target="editorial", diagnostics=(),
            workflow_input=None, skill_input=None,
        )
        rid = adapter.invoke_compiled(compiled, _legacy_invocation(skill_id="topos.auto_caption"))
        rec = rt.get_run(rid)
        assert rec.status == RunStatus.FAILED
        assert rec.error_code == "RECORD_FAILED"


class TestAud22008EditorialAnalysesIsolation:
    def test_analyses_not_leaked_across_runs(self):
        from src.features.ai_harness.analysis_skills import collect_transcript_trace as _  # noqa: F401

        analysis = _analysis_with_router("qwen3_asr_demucs_v4")
        provider_calls: list[tuple[tuple[str, ...], str]] = []

        def provider(asset_ids: tuple[str, ...], kind: str) -> Any:
            provider_calls.append((asset_ids, kind))
            if len(provider_calls) == 1:
                return ((), {"a1": analysis})  # 第一次：segments + analyses
            return ((),)  # 第二次：只有 segments（无 analyses 二元组）

        svc = _RecordingService()
        rt = HarnessRuntime()
        adapter = LegacyHarnessAdapter(svc, rt)
        adapter.set_editorial_provider(provider)
        seen_segs: list[tuple] = []

        def _fake_build(inv: Any, cancel_requested: Any = None,
                        run_ctx: Any = None) -> Any:
            segs = adapter._fetch_evidence(  # noqa: SLF001 — 白盒验证 run 级隔离
                inv.scope.selected_asset_ids, "transcript", run_ctx,
            )
            seen_segs.append(segs)
            return SimpleNamespace(
                diagnostics=(), degraded=False, artifacts=(), plan=None,
            )

        adapter._build_editorial_outcome = _fake_build  # type: ignore[method-assign]
        compiled = SimpleNamespace(
            runnable=True, target="editorial", diagnostics=(),
            workflow_input=None, skill_input=None,
        )
        rid1 = adapter.invoke_compiled(compiled, _legacy_invocation(skill_id="topos.auto_caption"))
        rid2 = adapter.invoke_compiled(compiled, _legacy_invocation(skill_id="topos.auto_caption"))
        rec1 = rt.get_run(rid1)
        rec2 = rt.get_run(rid2)
        # run 1：analyses 存在 → 追踪记录
        assert any(d.get("code") == "TRANSCRIPT_ROUTE_TRACE" for d in rec1.diagnostics)
        # run 2：本次 provider 未返回 analyses → 如实跳过，不复用上一 run 数据
        assert not any(d.get("code") == "TRANSCRIPT_ROUTE_TRACE" for d in rec2.diagnostics)
        # adapter 不再持有跨 run 可变实例属性
        assert not hasattr(adapter, "_editorial_analyses")


# ================================================================
# AUD-22014：角色超时预算
# ================================================================


class _SlowClient:
    last_usage = None

    def complete(self, messages, *, json_mode=False, temperature=0.7, max_tokens=None):
        time.sleep(0.5)
        return '{"ok":true}'


class _FastClient:
    last_usage = None

    def complete(self, messages, *, json_mode=False, temperature=0.7, max_tokens=None):
        return '{"ok":true}'


class TestAud22014TimeoutBudget:
    def test_timeout_returns_visible_degradation_promptly(self):
        started = time.monotonic()
        resp = LlmRoleModelClient(_SlowClient()).generate(
            system="s", user="u", role="qa_reviewer", timeout_ms=80,
        )
        elapsed = time.monotonic() - started
        assert elapsed < 0.4  # 不等慢端点跑完
        assert resp.error == "role_timeout"
        assert resp.finish_reason == "timeout"
        assert resp.text == ""

    def test_call_within_budget_returns_text(self):
        resp = LlmRoleModelClient(_FastClient()).generate(
            system="s", user="u", role="qa_reviewer", timeout_ms=5000,
        )
        assert resp.text == '{"ok":true}'
        assert resp.error == ""


# ================================================================
# AUD-22017：HTTP transport 重定向拒绝
# ================================================================


class _RedirectHTTPServer(ThreadingHTTPServer):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.received_headers: dict[str, str] | None = None
        self.evil_hits = 0

    daemon_threads = True


class _RedirectHandler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:  # noqa: N802 — http.server 约定
        server: _RedirectHTTPServer = self.server  # type: ignore[assignment]
        if self.path.rstrip("/").endswith("/mcp"):
            self.send_response(302)
            self.send_header(
                "Location", f"http://127.0.0.1:{server.server_port}/evil",
            )
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        server.evil_hits += 1
        server.received_headers = {
            key.lower(): value for key, value in self.headers.items()
        }
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "result": {}}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: Any) -> None:
        pass


class TestAud22017RedirectCredentials:
    def test_redirect_refused_and_credentials_not_forwarded(self):
        server = _RedirectHTTPServer(("127.0.0.1", 0), _RedirectHandler)
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            transport = HttpMcpTransport(
                f"http://127.0.0.1:{port}", api_key="secret-bearer-key",
                request_timeout=5.0, startup_timeout=5.0,
            )
            with pytest.raises(McpHttpTransportError) as excinfo:
                transport.send_request({
                    "jsonrpc": "2.0", "id": 1,
                    "method": "tools/list", "params": {},
                })
            assert "重定向" in str(excinfo.value)
            # 重定向目标从未被请求——更不可能收到 Authorization 头
            assert server.evil_hits == 0
            assert server.received_headers is None
        finally:
            server.shutdown()
            server.server_close()


# ================================================================
# AUD-22021：discovery 失败 fail-closed
# ================================================================


class _CrashTransport:
    def send_request(self, req):
        raise RuntimeError("server crashed")

    def close(self):
        pass


def _profile(**kw: Any) -> McpConnectionProfile:
    defaults = dict(
        profile_id="srv", server_name="test-mcp", transport="local_stdio",
        data_scope="read_only", enabled_by_default=True,
    )
    defaults.update(kw)
    return McpConnectionProfile(**defaults)


class TestAud22021DiscoveryFailClosed:
    def test_discovery_failure_denies_even_with_write_approval(self):
        reg = McpServerRegistry()
        reg.register(_profile())
        client = McpOutboundClient(reg, transport_factory=lambda p: _CrashTransport())
        r = client.call_tool("srv", "get_x", {}, write_approved=True)
        assert r["ok"] is False
        assert r["decision"] == "error_discovery_failed"
        # 授权审计记录该拒绝（按写处理，fail-closed 与 AUD-22020 语义一致）
        events = client.auth_log.events()
        assert events[-1].decision == "error"
        assert events[-1].write_operation is True
        client.close()


# ================================================================
# AUD-22101：不可信 JSON Schema 复杂度上限
# ================================================================


class TestAud22101SchemaComplexity:
    def test_variant_count_capped(self):
        schema = {"anyOf": [{"type": "object"} for _ in range(65)]}
        err = validate_json_schema({}, schema)
        assert err is not None and "variants" in err

    def test_combinatorial_explosion_rejected_quickly(self):
        schema: Any = {"type": "object"}
        for _ in range(8):  # 8^8 = 16,777,216 次分支求值（无预算时）
            schema = {"allOf": [schema for _ in range(8)]}
        started = time.monotonic()
        err = validate_json_schema({}, schema)
        elapsed = time.monotonic() - started
        assert err is not None
        assert "budget" in err or "variants" in err
        assert elapsed < 5.0

    def test_overlong_pattern_rejected(self):
        schema = {"type": "string", "pattern": "a" * 257}
        err = validate_json_schema("x", schema)
        assert err is not None and "pattern" in err and "too long" in err

    def test_normal_schemas_still_validate(self):
        schema = {
            "type": "object", "required": ["query"],
            "properties": {"query": {"type": "string"}},
        }
        assert validate_json_schema({"query": "x"}, schema) is None
        assert validate_json_schema({}, schema) is not None


# ================================================================
# AUD-22102 / 22103 / 22104：stdio transport
# ================================================================


class TestAud22102QueueGenerationIsolation:
    def test_old_drain_thread_sentinel_lands_in_old_queue_only(self):
        transport = SubprocessStdioTransport(("true",))
        old_q: queue.Queue = queue.Queue()
        new_q: queue.Queue = queue.Queue()
        transport._stdout_queue = new_q  # 模拟 close→start 已换代
        # 旧线程惊醒（proc 已被 close 置 None）→ finally 哨兵
        transport._drain_stdout(None, old_q)
        assert old_q.get_nowait() is None
        assert new_q.empty()

    def test_restart_after_close_still_answers(self):
        script = (
            "import json, sys\n"
            "for line in sys.stdin:\n"
            "    line = line.strip()\n"
            "    if not line:\n"
            "        continue\n"
            "    try:\n"
            "        msg = json.loads(line)\n"
            "    except Exception:\n"
            "        continue\n"
            "    out = {'jsonrpc': '2.0', 'id': msg.get('id'),\n"
            "           'result': {'ok': True}}\n"
            "    sys.stdout.write(json.dumps(out) + '\\n')\n"
            "    sys.stdout.flush()\n"
        )
        transport = SubprocessStdioTransport(
            (sys.executable, "-u", "-c", script),
            request_timeout=8.0, startup_timeout=8.0,
        )
        try:
            resp = transport.send_request({
                "jsonrpc": "2.0", "method": "server/discover", "params": {},
            })
            assert resp["result"] == {"ok": True}
            transport.close()
            # 给旧泄放线程投递 EOF 哨兵的窗口（旧行为会污染重启后的新队列）
            time.sleep(0.2)
            resp2 = transport.send_request({
                "jsonrpc": "2.0", "method": "server/discover", "params": {},
            })
            assert resp2["result"] == {"ok": True}
        finally:
            transport.close()


class TestAud22103CloseDoesNotWaitForRequestLock:
    def test_close_returns_while_request_lock_is_held(self):
        transport = SubprocessStdioTransport(("true",))
        release = threading.Event()

        def hold_lock() -> None:
            with transport._lock:  # noqa: SLF001 — 模拟在途请求持锁等待响应
                release.wait(3.0)

        holder = threading.Thread(target=hold_lock, daemon=True)
        holder.start()
        time.sleep(0.1)
        started = time.monotonic()
        transport.close()  # 绝不排队等 _lock（旧行为最长阻塞 request_timeout）
        elapsed = time.monotonic() - started
        release.set()
        holder.join(timeout=3.0)
        assert elapsed < 1.0
        assert transport._started is False


class TestAud22104BoundedStdoutQueue:
    def test_queue_is_bounded_with_drop_oldest(self):
        transport = SubprocessStdioTransport(("true",))
        out_q = transport._stdout_queue
        assert out_q.maxsize == _STDOUT_QUEUE_MAX
        for i in range(_STDOUT_QUEUE_MAX):
            transport._put_stdout(out_q, {"id": i})
        # 满队后入队哨兵：丢最旧，哨兵必须入队（_roundtrip 依赖它判退出）
        transport._put_stdout(out_q, None)
        assert out_q.qsize() == _STDOUT_QUEUE_MAX
        assert transport._dropped_stdout_messages == 1
        drained: list[Any] = []
        while True:
            try:
                drained.append(out_q.get_nowait())
            except queue.Empty:
                break
        assert drained[-1] is None
        assert drained[0] == {"id": 1}  # 最旧的 {"id": 0} 被丢弃


# ================================================================
# AUD-22105：进程隔离插件 invoke 看门狗
# ================================================================


class _StuckPlugin:
    """卡死插件：invoke 永不回包（模拟阻塞调用）。"""

    def activate(self, context: Any) -> None:
        return None

    def health(self) -> Any:
        from src.features.ai_harness.plugin_runtime import PluginHealth
        return PluginHealth(status="ok", message="")

    def invoke(self, tool_name: str, args: Any, *, approval: Any = None,
               context: Any = None) -> dict[str, Any]:
        time.sleep(60)
        return {}


def _stuck_plugin_factory() -> _StuckPlugin:
    return _StuckPlugin()


class TestAud22105InvokeWatchdog:
    def test_watchdog_budget_resolution(self):
        from src.features.ai_harness.plugin_isolation import ProcessIsolatedPlugin
        from src.features.ai_harness.plugin_runtime import PluginExecutionPolicy

        adapter = ProcessIsolatedPlugin(_stuck_plugin_factory, invoke_timeout_seconds=7.0)
        assert adapter._watchdog_budget_seconds() == 7.0
        adapter._policy = PluginExecutionPolicy(timeout_seconds=2.0)
        assert adapter._watchdog_budget_seconds() == 2.0
        unlimited = ProcessIsolatedPlugin(_stuck_plugin_factory, invoke_timeout_seconds=None)
        assert unlimited._watchdog_budget_seconds() is None

    def test_invalid_invoke_timeout_rejected(self):
        from src.features.ai_harness.plugin_isolation import ProcessIsolatedPlugin

        with pytest.raises(ValueError):
            ProcessIsolatedPlugin(_stuck_plugin_factory, invoke_timeout_seconds=0)
        with pytest.raises(ValueError):
            ProcessIsolatedPlugin(_stuck_plugin_factory, invoke_timeout_seconds=-1.0)

    def test_stuck_child_times_out_and_lock_is_released(self):
        from src.features.ai_harness.plugin_isolation import ProcessIsolatedPlugin
        from src.features.ai_harness.plugin_runtime import (
            PluginContext,
            PluginExecutionPolicy,
            PluginInvocationTimedOut,
        )

        adapter = ProcessIsolatedPlugin(
            _stuck_plugin_factory, invoke_timeout_seconds=1.0,
        )
        adapter.activate(PluginContext(
            plugin_id="stuck-test",
            policy=PluginExecutionPolicy(),
        ))
        started = time.monotonic()
        # context=None + policy 无超时：旧实现会永久自旋并占住实例锁
        with pytest.raises(PluginInvocationTimedOut):
            adapter.invoke("tool", {})
        elapsed = time.monotonic() - started
        assert elapsed < 10.0
        # 看门狗终止子进程后锁已释放：health()（持同一把锁）必须立即返回，
        # 而不是陪跑 60s 的卡死调用
        health_started = time.monotonic()
        health = adapter.health()
        assert time.monotonic() - health_started < 5.0
        assert health is not None
        adapter.deactivate()
