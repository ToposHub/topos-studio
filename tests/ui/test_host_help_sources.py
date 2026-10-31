"""P1.4 批次1：HostHelpSources 适配器（5 源 → HelpSources Protocol）。

> 验证 MainWindow 持有的 5 个 registry 能被适配成 HelpSources Protocol，
> 任一源缺失/异常时该源返回 None（HelpIndex 据此跳过，不崩）。
> 形状与 tests/edit/ai_editing/test_help_index.py:_StubSources 严格对齐。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

pytestmark = pytest.mark.edit_core


# ---------------------------------------------------------------------- #
# fake host / fake registries（dataclass，无 mock 库依赖）
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
    _overrides: dict[str, str] = field(default_factory=lambda: {})

    def get_all_actions(self) -> list[_FakeShortcutAction]:
        return list(self.actions)

    def get_shortcut(self, action_id: str) -> str:
        return self._overrides.get(action_id, next(
            (a.default_shortcut for a in self.actions if a.id == action_id), ""
        ))


@dataclass
class _FakeFeatureFlags:
    enabled: dict[str, bool] = field(default_factory=lambda: {
        "edit": True, "color": True, "ai_assistant": False,
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
class _FakeHost:
    """持 5 个 fake registry 的最小 host。属性缺失时返回 None。"""
    shortcut_manager: _FakeShortcutManager | None = None
    feature_flags: _FakeFeatureFlags | None = None
    feature_registry: _FakeFeatureRegistry | None = None


# ---------------------------------------------------------------------- #
# list_diagnostics
# ---------------------------------------------------------------------- #


class TestListDiagnostics:
    def test_returns_real_diagnostic_codes(self, qapp):
        """遍历真实 TaepDiagnosticCode 枚举，返回 24 个码（非空）。"""
        from src.app.controllers.host_help_sources import HostHelpSources
        from src.features.edit.ai_editing.diagnostics import TaepDiagnosticCode

        sources = HostHelpSources(_FakeHost())
        result = sources.list_diagnostics()
        assert result is not None, "list_diagnostics 不应返回 None"
        assert len(result) >= 20, f"应至少 20 个诊断码，实际 {len(result)}"
        codes = {item["code"] for item in result}
        assert "TAEP_ASSET_OFFLINE" in codes, "应包含 TAEP_ASSET_OFFLINE"

    def test_item_shape_matches_protocol(self, qapp):
        """每项含 {code, label, message_template, suggested_action} 4 键。"""
        from src.app.controllers.host_help_sources import HostHelpSources

        result = HostHelpSources(_FakeHost()).list_diagnostics()
        assert result, "应有诊断码"
        item = result[0]
        assert {"code", "label", "message_template", "suggested_action"} <= set(item.keys()), (
            f"诊断项形状错误：{item.keys()}"
        )
        assert item["code"], "code 不能为空"

    def test_uses_message_template_zh(self, qapp):
        """message_template 取自 MESSAGE_TEMPLATES zh 文案。"""
        from src.app.controllers.host_help_sources import HostHelpSources

        result = HostHelpSources(_FakeHost()).list_diagnostics()
        offline = next((i for i in result if i["code"] == "TAEP_ASSET_OFFLINE"), None)
        assert offline is not None
        assert offline["message_template"], "ASSET_OFFLINE 应有模板"

    def test_suggested_action_from_explanations(self, qapp):
        """suggested_action 取自 _DIAGNOSTIC_EXPLANATIONS remediation（不编造）。"""
        from src.app.controllers.host_help_sources import HostHelpSources

        result = HostHelpSources(_FakeHost()).list_diagnostics()
        offline = next((i for i in result if i["code"] == "TAEP_ASSET_OFFLINE"), None)
        assert offline is not None
        # _DIAGNOSTIC_EXPLANATIONS 有 ASSET_OFFLINE 的 remediation，应非空
        assert offline["suggested_action"], "ASSET_OFFLINE 应有 suggested_action"


# ---------------------------------------------------------------------- #
# list_shortcuts
# ---------------------------------------------------------------------- #


class TestListShortcuts:
    def test_returns_actions_from_manager(self, qapp):
        """从 shortcut_manager.get_all_actions() 取快捷键。"""
        from src.app.controllers.host_help_sources import HostHelpSources

        host = _FakeHost(shortcut_manager=_FakeShortcutManager(actions=[
            _FakeShortcutAction(id="edit.add_keyframe", label="加关键帧",
                                category="edit", default_shortcut="K", feature_id="color"),
        ]))
        result = HostHelpSources(host).list_shortcuts()
        assert result is not None and len(result) == 1
        assert result[0]["id"] == "edit.add_keyframe"

    def test_item_shape(self, qapp):
        """每项含 {id, label, category, shortcut, feature_id}。"""
        from src.app.controllers.host_help_sources import HostHelpSources

        host = _FakeHost(shortcut_manager=_FakeShortcutManager(actions=[
            _FakeShortcutAction(),
        ]))
        result = HostHelpSources(host).list_shortcuts()
        assert result and len(result) == 1
        item = result[0]
        assert {"id", "label", "category", "shortcut", "feature_id"} <= set(item.keys()), (
            f"shortcut 项形状错误：{item.keys()}"
        )

    def test_uses_active_shortcut_including_override(self, qapp):
        """shortcut 用 manager.get_shortcut（含用户覆盖）。"""
        from src.app.controllers.host_help_sources import HostHelpSources

        host = _FakeHost(shortcut_manager=_FakeShortcutManager(
            actions=[_FakeShortcutAction(id="x", default_shortcut="K")],
            _overrides={"x": "Ctrl+K"},
        ))
        result = HostHelpSources(host).list_shortcuts()
        assert result[0]["shortcut"] == "Ctrl+K", "应用户覆盖后的当前快捷键"

    def test_no_manager_returns_none(self, qapp):
        """host 无 shortcut_manager → 返回 None（HelpIndex 跳过）。"""
        from src.app.controllers.host_help_sources import HostHelpSources

        result = HostHelpSources(_FakeHost()).list_shortcuts()
        assert result is None


# ---------------------------------------------------------------------- #
# list_capabilities
# ---------------------------------------------------------------------- #


class TestListCapabilities:
    def test_returns_real_operations(self, qapp):
        """调 capability_manifest.build_manifest()，返回真实 operations。"""
        from src.app.controllers.host_help_sources import HostHelpSources

        result = HostHelpSources(_FakeHost()).list_capabilities()
        assert result is not None, "list_capabilities 不应返回 None"
        assert "operations" in result and result["operations"], "应有 operations"
        assert "add_clip" in result["operations"], "应含 add_clip（COMPILABLE_OPERATIONS）"

    def test_effects_as_dicts(self, qapp):
        """effects 每项是 dict（含 effect_type），供 _add_capabilities 消费。"""
        from src.app.controllers.host_help_sources import HostHelpSources

        result = HostHelpSources(_FakeHost()).list_capabilities()
        if result.get("effects"):
            for fx in result["effects"]:
                assert isinstance(fx, dict), f"effect 应是 dict：{type(fx)}"
                assert "effect_type" in fx, "effect dict 应含 effect_type"


# ---------------------------------------------------------------------- #
# list_features
# ---------------------------------------------------------------------- #


class TestListFeatures:
    def test_returns_feature_flags(self, qapp):
        """遍历 feature_flags，返回 {feature_id, enabled, label}。"""
        from src.app.controllers.host_help_sources import HostHelpSources

        host = _FakeHost(feature_flags=_FakeFeatureFlags())
        result = HostHelpSources(host).list_features()
        assert result is not None
        assert len(result) >= 3
        ids = {f["feature_id"] for f in result}
        assert {"edit", "color", "ai_assistant"} <= ids

    def test_item_shape(self, qapp):
        from src.app.controllers.host_help_sources import HostHelpSources

        host = _FakeHost(feature_flags=_FakeFeatureFlags())
        result = HostHelpSources(host).list_features()
        for f in result:
            assert {"feature_id", "enabled", "label"} <= set(f.keys())

    def test_disabled_flag_reflected(self, qapp):
        """enabled 字段反映 feature_flags 真实状态（ai_assistant=False）。"""
        from src.app.controllers.host_help_sources import HostHelpSources

        host = _FakeHost(feature_flags=_FakeFeatureFlags())
        result = HostHelpSources(host).list_features()
        ai = next(f for f in result if f["feature_id"] == "ai_assistant")
        assert ai["enabled"] is False, "ai_assistant 应为 False"

    def test_no_feature_flags_returns_none(self, qapp):
        """host 无 feature_flags → 返回 None。"""
        from src.app.controllers.host_help_sources import HostHelpSources

        result = HostHelpSources(_FakeHost()).list_features()
        assert result is None


# ---------------------------------------------------------------------- #
# list_panels
# ---------------------------------------------------------------------- #


class TestListPanels:
    def test_returns_pages_from_registry(self, qapp):
        """从 feature_registry.get_all_pages() 取面板。"""
        from src.app.controllers.host_help_sources import HostHelpSources

        host = _FakeHost(
            feature_flags=_FakeFeatureFlags(),
            feature_registry=_FakeFeatureRegistry(pages=[
                _FakePageDescriptor(page_id="edit.timeline", title="时间线"),
            ], modules={"edit": _FakeFeatureModule("edit")}),
        )
        result = HostHelpSources(host).list_panels()
        assert result is not None and len(result) == 1
        assert result[0]["panel_id"] == "edit.timeline"

    def test_item_shape(self, qapp):
        """每项含 {panel_id, title, category, page_ids, feature_id, enabled}。"""
        from src.app.controllers.host_help_sources import HostHelpSources

        host = _FakeHost(
            feature_flags=_FakeFeatureFlags(),
            feature_registry=_FakeFeatureRegistry(pages=[_FakePageDescriptor()]),
        )
        result = HostHelpSources(host).list_panels()
        assert result and len(result) == 1
        item = result[0]
        assert {"panel_id", "title", "category", "page_ids", "feature_id", "enabled"} <= set(item.keys()), (
            f"panel 项形状错误：{item.keys()}"
        )

    def test_no_registry_returns_none(self, qapp):
        """host 无 feature_registry → 返回 None。"""
        from src.app.controllers.host_help_sources import HostHelpSources

        result = HostHelpSources(_FakeHost()).list_panels()
        assert result is None


# ---------------------------------------------------------------------- #
# 单源异常 / 整体降级
# ---------------------------------------------------------------------- #


class TestResilience:
    def test_single_source_exception_returns_none(self, qapp):
        """单源抛异常 → 该方法返回 None，不影响其他源。"""

        class _BadManager:
            def get_all_actions(self):
                raise RuntimeError("boom")

        from src.app.controllers.host_help_sources import HostHelpSources

        host = _FakeHost(shortcut_manager=_BadManager())
        # shortcuts 异常 → None
        assert HostHelpSources(host).list_shortcuts() is None
        # 其他源不受影响（diagnostics 是模块常量，应正常）
        diag = HostHelpSources(host).list_diagnostics()
        assert diag is not None and len(diag) >= 20

    def test_help_sources_is_usable_for_help_index_build(self, qapp):
        """HostHelpSources 能直接喂给 HelpIndex.build 不报错（Protocol 兼容）。"""
        from src.app.controllers.host_help_sources import HostHelpSources
        from src.features.edit.ai_editing.help_index import HelpIndex

        host = _FakeHost(
            shortcut_manager=_FakeShortcutManager(actions=[
                _FakeShortcutAction(id="edit.add_keyframe", label="加关键帧",
                                    category="edit", default_shortcut="K"),
            ]),
            feature_flags=_FakeFeatureFlags(),
            feature_registry=_FakeFeatureRegistry(pages=[
                _FakePageDescriptor(page_id="edit.timeline", title="时间线"),
            ]),
        )
        index = HelpIndex.build(HostHelpSources(host))
        # 至少诊断码 + 快捷键 + features + panels 都进了
        assert index.count >= 25, f"HelpIndex 条目偏少：{index.count}"
