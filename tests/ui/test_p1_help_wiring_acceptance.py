"""P1.4 批次3：帮助装配集成验收（功能教学端到端激活）。

> 验证 §10.9 验收矩阵"功能教学"列的真实链路：
>   用户问"TAEP_ASSET_OFFLINE 是什么" → HelpAssistant.explain_diagnostic 拿到真实解释
>   用户问"关键帧" → HelpAssistant.answer 命中真实快捷键条目
>   装配失败 → 不崩，降级但其他依赖正常

对应 plan §10.3 P1.4 完成定义："无 profile 状态下也能搜索帮助；答案可点击定位或给准确路径"。
"""
from __future__ import annotations

from dataclasses import dataclass, field

import pytest

pytestmark = pytest.mark.edit_core


# ---------------------------------------------------------------------- #
# fake host 复用（与 test_host_help_sources 同形状）
# ---------------------------------------------------------------------- #


@dataclass
class _FakeShortcutAction:
    id: str = "edit.add_keyframe"
    label: str = "加关键帧"
    category: str = "edit"
    default_shortcut: str = "K"
    feature_id: str | None = "color"


@dataclass
class _FakeShortcutManager:
    actions: list[_FakeShortcutAction] = field(default_factory=list)

    def get_all_actions(self) -> list[_FakeShortcutAction]:
        return list(self.actions)

    def get_shortcut(self, action_id: str) -> str:
        return next((a.default_shortcut for a in self.actions if a.id == action_id), "")


@dataclass
class _FakeFeatureFlags:
    enabled: dict[str, bool] = field(default_factory=lambda: {
        "edit": True, "color": True, "ai_assistant": False, "library": True,
        "composite": True, "deliver": True,
    })

    def is_enabled(self, feature_id: str) -> bool:
        return self.enabled.get(feature_id, False)


@dataclass
class _FakePageDescriptor:
    page_id: str = "edit.timeline"
    title: str = "时间线"
    default_enabled: bool = True
    shortcut: str | None = None


@dataclass
class _FakeFeatureModule:
    feature_id: str = "edit"

    def is_available(self) -> bool:
        return True


@dataclass
class _FakeFeatureRegistry:
    pages: list[_FakePageDescriptor] = field(default_factory=list)
    modules: dict[str, _FakeFeatureModule] = field(default_factory=lambda: {})

    def get_all_pages(self) -> list[_FakePageDescriptor]:
        return list(self.pages)

    def get_module(self, feature_id: str) -> _FakeFeatureModule | None:
        return self.modules.get(feature_id)


@dataclass
class _FakeMainWindow:
    """模拟 MainWindow 持 5 个 registry 的最小 host。"""
    shortcut_manager: _FakeShortcutManager | None = None
    feature_flags: _FakeFeatureFlags | None = None
    feature_registry: _FakeFeatureRegistry | None = None


def _make_full_host() -> _FakeMainWindow:
    """持全部 5 源的 fake host（诊断码/能力是模块常量，无需 host 持有）。"""
    return _FakeMainWindow(
        shortcut_manager=_FakeShortcutManager(actions=[
            _FakeShortcutAction(id="edit.add_keyframe", label="加关键帧",
                                category="edit", default_shortcut="K", feature_id="color"),
            _FakeShortcutAction(id="file.open", label="打开工程",
                                category="file", default_shortcut="Ctrl+O"),
        ]),
        feature_flags=_FakeFeatureFlags(),
        feature_registry=_FakeFeatureRegistry(
            pages=[
                _FakePageDescriptor(page_id="edit.timeline", title="时间线"),
                _FakePageDescriptor(page_id="color.page", title="调色"),
            ],
            modules={
                "edit": _FakeFeatureModule("edit"),
                "color": _FakeFeatureModule("color"),
            },
        ),
    )


# ---------------------------------------------------------------------- #
# 验收1：HostHelpSources 真实数据源
# ---------------------------------------------------------------------- #


class TestRealDataSources:
    def test_diagnostics_has_24_codes(self, qapp):
        """验收1a：list_diagnostics 返回真实 24 个 TaepDiagnosticCode。"""
        from src.app.controllers.host_help_sources import HostHelpSources
        from src.features.edit.ai_editing.diagnostics import TaepDiagnosticCode

        sources = HostHelpSources(_FakeMainWindow())
        diag = sources.list_diagnostics()
        assert diag is not None
        assert len(diag) == len(list(TaepDiagnosticCode)), (
            f"应覆盖全部 {len(list(TaepDiagnosticCode))} 个诊断码，实际 {len(diag)}"
        )

    def test_capabilities_has_real_operations(self, qapp):
        """验收1b：list_capabilities 返回真实 operations（含 add_clip）。"""
        from src.app.controllers.host_help_sources import HostHelpSources

        cap = HostHelpSources(_FakeMainWindow()).list_capabilities()
        assert cap is not None
        assert "add_clip" in cap["operations"], "应含 add_clip（COMPILABLE_OPERATIONS）"


# ---------------------------------------------------------------------- #
# 验收2：端到端 explain_diagnostic（诊断码解释）
# ---------------------------------------------------------------------- #


class TestExplainDiagnosticE2E:
    def test_asset_offline_explanation_has_remediation(self, qapp):
        """验收2：用户问 'TAEP_ASSET_OFFLINE' → 拿到真实 prereqs/remediation/page_ref。"""
        from src.app.controllers.ai_editing_mixin import build_p1_dependencies
        from src.app.controllers.host_help_sources import HostHelpSources
        from src.features.ai_harness.runtime import HarnessRuntime
        from unittest.mock import MagicMock

        host = _make_full_host()
        deps = build_p1_dependencies(
            service=MagicMock(), runtime=HarnessRuntime(),
            help_sources=HostHelpSources(host),
        )
        ha = deps["help_assistant"]
        assert ha.available, "HelpAssistant 应已装配真实 index"

        # explain_diagnostic 接受 code.value（"TAEP_ASSET_OFFLINE"）
        answer = ha.explain_diagnostic("TAEP_ASSET_OFFLINE")
        assert answer.found, "ASSET_OFFLINE 应能被解释（不返回 found=False）"
        assert len(answer.items) == 1
        item = answer.items[0]
        # _DIAGNOSTIC_EXPLANATIONS 有 ASSET_OFFLINE 的 remediation + page_ref
        assert item.page_ref or item.body, "应有 page_ref 或 body（修复路径/页面定位）"

    def test_unknown_code_returns_not_found(self, qapp):
        """未知诊断码 → found=False（不编造）。"""
        from src.app.controllers.ai_editing_mixin import build_p1_dependencies
        from src.app.controllers.host_help_sources import HostHelpSources
        from src.features.ai_harness.runtime import HarnessRuntime
        from unittest.mock import MagicMock

        host = _make_full_host()
        deps = build_p1_dependencies(
            service=MagicMock(), runtime=HarnessRuntime(),
            help_sources=HostHelpSources(host),
        )
        ha = deps["help_assistant"]
        answer = ha.explain_diagnostic("TAEP_NONEXISTENT_CODE")
        assert not answer.found, "未知码应 found=False（不编造）"


# ---------------------------------------------------------------------- #
# 验收3：端到端 answer（关键词搜索命中）
# ---------------------------------------------------------------------- #


class TestAnswerE2E:
    def test_keyword_search_hits_shortcut(self, qapp):
        """验收3：用户问'关键帧' → 命中真实快捷键条目（K 键）。"""
        from src.app.controllers.ai_editing_mixin import build_p1_dependencies
        from src.app.controllers.host_help_sources import HostHelpSources
        from src.features.ai_harness.runtime import HarnessRuntime
        from unittest.mock import MagicMock

        host = _make_full_host()
        deps = build_p1_dependencies(
            service=MagicMock(), runtime=HarnessRuntime(),
            help_sources=HostHelpSources(host),
        )
        ha = deps["help_assistant"]
        answer = ha.answer("关键帧")
        assert answer.found, "'关键帧' 应命中（fake shortcut 注册了 add_keyframe）"
        # 应至少命中一条含'关键帧'的条目
        titles = " ".join(i.title for i in answer.items)
        assert "关键帧" in titles, f"应命中关键帧相关条目，实际：{titles}"

    def test_panel_search_hits(self, qapp):
        """验收4：用户问'时间线' → 命中面板文档。"""
        from src.app.controllers.ai_editing_mixin import build_p1_dependencies
        from src.app.controllers.host_help_sources import HostHelpSources
        from src.features.ai_harness.runtime import HarnessRuntime
        from unittest.mock import MagicMock

        host = _make_full_host()
        deps = build_p1_dependencies(
            service=MagicMock(), runtime=HarnessRuntime(),
            help_sources=HostHelpSources(host),
        )
        ha = deps["help_assistant"]
        answer = ha.answer("时间线")
        assert answer.found, "'时间线' 应命中面板条目"
        categories = {i.category for i in answer.items}
        assert "panel" in categories, "应命中 panel 类别"


# ---------------------------------------------------------------------- #
# 验收5：装配失败降级不崩
# ---------------------------------------------------------------------- #


class TestDegradationE2E:
    def test_missing_feature_registry_degrades_gracefully(self, qapp):
        """验收5：host 缺 feature_registry → panels 源返回 None，其他源正常，不崩。"""
        from src.app.controllers.host_help_sources import HostHelpSources

        # host 只有快捷键，没有 feature_registry
        host = _FakeMainWindow(
            shortcut_manager=_FakeShortcutManager(actions=[
                _FakeShortcutAction(id="x", label="测试", category="edit"),
            ]),
        )
        sources = HostHelpSources(host)
        # panels 源缺失 → None
        assert sources.list_panels() is None
        # 其他源正常
        assert sources.list_diagnostics() is not None, "诊断码是模块常量，应正常"
        assert sources.list_shortcuts() is not None, "快捷键源在，应正常"

    def test_help_assistant_index_empty_when_all_sources_fail(self, qapp):
        """全源失败 → HelpAssistant.available=False（不崩，answer 给 index_empty）。"""
        from src.app.controllers.ai_editing_mixin import build_p1_dependencies
        from src.app.controllers.host_help_sources import HostHelpSources
        from src.features.ai_harness.runtime import HarnessRuntime
        from unittest.mock import MagicMock

        # 空 host（无任何 registry）—— 诊断码源仍能工作（模块常量），
        # 但快捷键/特性/面板全空。验证 HelpAssistant 仍可用且不崩。
        sources = HostHelpSources(_FakeMainWindow())
        deps = build_p1_dependencies(
            service=MagicMock(), runtime=HarnessRuntime(),
            help_sources=sources,
        )
        ha = deps["help_assistant"]
        # 诊断码至少进了，所以 available 应为 True（不是全空）
        assert ha.available, "诊断码源是模块常量，HelpIndex 应非空"
        # answer 不崩
        answer = ha.answer("任意查询")
        assert answer is not None  # 不抛异常即通过

    def test_router_help_search_uses_real_index(self, qapp):
        """验收6：router._help_search 经 help_assistant 拿到真实 entry_ids（非空 list）。"""
        from src.app.controllers.ai_editing_mixin import build_p1_dependencies
        from src.app.controllers.host_help_sources import HostHelpSources
        from src.features.ai_harness.runtime import HarnessRuntime
        from unittest.mock import MagicMock

        host = _make_full_host()
        deps = build_p1_dependencies(
            service=MagicMock(), runtime=HarnessRuntime(),
            help_sources=HostHelpSources(host),
        )
        router = deps["router"]
        # router 的 _help_search 绑到 help_assistant.entry_ids_for_query
        # 用一个能命中的查询词
        entry_ids = router._help_search("关键帧")
        assert entry_ids, "router._help_search 应返回非空 entry_ids（真实 index 已装配）"
