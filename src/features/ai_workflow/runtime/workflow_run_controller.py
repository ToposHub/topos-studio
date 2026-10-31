"""P9.3 WorkflowRunController —— Run 按钮的"大脑"（plan §10.7 P9）。

> 连 ``panel.run_requested`` → :class:`WorkflowCompiler.compile` → 起后台 worker
> （QThread）→ :meth:`LegacyHarnessAdapter.invoke_compiled`（注入 node 事件 emitter）
> → runtime 发 ``node.*`` 事件 → 订阅者经 Qt signal 切回主线程回填节点色 +
> :class:`RunDrawerState`。

线程模型（仿 ``ai_edit_controller.py:_PlanWorker``）：

- **主线程**：compile（纯 Python，快）+ 注册订阅者 + 接收 worker 的 finished
  signal 回填 final RunDrawerState。
- **worker 线程**：``invoke_compiled``（含 FakeWorkflowService.run_workflow 的
  sleep）。worker 只动 runtime（runtime 内部用 RLock 保护）；**不直接动 Qt 控件**。
- **订阅者**：runtime 在锁外调用 subscriber；subscriber 把事件塞进主线程的
  ``_node_event_arrived`` signal（Qt 自动跨线程派发到主线程），主线程槽里再调
  ``adapter.set_node_status`` / 聚合 RunDrawerState。

红线（plan §1.4.3 / §10.9 / §10.1）：

- **不引入** ``moderngl`` / ``composite.core``。
- **不写 timeline**：执行经 WorkflowCompiler → invoke_compiled → runtime；
  FakeWorkflowService 无 timeline 副作用。
- **runtime 单一执行边界**：用 :class:`LegacyHarnessAdapter`，不造第二 executor。
- **fake provider 诚实**：Run Drawer 条目标"合成运行"。
- **订阅者线程安全**：subscriber 不直接动 Qt（经 signal 切回）。
"""
from __future__ import annotations

import logging
import time
from typing import Any, Callable

from PyQt6.QtCore import QCoreApplication, QObject, QThread, pyqtSignal, pyqtSlot

from src.features.ai_harness.invocation_models import RunEventKind, RunStatus
from src.features.ai_harness.legacy_adapters import LegacyHarnessAdapter
from src.features.ai_workflow.runtime.workflow_run_bridge import WorkflowRunBridge

_LOG = logging.getLogger(__name__)

#: node.* 事件短名 → adapter.set_node_status 用的状态字符串（与 VisualTokens 对齐）。
_NODE_STATUS_MAP = {
    "queued": "queued",
    "started": "running",
    "progress": "running",
    "waiting_input": "waiting_input",
    "succeeded": "succeeded",
    "failed": "failed",
    "cancelled": "cancelled",
    "cached": "cached",
    "skipped": "skipped",
}


class _RunWorker(QThread):
    """后台线程跑 ``invoke_compiled``（含 FakeWorkflowService 的 sleep）。

    完成或失败后通过 Qt signal 自动切回主线程。worker 线程内只动 runtime，
    不直接访问 Qt 控件（节点色回填经订阅者 + signal 在主线程完成）。
    """

    finished_run = pyqtSignal(str)   # run_id
    failed = pyqtSignal(str, str)    # run_id, error_message

    def __init__(
        self,
        *,
        adapter: LegacyHarnessAdapter,
        compiled: Any,
        invocation: Any,
        runtime: Any,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._adapter = adapter
        self._compiled = compiled
        self._invocation = invocation
        self._runtime = runtime
        self._run_id = ""
        self._error = ""

    @property
    def run_id(self) -> str:
        return self._run_id

    def run(self) -> None:
        try:
            self._run_id = self._adapter.invoke_compiled(
                self._compiled, self._invocation,
            )
            self.finished_run.emit(self._run_id)
        except Exception as exc:  # noqa: BLE001
            _LOG.exception("workflow run worker failed")
            self._error = str(exc)
            self.failed.emit(self._run_id, self._error)


class WorkflowRunController(QObject):
    """连 panel 的 run/cancel 意图到 WorkflowCompiler + HarnessRuntime。

    通过注入式依赖（runtime / compiler / fake_service / adapter / panel）解耦，
    生产宿主与测试 fake 各自提供实例。
    """

    # 订阅者收到 node.* 事件时，经此 signal 切回主线程（Qt 自动跨线程派发）。
    _node_event_arrived = pyqtSignal(str, str, str)  # run_id, kind, node_id
    _run_finished = pyqtSignal(str, bool)            # run_id, success
    # worker 内 invoke_compiled→runtime.start 分配 run_id 后，经此 signal 报回主线程。
    _run_id_assigned = pyqtSignal(str)
    # 真实后端的阶段事件（payload {"stage": ...}）→ 主线程按节点类别映射状态。
    _stage_event_arrived = pyqtSignal(str)

    def __init__(
        self,
        *,
        runtime: Any,
        compiler: Any,
        adapter: Any,
        panel: Any,
        fake_service: Any = None,
        service: Any = None,
        service_provider: Callable[[], Any] | None = None,
        context_provider: Callable[[Any], Any] | None = None,
        action_service: Any = None,
        composite_render_provider: Any = None,
        exposed_input_resolver: Callable[[Any, Any], None] | None = None,
        skill_id: str = "topos.workflow.synthetic",
        project_id: str = "ai-workflow",
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._runtime = runtime
        self._compiler = compiler
        self._service = service
        self._service_provider = service_provider
        self._fake_service = fake_service
        self._context_provider = context_provider
        self._action_service = action_service
        # plan §9.5：composite 渲染 provider（worker 线程执行）+ exposed input
        # 解析器（主线程执行——读 composite_page/EditProject 涉及 Qt）。
        self._composite_render_provider = composite_render_provider
        self._exposed_input_resolver = exposed_input_resolver
        self._adapter = adapter
        self._panel = panel
        self._skill_id = skill_id
        self._project_id = project_id
        self._last_compilation: Any = None
        self._worker: _RunWorker | None = None
        self._legacy_adapter: LegacyHarnessAdapter | None = None
        self._bridge: WorkflowRunBridge | None = None
        self._current_run_id: str = ""
        self._active_node_status: dict[str, str] = {}
        # cancel 可能在 run_id 尚未回填时被调用（用户极快点 Cancel）；置 pending，
        # _on_created 拿到 run_id 后立即 cancel。
        self._cancel_pending: bool = False

        # 订阅 runtime 事件（主线程注册一次；subscriber 在 runtime 锁外被调）。
        self._unsubscribe = runtime.add_subscriber(self._on_run_event)
        # signal → 主线程槽
        self._node_event_arrived.connect(self._apply_node_status)
        self._run_finished.connect(self._on_run_finalized)
        self._stage_event_arrived.connect(self._apply_stage_event)

    # ------------------------------------------------------------------ #
    # 公共 API
    # ------------------------------------------------------------------ #

    def run(self) -> None:
        """触发运行：compile → （成功）起 worker。同步在主线程做 compile。"""
        result, service = self._compile_current()
        if result is None or not result.success:
            return
        if result.compiled is None or result.invocation is None:
            self._push_diagnostics_to_drawer(
                ({"code": "NO_COMPILED", "message": "compile 返回 success 但无 compiled"},)
            )
            return
        if service is None:
            self._push_diagnostics_to_drawer(({
                "code": "WORKFLOW_BACKEND_UNAVAILABLE",
                "message": "没有可用的 AssistantService；运行已安全停止",
            },))
            return
        # 主线程阶段填充 exposed inputs（plan §9.5 composite 图快照等）——
        # 解析涉及 Qt/工程对象，不能推迟到 worker 线程。
        if self._exposed_input_resolver is not None:
            try:
                self._exposed_input_resolver(
                    result.invocation, getattr(self._panel, "current_graph", None),
                )
            except Exception:  # noqa: BLE001 — 解析失败降级，不阻断 Run
                _LOG.warning("exposed input resolver failed", exc_info=True)
        self._start_worker(result.compiled, result.invocation)

    def preview_plan(self) -> bool:
        """Compile and display a non-mutating plan preview without starting a run."""
        if (
            self._action_service is not None
            and self._current_run_id
            and self._action_service.get_plan(self._current_run_id) is not None
        ):
            result = self.preview_current_plan()
            return bool(getattr(result, "ok", False))
        result, _service = self._compile_current()
        return bool(result is not None and result.success)

    def _compile_current(self) -> tuple[Any | None, Any]:
        """Build one live compilation shared by Preview and Run."""
        graph = getattr(self._panel, "current_graph", None)
        if graph is None:
            _LOG.warning("run requested but no current graph")
            return None, None
        service = self._resolve_service()
        compile_kwargs: dict[str, Any] = {
            "skill_id": str(getattr(self._panel, "current_skill_id", "") or self._skill_id),
            "project_id": self._project_id,
            "timeline_id": "",
            "selected_asset_ids": (),
            "permission": "propose",
        }
        if self._context_provider is not None:
            if service is None:
                self._push_diagnostics_to_drawer(({
                    "code": "WORKFLOW_BACKEND_UNAVAILABLE",
                    "message": "真实 AssistantService 尚未装配；未启用 fake provider",
                },))
                return None, service
            try:
                context = self._context_provider(service)
            except Exception as exc:  # context resolver returns typed errors
                code = getattr(exc, "code", "WORKFLOW_CONTEXT_INVALID")
                self._push_diagnostics_to_drawer(({
                    "code": code, "message": str(exc),
                },))
                if hasattr(self._panel, "set_context_snapshot"):
                    self._panel.set_context_snapshot(None, status=f"阻断: {code}")
                return None, service
            if hasattr(self._panel, "set_context_snapshot"):
                provider_label = str(
                    getattr(service, "active_profile_id", "")
                    or service.__class__.__name__
                )
                self._panel.set_context_snapshot(
                    context, provider_label=provider_label, status="已绑定"
                )
            compile_kwargs.update({
                "project_id": context.project_id,
                "timeline_id": context.timeline_id,
                "selected_asset_ids": context.selected_asset_ids,
                "brief_id": context.brief_id,
                "brief_revision": context.brief_revision,
                "base_revision": context.base_revision,
                "analysis_snapshot_hash": context.analysis_snapshot_hash,
                "profile_id": context.profile_id,
                "network_allowed": context.network_allowed,
                "available_capabilities": set(context.available_capabilities),
                "permission": context.permission,
            })
        # compile（纯 Python，主线程；快）
        result = self._compiler.compile(
            graph, **compile_kwargs,
        )
        self._last_compilation = result
        if hasattr(self._panel, "set_plan_preview"):
            self._panel.set_plan_preview(
                getattr(result, "plan_preview", {}),
                plan_hash=getattr(result, "plan_hash", ""),
                status="编译预览" if result.success else "编译阻断",
                can_approve=False,
            )
        if not result.success:
            # 编译失败：diagnostics 进 Run Drawer，不启动 run
            self._push_diagnostics_to_drawer(result.diagnostics)
        return result, service

    def cancel(self) -> None:
        """请求取消当前 run（协作 token；不 kill 线程）。

        若 run_id 尚未回填（用户在 run.created 前点 Cancel），或 ``_current_run_id``
        指向**已终态的旧 run**（第二次 run 起的窗口期），置 ``_cancel_pending``，
        ``_on_created`` 拿到新 run_id 后立即 cancel（AUD-23404：旧行为只看
        ``_current_run_id`` 真值——窗口期取消会被发往死 run 而静默丢失）。
        """
        if self._current_run_is_active():
            self._runtime.cancel(self._current_run_id, reason="user_requested")
            return
        self._cancel_pending = True
        # 竞态收口：主线程读 ``_current_run_id``（空）与置 pending 之间，worker
        # 线程的 run.created 订阅者可能已完成回填并消费过 pending（当时仍为
        # False）→ 取消丢失。置位后复查一次：若 run 已创建则直接取消
        # （runtime.cancel 幂等，重复调用无害），pending 保持给更晚的创建路径。
        if self._current_run_is_active():
            self._cancel_pending = False
            self._runtime.cancel(self._current_run_id, reason="user_requested")

    def _current_run_is_active(self) -> bool:
        """当前 run_id 是否指向仍在执行的 run（非终态且 runtime 可见）。"""
        if not self._current_run_id:
            return False
        record = self._runtime.get_run(self._current_run_id)
        if record is None:
            return False
        return str(getattr(record, "status", "")) not in RunStatus.TERMINAL_STATES

    @property
    def current_run_id(self) -> str:
        return self._current_run_id

    def preview_current_plan(self) -> Any:
        """Preview the EditPlan produced by the current run; never writes."""
        if self._action_service is None or not self._current_run_id:
            return None
        result = self._action_service.preview(self._current_run_id)
        if getattr(result, "ok", False):
            preview = self._action_service.get_preview(self._current_run_id)
            diff = getattr(getattr(preview, "diff", None), "to_dict", lambda: {})()
            if hasattr(self._panel, "set_plan_preview"):
                self._panel.set_plan_preview(
                    {"diff": diff, "diff_hash": result.diff_hash,
                     "plan_hash": result.plan_hash},
                    plan_hash=result.plan_hash, status="真实 Diff 已生成", can_approve=True,
                )
        if not getattr(result, "ok", False):
            self._push_diagnostics_to_drawer(({
                "code": getattr(result, "diagnostic_code", "PLAN_PREVIEW_FAILED"),
                "message": getattr(result, "message", "EditPlan 预览失败"),
            },))
        return result

    def approve_current_plan(self) -> Any:
        """Ask the host to show its approval dialog and issue a scoped grant."""
        if self._action_service is None or not self._current_run_id:
            return None
        result = self._action_service.request_approval(self._current_run_id)
        if getattr(result, "ok", False):
            if hasattr(self._panel, "set_approval_state"):
                self._panel.set_approval_state(result.grant_id, status="已批准")
        else:
            self._push_diagnostics_to_drawer(({
                "code": getattr(result, "diagnostic_code", "APPROVAL_REQUEST_FAILED"),
                "message": getattr(result, "message", "审批请求失败"),
            },))
        return result

    def apply_current_plan(self, grant_id: str) -> Any:
        """Apply only with a host-issued grant through the action service."""
        if self._action_service is None or not self._current_run_id:
            return None
        result = self._action_service.apply(self._current_run_id, grant_id)
        if not getattr(result, "ok", False):
            self._push_diagnostics_to_drawer(({
                "code": getattr(result, "diagnostic_code", "COMMAND_COMMIT_FAILED"),
                "message": getattr(result, "message", "EditPlan 应用失败"),
            },))
        return result

    def retry(self, run_id: str = "") -> bool:
        """Recompile current live context before retrying; never replay stale input."""
        target = run_id or self._current_run_id
        if target and target != self._current_run_id:
            return False
        record = self._runtime.get_run(target) if target else None
        if record is not None and record.status not in (RunStatus.FAILED, RunStatus.CANCELLED):
            return False
        self.run()
        return True

    def handoff(self, run_id: str = "") -> bool:
        """Request a real cooperative stop for manual takeover."""
        target = run_id or self._current_run_id
        handler = getattr(self._runtime, "handoff", None)
        handed_off = bool(target and callable(handler) and handler(target))
        if handed_off:
            self._refresh_run_drawer()
        return handed_off

    def rollback(self, run_id: str = "") -> Any:
        """Ask the action boundary to perform a host-verified rollback."""
        target = run_id or self._current_run_id
        if self._action_service is None or not target:
            return None
        result = self._action_service.rollback(target)
        if not getattr(result, "ok", False):
            self._push_diagnostics_to_drawer(({
                "code": getattr(result, "diagnostic_code", "ROLLBACK_FAILED"),
                "message": getattr(result, "message", "AI EditPlan 回滚失败"),
            },))
        else:
            self._refresh_run_drawer()
        return result

    def resume(self, run_id: str = "") -> bool:
        """Resume a runtime waiting state; execution remains cooperative."""
        target = run_id or self._current_run_id
        resumed = bool(target and self._runtime.resume(target))
        if resumed:
            self._refresh_run_drawer()
        return resumed

    def diagnostics(self, run_id: str = "") -> tuple[dict[str, Any], ...]:
        record = self._runtime.get_run(run_id or self._current_run_id)
        return tuple(record.diagnostics) if record is not None else ()

    def show_diagnostics(self, run_id: str = "") -> tuple[dict[str, Any], ...]:
        diagnostics = self.diagnostics(run_id)
        if hasattr(self._panel, "set_diagnostics"):
            self._panel.set_diagnostics(diagnostics)
        return diagnostics

    def show_artifact(self, artifact_ref: str = "") -> dict[str, Any] | None:
        """Show one redacted artifact reference and ask the host to locate it if supported."""
        artifact_id = str(artifact_ref or "")
        if ":artifact:" in artifact_id:
            artifact_id = artifact_id.split(":artifact:", 1)[1]
        for record in self._runtime.list_runs():
            for artifact in record.artifacts:
                if artifact.artifact_id != artifact_id:
                    continue
                details = {
                    "run_id": record.run_id,
                    "artifact_id": artifact.artifact_id,
                    "artifact_type": artifact.artifact_type,
                    "producer_node_id": artifact.producer_node_id,
                    "summary": artifact.summary,
                }
                if hasattr(self._panel, "set_artifact_details"):
                    self._panel.set_artifact_details(details)
                locator = getattr(self._bridge, "locate_artifact", None)
                if callable(locator):
                    try:
                        locator(artifact.artifact_id)
                    except Exception:  # noqa: BLE001 — optional host locator must not break UI
                        _LOG.debug("host artifact locator failed", exc_info=True)
                return details
        if hasattr(self._panel, "set_artifact_details"):
            self._panel.set_artifact_details({
                "artifact_id": artifact_id,
                "status": "unavailable",
                "message": "artifact 引用已失效或不属于当前 runtime",
            })
        return None

    def wait_for_run(self, timeout_s: float = 5.0) -> None:
        """测试用：阻塞等当前 worker 跑完，同时泵主线程 Qt 事件。

        worker 在后台线程发 node.* 事件 → 订阅者 emit ``_node_event_arrived`` signal
        （跨线程派发到主线程）；本方法在 ``worker.wait`` 的同时调
        ``processEvents`` 让主线程处理这些 signal，否则槽函数要等 wait 返回后才
        执行（测试会看到节点色未更新）。
        """
        if self._worker is None:
            return
        deadline_ns = time.monotonic_ns() + int(timeout_s * 1e9)
        while self._worker.isRunning():
            # 最多等 5ms 再检查，期间泵主线程事件
            self._worker.wait(5)
            try:
                # Only drain Qt Core signals emitted by the worker.  Pumping the
                # entire widget queue here can touch deferred-deleted dock
                # children while a test/app teardown is in progress.
                QCoreApplication.sendPostedEvents(self)
            except Exception:  # noqa: BLE001
                pass
            if time.monotonic_ns() > deadline_ns:
                break

    def dispose(self) -> None:
        """断开订阅 + panel 信号（工程切换/teardown 调用）。"""
        if self._unsubscribe is not None:
            try:
                self._unsubscribe()
            except Exception:  # noqa: BLE001
                pass
            self._unsubscribe = None
        if self._worker is not None and self._worker.isRunning():
            if not self._worker.wait(2000):
                # AUD-23407：有界等待超时后不能既不取消 run 也不理会仍在
                # 运行的线程——先请求协作取消（runtime.cancel 幂等），再
                # 给一次短等待让 worker 有机会响应；仍存活则以 warning
                # 留痕并保留 _worker 引用（绝不覆盖运行中的 QThread 引用，
                # 见 AUD-23405），由调用方/进程收尾。
                if self._current_run_is_active():
                    try:
                        self._runtime.cancel(
                            self._current_run_id, reason="dispose_timeout",
                        )
                    except Exception:  # noqa: BLE001 — cancel 必须不阻断 teardown
                        _LOG.warning(
                            "dispose: cooperative cancel failed",
                            exc_info=True,
                        )
                if not self._worker.wait(1000):
                    _LOG.warning(
                        "dispose: workflow run worker still running after "
                        "cancel (run_id=%r); leaving the thread intact",
                        self._current_run_id,
                    )

    # ------------------------------------------------------------------ #
    # 内部：启动 worker
    # ------------------------------------------------------------------ #

    def _resolve_service(self) -> Any:
        # 显式合成后端优先：dock 装配仅在 AI_WORKFLOW_FAKE_PROVIDER=1 时注入
        # fake_service，此时即使 Edit Assistant 已装配真实 AssistantService 也
        # 必须走 fake（否则开发开关形同虚设）。其余情况走真实 provider。
        if self._fake_service is not None:
            return self._fake_service
        if self._service_provider is not None:
            return self._service_provider()
        return self._service

    def _start_worker(self, compiled: Any, invocation: Any) -> None:
        # AUD-23405：在跑防护。覆盖仍运行中的旧 worker 引用会使 QThread 的
        # C++ 对象被析构（"QThread: Destroyed while thread is still running"
        # 崩溃）。生产 UI 靠 panel 禁用 Run 防重复，本防线覆盖绕过 panel 的
        # 程序化调用：先协作取消旧 run，再拒绝并发启动（不覆盖 worker 引用）。
        if self._worker is not None and self._worker.isRunning():
            _LOG.warning(
                "run requested while previous worker still running; "
                "cancelling previous run and refusing concurrent start",
            )
            if self._current_run_is_active():
                self._runtime.cancel(
                    self._current_run_id, reason="superseded_by_new_run",
                )
            self._push_diagnostics_to_drawer(({
                "code": "RUN_ALREADY_ACTIVE",
                "message": "上一个运行尚未结束；已请求协作取消，请稍后重试",
            },))
            return
        # AUD-23404：清空上一个 run 的 run_id。不清空时，新 run 的 run_id 回填
        # 前（worker 线程内 runtime.start 派发 run.created 才回填）用户点
        # Cancel 会把取消发往**已终态的旧 run**，新 run 的取消被静默吞掉。
        self._current_run_id = ""
        # 重置节点状态追踪 + cancel pending
        self._active_node_status = {}
        self._cancel_pending = False
        # 标记 panel 进入 running（禁用 Run、使能 Cancel）
        self._panel.set_running(True)
        # 装配 LegacyHarnessAdapter（workflow 路径）+ 注入 node emitter。
        service = self._resolve_service()
        if service is None:
            self._panel.set_running(False)
            self._push_diagnostics_to_drawer(({
                "code": "WORKFLOW_BACKEND_UNAVAILABLE",
                "message": "没有可用的 Workflow 后端；未执行任何动作",
            },))
            return
        self._legacy_adapter = LegacyHarnessAdapter(service, self._runtime)
        editorial_provider = getattr(service, "get_analysis_evidence", None)
        if callable(editorial_provider):
            self._legacy_adapter.set_editorial_provider(editorial_provider)
        if self._action_service is not None:
            self._legacy_adapter.set_plan_observer(self._action_service.register_plan)
        if self._composite_render_provider is not None:
            self._legacy_adapter.set_composite_render_provider(
                self._composite_render_provider
            )
        self._bridge = WorkflowRunBridge(self._runtime)

        # run_id 在 invoke_compiled 内由 runtime.start 分配，本控制器无法在调用前
        # 拿到。但 node 事件 emitter 在 run 期间被 FakeWorkflowService 调用，必须知道
        # run_id。解决：用一个可变容器存 run_id；run_id 通过 run.created 事件（订阅者
        # 收到）回填；emitter 闭包每次读容器的当前值。
        # 为避免 emitter 在 run_id 尚未回填时被调用，FakeWorkflowService 的 started
        # 事件总在 runtime.start 之后（_run_workflow_path_with_input 先 mark_running
        # 再 run_workflow），此时 run.created 已发，订阅者已回填容器。
        run_id_box: list[str] = [""]

        def _on_created(event: Any) -> None:
            if event.kind == RunEventKind.RUN_CREATED and not run_id_box[0]:
                run_id_box[0] = event.run_id
                self._current_run_id = event.run_id
                # 若用户已在 run_id 回填前点 Cancel，立即 cancel。
                if self._cancel_pending:
                    self._runtime.cancel(event.run_id, reason="user_requested")
                    self._cancel_pending = False

        unsub_created = self._runtime.add_subscriber(_on_created)
        bridge = self._bridge

        def _emit(kind: str, node_id: str) -> None:
            rid = run_id_box[0]
            if rid:
                bridge.make_emit_for_run(rid)(kind, node_id)
            else:
                _LOG.debug("node event before run_id assigned: %s/%s", kind, node_id)

        self._legacy_adapter.set_workflow_node_emitter(_emit)
        self._worker = _RunWorker(
            adapter=self._legacy_adapter,
            compiled=compiled,
            invocation=invocation,
            runtime=self._runtime,
        )
        self._worker.finished_run.connect(self._on_worker_finished)
        self._worker.failed.connect(self._on_worker_failed)
        # worker 跑完或失败时解绑一次性 run.created 捕获订阅
        def _cleanup_created(*_a: Any) -> None:
            if unsub_created is not None:
                try:
                    unsub_created()
                except Exception:  # noqa: BLE001
                    pass
        self._worker.finished_run.connect(_cleanup_created)
        self._worker.failed.connect(_cleanup_created)
        self._worker.start()

    # ------------------------------------------------------------------ #
    # 内部：订阅者回调（runtime 锁外，可能在 worker 线程）
    # ------------------------------------------------------------------ #

    def _on_run_event(self, event: Any) -> None:
        """runtime subscriber：把 node.* 事件经 signal 切回主线程。

        **不直接动 Qt 控件**（跨线程不安全）；只 emit signal，由主线程槽处理。
        run 级终态事件 → emit _run_finished。
        """
        kind = event.kind
        if kind in (
            RunEventKind.NODE_STARTED, RunEventKind.NODE_SUCCEEDED,
            RunEventKind.NODE_FAILED, RunEventKind.NODE_CANCELLED,
            RunEventKind.NODE_QUEUED, RunEventKind.NODE_SKIPPED,
            RunEventKind.NODE_CACHED, RunEventKind.NODE_WAITING_INPUT,
            RunEventKind.NODE_PROGRESS,
        ):
            # 只关心当前 run 的 node 事件
            if event.run_id != self._current_run_id:
                return
            # 真实后端阶段事件（node_id 为空 + payload 带 stage）单独走 stage 映射。
            if kind == RunEventKind.NODE_PROGRESS and not event.node_id:
                stage = str((event.payload or {}).get("stage", "") or "")
                if stage:
                    self._stage_event_arrived.emit(stage)
                return
            if event.node_id:
                self._node_event_arrived.emit(event.run_id, kind, event.node_id)
            return
        # run 级终态
        if kind == RunEventKind.RUN_STATE_CHANGED and event.run_id == self._current_run_id:
            status = event.payload.get("status", "")
            if status in RunStatus.TERMINAL_STATES:
                success = status in (
                    RunStatus.SUCCEEDED, RunStatus.SUCCEEDED_WITH_WARNINGS, RunStatus.SKIPPED,
                )
                self._run_finished.emit(event.run_id, success)

    # ------------------------------------------------------------------ #
    # 内部：主线程槽
    # ------------------------------------------------------------------ #

    @pyqtSlot(str, str, str)
    def _apply_node_status(self, run_id: str, kind: str, node_id: str) -> None:
        """主线程：把 node 事件映射成 adapter.set_node_status。"""
        # kind 形如 "node.started" → 取短名
        short = kind.split(".", 1)[1] if "." in kind else kind
        status = _NODE_STATUS_MAP.get(short)
        if status is None:
            return
        self._active_node_status[node_id] = status
        try:
            self._adapter.set_node_status(node_id, status)
        except Exception:  # noqa: BLE001
            _LOG.exception("set_node_status failed for %s", node_id)
        # 顺便刷新 Run Drawer（轻量：当前节点状态快照）
        self._refresh_run_drawer()

    @pyqtSlot(str)
    def _apply_stage_event(self, stage: str) -> None:
        """主线程：真实后端阶段事件 → 按节点 spec 类别的诚实状态映射。

        真实 WorkflowRunner 按阶段整体执行（分析节点消费既有证据、规划节点
        产 variant、审批/写入节点留在宿主边界），因此这里的每个状态都反映
        实际发生的事：分析=cached（证据已存在）、未执行=skipped、审批
        =waiting_input（等用户），不伪造逐节点进度。
        """
        graph = getattr(self._last_compilation, "graph", None)
        if graph is None:
            return
        updates = self._stage_node_updates(stage, graph.nodes)
        if not updates:
            return
        for node_id, status in updates.items():
            self._active_node_status[node_id] = status
            try:
                self._adapter.set_node_status(node_id, status)
            except Exception:  # noqa: BLE001
                _LOG.exception("set_node_status failed for %s", node_id)
        self._refresh_run_drawer()

    @staticmethod
    def _classify_node(spec_id: str) -> str:
        """节点 spec → 执行角色（input/analysis/planner/production/gate/other）。"""
        if spec_id.startswith("topos.media.") or spec_id == "topos.timeline.current":
            return "input"
        if spec_id.startswith("topos.analysis."):
            return "analysis"
        if spec_id.startswith("topos.agent."):
            return "planner"
        if spec_id.startswith(("topos.caption.", "topos.text.", "topos.tts.",
                               "topos.timeline.propose_")):
            return "production"
        if spec_id in ("topos.user.approval",
                       "topos.timeline.apply_approved_plan",
                       "topos.timeline.preview_diff"):
            return "gate"
        return "other"

    def _stage_node_updates(
        self, stage: str, nodes: Any,
    ) -> dict[str, str]:
        """把阶段名翻译为 {node_id: status}（只含该阶段实际决定状态的节点）。"""
        updates: dict[str, str] = {}
        roles: dict[str, str] = {
            n.node_id: self._classify_node(n.spec_id) for n in nodes
        }
        if stage == "workflow_started":
            for nid, role in roles.items():
                if role == "input":
                    updates[nid] = "running"
        elif stage == "evidence_ready":
            for nid, role in roles.items():
                if role == "input":
                    updates[nid] = "succeeded"
                elif role == "analysis":
                    # WorkflowRunner 消费既有分析证据，不重跑分析。
                    updates[nid] = "cached"
        elif stage == "evidence_missing":
            for nid, role in roles.items():
                if role == "analysis":
                    updates[nid] = "skipped"
                elif role == "planner":
                    updates[nid] = "skipped"
        elif stage == "planning_started":
            for nid, role in roles.items():
                if role in ("planner", "production"):
                    updates[nid] = "running"
        elif stage == "variants_ready":
            for nid, role in roles.items():
                if role == "planner":
                    updates[nid] = "succeeded"
                elif role == "gate":
                    # 方案已就绪；预览/审批/写入等待用户动作（宿主边界）。
                    updates[nid] = "waiting_input"
        elif stage == "artifact_saved":
            for nid, role in roles.items():
                if role in ("production", "planner"):
                    updates[nid] = "succeeded"
        elif stage == "degraded":
            for nid, role in roles.items():
                if role in ("planner", "production") and nid not in self._active_node_status:
                    updates[nid] = "skipped"
        return updates

    @pyqtSlot(str)
    def _on_worker_finished(self, run_id: str) -> None:
        """worker 跑完（主线程）：刷新 final RunDrawerState + 解锁 panel。"""
        self._refresh_run_drawer(final=True)
        self._panel.set_running(False)

    @pyqtSlot(str, str)
    def _on_worker_failed(self, run_id: str, error: str) -> None:
        _LOG.error("workflow run failed: %s", error)
        self._panel.set_running(False)

    @pyqtSlot(str, bool)
    def _on_run_finalized(self, run_id: str, success: bool) -> None:
        """run 终态（主线程）：刷新 Run Drawer final。"""
        self._refresh_run_drawer(final=True)

    # ------------------------------------------------------------------ #
    # 内部：Run Drawer 聚合
    # ------------------------------------------------------------------ #

    def _push_diagnostics_to_drawer(self, diagnostics: Any) -> None:
        """compile 失败：把 diagnostics 显示进 Run Drawer。"""
        try:
            from src.features.edit.ui.ai_assistant_composer import (
                RunDrawerItem, RunDrawerState,
            )
        except ImportError:
            return
        items = []
        for i, diag in enumerate(diagnostics or ()):
            code = diag.get("code", "UNKNOWN") if isinstance(diag, dict) else str(diag)
            msg = diag.get("message", "") if isinstance(diag, dict) else ""
            items.append(RunDrawerItem(
                item_id=f"diag-{i}",
                label=tr_safe("workflow.run.compile_failed", "编译失败"),
                status="failed",
                error_code=str(code),
                output_summary=str(msg)[:80],
                can_view_diagnostics=True,
            ))
        state = RunDrawerState(items=items, failed_count=len(items))
        self._panel.set_run_drawer(state)

    def _refresh_run_drawer(self, *, final: bool = False) -> None:
        """聚合当前 run + 节点状态 → RunDrawerState，灌进 panel。"""
        try:
            from src.features.edit.ui.ai_assistant_composer import (
                RunDrawerItem, RunDrawerState,
            )
        except ImportError:
            return
        if not self._current_run_id:
            return
        rec = self._runtime.get_run(self._current_run_id)
        run_status = rec.status if rec else "idle"
        # 状态映射到 RunDrawer status 词汇
        drawer_status = {
            RunStatus.RUNNING: "running",
            RunStatus.QUEUED: "queued",
            RunStatus.VALIDATING: "queued",
            RunStatus.WAITING_FOR_USER: "waiting_input",
            RunStatus.SUCCEEDED: "succeeded",
            RunStatus.SUCCEEDED_WITH_WARNINGS: "succeeded",
            RunStatus.FAILED: "failed",
            RunStatus.CANCELLED: "cancelled",
            RunStatus.SKIPPED: "skipped",
        }.get(run_status, "running")
        scope = self._runtime.get_invocation(self._current_run_id) if rec else None
        scope_obj = getattr(scope, "scope", None)
        asset_ids = tuple(getattr(scope_obj, "selected_asset_ids", ()) or ())
        label = "合成运行" if self._is_fake_backend() else "真实 Assistant Workflow"
        plan_hash = getattr(self._last_compilation, "plan_hash", "")
        plan_summary = (
            f"Plan {plan_hash[:12]} · {len(asset_ids)} 个素材"
            if plan_hash else ""
        )
        item = RunDrawerItem(
            item_id=self._current_run_id,
            label=tr_safe("workflow.run.synthetic", label),
            status=drawer_status,
            progress=rec.progress if rec else 0.0,
            can_cancel=(drawer_status in ("running", "queued", "waiting_input")),
            can_retry=(drawer_status in ("failed", "cancelled")),
            can_view_diagnostics=bool(rec and rec.diagnostics),
            error_code=(rec.error_code if rec else ""),
            scope_summary=plan_summary,
            output_summary=(
                f"{len(rec.artifacts)} 个 artifact · {rec.cost:.4f} 成本"
                if rec else ""
            ),
            can_resume=(drawer_status == "waiting_input"),
            can_handoff=(drawer_status in ("running", "queued", "waiting_input")),
            can_rollback=bool(
                self._action_service is not None
                and getattr(self._action_service, "can_rollback", lambda _rid: False)(
                    self._current_run_id
                )
            ),
            kind="run",
            depth=0,
            checkpoint_summary=self._checkpoint_summary(rec),
            recovery_summary=self._recovery_summary(drawer_status, rec),
        )
        items = [item]
        node_labels = {
            getattr(node, "node_id", ""): getattr(node, "spec_id", "node")
            for node in getattr(getattr(self._last_compilation, "graph", None), "nodes", ())
        }
        for node_id, status in self._active_node_status.items():
            items.append(RunDrawerItem(
                item_id=f"{self._current_run_id}:node:{node_id}",
                parent_id=self._current_run_id,
                label=node_labels.get(node_id, node_id),
                status=status,
                kind="node",
                depth=1,
                output_summary=f"node_id={node_id}",
            ))
        for job in (getattr(rec, "jobs", ()) if rec else ()):
            items.append(RunDrawerItem(
                item_id=f"{self._current_run_id}:job:{job.job_id}",
                parent_id=self._current_run_id,
                label=job.label or job.kind or job.job_id,
                status=self._drawer_status(str(job.status)),
                progress=float(getattr(job, "progress", 0.0) or 0.0),
                kind="job",
                depth=1,
                output_summary=f"job_id={job.job_id}",
            ))
        for artifact in (getattr(rec, "artifacts", ()) if rec else ()):
            items.append(RunDrawerItem(
                item_id=f"{self._current_run_id}:artifact:{artifact.artifact_id}",
                parent_id=self._current_run_id,
                label=artifact.artifact_type or "artifact",
                status="succeeded",
                kind="artifact",
                depth=1,
                output_summary=artifact.summary,
            ))
        active = 1 if drawer_status in ("running", "queued", "waiting_input") else 0
        done = 1 if drawer_status in ("succeeded", "skipped") else 0
        failed = 1 if drawer_status == "failed" else 0
        history = self._history_items(self._current_run_id)
        state = RunDrawerState(
            items=items, active_count=active, completed_count=done, failed_count=failed,
            history=history,
        )
        self._refresh_release_gate(rec)
        self._refresh_metrics()
        self._panel.set_run_drawer(state)

    def _refresh_metrics(self) -> None:
        if not hasattr(self._panel, "set_metrics"):
            return
        getter = getattr(self._runtime, "get_metrics", None)
        if not callable(getter):
            return
        try:
            self._panel.set_metrics(getter())
        except Exception:  # noqa: BLE001 — metrics must never affect run UI
            _LOG.debug("runtime metrics refresh failed", exc_info=True)

    def _refresh_release_gate(self, record: Any | None) -> None:
        if record is None or not hasattr(self._panel, "set_release_gate"):
            return
        status = str(getattr(record, "status", "") or "")
        terminal_success = status in (
            RunStatus.SUCCEEDED, RunStatus.SUCCEEDED_WITH_WARNINGS, RunStatus.SKIPPED,
        )
        diagnostics = tuple(getattr(record, "diagnostics", ()) or ())
        checks = [
            {
                "id": "workflow_execution",
                "label": "Workflow 执行",
                "status": "passed" if terminal_success else (
                    "failed" if status == RunStatus.FAILED else "pending"
                ),
                "detail": status,
            },
            {
                "id": "diagnostics",
                "label": "诊断",
                "status": "failed" if diagnostics else (
                    "passed" if terminal_success else "pending"
                ),
                "detail": f"{len(diagnostics)} 条诊断",
            },
            {
                "id": "artifacts",
                "label": "Artifact 输出",
                "status": "passed" if record.artifacts else "pending",
                "detail": f"{len(record.artifacts)} 个 artifact",
            },
            {
                "id": "host_delivery_gate",
                "label": "宿主交付质量门",
                "status": "not_run",
                "detail": "未执行导出/ffprobe/平台规格验证，不能宣称可交付",
            },
        ]
        self._panel.set_release_gate(checks)

    @staticmethod
    def _drawer_status(status: str) -> str:
        return {
            "validating": "queued", "cancelling": "running",
            "succeeded_with_warnings": "succeeded",
        }.get(status, status if status in {
            "idle", "queued", "running", "cached", "waiting_input",
            "succeeded", "failed", "cancelled", "skipped",
        } else "idle")

    @staticmethod
    def _checkpoint_summary(record: Any) -> str:
        events = tuple(getattr(record, "events", ()) or ())
        kinds = {str(getattr(event, "kind", "")) for event in events}
        if "approval.required" in kinds:
            return "等待审批"
        if "run.resumed" in kinds:
            return "已从等待点恢复"
        if getattr(record, "is_terminal", False):
            return "终态快照"
        return "运行状态已记录"

    @staticmethod
    def _recovery_summary(status: str, record: Any | None = None) -> str:
        if str(getattr(record, "cancel_reason", "") or "") == "manual_handoff":
            return "已请求人工接管，等待 worker 收敛"
        return {
            "failed": "可重试 / 可查看诊断",
            "cancelled": "可重新规划后重试",
            "waiting_input": "等待用户确认后恢复",
        }.get(status, "")

    def _history_items(self, current_run_id: str) -> list[Any]:
        from src.features.edit.ui.ai_assistant_composer import RunDrawerItem

        items = []
        for record in reversed(self._runtime.list_runs()):
            if record.run_id == current_run_id:
                continue
            status = self._drawer_status(record.status)
            items.append(RunDrawerItem(
                item_id=record.run_id,
                label=record.skill_id or record.run_id[:12],
                status=status,
                progress=float(getattr(record, "progress", 0.0) or 0.0),
                scope_summary=f"{record.run_id[:12]} · {record.source}",
                output_summary=f"{len(record.artifacts)} 个 artifact · {record.cost:.4f} 成本",
                can_retry=(status in ("failed", "cancelled")),
                can_view_diagnostics=bool(record.diagnostics),
                can_resume=(status == "waiting_input"),
                kind="history",
                checkpoint_summary=self._checkpoint_summary(record),
                recovery_summary=self._recovery_summary(status, record),
            ))
            if len(items) >= 8:
                break
        return items

    def _is_fake_backend(self) -> bool:
        service = self._resolve_service()
        return service is not None and service.__class__.__name__ == "FakeWorkflowService"


def tr_safe(key: str, default: str) -> str:
    """i18n tr 的安全包装（controller 可能在 i18n 未就绪时被测试调用）。"""
    try:
        from src.shared.i18n.i18n import tr
        return tr(key, default)
    except Exception:  # noqa: BLE001
        return default


__all__ = ["WorkflowRunController"]
