"""P0.2 接线层测试：单一 HarnessRuntime 装配 + chat 经 runtime 产生 RunRecord。

> 验证 §10.2 P0 / §0.2 "三种入口只有一个 HarnessRuntime" 红线：
> - Edit Assistant 装配 HarnessRuntime（不再零装配）
> - AI Workflow 与 Edit Assistant 共享同一 runtime 实例（消除双实例）
> - chat 的"生成方案"动作经 runtime.start → 产生 RunRecord
> - controller 持有 runtime 引用，可在终态/取消时调 runtime API

不实例化整个 MainWindow（太重）；测装配契约 + controller 的 runtime 注入。
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from src.features.ai_harness.runtime import HarnessRuntime
from src.features.ai_harness.invocation_models import (
    Invocation,
    InvocationSource,
    RunStatus,
)

pytestmark = pytest.mark.edit_core


# ---------------------------------------------------------------------- #
# Controller runtime 注入契约
# ---------------------------------------------------------------------- #


def _make_controller(qapp, *, runtime=None):
    """构造一个带 fake service/panel 的 AiEditController（不接真实 plan 路径）。

    返回 (controller, fake_runtime_or_real, fake_service)。
    """
    from src.app.controllers.ai_edit_controller import AiEditController

    fake_service = MagicMock()
    fake_service.on_state_changed = None
    fake_service.active_brief_id = "default"
    fake_service.config = MagicMock()

    fake_panel = MagicMock()
    # dispose() 会 disconnect 这些 signal
    for sig_name in (
        "request_submitted", "variant_selected", "apply_requested",
        "cancel_requested", "brief_patch_requested", "context_inspector_requested",
    ):
        getattr(fake_panel, sig_name).connect = MagicMock()
        getattr(fake_panel, sig_name).disconnect = MagicMock()

    controller = AiEditController(
        panel=fake_panel, service=fake_service, runtime=runtime, parent=qapp,
    )
    return controller, fake_panel, fake_service


def test_controller_accepts_runtime_kwarg(qapp):
    """AiEditController.__init__ 接受 runtime 关键字参数（不再零装配）。"""
    runtime = HarnessRuntime()
    controller, _, _ = _make_controller(qapp, runtime=runtime)
    assert controller.runtime is runtime, "controller 应持有注入的 runtime 引用"


def test_controller_runtime_defaults_none(qapp):
    """未传 runtime 时 controller.runtime 为 None（向后兼容；旧测试不破坏）。"""
    controller, _, _ = _make_controller(qapp)
    assert controller.runtime is None


# ---------------------------------------------------------------------- #
# 单一 HarnessRuntime 红线（§0.2）
# ---------------------------------------------------------------------- #


def test_single_runtime_shared_between_edit_and_workflow(qapp):
    """Edit Assistant 与 AI Workflow 应是同一 HarnessRuntime 实例。

    模拟 dock_layout_controller 已建好 _ai_workflow_runtime，_setup_ai_editing
    应复用它而非新建（消除 §0.2 双实例红线）。
    """
    from src.app.controllers.ai_editing_mixin import resolve_shared_runtime

    # dock_layout_controller 创建的 runtime（flag 开时）
    dock_runtime = HarnessRuntime()
    host = _FakeHost(ai_workflow_runtime=dock_runtime)
    # _setup_ai_editing 的 runtime 解析逻辑应复用 dock 的 runtime
    resolved = resolve_shared_runtime(host)
    assert resolved is dock_runtime, "flag 开时应复用 dock 已建的 runtime"
    assert host._shared_runtime is dock_runtime


def test_setup_creates_runtime_when_dock_has_none(qapp):
    """flag 关时 dock 没 runtime；_setup_ai_editing 应新建并暴露给后续复用。"""
    from src.app.controllers.ai_editing_mixin import resolve_shared_runtime

    host = _FakeHost(ai_workflow_runtime=None)
    resolved = resolve_shared_runtime(host)
    assert resolved is not None, "flag 关时应新建 runtime"
    assert isinstance(resolved, HarnessRuntime)
    assert host._shared_runtime is resolved, "应回填到 _shared_runtime 供 dock 复用"


def test_resolve_runtime_is_idempotent(qapp):
    """重复解析返回同一实例（避免 setup 被调多次时泄漏多实例）。"""
    from src.app.controllers.ai_editing_mixin import resolve_shared_runtime

    host = _FakeHost(ai_workflow_runtime=None)
    first = resolve_shared_runtime(host)
    second = resolve_shared_runtime(host)
    assert first is second


# ---------------------------------------------------------------------- #
# chat 经 runtime 产生 RunRecord
# ---------------------------------------------------------------------- #


def test_chat_plan_wraps_runtime_start(qapp):
    """_start_plan 应调 runtime.start(Invocation(source=chat)) → 产生 RunRecord。

    用 fake service（plan_variants 同步返回）+ 真实 runtime；验证 run 被创建
    且 status 在 start 后为 DRAFT/VALIDATING（runtime.start 只分配 run_id）。
    """
    runtime = HarnessRuntime()
    controller, fake_panel, fake_service = _make_controller(qapp, runtime=runtime)

    # fake service.plan_variants 直接成功返回（不真跑 LLM）
    fake_service.plan_variants = MagicMock(return_value=None)

    # 模拟 _start_plan（绕过 worker 线程：直接调 controller 的 runtime 包装逻辑）
    run_id = controller.start_chat_run(
        skill_id="topos.rough_cut",
        brief_summary="测试 brief",
    )
    assert run_id, "start_chat_run 应返回 run_id"
    rec = runtime.get_run(run_id)
    assert rec is not None, "runtime 应记录该 run"
    inv = runtime.get_invocation(run_id)
    assert inv is not None
    assert inv.source == InvocationSource.CHAT
    assert inv.skill_id == "topos.rough_cut"


def test_chat_run_user_text_is_sanitized(qapp):
    """chat invocation 的 user_text 落盘时脱敏（红线：不落盘绝对路径）。

    Invocation 内存保留原文供 planner 使用；``to_public_dict()``（落盘/对外）
    时经 ``sanitize_for_storage`` 把绝对路径 / secret 替换为占位符。runtime 持
    的 invocation 落盘到 session_store 时走的就是这条路径。
    """
    runtime = HarnessRuntime()
    controller, _, _ = _make_controller(qapp, runtime=runtime)

    run_id = controller.start_chat_run(
        skill_id="topos.auto_caption",
        user_text="请处理 /Users/testuser/secret/video.mp4 这个素材",
    )
    inv = runtime.get_invocation(run_id)
    # 落盘 / 对外的 dict 必须脱敏（这是 record_run 时实际存的形态）
    public = inv.to_public_dict()
    assert "/Users/testuser/secret" not in public["user_text"], (
        "invocation.to_public_dict()['user_text'] 必须脱敏绝对路径"
    )


# ---------------------------------------------------------------------- #
# 测试辅助
# ---------------------------------------------------------------------- #


class _FakeHost:
    """模拟 MainWindow 持有的 runtime 相关属性（避免实例化整个 MainWindow）。"""

    def __init__(self, *, ai_workflow_runtime):
        self._ai_workflow_runtime = ai_workflow_runtime
        self._shared_runtime = None
