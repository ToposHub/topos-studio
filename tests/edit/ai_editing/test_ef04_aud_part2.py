"""EF-04 子运行 2/2 修复验证（AUD-14117 … AUD-14918，18 条 Suggestion 级）。

每条审计发现至少一个正例（修复后的正确行为）+ 一个回退敏感性检查（修复
回退时必然失败）。不依赖 GPU / Qt / 真实 AI 服务：

- 远程 TTS / ASR 全部用可控 fake + monkeypatch，绝不发起真实网络请求；
- 线程相关测试用 Barrier 同步 + 有界 sleep，绝不无限等待；
- TTS/ASR 模型加载路径全部被 fake 替换（不加载 3.8GB 权重）；
- 测试 gap 类（AUD-14600/14601/14603）同时做功能契约验证与测试文件
  自身的源码 introspection（旧缺陷形态出现即失败）。
"""

from __future__ import annotations

import ast
import dataclasses
import importlib.util
import inspect
import logging
import threading
import time
from collections import deque
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from src.features.edit.ai_editing import subtitle_draft
from src.features.edit.ai_editing import toposctl
from src.features.edit.ai_editing.assistant_events import AssistantState
from src.features.edit.ai_editing.assistant_service import AssistantService
from src.features.edit.ai_editing.approval_service import ApprovalService
from src.features.edit.ai_editing.edit_project_service import ProjectSequences
from src.features.edit.ai_editing.models import (
    EditOperation,
    EditPlan,
    OperationKind,
    PlanTarget,
)
from src.features.edit.ai_editing.plan_compiler import COMPILABLE_OPERATIONS
from src.features.edit.ai_editing.profile_tts_provider import ProfileAwareTtsProvider
from src.features.edit.ai_editing.provider_plugin import (
    AnalysisPluginHost,
    ProviderDescriptor,
)
from src.features.edit.ai_editing.release_evidence import TelemetryCollector
from src.features.edit.ai_editing.runtime_capability_snapshot import (
    CapabilitySnapshotStore,
    RuntimeCapabilityEntry,
    RuntimeCapabilitySnapshot,
    RuntimeCapabilityState,
)
from src.features.edit.ai_editing.schema import validate_plan_structure
from src.features.edit.ai_editing.timebase import TimebaseService
from src.features.edit.ai_editing.tool_call_log import ToolCallLog, ToolCallRecord
from src.features.edit.ai_editing.tool_result import ToolResult
from src.features.edit.ai_editing.transcript_provider import QwenASRProvider
from src.features.edit.ai_editing.transcript_router import ProfileAwareTranscriptProvider
from src.features.edit.ai_editing.transaction_service import (
    TransactionService,
    _expected_commit_scope,
)
from src.features.edit.ai_editing.tts_provider import Qwen3TTSProvider
from src.features.edit.ai_editing.workflow import WorkflowId, WorkflowInput
from src.shared.media.metadata import Rational

pytestmark = pytest.mark.edit_core

_REPO_ROOT = Path(__file__).resolve().parents[3]
_CANCEL_TEST_PATH = (
    _REPO_ROOT / "src/features/edit/ai_editing/tests/test_assistant_service_cancel.py"
)


def _is_lock(obj: Any) -> bool:
    """threading.Lock() 是工厂函数，isinstance 必须用其返回类型。"""
    return isinstance(obj, type(threading.Lock()))


def _load_cancel_test_source() -> str:
    return _CANCEL_TEST_PATH.read_text(encoding="utf-8")


# ---------------------------------------------------------------------- #
# AUD-14117：远程合成逐 cue 同步 generate 无超时 → 有界等待
# ---------------------------------------------------------------------- #


class _FakeRemoteTtsProvider:
    """可控的远程 TTS provider（不发起任何网络请求）。"""

    provider_id = "remote-tts-fake"

    def __init__(self, *, hang_seconds: float = 0.0) -> None:
        self.hang_seconds = hang_seconds
        self.calls = 0

    def generate(self, request: Any) -> Any:
        self.calls += 1
        if self.hang_seconds > 0:
            time.sleep(self.hang_seconds)
        return SimpleNamespace(
            error_code="",
            error_message="",
            candidates=[SimpleNamespace(preview_handle="/tmp/fake-cue.mp3")],
        )


def _tts_host(provider: Any) -> tuple[ProfileAwareTtsProvider, Any]:
    """构造走 remote 路径的 ProfileAwareTtsProvider（registry/凭证全 fake）。"""
    profile = SimpleNamespace(
        profile_id="cloud-voice",
        display_name="Cloud Voice",
        model_id="voice-1",
        source_kind="cloud",
        provider_kind="http",
    )
    registry = SimpleNamespace(
        active_profile_for_capability=lambda cap: profile,
        list=lambda: [],
    )
    credential_store = SimpleNamespace(get_secret=lambda ref: "secret")
    host = ProfileAwareTtsProvider(
        local_provider=MagicMock(), registry=registry, credential_store=credential_store,
    )
    host._remote_by_profile["cloud-voice"] = provider
    return host, profile


def test_aud_14117_bounded_generate_returns_fast_result() -> None:
    """正例：provider.generate 正常返回时 _generate_bounded 原样透传。"""
    host, _profile = _tts_host(_FakeRemoteTtsProvider())
    request = SimpleNamespace(request_id="req-1")
    result = host._generate_bounded(_FakeRemoteTtsProvider(), request)
    assert result.error_code == ""
    assert result.candidates[0].preview_handle == "/tmp/fake-cue.mp3"


def test_aud_14117_bounded_generate_times_out_instead_of_hanging() -> None:
    """正例：远程挂起时在超时上限内抛 RuntimeError，不再无限期阻塞。"""
    host, _profile = _tts_host(_FakeRemoteTtsProvider())
    host.REMOTE_CUE_TIMEOUT_SECONDS = 0.3  # 实例级调小，测试快速返回
    hanging = _FakeRemoteTtsProvider(hang_seconds=5.0)
    started = time.monotonic()
    with pytest.raises(RuntimeError, match="远程 TTS"):
        host._generate_bounded(hanging, SimpleNamespace(request_id="req-2"))
    elapsed = time.monotonic() - started
    assert elapsed < 3.0, f"超时后必须尽快返回，实际耗时 {elapsed:.2f}s"


def test_aud_14117_synthesize_routes_each_cue_through_bounded_call(monkeypatch: pytest.MonkeyPatch) -> None:
    """正例：synthesize 的逐 cue 循环经由 _generate_bounded（每 cue 一次）。

    回退敏感性：monkeypatch(raising=True) 在修复回退（无 _generate_bounded）
    时直接报 AttributeError → 测试失败，且不会进入旧的无限阻塞路径。
    """
    fake_remote = _FakeRemoteTtsProvider()
    host, _profile = _tts_host(fake_remote)
    bounded_calls: list[int] = []

    def _recording_bounded(provider: Any, request: Any) -> Any:
        bounded_calls.append(1)
        return SimpleNamespace(
            error_code="",
            error_message="",
            candidates=[SimpleNamespace(preview_handle=f"/tmp/cue-{len(bounded_calls)}.mp3")],
        )

    monkeypatch.setattr(host, "_generate_bounded", _recording_bounded, raising=True)
    # ffmpeg 解码与 WAV 落盘均 fake 掉（不真正起子进程/写盘）。
    monkeypatch.setattr(
        ProfileAwareTtsProvider, "_decode_audio",
        staticmethod(lambda path: ("wave-bytes", 48000)),
    )
    monkeypatch.setattr(
        ProfileAwareTtsProvider, "_write_remote_wav",
        staticmethod(lambda waves, **kw: SimpleNamespace(path="out.wav")),
    )
    from src.features.edit.ai_editing.tts_provider import SpeechCue

    host.synthesize(
        [SpeechCue(text="第一句", start_frame=0), SpeechCue(text="第二句", start_frame=48)],
        fps=24.0,
    )
    assert len(bounded_calls) == 2, "每条 cue 都必须经过有界的 _generate_bounded"


# ---------------------------------------------------------------------- #
# AUD-14201：AnalysisPluginHost._bindings 无锁 → 加锁
# ---------------------------------------------------------------------- #


class _PluginProvider:
    def __init__(self, provider_id: str) -> None:
        self.provider_id = provider_id
        self.supported_kinds: tuple[Any, ...] = ()


def test_aud_14201_host_has_bindings_lock() -> None:
    """正例：宿主持有 _bindings_lock（与 PluginRegistry 同一保护级别）。"""
    from src.features.edit.ai_editing.analysis_service import AnalysisService

    host = AnalysisPluginHost(AnalysisService())
    assert _is_lock(host._bindings_lock), "AnalysisPluginHost 必须持有 _bindings 锁"


def test_aud_14201_concurrent_register_and_list_is_safe() -> None:
    """正例：并发 register 与 list_descriptors/descriptor 不抛竞态异常。"""
    from src.features.edit.ai_editing.analysis_service import AnalysisService

    host = AnalysisPluginHost(AnalysisService())
    errors: list[BaseException] = []

    def _register(index: int) -> None:
        try:
            provider = _PluginProvider(f"transcript.p{index}")
            descriptor = ProviderDescriptor(
                provider_id=provider.provider_id,
                display_name=f"plugin {index}",
                capabilities=("transcript",),
            )
            assert host.register(provider, descriptor)
        except BaseException as exc:  # noqa: BLE001 — 测试收集线程内异常
            errors.append(exc)

    stop = threading.Event()

    def _hammer() -> None:
        try:
            while not stop.is_set():
                host.list_descriptors()
                host.list_descriptors(active_only=True)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    hammer_thread = threading.Thread(target=_hammer, daemon=True)
    hammer_thread.start()
    workers = [threading.Thread(target=_register, args=(i,)) for i in range(8)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=10)
    stop.set()
    hammer_thread.join(timeout=5)

    assert errors == [], f"并发 register/list 不应产生异常: {errors[:3]}"
    assert len(host.list_descriptors()) == 8


# ---------------------------------------------------------------------- #
# AUD-14206：TelemetryCollector FIFO 淘汰 O(n) pop(0) → deque(maxlen)
# ---------------------------------------------------------------------- #


def test_aud_14206_events_fifo_eviction_correct_and_deque_backed() -> None:
    """正例：饱和后只保留最新 max_events 条，且底层是 deque（O(1) 淘汰）。"""
    collector = TelemetryCollector(opt_in=True, max_events=5)
    for index in range(100):
        collector.emit(f"event-{index}")
    events = collector.events()
    assert len(events) == 5
    assert events[0].name == "event-95", "最旧的 95 条应被淘汰"
    assert events[-1].name == "event-99"
    assert isinstance(collector._events, deque), (
        "AUD-14206：事件队列必须用 deque(maxlen=...) 承载（回退到 list.pop(0) 即失败）"
    )
    exported = collector.export()
    assert len(exported["events"]) == 5


# ---------------------------------------------------------------------- #
# AUD-14207：TelemetryCollector 无锁 → 加锁
# ---------------------------------------------------------------------- #


def test_aud_14207_collector_has_lock() -> None:
    """正例：collector 持有一把锁保护 _opt_in/_events。"""
    collector = TelemetryCollector(opt_in=True)
    assert hasattr(collector, "_lock") and _is_lock(collector._lock), (
        "AUD-14207：TelemetryCollector 必须持锁（回退到无锁实现即失败）"
    )


def test_aud_14207_concurrent_emit_export_optin_no_error() -> None:
    """正例：emit/export/set_opt_in 并发压测无异常、成功 emit 全部入账。"""
    collector = TelemetryCollector(opt_in=True, max_events=10_000)
    errors: list[BaseException] = []
    barrier = threading.Barrier(6)
    success_counter = [0]
    counter_lock = threading.Lock()

    def _emit_worker() -> None:
        try:
            barrier.wait(timeout=5)
            for index in range(200):
                if collector.emit(f"e-{threading.get_ident()}-{index}"):
                    with counter_lock:
                        success_counter[0] += 1
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    def _read_worker() -> None:
        try:
            barrier.wait(timeout=5)
            for _ in range(50):
                collector.events()
                collector.export()
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    def _optin_worker() -> None:
        try:
            barrier.wait(timeout=5)
            for _ in range(50):
                collector.set_opt_in(True)  # 恒为 True：不清空，只竞争锁
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    workers = (
        [threading.Thread(target=_emit_worker) for _ in range(3)]
        + [threading.Thread(target=_read_worker) for _ in range(2)]
        + [threading.Thread(target=_optin_worker)]
    )
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=15)

    assert errors == [], f"并发访问不应产生异常: {errors[:3]}"
    assert success_counter[0] == 600
    assert len(collector.events()) == 600


# ---------------------------------------------------------------------- #
# AUD-14314：CapabilitySnapshotStore 无锁 → 加锁 + 退订幂等
# ---------------------------------------------------------------------- #


def _snapshot(mark: str) -> RuntimeCapabilitySnapshot:
    entry = RuntimeCapabilityEntry(
        component_id=f"probe.{mark}", state=RuntimeCapabilityState.AVAILABLE,
    )
    return RuntimeCapabilitySnapshot(
        entries=(entry,), platform={"os": "test"}, git_revision="rev",
    )


def test_aud_14314_store_lock_and_idempotent_unsubscribe() -> None:
    """正例：store 持锁；subscribe 返回的退订函数可重复调用（幂等）。"""
    store = CapabilitySnapshotStore()
    assert hasattr(store, "_lock") and _is_lock(store._lock), (
        "AUD-14314：CapabilitySnapshotStore 必须持锁（回退到无锁实现即失败）"
    )

    notified: list[str] = []
    unsubscribe = store.subscribe(lambda snap: notified.append(snap.entries[0].component_id))
    assert store.set(_snapshot("a")) is True
    # 同一 snapshot（同 ID）再次 set → 无可见变化，不通知
    assert store.set(_snapshot("a")) is False
    assert notified == ["probe.a"]

    unsubscribe()
    unsubscribe()  # 幂等：旧 lambda 版本第二次 remove 会抛 ValueError
    assert store.set(_snapshot("b")) is True
    assert notified == ["probe.a"], "退订后不应再收到通知"


def test_aud_14314_failing_listener_does_not_block_update() -> None:
    """正例：单个监听者抛异常不影响其它监听者与状态更新（且留痕不静默）。"""
    store = CapabilitySnapshotStore()

    def _boom(_snap: RuntimeCapabilitySnapshot) -> None:
        raise RuntimeError("listener exploded")

    good: list[str] = []
    store.subscribe(_boom)
    store.subscribe(lambda snap: good.append(snap.entries[0].component_id))
    assert store.set(_snapshot("x")) is True
    assert good == ["probe.x"]


def test_aud_14314_concurrent_set_subscribe_unsubscribe_no_error() -> None:
    """正例：set/subscribe/退订并发无竞态异常。"""
    store = CapabilitySnapshotStore()
    errors: list[BaseException] = []
    barrier = threading.Barrier(8)

    def _setter(index: int) -> None:
        try:
            barrier.wait(timeout=5)
            for step in range(30):
                store.set(_snapshot(f"s{index}-{step}"))
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    def _subscriber() -> None:
        try:
            barrier.wait(timeout=5)
            for _ in range(30):
                unsubscribe = store.subscribe(lambda _snap: None)
                store.get()
                unsubscribe()
                unsubscribe()
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    workers = [threading.Thread(target=_setter, args=(i,)) for i in range(4)]
    workers += [threading.Thread(target=_subscriber) for _ in range(4)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=15)
    assert errors == [], f"并发 set/subscribe/退订不应产生异常: {errors[:3]}"


# ---------------------------------------------------------------------- #
# AUD-14318：validate_plan_structure 两个连续 docstring（死代码）→ 合并
# ---------------------------------------------------------------------- #


def test_aud_14318_single_docstring_with_merged_content() -> None:
    """正例：函数体只有一个 docstring 字面量，且合并后含 Args/Returns 两节。"""
    doc = validate_plan_structure.__doc__ or ""
    assert "Args:" in doc and "compilable_operations" in doc
    assert "诊断元组列表" in doc, "第二个 docstring 的内容必须并入唯一 docstring"

    tree = ast.parse(inspect.getsource(validate_plan_structure))
    fn = tree.body[0]
    dead_strings = [
        node
        for node in fn.body
        if isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
    ]
    assert len(dead_strings) == 1, (
        f"函数体只允许一个 docstring 字面量，发现 {len(dead_strings)} 个"
        "（AUD-14318：第二个是死代码）"
    )


def test_aud_14318_validate_plan_structure_still_works() -> None:
    """正例：合并 docstring 后校验行为不变（缺字段仍报错、合法 plan 返回空）。"""
    findings = validate_plan_structure({})
    assert findings, "缺顶层必填字段必须报错"
    valid = validate_plan_structure({
        "plan_id": "p1", "base_revision": 0,
        "target": {"project_id": "proj", "timeline_id": "tl"},
        "operations": [],
    })
    assert valid == []


# ---------------------------------------------------------------------- #
# AUD-14504：subtitle_draft 模块 docstring 与 ADD_SUBTITLE 现状不符
# ---------------------------------------------------------------------- #


def test_aud_14504_docstring_matches_compilable_reality() -> None:
    """正例：docstring 不再声称 ADD_SUBTITLE 未接通，并与编译器事实一致。"""
    doc = subtitle_draft.__doc__ or ""
    assert "未在编译器接通" not in doc, (
        "AUD-14504：ADD_SUBTITLE 已接入 plan_compiler，docstring 的旧声明必须删除"
    )
    assert "COMPILABLE_OPERATIONS" in doc, "应指向新的单一真相源"
    assert OperationKind.ADD_SUBTITLE in COMPILABLE_OPERATIONS, (
        "前提：ADD_SUBTITLE 当前确实可编译（否则本条应记 INVALID）"
    )


# ---------------------------------------------------------------------- #
# AUD-14600：cancelled 短路测试断言过弱 → 钉死降级契约与状态机
# ---------------------------------------------------------------------- #


def _assistant_service() -> AssistantService:
    fake_session = MagicMock()
    fake_session.get_snapshot.return_value = MagicMock(
        active_timeline=None, project_id="proj-1", assets={},
    )
    return AssistantService(fake_session)


def _workflow_input() -> WorkflowInput:
    return WorkflowInput(
        workflow_id=WorkflowId.VOICEOVER_POLISH,
        asset_ids=("a1",),
        timeline_id="tl-1",
        project_id="proj-1",
        base_revision=0,
        brief_id="default",
    )


def _capture_snapshot(service: AssistantService) -> Any:
    captured: list[Any] = []
    service.on_state_changed = lambda snap: captured.append(snap)
    service._emit_state()
    assert captured
    return captured[-1]


def test_aud_14600_cancelled_contract_is_enforced() -> None:
    """正例：已取消短路返回 degradation='cancelled' 且状态机翻到 CANCELLED。"""
    service = _assistant_service()
    result = service.run_workflow(
        _workflow_input(), cancel_requested=lambda: True,
    )
    assert getattr(result, "degradation", "") == "cancelled"
    assert _capture_snapshot(service).state == AssistantState.CANCELLED


def test_aud_14600_weak_assertion_removed_from_test_file() -> None:
    """反例（回退敏感）：测试文件不得退回 'assert result is not None' 弱断言。"""
    source = _load_cancel_test_source()
    fn_start = source.index("def test_run_workflow_short_circuits_when_already_cancelled")
    fn_end = source.index("\ndef ", fn_start + 10)
    body = source[fn_start:fn_end]
    assert "assert result is not None" not in body, (
        "AUD-14600：cancelled 短路测试不得只断言 result is not None"
    )
    normalized = body.replace("'", '"').replace(" ", "")
    assert '"cancelled"' in normalized and "degradation" in normalized, (
        "必须显式断言 cancelled 降级契约"
    )
    assert "AssistantState.CANCELLED" in body, "必须断言状态机翻转"


# ---------------------------------------------------------------------- #
# AUD-14601：emit_node_event 测试回调签名与真实调用不匹配（死脚手架）
# ---------------------------------------------------------------------- #


class _StageEmittingRunner:
    """真实把 emit_stage 回调执行一次的 fake runner（复刻 WorkflowRunner 契约）。"""

    def __init__(self, stages: tuple[str, ...] = ("analyze",)) -> None:
        self._stages = stages

    def run(self, inp: WorkflowInput, *, emit_stage: Any = None) -> Any:
        from src.features.edit.ai_editing.workflow import WorkflowResult

        if emit_stage is not None:
            for stage in self._stages:
                emit_stage(stage)
        return WorkflowResult(workflow_id=inp.workflow_id)


def test_aud_14601_real_emit_contract_reaches_callback() -> None:
    """正例：emit_node_event 以真实签名收到阶段事件 (progress, '', payload)。"""
    service = _assistant_service()
    emitted: list[tuple[str, str, dict]] = []

    def emit(kind: str, node_id: str, payload: dict | None = None) -> None:
        emitted.append((kind, node_id, dict(payload or {})))

    service._workflow_runner = _StageEmittingRunner(stages=("analyze",))
    service.run_workflow(
        _workflow_input(), cancel_requested=lambda: False, emit_node_event=emit,
    )
    assert emitted == [("progress", "", {"stage": "analyze"})]


def test_aud_14601_dead_scaffolding_removed_from_test_file() -> None:
    """反例（回退敏感）：测试文件不得退回 (kind, node_id) 双参死脚手架。"""
    source = _load_cancel_test_source()
    assert "_StageEmittingRunner" in source, (
        "AUD-14601：必须用会真实执行回调的 runner 替换 MagicMock 死脚手架"
    )
    assert "emitted.append((kind, node_id))" not in source, (
        "旧两参回调体（emitted 恒空且从未断言）不得回归"
    )
    assert "payload" in source, "回调签名必须包含真实调用的 payload 参数"


# ---------------------------------------------------------------------- #
# AUD-14603：WorkflowResult 函数内重复局部导入 → 上移模块顶部
# ---------------------------------------------------------------------- #


def test_aud_14603_no_local_workflow_imports_in_functions() -> None:
    """正例/反例（回退敏感）：workflow 导入只在模块顶部，函数体内无局部导入。"""
    tree = ast.parse(_load_cancel_test_source())

    def _local_workflow_imports(node: ast.AST) -> list[ast.stmt]:
        found: list[ast.stmt] = []
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.Import, ast.ImportFrom)):
                module = getattr(child, "module", "") or ""
                names = [alias.name for alias in getattr(child, "names", [])]
                if module.endswith("workflow") or "WorkflowResult" in names:
                    found.append(child)
            elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                found.extend(_local_workflow_imports(child))
        return found

    local_hits: list[ast.stmt] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            local_hits.extend(_local_workflow_imports(node))
    assert local_hits == [], (
        "AUD-14603：WorkflowResult 不得在函数体内重复局部导入，应上移模块顶部"
    )

    top_level_names: list[str] = []
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and (node.module or "").endswith("workflow"):
            top_level_names.extend(alias.name for alias in node.names)
    assert "WorkflowResult" in top_level_names, "模块顶部必须导入 WorkflowResult"


# ---------------------------------------------------------------------- #
# AUD-14703：from_audio_sample 浮点运算 → 全程整数运算
# ---------------------------------------------------------------------- #


def test_aud_14703_integer_math_exact_conversion() -> None:
    """正例：整数换算与既有契约一致（48000 sample @ 48kHz / 24fps）。"""
    service = TimebaseService()
    assert service.from_audio_sample(48000, 48000, Rational(24, 1)) == 24
    assert service.from_audio_sample(24000, 48000, Rational(24, 1)) == 12


def test_aud_14703_exact_half_rounds_half_up_not_bankers() -> None:
    """反例（回退敏感）：精确 .5 按有理数 half-away-from-zero，不走浮点 round。

    sample=1 @ 2Hz、fps=1 → 精确 0.5 帧。旧浮点实现 round(0.5)=0（banker's），
    新整数实现 (1 + 1)//2 = 1（与 _pts_to_frame_via_timebase 同一策略族）。
    """
    service = TimebaseService()
    assert service.from_audio_sample(1, 2, Rational(1, 1)) == 1


def test_aud_14703_invalid_sample_rate_still_rejected() -> None:
    """正例：非法 sample_rate 行为保持（ValueError）。"""
    service = TimebaseService()
    with pytest.raises(ValueError, match="sample_rate must be positive"):
        service.from_audio_sample(100, 0, Rational(24, 1))


# ---------------------------------------------------------------------- #
# AUD-14711：ToolCallLog FIFO 淘汰 O(n) pop(0) → deque(maxlen)
# ---------------------------------------------------------------------- #


def _record(call_id: str) -> ToolCallRecord:
    return ToolCallRecord(call_id=call_id, timestamp=0.0, tool_name="topos.test")


def test_aud_14711_fifo_eviction_correct_and_deque_backed() -> None:
    """正例：max_entries=3 时只保留最新 3 条，底层是 deque（O(1) 淘汰）。"""
    log = ToolCallLog(max_entries=3)
    for index in range(5):
        log.record(_record(f"tc-{index:08d}"))
    ids = [record.call_id for record in log.records()]
    assert ids == ["tc-00000002", "tc-00000003", "tc-00000004"]
    assert log.count == 3
    assert isinstance(log._records, deque), (
        "AUD-14711：记录容器必须是 deque(maxlen=...)（回退到 list.pop(0) 即失败）"
    )


def test_aud_14711_unbounded_log_keeps_all_records() -> None:
    """正例：未饱和时全部保留（淘汰语义只在超限时生效）。"""
    log = ToolCallLog(max_entries=100)
    for index in range(10):
        log.record(_record(f"tc-{index:08d}"))
    assert log.count == 10


# ---------------------------------------------------------------------- #
# AUD-14803：to_dict 的 artifact_refs 未过 sanitize_for_storage
# ---------------------------------------------------------------------- #


def test_aud_14803_artifact_refs_are_sanitized() -> None:
    """正例：绝对路径 / secret 形状的 artifact_ref 在 to_dict 中被脱敏。"""
    result = ToolResult.success(
        "topos.export",
        artifact_refs=("/Users/bob/Projects/movie/render.png", "api_key=abcdef123456", "plain-id"),
    )
    refs = result.to_dict()["artifact_refs"]
    assert refs[0] == "<absolute-path>", "绝对路径 ref 必须脱敏"
    assert "abcdef123456" not in refs[1], "secret 形状 ref 必须脱敏"
    assert refs[2] == "plain-id", "普通 ref 原样保留"


def test_aud_14803_raw_absolute_path_never_leaks() -> None:
    """反例（回退敏感）：任何绝对路径都不得原样出现在 to_dict 输出里。"""
    result = ToolResult.success(
        "topos.export", artifact_refs=("/Users/bob/secret/pixel.png",),
    )
    payload = repr(result.to_dict())
    assert "/Users/bob/secret/pixel.png" not in payload, (
        "AUD-14803：artifact_refs 未过 sanitize_for_storage（模块红线：审计事件"
        "绝不泄漏绝对路径）"
    )


# ---------------------------------------------------------------------- #
# AUD-14809：--log-level 非法值使 main() 崩溃 → 降级 WARNING + 留痕
# ---------------------------------------------------------------------- #


@pytest.fixture()
def _restore_toposctl_level() -> Any:
    yield None
    logging.getLogger("toposctl").setLevel(logging.NOTSET)


def test_aud_14809_invalid_log_level_degrades_instead_of_crashing(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, _restore_toposctl_level: None,
) -> None:
    """正例：非法 --log-level 不再抛 ValueError；降级 WARNING 并 stderr 留痕。"""
    with caplog.at_level(logging.WARNING, logger="toposctl"):
        exit_code = toposctl.main(
            ["version", "--log-level", "bogus-level"],
            audit_log_path=tmp_path / "audit.jsonl",
        )
        assert exit_code == 0
        # 断言放在 at_level 上下文内（caplog 退出时会恢复原级别）
        assert logging.getLogger("toposctl").level == logging.WARNING
    assert any(
        "invalid --log-level" in record.getMessage() for record in caplog.records
    ), "降级时必须留 warning 日志（不静默吞错）"


def test_aud_14809_valid_log_level_still_applies(
    tmp_path: Path, _restore_toposctl_level: None,
) -> None:
    """正例：合法级别名行为保持（debug → DEBUG）。"""
    exit_code = toposctl.main(
        ["version", "--log-level", "debug"], audit_log_path=tmp_path / "audit.jsonl",
    )
    assert exit_code == 0
    assert logging.getLogger("toposctl").level == logging.DEBUG


# ---------------------------------------------------------------------- #
# AUD-14817：grant 在乐观锁检查之前被 consume → 预检后授权
# ---------------------------------------------------------------------- #


def _make_state(revision: int = 0) -> ProjectSequences:
    from src.features.edit.core.project import EditProject, MediaAsset, TimelineSequenceRef
    from src.features.edit.core.timeline_v2 import EditTrack, TimelineSequence

    seq = TimelineSequence(
        timeline_id="t1", name="Main", fps=Rational(24, 1),
        width=1920, height=1080, sample_rate=48000,
        video_tracks=(EditTrack(track_id="v1", name="V1", track_type="video", index=0),),
    )
    asset = MediaAsset(
        asset_id="a1", path="/media/clip.mp4", media_type="video",
        display_name="Clip A", metadata={"duration_frames": 5000},
    )
    ref = TimelineSequenceRef(
        timeline_id="t1", name="Main", fps=24.0,
        width=1920, height=1080, sample_rate=48000,
    )
    project = EditProject(
        project_id="p1", name="AI Project",
        assets={"a1": asset},
        timelines={"t1": ref},
        root_bin_id="root",
        active_timeline_id="t1",
    )
    if revision:
        seq = dataclasses.replace(seq, revision=revision)
    return ProjectSequences(project=project, sequences={"t1": seq})


def _add_clip_op() -> EditOperation:
    return EditOperation("op1", OperationKind.ADD_CLIP, params={
        "track_id": "v1", "asset_id": "a1",
        "source": {"start": 0, "duration": 100},
        "timeline": {"start": 0, "duration": 100},
    })


def test_aud_14817_revision_conflict_does_not_burn_grant(monkeypatch: pytest.MonkeyPatch) -> None:
    """正例：revision 冲突在授权之前被检出——single_use grant 仍可消费。

    场景模拟"preview 之后、提交之前 state 被改"的 TOCTOU：用真实 preview 对
    新鲜 state 算出 diff_hash 并据此签发 grant，然后对已过期的 state 提交。
    修复前：grant 先被 consume，commit_command 再抛 RevisionConflict → grant
    白白烧掉（本测试的 grant 仍可消费断言即失败）。
    """
    from src.features.edit.ai_editing.transaction_service import TransactionService

    fresh = _make_state(0)
    plan = EditPlan.create(
        PlanTarget("p1", "t1"), 0, operations=(_add_clip_op(),),
    )
    service = TransactionService()
    preview = service.preview(plan, fresh)
    assert not preview.has_errors and preview.compiled is not None

    scope = _expected_commit_scope(plan, preview.diff_hash)
    approval_service = ApprovalService()
    grant = approval_service.issue_after_user_confirmation(scope, issued_by="test")

    stale = _make_state(revision=7)

    def _fake_preview(plan_arg: EditPlan, state_arg: ProjectSequences, **_kw: Any) -> Any:
        # 模拟 preview 在旧快照上通过（提交时 state 已被他人推进）。
        return preview

    monkeypatch.setattr(service, "preview", _fake_preview)

    result = service.commit(
        plan, stale, approval=grant, approval_service=approval_service,
    )
    assert result.applied is False
    assert "revision" in (result.error or "").lower()

    # 关键断言：grant 未被烧掉，仍可正常 consume
    consumed = approval_service.consume(grant.grant_id, scope)
    assert consumed.grant_id == grant.grant_id


# ---------------------------------------------------------------------- #
# AUD-14904：analyze() 残留 thread_local.request_options → finally 恢复
# ---------------------------------------------------------------------- #


def _bare_asr_provider() -> QwenASRProvider:
    """绕过重量级 __init__，只装测试需要的 _thread_local。"""
    provider = object.__new__(QwenASRProvider)
    provider._thread_local = threading.local()
    return provider


def test_aud_14904_request_options_cleaned_after_analyze() -> None:
    """正例：analyze 正常返回后 thread_local.request_options 被清除。"""
    provider = _bare_asr_provider()
    seen: dict[str, Any] = {}

    def _impl(asset_id: str, fingerprint: Any, *, fps: str, progress: Any = None) -> tuple[Any, tuple[Any, ...]]:
        seen["options"] = getattr(provider._thread_local, "request_options", "<missing>")
        return ("analysis", ())

    provider._analyze_impl = _impl  # type: ignore[method-assign]
    provider.analyze("asset-1", fingerprint=None, fps="24/1", options={"model": "m1"})
    assert seen["options"] == {"model": "m1"}
    assert not hasattr(provider._thread_local, "request_options"), (
        "AUD-14904：调用结束后不得残留 request_options（线程池线程会复用）"
    )


def test_aud_14904_exception_path_still_cleans_up() -> None:
    """正例：analyze 异常路径同样恢复 thread_local（finally 语义）。"""
    provider = _bare_asr_provider()

    def _boom(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("asr exploded")

    provider._analyze_impl = _boom  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="asr exploded"):
        provider.analyze("asset-1", fingerprint=None, fps="24/1", options={"k": "v"})
    assert not hasattr(provider._thread_local, "request_options")


def test_aud_14904_preexisting_value_restored() -> None:
    """正例：调用前已存在的 request_options 在调用后被原样恢复。"""
    provider = _bare_asr_provider()
    provider._thread_local.request_options = {"previous": True}
    provider._analyze_impl = (  # type: ignore[method-assign]
        lambda *_a, **_kw: ("analysis", ())
    )
    provider.analyze("asset-1", fingerprint=None, fps="24/1", options={"new": 1})
    assert provider._thread_local.request_options == {"previous": True}


# ---------------------------------------------------------------------- #
# AUD-14912：_remote_by_profile 缓存无锁 → 双检锁
# ---------------------------------------------------------------------- #


def test_aud_14912_remote_provider_created_exactly_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """正例：并发 _remote 只实例化一个 RemoteTranscriptProvider。"""
    import src.features.edit.ai_editing.remote_transcript_provider as rtp_module

    created: list[str] = []

    class _FakeRemote:
        def __init__(self, profile: Any, credential_store: Any, *, asset_path_provider: Any = None) -> None:
            created.append(profile.profile_id)
            time.sleep(0.05)  # 放大竞态窗口
            self.profile_id = profile.profile_id

    monkeypatch.setattr(rtp_module, "RemoteTranscriptProvider", _FakeRemote)

    profile = SimpleNamespace(
        profile_id="cloud-asr", source_kind="cloud", provider_kind="http",
    )
    registry = SimpleNamespace(
        active_profile_for_capability=lambda _cap: profile, list=lambda: [],
    )
    credential_store = SimpleNamespace(get_secret=lambda ref: "secret")
    provider = ProfileAwareTranscriptProvider(MagicMock(), registry, credential_store)

    assert hasattr(provider, "_remote_lock") and _is_lock(provider._remote_lock), (
        "AUD-14912：缓存字典必须持锁（回退到无锁实现即失败）"
    )

    barrier = threading.Barrier(8)
    results: dict[int, Any] = {}
    errors: list[BaseException] = []

    def _worker(index: int) -> None:
        try:
            barrier.wait(timeout=5)
            results[index] = provider._remote(profile)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    workers = [threading.Thread(target=_worker, args=(i,)) for i in range(8)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=15)

    assert errors == []
    assert len(created) == 1, f"并发 miss 只允许构造一次，实际 {len(created)} 次"
    assert all(results[i] is results[0] for i in results)


# ---------------------------------------------------------------------- #
# AUD-14918：_engines 引擎缓存无锁 → 双检锁
# ---------------------------------------------------------------------- #


def test_aud_14918_engine_loaded_exactly_once_under_concurrency() -> None:
    """正例：并发 _get_engine 只加载一次权重（不重复加载 3.8GB）。"""
    provider = Qwen3TTSProvider()
    assert hasattr(provider, "_engines_lock") and _is_lock(provider._engines_lock), (
        "AUD-14918：引擎缓存必须持锁（回退到无锁实现即失败）"
    )

    loads: list[str] = []

    def _fake_resolve(pack_id: str) -> str:
        return f"/models/{pack_id}"

    def _fake_load(path: str, model_kind: str) -> Any:
        loads.append(model_kind)
        time.sleep(0.05)  # 放大竞态窗口（模拟加载 3.8GB 权重）
        return SimpleNamespace(kind=model_kind)

    provider._resolve_model_path = _fake_resolve  # type: ignore[method-assign]
    provider._load_model = _fake_load  # type: ignore[method-assign]

    barrier = threading.Barrier(8)
    results: dict[int, Any] = {}
    errors: list[BaseException] = []

    def _worker(index: int) -> None:
        try:
            barrier.wait(timeout=5)
            results[index] = provider._get_engine("custom_voice")
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    workers = [threading.Thread(target=_worker, args=(i,)) for i in range(8)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=15)

    assert errors == []
    assert loads == ["custom_voice"], f"只允许加载一次，实际 {len(loads)} 次"
    assert all(results[i] is results[0] for i in results)


def test_aud_14918_second_kind_loads_independently() -> None:
    """正例：不同 model_kind 各自加载一次（缓存按 kind 分立语义保持）。"""
    provider = Qwen3TTSProvider()
    loads: list[str] = []

    def _fake_load(path: str, model_kind: str) -> Any:
        loads.append(model_kind)
        return SimpleNamespace(kind=model_kind)

    provider._resolve_model_path = lambda pack_id: f"/models/{pack_id}"  # type: ignore[method-assign]
    provider._load_model = _fake_load  # type: ignore[method-assign]

    engine_a = provider._get_engine("custom_voice")
    engine_b = provider._get_engine("base")
    assert engine_a is provider._get_engine("custom_voice"), "命中缓存应返回同一实例"
    assert engine_b is provider._get_engine("base")
    assert sorted(loads) == ["base", "custom_voice"]
