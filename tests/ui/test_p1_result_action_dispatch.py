"""P1.5 动作接线批次1：panel signal 转发 + controller 单一入口分派。

> 验证：
> - panel 有 result_action_triggered signal（转发 AiResultCard.action_triggered）
> - controller._on_result_action 按 action_kind 分派到 retry/adjust_params
> - 未知 action_kind 不崩（no-op）
> - approve/apply 留下批（仅 log，不崩）
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from src.features.ai_harness.result_card import error_result
from tests.ui.helpers import find_flow_widget

pytestmark = pytest.mark.edit_core


# ---------------------------------------------------------------------- #
# panel signal 转发
# ---------------------------------------------------------------------- #


class TestPanelSignalForwarding:
    def test_panel_has_result_action_triggered_signal(self, qapp):
        """panel 有 result_action_triggered signal。"""
        from src.features.edit.ui.ai_edit_assistant_panel import AiEditAssistantPanel
        panel = AiEditAssistantPanel()
        assert hasattr(panel, "result_action_triggered"), (
            "panel 必须有 result_action_triggered signal"
        )

    def test_set_result_card_forwards_action_triggered(self, qapp):
        """set_result_card 后，卡的 action_triggered emit → panel.result_action_triggered emit。"""
        from src.features.edit.ui.ai_edit_assistant_panel import AiEditAssistantPanel
        from src.features.edit.ui.ai_result_card import AiResultCard

        panel = AiEditAssistantPanel()
        state = error_result(
            result_id="run-1", title="失败",
            error_code="E1", error_message="boom",
        )
        panel.set_result_card(state)

        received: list[tuple[str, str]] = []
        panel.result_action_triggered.connect(
            lambda rid, ak: received.append((rid, ak))
        )
        # 取最后插入的卡，emit 它的 action_triggered
        card = find_flow_widget(panel, AiResultCard)
        assert isinstance(card, AiResultCard)
        # retry 动作（FAILED 卡默认 enabled retry）
        card.action_triggered.emit("run-1", "retry")
        assert received == [("run-1", "retry")], "panel 应转发 action_triggered"


# ---------------------------------------------------------------------- #
# controller 单一入口分派
# ---------------------------------------------------------------------- #


def _make_controller(qapp):
    """构造带最小依赖的 AiEditController（fake service/bridge）。"""
    from src.app.controllers.ai_edit_controller import AiEditController
    from src.features.edit.ui.ai_edit_assistant_panel import AiEditAssistantPanel

    panel = AiEditAssistantPanel()
    service = MagicMock()
    bridge = MagicMock()
    return AiEditController(panel=panel, service=service, bridge=bridge)


class TestControllerDispatch:
    def test_controller_has_on_result_action_method(self, qapp):
        """controller 有 _on_result_action 方法。"""
        controller = _make_controller(qapp)
        assert hasattr(controller, "_on_result_action"), (
            "controller 必须有 _on_result_action 方法"
        )

    def test_dispatch_retry_calls_retry_handler(self, qapp, monkeypatch):
        """_on_result_action(run_id, 'retry') 调 _on_result_action_retry。"""
        controller = _make_controller(qapp)
        called: list[str] = []
        monkeypatch.setattr(
            controller, "_on_result_action_retry",
            lambda rid: called.append(rid),
        )
        controller._on_result_action("run-1", "retry")
        assert called == ["run-1"]

    def test_dispatch_adjust_params_calls_handler(self, qapp, monkeypatch):
        """_on_result_action(run_id, 'adjust_params') 调 _on_result_action_adjust_params。"""
        controller = _make_controller(qapp)
        called: list[str] = []
        monkeypatch.setattr(
            controller, "_on_result_action_adjust_params",
            lambda rid: called.append(rid),
        )
        controller._on_result_action("run-1", "adjust_params")
        assert called == ["run-1"]

    def test_dispatch_unknown_action_does_not_crash(self, qapp):
        """未知 action_kind 不崩（no-op）。"""
        controller = _make_controller(qapp)
        # 不应抛异常
        controller._on_result_action("run-1", "unknown_action")

    def test_dispatch_approve_apply_does_not_crash(self, qapp):
        """approve/apply 留下批，仅 log 不崩。"""
        controller = _make_controller(qapp)
        controller._on_result_action("run-1", "approve")
        controller._on_result_action("run-1", "apply")


# ---------------------------------------------------------------------- #
# controller 连接 panel signal
# ---------------------------------------------------------------------- #


class TestControllerConnection:
    def test_panel_signal_connected_to_controller(self, qapp):
        """panel.result_action_triggered 连接到 controller（receiver 数 > 0）。

        Qt signal 连接绑定的是方法对象引用，monkeypatch 替换实例属性不会改变
        已建立的连接，因此这里用 ``receivers()`` 验证连接存在而非替换方法。
        """
        from PyQt6.QtCore import QObject
        controller = _make_controller(qapp)
        # panel.result_action_triggered 至少有 1 个接收者（controller._on_result_action）
        receivers = controller._panel.receivers(
            getattr(type(controller._panel), "result_action_triggered")
        )
        assert receivers >= 1, (
            f"result_action_triggered 应至少连 1 个接收者，实际 {receivers}"
        )


# ---------------------------------------------------------------------- #
# retry/adjust_params handler 存在（实现见批次2/3，这里先验证签名存在）
# ---------------------------------------------------------------------- #


class TestHandlersExist:
    def test_retry_handler_exists(self, qapp):
        """controller 有 _on_result_action_retry 方法（批次2 实现）。"""
        controller = _make_controller(qapp)
        assert hasattr(controller, "_on_result_action_retry")

    def test_adjust_params_handler_exists(self, qapp):
        """controller 有 _on_result_action_adjust_params 方法（批次3 实现）。"""
        controller = _make_controller(qapp)
        assert hasattr(controller, "_on_result_action_adjust_params")
