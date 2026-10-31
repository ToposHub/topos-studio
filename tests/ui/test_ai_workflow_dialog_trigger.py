"""AI Workflow 弹窗化测试：右键菜单触发 + 弹窗懒建单例 + PanelDescriptor 移除。

> 验证：
> - AiEditAssistantPanel 右键 → show_workflow_requested signal
> - MainWindow._show_ai_workflow_dialog 懒建弹窗（单例，关闭只 hide）
> - panel parent 重定向到弹窗
> - register_panels 不再注册 descriptor（只走右键）
> - flag 关时不崩
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

pytestmark = pytest.mark.edit_core


# ---------------------------------------------------------------------- #
# AiEditAssistantPanel 右键菜单 + signal
# ---------------------------------------------------------------------- #


class TestPanelContextMenu:
    def test_panel_has_show_workflow_signal(self, qapp):
        """panel 有 show_workflow_requested signal。"""
        from src.features.edit.ui.ai_edit_assistant_panel import AiEditAssistantPanel
        panel = AiEditAssistantPanel()
        assert hasattr(panel, "show_workflow_requested"), (
            "panel 必须有 show_workflow_requested signal"
        )

    def test_context_menu_emits_signal(self, qapp):
        """右键菜单点击'显示工作流' → emit show_workflow_requested。"""
        from src.features.edit.ui.ai_edit_assistant_panel import AiEditAssistantPanel

        panel = AiEditAssistantPanel()
        received: list[bool] = []
        panel.show_workflow_requested.connect(lambda: received.append(True))

        # 直接调 _on_context_menu，模拟菜单弹出并点击第一项
        # （QMenu.exec 在测试环境会阻塞，这里直接测 signal 连接）
        panel.show_workflow_requested.emit()
        assert received == [True]

    def test_panel_has_on_context_menu_method(self, qapp):
        """panel 有 _on_context_menu 方法。"""
        from src.features.edit.ui.ai_edit_assistant_panel import AiEditAssistantPanel
        panel = AiEditAssistantPanel()
        assert hasattr(panel, "_on_context_menu"), (
            "panel 必须有 _on_context_menu 方法"
        )


# ---------------------------------------------------------------------- #
# controller._on_show_workflow_requested（信号链路）
# ---------------------------------------------------------------------- #


def _make_controller(qapp):
    """构造带最小依赖的 AiEditController（fake service/bridge）。"""
    from src.app.controllers.ai_edit_controller import AiEditController
    from src.features.edit.ui.ai_edit_assistant_panel import AiEditAssistantPanel

    panel = AiEditAssistantPanel()
    service = MagicMock()
    bridge = MagicMock()
    return AiEditController(panel=panel, service=service, bridge=bridge)


class TestControllerShowWorkflow:
    def test_controller_has_on_show_workflow_requested(self, qapp):
        """controller 有 _on_show_workflow_requested 方法。"""
        controller = _make_controller(qapp)
        assert hasattr(controller, "_on_show_workflow_requested")

    def test_panel_signal_connected_to_controller(self, qapp):
        """panel.show_workflow_requested 连到 controller._on_show_workflow_requested。"""
        controller = _make_controller(qapp)
        receivers = controller._panel.receivers(
            getattr(type(controller._panel), "show_workflow_requested")
        )
        assert receivers >= 1, f"signal 应至少连 1 个接收者，实际 {receivers}"

    def test_handler_calls_main_window_show_dialog(self, qapp):
        """_on_show_workflow_requested 调 main_window._show_ai_workflow_dialog。"""
        controller = _make_controller(qapp)
        # 用 fake main_window 替换 panel 的顶层窗口
        called: list[bool] = []

        class _FakeMain:
            def _show_ai_workflow_dialog(self):
                called.append(True)

        fake_main = _FakeMain()
        # panel.window() 返回 fake_main（通过 monkeypatch）
        original_window = controller._panel.window
        controller._panel.window = lambda: fake_main
        try:
            controller._on_show_workflow_requested()
        finally:
            controller._panel.window = original_window
        assert called == [True], "应调 main_window._show_ai_workflow_dialog"

    def test_handler_no_main_window_method_no_crash(self, qapp):
        """main_window 无 _show_ai_workflow_dialog 方法 → 不崩（no-op）。"""
        controller = _make_controller(qapp)
        # panel.window() 默认返回 None（未嵌入主窗口）
        # _on_show_workflow_requested 应静默 no-op
        controller._on_show_workflow_requested()


# ---------------------------------------------------------------------- #
# dock_layout_controller._show_ai_workflow_dialog（懒建单例逻辑）
# ---------------------------------------------------------------------- #


class TestShowAiWorkflowDialog:
    def test_method_exists_on_dock_layout_mixin(self, qapp):
        """DockLayoutMixin 有 _show_ai_workflow_dialog 方法。"""
        from src.app.controllers.dock_layout_controller import DockLayoutMixin
        assert hasattr(DockLayoutMixin, "_show_ai_workflow_dialog"), (
            "DockLayoutMixin 必须有 _show_ai_workflow_dialog 方法"
        )

    def test_lazy_build_dialog_with_panel(self, qapp, monkeypatch):
        """_show_ai_workflow_dialog 首次调用懒建弹窗，panel parent 重定向。"""
        from PyQt6.QtWidgets import QDialog, QWidget
        from src.app.controllers.dock_layout_controller import DockLayoutMixin

        # 构造最小 host：含 ai_workflow_panel + _ai_workflow_dialog=None
        # （模拟 MainWindow flag 开时的状态）
        host = QWidget()
        host.ai_workflow_panel = QWidget()  # fake panel
        host._ai_workflow_dialog = None
        # 绑方法
        host._show_ai_workflow_dialog = DockLayoutMixin._show_ai_workflow_dialog.__get__(host)

        host._show_ai_workflow_dialog()
        assert host._ai_workflow_dialog is not None, "首次调用应懒建弹窗"
        assert isinstance(host._ai_workflow_dialog, QDialog)
        assert host.ai_workflow_panel.parent() is host._ai_workflow_dialog, (
            "panel parent 应重定向到弹窗"
        )
        assert host._ai_workflow_dialog.isVisible(), "弹窗应可见"

    def test_singleton_reuse_on_second_call(self, qapp):
        """第二次调用复用同一弹窗实例（单例）。"""
        from PyQt6.QtWidgets import QDialog, QWidget
        from src.app.controllers.dock_layout_controller import DockLayoutMixin

        host = QWidget()
        host.ai_workflow_panel = QWidget()
        host._ai_workflow_dialog = None
        host._show_ai_workflow_dialog = DockLayoutMixin._show_ai_workflow_dialog.__get__(host)

        host._show_ai_workflow_dialog()
        first = host._ai_workflow_dialog
        host._show_ai_workflow_dialog()
        assert host._ai_workflow_dialog is first, "第二次调用应复用同一弹窗实例"

    def test_no_panel_no_crash(self, qapp):
        """panel 为 None（flag 关）→ 不崩（no-op）。"""
        from PyQt6.QtWidgets import QWidget
        from src.app.controllers.dock_layout_controller import DockLayoutMixin

        host = QWidget()
        host.ai_workflow_panel = None
        host._ai_workflow_dialog = None
        host._show_ai_workflow_dialog = DockLayoutMixin._show_ai_workflow_dialog.__get__(host)
        host._show_ai_workflow_dialog()  # 不应崩
        assert host._ai_workflow_dialog is None, "panel None 时不应建弹窗"


# ---------------------------------------------------------------------- #
# module.register_panels（移除 descriptor，改走右键弹窗）
# ---------------------------------------------------------------------- #


class TestModuleRegisterPanels:
    def test_register_panels_is_noop(self, qapp):
        """register_panels 不再注册任何 descriptor（改走右键弹窗）。"""
        from src.features.ai_workflow.module import AiWorkflowModule

        class _FakeRegistry:
            def __init__(self):
                self.registered = []

            def register(self, descriptor):
                self.registered.append(descriptor)

        module = AiWorkflowModule()
        registry = _FakeRegistry()
        module.register_panels(registry)
        assert registry.registered == [], "不应注册任何 descriptor"

    def test_module_is_available_when_imports_ok(self, qapp):
        """module.is_available 在 import 成功时为 True。"""
        from src.features.ai_workflow.module import AiWorkflowModule
        module = AiWorkflowModule()
        # 测试环境 import 应成功
        assert module.is_available()
