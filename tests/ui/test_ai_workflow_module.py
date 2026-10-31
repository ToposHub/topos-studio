"""P8.6 测试：AiWorkflowModule + FeatureModule 装配。

验证 :class:`AiWorkflowModule` 的契约（plan §10.7 / FeatureModule Protocol）：

- ``feature_id == "ai_workflow"``。
- ``is_available()`` 在 PyQt6 可用时 True。
- ``register_i18n()`` 加载翻译（``panel.ai_workflow`` key 可查）。
- ``register_panels()`` 注册 :class:`PanelDescriptor`（dock_attr / page_ids / default_visible）。
- ``register_pages()`` 返回空（不注册顶级 page）。
"""
from __future__ import annotations

import pytest


def test_module_feature_id() -> None:
    from src.features.ai_workflow.module import AiWorkflowModule
    m = AiWorkflowModule()
    assert m.feature_id == "ai_workflow"


def test_module_is_available_with_pyqt(qapp) -> None:
    from src.features.ai_workflow.module import AiWorkflowModule
    m = AiWorkflowModule()
    # PyQt6 可用（qapp fixture 已建）→ 应 True
    assert m.is_available() is True


def test_register_i18n_loads_translations() -> None:
    from src.features.ai_workflow.module import AiWorkflowModule
    from src.shared.i18n.i18n import I18N
    AiWorkflowModule().register_i18n()
    # panel.ai_workflow key 应可查
    val = I18N.tr("panel.ai_workflow", None)
    assert val is not None
    assert "workflow" in val.lower() or "工作流" in val


def test_register_pages_returns_empty() -> None:
    """AI Workflow 不注册顶级 page（作为 Edit 页 dock）。"""
    from src.features.ai_workflow.module import AiWorkflowModule
    m = AiWorkflowModule()
    pages = m.register_pages(context=None)
    assert pages == []


class _FakePanelRegistry:
    def __init__(self):
        self.registered = []

    def register(self, descriptor):
        self.registered.append(descriptor)


def test_register_panels_registers_descriptor(qapp) -> None:
    """register_panels 改为 no-op（AI Workflow 走右键弹窗，不再注册 dock descriptor）。"""
    from src.features.ai_workflow.module import AiWorkflowModule
    m = AiWorkflowModule()
    fake = _FakePanelRegistry()
    m.register_panels(fake)
    # 改走右键弹窗后不再注册任何 descriptor（Window > Panels 不出现 AI Workflow）
    assert len(fake.registered) == 0


def test_register_panels_noop_when_unavailable(monkeypatch) -> None:
    from src.features.ai_workflow.module import AiWorkflowModule
    m = AiWorkflowModule()
    # 强制 _available = False
    m._available = False
    fake = _FakePanelRegistry()
    m.register_panels(fake)
    assert len(fake.registered) == 0


def test_module_loader_registers_when_flag_on(monkeypatch) -> None:
    """module_loader 在 ai_workflow flag 开时注册 AiWorkflowModule。"""
    from src.app.module_loader import register_builtin_modules
    from src.app.feature_registry import FeatureRegistry

    # 构造一个 fake context，ai_workflow flag 开
    class _FakeFlags:
        def is_enabled(self, name):
            return name == "ai_workflow"

    class _FakeContext:
        feature_flags = _FakeFlags()
        save_router = None
        panel_registry = None
        action_registry = None
        i18n_loaded = False

    registry = FeatureRegistry()
    registry._context = _FakeContext()

    register_builtin_modules(registry)

    # AiWorkflowModule 应被注册
    features = registry.get_available_features()
    assert "ai_workflow" in features


def test_module_loader_skips_when_flag_off(monkeypatch) -> None:
    """module_loader 在 ai_workflow flag 关时不注册。"""
    from src.app.module_loader import register_builtin_modules
    from src.app.feature_registry import FeatureRegistry

    class _FakeFlags:
        def is_enabled(self, name):
            return False  # 全关

    class _FakeContext:
        feature_flags = _FakeFlags()
        save_router = None
        panel_registry = None
        action_registry = None
        i18n_loaded = False

    registry = FeatureRegistry()
    registry._context = _FakeContext()

    register_builtin_modules(registry)
    features = registry.get_available_features()
    assert "ai_workflow" not in features
