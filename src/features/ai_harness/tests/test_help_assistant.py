"""HelpAssistant 单元测试（plan §10.3 P1.4）。

覆盖 plan §10.3 P1.4 验收：

- HelpIndex.search 作为无模型可用 Skill。
- 回答带 entry_id / 页面位置 / feature 状态 / 下一步。
- 不从模型记忆编造（找不到 → found=False，不编步骤）。
- 不可用功能标注未启用。
- 无 profile 也能搜索（不依赖 profile）。
- entry_ids_for_query 供 router.help_search 注入。

红线（plan §10.3 P1.4 / §1.3）：外部输入不可信；不编造产品功能。
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.edit_core

from src.features.ai_harness.help_assistant import (
    HelpAssistant,
    build_help_assistant,
)
from src.features.edit.ai_editing.help_index import HelpEntry, HelpIndex


# ================================================================
# 测试用 HelpIndex 装配
# ================================================================


def _index_with(*entries: HelpEntry) -> HelpIndex:
    """AUD-22510：经公共测试缝 from_entries 装配，不再触私有 _entries。"""
    return HelpIndex.from_entries(entries)


def _entry(
    entry_id: str = "panel:timeline", title: str = "时间线面板",
    body: str = "在时间线面板编辑片段。", category: str = "panel",
    page_ref: str = "面板 > 时间线", shortcut_hint: str = "Ctrl+T",
    next_steps=("打开时间线面板", "拖入素材"),
    feature_id: str | None = None, enabled: bool = True,
) -> HelpEntry:
    return HelpEntry(
        entry_id=entry_id, title=title, body=body, category=category,
        page_ref=page_ref, shortcut_hint=shortcut_hint,
        next_steps=tuple(next_steps), feature_id=feature_id, enabled=enabled,
    )


# ================================================================
# answer —— 核心验收
# ================================================================


class TestAnswer:
    def test_finds_entries_with_full_metadata(self):
        """回答带 entry_id / page_ref / shortcut / next_steps / feature 状态。"""
        idx = _index_with(_entry())
        ha = HelpAssistant(idx)
        ans = ha.answer("时间线")
        assert ans.found is True
        assert len(ans.items) == 1
        item = ans.items[0]
        assert item.entry_id == "panel:timeline"
        assert item.page_ref == "面板 > 时间线"
        assert item.shortcut_hint == "Ctrl+T"
        assert item.next_steps == ("打开时间线面板", "拖入素材")
        assert item.feature_enabled is True

    def test_not_found_does_not_fabricate(self):
        """红线：找不到 → found=False，绝不编造步骤。"""
        idx = _index_with(_entry())
        ha = HelpAssistant(idx)
        ans = ha.answer("完全不存在的功能 xyz")
        assert ans.found is False
        assert ans.items == ()
        assert "未在帮助索引中找到" in ans.summary

    def test_disabled_feature_marked(self):
        """不可用功能标注未启用（plan §10.3 P1.4）。"""
        idx = _index_with(_entry(
            entry_id="feature:comp", title="合成功能",
            feature_id="composite", enabled=False,
        ))
        ha = HelpAssistant(idx)
        ans = ha.answer("合成")
        assert ans.found is True
        assert ans.items[0].feature_enabled is False
        assert "未启用" in ans.items[0].availability_label

    def test_empty_query_returns_not_found(self):
        ha = HelpAssistant(_index_with(_entry()))
        ans = ha.answer("")
        assert ans.found is False
        assert ans.items == ()

    def test_summary_with_multiple_results(self):
        idx = _index_with(
            _entry(entry_id="a", title="关键帧"),
            _entry(entry_id="b", title="关键帧动画"),
        )
        ha = HelpAssistant(idx)
        ans = ha.answer("关键帧")
        assert "2" in ans.summary

    def test_works_without_profile(self):
        """无 profile 也能搜索（plan §10.3 P1.4）。"""
        ha = HelpAssistant(_index_with(_entry()))
        # HelpAssistant 根本不持有 profile 概念 → 永远可搜
        ans = ha.answer("时间线")
        assert ans.found is True


# ================================================================
# index 未装配
# ================================================================


class TestIndexUnavailable:
    def test_no_index_gives_friendly_message(self):
        ha = HelpAssistant(index=None)
        ans = ha.answer("时间线")
        assert ans.found is False
        assert ans.index_empty is True
        assert "未装配" in ans.summary

    def test_available_property(self):
        assert HelpAssistant(index=None).available is False
        assert HelpAssistant(_index_with(_entry())).available is True

    def test_attach_makes_available(self):
        ha = HelpAssistant(index=None)
        assert ha.available is False
        ha.attach(_index_with(_entry()))
        assert ha.available is True


# ================================================================
# entry_ids_for_query（router 注入）
# ================================================================


class TestEntryIdsForRouter:
    def test_returns_entry_ids(self):
        idx = _index_with(
            _entry(entry_id="panel:a", title="关键帧"),
            _entry(entry_id="panel:b", title="关键帧动画"),
        )
        ha = HelpAssistant(idx)
        ids = ha.entry_ids_for_query("关键帧")
        assert set(ids) == {"panel:a", "panel:b"}

    def test_empty_when_no_index(self):
        ha = HelpAssistant(index=None)
        assert ha.entry_ids_for_query("x") == []

    def test_limit_respected(self):
        idx = _index_with(*[_entry(entry_id=f"e{i}", title="关键帧") for i in range(8)])
        ha = HelpAssistant(idx)
        assert len(ha.entry_ids_for_query("关键帧", limit=3)) <= 3


# ================================================================
# explain_diagnostic
# ================================================================


class TestExplainDiagnostic:
    def test_known_diagnostic(self):
        idx = _index_with(HelpEntry(
            entry_id="diag:TAEP_PERMISSION_DENIED", title="权限被拒",
            body="审批 token 无效或已过期。", category="diagnostic",
            next_steps=("重新预览 diff", "重新审批"),
        ))
        ha = HelpAssistant(idx)
        ans = ha.explain_diagnostic("TAEP_PERMISSION_DENIED")
        assert ans.found is True
        assert ans.items[0].entry_id == "diag:TAEP_PERMISSION_DENIED"
        assert ans.items[0].next_steps == ("重新预览 diff", "重新审批")

    def test_unknown_diagnostic_not_found(self):
        ha = HelpAssistant(_index_with())
        ans = ha.explain_diagnostic("UNKNOWN_CODE")
        assert ans.found is False


# ================================================================
# build_help_assistant 工厂
# ================================================================


class TestBuildFactory:
    def test_no_sources_returns_empty(self):
        ha = build_help_assistant(None)
        assert ha.available is False

    def test_bad_sources_returns_empty_not_raise(self):
        class Bad:
            def list_diagnostics(self):
                raise RuntimeError("boom")
        ha = build_help_assistant(Bad())
        assert ha.available is False  # 装配失败被吞


class TestAud22510FromEntriesSeam:
    """AUD-22510：HelpIndex 公开测试缝取代私有 ``_entries`` 直填。"""

    def test_from_entries_assembles_index(self):
        idx = _index_with(
            _entry(entry_id="panel:a"), _entry(entry_id="panel:b"))
        assert idx.count == 2
        assert idx.get("panel:a") is not None
        assert idx.get("panel:b") is not None
        assert idx.get("panel:missing") is None

    def test_duplicate_id_last_wins(self):
        idx = _index_with(
            _entry(entry_id="panel:a", title="旧"),
            _entry(entry_id="panel:a", title="新"),
        )
        assert idx.count == 1
        assert idx.get("panel:a").title == "新"

    def test_helper_no_longer_touches_private_entries(self):
        import inspect
        src = inspect.getsource(_index_with)
        assert "._entries" not in src, (
            "测试装配应走 HelpIndex.from_entries 公共缝（AUD-22510），"
            "不得再直接写私有 _entries"
        )
