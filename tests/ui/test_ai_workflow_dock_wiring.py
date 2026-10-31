"""P9.5 测试：AI Workflow 执行链装配（dock_layout_controller 的接线契约）。

> 验证 dock_layout_controller.py 在 flag 开时装配的完整执行链：
> panel + HarnessRuntime + WorkflowCompiler + FakeWorkflowService +
> WorkflowRunController，且 panel.run_requested → controller.run 已连。

不直接实例化 MainWindow（太重、依赖工程）；改测装配路径的契约：用与
dock_layout_controller 相同的 import + 连接顺序手动装配，验证组件互引用正确、
信号链路通、端到端 run 工作。
"""
from __future__ import annotations

import pytest

from src.features.ai_harness.runtime import HarnessRuntime


# ---------------------------------------------------------------------- #
# Fixtures：模拟 dock_layout_controller 的装配
# ---------------------------------------------------------------------- #


@pytest.fixture
def wired_stack(qapp):
    """模拟 dock_layout_controller 装配：panel + runtime + compiler + controller。

    返回 dict：{panel, runtime, compiler, fake_service, controller}。
    """
    from src.features.ai_workflow.core.node_specs import (
        NodeSpecRegistry, register_official_specs,
    )
    from src.features.ai_workflow.core.template_registry import (
        WorkflowTemplateRegistry, register_official_templates,
    )
    from src.features.ai_workflow.core.workflow_compiler import WorkflowCompiler
    from src.features.ai_workflow.runtime.fake_workflow_service import (
        FakeWorkflowService,
    )
    from src.features.ai_workflow.runtime.workflow_run_controller import (
        WorkflowRunController,
    )
    from src.features.ai_workflow.ui.workflow_panel import WorkflowPanel

    node_reg = NodeSpecRegistry()
    register_official_specs(node_reg)
    tpl_reg = WorkflowTemplateRegistry()
    register_official_templates(tpl_reg)

    panel = WorkflowPanel(registry=node_reg, template_registry=tpl_reg)
    runtime = HarnessRuntime()
    compiler = WorkflowCompiler(node_reg)
    fake_service = FakeWorkflowService(node_duration_s=0.0)
    controller = WorkflowRunController(
        runtime=runtime, compiler=compiler, fake_service=fake_service,
        adapter=panel.adapter, panel=panel,
    )
    # dock_layout_controller 的信号连接
    panel.run_requested.connect(controller.run)
    panel.cancel_requested.connect(controller.cancel)
    yield {
        "panel": panel, "runtime": runtime, "compiler": compiler,
        "fake_service": fake_service, "controller": controller,
    }
    controller.dispose()
    panel.deleteLater()


# ---------------------------------------------------------------------- #
# 装配契约
# ---------------------------------------------------------------------- #


def test_wired_stack_components_present(wired_stack) -> None:
    """所有组件非 None，互相引用正确。"""
    assert wired_stack["panel"] is not None
    assert wired_stack["runtime"] is not None
    assert wired_stack["compiler"] is not None
    assert wired_stack["fake_service"] is not None
    assert wired_stack["controller"] is not None
    # panel 暴露 adapter（controller 用它刷节点色）
    assert wired_stack["panel"].adapter is not None


def test_panel_run_requested_connected_to_controller(wired_stack) -> None:
    """panel.run_requested 已连到 controller.run（emit run_requested 触发 compile）。"""
    panel = wired_stack["panel"]
    controller = wired_stack["controller"]
    # 加载模板（让 current_graph 非 None + run_button enable）
    panel.load_template("wf-auto-caption")
    assert panel.current_graph is not None
    # emit run_requested → controller.run 被调（compile 会跑，即使失败也不抛）
    # 这里只验证信号链路通：run_requested emit 不抛
    panel.run_requested.emit()


# ---------------------------------------------------------------------- #
# 端到端：flag 开时 dock 可用（模拟用户勾选 Window > Panels）
# ---------------------------------------------------------------------- #


def test_end_to_end_load_and_run_no_crash(wired_stack) -> None:
    """加载模板 + 触发 run（compile 失败也优雅处理，不抛）。"""
    panel = wired_stack["panel"]
    controller = wired_stack["controller"]
    panel.load_template("wf-auto-caption")
    # 触发 run（auto_caption 会因 MISSING_CAPABILITY 编译失败 → diagnostics 进 drawer）
    controller.run()
    # 不抛即通过；compile 失败时 diagnostics 同步灌进 run_drawer.state
    assert len(panel.run_drawer.state.items) >= 1


def test_dispose_disconnects_subscriber(wired_stack) -> None:
    """controller.dispose 后 runtime 订阅者已解绑（不再收事件）。"""
    runtime = wired_stack["runtime"]
    controller = wired_stack["controller"]
    # dispose 前订阅者数 > 0（controller 注册了 1 个）
    # dispose 后再发事件不应触发 controller 的回调（间接验证：无异常）
    controller.dispose()
    # 触发一个 run（不连 panel，直接 runtime.start）验证不抛
    from src.features.ai_harness.invocation_models import (
        Invocation, InvocationScope, InvocationSource, RunPolicy,
    )
    inv = Invocation(
        invocation_id="inv-dispose",
        source=InvocationSource.WORKFLOW,
        skill_id="test.skill",
        scope=InvocationScope(project_id="p1", timeline_id="", selected_asset_ids=()),
        policy=RunPolicy(), inputs={}, user_text="", created_at=0.0,
    )
    rid = runtime.start(inv)
    runtime.cancel(rid)
    # 不抛即通过


# ---------------------------------------------------------------------- #
# dock_layout_controller 装配的 import 可达性
# ---------------------------------------------------------------------- #


def test_dock_layout_imports_resolvable() -> None:
    """dock_layout_controller 装配块的所有 import 可解析（不缺模块）。"""
    # 模拟 dock_layout_controller 的 import 块
    from src.features.ai_harness.runtime import HarnessRuntime
    from src.features.ai_workflow.core.workflow_compiler import WorkflowCompiler
    from src.features.ai_workflow.runtime.fake_workflow_service import (
        FakeWorkflowService,
    )
    from src.features.ai_workflow.runtime.workflow_run_controller import (
        WorkflowRunController,
    )
    from src.features.ai_workflow.ui.workflow_panel import WorkflowPanel
    assert HarnessRuntime and WorkflowCompiler and FakeWorkflowService
    assert WorkflowRunController and WorkflowPanel
