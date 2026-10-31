"""P9.2 WorkflowRunBridge —— FakeWorkflowService 回调 → runtime node 事件。

> :class:`FakeWorkflowService` 用纯函数回调 ``emit_node_event(kind, node_id)``
> 发合成节点事件。:class:`WorkflowRunBridge` 把这个回调接进
> :class:`HarnessRuntime` 的事件流：闭包绑定 ``run_id``，内部调
> :meth:`HarnessRuntime.emit_node_event`。

职责（plan §10.7 P9）：

- 桥接 fake 服务的回调式 node 事件与 runtime 的订阅式事件流。
- 让 controller 能在 worker 线程内产 node 事件，主线程订阅者据此刷新节点色。

红线：

- 纯 Python，不依赖 Qt（闭包可跨线程调用；runtime 内部用 RLock 保护）。
- 不写 timeline；不引入 ``moderngl`` / ``composite.core``。
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from src.features.ai_harness.runtime import HarnessRuntime


class WorkflowRunBridge:
    """把 fake 服务的 node 事件回调接进 :class:`HarnessRuntime`。

    用法（worker 线程内）::

        bridge = WorkflowRunBridge(runtime)
        emit = bridge.make_emit_for_run(run_id)
        fake_service.run_workflow(wf_input, emit_node_event=emit, ...)
    """

    def __init__(self, runtime: "HarnessRuntime") -> None:
        self._runtime = runtime

    def make_emit_for_run(
        self, run_id: str,
    ) -> Callable[..., None]:
        """返回闭包 ``(kind, node_id, payload=None) -> None``。

        闭包内部调 :meth:`HarnessRuntime.emit_node_event`。run 不存在或已
        detached 时静默 no-op（runtime 自身保证）。

        ``payload``（可选 dict）供真实后端传阶段信息（如
        ``{"stage": "planning_started"}``）；FakeWorkflowService 的两参调用
        不受影响。
        """
        runtime = self._runtime

        def emit(kind: str, node_id: str, payload: dict | None = None) -> None:
            runtime.emit_node_event(run_id, kind, node_id, payload=payload)

        return emit


__all__ = ["WorkflowRunBridge"]
