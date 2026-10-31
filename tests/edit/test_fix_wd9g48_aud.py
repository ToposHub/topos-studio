"""Regression tests for remediation batch D (report wd9-g48).

Covers the Major entries fixed in this wave:

- AUD-13501 execution_policy: restricted_auto tier cannot cancel the
  AUTO_PUBLISH independent consent boundary.
- AUD-13502 generated_media_materializer: malformed/zero ``Content-Length``
  must not escape the download error boundary (ZeroDivisionError).
- AUD-13600 generation_provider_pack: non-numeric external payload fields
  return ``GEN_PROVIDER_SCHEMA_INVALID`` instead of raising ValueError.
- AUD-13601 http_generation_provider: malformed ``Content-Length`` must not
  raise an untyped ``ValueError`` out of ``generate``.
- AUD-13602 http_generation_provider: per-request remote job id mapping.
- AUD-13603 goal_supervisor: ``run`` folds ``GoalRevisionConflict`` into a
  stale ``GoalRunResult`` instead of raising.
- AUD-13701 jobs: ``JobRegistry`` event bookkeeping is thread-safe.
- AUD-13702 jobs: ``JobWorkerPool`` reclaims completed futures.
- AUD-13708 llm_client: ``OpenAICompatClient.complete`` never returns None.
- AUD-13709 llm_client: Responses client omits ``max_output_tokens`` when
  ``max_tokens`` is None (backend default, not 1024).
- AUD-13801 llm_variant_planner: beat allocation keeps positive durations and
  raises ``LLMPlannerError`` for non-positive totals.
- AUD-13820 mcp_local_ipc: dispatcher timeout marks the envelope abandoned so
  the queued request is skipped on the GUI thread.
- AUD-13850 mcp_vision_gateway: ``_ensure_client`` is lock-protected and
  refuses to rebuild after ``shutdown``.
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass
from typing import Any

import pytest

from src.features.edit.ai_editing.execution_policy import (
    ActionRequest,
    ExecutionAction,
    ExecutionPolicy,
    ExecutionScope,
)
from src.features.edit.ai_editing.generated_media_materializer import (
    GeneratedMediaMaterializer,
)
from src.features.edit.ai_editing.generation_models import (
    GeneratedAsset,
    GenerationConsent,
    GenerationModality,
    GenerationProvenance,
    GenerationRequest,
)
from src.features.edit.ai_editing.generation_provider_pack import _normalize_result
from src.features.edit.ai_editing.goal_supervisor import (
    GoalCycleResult,
    GoalLimits,
    GoalSpec,
    GoalStatus,
    GoalSupervisor,
    InMemoryGoalCheckpointStore,
)
from src.features.edit.ai_editing.http_generation_provider import (
    HttpGenerationProvider,
)
from src.features.edit.ai_editing.jobs import JobKind, JobRegistry, JobWorkerPool
from src.features.edit.ai_editing.llm_client import (
    LLMError,
    OpenAICompatClient,
    OpenAIResponsesClient,
)
from src.features.edit.ai_editing.llm_variant_planner import (
    LLMPlannerError,
    _allocate_beat_ranges,
)
from src.features.edit.ai_editing.mcp_vision_gateway import McpVisionGateway
from src.shared.ai_models.generation.provider_profiles import (
    AiProviderProfile,
    FakeCredentialStore,
)


# ======================================================================
# AUD-13501: restricted_auto 不得取消 AUTO_PUBLISH 的独立同意边界
# ======================================================================


def _restricted_auto_policy() -> ExecutionPolicy:
    return ExecutionPolicy.from_permission_tier(
        "restricted_auto", ExecutionScope(project_id="proj-1"),
    )


def test_restricted_auto_publish_requires_explicit_consent() -> None:
    policy = _restricted_auto_policy()
    decision = policy.evaluate(
        ActionRequest(action=ExecutionAction.AUTO_PUBLISH, project_id="proj-1"),
        approval_granted=False,
    )
    assert decision.allowed is False
    assert decision.needs_approval is True


def test_restricted_auto_publish_allows_with_explicit_consent() -> None:
    policy = _restricted_auto_policy()
    decision = policy.evaluate(
        ActionRequest(action=ExecutionAction.AUTO_PUBLISH, project_id="proj-1"),
        approval_granted=True,
    )
    assert decision.allowed is True


def test_restricted_auto_generic_write_stays_auto() -> None:
    """Tier 放松仅作用于普通写操作（LOCAL_EDIT 仍免逐次审批）。"""
    policy = _restricted_auto_policy()
    decision = policy.evaluate(
        ActionRequest(action=ExecutionAction.LOCAL_EDIT, project_id="proj-1"),
        approval_granted=False,
    )
    assert decision.allowed is True


# ======================================================================
# AUD-13502: Content-Length: 0 / 非数字头不得让 ZeroDivisionError 逃逸
# ======================================================================


class _FakeResponse:
    def __init__(self, payload: bytes, headers: dict[str, str]) -> None:
        self._payload = payload
        self.headers = headers

    def read(self, size: int = -1) -> bytes:
        if not self._payload:
            return b""
        chunk, self._payload = self._payload[:size], self._payload[size:]
        return chunk

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *_args: Any) -> None:
        return None


def _media_asset() -> GeneratedAsset:
    return GeneratedAsset(
        asset_id="gen:test/one", request_id="req-1",
        modality=GenerationModality.VIDEO,
        provenance=GenerationProvenance(provider_id="test", model_id="m1"),
        preview_handle="https://provider.example/out/video.mp4",
    )


def test_zero_content_length_header_does_not_divide_by_zero(tmp_path: Any) -> None:
    """服务器声明 Content-Length: 0 但仍发送 body：不得抛 ZeroDivisionError。"""
    def opener(_request: Any, *, timeout: float) -> _FakeResponse:
        return _FakeResponse(b"media-bytes", {"Content-Length": "0"})

    seen: list[float] = []

    def progress(value: float, _message: str) -> None:
        seen.append(value)

    result = GeneratedMediaMaterializer(
        cache_dir=tmp_path / "cache", opener=opener,
    ).materialize_with_progress(_media_asset(), progress=progress)

    assert result.succeeded
    assert seen == [1.0]  # 下载期进度被跳过（无安全分母），只在完成时报 1.0


def test_malformed_content_length_header_is_ignored(tmp_path: Any) -> None:
    def opener(_request: Any, *, timeout: float) -> _FakeResponse:
        return _FakeResponse(b"media-bytes", {"Content-Length": "12x3"})

    result = GeneratedMediaMaterializer(
        cache_dir=tmp_path / "cache2", opener=opener,
    ).materialize(_media_asset())

    assert result.succeeded  # 非数字头视为未知长度，由实读上限兜底


# ======================================================================
# AUD-13600: 外部 payload 数值字段安全转换
# ======================================================================


def _pack_request(request_id: str = "req-1") -> GenerationRequest:
    return GenerationRequest(
        request_id=request_id,
        modality=GenerationModality.VIDEO,
        prompt="产品发布会特写",
        consent=GenerationConsent(
            confirmed=True,
            provider_id="seedance",
            cost_acknowledged=True,
            upload_acknowledged=True,
            copyright_acknowledged=True,
        ),
    )


def test_normalize_result_non_numeric_duration_is_schema_invalid() -> None:
    result = _normalize_result(
        "seedance", "video-v1", _pack_request(),
        {"outputs": [{"url": "https://cdn.example/a.mp4", "duration_seconds": "unknown"}]},
    )
    assert result is not None
    assert result.error_code == "GEN_PROVIDER_SCHEMA_INVALID"


def test_normalize_result_non_numeric_cost_is_schema_invalid() -> None:
    result = _normalize_result(
        "seedance", "video-v1", _pack_request(),
        {"outputs": [{"url": "https://cdn.example/a.mp4", "cost_units": "0.5USD"}]},
    )
    assert result is not None
    assert result.error_code == "GEN_PROVIDER_SCHEMA_INVALID"


def test_normalize_result_non_numeric_actual_cost_is_schema_invalid() -> None:
    result = _normalize_result(
        "seedance", "video-v1", _pack_request(),
        {
            "outputs": [{"url": "https://cdn.example/a.mp4"}],
            "actual_cost_units": "abc",
        },
    )
    assert result is not None
    assert result.error_code == "GEN_PROVIDER_SCHEMA_INVALID"


def test_normalize_result_numeric_strings_still_parse() -> None:
    result = _normalize_result(
        "seedance", "video-v1", _pack_request(),
        {
            "outputs": [{
                "url": "https://cdn.example/a.mp4",
                "cost_units": "0.5",
                "duration_seconds": "2.5",
            }],
            "actual_cost_units": "1.25",
        },
    )
    assert result is not None and result.succeeded
    assert result.candidates[0].cost_units == pytest.approx(0.5)
    assert result.candidates[0].duration_seconds == pytest.approx(2.5)
    assert result.actual_cost_units == pytest.approx(1.25)


# ======================================================================
# AUD-13601 / AUD-13602: HTTP generation adapter
# ======================================================================


class _ScriptedResponse:
    def __init__(self, payload: dict[str, Any], length_header: str) -> None:
        self._raw = json.dumps(payload).encode("utf-8")
        self.headers = {"Content-Length": length_header}

    def read(self, limit: int = -1) -> bytes:
        return self._raw

    def __enter__(self) -> "_ScriptedResponse":
        return self

    def __exit__(self, *_args: Any) -> None:
        return None


class _ScriptedOpener:
    def __init__(self, *responses: _ScriptedResponse) -> None:
        self.responses = list(responses)

    def __call__(self, request: Any, *, timeout: float) -> _ScriptedResponse:
        return self.responses.pop(0)


def _http_profile() -> AiProviderProfile:
    return AiProviderProfile(
        profile_id="remote-video",
        display_name="Remote Video",
        provider_kind="generation_http",
        endpoint="https://provider.example",
        model_id="video-v1",
        credential_ref="topos:remote-video",
        modalities=("video",),
    )


def _http_provider(opener: _ScriptedOpener) -> HttpGenerationProvider:
    credentials = FakeCredentialStore()
    credentials._store["topos:remote-video"] = "secret-token"
    return HttpGenerationProvider(
        _http_profile(), credentials,
        opener=opener, poll_interval_seconds=0.0,
    )


def test_malformed_content_length_returns_typed_result_not_value_error() -> None:
    opener = _ScriptedOpener(
        _ScriptedResponse(
            {"outputs": [{"url": "https://cdn.example/a.mp4"}], "status": "succeeded"},
            length_header="12x3",  # 非法头：旧代码在这里抛 ValueError
        )
    )
    result = _http_provider(opener).generate(_pack_request())
    assert result.succeeded  # 非法头被忽略，靠实读上限兜底；绝不裸抛 ValueError


def test_remote_job_id_is_tracked_per_request() -> None:
    opener = _ScriptedOpener(
        _ScriptedResponse({"job_id": "job-1", "status": "queued"}, "2"),
        _ScriptedResponse(
            {"outputs": [{"url": "https://cdn.example/a.mp4"}], "status": "succeeded"}, "2",
        ),
        _ScriptedResponse({"job_id": "job-2", "status": "queued"}, "2"),
        _ScriptedResponse(
            {"outputs": [{"url": "https://cdn.example/b.mp4"}], "status": "succeeded"}, "2",
        ),
    )
    provider = _http_provider(opener)
    assert provider.generate(_pack_request("req-1")).succeeded
    assert provider.generate(_pack_request("req-2")).succeeded

    # 逐请求通道无歧义：并发共享 worker 下取消不会指向错误任务。
    assert provider.remote_job_id("req-1") == "job-1"
    assert provider.remote_job_id("req-2") == "job-2"
    assert provider.remote_job_id("req-missing") == ""
    # 兼容属性仍指向最近一次提交。
    assert provider.last_remote_job_id == "job-2"


# ======================================================================
# AUD-13603: run() 折叠 GoalRevisionConflict
# ======================================================================


@dataclass
class _SingleCycleRunner:
    def run_cycle(self, _goal: Any, _checkpoint: Any) -> GoalCycleResult:
        return GoalCycleResult(completed=True)


@dataclass
class _RetryableCycleRunner:
    def run_cycle(self, _goal: Any, _checkpoint: Any) -> GoalCycleResult:
        return GoalCycleResult(retryable=True)


class _AlwaysConflictingStore(InMemoryGoalCheckpointStore):
    """compare_and_save 永远失败：模拟并发修订（GoalRevisionConflict）。"""

    def compare_and_save(
        self, goal_id: str, expected_revision: int, checkpoint: Any,
    ) -> bool:
        return False


def _goal(**overrides: Any) -> GoalSpec:
    values: dict[str, Any] = {
        "goal_id": "goal-1",
        "project_id": "project-1",
        "timeline_id": "timeline-1",
        "objective_ref": "skill:product-ad",
    }
    values.update(overrides)
    return GoalSpec(**values)


def test_run_scope_change_conflict_returns_stale_result() -> None:
    seed = InMemoryGoalCheckpointStore()
    GoalSupervisor(
        _SingleCycleRunner(), limits=GoalLimits(max_cycles=1), checkpoints=seed,
    ).run(_goal())

    conflicting = _AlwaysConflictingStore()
    conflicting._items.update(seed._items)
    result = GoalSupervisor(
        _SingleCycleRunner(), limits=GoalLimits(max_cycles=1), checkpoints=conflicting,
    ).run(_goal(timeline_id="other-timeline"), resume=True)

    # 不得抛 GoalRevisionConflict；返回可展示的 stale GoalRunResult。
    assert result.status == GoalStatus.SUCCEEDED  # store 里仍是旧 checkpoint
    assert "stale goal revision" in result.diagnostics[0]


def test_run_finish_conflict_returns_stale_result() -> None:
    # 种子 checkpoint 保持 RUNNING（非终态），让 run() 一路走到 _finish。
    seed = InMemoryGoalCheckpointStore()
    GoalSupervisor(
        _RetryableCycleRunner(), limits=GoalLimits(max_cycles=3), checkpoints=seed,
    ).run(_goal())

    conflicting = _AlwaysConflictingStore()
    conflicting._items.update(seed._items)
    result = GoalSupervisor(
        _SingleCycleRunner(), limits=GoalLimits(max_cycles=3), checkpoints=conflicting,
    ).run(_goal(), resume=True)

    assert isinstance(result.status, GoalStatus)
    assert "stale goal revision" in result.diagnostics[0]


# ======================================================================
# AUD-13701 / AUD-13702: jobs 并发与 Future 回收
# ======================================================================


def test_registry_event_sequences_are_unique_under_concurrency() -> None:
    registry = JobRegistry()
    handles = [registry.submit(JobKind.PREVIEW, f"job-{i}") for i in range(4)]
    errors: list[BaseException] = []

    def hammer(handle: Any) -> None:
        try:
            for _ in range(75):
                handle.update_progress(0.5, "tick")
        except BaseException as exc:  # noqa: BLE001 — 记录线程内失败
            errors.append(exc)

    threads = [threading.Thread(target=hammer, args=(h,)) for h in handles]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert errors == []
    events = registry.events_since(0)
    sequences = [event.sequence for event in events]
    assert len(sequences) == 4 * 75 + 4  # 75 progress + queued per job
    assert len(set(sequences)) == len(sequences)  # 无重复/跳号
    assert sequences == sorted(sequences)


def test_worker_pool_reclaims_done_futures() -> None:
    registry = JobRegistry()
    pool = JobWorkerPool(registry)
    try:
        first = registry.submit(JobKind.EXPORT, "first")
        future_first = pool.submit(first, lambda job: {"done": True})
        assert future_first.result(timeout=5) == {"done": True}

        second = registry.submit(JobKind.EXPORT, "second")
        pool.submit(second, lambda job: {"done": True})

        # 下一次 submit 时已完成的 Future（含其 result 返回值）被回收。
        assert pool.future(first.job_id) is None
        assert pool.future(second.job_id) is not None
        assert pool.cleanup_done() >= 0
    finally:
        pool.shutdown()


# ======================================================================
# AUD-13708 / AUD-13709: llm_client
# ======================================================================


def _llm_profile() -> AiProviderProfile:
    return AiProviderProfile(
        profile_id="llm-test",
        display_name="LLM Test",
        provider_kind="openai",
        endpoint="https://api.example",
        model_id="test-model",
    )


def test_openai_compat_null_content_raises_llm_error(monkeypatch: pytest.MonkeyPatch) -> None:
    from src.features.edit.ai_editing import llm_client as llm_module

    def fake_post_json(_url: str, _headers: Any, _body: Any, *, policy: Any) -> dict[str, Any]:
        return {
            "choices": [{
                "message": {"content": None},
                "finish_reason": "content_filter",
            }],
        }

    monkeypatch.setattr(llm_module, "_post_json", fake_post_json)
    client = OpenAICompatClient(_llm_profile(), "key")
    with pytest.raises(LLMError) as excinfo:
        client.complete([{"role": "user", "content": "hi"}])
    assert excinfo.value.code == "LLM_INVALID_RESPONSE"


def test_openai_compat_complete_returns_str(monkeypatch: pytest.MonkeyPatch) -> None:
    from src.features.edit.ai_editing import llm_client as llm_module

    def fake_post_json(_url: str, _headers: Any, _body: Any, *, policy: Any) -> dict[str, Any]:
        return {
            "choices": [{
                "message": {"content": "hello"},
                "finish_reason": "stop",
            }],
        }

    monkeypatch.setattr(llm_module, "_post_json", fake_post_json)
    client = OpenAICompatClient(_llm_profile(), "key")
    text = client.complete([{"role": "user", "content": "hi"}])
    assert isinstance(text, str)
    assert text == "hello"


def test_responses_client_omits_max_output_tokens_when_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.features.edit.ai_editing import llm_client as llm_module

    captured: dict[str, Any] = {}

    def fake_post_json(_url: str, _headers: Any, body: Any, *, policy: Any) -> dict[str, Any]:
        captured["body"] = body
        return {
            "output": [{"content": [{"type": "output_text", "text": "ok"}]}],
            "usage": {"input_tokens": 1, "output_tokens": 1},
        }

    monkeypatch.setattr(llm_module, "_post_json", fake_post_json)
    client = OpenAIResponsesClient(_llm_profile(), "key")

    text = client.complete([{"role": "user", "content": "hi"}], max_tokens=None)
    assert text == "ok"
    assert "max_output_tokens" not in captured["body"]  # None → 后端默认

    client.complete([{"role": "user", "content": "hi"}], max_tokens=512)
    assert captured["body"]["max_output_tokens"] == 512


# ======================================================================
# AUD-13801: beat 时长分配
# ======================================================================


def test_allocate_beat_ranges_short_total_keeps_positive_durations() -> None:
    ranges = _allocate_beat_ranges(("a", "b", "c"), 2)
    assert [duration for _beat, fr in ranges for duration in [fr.duration]] == [1, 1, 1]
    starts = [fr.start for _beat, fr in ranges]
    assert starts == [0, 1, 2]


def test_allocate_beat_ranges_normal_total_distributes_remainder() -> None:
    ranges = _allocate_beat_ranges(("a", "b", "c"), 90)
    durations = [fr.duration for _beat, fr in ranges]
    assert durations == [30, 30, 30]
    ranges = _allocate_beat_ranges(("a", "b", "c"), 91)
    durations = [fr.duration for _beat, fr in ranges]
    assert durations == [30, 30, 31]


def test_allocate_beat_ranges_non_positive_total_raises_planner_error() -> None:
    with pytest.raises(LLMPlannerError):
        _allocate_beat_ranges(("a", "b", "c"), 0)
    with pytest.raises(LLMPlannerError):
        _allocate_beat_ranges(("a", "b", "c"), -5)


# ======================================================================
# AUD-13820: dispatcher 超时必须等价于取消
# ======================================================================


class _RecordingServer:
    """最小 McpServer 替身：记录 handle_request 调用。"""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def handle_request(self, session: Any, request: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(request)
        return {"jsonrpc": "2.0", "id": request.get("id"), "result": {}}


class _TestDispatcherBridge:
    """直接构造 QtMcpRequestDispatcher 的内部 bridge 行为（无 Qt 事件循环）。

    PyQt6 缺失时（如最小 CI 环境）跳过：本用例验证的 abandoned 契约
    依赖真实的 _Bridge/_handle 代码路径。
    """

    def __init__(self) -> None:
        pytest.importorskip("PyQt6")
        from src.features.edit.ai_editing.mcp_local_ipc import QtMcpRequestDispatcher

        self.server = _RecordingServer()
        self.dispatcher = QtMcpRequestDispatcher(self.server, timeout_seconds=1.0)


def _make_envelope(abandoned: bool) -> dict[str, Any]:
    return {
        "session": None,
        "request": {"jsonrpc": "2.0", "id": 7, "method": "tools/call"},
        "event": threading.Event(),
        "abandoned": abandoned,
    }


def test_dispatcher_handle_skips_abandoned_envelope() -> None:
    bridge = _TestDispatcherBridge()
    envelope = _make_envelope(abandoned=True)
    bridge.dispatcher._bridge._handle(envelope)
    assert bridge.server.calls == []          # 超时后的排队请求不再执行
    assert envelope["event"].is_set()         # 但事件必须释放，避免句柄悬挂


def test_dispatcher_handle_executes_fresh_envelope() -> None:
    bridge = _TestDispatcherBridge()
    envelope = _make_envelope(abandoned=False)
    bridge.dispatcher._bridge._handle(envelope)
    assert len(bridge.server.calls) == 1
    assert envelope["event"].is_set()
    assert envelope["response"]["id"] == 7


def test_dispatcher_timeout_marks_envelope_abandoned() -> None:
    """端到端：emit 后无人处理 → 超时返回 -32603 且 envelope 标记 abandoned。"""
    bridge = _TestDispatcherBridge()
    captured: list[dict[str, Any]] = []
    # 直接连接（同线程）捕获 emit 出去的 envelope；QueuedConnection 的
    # _handle 不会在无事件循环时运行，dispatch 必然走到超时分支。
    bridge.dispatcher._bridge.requested.connect(captured.append)

    response = bridge.dispatcher.dispatch(None, {"jsonrpc": "2.0", "id": 9, "method": "ping"})

    assert response["error"]["code"] == -32603
    assert len(captured) == 1
    assert captured[0]["abandoned"] is True
    # 超时后 GUI 线程再处理该排队请求时必须跳过执行。
    bridge.dispatcher._bridge._handle(captured[0])
    assert bridge.server.calls == []


# ======================================================================
# AUD-13850: _ensure_client 持锁 + shutdown 后不再重建
# ======================================================================


def test_ensure_client_builds_exactly_one_client_under_concurrency() -> None:
    calls: list[str] = []
    lock = threading.Lock()

    def slow_key_provider() -> str:
        with lock:
            calls.append("key")
        time.sleep(0.05)  # 放大并发窗口：旧代码会重复进入构建路径
        return "Z_AI_API_KEY"

    gateway = McpVisionGateway(
        api_key_provider=slow_key_provider,
        npx_path="/fake/npx",
        transport_factory=lambda profile: None,
    )
    results: list[Any] = [None] * 4

    def ensure(index: int) -> None:
        results[index] = gateway._ensure_client()

    threads = [threading.Thread(target=ensure, args=(i,)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    # 只构建一个 client；所有线程拿到同一实例（输家 client 的 npx 子进程
    # 不会再被泄漏）。
    assert calls == ["key"]
    assert all(r is results[0] for r in results)
    assert results[0] is not None


def test_ensure_client_refuses_to_rebuild_after_shutdown() -> None:
    gateway = McpVisionGateway(
        api_key_provider=lambda: "Z_AI_API_KEY",
        npx_path="/fake/npx",
        transport_factory=lambda profile: None,
    )
    first = gateway._ensure_client()
    assert first is not None
    gateway.shutdown()
    assert gateway._ensure_client() is None  # shutdown 后不再写回新 client
