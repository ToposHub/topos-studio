"""P9.6 集成验收：Run Drawer + WorkflowCompiler 接线（plan §10.7 P9）。

验收项：

1. **端到端 compile 失败**：load auto_caption 官方模板 → Run → compile 因
   MISSING_CAPABILITY 失败 → diagnostics 进 Run Drawer，无 run 启动，节点色不变。
   （真实模板在无 capabilities 时自然编译失败；这是无需真实环境的诚实验收路径。）
2. **红线扫描**：所有 runtime/*.py + ui/*.py 源码不含 moderngl / composite.core /
   TextureResult / timeline_write / CommandStack / commit_macro。
3. **runtime 单一执行边界**：grep ``class.*Executor`` 在 ai_workflow runtime/ui
   不命中（controller 用 LegacyHarnessAdapter，不造第二 executor）。
4. **fake provider 诚实**：FakeWorkflowService docstring 标'合成'；Run Drawer 条目
   label 含'合成'。
5. **节点状态映射**：node.* 事件 → adapter.set_node_status 的 9 种状态全覆盖。
6. **订阅者线程安全**：controller 源码 _on_run_event 不直接调 set_node_status
   （经 signal 切回）。
7. **Run 按钮使能**：随 graph 状态切换（load → enable，clear → disable）。
"""
from __future__ import annotations

import os
import re

import pytest


# ---------------------------------------------------------------------- #
# 辅助
# ---------------------------------------------------------------------- #


_AI_WORKFLOW_DIR = os.path.normpath(os.path.join(
    os.path.dirname(__file__), "..", "..",
    "src", "features", "ai_workflow",
))


def _read_dir_sources(dir_rel: str) -> str:
    """聚合 ai_workflow/<dir_rel>/*.py 源码文本。"""
    path = os.path.join(_AI_WORKFLOW_DIR, dir_rel)
    chunks = []
    if not os.path.isdir(path):
        return ""
    for name in sorted(os.listdir(path)):
        if name.endswith(".py"):
            with open(os.path.join(path, name), "r", encoding="utf-8") as f:
                chunks.append(f.read())
    return "\n".join(chunks)


# ---------------------------------------------------------------------- #
# 验收 2：红线扫描（runtime + ui 全部源码）
# ---------------------------------------------------------------------- #


def test_redline_no_moderngl_in_runtime_or_ui() -> None:
    """红线：runtime + ui 源码不含 'import moderngl'。"""
    for sub in ("runtime", "ui"):
        source = _read_dir_sources(sub)
        assert "import moderngl" not in source, (
            f"{sub}/ 含 'import moderngl'（红线违反）"
        )


def test_redline_no_composite_core_in_runtime_or_ui() -> None:
    """红线：runtime + ui 源码不含 'from src.features.composite.core'。"""
    for sub in ("runtime", "ui"):
        source = _read_dir_sources(sub)
        assert "from src.features.composite.core" not in source, (
            f"{sub}/ 含 composite.core 引用（红线违反）"
        )


def test_redline_no_gpu_texture_in_runtime_or_ui() -> None:
    """红线：runtime + ui 源码不含 'TextureResult'。"""
    for sub in ("runtime", "ui"):
        source = _read_dir_sources(sub)
        assert "TextureResult" not in source, (
            f"{sub}/ 含 TextureResult（不传 GPU texture）"
        )


def test_redline_no_timeline_write_in_runtime_or_ui() -> None:
    """红线：runtime + ui 源码不直接写 timeline。"""
    forbidden = ("timeline_write", "CommandStack", "commit_macro", "apply_edit_plan")
    for sub in ("runtime", "ui"):
        source = _read_dir_sources(sub)
        for token in forbidden:
            assert token not in source, (
                f"{sub}/ 含 {token}（不直接写 timeline，plan §1.4.3）"
            )


# ---------------------------------------------------------------------- #
# 验收 3：runtime 单一执行边界（无第二 executor）
# ---------------------------------------------------------------------- #


def test_no_new_executor_class_in_ai_workflow() -> None:
    """controller 用 LegacyHarnessAdapter，runtime/ui 不定义新 Executor 类。"""
    pattern = re.compile(r"^\s*class\s+\w*Executor\b", re.MULTILINE)
    for sub in ("runtime", "ui"):
        source = _read_dir_sources(sub)
        matches = pattern.findall(source)
        assert not matches, (
            f"{sub}/ 定义了 Executor 类（违反单一执行边界，plan §10.9）：{matches}"
        )


# ---------------------------------------------------------------------- #
# 验收 4：fake provider 诚实
# ---------------------------------------------------------------------- #


def test_fake_service_docstring_marks_synthetic() -> None:
    """FakeWorkflowService docstring 标'合成'。"""
    from src.features.ai_workflow.runtime.fake_workflow_service import (
        FakeWorkflowService,
    )
    assert "合成" in (FakeWorkflowService.__doc__ or "")


def test_run_drawer_label_marks_synthetic() -> None:
    """controller 的 Run Drawer 条目 label 含'合成'。"""
    ctrl_path = os.path.join(_AI_WORKFLOW_DIR, "runtime", "workflow_run_controller.py")
    with open(ctrl_path, "r", encoding="utf-8") as f:
        source = f.read()
    # _refresh_run_drawer 里 label 用 tr_safe("workflow.run.synthetic", "合成运行")
    assert "合成运行" in source, "Run Drawer 条目 label 应含'合成运行'（诚实标注）"


# ---------------------------------------------------------------------- #
# 验收 5：节点状态映射全覆盖
# ---------------------------------------------------------------------- #


def test_node_status_map_covers_all_node_events() -> None:
    """_NODE_STATUS_MAP 覆盖 RunEventKind.NODE_* 全部 9 种。"""
    from src.features.ai_harness.invocation_models import RunEventKind
    from src.features.ai_workflow.runtime.workflow_run_controller import (
        _NODE_STATUS_MAP,
    )
    node_kinds = {
        RunEventKind.NODE_QUEUED, RunEventKind.NODE_STARTED,
        RunEventKind.NODE_PROGRESS, RunEventKind.NODE_WAITING_INPUT,
        RunEventKind.NODE_SUCCEEDED, RunEventKind.NODE_FAILED,
        RunEventKind.NODE_CANCELLED, RunEventKind.NODE_CACHED,
        RunEventKind.NODE_SKIPPED,
    }
    # _NODE_STATUS_MAP 的 key 是短名（queued/started/...），与 NODE_* 去前缀对齐
    expected_short = {k.split(".", 1)[1] for k in node_kinds}
    assert set(_NODE_STATUS_MAP.keys()) == expected_short, (
        f"_NODE_STATUS_MAP 应覆盖全部 node 事件短名；缺："
        f"{expected_short - set(_NODE_STATUS_MAP.keys())}"
    )


# ---------------------------------------------------------------------- #
# 验收 6：订阅者线程安全（controller _on_run_event 不直接动 Qt）
# ---------------------------------------------------------------------- #


def test_subscriber_does_not_directly_touch_qt() -> None:
    """_on_run_event 只 emit signal，不直接调 adapter.set_node_status。"""
    ctrl_path = os.path.join(
        _AI_WORKFLOW_DIR, "runtime", "workflow_run_controller.py",
    )
    with open(ctrl_path, "r", encoding="utf-8") as f:
        source = f.read()
    # 提取 _on_run_event 方法体
    match = re.search(
        r"def _on_run_event\(self.*?\n((?:    .*?\n)+?)(?=    def |\nclass |\Z)",
        source, re.DOTALL,
    )
    assert match, "_on_run_event 方法未找到"
    body = match.group(1)
    # body 内不应直接调 set_node_status（那是主线程槽 _apply_node_status 的职责）
    assert "set_node_status" not in body, (
        "_on_run_event 不应直接调 adapter.set_node_status（跨线程不安全）；"
        "应经 _node_event_arrived signal 切回主线程 _apply_node_status"
    )
    # body 内应 emit signal
    assert "_node_event_arrived.emit" in body or "_run_finished.emit" in body, (
        "_on_run_event 应 emit signal 切回主线程"
    )


# ---------------------------------------------------------------------- #
# 验收 1：端到端 compile 失败路径（真实 auto_caption 模板）
# ---------------------------------------------------------------------- #


@pytest.fixture
def wired_stack(qapp):
    """装配完整执行链（同 test_ai_workflow_dock_wiring.py）。"""
    from src.features.ai_workflow.core.node_specs import (
        NodeSpecRegistry, register_official_specs,
    )
    from src.features.ai_workflow.core.template_registry import (
        WorkflowTemplateRegistry, register_official_templates,
    )
    from src.features.ai_workflow.core.workflow_compiler import WorkflowCompiler
    from src.features.ai_harness.runtime import HarnessRuntime
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
    panel.run_requested.connect(controller.run)
    panel.cancel_requested.connect(controller.cancel)
    yield {"panel": panel, "runtime": runtime, "controller": controller}
    controller.dispose()
    panel.deleteLater()


def test_e2e_compile_failure_diagnostics_to_drawer(wired_stack) -> None:
    """auto_caption 模板（无 capabilities）→ compile 失败 → diagnostics 进 drawer。"""
    panel = wired_stack["panel"]
    runtime = wired_stack["runtime"]
    controller = wired_stack["controller"]
    panel.load_template("wf-auto-caption")
    assert panel.current_graph is not None
    # Run 按钮 enable
    assert panel.run_button.isEnabled()
    # 触发 run
    controller.run()
    # compile 失败：无 run 启动
    assert runtime.list_runs() == [], "compile 失败不应启动 run"
    # diagnostics 进 drawer
    drawer_state = panel.run_drawer.state
    assert len(drawer_state.items) >= 1
    assert drawer_state.items[0].status == "failed"
    assert drawer_state.items[0].error_code  # 非空 error code


def test_e2e_compile_failure_node_status_unchanged(wired_stack) -> None:
    """compile 失败 → 节点色不变（保持 idle）。"""
    panel = wired_stack["panel"]
    controller = wired_stack["controller"]
    panel.load_template("wf-auto-caption")
    # 记录节点初始状态
    node_ids = list(panel.adapter.view._node_items.keys())
    initial = {nid: panel.adapter.view.get_node_item(nid).status for nid in node_ids}
    controller.run()  # compile 失败
    # 节点色不变
    for nid in node_ids:
        assert panel.adapter.view.get_node_item(nid).status == initial[nid]


# ---------------------------------------------------------------------- #
# 验收 7：Run 按钮使能（随 graph 状态）
# ---------------------------------------------------------------------- #


def test_run_button_enables_on_load_disables_on_clear(wired_stack) -> None:
    panel = wired_stack["panel"]
    # 初始无 graph → 禁用
    assert not panel.run_button.isEnabled()
    panel.load_template("wf-auto-caption")
    assert panel.run_button.isEnabled()
    panel.clear_canvas()
    assert not panel.run_button.isEnabled()


# ---------------------------------------------------------------------- #
# 验收 8：成功路径（compile stub → worker → 节点色 succeeded）
# ---------------------------------------------------------------------- #


def test_e2e_success_path_node_status_succeeds(qapp) -> None:
    """成功路径：stub compile 成功 → worker 跑 → 节点色 succeeded。

    用真实 adapter（含真实 WorkflowNodeItem）+ stub compiler（绕过真实模板的
    capability 缺失），验证 node.* 事件 → adapter.set_node_status → 节点色。
    """
    from src.features.ai_harness.invocation_models import (
        Invocation, InvocationScope, InvocationSource, RunPolicy,
    )
    from src.features.ai_harness.runtime import HarnessRuntime
    from src.features.ai_workflow.core.node_specs import (
        NodeSpecRegistry, register_official_specs,
    )
    from src.features.ai_workflow.core.template_registry import (
        WorkflowTemplateRegistry, register_official_templates,
    )
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
    panel.load_template("wf-auto-caption")
    # 拿到真实节点 id（input/asr/caption）
    node_ids = list(panel.adapter.view._node_items.keys())
    assert len(node_ids) >= 1

    # stub compiler：返回成功 + FakeWorkflowService 友好的 wf_input（带 node_ids）
    class _FakeCompiled:
        runnable = True
        target = "workflow"
        workflow_input = type("_W", (), {"node_ids": tuple(node_ids)})()
        skill_input = None
        diagnostics = ()

    class _StubCompiler:
        def compile(self, graph, **kwargs):
            return type("_R", (), {
                "success": True, "graph": graph,
                "invocation": Invocation(
                    invocation_id="inv-e2e", source=InvocationSource.WORKFLOW,
                    skill_id="test", scope=InvocationScope(
                        project_id="p1", timeline_id="", selected_asset_ids=(),
                    ), policy=RunPolicy(), inputs={}, user_text="",
                    created_at=0.0,
                ),
                "compiled": _FakeCompiled(),
                "diagnostics": (), "compiled_at": 1.0,
            })()

    runtime = HarnessRuntime()
    controller = WorkflowRunController(
        runtime=runtime, compiler=_StubCompiler(),
        fake_service=FakeWorkflowService(node_duration_s=0.0),
        adapter=panel.adapter, panel=panel,
    )
    try:
        controller.run()
        controller.wait_for_run(timeout_s=3.0)
        # 所有节点应 succeeded（node.* 事件 → set_node_status → 节点色）
        for nid in node_ids:
            item = panel.adapter.view.get_node_item(nid)
            assert item is not None
            assert item.status == "succeeded", (
                f"节点 {nid} 应 succeeded；实际：{item.status}"
            )
    finally:
        controller.dispose()
        panel.deleteLater()
