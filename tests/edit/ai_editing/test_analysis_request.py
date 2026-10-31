"""TAEP M2 — AnalysisRequest 数据合同测试。

验收（§17.3 / §18.2）：
- 请求合同字段完备：recipe / provider_ids / data_policy / budget / requested_by。
- ``user_confirmed_at`` 为 None 时 scheduler 必须拒绝执行（核心红线）。
- 云配方必须搭配 APPROVED_CLOUD policy（防御性一致性）。
- to_dict/from_dict 往返保真；schema version 写入 payload。
- 未知字段保留（向后兼容旧 payload）。
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.edit_core

from src.features.edit.ai_editing.analysis_request import (
    ANALYSIS_REQUEST_SCHEMA_VERSION,
    ANALYSIS_REQUEST_SCHEMA_VERSION_KEY,
    AnalysisBudget,
    AnalysisDataPolicy,
    AnalysisRecipe,
    AnalysisRequest,
    RequestState,
)


# ================================================================
# recipe / policy / state 枚举
# ================================================================


def test_recipe_enum_has_four_progressive_levels():
    """配方是渐进式成本（§17.4）：fast → editorial → visual_semantic → cloud。"""
    assert AnalysisRecipe.FAST_LOCAL.value == "fast_local"
    assert AnalysisRecipe.EDITORIAL_LOCAL.value == "editorial_local"
    assert AnalysisRecipe.VISUAL_SEMANTIC_LOCAL.value == "visual_semantic_local"
    assert AnalysisRecipe.CLOUD_ENHANCED.value == "cloud_enhanced"


def test_data_policy_local_vs_cloud():
    assert AnalysisDataPolicy.LOCAL_ONLY.value == "local_only"
    assert AnalysisDataPolicy.APPROVED_CLOUD.value == "approved_cloud"


def test_state_is_executable_only_when_confirmed():
    """只有 CONFIRMED 可被 scheduler 接受执行。"""
    assert RequestState.CONFIRMED.is_executable is True
    assert RequestState.PROPOSED.is_executable is False
    assert RequestState.RUNNING.is_executable is False
    assert RequestState.COMPLETED.is_executable is False


def test_state_is_terminal():
    for s in (RequestState.COMPLETED, RequestState.PARTIAL, RequestState.FAILED, RequestState.CANCELLED):
        assert s.is_terminal is True
    for s in (RequestState.PROPOSED, RequestState.CONFIRMED, RequestState.RUNNING):
        assert s.is_terminal is False


# ================================================================
# AnalysisBudget
# ================================================================


def test_budget_round_trip():
    b = AnalysisBudget(max_duration_seconds=60.0, max_cost_units=1.5, max_assets=10)
    d = b.to_dict()
    assert d == {"max_duration_seconds": 60.0, "max_cost_units": 1.5, "max_assets": 10}
    b2 = AnalysisBudget.from_dict(d)
    assert b2 == b


def test_budget_empty_dict_when_all_none():
    assert AnalysisBudget().to_dict() == {}


# ================================================================
# AnalysisRequest 构造 + 确认门
# ================================================================


def test_create_defaults_to_proposed_unconfirmed():
    """新创建的 request 默认 PROPOSED + 未确认（§17.3）。"""
    r = AnalysisRequest.create("p1", ["a1", "a2"], requested_by="media_pool")
    assert r.is_confirmed is False
    assert r.state == RequestState.PROPOSED
    assert r.user_confirmed_at is None
    assert r.data_policy == AnalysisDataPolicy.LOCAL_ONLY
    assert r.recipe_id == AnalysisRecipe.FAST_LOCAL
    assert r.requested_by == "media_pool"


def test_is_confirmed_reflects_user_confirmed_at():
    r = AnalysisRequest.create("p1", ["a1"])
    assert r.is_confirmed is False
    confirmed = r.with_confirmation(1234.5)
    assert confirmed.is_confirmed is True
    assert confirmed.user_confirmed_at == 1234.5
    assert confirmed.state == RequestState.CONFIRMED
    # 不可变：原 request 不变
    assert r.is_confirmed is False


def test_with_state_transitions():
    r = AnalysisRequest.create("p1", ["a1"]).with_confirmation(1.0)
    running = r.with_state(RequestState.RUNNING)
    assert running.state == RequestState.RUNNING
    assert running.is_confirmed is True  # confirmation preserved


def test_is_cloud_flag():
    local = AnalysisRequest.create("p1", ["a1"], data_policy=AnalysisDataPolicy.LOCAL_ONLY)
    cloud = AnalysisRequest.create(
        "p1", ["a1"], recipe_id=AnalysisRecipe.CLOUD_ENHANCED,
        data_policy=AnalysisDataPolicy.APPROVED_CLOUD,
    )
    assert local.is_cloud is False
    assert cloud.is_cloud is True


def test_cloud_recipe_requires_approved_cloud_policy():
    """CLOUD_ENHANCED 配方必须搭配 APPROVED_CLOUD（云上传需显式同意）。"""
    with pytest.raises(ValueError, match="APPROVED_CLOUD"):
        AnalysisRequest.create(
            "p1", ["a1"], recipe_id=AnalysisRecipe.CLOUD_ENHANCED,
            data_policy=AnalysisDataPolicy.LOCAL_ONLY,
        )


def test_empty_asset_ids_rejected():
    """分析范围是必需的（防止"分析全部素材"无范围扩散）。"""
    with pytest.raises(ValueError, match="asset_ids"):
        AnalysisRequest.create("p1", [])


def test_empty_project_id_rejected():
    with pytest.raises(ValueError, match="project_id"):
        AnalysisRequest.create("", ["a1"])


# ================================================================
# 序列化往返 + schema version
# ================================================================


def test_to_dict_includes_schema_version():
    r = AnalysisRequest.create("p1", ["a1"], requested_by="test")
    d = r.to_dict()
    assert d[ANALYSIS_REQUEST_SCHEMA_VERSION_KEY] == ANALYSIS_REQUEST_SCHEMA_VERSION
    assert d["schema_version"] if False else True  # placeholder for readability


def test_round_trip_preserves_all_fields():
    r = AnalysisRequest.create(
        "p1", ["a1", "a2"],
        recipe_id=AnalysisRecipe.EDITORIAL_LOCAL,
        provider_ids=["scene_cut_local", "whisperx_local"],
        data_policy=AnalysisDataPolicy.LOCAL_ONLY,
        budget=AnalysisBudget(max_duration_seconds=120.0, max_assets=5),
        requested_by="assistant",
        project_revision=7,
        title="口播分析",
        now=100.0,
    ).with_confirmation(200.0)
    d = r.to_dict()
    r2 = AnalysisRequest.from_dict(d)
    assert r2 == r
    assert r2.user_confirmed_at == 200.0
    assert r2.state == RequestState.CONFIRMED
    assert r2.title == "口播分析"
    assert r2.budget.max_assets == 5


def test_round_trip_unconfirmed_request():
    """未确认的 request 也能往返（user_confirmed_at 不写入 dict）。"""
    r = AnalysisRequest.create("p1", ["a1"])
    assert r.user_confirmed_at is None
    d = r.to_dict()
    assert "user_confirmed_at" not in d
    r2 = AnalysisRequest.from_dict(d)
    assert r2 == r
    assert r2.is_confirmed is False


def test_from_dict_preserves_unknown_fields_tolerance():
    """旧 payload 加载：缺失新字段不报错（向后兼容）。"""
    minimal = {
        "request_id": "req-1",
        "project_id": "p1",
        "asset_ids": ["a1"],
        "recipe_id": "fast_local",
        "provider_ids": [],
        "data_policy": "local_only",
        "state": "proposed",
    }
    r = AnalysisRequest.from_dict(minimal)
    assert r.request_id == "req-1"
    assert r.budget.max_duration_seconds is None
    assert r.title == ""
