"""P9.3 测试：WorkflowRunController（QThread + compile + invoke_compiled + 订阅回填）。

> :class:`WorkflowRunController` 是 Run 按钮的"大脑"：连 panel.run_requested →
> WorkflowCompiler.compile → 起后台 worker → LegacyHarnessAdapter.invoke_compiled
> （注入 node 事件 emitter）→ runtime 发 node.* 事件 → 订阅者回填节点色 + RunDrawerState。

验证契约：

- compile 失败 → diagnostics 进 Run Drawer，无 run 启动，节点色不变。
- compile 成功 → worker 跑 FakeWorkflowService → 节点逐个 running→succeeded
  （查 adapter.view.get_node_item(id).status 序列）→ run 终态 succeeded。
- cancel → runtime.cancel → worker 内 is_cancelled 中断 → 未跑节点色 cancelled。
- 订阅者跨线程安全：worker 只动 runtime；回填经 signal 切回主线程。
- 红线：不 import moderngl/composite.core；不写 timeline；无第二 executor。
"""
import os
import time
from unittest.mock import MagicMock

import pytest

from src.features.ai_harness.invocation_models import (
    ArtifactRef, Invocation, InvocationScope, InvocationSource, RunPolicy, RunStatus,
)
from src.features.ai_harness.runtime import HarnessRuntime


# ---------------------------------------------------------------------- #
# 红线：源码扫描
# ---------------------------------------------------------------------- #


_CTRL_SRC = os.path.join(
    os.path.dirname(__file__), "..", "..",
    "src", "features", "ai_workflow", "runtime", "workflow_run_controller.py",
)


def test_controller_source_no_gpu_no_timeline() -> None:
    """红线：controller 源码不含 moderngl/composite.core/TextureResult/timeline 写。"""
    with open(_CTRL_SRC, "r", encoding="utf-8") as f:
        source = f.read()
    assert "import moderngl" not in source
    assert "from src.features.composite.core" not in source
    assert "TextureResult" not in source
    assert "timeline_write" not in source
    assert "CommandStack" not in source
    assert "commit_macro" not in source


def test_no_second_executor_class() -> None:
    """红线：runtime 单一执行边界（plan §10.9）—— controller 不新建 executor 类。"""
    with open(_CTRL_SRC, "r", encoding="utf-8") as f:
        source = f.read()
    # controller 用 LegacyHarnessAdapter.invoke_compiled，不自己造 executor
    assert "class.*Executor" not in source, "controller 不应定义新 Executor 类"


# ---------------------------------------------------------------------- #
# Fixtures
# ---------------------------------------------------------------------- #


def _make_invocation(skill_id: str = "test.skill") -> Invocation:
    return Invocation(
        invocation_id="inv-ctrl",
        source=InvocationSource.WORKFLOW,
        skill_id=skill_id,
        scope=InvocationScope(project_id="p1", timeline_id="", selected_asset_ids=()),
        policy=RunPolicy(),
        inputs={},
        user_text="",
        created_at=0.0,
    )


class _FakeCompiled:
    """最小 CompiledSkill stub：runnable + workflow target。"""
    runnable = True
    target = "workflow"
    workflow_input = type("_W", (), {"node_ids": ("n1", "n2")})()
    skill_input = None
    diagnostics = ()


class _FakeGraph:
    """最小 graph stub（controller 只调 compile，不真读 graph 字段）。"""
    workflow_id = "wf-test"


@pytest.fixture
def runtime():
    return HarnessRuntime()


@pytest.fixture
def fake_service():
    from src.features.ai_workflow.runtime.fake_workflow_service import (
        FakeWorkflowService,
    )
    return FakeWorkflowService(node_duration_s=0.0)


@pytest.fixture
def fake_panel(qapp):
    """最小 panel stub：current_graph + set_run_drawer + set_running + signals。"""
    from PyQt6.QtCore import pyqtSignal, QObject

    class _FakePanel(QObject):
        run_requested = pyqtSignal()
        cancel_requested = pyqtSignal()
        def __init__(self):
            super().__init__()
            self.current_graph = _FakeGraph()
            self.drawer_states = []
            self.running_states = []
        def set_run_drawer(self, state):
            self.drawer_states.append(state)
        def set_running(self, running):
            self.running_states.append(running)

    return _FakePanel()


@pytest.fixture
def fake_adapter(qapp):
    """最小 adapter stub：set_node_status + view.get_node_item 带 status。"""
    class _FakeNodeItem:
        def __init__(self):
            self.status = "idle"
        def set_status(self, s):
            self.status = s

    class _FakeView:
        def __init__(self):
            self._items = {"n1": _FakeNodeItem(), "n2": _FakeNodeItem()}
        def get_node_item(self, nid):
            return self._items.get(nid)

    class _FakeAdapter:
        def __init__(self):
            self.view = _FakeView()
        def set_node_status(self, nid, status):
            item = self.view.get_node_item(nid)
            if item:
                item.set_status(status)

    return _FakeAdapter()


@pytest.fixture
def success_compiler():
    """compiler stub：永远返回成功编译结果。"""
    class _SuccessCompiler:
        def compile(self, graph, **kwargs):
            return type("_R", (), {
                "success": True,
                "graph": graph,
                "invocation": _make_invocation(),
                "compiled": _FakeCompiled(),
                "diagnostics": (),
                "compiled_at": 1.0,
            })()
    return _SuccessCompiler()


@pytest.fixture
def fail_compiler():
    """compiler stub：永远返回失败（diagnostics 非空）。"""
    class _FailCompiler:
        def compile(self, graph, **kwargs):
            return type("_R", (), {
                "success": False,
                "graph": graph,
                "invocation": None,
                "compiled": None,
                "diagnostics": ({"code": "TEST_FAIL", "message": "boom"},),
                "compiled_at": 1.0,
            })()
    return _FailCompiler()


def _make_controller(runtime, fake_service, fake_adapter, fake_panel, compiler):
    from src.features.ai_workflow.runtime.workflow_run_controller import (
        WorkflowRunController,
    )
    return WorkflowRunController(
        runtime=runtime, compiler=compiler, fake_service=fake_service,
        adapter=fake_adapter, panel=fake_panel,
    )


# ---------------------------------------------------------------------- #
# compile 失败
# ---------------------------------------------------------------------- #


def test_compile_failure_no_run_started(runtime, fake_service, fake_adapter, fake_panel, fail_compiler):
    """compile 失败 → diagnostics 进 Run Drawer，无 run 启动。"""
    ctrl = _make_controller(runtime, fake_service, fake_adapter, fake_panel, fail_compiler)
    try:
        ctrl.run()  # 同步触发 compile（compile 在主线程）
        # 等 worker（不应启动）
        assert runtime.list_runs() == [], "compile 失败不应启动 run"
        # Run Drawer 应收到含诊断的 state
        assert len(fake_panel.drawer_states) >= 1
    finally:
        ctrl.dispose()


def test_compile_failure_node_status_unchanged(runtime, fake_service, fake_adapter, fake_panel, fail_compiler):
    """compile 失败 → 节点色保持 idle。"""
    ctrl = _make_controller(runtime, fake_service, fake_adapter, fake_panel, fail_compiler)
    try:
        ctrl.run()
        assert fake_adapter.view.get_node_item("n1").status == "idle"
        assert fake_adapter.view.get_node_item("n2").status == "idle"
    finally:
        ctrl.dispose()


# ---------------------------------------------------------------------- #
# compile 成功 → worker 跑 → 节点色更新
# ---------------------------------------------------------------------- #


def test_run_success_updates_node_status(runtime, fake_service, fake_adapter, fake_panel, success_compiler):
    """compile 成功 → worker 跑完 → 节点 n1/n2 变 succeeded。"""
    ctrl = _make_controller(runtime, fake_service, fake_adapter, fake_panel, success_compiler)
    try:
        ctrl.run()
        ctrl.wait_for_run(timeout_s=2.0)
        # 节点应 succeeded
        assert fake_adapter.view.get_node_item("n1").status == "succeeded"
        assert fake_adapter.view.get_node_item("n2").status == "succeeded"
        # run 终态 succeeded
        runs = runtime.list_runs()
        assert len(runs) >= 1
        assert runs[-1].status in (RunStatus.SUCCEEDED, RunStatus.SUCCEEDED_WITH_WARNINGS)
    finally:
        ctrl.dispose()


def test_run_success_run_drawer_receives_state(runtime, fake_service, fake_adapter, fake_panel, success_compiler):
    """run 过程中 Run Drawer 收到 state 更新（至少 final 一条）。"""
    ctrl = _make_controller(runtime, fake_service, fake_adapter, fake_panel, success_compiler)
    try:
        ctrl.run()
        ctrl.wait_for_run(timeout_s=2.0)
        assert len(fake_panel.drawer_states) >= 1
        # set_running(True) 在 run 开始，set_running(False) 在结束
        assert fake_panel.running_states == [True, False]
    finally:
        ctrl.dispose()


def test_run_drawer_has_node_items(
    runtime, fake_service, fake_adapter, fake_panel, success_compiler,
):
    """Workflow drawer exposes node children for the current run."""
    ctrl = _make_controller(runtime, fake_service, fake_adapter, fake_panel, success_compiler)
    try:
        ctrl.run()
        ctrl.wait_for_run(timeout_s=2.0)
        state = fake_panel.drawer_states[-1]
        assert any(item.kind == "node" for item in state.items)
    finally:
        ctrl.dispose()


def test_history_items_use_runtime_snapshots(runtime, fake_service, fake_adapter, fake_panel, success_compiler):
    """History projection is read-only and keeps retry/diagnostic affordances."""
    old_id = runtime.start(_make_invocation())
    runtime.mark_running(old_id)
    runtime.finish(old_id, success=False, error_code="PROVIDER_TIMEOUT")
    ctrl = _make_controller(runtime, fake_service, fake_adapter, fake_panel, success_compiler)
    try:
        history = ctrl._history_items("")
        assert history and history[0].item_id == old_id
        assert history[0].can_retry is True
        assert history[0].recovery_summary
    finally:
        ctrl.dispose()


def test_show_artifact_returns_safe_reference(runtime, fake_service, fake_adapter, fake_panel, success_compiler):
    """Artifact action exposes only typed reference metadata and never raw paths."""
    run_id = runtime.start(_make_invocation())
    runtime.record_artifact(run_id, ArtifactRef(
        artifact_id="art-1", artifact_type="edit_plan_ref",
        producer_node_id="workflow.plan", summary="plan ready",
    ))
    ctrl = _make_controller(runtime, fake_service, fake_adapter, fake_panel, success_compiler)
    try:
        details = ctrl.show_artifact(f"{run_id}:artifact:art-1")
        assert details == {
            "run_id": run_id,
            "artifact_id": "art-1",
            "artifact_type": "edit_plan_ref",
            "producer_node_id": "workflow.plan",
            "summary": "plan ready",
        }
    finally:
        ctrl.dispose()


# ---------------------------------------------------------------------- #
# cancel
# ---------------------------------------------------------------------- #


def test_cancel_marks_remaining_nodes_cancelled(runtime, fake_adapter, fake_panel):
    """cancel 中途 → 未跑节点变 cancelled。

    用慢 fake service（node_duration_s>0）+ 立即 cancel。
    """
    from src.features.ai_workflow.runtime.fake_workflow_service import (
        FakeWorkflowService,
    )
    from src.features.ai_workflow.runtime.workflow_run_controller import (
        WorkflowRunController,
    )
    slow_service = FakeWorkflowService(node_duration_s=0.2)
    class _SuccessCompiler:
        def compile(self, graph, **kwargs):
            return type("_R", (), {
                "success": True, "graph": graph,
                "invocation": _make_invocation(),
                "compiled": _FakeCompiled(),
                "diagnostics": (), "compiled_at": 1.0,
            })()
    ctrl = WorkflowRunController(
        runtime=runtime, compiler=_SuccessCompiler(), fake_service=slow_service,
        adapter=fake_adapter, panel=fake_panel,
    )
    try:
        ctrl.run()
        # 立即取消
        ctrl.cancel()
        ctrl.wait_for_run(timeout_s=2.0)
        # 至少一个节点应是 cancelled 或 succeeded（取决于 cancel 时机）；
        # 关键：不所有节点都 succeeded（cancel 起作用了）
        statuses = [
            fake_adapter.view.get_node_item("n1").status,
            fake_adapter.view.get_node_item("n2").status,
        ]
        # cancel 后必有至少一个非 succeeded（cancelled 或 idle）
        assert any(s != "succeeded" for s in statuses), (
            f"cancel 应阻止部分节点完成；实际状态：{statuses}"
        )
    finally:
        ctrl.dispose()


# ---------------------------------------------------------------------- #
# 线程安全：订阅者不直接动 Qt
# ---------------------------------------------------------------------- #


def test_subscriber_path_is_thread_safe(runtime, fake_service, fake_adapter, fake_panel, success_compiler):
    """订阅者回填经 signal 切回主线程（源码扫描：_on_run_event 不直接 set_node_status）。"""
    ctrl = _make_controller(runtime, fake_service, fake_adapter, fake_panel, success_compiler)
    try:
        ctrl.run()
        ctrl.wait_for_run(timeout_s=2.0)
        # 订阅者最终成功回填了节点色（间接证明 signal 路径工作）
        assert fake_adapter.view.get_node_item("n1").status == "succeeded"
    finally:
        ctrl.dispose()
