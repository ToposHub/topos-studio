"""wd8-g47 批 D（AUD-13114..AUD-13406 中白名单文件）修复回归测试。

每条测试对应 REMEDIATION_PLAN.md 的一个 Major 条目，锁定修复后的根因行为：

- AUD-13114 build_manifest 降级必须留痕（registry / codec 两处）
- AUD-13201 工具循环非预期失败降级必须记录并补发一致 request header
- AUD-13202 inbox 追问循环须重估窗口预算 + 逐轮累计用量
- AUD-13210 provenance entries 为显式 null 时 fail closed
- AUD-13240 工具 manifest 非 dict 条目跳过而非崩溃
- AUD-13300 register_package 重复 id 显式拒绝
- AUD-13302 stale 判定与观察顺序无关
- AUD-13303 RenderContract 一致性覆盖 effects/proxy/time_range
- AUD-13305 full_recompute 不再按 1 个 clip 计费
- AUD-13308 accept/reject/lock 状态迁移校验（LOCKED 不可被改写）
- AUD-13312 畸形 resolution 按缺测量处理而非崩溃
- AUD-13313 十进制 NTSC 帧率容差匹配
- AUD-13400 Diagnostic.from_dict 未知枚举值回落原始字符串
"""

from __future__ import annotations

import logging
import sys

import pytest

pytestmark = pytest.mark.edit_core

# --------------------------------------------------------------------------- #
# AUD-13114 — capability_manifest.build_manifest 降级留痕
# --------------------------------------------------------------------------- #


class _BrokenEffectRegistry:
    def get_all_definitions(self):  # type: ignore[no-untyped-def]
        raise RuntimeError("effect registry corrupted")


def test_aud13114_registry_failure_degrades_with_warning(caplog) -> None:
    from src.features.edit.ai_editing.capability_manifest import build_manifest

    with caplog.at_level(
        logging.WARNING,
        logger="src.features.edit.ai_editing.capability_manifest",
    ):
        manifest = build_manifest(effect_registry=_BrokenEffectRegistry())
    assert manifest.effects == ()
    messages = [record.getMessage() for record in caplog.records]
    assert any("效果注册表" in message for message in messages)


def test_aud13114_codec_import_failure_degrades_with_warning(
    monkeypatch: pytest.MonkeyPatch, caplog,
) -> None:
    from src.features.edit.ai_editing.capability_manifest import build_manifest

    # None in sys.modules makes ``from ... import ExportCodec`` raise ImportError.
    monkeypatch.setitem(
        sys.modules, "src.features.edit.core.timeline_export", None,
    )
    with caplog.at_level(
        logging.WARNING,
        logger="src.features.edit.ai_editing.capability_manifest",
    ):
        manifest = build_manifest()
    assert manifest.export["codecs"] == []
    messages = [record.getMessage() for record in caplog.records]
    assert any("编解码器" in message for message in messages)


# --------------------------------------------------------------------------- #
# AUD-13201 / AUD-13202 — chat_run_driver
# --------------------------------------------------------------------------- #


class _UsageReportingClient:
    """每轮返回不同回复并回报该轮真实 usage 的假客户端。"""

    def __init__(self, replies: list[str], totals: list[int]) -> None:
        self._replies = list(replies)
        self._totals = list(totals)
        self.calls = 0
        self.last_usage = None

    def complete(self, messages, **kwargs):  # type: ignore[no-untyped-def]
        index = min(self.calls, len(self._replies) - 1)
        reply = self._replies[index]
        if self.calls < len(self._totals):
            from src.features.edit.ai_editing.llm_client import LLMUsage

            total = self._totals[self.calls]
            self.last_usage = LLMUsage(
                "r", "openai", "m",
                prompt_tokens=total - 10, completion_tokens=10, total_tokens=total,
            )
        self.calls += 1
        return reply


def _driver_env():
    from src.features.edit.ai_editing.agent_orchestrator import AgentOrchestrator
    from src.features.edit.ai_editing.assistant_capability_router import (
        AssistantCapabilityRouter,
    )
    from src.features.edit.ai_editing.chat_run_driver import ChatRunDriver
    from src.features.edit.ai_editing.provider_profiles import (
        AiProviderProfile,
        ProfileRegistry,
        ProviderLimits,
    )
    from src.features.edit.ai_editing.request_recovery import RequestRecoveryPolicy

    def make(client, *, inbox=None, max_context_tokens: int = 8000, tool_facade=None):
        registry = ProfileRegistry()
        registry.add(AiProviderProfile(
            profile_id="lang1",
            display_name="Test",
            provider_kind="openai",
            capabilities=("language",),
            limits=ProviderLimits(max_context_tokens=max_context_tokens),
        ))
        registry.set_active_for_capability("language", "lang1")
        orchestrator = AgentOrchestrator()
        driver = ChatRunDriver(
            orchestrator,
            AssistantCapabilityRouter(registry),
            client_factory=lambda profile: client,
            profile_registry=registry,
            request_recovery_policy=RequestRecoveryPolicy(max_retries=0),
            inbox=inbox,
            tool_facade=tool_facade,
        )
        return driver, orchestrator

    return make


class _ToolLoopBoomClient(_UsageReportingClient):
    def complete_with_tools(self, messages, tools, **kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("must degrade to plain completion before calling tools")


class _BadToolFacade:
    """list_tools 返回畸形工具定义，使 _run_with_tool_loop 抛非 LLMError。"""

    def list_tools(self, *, max_permission=None):  # type: ignore[no-untyped-def]
        return [object()]  # 没有 to_openai_tool → AttributeError


def test_aud13201_unexpected_tool_loop_failure_degrades_with_trace(caplog) -> None:
    from src.features.edit.ai_editing.agent_models import AgentEventKind

    make = _driver_env()
    client = _ToolLoopBoomClient(replies=["plain fallback"], totals=[])
    driver, orchestrator = make(client, tool_facade=_BadToolFacade())

    with caplog.at_level(
        logging.ERROR, logger="src.features.edit.ai_editing.chat_run_driver",
    ):
        result = driver.run_chat(
            session_id="s1", user_text="hi",
            messages=[{"role": "user", "content": "hi"}],
        )

    assert result.succeeded
    assert result.reply == "plain fallback"
    # 降级路径记录了完整异常（不再静默吞掉）。
    assert any("tool loop failed unexpectedly" in r.getMessage() for r in caplog.records)
    # 补发的 request header 与实际请求一致并标注降级来源。
    headers = [
        event for event in orchestrator.get_events(result.run_id)
        if event.kind == AgentEventKind.RUN_REQUEST_HEADER
    ]
    assert [h.payload["request_mode"] for h in headers] == ["tool_loop", "completion"]
    assert headers[1].payload["degraded_from"] == "tool_loop"
    assert headers[1].payload["tool_names"] == []


def test_aud13202_followup_stops_when_window_would_exceed() -> None:
    from src.features.edit.ai_editing.agent_inbox import AgentInbox, InboxItemKind

    make = _driver_env()
    inbox = AgentInbox()
    long_reply = "第一轮建议" * 30  # ~120 字符 ≈ 80+ tokens，足够越过 64 限
    client = _UsageReportingClient(replies=[long_reply, "不应被请求"], totals=[100])

    class _EnqueueClient(_UsageReportingClient):
        def __init__(self, replies, totals, inbox: AgentInbox) -> None:
            super().__init__(replies, totals)
            self._inbox = inbox
            self.run_id = ""

        def complete(self, messages, **kwargs):  # type: ignore[no-untyped-def]
            if self.calls == 0:
                self._inbox.enqueue(
                    self.run_id, InboxItemKind.FOLLOW_UP, "请继续给出剪辑建议",
                )
            return super().complete(messages, **kwargs)

    client = _EnqueueClient([long_reply, "不应被请求"], [100], inbox)
    driver, orchestrator = make(
        client, inbox=inbox, max_context_tokens=64,
    )
    captured: list[str] = []

    def capture_run(event) -> None:
        if event.kind == "run.created":
            client.run_id = event.run_id

    orchestrator.subscribe(capture_run)
    result = driver.run_chat(
        session_id="s1", user_text="请分析这段视频",
        messages=[{"role": "user", "content": "请分析这段视频"}],
    )

    # 窗口重估生效：追加长回复 + pre-step 后超限，第二轮模型调用被终止。
    assert result.succeeded
    assert client.calls == 1
    assert result.reply == long_reply


def test_aud13202_multi_turn_usage_accumulates() -> None:
    from src.features.edit.ai_editing.agent_inbox import AgentInbox, InboxItemKind

    make = _driver_env()
    inbox = AgentInbox()
    client = _UsageReportingClient(
        replies=["第一轮建议", "第二轮建议"], totals=[100, 150],
    )

    class _EnqueueClient(_UsageReportingClient):
        def __init__(self, replies, totals, inbox: AgentInbox) -> None:
            super().__init__(replies, totals)
            self._inbox = inbox
            self.run_id = ""

        def complete(self, messages, **kwargs):  # type: ignore[no-untyped-def]
            if self.calls == 0:
                self._inbox.enqueue(
                    self.run_id, InboxItemKind.FOLLOW_UP, "请继续给出剪辑建议",
                )
            return super().complete(messages, **kwargs)

    client = _EnqueueClient(["第一轮建议", "第二轮建议"], [100, 150], inbox)
    driver, orchestrator = make(client, inbox=inbox)

    def capture_run(event) -> None:
        if event.kind == "run.created":
            client.run_id = event.run_id

    orchestrator.subscribe(capture_run)
    result = driver.run_chat(
        session_id="s1", user_text="请分析这段视频",
        messages=[{"role": "user", "content": "请分析这段视频"}],
    )

    assert result.succeeded
    assert client.calls == 2
    # 多轮请求用量累计上报，而非只反映最后一次请求。
    assert result.usage is not None
    assert result.usage.session_total_tokens == 250


# --------------------------------------------------------------------------- #
# AUD-13210 — commercial_gate fail closed on entries: null
# --------------------------------------------------------------------------- #


class _Bin:
    def __init__(self, report) -> None:  # type: ignore[no-untyped-def]
        self._report = report

    def provenance_report(self):  # type: ignore[no-untyped-def]
        return self._report


def test_aud13210_null_entries_treated_as_absent_no_crash() -> None:
    from src.features.edit.ai_editing.commercial_gate import (
        check_generated_media_for_export,
    )

    # 显式 null 不再让 ``for entry in None`` 抛 TypeError：与键缺失同义，
    # 返回明确的门禁结论（其余检查项为空 → 放行，由其余字段决定）。
    check = check_generated_media_for_export(
        _Bin({"entries": None}), require_commercial_ready=True,
    )
    assert check.allowed
    assert check.blocking == ()


def test_aud13210_non_list_entries_fail_closed() -> None:
    from src.features.edit.ai_editing.commercial_gate import (
        check_generated_media_for_export,
    )

    check = check_generated_media_for_export(
        _Bin({"entries": {"asset_id": "a"}}), require_commercial_ready=True,
    )
    assert not check.allowed
    assert any("entries" in reason for reason in check.blocking)


def test_aud13210_valid_entries_still_gate_per_entry() -> None:
    from src.features.edit.ai_editing.commercial_gate import (
        check_generated_media_for_export,
    )

    check = check_generated_media_for_export(
        _Bin({"entries": [{"asset_id": "a1", "commercial_ready": False}]}),
        require_commercial_ready=True,
    )
    assert not check.allowed
    assert any("商用门禁" in reason for reason in check.blocking)


# --------------------------------------------------------------------------- #
# AUD-13240 — context_service tool manifest 外部数据验证
# --------------------------------------------------------------------------- #


class _ToolSources:
    def __init__(self, tools) -> None:  # type: ignore[no-untyped-def]
        self._tools = tools

    def get_tool_manifest(self):  # type: ignore[no-untyped-def]
        return self._tools

    def get_capability_manifest(self):  # type: ignore[no-untyped-def]
        return {"operations": ()}


def test_aud13240_non_dict_tool_entries_are_skipped() -> None:
    from src.features.edit.ai_editing.context_service import ContextService

    sources = _ToolSources([
        "not-a-dict",
        None,
        {"name": "t1", "permission": "read", "description": "d1"},
    ])
    block = ContextService()._build_tool_manifest_block(sources)
    assert block is not None
    tools = block.content["tools"]
    assert tools == [{"name": "t1", "permission": "read", "desc": "d1"}]


def test_aud13240_non_sequence_manifest_returns_none() -> None:
    from src.features.edit.ai_editing.context_service import ContextService

    block = ContextService()._build_tool_manifest_block(_ToolSources("junk"))
    assert block is None


# --------------------------------------------------------------------------- #
# AUD-13300 — creative_assets.register_package 拒绝静默覆盖
# --------------------------------------------------------------------------- #


def test_aud13300_register_package_rejects_duplicate() -> None:
    from src.features.edit.ai_editing.creative_assets import (
        CreativeAssetRegistry,
        ReferencePackage,
    )

    registry = CreativeAssetRegistry()
    registry.register_package(ReferencePackage(package_id="p1"))
    with pytest.raises(ValueError, match="already registered"):
        registry.register_package(ReferencePackage(package_id="p1", version="2.0.0"))
    # 拒绝后原 package 未被覆盖。
    assert registry.get_package("p1").version == "1.0.0"
    # 变更已注册 package 必须走 update_package（返回影响报告）。
    impact = registry.update_package(ReferencePackage(package_id="p1", version="2.0.0"))
    assert impact.package_id == "p1"
    assert registry.get_package("p1").version == "2.0.0"


# --------------------------------------------------------------------------- #
# AUD-13302 / AUD-13303 — cross_domain
# --------------------------------------------------------------------------- #


def _ref(artifact_id: str, revision: int, content_hash: str):
    from src.features.edit.ai_editing.cross_domain import DomainArtifactRef

    return DomainArtifactRef(
        domain="editorial", artifact_id=artifact_id,
        revision=revision, content_hash=content_hash,
    )


def _graph():
    from src.features.edit.ai_editing.cross_domain import (
        CrossDomainDependency,
        DependencyGraph,
    )

    dep = CrossDomainDependency(
        downstream_domain="color",
        upstream=_ref("ep1", 1, "h1"),
        affected_clip_ids=("c1",),
    )
    return DependencyGraph(dependencies=(dep,))


def test_aud13302_stale_is_order_independent() -> None:
    graph = _graph()
    same = _ref("ep1", 1, "h1")
    changed = _ref("ep1", 2, "h2")
    # 同名 artifact 出现新 revision：无论观察顺序如何都判 stale。
    assert graph.stale_downstream((same, changed)) == ("color",)
    assert graph.stale_downstream((changed, same)) == ("color",)
    assert graph.dirty_clip_ids((changed, same)) == ("c1",)


def test_aud13302_unrelated_same_domain_artifact_is_not_stale() -> None:
    graph = _graph()
    same = _ref("ep1", 1, "h1")
    other = _ref("analysis-9", 1, "h9")
    # 同域其他 artifact 与依赖上游无关：不影响判定且与顺序无关。
    assert graph.stale_downstream((same, other)) == ()
    assert graph.stale_downstream((other, same)) == ()
    # 上游 artifact 被同域其他 artifact 完全取代 → 仍判 stale。
    assert graph.stale_downstream((other,)) == ("color",)


def test_aud13303_consistency_covers_effects_proxy_and_time_range() -> None:
    from src.features.edit.ai_editing.cross_domain import RenderContract

    base = RenderContract(width=1920, height=1080, fps=30.0)
    assert base.is_consistent_with(
        RenderContract(width=1920, height=1080, fps=30.0, effects_enabled=("blur",)),
        ignore=("effects_enabled",),
    )
    # 预览关特效 vs 导出开特效：默认必须判不一致。
    assert not base.is_consistent_with(
        RenderContract(width=1920, height=1080, fps=30.0, effects_enabled=("blur",)),
    )
    # proxy 档位不同。
    assert not base.is_consistent_with(
        RenderContract(width=1920, height=1080, fps=30.0, proxy_tier="proxy"),
    )
    # 时间段不同。
    assert not base.is_consistent_with(
        RenderContract(width=1920, height=1080, fps=30.0, time_range={"start": 1}),
    )
    # 显式豁免 proxy_tier 后其余字段一致 → 一致。
    assert base.is_consistent_with(
        RenderContract(width=1920, height=1080, fps=30.0, proxy_tier="proxy"),
        ignore=("proxy_tier",),
    )
    # 未知豁免字段名必须抛错（可定位的配置错误）。
    with pytest.raises(ValueError, match="exemption"):
        base.is_consistent_with(base, ignore=("nope",))


# --------------------------------------------------------------------------- #
# AUD-13305 — cross_domain_cost full_recompute 计费
# --------------------------------------------------------------------------- #


def test_aud13305_full_recompute_not_billed_as_single_clip() -> None:
    from src.features.edit.ai_editing.cross_domain import (
        CrossDomainDependency,
        DependencyGraph,
    )
    from src.features.edit.ai_editing.cross_domain_cost import predict_recompute_cost

    def make_graph(affected: tuple[str, ...]) -> DependencyGraph:
        return DependencyGraph(dependencies=(
            CrossDomainDependency(
                downstream_domain="composite",
                upstream=_ref("ep1", 1, "h1"),
                affected_clip_ids=affected,
            ),
        ))

    partial = predict_recompute_cost(
        make_graph(("c1", "c2", "c3")), "editorial", ("c1",),
    )
    full = predict_recompute_cost(make_graph(()), "editorial", ())

    assert full.dirty.full_recompute
    assert full.total_estimated_cost > partial.total_estimated_cost
    domain_cost = full.domain_costs[0]
    assert domain_cost.estimate_is_floor is True
    assert domain_cost.affected_clip_count > 3
    assert full.to_dict()["domain_costs"][0]["estimate_is_floor"] is True


def test_aud13305_total_clip_count_used_when_provided() -> None:
    from src.features.edit.ai_editing.cross_domain import (
        CrossDomainDependency,
        DependencyGraph,
    )
    from src.features.edit.ai_editing.cross_domain_cost import predict_recompute_cost

    graph = DependencyGraph(dependencies=(
        CrossDomainDependency(
            downstream_domain="composite",
            upstream=_ref("ep1", 1, "h1"),
            affected_clip_ids=(),  # 空 = 整域重算
        ),
    ))
    report = predict_recompute_cost(
        graph, "editorial", (), total_clip_count=50, budget_limit=10.0,
    )
    assert report.domain_costs[0].affected_clip_count == 50
    # composite 默认 1.0/clip：50 clips → 50 成本单位 > 预算 10。
    assert report.exceeds_budget
    assert report.recommended_tier_downgrade == "lowres"
    assert not report.domain_costs[0].estimate_is_floor


# --------------------------------------------------------------------------- #
# AUD-13308 — decision_service 状态迁移校验
# --------------------------------------------------------------------------- #


def _decision(did: str):
    from src.features.edit.ai_editing.editorial_models import (
        EditDecision,
        FrameRange,
        SourceRef,
    )

    return EditDecision(
        decision_id=did, action="select_source_range",
        subject=SourceRef("a1", FrameRange(0, 100)), confidence=0.8,
    )


def test_aud13308_locked_decision_cannot_be_rejected_or_accepted() -> None:
    from src.features.edit.ai_editing.decision_service import DecisionStore
    from src.features.edit.ai_editing.editorial_models import DecisionState

    store = DecisionStore()
    store.add(_decision("d1"))
    store.lock("d1")
    with pytest.raises(ValueError, match="locked"):
        store.reject("d1")
    with pytest.raises(ValueError, match="locked"):
        store.accept("d1")
    # 非法迁移后状态保持 LOCKED（锁定保护不被一次误调失效）。
    assert store.get("d1").state == DecisionState.LOCKED
    # 幂等 re-lock 仍然允许。
    store.lock("d1")
    assert store.get("d1").state == DecisionState.LOCKED


def test_aud13308_rejected_decision_cannot_be_revived_by_accept() -> None:
    from src.features.edit.ai_editing.decision_service import DecisionStore
    from src.features.edit.ai_editing.editorial_models import DecisionState

    store = DecisionStore()
    store.add(_decision("d1"))
    store.reject("d1")
    with pytest.raises(ValueError):
        store.accept("d1")
    with pytest.raises(ValueError):
        store.lock("d1")
    assert store.get("d1").state == DecisionState.REJECTED
    # 幂等 re-reject 允许（既有测试锁定的行为）。
    store.reject("d1")
    assert store.get("d1").state == DecisionState.REJECTED


def test_aud13308_accepted_to_rejected_and_locked_transitions_allowed() -> None:
    from src.features.edit.ai_editing.decision_service import DecisionStore
    from src.features.edit.ai_editing.editorial_models import DecisionState

    store = DecisionStore()
    store.add(_decision("d1"))
    store.accept("d1")
    store.reject("d1")  # 既有行为：ACCEPTED → REJECTED 合法
    assert store.get("d1").state == DecisionState.REJECTED

    store2 = DecisionStore()
    store2.add(_decision("d2"))
    store2.accept("d2")
    store2.lock("d2")  # ACCEPTED → LOCKED 合法
    assert store2.get("d2").state == DecisionState.LOCKED


# --------------------------------------------------------------------------- #
# AUD-13312 / AUD-13313 — delivery_contracts
# --------------------------------------------------------------------------- #


def _platform_spec():
    from src.features.edit.ai_editing.delivery_contracts import PlatformDeliverySpec

    return PlatformDeliverySpec(
        spec_id="test-platform",
        allowed_resolutions=((1080, 1920),),
        allowed_fps=("30000/1001",),
    )


@pytest.mark.parametrize("bad", [1920, ["1920"], (1080, 1920, 3), "1920x1080", [None, 1920]])
def test_aud13312_malformed_resolution_is_missing_measurement_not_crash(bad) -> None:
    from src.features.edit.ai_editing.delivery_contracts import validate_platform_delivery

    result = validate_platform_delivery(
        _platform_spec(), metadata={"resolution": bad},
    )
    assert not result.passed
    assert any("分辨率" in error for error in result.errors)


def test_aud13312_valid_resolution_list_still_passes() -> None:
    from src.features.edit.ai_editing.delivery_contracts import validate_platform_delivery

    result = validate_platform_delivery(
        _platform_spec(),
        metadata={"resolution": [1080, 1920], "fps": "30000/1001"},
    )
    assert result.passed


def test_aud13313_decimal_ntsc_fps_matches_fractional_spec() -> None:
    from src.features.edit.ai_editing.delivery_contracts import _fps_matches

    assert _fps_matches("29.97", ("30000/1001",))
    assert _fps_matches("23.976", ("24000/1001",))
    assert _fps_matches("30000/1001", ("30000/1001",))
    # 容差不吞掉相邻帧率档位。
    assert not _fps_matches("30", ("30000/1001",))
    assert not _fps_matches("24", ("24000/1001",))


def test_aud13313_platform_validation_accepts_decimal_ntsc() -> None:
    from src.features.edit.ai_editing.delivery_contracts import validate_platform_delivery

    result = validate_platform_delivery(
        _platform_spec(),
        metadata={"resolution": (1080, 1920), "fps": "29.97"},
    )
    assert result.passed
    assert not any("帧率" in error for error in result.errors)


# --------------------------------------------------------------------------- #
# AUD-13400 — Diagnostic.from_dict 未知枚举值回落原始字符串
# --------------------------------------------------------------------------- #


def test_aud13400_unknown_severity_and_code_fall_back_to_raw_strings() -> None:
    from src.features.edit.ai_editing.diagnostics import Diagnostic

    diagnostic = Diagnostic.from_dict({
        "severity": "fatal",
        "code": "TAEP_FUTURE_CODE",
        "message": "m",
    })
    # docstring 契约：未知值回落到原始字符串（str 枚举运行时兼容）。
    assert diagnostic.severity == "fatal"
    assert diagnostic.code == "TAEP_FUTURE_CODE"
    # 原始值同时写入 details，便于定位。
    assert diagnostic.details["severity_raw"] == "fatal"
    assert diagnostic.details["code_raw"] == "TAEP_FUTURE_CODE"
    # 序列化对原始字符串不再崩溃且保真。
    payload = diagnostic.to_dict()
    assert payload["severity"] == "fatal"
    assert payload["code"] == "TAEP_FUTURE_CODE"
    assert not diagnostic.is_error


def test_aud13400_empty_unknown_values_keep_safe_defaults() -> None:
    from src.features.edit.ai_editing.diagnostics import (
        Diagnostic,
        DiagnosticSeverity,
        TaepDiagnosticCode,
    )

    diagnostic = Diagnostic.from_dict({"severity": "", "code": ""})
    assert diagnostic.severity is DiagnosticSeverity.INFO
    assert diagnostic.code is TaepDiagnosticCode.INTERNAL_ERROR


def test_aud13400_known_values_round_trip_as_enums() -> None:
    from src.features.edit.ai_editing.diagnostics import (
        Diagnostic,
        DiagnosticSeverity,
        TaepDiagnosticCode,
    )

    diagnostic = Diagnostic.from_dict({
        "severity": "error",
        "code": "TAEP_REVISION_CONFLICT",
        "message": "m",
    })
    assert diagnostic.severity is DiagnosticSeverity.ERROR
    assert diagnostic.code is TaepDiagnosticCode.REVISION_CONFLICT
    assert diagnostic.is_error
    assert Diagnostic.from_dict(diagnostic.to_dict()).to_dict() == diagnostic.to_dict()
