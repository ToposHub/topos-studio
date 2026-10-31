"""EF-04 子运行 1/2 修复验证（AUD-12704 … AUD-14105，19 条 Suggestion 级）。

每条审计发现至少一个正例（修复后的正确行为）+ 一个反例（旧缺陷场景，
修复回退时必然失败）。不依赖 GPU / Qt / 真实 AI 服务；线程相关测试用
可控的 fake 注入，绝不等待真实网络。
"""

from __future__ import annotations

import hashlib
import inspect
import json
import threading
import time
from collections import deque
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from src.features.edit.ai_editing.agent_metrics import AgentMetricsCollector
from src.features.edit.ai_editing.agent_models import AgentTask
from src.features.edit.ai_editing.agent_orchestrator import AgentOrchestrator
from src.features.edit.ai_editing.analysis_service import AnalysisService
from src.features.edit.ai_editing.audit_store import AuditEntry, AuditStore
from src.features.edit.ai_editing.capability_catalog import CapabilityCatalogEntry
from src.features.edit.ai_editing.edit_capability_plugin import EditCapabilityDescriptor
from src.features.edit.ai_editing.evidence_kinds import (
    DomainSnapshot,
    ExtendedProjectSnapshot,
)
from src.features.edit.ai_editing.generation_bin import GenerationBin
from src.features.edit.ai_editing.generation_models import (
    GeneratedAsset,
    GenerationModality,
    GenerationProvenance,
)
from src.features.edit.ai_editing.llm_cost import CostLedger, make_cost_record
from src.features.edit.ai_editing.mcp_plugin import McpPluginDescriptor, McpPluginHost
from src.features.edit.ai_editing.mcp_vision_gateway import McpVisionGateway
from src.features.edit.ai_editing.media_generation_service import GenerationService
from src.features.edit.ai_editing.ocr_provider import OcrProvider
from src.features.edit.ai_editing.plan_repository import PlanRepository


# ---------------------------------------------------------------------- #
# 共享小工厂
# ---------------------------------------------------------------------- #


def _task(session_id: str = "s1") -> AgentTask:
    return AgentTask(session_id=session_id, title="m")


def _generated_asset(asset_id: str, handle: str, expected_sha256: str = "") -> GeneratedAsset:
    return GeneratedAsset(
        asset_id=asset_id,
        request_id="req-1",
        modality=GenerationModality.VIDEO,
        provenance=GenerationProvenance(provider_id="test", model_id="m1"),
        preview_handle=handle,
        expected_sha256=expected_sha256,
    )


def _is_rlock(obj: Any) -> bool:
    """threading.RLock() 是工厂函数，isinstance 必须用其返回类型。"""
    return isinstance(obj, type(threading.RLock()))


class _Response:
    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    @property
    def headers(self) -> dict[str, str]:
        return {"Content-Length": str(len(self._payload))}

    def read(self, size: int = -1) -> bytes:
        if not self._payload:
            return b""
        chunk, self._payload = self._payload[:size], self._payload[size:]
        return chunk

    def __enter__(self) -> "_Response":
        return self

    def __exit__(self, *_args: Any) -> None:
        return None


# ---------------------------------------------------------------------- #
# AUD-12704：AgentMetricsCollector._trackers 无界增长
# ---------------------------------------------------------------------- #


class TestAud12704TrackerEviction:
    def test_terminal_trackers_bounded_and_oldest_evicted(self) -> None:
        orch = AgentOrchestrator()
        collector = AgentMetricsCollector(max_terminal_trackers=2)
        collector.attach(orch)
        run_ids = []
        for index in range(3):
            run_id = orch.start_task(_task(f"s{index}"))
            run_ids.append(run_id)
            orch.fail(run_id)
        # 三个终态 run，最多保留 2 个 tracker（最旧的 run 被淘汰）
        assert len(collector._trackers) == 2
        assert collector.get_metrics(run_ids[1]) is not None
        assert collector.get_metrics(run_ids[2]) is not None
        assert collector.get_metrics(run_ids[0]) is None

    def test_in_flight_runs_are_never_evicted(self) -> None:
        orch = AgentOrchestrator()
        collector = AgentMetricsCollector(max_terminal_trackers=1)
        collector.attach(orch)
        active = orch.start_task(_task("active"))
        for index in range(3):
            run_id = orch.start_task(_task(f"done{index}"))
            orch.fail(run_id)
        # 淘汰只针对终态 tracker；进行中的 run 必须保留
        assert collector.get_metrics(active) is not None
        assert len(collector._trackers) == 2

    def test_prune_returns_evicted_count(self) -> None:
        orch = AgentOrchestrator()
        collector = AgentMetricsCollector()
        collector.attach(orch)
        run_ids = []
        for index in range(4):
            run_id = orch.start_task(_task(f"s{index}"))
            run_ids.append(run_id)
            orch.fail(run_id)
        evicted = collector.prune(keep_last_n=1)
        assert evicted == 3
        assert len(collector._trackers) == 1
        assert collector.get_metrics(run_ids[3]) is not None

    def test_invalid_bound_rejected(self) -> None:
        with pytest.raises(ValueError):
            AgentMetricsCollector(max_terminal_trackers=0)


# ---------------------------------------------------------------------- #
# AUD-12907：search_assets 每段新建 SemanticIndex
# ---------------------------------------------------------------------- #


class TestAud12907SharedProbeIndex:
    def _svc_with_two_assets(self) -> AnalysisService:
        from src.features.edit.ai_editing.editorial_models import (
            FrameRange,
            SegmentKind,
            SemanticSegment,
        )

        svc = AnalysisService()

        def _seg(seg_id: str, asset_id: str, kind: Any, start: int, payload: dict[str, Any]) -> Any:
            return SemanticSegment(
                segment_id=seg_id, asset_id=asset_id,
                source_range=FrameRange(start, start + 10), kind=kind,
                payload=payload, confidence=0.9,
            )

        record_a = SimpleNamespace(segments=(
            _seg("seg-a1", "a1", SegmentKind.TRANSCRIPT, 0, {"text": "产品卖点非常突出"}),
            _seg("seg-a2", "a1", SegmentKind.SHOT, 100, {"description": "空镜头"}),
        ))
        record_b = SimpleNamespace(segments=(
            _seg("seg-b1", "b1", SegmentKind.TRANSCRIPT, 0, {"text": "完全无关的转录"}),
        ))
        with svc._state_lock:
            svc._records = {"a1": record_a, "b1": record_b}
        return svc

    def test_text_filter_matches_expected_segments_only(self) -> None:
        svc = self._svc_with_two_assets()
        results = svc.search_assets(text="产品")
        assert [s.segment_id for s in results] == ["seg-a1"]
        all_results = svc.search_assets()
        assert len(all_results) == 3

    def test_kind_filter_and_limit_still_work(self) -> None:
        from src.features.edit.ai_editing.editorial_models import SegmentKind

        svc = self._svc_with_two_assets()
        assert [s.segment_id for s in svc.search_assets(kind=SegmentKind.SHOT)] == ["seg-a2"]
        assert len(svc.search_assets(limit=2)) == 2

    def test_search_builds_single_probe_not_per_segment(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """修复回退（每段新建 SemanticIndex）时本测试必须失败。"""
        from src.features.edit.ai_editing import semantic_index

        svc = self._svc_with_two_assets()
        calls: list[Any] = []
        original_init = semantic_index.SemanticIndex.__init__

        def counting_init(self: Any) -> None:
            calls.append(1)
            original_init(self)

        monkeypatch.setattr(semantic_index.SemanticIndex, "__init__", counting_init)
        svc.search_assets(text="产品")
        assert len(calls) == 1  # 整轮搜索只构建一个共享探针索引


# ---------------------------------------------------------------------- #
# AUD-13100：AuditStore 全程无锁
# ---------------------------------------------------------------------- #


class TestAud13100AuditStoreLock:
    def test_store_declares_rlock(self) -> None:
        store = AuditStore()
        assert _is_rlock(store._lock)

    def test_concurrent_record_keeps_every_entry(self) -> None:
        store = AuditStore(max_entries=10000)
        errors: list[BaseException] = []

        def worker(thread_index: int) -> None:
            try:
                for i in range(50):
                    store.record(AuditEntry(
                        timestamp=0.0, tool=f"t{thread_index}-{i}",
                        plan_id=f"p{thread_index}-{i}", applied=True,
                        new_revision=1,
                    ))
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(t,)) for t in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert not errors
        assert len(store.list_entries()) == 8 * 50
        # export_all 快照一致性：条目数与内存一致
        exported = store.export_all()
        assert exported is not None and len(exported["entries"]) == 8 * 50

    def test_concurrent_read_during_record_no_runtime_error(self) -> None:
        store = AuditStore()
        for i in range(64):
            store.record(AuditEntry(0.0, "seed", f"p{i}", True, None))
        errors: list[BaseException] = []

        def reader() -> None:
            # 有界读取循环 + 礼让 sleep：并发压测但不依赖调度无限等待。
            try:
                for _ in range(200):
                    store.list_entries()
                    store.export_all()
                    time.sleep(0)
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        readers = [threading.Thread(target=reader) for _ in range(3)]
        for r in readers:
            r.start()
        for i in range(200):
            store.record(AuditEntry(0.0, "x", f"x{i}", True, None))
        for r in readers:
            r.join(timeout=30)
            assert not r.is_alive()
        assert not errors


# ---------------------------------------------------------------------- #
# AUD-13104：紧急停用分支 revoke 抛异常逃逸 + grant 泄漏
# ---------------------------------------------------------------------- #


class _AutoHost:
    """与 test_autonomous_agent.py 相同形状的最小 host fake。"""

    def __init__(self) -> None:
        from src.features.edit.ai_editing.approval_service import ApprovalService

        self.revision = 4
        self.commit_revision = 5
        self.approvals = ApprovalService()
        self.committed = False
        self.selected_asset_ids = ("a1",)
        self.revoke_failures: list[BaseException] = []

    def get_project_id(self) -> str:
        return "p1"

    def get_project_sequences(self) -> Any:
        return SimpleNamespace(
            project=SimpleNamespace(project_id="p1"),
            sequences={"t1": SimpleNamespace(revision=self.revision)},
        )

    def check_ai_action(self, _action: Any, **_kwargs: Any) -> Any:
        return SimpleNamespace(allowed=True, reason="")

    def issue_automatic_plan_approval(
        self, plan: Any, preview: Any, *, asset_ids: tuple[str, ...] = (),
    ) -> Any:
        from src.features.edit.ai_editing.transaction_service import (
            _expected_commit_scope,
        )

        return self.approvals.issue_after_user_confirmation(
            _expected_commit_scope(plan, diff_hash=getattr(preview, "diff_hash", None)),
            issued_by="restricted-automatic",
        )

    def approval_service(self) -> Any:
        return self.approvals

    def get_selected_asset_ids(self) -> tuple[str, ...]:
        return self.selected_asset_ids

    def commit_ai_command(self, _tid: str, _macro: Any, *, undo_label: str) -> Any:
        self.committed = True
        return SimpleNamespace(revision=self.commit_revision, undo_label=undo_label)

    def rollback_ai_command(self, _tid: str, **_kw: Any) -> Any:
        return SimpleNamespace(revision=self.revision)


def _auto_agent(host: _AutoHost) -> Any:
    from src.features.edit.ai_editing.autonomous_agent import (
        AutonomousAgent,
        AutonomousAgentConfig,
    )

    compiled = SimpleNamespace(macro=object(), undo_label="AI 自动剪辑")
    return AutonomousAgent(
        host,
        preview_plan=lambda _plan, _state: SimpleNamespace(
            has_errors=False, compiled=compiled, diff_hash="diff-1",
        ),
        compile_plan=lambda _plan: compiled,
        config=AutonomousAgentConfig(enabled=True),
    )


def _auto_plan() -> Any:
    from src.features.edit.ai_editing.models import (
        EditOperation,
        EditPlan,
        OperationKind,
        PlanTarget,
    )

    return EditPlan.create(
        plan_id="ef04-plan",
        target=PlanTarget(project_id="p1", timeline_id="t1"),
        base_revision=4,
        title="自动剪辑",
        operations=(EditOperation(
            op_id="op-1", kind=OperationKind.ADD_CLIP, params={"asset_id": "a1"},
        ),),
    )


class TestAud13104RevokeProtection:
    def test_emergency_disable_returns_blocked_and_revokes(self) -> None:
        host = _AutoHost()
        host.is_autonomous_mode_enabled = lambda: False
        result = _auto_agent(host).apply(_auto_plan())
        assert result.applied is False
        assert result.code == "AUTO_MODE_DISABLED"
        assert host.committed is False
        assert host.approvals.active_count() == 0

    def test_revoke_failure_does_not_escape_apply(self) -> None:
        """修复回退（裸 revoke）时本测试必须失败：异常会逃出 apply()。"""
        host = _AutoHost()
        host.is_autonomous_mode_enabled = lambda: False

        def _boom(_grant_id: str) -> None:
            raise RuntimeError("revoke exploded")

        host.approvals.revoke = _boom  # type: ignore[method-assign]
        result = _auto_agent(host).apply(_auto_plan())
        assert result.code == "AUTO_MODE_DISABLED"
        assert host.committed is False

    def test_live_checker_exception_still_revokes(self) -> None:
        host = _AutoHost()

        def _broken_check() -> bool:
            raise RuntimeError("safety check exploded")

        host.is_autonomous_mode_enabled = _broken_check
        result = _auto_agent(host).apply(_auto_plan())
        assert result.code == "AUTO_MODE_DISABLED"
        assert host.approvals.active_count() == 0


# ---------------------------------------------------------------------- #
# AUD-13112：capability_catalog.remove() 可删核心条目
# ---------------------------------------------------------------------- #


class TestAud13112RemoveCoreGuard:
    def _catalog(self) -> Any:
        from src.features.edit.ai_editing.capability_catalog import CapabilityCatalog

        catalog = CapabilityCatalog()
        catalog.register(CapabilityCatalogEntry(
            name="core.tool", domain="system", permission="read", description="core",
        ))
        catalog.register(CapabilityCatalogEntry(
            name="plugin.tool", domain="system", permission="read", description="ext",
            owner_plugin_id="plug-1",
        ))
        return catalog

    def test_core_entry_not_removable(self) -> None:
        catalog = self._catalog()
        assert catalog.remove("core.tool") is False
        assert catalog.remove("core.tool", owner_plugin_id="plug-1") is False
        assert catalog.get("core.tool") is not None

    def test_extension_entry_removed_only_by_owner(self) -> None:
        catalog = self._catalog()
        assert catalog.remove("plugin.tool") is False  # 未声明归属
        assert catalog.remove("plugin.tool", owner_plugin_id="plug-2") is False
        assert catalog.remove("plugin.tool", owner_plugin_id="plug-1") is True
        assert catalog.get("plugin.tool") is None

    def test_remove_missing_entry_returns_false(self) -> None:
        assert self._catalog().remove("nope", owner_plugin_id="x") is False


# ---------------------------------------------------------------------- #
# AUD-13221：SubagentPluginHost._child_agents 只增不减
# ---------------------------------------------------------------------- #


class TestAud13221ChildAgentsBound:
    def _host(self, max_child_agents: int) -> Any:
        from src.features.ai_harness.subagent_runtime import SubagentRegistry
        from src.features.edit.ai_editing.composition_plugin import (
            SubagentDefinition,
            SubagentPluginHost,
            SubagentSupervisor,
        )

        host = SubagentPluginHost(
            SubagentSupervisor(SubagentRegistry()),
            max_child_agents=max_child_agents,
        )
        host.register(SubagentDefinition(
            agent_id="researcher",
            display_name="Researcher",
            runner=lambda request: "evidence",
            continuable=True,
        ))
        return host

    def test_child_map_stays_bounded(self) -> None:
        host = self._host(max_child_agents=2)
        child_ids = [
            host.run(
                parent_run_id=f"p{i}", session_id="s1",
                agent_id="researcher", prompt="go",
            ).child_run_id
            for i in range(3)
        ]
        assert len(host._child_agents) == 2
        # 最旧的映射被淘汰，最新的保留（continue_child 查找表可用）
        assert child_ids[0] not in host._child_agents
        assert child_ids[2] in host._child_agents

    def test_continue_child_still_resolves_newest(self) -> None:
        host = self._host(max_child_agents=2)
        result = host.run(
            parent_run_id="p", session_id="s1",
            agent_id="researcher", prompt="go",
        )
        assert host.continue_child(result.child_run_id, "more").status == "succeeded"

    def test_invalid_bound_rejected(self) -> None:
        from src.features.ai_harness.subagent_runtime import SubagentRegistry
        from src.features.edit.ai_editing.composition_plugin import (
            SubagentPluginHost,
            SubagentSupervisor,
        )

        with pytest.raises(ValueError):
            SubagentPluginHost(
                SubagentSupervisor(SubagentRegistry()), max_child_agents=0,
            )


# ---------------------------------------------------------------------- #
# AUD-13405：display_name 未做类型归一化
# ---------------------------------------------------------------------- #


class TestAud13405DisplayNameNormalization:
    def test_non_string_display_name_is_normalized(self) -> None:
        descriptor = EditCapabilityDescriptor(
            plugin_id="p1", display_name=123,  # type: ignore[arg-type]
        )
        # to_dict 的 [:200] 切片不再崩溃
        payload = descriptor.to_dict()
        assert payload["display_name"] == "123"
        assert isinstance(descriptor.display_name, str)

    def test_empty_display_name_still_rejected(self) -> None:
        with pytest.raises(ValueError):
            EditCapabilityDescriptor(plugin_id="p1", display_name="   ")

    def test_normal_string_roundtrip_preserved(self) -> None:
        descriptor = EditCapabilityDescriptor(plugin_id="p1", display_name="  My Plugin ")
        assert descriptor.display_name == "My Plugin"


# ---------------------------------------------------------------------- #
# AUD-13504：缓存命中仍全文重算 sha256
# ---------------------------------------------------------------------- #


class TestAud13504CacheReuseSkipsRehash:
    def _materializer(self, cache_dir: Path, payload: bytes) -> Any:
        from src.features.edit.ai_editing.generated_media_materializer import (
            GeneratedMediaMaterializer,
        )

        return GeneratedMediaMaterializer(
            cache_dir=cache_dir,
            opener=lambda _request, **_kw: _Response(payload),
            timeout=3.0,
        )

    def test_second_reuse_skips_full_rehash(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """修复回退（复用时全文重哈希）时本测试必须失败。"""
        from src.features.edit.ai_editing import generated_media_materializer as gmm

        payload = b"downloaded-media-bytes"
        materializer = self._materializer(tmp_path / "cache", payload)
        asset = _generated_asset("gen:ef04/one", "https://provider.example/out/video.mp4")

        first = materializer.materialize(asset)
        assert first.succeeded

        # 首次下载允许调用 _with_metadata；之后的缓存复用必须走 sidecar。
        original = gmm.GeneratedMediaMaterializer._with_metadata

        def _no_rehash_after_download(*args: Any, **kwargs: Any) -> Any:
            raise AssertionError("cache reuse must not re-hash the whole file")

        monkeypatch.setattr(
            gmm.GeneratedMediaMaterializer, "_with_metadata",
            staticmethod(_no_rehash_after_download),
        )
        try:
            cached = materializer.materialize(asset)
        finally:
            monkeypatch.setattr(
                gmm.GeneratedMediaMaterializer, "_with_metadata",
                staticmethod(original),
            )
        assert cached.succeeded
        assert cached.path == first.path
        assert cached.sha256 == hashlib.sha256(payload).hexdigest()
        assert cached.size_bytes == len(payload)

    def test_sidecar_size_mismatch_falls_back_to_rehash(self, tmp_path: Path) -> None:
        from src.features.edit.ai_editing.generated_media_materializer import (
            GeneratedMediaMaterializer,
        )

        payload = b"downloaded-media-bytes"
        materializer = self._materializer(tmp_path / "cache", payload)
        asset = _generated_asset(
            "gen:ef04/two", "https://provider.example/out/video.mp4",
            expected_sha256=hashlib.sha256(payload).hexdigest(),
        )
        assert materializer.materialize(asset).succeeded

        # 篡改缓存内容（size 变化）→ sidecar size 不再可信 → 回退全文重算。
        # checksum 不匹配即 fail-closed 删除坏缓存并重新下载（语义与修复前
        # 一致）；绝不能把损坏字节当作缓存命中返回。
        target = next(
            p for p in (tmp_path / "cache").iterdir() if p.suffix == ".mp4"
        )
        target.write_bytes(b"corrupted!")
        result = materializer.materialize(asset)
        if result.succeeded:
            # 重新下载成功：内容必须回到与 expected 一致的字节
            assert Path(str(result.path)).read_bytes() == payload
            assert result.sha256 == hashlib.sha256(payload).hexdigest()
        else:
            assert "checksum" in result.message
            assert not target.exists()

    def test_sidecar_absent_falls_back_to_rehash(self, tmp_path: Path) -> None:
        from src.features.edit.ai_editing.generated_media_materializer import (
            GeneratedMediaMaterializer,
        )

        payload = b"other-bytes"
        materializer = self._materializer(tmp_path / "cache", payload)
        asset = _generated_asset("gen:ef04/three", "https://provider.example/out/video.mp4")
        first = materializer.materialize(asset)
        assert first.succeeded
        GeneratedMediaMaterializer._sidecar_path(Path(str(first.path))).unlink()
        cached = materializer.materialize(asset)
        assert cached.succeeded
        assert cached.sha256 == hashlib.sha256(payload).hexdigest()

    def test_failed_download_leaves_no_sidecar(self, tmp_path: Path) -> None:
        from src.features.edit.ai_editing.generated_media_materializer import (
            GeneratedMediaMaterializer,
        )

        materializer = GeneratedMediaMaterializer(
            cache_dir=tmp_path / "cache", max_bytes=4,
            opener=lambda _request, **_kw: _Response(b"0123456789"),
        )
        result = materializer.materialize(
            _generated_asset("gen:ef04/four", "https://provider.example/out/video.mp4"),
        )
        assert result.succeeded is False
        assert list((tmp_path / "cache").glob("*")) == []


# ---------------------------------------------------------------------- #
# AUD-13505：GenerationBin 无锁无线程契约
# ---------------------------------------------------------------------- #


class TestAud13505GenerationBinThreadContract:
    def test_bin_declares_rlock_and_thread_safe_doc(self) -> None:
        assert _is_rlock(GenerationBin()._lock)
        assert "线程安全" in (GenerationBin.__doc__ or "")

    def test_concurrent_mutation_and_iteration_no_runtime_error(self) -> None:
        bin_ = GenerationBin()
        assets = tuple(
            _generated_asset(f"gen-{i}", "stub://x") for i in range(200)
        )
        errors: list[BaseException] = []

        def reader() -> None:
            try:
                for _ in range(200):
                    bin_.all_assets()
                    bin_.pending_preview()
                    time.sleep(0)
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        readers = [threading.Thread(target=reader) for _ in range(3)]
        for r in readers:
            r.start()
        for chunk in (assets[i:i + 10] for i in range(0, len(assets), 10)):
            bin_.add_candidates(chunk)
        for r in readers:
            r.join(timeout=30)
            assert not r.is_alive()
        assert not errors
        assert bin_.count == 200

    def test_state_machine_semantics_preserved(self) -> None:
        bin_ = GenerationBin()
        asset = _generated_asset("gen-1", "stub://x")
        bin_.add_candidates((asset,))
        previewed = bin_.mark_previewed("gen-1")
        assert previewed.state.value == "previewed"
        discarded = bin_.discard("gen-1")
        assert discarded is not None and discarded.state.value == "discarded"
        assert bin_.clear_discarded() == 1


# ---------------------------------------------------------------------- #
# AUD-13508：fingerprint() 未纳入 selection
# ---------------------------------------------------------------------- #


def _snapshot(selection: dict[str, Any]) -> ExtendedProjectSnapshot:
    return ExtendedProjectSnapshot(
        project_id="p1", timeline_id="t1", base_revision=1,
        domains=(DomainSnapshot(domain="editorial", revision=1, fingerprint="h1"),),
        selection=selection,
    )


class TestAud13508FingerprintIncludesSelection:
    def test_selection_change_changes_fingerprint(self) -> None:
        base = _snapshot({})
        changed = _snapshot({"clips": ["clip-1"]})
        assert base.fingerprint() != changed.fingerprint()

    def test_fingerprint_stable_for_same_selection(self) -> None:
        assert _snapshot({"clips": ["clip-1"]}).fingerprint() == _snapshot(
            {"clips": ["clip-1"]},
        ).fingerprint()

    def test_docstring_claim_matches_behavior(self) -> None:
        doc = inspect.getdoc(ExtendedProjectSnapshot.fingerprint) or ""
        assert "selection" in doc.lower()


# ---------------------------------------------------------------------- #
# AUD-13607：SeedanceVideoProviderPack._jobs/_requests 无锁
# ---------------------------------------------------------------------- #


class _SeedTransport:
    def create(self, _payload: Any) -> dict[str, Any]:
        return {"id": "remote-1", "status": "queued"}

    def poll(self, _job_id: str) -> dict[str, Any]:
        return {"status": "running"}

    def cancel(self, _job_id: str) -> dict[str, Any]:
        return {"status": "cancelled"}


class TestAud13607SeedancePackLock:
    def _pack(self) -> Any:
        from src.features.edit.ai_editing.generation_models import (
            GenerationConsent,
            GenerationRequest,
        )
        from src.features.edit.ai_editing.generation_provider_pack import (
            SeedanceVideoProviderPack,
        )

        request = GenerationRequest(
            request_id="r-ef04", modality=GenerationModality.VIDEO,
            prompt="产品特写", consent=GenerationConsent(
                confirmed=True, provider_id="seedance",
                cost_acknowledged=True, upload_acknowledged=True,
                copyright_acknowledged=True,
            ),
        )
        return SeedanceVideoProviderPack(_SeedTransport()), request

    def test_pack_declares_state_lock(self) -> None:
        pack, _ = self._pack()
        assert _is_rlock(pack._state_lock)

    def test_concurrent_poll_and_cancel_no_error(self) -> None:
        """修复回退（无锁）时并发读写易触发竞态；本测试在修复后必须稳定通过。"""
        pack, request = self._pack()
        job = pack.create(request)
        errors: list[BaseException] = []

        def poller() -> None:
            try:
                for _ in range(50):
                    pack.poll(job.job_id)
                    time.sleep(0)
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        def canceller() -> None:
            try:
                for _ in range(50):
                    pack.cancel(job.job_id)
                    time.sleep(0)
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=poller) for _ in range(2)]
        threads += [threading.Thread(target=canceller) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)
            assert not t.is_alive()
        assert not errors
        with pack._state_lock:
            assert job.job_id in pack._jobs
            assert job.job_id in pack._requests


# ---------------------------------------------------------------------- #
# AUD-13608：ProviderRegistry 无锁
# ---------------------------------------------------------------------- #


class TestAud13608RegistryLock:
    def _registry(self) -> Any:
        from src.features.edit.ai_editing.generation_provider import ProviderRegistry
        from src.features.edit.ai_editing.generation_provider_pack import (
            ContractMockProviderPack,
            ProviderPackGenerationAdapter,
        )

        return ProviderRegistry(), ProviderPackGenerationAdapter(
            ContractMockProviderPack(), poll_interval_seconds=0.0,
        )

    def test_registry_declares_rlock(self) -> None:
        registry, _ = self._registry()
        assert _is_rlock(registry._lock)

    def test_concurrent_register_and_lookup_no_runtime_error(self) -> None:
        registry, adapter = self._registry()
        from src.features.edit.ai_editing.generation_models import (
            GenerationConsent,
            GenerationModality,
            GenerationRequest,
        )

        errors: list[BaseException] = []

        def lookup() -> None:
            # 有界循环 + 礼让 sleep：并发压测但保证测试确定性终止。
            try:
                for _ in range(300):
                    registry.list_providers()
                    registry.preferred_for(GenerationModality.IMAGE)
                    time.sleep(0)
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        def registrar(index: int) -> None:
            try:
                for i in range(5):
                    registry.register(adapter, preferred=(i == 0))
                    request = GenerationRequest(
                        request_id=f"r-{index}-{i}",
                        modality=GenerationModality.IMAGE, prompt="x",
                        consent=GenerationConsent(
                            confirmed=True, provider_id="pack-contract-mock",
                            cost_acknowledged=True, upload_acknowledged=True,
                            copyright_acknowledged=True,
                        ),
                    )
                    adapter.generate(request)
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        lookups = [threading.Thread(target=lookup) for _ in range(2)]
        writers = [threading.Thread(target=registrar, args=(i,)) for i in range(2)]
        for t in lookups + writers:
            t.start()
        for t in lookups + writers:
            t.join(timeout=60)
            assert not t.is_alive()
        assert not errors
        assert registry.count == 1  # 同 provider_id 幂等覆盖


# ---------------------------------------------------------------------- #
# AUD-13612：_feature_enabled 重复调用 list_features()
# ---------------------------------------------------------------------- #


class _CountingSources:
    def __init__(self) -> None:
        self.feature_calls = 0

    def list_diagnostics(self) -> list[dict[str, Any]]:
        return []

    def list_shortcuts(self) -> list[dict[str, Any]]:
        return [
            {"id": f"sc.{i}", "label": f"s{i}", "category": "c",
             "shortcut": "K", "feature_id": "color"}
            for i in range(5)
        ]

    def list_capabilities(self) -> dict[str, Any]:
        return {}

    def list_features(self) -> list[dict[str, Any]]:
        self.feature_calls += 1
        return [{"feature_id": "color", "enabled": True, "label": "调色"}]

    def list_panels(self) -> list[dict[str, Any]]:
        return [
            {"panel_id": f"panel.{i}", "title": f"p{i}", "category": "c",
             "page_ids": (), "feature_id": "color", "enabled": True}
            for i in range(5)
        ]


class TestAud13612FeatureMapSingleFetch:
    def test_build_calls_list_features_exactly_once(self) -> None:
        from src.features.edit.ai_editing.help_index import HelpIndex

        sources = _CountingSources()
        index = HelpIndex.build(sources)
        # 1 次预构建映射 + 1 次 _add_features 自身的条目枚举。修复回退
        # （每条 shortcut/panel 各调一次）时这里会是 12 次。
        assert sources.feature_calls == 2
        assert index.count == 11  # 5 shortcuts + 5 panels + 1 feature 条目

    def test_disabled_feature_marks_entries_disabled(self) -> None:
        from src.features.edit.ai_editing.help_index import HelpIndex

        sources = _CountingSources()
        sources.list_features = lambda: [  # type: ignore[method-assign]
            {"feature_id": "color", "enabled": False, "label": "调色"},
        ]
        index = HelpIndex.build(sources)
        assert index.get("shortcut:sc.0") is not None
        assert index.get("shortcut:sc.0").enabled is False
        assert index.get("panel:panel.0").enabled is False

    def test_unknown_feature_assumed_enabled(self) -> None:
        from src.features.edit.ai_editing.help_index import HelpIndex

        sources = _CountingSources()
        index = HelpIndex.build(sources)
        # feature_map 里没有的 id → 假定可用（与旧行为一致）
        assert HelpIndex._feature_enabled({"color": True}, "mystery") is True
        assert HelpIndex._feature_enabled({}, None) is True


# ---------------------------------------------------------------------- #
# AUD-13707：can_spend + record 非原子
# ---------------------------------------------------------------------- #


class TestAud13707TryReserve:
    def test_try_reserve_records_when_within_limit(self) -> None:
        ledger = CostLedger()
        record = make_cost_record(
            request_id="r1", provider_kind="api", model_id="m",
            request_type="chat", total_tokens=10, cost_units=0.4,
        )
        assert ledger.try_reserve(record, limit=1.0) is True
        assert ledger.total_cost() == pytest.approx(0.4)

    def test_try_reserve_rejects_and_does_not_record(self) -> None:
        ledger = CostLedger()
        first = make_cost_record(
            request_id="r1", provider_kind="api", model_id="m",
            request_type="chat", total_tokens=1, cost_units=0.7,
        )
        second = make_cost_record(
            request_id="r2", provider_kind="api", model_id="m",
            request_type="chat", total_tokens=1, cost_units=0.7,
        )
        assert ledger.try_reserve(first, limit=1.0) is True
        assert ledger.try_reserve(second, limit=1.0) is False
        assert ledger.total_cost() == pytest.approx(0.7)
        assert len(ledger.records()) == 1

    def test_try_reserve_unlimited_always_records(self) -> None:
        ledger = CostLedger()
        record = make_cost_record(
            request_id="r1", provider_kind="api", model_id="m",
            request_type="chat", total_tokens=1, cost_units=99.0,
        )
        assert ledger.try_reserve(record, limit=None) is True

    def test_hard_budget_under_concurrency(self) -> None:
        """修复回退（无 try_reserve）时本测试必然 AttributeError 失败。"""
        ledger = CostLedger()
        successes: list[bool] = []
        lock = threading.Lock()

        def worker() -> None:
            record = make_cost_record(
                request_id="r", provider_kind="api", model_id="m",
                request_type="chat", total_tokens=1, cost_units=0.6,
            )
            ok = ledger.try_reserve(record, limit=1.0)
            with lock:
                successes.append(ok)

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert sum(successes) == 1  # 硬预算：只允许一个请求入账
        assert ledger.total_cost() == pytest.approx(0.6)


# ---------------------------------------------------------------------- #
# AUD-13830：McpPluginHost._servers 无锁无清理
# ---------------------------------------------------------------------- #


class _McpFacade:
    def list_tools(self, **_: Any) -> list[Any]:
        return []

    def invoke(self, *_: Any, **__: Any) -> tuple[dict[str, Any], None]:
        return {"ok": True}, None


def _descriptor(plugin_id: str) -> McpPluginDescriptor:
    return McpPluginDescriptor(
        plugin_id=plugin_id, display_name=f"Plugin {plugin_id}",
        data_policy="local",
    )


class TestAud13830McpHostLock:
    def test_host_declares_lock_and_register_snapshot_work(self) -> None:
        from src.features.edit.ai_editing.mcp_gateway import McpServer

        host = McpPluginHost()
        assert isinstance(host._lock, type(threading.Lock()))
        assert host.register(McpServer(_McpFacade()), _descriptor("mcp.a"))
        snapshot = host.snapshot()
        assert snapshot["connectors"][0]["plugin_id"] == "mcp.a"

    def test_remove_provides_cleanup_path(self) -> None:
        from src.features.edit.ai_editing.mcp_gateway import McpServer

        host = McpPluginHost()
        host.register(McpServer(_McpFacade()), _descriptor("mcp.a"))
        assert host.remove("mcp.a") is True
        assert host.remove("mcp.a") is False  # 二次移除：已不存在
        assert host.snapshot()["connectors"] == []
        assert host.remove("never-registered") is False

    def test_concurrent_register_and_snapshot_no_runtime_error(self) -> None:
        from src.features.edit.ai_editing.mcp_gateway import McpServer

        host = McpPluginHost()
        host.register(McpServer(_McpFacade()), _descriptor("mcp.seed"))
        errors: list[BaseException] = []

        def snapper() -> None:
            try:
                for _ in range(300):
                    host.snapshot()
                    time.sleep(0)
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        readers = [threading.Thread(target=snapper) for _ in range(2)]
        for r in readers:
            r.start()
        for i in range(30):
            host.register(McpServer(_McpFacade()), _descriptor(f"mcp.p{i}"))
        for r in readers:
            r.join(timeout=30)
            assert not r.is_alive()
        assert not errors


# ---------------------------------------------------------------------- #
# AUD-13851：_session_images / _server_disabled 读写无锁
# ---------------------------------------------------------------------- #


class TestAud13851VisionGatewayState:
    def _gateway(self) -> McpVisionGateway:
        return McpVisionGateway(api_key_provider=lambda: None, npx_path="/usr/bin/npx")

    def test_session_images_bounded_deque(self, tmp_path: Path) -> None:
        gateway = self._gateway()
        assert isinstance(gateway._session_images, deque)
        assert gateway._session_images.maxlen == 8
        paths = []
        for i in range(10):
            p = tmp_path / f"img_{i}.jpg"
            p.write_bytes(b"x")
            paths.append(str(p))
            gateway.register_session_image(str(p))
        assert len(gateway._session_images) == 8
        # 保留最近 8 张（新在后）
        assert list(gateway._session_images) == paths[2:]
        assert gateway.resolve_image_ref("latest") == paths[-1]

    def test_resolve_latest_empty_returns_empty(self) -> None:
        assert self._gateway().resolve_image_ref("latest") == ""

    def test_server_disabled_guarded_roundtrip(self) -> None:
        from src.features.edit.ai_editing.mcp_vision_gateway import (
            ZHIPU_VISION_PROFILE_ID,
        )

        gateway = self._gateway()
        assert isinstance(gateway._state_lock, type(threading.Lock()))
        assert gateway.is_server_enabled(ZHIPU_VISION_PROFILE_ID) is True
        assert gateway.set_server_enabled(ZHIPU_VISION_PROFILE_ID, False) is True
        assert gateway.is_server_enabled(ZHIPU_VISION_PROFILE_ID) is False
        statuses = {s["profile_id"]: s for s in gateway.server_statuses()}
        assert statuses[ZHIPU_VISION_PROFILE_ID]["enabled"] is False
        assert gateway.set_server_enabled(ZHIPU_VISION_PROFILE_ID, True) is True
        assert gateway.is_server_enabled(ZHIPU_VISION_PROFILE_ID) is True

    def test_unknown_profile_rejected(self) -> None:
        assert self._gateway().set_server_enabled("nope", False) is False


# ---------------------------------------------------------------------- #
# AUD-13905：cancel() docstring 与实际行为不符
# ---------------------------------------------------------------------- #


class TestAud13905CancelDocstring:
    def test_docstring_no_longer_claims_terminal_cancel_returns_false(self) -> None:
        doc = inspect.getdoc(GenerationService.cancel) or ""
        assert "返回 False" not in doc
        # 新 docstring 说明真实语义：True = 请求已下发，不代表取消生效
        assert "请求已下发" in doc

    def test_registry_cancel_true_for_existing_false_for_unknown(self) -> None:
        from src.features.edit.ai_editing.jobs import JobKind, JobRegistry

        registry = JobRegistry()
        job = registry.submit(JobKind.GENERATION, label="x")
        assert registry.cancel(job.job_id) is True
        assert registry.cancel("missing-job") is False


# ---------------------------------------------------------------------- #
# AUD-14014：_engine 惰性初始化无锁
# ---------------------------------------------------------------------- #


class TestAud14014EngineDoubleCheckedLock:
    def _provider(self) -> OcrProvider:
        return OcrProvider()

    def test_provider_declares_engine_lock(self) -> None:
        assert isinstance(self._provider()._engine_lock, type(threading.Lock()))

    def test_concurrent_get_engine_builds_exactly_once(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """修复回退（无锁 check-then-act）时本测试必须失败：构建两次。"""
        provider = self._provider()
        build_calls: list[int] = []
        gate = threading.Event()
        built = object()

        def slow_build(self_: OcrProvider) -> Any:
            build_calls.append(1)
            gate.wait(timeout=5.0)  # 拉大竞争窗口
            return built

        monkeypatch.setattr(OcrProvider, "_build_engine", slow_build)
        results: list[Any] = []
        lock = threading.Lock()

        def caller() -> None:
            engine = provider._get_engine()
            with lock:
                results.append(engine)

        threads = [threading.Thread(target=caller) for _ in range(4)]
        for t in threads:
            t.start()
        time.sleep(0.15)  # 确保全部线程已进入构建临界区
        gate.set()
        for t in threads:
            t.join()
        assert build_calls == [1]  # 只构建一次
        assert all(r is built for r in results)

    def test_cached_engine_returned_without_lock_reentry(self, monkeypatch: pytest.MonkeyPatch) -> None:
        provider = self._provider()

        def fail_build(self_: OcrProvider) -> Any:
            raise AssertionError("cached engine must not rebuild")

        monkeypatch.setattr(OcrProvider, "_build_engine", fail_build)
        provider._engine = object()
        assert provider._get_engine() is provider._engine


# ---------------------------------------------------------------------- #
# AUD-14105：find_by_hash O(n) + 无淘汰
# ---------------------------------------------------------------------- #


class _FakePlan:
    def __init__(self, plan_id: str, content: str) -> None:
        self.plan_id = plan_id
        self._content = content

    def to_dict(self) -> dict[str, Any]:
        return {"plan_id": self.plan_id, "content": self._content}


class TestAud14105HashIndex:
    def test_find_by_hash_roundtrip(self) -> None:
        repo = PlanRepository()
        stored = repo.store(_FakePlan("plan-1", "a"))
        assert repo.find_by_hash(stored.content_hash) is not None
        assert repo.find_by_hash(stored.content_hash).plan_id == "plan-1"
        assert repo.find_by_hash("deadbeef") is None

    def test_reindex_on_overwrite_with_new_content(self) -> None:
        """修复回退（线性扫描）时语义虽同，但本测试锁定反向索引的维护正确性。"""
        repo = PlanRepository()
        first = repo.store(_FakePlan("plan-1", "v1"))
        second = repo.store(_FakePlan("plan-1", "v2"))
        assert first.content_hash != second.content_hash
        # 旧 hash 不再解析到该 plan
        found = repo.find_by_hash(first.content_hash)
        assert found is None or found.plan_id != "plan-1"
        assert repo.find_by_hash(second.content_hash).plan_id == "plan-1"
        # 反向索引与仓库大小一致
        assert repo.count() == 1

    def test_remove_and_clear_maintain_index(self) -> None:
        repo = PlanRepository()
        stored = repo.store(_FakePlan("plan-1", "a"))
        assert repo.remove("plan-1") is True
        assert repo.find_by_hash(stored.content_hash) is None
        stored2 = repo.store(_FakePlan("plan-2", "b"))
        repo.clear()
        assert repo.find_by_hash(stored2.content_hash) is None

    def test_import_all_maintains_index(self) -> None:
        from src.features.edit.ai_editing.models import (
            EditOperation,
            EditPlan,
            OperationKind,
            PlanTarget,
        )

        plan = EditPlan.create(
            plan_id="ef04-import",
            target=PlanTarget(project_id="p1", timeline_id="t1"),
            base_revision=1,
            title="导入",
            operations=(EditOperation(
                op_id="op-1", kind=OperationKind.ADD_CLIP, params={"asset_id": "a1"},
            ),),
        )
        repo = PlanRepository()
        stored = repo.store(plan)
        exported = repo.export_all()
        other = PlanRepository()
        assert other.import_all(exported) == 1
        restored = other.find_by_hash(stored.content_hash)
        assert restored is not None
        assert restored.plan_id == "ef04-import"

    def test_optional_capacity_bound_evicts_oldest(self) -> None:
        repo = PlanRepository(max_plans=2)
        s1 = repo.store(_FakePlan("p1", "a"))
        s2 = repo.store(_FakePlan("p2", "b"))
        repo.store(_FakePlan("p3", "c"))
        assert repo.count() == 2
        assert repo.get("p1") is None  # 最旧被淘汰
        assert repo.find_by_hash(s1.content_hash) is None
        assert repo.find_by_hash(s2.content_hash) is not None
        with pytest.raises(ValueError):
            PlanRepository(max_plans=0)

    def test_default_repository_keeps_all_plans(self) -> None:
        repo = PlanRepository()
        for i in range(5):
            repo.store(_FakePlan(f"p{i}", f"c{i}"))
        assert repo.count() == 5
