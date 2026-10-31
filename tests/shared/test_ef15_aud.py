"""批 EF-15（src/shared/ai_models 12 条 + src/features/ai_harness 11 条）修复验证。

覆盖（每条含正例/反例断言，修复回退时必须 FAILED）：

ai_harness:
- AUD-21900  __init__ 模块 docstring 与实际导出一致
- AUD-21911  worker 线程回调的 JobRunBridge.sync 在锁保护下（与 AUD-22319 同一修复）
- AUD-22022  MCP registry / auth log / transport 缓存 / 请求 id 均有锁
- AUD-22200  PluginRolloutRouter 有锁，register/remove/迭代不再竞态
- AUD-22312  _emit/_emit_node 的事件 append 受 runtime RLock 保护
- AUD-22313  retry() 空锁块死代码已删除
- AUD-22317  RuntimeInvariantEngine._seen/_violations 有锁
- AUD-22318  _on_event 不再对每事件回放全量历史（O(1) 增量索引）
- AUD-22319  JobRunBridge._bindings/_job_to_runs 有锁，propagate_cancel 快照迭代
- AUD-22321  SessionEventLog 有锁，并发 append seq 唯一
- AUD-22413  SkillStudioStore 有锁 + 容量上限；死字段 _registry 已删除

ai_models:
- AUD-31503  _load_with_timeout 记录真实加载异常 + 收容超时 loader 线程
- AUD-31525  AIDenoiseThreadController error 路径清理 worker（现状已修，回归护栏）
- AUD-31603  security 子进程调用带 timeout，超时有界且语义正确
- AUD-31617  model_pack manifest 原子写 + 加载失败 warning 留痕
- AUD-31618  default_cache_dir docstring 与实际路径推导一致（不再依赖 parents[4]）
- AUD-31619  ModelPackManager._installed / manifest 保存有锁
- AUD-31710  generated_media 缓存有容量上限与最旧优先清理
- AUD-31807  ONNX 会话缓存 per-path 防重复加载 + LRU 上限 + worker deleteLater
- AUD-31808  _run_inference 死形参 / 未用 import time 删除；预览结果确实存储
- AUD-31810  model_manager manifest 原子写
- AUD-31811  MODEL_REGISTRY 写锁 / 单例双检锁 / cancel_download 不同步 wait
- AUD-31815  RIFE 会话走统一 provider 配置（不硬编码 CPU）+ 模块缓存有锁

测试不加载真实模型权重（全部 mock ort / ConfigManager 重依赖），不弹模态对话框。
"""

from __future__ import annotations

import base64
import inspect
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Callable
from unittest.mock import patch

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


# ---------------------------------------------------------------------------
# ai_harness: AUD-21900 — 模块 docstring 与实际导出一致
# ---------------------------------------------------------------------------

class TestAud21900InitDocstring:
    def test_docstring_covers_actual_module_surface(self) -> None:
        """docstring 不再停留在 P0 描述，且提及现存的主要子模块。"""
        import src.features.ai_harness as harness

        doc = harness.__doc__ or ""
        # 反例：旧 P0 限定描述必须消失
        assert "P0 阶段（本文件）只定义" not in doc
        # 正例：现存主要能力面被描述
        for token in (
            "plugin_rollout", "skill_studio_drafts", "mcp_outbound",
            "run_trace", "generation_adapter", "scheduler_adapter",
        ):
            assert token in doc, f"docstring missing {token}"
        # 导出的符号确实存在
        for name in harness.__all__:
            assert hasattr(harness, name), name


# ---------------------------------------------------------------------------
# ai_harness: AUD-21911 + AUD-22319 — JobRunBridge 线程安全
# ---------------------------------------------------------------------------

class _FakeJobHandle:
    def __init__(self, job_id: str, status: str = "running", progress: float = 0.5):
        self.job_id = job_id
        self.kind = "analysis"
        self.label = "job"
        self.status = status
        self.progress = progress
        self.cancel_requested = False

    def request_cancel(self) -> None:
        self.cancel_requested = True


class _FakeRegistry:
    def __init__(self) -> None:
        self.jobs: dict[str, _FakeJobHandle] = {}

    def get(self, job_id: str):
        return self.jobs.get(job_id)


class TestAud21911Aud22319JobRunBridge:
    def test_bridge_has_lock_and_basic_sync_works(self) -> None:
        """正例：bind/track/sync/propagate_cancel 语义不变；存在 RLock。"""
        from src.features.ai_harness.invocation_models import Invocation
        from src.features.ai_harness.runtime import HarnessRuntime
        from src.features.ai_harness.scheduler_adapter import JobRunBridge

        runtime = HarnessRuntime()
        bridge = JobRunBridge(runtime)
        # AUD-22319 反例断言：必须持锁（回退为无锁实现时此断言失败）
        assert isinstance(bridge._lock, type(threading.RLock()))

        run_id = runtime.start(Invocation())
        registry = _FakeRegistry()
        handle = _FakeJobHandle("job-1")
        registry.jobs["job-1"] = handle
        bridge.bind(run_id, registry)
        assert bridge.sync(run_id, "job-1") is True
        assert runtime.cancel(run_id, reason="test") is True
        assert bridge.propagate_cancel(run_id) == 1
        assert handle.cancel_requested is True
        assert bridge.sync("unknown-run", "job-1") is False

    def test_concurrent_track_sync_propagate_never_crashes(self) -> None:
        """多线程并发 track/sync/propagate_cancel 无异常（迭代竞态被快照消除）。"""
        from src.features.ai_harness.invocation_models import Invocation
        from src.features.ai_harness.runtime import HarnessRuntime
        from src.features.ai_harness.scheduler_adapter import JobRunBridge

        runtime = HarnessRuntime()
        run_id = runtime.start(Invocation())
        bridge = JobRunBridge(runtime)
        registry = _FakeRegistry()
        bridge.bind(run_id, registry)

        errors: list[BaseException] = []

        def tracker(base: int) -> None:
            try:
                for i in range(60):
                    jid = f"job-{base}-{i}"
                    registry.jobs[jid] = _FakeJobHandle(jid)
                    bridge.track(run_id, jid, registry)
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        def propagator() -> None:
            try:
                for _ in range(60):
                    bridge.propagate_cancel(run_id)
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=tracker, args=(t,)) for t in range(4)]
        threads += [threading.Thread(target=propagator) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        assert errors == []


# ---------------------------------------------------------------------------
# ai_harness: AUD-22022 — MCP registry / auth log / transports / request id
# ---------------------------------------------------------------------------

class TestAud22022McpOutboundLocks:
    def _registry(self):
        from src.features.ai_harness.mcp_outbound import McpConnectionProfile
        return McpConnectionProfile(profile_id="p1", server_name="srv")

    def test_auth_log_lock_and_behavior(self) -> None:
        from src.features.ai_harness.mcp_outbound import (
            McpAuthorizationEvent,
            McpAuthorizationLog,
        )

        log = McpAuthorizationLog(max_events=10)
        assert isinstance(log._lock, type(threading.Lock()))
        for i in range(20):
            log.record(McpAuthorizationEvent(
                timestamp=i, profile_id="p1", server_name="s",
                tool_name="t", decision="denied_test",
            ))
        assert len(log.events()) == 10
        assert log.denial_count() == 10

        errors: list[BaseException] = []

        def hammer() -> None:
            try:
                for _ in range(100):
                    log.record(McpAuthorizationEvent(
                        timestamp=0.0, profile_id="p", server_name="s",
                        tool_name="t", decision="allowed",
                    ))
                    log.events()
                    log.denial_count()
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=hammer) for _ in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        assert errors == []
        assert len(log.events()) == 10

    def test_registry_lock_and_manifest_ttl(self) -> None:
        from src.features.ai_harness.mcp_outbound import (
            McpServerManifest,
            McpServerRegistry,
        )

        registry = McpServerRegistry()
        assert isinstance(registry._lock, type(threading.RLock()))
        profile = self._registry()
        registry.register(profile)
        assert registry.get("p1") is profile
        assert registry.is_allowlisted("p1") is True
        registry.mark_blocked("p1", "missing deps")
        assert registry.enable_for_session("p1") is False

        manifest = McpServerManifest(
            server_name="srv", server_version="1", protocol_version="2024",
            tools=(), capabilities={}, ttl_ms=0,
        )
        registry.cache_manifest("p1", manifest)
        time.sleep(0.01)
        assert registry.get_manifest("p1") is None  # TTL 过期被淘汰

    def test_request_ids_unique_under_concurrency(self) -> None:
        """AUD-22022：同毫秒并发请求 id 不再相撞（旧实现无 _next_request_id）。"""
        from src.features.ai_harness.mcp_outbound import McpOutboundClient
        from src.features.ai_harness.mcp_outbound import McpServerRegistry

        client = McpOutboundClient(McpServerRegistry())
        ids: list[str] = []
        lock = threading.Lock()

        def gen() -> None:
            local = [client._next_request_id("topos", 123.0) for _ in range(200)]
            with lock:
                ids.extend(local)

        threads = [threading.Thread(target=gen) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        assert len(ids) == 1600
        assert len(set(ids)) == 1600  # 全局唯一

    def test_transport_cache_converges_to_single_transport(self) -> None:
        """并发首调同一 profile 只保留一个 transport（旧实现缓存两条）。"""
        from src.features.ai_harness.mcp_outbound import (
            McpOutboundClient,
            McpServerRegistry,
        )

        class _Transport:
            def __init__(self, tag: int) -> None:
                self.tag = tag

            def close(self) -> None:
                pass

        profile = self._registry()
        barrier = threading.Barrier(4)
        calls: list[_Transport] = []

        def factory(_profile):
            barrier.wait(timeout=5)  # 拉大 check-then-act 窗口
            t = _Transport(len(calls))
            calls.append(t)
            return t

        client = McpOutboundClient(
            McpServerRegistry(), transport_factory=factory,
        )
        results: list[_Transport] = []
        threads = [
            threading.Thread(target=lambda r=results: r.append(
                client._get_transport(profile)))
            for _ in range(4)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        assert len(client._transports) == 1
        assert results[0] is results[1] is results[2] is results[3]

    def test_close_snapshots_and_closes_all(self) -> None:
        from src.features.ai_harness.mcp_outbound import (
            McpOutboundClient,
            McpServerRegistry,
        )

        closed: list[int] = []

        class _T:
            def close(self) -> None:
                closed.append(1)

        client = McpOutboundClient(McpServerRegistry())
        client._transports["a"] = _T()
        client._transports["b"] = _T()
        client.close()
        assert len(closed) == 2
        assert client._transports == {}


# ---------------------------------------------------------------------------
# ai_harness: AUD-22200 — PluginRolloutRouter 锁
# ---------------------------------------------------------------------------

class TestAud22200PluginRolloutRouter:
    def _flag(self, plugin_id: str = "plug.a", percentage: int = 100):
        from src.features.ai_harness.plugin_rollout import PluginRolloutFlag
        return PluginRolloutFlag(
            plugin_id=plugin_id, target_version="1.1.0",
            percentage=percentage, started_at=0.0,
        )

    def test_lock_present_and_register_semantics(self) -> None:
        from src.features.ai_harness.plugin_rollout import PluginRolloutRouter

        router = PluginRolloutRouter()
        assert isinstance(router._lock, type(threading.Lock()))
        assert router.register(self._flag()) is True
        assert router.register(self._flag()) is False  # 重复注册拒绝（语义不变）
        assert router.target_for_new_run(
            "plug.a", run_id="r1", run_created_at=10.0) == "1.1.0"
        assert router.to_dict()["flags"][0]["plugin_id"] == "plug.a"
        assert router.remove("plug.a") is True
        assert router.remove("plug.a") is False
        assert router.target_for_new_run(
            "plug.a", run_id="r1", run_created_at=10.0) is None

    def test_concurrent_register_remove_iterate_no_crash(self) -> None:
        from src.features.ai_harness.plugin_rollout import PluginRolloutRouter

        router = PluginRolloutRouter()
        errors: list[BaseException] = []

        def mutator(base: int) -> None:
            try:
                for i in range(80):
                    router.register(self._flag(f"plug.{base}.{i}"))
                    router.to_dict()
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        def remover() -> None:
            try:
                for i in range(80):
                    router.remove(f"plug.0.{i}")
                    router.target_for_new_run(
                        f"plug.1.{i}", run_id="r", run_created_at=9.0)
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=mutator, args=(b,)) for b in range(3)]
        threads += [threading.Thread(target=remover) for _ in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        assert errors == []


# ---------------------------------------------------------------------------
# ai_harness: AUD-22312 — _emit/_emit_node 事件流受 RLock 保护
# ---------------------------------------------------------------------------

class TestAud22312RuntimeEmitLock:
    def test_emit_blocks_while_lock_held_elsewhere(self) -> None:
        """持锁期间 _emit 必须阻塞（证明 append 在锁内；回退后立即完成）。"""
        from src.features.ai_harness.invocation_models import Invocation, RunEventKind
        from src.features.ai_harness.runtime import HarnessRuntime

        rt = HarnessRuntime()
        # 复核回退抽验发现的盲区：_dispatch 内部同样会短暂持有 self._lock
        # （订阅者快照），即使 append 被移出锁，emit 仍会在 dispatch 处阻塞、
        # 本测试空转通过。stub 掉 dispatch，使断言只对 append 所在临界区
        # 敏感（AUD-22312 的修复本体）。
        rt._dispatch = lambda event: None  # type: ignore[method-assign]
        run_id = rt.start(Invocation())
        state = rt._runs[run_id]

        entered = threading.Event()
        release = threading.Event()

        def hold_lock() -> None:
            with rt._lock:
                entered.set()
                release.wait(timeout=5)

        holder = threading.Thread(target=hold_lock, daemon=True)
        holder.start()
        assert entered.wait(timeout=5)

        emitter = threading.Thread(
            target=lambda: rt._emit(state, RunEventKind.RUN_CREATED, payload={}),
            daemon=True,
        )
        emitter.start()
        emitter.join(timeout=0.4)
        # 反例断言（AUD-22312 修复点）：锁未释放前 emit 不应完成
        assert emitter.is_alive(), (
            "_emit 在他人持锁期间完成了事件 append——锁保护被回退"
        )
        release.set()
        emitter.join(timeout=5)
        assert not emitter.is_alive()
        assert len(state.events) >= 2  # run.created + 本次

    def test_emit_node_appends_node_event(self) -> None:
        from src.features.ai_harness.invocation_models import Invocation, RunEventKind
        from src.features.ai_harness.runtime import HarnessRuntime

        rt = HarnessRuntime()
        run_id = rt.start(Invocation())
        state = rt._runs[run_id]
        rt._emit_node(state, RunEventKind.NODE_STARTED, node_id="n1", payload={})
        assert state.events[-1].node_id == "n1"


# ---------------------------------------------------------------------------
# ai_harness: AUD-22313 — retry() 空锁块死代码
# ---------------------------------------------------------------------------

class TestAud22313RetryDeadCode:
    def test_retry_has_no_empty_lock_block(self) -> None:
        """retry() 源码不再含死代码标记（new_state + pass 空块）。"""
        from src.features.ai_harness.runtime import HarnessRuntime

        src_text = inspect.getsource(HarnessRuntime.retry)
        assert "new_state" not in src_text
        assert "pass" not in src_text

    def test_retry_still_works(self) -> None:
        from src.features.ai_harness.invocation_models import Invocation
        from src.features.ai_harness.runtime import HarnessRuntime

        rt = HarnessRuntime()
        run_id = rt.start(Invocation())
        new_id = rt.retry(run_id)
        assert new_id is not None and new_id != run_id
        # retry 事件已发布
        kinds = [e.kind for e in rt._runs[new_id].events]
        assert any("retry" in str(k) for k in kinds)


# ---------------------------------------------------------------------------
# ai_harness: AUD-22317 / AUD-22318 — RuntimeInvariantEngine
# ---------------------------------------------------------------------------

def _agent_event(kind: str, run_id: str = "r1", event_id: str = "e", payload=None):
    return SimpleNamespace(
        kind=kind, run_id=run_id, event_id=event_id,
        payload=payload if payload is not None else {},
    )


class TestAud22317Aud22318RuntimeInvariants:
    def _engine(self, strict: bool = False):
        from src.features.ai_harness.runtime_invariants import RuntimeInvariantEngine

        orch = _CountingOrchestrator()
        engine = RuntimeInvariantEngine(orch, strict=strict)
        return engine, orch

    def test_lock_present_and_violation_deduped(self) -> None:
        from src.features.ai_harness.runtime_invariants import RuntimeInvariantEngine

        engine, _ = self._engine()
        assert isinstance(engine._lock, type(threading.Lock()))
        bad = _agent_event(
            "run.tool_finished", event_id="e1",
            payload={"call_id": "c1", "error_code": "OK"},
        )
        engine._on_event(bad)
        engine._on_event(bad)  # 同一违例重复回调只记一次
        assert len(engine.violations) == 1
        assert engine.violations[0].code == "TOOL_FINISH_WITHOUT_START"
        engine.clear()
        assert engine.violations == ()

    def test_started_then_finished_is_clean(self) -> None:
        from src.features.edit.ai_editing.agent_models import AgentEventKind

        engine, _ = self._engine()
        engine._on_event(_agent_event(
            AgentEventKind.RUN_TOOL_STARTED, event_id="a",
            payload={"call_id": "c1"},
        ))
        engine._on_event(_agent_event(
            AgentEventKind.RUN_TOOL_FINISHED, event_id="b",
            payload={"call_id": "c1", "error_code": "OK"},
        ))
        assert engine.violations == ()

    def test_retry_after_terminal_flagged(self) -> None:
        from src.features.edit.ai_editing.agent_models import AgentEventKind

        engine, _ = self._engine()
        engine._on_event(_agent_event(AgentEventKind.RUN_COMPLETED, event_id="a"))
        engine._on_event(_agent_event(AgentEventKind.RUN_REQUEST_RETRY, event_id="b"))
        assert [v.code for v in engine.violations] == ["RETRY_AFTER_TERMINAL"]

    def test_no_full_history_replay_for_core_checks(self) -> None:
        """AUD-22318 反例断言：仅核心检查时 _on_event 不再调用 get_events。"""
        from src.features.edit.ai_editing.agent_models import AgentEventKind

        engine, orch = self._engine()
        for i in range(5):
            engine._on_event(_agent_event(
                AgentEventKind.RUN_TOOL_STARTED, event_id=f"s{i}",
                payload={"call_id": f"c{i}"},
            ))
        assert orch.get_events_calls == 0, (
            "_on_event 回放了全量历史（O(n²) 未修复）"
        )

    def test_custom_checks_still_receive_full_history(self) -> None:
        from src.features.ai_harness.runtime_invariants import (
            InvariantViolation,
            RuntimeInvariantEngine,
        )

        seen_lengths: list[int] = []

        def custom(event, events) -> InvariantViolation | None:
            seen_lengths.append(len(events))
            return None

        orch = _CountingOrchestrator()
        orch._events["r1"] = [_agent_event("run.created", event_id="x")] * 3
        engine = RuntimeInvariantEngine(orch, checks=(custom,))
        engine._on_event(_agent_event("run.pre_step", event_id="y"))
        assert seen_lengths and seen_lengths[0] == 3

    def test_concurrent_on_event_no_duplicate(self) -> None:
        from src.features.edit.ai_editing.agent_models import AgentEventKind

        engine, _ = self._engine()
        bad = _agent_event(
            AgentEventKind.RUN_TOOL_FINISHED, event_id="e1",
            payload={"call_id": "c1", "error_code": "OK"},
        )
        threads = [
            threading.Thread(target=lambda: engine._on_event(bad))
            for _ in range(8)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
        assert len(engine.violations) == 1


class _CountingOrchestrator:
    """最小 orchestrator 替身：subscribe/get_events + get_events 调用计数。"""

    def __init__(self) -> None:
        self._events: dict[str, list] = {}
        self.get_events_calls = 0

    def subscribe(self, fn) -> Callable[[], None]:
        return lambda: None

    def get_events(self, run_id: str) -> tuple:
        self.get_events_calls += 1
        return tuple(self._events.get(run_id, ()))


# ---------------------------------------------------------------------------
# ai_harness: AUD-22321 — SessionEventLog 锁
# ---------------------------------------------------------------------------

class TestAud22321SessionEventLog:
    def test_lock_present_and_append_seq_unique(self) -> None:
        from src.features.ai_harness.session_fork import SessionEventLog

        log = SessionEventLog()
        assert isinstance(log._lock, type(threading.Lock()))

        barrier = threading.Barrier(8)
        errors: list[BaseException] = []

        def appender(tid: int) -> None:
            try:
                barrier.wait(timeout=5)
                for _ in range(50):
                    log.append("s1", "chat.message", payload={"t": tid})
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=appender, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        assert errors == []
        rows = log.events("s1")
        assert len(rows) == 400
        seqs = [e.seq for e in rows]
        # 反例断言（AUD-22321 修复点）：check-then-act 竞态会产生重复 seq
        assert len(set(seqs)) == 400, "并发 append 出现重复 seq——锁保护被回退"
        assert sorted(seqs) == list(range(400))

    def test_fork_semantics_preserved(self) -> None:
        from src.features.ai_harness.session_fork import (
            SessionEventLog,
            SessionForkError,
        )

        log = SessionEventLog()
        for i in range(5):
            log.append("s1", "ev", payload={"i": i})
        fork = log.fork("s1", at_seq=2)
        assert fork.event_count == 3
        assert len(log.events(fork.fork_session_id)) == 3
        with pytest.raises(SessionForkError):
            log.fork("s1", at_seq=99)
        assert log.fork_info(fork.fork_session_id) is fork


# ---------------------------------------------------------------------------
# ai_harness: AUD-22413 — SkillStudioStore 锁 + 容量 + 死字段
# ---------------------------------------------------------------------------

class TestAud22413SkillStudioStore:
    @staticmethod
    def _draft(author: str = "dev", title: str = "t", now: float = 0.0):
        from src.features.ai_harness.skill_studio_drafts import new_draft_from_workflow

        return new_draft_from_workflow(
            author=author,
            workflow_definition={
                "workflow_id": f"wf-{title}",
                "name": title,
                "nodes": [{"node_id": "n1", "spec_id": "topos.tts.qwen3"}],
            },
            title=title,
            now=now,
        )

    def test_store_lock_and_no_dead_registry_field(self) -> None:
        from src.features.ai_harness.skill_studio_drafts import SkillStudioStore

        store = SkillStudioStore()
        assert isinstance(store._lock, type(threading.RLock()))
        # AUD-22413：死字段 _registry 已删除（原注释自称"显式 None"）
        assert not hasattr(store, "_registry")

    def test_capacity_cap_evicts_oldest(self) -> None:
        from src.features.ai_harness.skill_studio_drafts import SkillStudioStore

        store = SkillStudioStore(max_drafts=3)
        drafts = [self._draft(title=f"d{i}", now=float(i)) for i in range(5)]
        for d in drafts:
            store.save(d)
        # 反例断言（AUD-22413 修复点）：无上限实现 count == 5
        assert store.count == 3
        kept_ids = {d.draft_id for d in store.list_drafts()}
        assert drafts[0].draft_id not in kept_ids
        assert drafts[1].draft_id not in kept_ids
        assert drafts[4].draft_id in kept_ids

    def test_save_update_does_not_evict_self(self) -> None:
        from src.features.ai_harness.skill_studio_drafts import SkillStudioStore

        store = SkillStudioStore(max_drafts=2)
        d1 = self._draft(title="a", now=1.0)
        d2 = self._draft(title="b", now=2.0)
        store.save(d1)
        store.save(d2)
        store.save(d1)  # 更新旧草稿也不淘汰
        assert store.count == 2

    def test_invalid_max_drafts_rejected(self) -> None:
        from src.features.ai_harness.skill_studio_drafts import SkillStudioStore

        with pytest.raises(ValueError):
            SkillStudioStore(max_drafts=0)


# ---------------------------------------------------------------------------
# ai_models: AUD-31503 — _load_with_timeout 异常留痕 + stray loader 收容
# ---------------------------------------------------------------------------

class TestAud31503LoadWithTimeout:
    def test_real_load_exception_is_logged_not_swallowed(self, caplog) -> None:
        """加载异常必须 error 留痕（旧实现静默置 None）。"""
        import logging as _logging

        import src.shared.ai_models.ai_plugin_engine as ape

        def _boom(*_a, **_k):
            raise RuntimeError("gpu exploded")

        with patch.object(ape.ort, "InferenceSession", side_effect=_boom):
            with caplog.at_level(_logging.ERROR, logger="src.shared.ai_models.ai_plugin_engine"):
                result = ape.AIPluginWorker._load_with_timeout(
                    "/fake/model.onnx", sess_options=None,
                    providers=["CPUExecutionProvider"], timeout=5,
                )
        assert result is None
        assert any("ONNX session load failed" in r.message for r in caplog.records)

    def test_timed_out_loader_thread_is_parked_and_reaped(self) -> None:
        """超时 loader 被收容进 _stray_loaders，退出后被顺手清理。"""
        import src.shared.ai_models.ai_plugin_engine as ape

        release = threading.Event()

        def _slow(*_a, **_k):
            release.wait(timeout=5)
            return object()

        with patch.object(ape.ort, "InferenceSession", side_effect=_slow):
            first = ape.AIPluginWorker._load_with_timeout(
                "/fake/slow.onnx", sess_options=None,
                providers=["CPUExecutionProvider"], timeout=0,
            )
        assert first is None  # 超时返回 None
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and ape.AIPluginWorker._stray_loaders == set():
            time.sleep(0.01)
        parked = set(ape.AIPluginWorker._stray_loaders)
        # 反例断言（AUD-31503 修复点）：旧实现没有任何收容所
        assert len(parked) == 1

        release.set()
        # 等 loader1 自然退出，再发起第二次超时调用（否则收容项在存活期
        # 不会被清理，断言会与退出时序竞态）。
        loader1 = next(iter(parked))
        deadline = time.monotonic() + 5
        while loader1.is_alive() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert not loader1.is_alive()

        # 收容线程退出后，下一次超时调用应顺手清掉已结束的收容项。
        # （第二次调用用独立 gate，保证它确实超时进收容所。）
        release2 = threading.Event()

        def _slow2(*_a, **_k):
            release2.wait(timeout=5)
            return object()

        with patch.object(ape.ort, "InferenceSession", side_effect=_slow2):
            ape.AIPluginWorker._load_with_timeout(
                "/fake/slow2.onnx", sess_options=None,
                providers=["CPUExecutionProvider"], timeout=0,
            )
        deadline = time.monotonic() + 5
        snapshot: set = set()
        while time.monotonic() < deadline:
            ape.AIPluginWorker._stray_loaders_mutex.lock()
            snapshot = set(ape.AIPluginWorker._stray_loaders)
            ape.AIPluginWorker._stray_loaders_mutex.unlock()
            if len(snapshot) == 1 and loader1 not in snapshot:
                break
            time.sleep(0.01)
        ape.AIPluginWorker._stray_loaders_mutex.lock()
        snapshot = set(ape.AIPluginWorker._stray_loaders)
        ape.AIPluginWorker._stray_loaders_mutex.unlock()
        # 反例断言（AUD-31503 修复点）：旧实现没有任何收容与清理
        assert len(snapshot) == 1
        assert loader1 not in snapshot  # 旧的已被清理
        loader2 = next(iter(snapshot))
        release2.set()
        deadline = time.monotonic() + 5
        while loader2.is_alive() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert not loader2.is_alive()  # 收容项最终自然退出


# ---------------------------------------------------------------------------
# ai_models: AUD-31525 — error 路径 worker 清理（现状已修，回归护栏）
# ---------------------------------------------------------------------------

class TestAud31525DenoiseWorkerCleanup:
    def test_error_path_cleans_worker_and_thread(self, qapp) -> None:
        """失败 run 结束后 controller 引用清空、worker 对象被 deleteLater。"""
        import PyQt6.sip as sip

        from src.shared.ai_models.denoise_worker import AIDenoiseThreadController

        controller = AIDenoiseThreadController()
        controller.start_processing(
            np.zeros((8, 8, 3), dtype=np.float32),
            {"model_path": "/nonexistent/model.onnx"},
        )
        worker = controller._worker
        assert worker is not None
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            qapp.processEvents()
            if not controller.is_running() and controller._worker is None:
                break
            time.sleep(0.02)
        qapp.processEvents()
        # 反例断言（AUD-31525 修复点）：error 路径不清理则引用残留
        assert controller._worker is None
        assert controller._thread is None
        assert controller.is_running() is False
        # worker 走 deleteLater：C++ 对象已析构，访问应抛 RuntimeError
        with pytest.raises(RuntimeError):
            worker.objectName()


# ---------------------------------------------------------------------------
# ai_models: AUD-31603 — security 子进程 timeout
# ---------------------------------------------------------------------------

class TestAud31603SecuritySubprocessTimeout:
    def test_all_security_calls_pass_timeout(self, monkeypatch, caplog) -> None:
        import logging as _logging

        from src.shared.ai_models.generation import credential_store as cs

        captured: dict[str, object] = {}

        def fake_run(cmd, **kwargs):
            captured["timeout"] = kwargs.get("timeout", "<<missing>>")
            captured["cmd0"] = cmd[0]
            return subprocess.CompletedProcess(cmd, 0, "", "")

        monkeypatch.setattr(cs.subprocess, "run", fake_run)
        # 正例：三个入口都带 timeout（反例：旧实现 kwargs 无 timeout）
        cs._MacOSSecurityKeychain.get_password("svc", "user")
        assert captured["timeout"] == cs._SECURITY_TIMEOUT_SECONDS
        cs._MacOSSecurityKeychain.set_password("svc", "user", "pw")
        assert captured["timeout"] == cs._SECURITY_TIMEOUT_SECONDS
        cs._MacOSSecurityKeychain.delete_password("svc", "user")
        assert captured["timeout"] == cs._SECURITY_TIMEOUT_SECONDS

    def test_get_password_timeout_returns_none_with_log(self, monkeypatch, caplog) -> None:
        import logging as _logging

        from src.shared.ai_models.generation import credential_store as cs

        def fake_run(cmd, **kwargs):
            raise subprocess.TimeoutExpired(cmd=cmd, timeout=1.0)

        monkeypatch.setattr(cs.subprocess, "run", fake_run)
        with caplog.at_level(_logging.WARNING, logger=cs.__name__):
            assert cs._MacOSSecurityKeychain.get_password("svc", "u") is None
            cs._MacOSSecurityKeychain.delete_password("svc", "u")  # 不抛
        assert any("timed out" in r.message for r in caplog.records)

    def test_set_password_timeout_raises_runtime_error(self, monkeypatch) -> None:
        from src.shared.ai_models.generation import credential_store as cs

        def fake_run(cmd, **kwargs):
            raise subprocess.TimeoutExpired(cmd=cmd, timeout=1.0)

        monkeypatch.setattr(cs.subprocess, "run", fake_run)
        with pytest.raises(RuntimeError):
            cs._MacOSSecurityKeychain.set_password("svc", "u", "pw")


# ---------------------------------------------------------------------------
# ai_models: AUD-31617 — model_pack manifest 原子写 + 加载失败留痕
# ---------------------------------------------------------------------------

class TestAud31617ModelPackManifestAtomic:
    def _manager(self, tmp_path: Path):
        from src.shared.ai_models.generation.model_pack_manager import ModelPackManager

        return ModelPackManager(cache_dir=tmp_path)

    def test_save_is_atomic_and_old_content_intact_until_replace(
        self, tmp_path, monkeypatch,
    ) -> None:
        import src.shared.ai_models.generation.model_pack_manager as mpm

        manager = self._manager(tmp_path)
        manager._manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manager._installed["p1"] = mpm._InstalledRecord(
            pack_id="p1", install_path=str(tmp_path), sha256="",
        )
        manager._save_manifest()
        old_text = manager._manifest_path.read_text(encoding="utf-8")

        real_replace = os.replace
        observed: dict[str, str] = {}

        def spy_replace(src, dst):
            # 在 replace 前一刻：目标文件必须仍是旧内容（绝不能是半截）
            observed["at_replace"] = Path(dst).read_text(encoding="utf-8")
            return real_replace(src, dst)

        manager._installed["p2"] = mpm._InstalledRecord(
            pack_id="p2", install_path=str(tmp_path), sha256="",
        )
        monkeypatch.setattr(mpm.os, "replace", spy_replace)
        manager._save_manifest()
        # 反例断言（AUD-31617 修复点）：旧实现 truncate 式写无 os.replace
        assert "at_replace" in observed
        assert '"pack_id": "p1"' in observed["at_replace"]
        assert '"pack_id": "p2"' not in observed["at_replace"]
        final = json.loads(manager._manifest_path.read_text(encoding="utf-8"))
        assert {r["pack_id"] for r in final["installed"]} == {"p1", "p2"}

    def test_failed_save_leaves_old_manifest_intact(self, tmp_path, monkeypatch, caplog) -> None:
        import logging as _logging

        import src.shared.ai_models.generation.model_pack_manager as mpm

        manager = self._manager(tmp_path)
        manager._installed["p1"] = mpm._InstalledRecord(
            pack_id="p1", install_path=str(tmp_path), sha256="",
        )
        manager._save_manifest()
        old_text = manager._manifest_path.read_text(encoding="utf-8")

        manager._installed["p2"] = mpm._InstalledRecord(
            pack_id="p2", install_path=str(tmp_path), sha256="",
        )

        def boom(_fd):
            raise OSError("disk full")

        monkeypatch.setattr(mpm.os, "fsync", boom)
        with caplog.at_level(_logging.WARNING, logger=mpm.__name__):
            manager._save_manifest()  # 吞掉 OSError 但留痕
        # 旧 manifest 未被截断损坏
        assert manager._manifest_path.read_text(encoding="utf-8") == old_text
        assert any("manifest save failed" in r.message for r in caplog.records)
        # 无 .tmp 残留
        leftovers = [p for p in tmp_path.iterdir() if p.name.startswith(".manifest")]
        assert leftovers == []

    def test_corrupt_manifest_load_logs_warning(self, tmp_path, caplog) -> None:
        import logging as _logging

        import src.shared.ai_models.generation.model_pack_manager as mpm

        (tmp_path / "manifest.json").write_text("{corrupt", encoding="utf-8")
        with caplog.at_level(_logging.WARNING, logger=mpm.__name__):
            self._manager(tmp_path)
        # 反例断言（AUD-31617 修复点）：旧实现 debug 级静默
        assert any("manifest load failed" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# ai_models: AUD-31618 — default_cache_dir docstring / 根推导
# ---------------------------------------------------------------------------

class TestAud31618DefaultCacheDir:
    def test_docstring_matches_actual_location(self) -> None:
        from src.shared.ai_models.generation import model_pack_manager as mpm

        src_text = inspect.getsource(mpm.default_cache_dir)
        doc = mpm.default_cache_dir.__doc__ or ""
        # 反例断言（AUD-31618 修复点）：旧 docstring 描述 edit 侧旧路径
        assert "src/features/edit/ai_editing/model_pack_manager.py" not in doc
        assert "src/shared/ai_models/generation" in doc

    def test_cache_dir_resolves_to_project_models(self) -> None:
        from src.shared.ai_models.generation.model_pack_manager import default_cache_dir

        result = default_cache_dir()
        assert result.name == "ai_editing"
        assert result.parent.name == "models"
        project_root = result.parent.parent
        # 推导的项目根包含 src/（当前仓库布局），且实现按 src 锚定而非
        # parents[4] 硬编码深度（实现回退为 parents[4] 时今日恰好不坏，
        # 由 docstring 断言兜底）
        assert (project_root / "src").is_dir()

    def test_root_derivation_no_longer_sole_parents4(self) -> None:
        from src.shared.ai_models.generation import model_pack_manager as mpm

        src_text = inspect.getsource(mpm.default_cache_dir)
        assert "for _parent in _here.parents" in src_text
        assert 'name == "src"' in src_text


# ---------------------------------------------------------------------------
# ai_models: AUD-31619 — ModelPackManager 并发保护
# ---------------------------------------------------------------------------

class TestAud31619ModelPackManagerLock:
    def _manager(self, tmp_path: Path):
        from src.shared.ai_models.generation.model_pack_manager import (
            ModelPackLicense,
            ModelPackManager,
            ModelPackSpec,
        )

        spec = ModelPackSpec(
            pack_id="ocr", capability="ocr", version="1.0",
            size_bytes=1, sha256="", languages=(),
            requires_gpu=False, min_vram_mb=0,
            license=ModelPackLicense(
                name="MIT", commercial_use=True,
                redistribution=True, attribution_required=False,
            ),
        )
        install_dir = tmp_path / "ocr"
        install_dir.mkdir(exist_ok=True)
        return ModelPackManager(cache_dir=tmp_path, specs=(spec,)), install_dir

    def test_lock_present_and_mark_installed_roundtrip(self, tmp_path) -> None:
        from src.shared.ai_models.generation.model_pack_manager import ModelPackManager

        manager, install_dir = self._manager(tmp_path)
        assert isinstance(manager._lock, type(threading.RLock()))
        assert manager.mark_installed("ocr", install_dir, verify=False) is True
        assert manager.get_status("ocr").installed is True
        assert manager.remove("ocr") is True
        assert manager.get_status("ocr").installed is False

    def test_concurrent_mark_get_remove_no_crash(self, tmp_path) -> None:
        manager, install_dir = self._manager(tmp_path)
        errors: list[BaseException] = []

        def writer() -> None:
            try:
                for i in range(30):
                    manager.mark_installed("ocr", install_dir, verify=False)
                    manager.remove("ocr")
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        def reader() -> None:
            try:
                for _ in range(60):
                    manager.get_status("ocr")
                    manager.all_status()
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=writer) for _ in range(2)]
        threads += [threading.Thread(target=reader) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)
        # 反例断言（AUD-31619 修复点）：无锁时 _save_manifest 迭代期间
        # 并发变更 dict 会抛 RuntimeError: dictionary changed size
        assert errors == []


# ---------------------------------------------------------------------------
# ai_models: AUD-31710 — generated_media 缓存容量收敛
# ---------------------------------------------------------------------------

class TestAud31710GeneratedMediaPrune:
    def test_prune_respects_file_cap_oldest_first(self, tmp_path: Path) -> None:
        from src.shared.ai_models.generation.volcengine_generation_provider import (
            prune_generated_media_cache,
        )

        for i in range(5):
            p = tmp_path / f"item-{i}.png"
            p.write_bytes(b"x" * 10)
            os.utime(p, (1000 + i, 1000 + i))
        removed = prune_generated_media_cache(tmp_path, max_bytes=0, max_files=3)
        assert removed == 2  # 旧实现：无任何清理 → 返回 0，断言失败
        names = sorted(p.name for p in tmp_path.iterdir())
        assert names == ["item-2.png", "item-3.png", "item-4.png"]

    def test_prune_respects_byte_cap(self, tmp_path: Path) -> None:
        from src.shared.ai_models.generation.volcengine_generation_provider import (
            prune_generated_media_cache,
        )

        for i in range(4):
            p = tmp_path / f"audio-{i}.mp3"
            p.write_bytes(b"x" * 100)
            os.utime(p, (2000 + i, 2000 + i))
        removed = prune_generated_media_cache(tmp_path, max_bytes=250, max_files=0)
        assert removed == 2
        assert sum(p.stat().st_size for p in tmp_path.iterdir()) == 200

    def test_prune_skips_temp_files_and_below_budget(self, tmp_path: Path) -> None:
        from src.shared.ai_models.generation.volcengine_generation_provider import (
            prune_generated_media_cache,
        )

        tmp = tmp_path / ".inflight.tmp"
        tmp.write_bytes(b"x" * 10)
        keep = tmp_path / "keep.png"
        keep.write_bytes(b"x" * 10)
        assert prune_generated_media_cache(tmp_path, max_bytes=10_000, max_files=100) == 0
        assert tmp.exists() and keep.exists()

    def test_cache_writer_triggers_prune(self, tmp_path, monkeypatch) -> None:
        """_cache_b64 写入后自动触发清理（旧实现无调用）。"""
        import src.shared.ai_models.generation.volcengine_generation_provider as vgp

        monkeypatch.setattr(vgp, "default_cache_dir", lambda: tmp_path)
        calls: list[Path] = []
        real_prune = vgp.prune_generated_media_cache

        def spy(root: Path, **kwargs) -> int:
            calls.append(root)
            return real_prune(root, **kwargs)

        monkeypatch.setattr(vgp, "prune_generated_media_cache", spy)
        encoded = base64.b64encode(b"pngbytes").decode("ascii")
        handle = vgp.VolcengineSeedreamImageProvider._cache_b64(
            "req-1", encoded, suffix=".png",
        )
        assert Path(handle).is_file()
        assert calls == [Path(handle).parent]


# ---------------------------------------------------------------------------
# ai_models: AUD-31807 / AUD-31808 — inference_engine
# ---------------------------------------------------------------------------

class TestAud31807SessionCacheAndWorkers:
    def setup_method(self) -> None:
        from src.shared.ai_models import inference_engine as ie

        ie.AIInferenceWorker._session_cache.clear()
        ie.AIInferenceWorker._loading_locks.clear()

    def test_session_cache_lru_cap(self) -> None:
        from src.shared.ai_models import inference_engine as ie

        assert ie.AIInferenceWorker._SESSION_CACHE_MAX == 4
        for i in range(6):
            ie.AIInferenceWorker._cache_put(f"m{i}", object())
        cache = ie.AIInferenceWorker._session_cache
        # 反例断言（AUD-31807 修复点）：旧实现无上限（len == 6）
        assert len(cache) == 4
        assert "m0" not in cache and "m1" not in cache
        assert list(cache.keys()) == ["m2", "m3", "m4", "m5"]

    def test_cache_hit_is_lru_touched(self) -> None:
        from src.shared.ai_models import inference_engine as ie

        s0, s1 = object(), object()
        ie.AIInferenceWorker._cache_put("a", s0)
        ie.AIInferenceWorker._cache_put("b", s1)
        assert ie.AIInferenceWorker._cache_get("a") is s0  # 触碰 a → a 移到末尾
        # 填满并超出上限：最旧且未触碰的 b 应先被淘汰
        for name in ("c", "d", "e"):
            ie.AIInferenceWorker._cache_put(name, object())
        cache = ie.AIInferenceWorker._session_cache
        assert "b" not in cache
        assert "a" in cache  # 被触碰过，比 b 新
        assert len(cache) == ie.AIInferenceWorker._SESSION_CACHE_MAX

    def test_path_lock_is_per_path(self) -> None:
        from src.shared.ai_models import inference_engine as ie

        lock_a1 = ie.AIInferenceWorker._path_lock("a.onnx")
        lock_a2 = ie.AIInferenceWorker._path_lock("a.onnx")
        lock_b = ie.AIInferenceWorker._path_lock("b.onnx")
        assert lock_a1 is lock_a2
        assert lock_a1 is not lock_b

    def _flush_deferred_deletes(self, qapp) -> None:
        """processEvents 不一定处理 DeferredDelete，显式冲一次。"""
        from PyQt6.QtCore import QEvent

        qapp.processEvents()
        qapp.sendPostedEvents(None, QEvent.Type.DeferredDelete)

    def test_worker_deleteLater_on_error(self, qapp) -> None:
        import PyQt6.sip as sip

        from src.shared.ai_models import inference_engine as ie

        engine = ie.AIInferenceEngine()
        worker = ie.AIInferenceWorker("k", "ai_denoise", {}, {}, 0)
        engine._workers["k"] = worker
        engine._on_worker_error("k", "boom", 0)
        self._flush_deferred_deletes(qapp)
        # 反例断言（AUD-31807 修复点）：旧实现只 pop 不 deleteLater
        assert engine._workers == {}
        assert sip.isdeleted(worker)

    def test_worker_deleteLater_on_finished_and_prune(self, qapp) -> None:
        import PyQt6.sip as sip

        from src.shared.ai_models import inference_engine as ie

        engine = ie.AIInferenceEngine()
        worker = ie.AIInferenceWorker("k", "ai_denoise", {}, {}, 0)
        engine._workers["k"] = worker
        engine._on_worker_finished("k", np.zeros((1, 1, 3), dtype=np.float32), 0)
        self._flush_deferred_deletes(qapp)
        assert engine._workers == {}
        assert sip.isdeleted(worker)

        retired = ie.AIInferenceWorker("r", "ai_denoise", {}, {}, 0)
        # 未启动的 QThread isFinished() 恒为 False；真实回收路径里线程已
        # 结束。这里用桩值模拟"线程已结束"的退休 worker。
        retired.isFinished = lambda: True  # type: ignore[method-assign]
        engine._retired_workers.append(retired)
        engine._lock.lock()
        engine._prune_retired_workers_locked()
        engine._lock.unlock()
        self._flush_deferred_deletes(qapp)
        assert engine._retired_workers == []
        assert sip.isdeleted(retired)


class TestAud31808DeadCodeAndPreview:
    def test_unused_import_time_removed(self) -> None:
        import src.shared.ai_models.inference_engine as ie

        # 反例断言（AUD-31808 修复点）：旧实现 import time 未使用
        assert not hasattr(ie, "time")

    def test_run_inference_dead_params_removed(self) -> None:
        from src.shared.ai_models import inference_engine as ie

        params = list(inspect.signature(ie.AIInferenceWorker._run_inference).parameters)
        # 正例：仅 (self, image, mask, session)；反例：旧实现含 tile_size/overlap
        assert params == ["self", "image", "mask", "session"]

    def test_preview_result_is_stored_and_bounded(self, qapp) -> None:
        from src.shared.ai_models import inference_engine as ie

        engine = ie.AIInferenceEngine()
        img = np.zeros((2, 2, 3), dtype=np.float32)
        engine._on_worker_preview("k1", img, 0)
        stored = engine.latest_preview("k1")
        # 反例断言（AUD-31808 修复点）：旧实现 result 被丢弃、无访问器
        assert stored is not None
        np.testing.assert_array_equal(stored, img)

        for i in range(12):
            engine._on_worker_preview(f"k{i}", img, 0)
        assert len(engine._latest_previews) <= 8  # 有界

    def test_stale_preview_not_stored_and_cleared_on_finish(self, qapp) -> None:
        from src.shared.ai_models import inference_engine as ie

        engine = ie.AIInferenceEngine()
        img = np.zeros((2, 2, 3), dtype=np.float32)
        engine.set_latest_request_id(5)
        engine._on_worker_preview("k", img, 3)  # 过期请求
        assert engine.latest_preview("k") is None

        engine._on_worker_preview("k2", img, 5)
        assert engine.latest_preview("k2") is not None
        engine._on_worker_finished("k2", img, 5)
        assert engine.latest_preview("k2") is None  # run 终结清空


# ---------------------------------------------------------------------------
# ai_models: AUD-31810 — model_manager manifest 原子写
# ---------------------------------------------------------------------------

class TestAud31810ModelManagerManifest:
    def test_save_atomic_old_content_intact_until_replace(self, tmp_path, monkeypatch) -> None:
        import src.shared.ai_models.model_manager as mm

        manager = mm.AIModelManager(cache_dir=str(tmp_path))
        manifest_path = Path(manager._manifest_path)
        manager._manifest = {"nafnet_denoise": {"name": "nafnet.onnx"}}
        manager._save_manifest()
        old_text = manifest_path.read_text(encoding="utf-8")

        real_replace = os.replace
        observed: dict[str, str] = {}

        def spy_replace(src, dst):
            observed["at_replace"] = Path(dst).read_text(encoding="utf-8")
            return real_replace(src, dst)

        manager._manifest["lama_inpaint"] = {"name": "lama.onnx"}
        monkeypatch.setattr(mm.os, "replace", spy_replace)
        manager._save_manifest()
        # 反例断言（AUD-31810 修复点）：旧实现 truncate 写无 os.replace
        assert "at_replace" in observed
        assert "lama_inpaint" not in observed["at_replace"]
        final = json.loads(manifest_path.read_text(encoding="utf-8"))
        assert "lama_inpaint" in final

    def test_failed_save_leaves_manifest_intact(self, tmp_path, monkeypatch, caplog) -> None:
        import logging as _logging

        import src.shared.ai_models.model_manager as mm

        manager = mm.AIModelManager(cache_dir=str(tmp_path))
        manifest_path = Path(manager._manifest_path)
        manager._manifest = {"keep": {"name": "k.onnx"}}
        manager._save_manifest()
        old_text = manifest_path.read_text(encoding="utf-8")

        def boom(_f):
            raise OSError("disk full")

        monkeypatch.setattr(mm.os, "fsync", boom)
        with caplog.at_level(_logging.ERROR, logger=mm.__name__):
            manager._save_manifest()
        assert manifest_path.read_text(encoding="utf-8") == old_text
        assert any("Failed to save manifest" in r.message for r in caplog.records)
        assert not (tmp_path / "manifest.json.tmp").exists()


# ---------------------------------------------------------------------------
# ai_models: AUD-31811 — registry 锁 / 单例双检 / cancel_download 不阻塞
# ---------------------------------------------------------------------------

class TestAud31811ModelManagerConcurrency:
    def test_cancel_download_never_waits(self) -> None:
        """cancel_download 不再同步 wait(3000)（反例：旧实现 wait_calls==1）。"""
        import src.shared.ai_models.model_manager as mm

        manager = mm.AIModelManager.__new__(mm.AIModelManager)
        manager._download_workers = {}
        worker = SimpleNamespace(
            cancel=lambda: None, isRunning=lambda: True,
            wait=lambda t=0: True,
        )
        wait_calls: list[int] = []
        worker.cancel = lambda: None
        worker.wait = lambda t=0: (wait_calls.append(t), True)[1]
        manager._download_workers["m1"] = worker

        manager.cancel_download("m1")
        assert wait_calls == []
        assert "m1" in manager._download_workers  # 运行中：引用保留给 finished 处理器

    def test_cancel_download_pops_finished_worker(self) -> None:
        import src.shared.ai_models.model_manager as mm

        manager = mm.AIModelManager.__new__(mm.AIModelManager)
        manager._download_workers = {}
        worker = SimpleNamespace(
            cancel=lambda: None, isRunning=lambda: False, wait=lambda t=0: True,
        )
        manager._download_workers["m1"] = worker
        manager.cancel_download("m1")
        assert "m1" not in manager._download_workers

    def test_singleton_creation_is_double_checked(self, tmp_path, monkeypatch) -> None:
        import src.shared.ai_models.model_manager as mm

        original = mm._model_manager_instance
        mm._model_manager_instance = None
        construct_calls: list[int] = []
        real_manager = mm.AIModelManager

        class SlowManager(real_manager):
            def __init__(self, cache_dir=None):
                super().__init__(cache_dir=str(tmp_path))
                construct_calls.append(1)
                time.sleep(0.15)

        monkeypatch.setattr(mm, "AIModelManager", SlowManager)
        try:
            barrier = threading.Barrier(8)
            results: list[object] = []

            def getter() -> None:
                barrier.wait(timeout=5)
                results.append(mm.get_model_manager())

            threads = [threading.Thread(target=getter) for _ in range(8)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=30)
            # 反例断言（AUD-31811 修复点）：无锁单例会构造多次
            assert len(construct_calls) == 1
            assert all(r is results[0] for r in results)
        finally:
            mm._model_manager_instance = original

    def test_registry_lock_exists_and_guards_writes(self, tmp_path) -> None:
        import src.shared.ai_models.model_manager as mm

        assert isinstance(mm._REGISTRY_LOCK, type(threading.Lock()))
        manager = mm.AIModelManager(cache_dir=str(tmp_path))
        info = mm.MODEL_REGISTRY["nafnet_denoise"]
        target = tmp_path / "nafnet.onnx"
        target.write_bytes(b"x" * 16)
        assert manager.register_local_model("nafnet_denoise", str(target)) is True
        assert info.is_available is True
        manager.clear_cache()
        assert info.is_available is False


# ---------------------------------------------------------------------------
# ai_models: AUD-31815 — RIFE provider 配置 + 缓存锁
# ---------------------------------------------------------------------------

class TestAud31815RifeSessionConfig:
    def setup_method(self) -> None:
        from src.shared.ai_models import rife_interpolator as ri

        ri._SESSION_CACHE.clear()

    def test_session_uses_configured_providers_and_options(self, monkeypatch) -> None:
        import src.shared.ai_models.rife_interpolator as ri

        captured: dict[str, object] = {}

        class FakeSession:
            pass

        def fake_session(path, sess_options=None, providers=None):
            captured["providers"] = providers
            captured["sess_options"] = sess_options
            return FakeSession()

        import onnxruntime
        monkeypatch.setattr(onnxruntime, "InferenceSession", fake_session)
        monkeypatch.setattr(
            ri, "get_configured_providers",
            lambda: ["FakeGPUProvider", "CPUExecutionProvider"],
        )
        monkeypatch.setattr(ri, "get_session_options", lambda: "SENTINEL_OPTS")
        import src.shared.ai_models.model_manager as mm
        monkeypatch.setattr(
            mm, "get_model_manager",
            lambda: SimpleNamespace(get_model_path=lambda _id: "/fake/rife.onnx"),
        )

        interp = ri.RifeInterpolator()
        session = interp._resolve_session()
        assert isinstance(session, FakeSession)
        # 反例断言（AUD-31815 修复点）：旧实现硬编码 ["CPUExecutionProvider"]
        assert captured["providers"] == ["FakeGPUProvider", "CPUExecutionProvider"]
        assert captured["sess_options"] == "SENTINEL_OPTS"
        assert ri._SESSION_CACHE["/fake/rife.onnx"] is session
        assert interp.has_model() is True

    def test_cache_lock_and_single_creation_under_concurrency(self, monkeypatch) -> None:
        import src.shared.ai_models.rife_interpolator as ri

        assert isinstance(ri._SESSION_CACHE_LOCK, type(threading.Lock()))

        construct_calls: list[int] = []

        class FakeSession:
            def __init__(self) -> None:
                construct_calls.append(1)
                time.sleep(0.1)  # 拉大 check-then-act 窗口

        import onnxruntime
        monkeypatch.setattr(
            onnxruntime, "InferenceSession",
            lambda *a, **k: FakeSession(),
        )
        monkeypatch.setattr(
            ri, "get_configured_providers", lambda: ["CPUExecutionProvider"],
        )
        monkeypatch.setattr(ri, "get_session_options", lambda: None)
        import src.shared.ai_models.model_manager as mm
        monkeypatch.setattr(
            mm, "get_model_manager",
            lambda: SimpleNamespace(get_model_path=lambda _id: "/fake/rife.onnx"),
        )

        barrier = threading.Barrier(4)
        sessions: list[object] = []

        def resolve() -> None:
            barrier.wait(timeout=5)
            interp = ri.RifeInterpolator()
            sessions.append(interp._resolve_session())

        threads = [threading.Thread(target=resolve) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        # 反例断言（AUD-31815 修复点）：无锁缓存会各建一份会话
        assert len(construct_calls) == 1
        assert all(s is sessions[0] for s in sessions)

    def test_resolve_failure_returns_none(self, monkeypatch) -> None:
        import src.shared.ai_models.rife_interpolator as ri
        import src.shared.ai_models.model_manager as mm

        monkeypatch.setattr(
            mm, "get_model_manager",
            lambda: SimpleNamespace(get_model_path=lambda _id: None),
        )
        assert ri.RifeInterpolator().has_model() is False
