"""TAEP M6 — HelpIndex 测试。

验收（§9.1 / §11.1 M6 红线）：
- 本地优先、不编造：从可信源聚合；找不到返回空/None，不编造。
- 可引用（citable）：每条带稳定 entry_id（进 ContextBundle.help_refs）。
- 可用性反映 feature flag：feature-dependent 条目标注 enabled/disabled（§9.2）。
- 不依赖 AI 模型：纯本地数据；无 profile 也能 search_help。
- search：关键词子串匹配（title 命中得分高）。
- explain_diagnostic：TAEP 码 → prereqs/remediation；未知码 → None（不编造）。
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.edit_core

from src.features.edit.ai_editing.help_index import (
    HelpIndex,
    SOURCE_DIAGNOSTIC,
    SOURCE_FEATURE,
    SOURCE_PANEL,
    SOURCE_SHORTCUT,
)


class _StubSources:
    """完整源的 stub（带诊断/快捷键/capability/feature/panel）。"""

    def list_diagnostics(self):
        return [
            {"code": "TAEP_ASSET_OFFLINE", "label": "素材离线",
             "message_template": "素材 {path} 离线", "suggested_action": "relink"},
            {"code": "TAEP_EXPORT_PREFLIGHT_FAILED", "label": "导出预检失败",
             "message_template": "导出预检失败"},
            {"code": "TAEP_UNKNOWN_NEW_CODE", "label": "未知新码",
             "message_template": "新码无解释模板"},
        ]

    def list_shortcuts(self):
        return [
            {"id": "file.open", "label": "打开工程", "category": "file",
             "shortcut": "Ctrl+O", "feature_id": None},
            {"id": "edit.add_keyframe", "label": "加关键帧", "category": "edit",
             "shortcut": "K", "feature_id": "color"},
        ]

    def list_capabilities(self):
        return {"operations": ["add_clip", "delete_clip"],
                "effects": [{"effect_type": "color_grade"}, {"effect_type": "vignette"}]}

    def list_features(self):
        return [
            {"feature_id": "edit", "enabled": True, "label": "剪辑"},
            {"feature_id": "color", "enabled": True, "label": "调色"},
            {"feature_id": "ai_assistant", "enabled": False, "label": "AI 助手"},
        ]

    def list_panels(self):
        return [
            {"panel_id": "edit.timeline", "title": "时间线", "category": "Edit",
             "page_ids": ["Edit"], "feature_id": "edit", "enabled": True},
            {"panel_id": "edit.ai_assistant", "title": "AI 助手", "category": "Edit",
             "page_ids": ["Edit"], "feature_id": "ai_assistant", "enabled": False},
        ]


@pytest.fixture
def index() -> HelpIndex:
    return HelpIndex.build(_StubSources())


# ---------------------------------------------------------------------- #
# 聚合
# ---------------------------------------------------------------------- #


class TestBuild:
    def test_aggregates_all_sources(self, index: HelpIndex) -> None:
        assert index.count >= 9  # 3 diag + 2 shortcut + 2 op + 2 effect + 3 feature + 2 panel
        cats = {e.category for e in [index.get(eid) for eid in index.all_entry_ids() if index.get(eid)]}
        assert "diagnostic" in cats
        assert "shortcut" in cats
        assert "capability" in cats
        assert "feature" in cats
        assert "panel" in cats

    def test_empty_sources_yields_empty_index(self) -> None:
        class _Empty:
            def list_diagnostics(self): return None
            def list_shortcuts(self): return None
            def list_capabilities(self): return None
            def list_features(self): return None
            def list_panels(self): return None
        idx = HelpIndex.build(_Empty())
        assert idx.count == 0


# ---------------------------------------------------------------------- #
# 不编造（红线）
# ---------------------------------------------------------------------- #


class TestNoFabrication:
    def test_get_unknown_returns_none(self, index: HelpIndex) -> None:
        """找不到的 entry_id → None（不编造；§9.1 红线）。"""
        assert index.get("diag:TAEP_DOES_NOT_EXIST") is None

    def test_explain_unknown_diagnostic_returns_none(self, index: HelpIndex) -> None:
        """未知诊断码 → None（不编造修复路径；§9.1 红线）。"""
        assert index.explain_diagnostic("TAEP_MADE_UP_CODE") is None


# ---------------------------------------------------------------------- #
# 诊断解释（prereqs + remediation）
# ---------------------------------------------------------------------- #


class TestExplainDiagnostic:
    def test_known_code_has_explanation(self, index: HelpIndex) -> None:
        e = index.explain_diagnostic("TAEP_ASSET_OFFLINE")
        assert e is not None
        assert e.entry_id == "diag:TAEP_ASSET_OFFLINE"
        assert len(e.prereqs) > 0  # 前置条件
        assert len(e.next_steps) > 0  # remediation 步骤
        assert e.page_ref  # 页面引用

    def test_known_code_without_template_still_has_entry(self, index: HelpIndex) -> None:
        """有码但无解释模板 → 仍入索引（body 用 message_template），prereqs 为空。"""
        e = index.explain_diagnostic("TAEP_UNKNOWN_NEW_CODE")
        assert e is not None
        assert e.prereqs == ()  # 无 _DIAGNOSTIC_EXPLANATIONS 模板


# ---------------------------------------------------------------------- #
# 搜索
# ---------------------------------------------------------------------- #


class TestSearch:
    def test_search_finds_by_title(self, index: HelpIndex) -> None:
        results = index.search("离线")
        assert any("离线" in r.title for r in results)

    def test_search_title_match_scores_higher(self, index: HelpIndex) -> None:
        """title 命中排在前面。"""
        results = index.search("关键帧")
        assert len(results) >= 1
        assert "关键帧" in results[0].title

    def test_empty_query_returns_all(self, index: HelpIndex) -> None:
        results = index.search("")
        assert len(results) == index.count

    def test_search_no_match_returns_empty(self, index: HelpIndex) -> None:
        results = index.search("完全不存在的术语 XYZ123")
        assert results == []

    def test_search_case_insensitive(self, index: HelpIndex) -> None:
        results = index.search("FILE.OPEN")
        assert any("file.open" in r.entry_id for r in results)


# ---------------------------------------------------------------------- #
# 可引用（citable）+ feature flag 可用性
# ---------------------------------------------------------------------- #


class TestCitabilityAndAvailability:
    def test_entry_ids_are_stable_and_citable(self, index: HelpIndex) -> None:
        ids = index.all_entry_ids()
        # 前缀稳定（diag:/shortcut:/feature:/panel:/capability:）
        prefixes = {iid.split(":", 1)[0] for iid in ids}
        assert "diag" in prefixes
        assert "shortcut" in prefixes
        assert "panel" in prefixes

    def test_disabled_feature_marked_not_enabled(self, index: HelpIndex) -> None:
        """§9.2：不可用功能明确标为未启用。"""
        ai_panel = index.get("panel:edit.ai_assistant")
        assert ai_panel is not None
        assert ai_panel.enabled is False  # ai_assistant feature 关闭

    def test_enabled_feature_marked_enabled(self, index: HelpIndex) -> None:
        timeline = index.get("panel:edit.timeline")
        assert timeline is not None
        assert timeline.enabled is True

    def test_feature_entry_reflects_flag(self, index: HelpIndex) -> None:
        ai_feature = index.get("feature:ai_assistant")
        assert ai_feature is not None
        assert ai_feature.enabled is False

    def test_to_dict_roundtrip(self, index: HelpIndex) -> None:
        e = index.explain_diagnostic("TAEP_ASSET_OFFLINE")
        d = e.to_dict()
        assert d["entry_id"] == e.entry_id
        assert "prereqs" in d
        assert "next_steps" in d


# ---------------------------------------------------------------------- #
# 不依赖 AI 模型（§11.1 M6）
# ---------------------------------------------------------------------- #


class TestNoModelDependency:
    def test_help_index_pure_local(self) -> None:
        """HelpIndex 不依赖 profile/模型；纯本地数据。"""
        idx = HelpIndex.build(_StubSources())
        # 无 profile/无 client 也能查询
        results = idx.search("导出")
        assert len(results) >= 1
