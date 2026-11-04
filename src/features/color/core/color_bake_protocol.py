"""R06: 离线 AI/bake 显式异步结果协议（DTO + 取消/晚到守卫）。

协议（计划 R06.5）：

- **提交**：``ColorBakeBroker.submit`` 登记 ``ColorBakeRequest``（typed
  target + source/graph revision + 资源 spec）。执行体（ONNX 管道、
  离线渲染 worker）由宿主另行接入——本模块只固化**身份/取消/接受**契约，
  不承载计算。
- **取消语义**：``cancel_for_graph(stack_id, graph_revision)``——图 revision
  前进（编辑/撤销/删节点）即取消该栈全部更低 revision 的在途任务；
  ``cancel_for_source(source_content_revision)`` 同理覆盖源内容变更；
  显式 ``cancel(task_id)`` 供 UI 取消。
- **晚到守卫**：``accept_result(task_id, payload, verify)`` 只有在
  （任务仍在途）且（verify 回调确认目标仍有效——R04 的
  ``verify_async_token``：会话/代际/栈/节点/实例）时接受；否则丢弃并
  返回 None（不写新目标，不半发布）。
- **同一规格供预览与全分辨率输出**：请求携带 spec（资源 id + 参数 +
  版本）；结果经引用发布（payload 归调用方解释），预览/导出共用同一
  task 语义，不重复执行体假设。
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping
from uuid import uuid4


@dataclass(frozen=True)
class ColorBakeRequest:
    """一次离线 bake 的完整身份（目标 + 源 revision + 资源 spec）。"""

    task_id: str
    target: Mapping[str, Any]        # session/stack/node/instance 身份
    stack_id: str
    graph_revision: int
    source_content_revision: int
    resource_id: str
    params: Mapping[str, Any] = field(default_factory=dict)
    spec_version: str = ""
    # R06-B（2026-11-05）：执行闭环扩展——全部可选，既有构造/测试不破坏。
    backend_id: str = ""             # 执行后端（注册表键；空 = 拒绝）
    source_clip_id: str = ""         # 源 clip 绑定（过期/切 clip 诊断）
    frame_range: tuple[int, int] | None = None  # 帧范围（含端点）
    cancel_token: Any = None         # R04 async token（verify 回调闭包引用）

    @property
    def spec(self) -> dict[str, Any]:
        """同一规格（预览/全分辨率输出共用）。"""
        return {"resource_id": self.resource_id,
                "params": dict(self.params),
                "spec_version": self.spec_version}


def make_bake_request(*, target: Mapping[str, Any], stack_id: str,
                      graph_revision: int, source_content_revision: int,
                      resource_id: str,
                      params: Mapping[str, Any] | None = None,
                      spec_version: str = "",
                      backend_id: str = "",
                      source_clip_id: str = "",
                      frame_range: tuple[int, int] | None = None,
                      cancel_token: Any = None) -> ColorBakeRequest:
    return ColorBakeRequest(
        task_id=uuid4().hex, target=dict(target), stack_id=stack_id,
        graph_revision=int(graph_revision),
        source_content_revision=int(source_content_revision),
        resource_id=resource_id, params=dict(params or {}),
        spec_version=spec_version, backend_id=backend_id,
        source_clip_id=source_clip_id,
        frame_range=tuple(frame_range) if frame_range else None,
        cancel_token=cancel_token)


@dataclass(frozen=True)
class ColorBakeStatus:
    """UI 可见的一次 bake 任务状态（不含像素/路径数据）。"""

    task_id: str
    phase: str                       # queued/running/succeeded/failed/cancelled
    percent: float = 0.0
    error_code: str = ""
    backend_id: str = ""
    bake_uri: str = ""               # 接受后的结果引用（topos-res://）


@dataclass(frozen=True)
class ColorBakeCapability:
    """后端能力声明：不可用必须显式（fail-closed），不得静默。"""

    backend_id: str
    categories: tuple[str, ...]
    available: bool
    disabled_reason: str = ""        # not_implemented / model_missing / ...

    def to_dict(self) -> dict[str, Any]:
        return {"backend_id": self.backend_id,
                "categories": list(self.categories),
                "available": self.available,
                "disabled_reason": self.disabled_reason}


class ColorBakeBroker:
    """在途 bake 任务的登记/取消/接受（线程安全；无计算、无 Qt）。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._pending: dict[str, ColorBakeRequest] = {}
        self._cancelled: set[str] = set()
        self._accepted: dict[str, Any] = {}

    # ── 提交/查询 ──────────────────────────────────────────────────
    def submit(self, request: ColorBakeRequest) -> str:
        with self._lock:
            self._pending[request.task_id] = request
        return request.task_id

    def pending(self) -> list[ColorBakeRequest]:
        with self._lock:
            return list(self._pending.values())

    def request(self, task_id: str) -> ColorBakeRequest | None:
        with self._lock:
            return self._pending.get(task_id)

    # ── 取消 ───────────────────────────────────────────────────────
    def cancel(self, task_id: str) -> bool:
        with self._lock:
            if task_id in self._pending:
                del self._pending[task_id]
                self._cancelled.add(task_id)
                return True
            return False

    def cancel_for_graph(self, stack_id: str, graph_revision: int) -> list[str]:
        """图 revision 前进：取消该栈全部更低 revision 的在途任务。"""
        cancelled: list[str] = []
        with self._lock:
            for task_id in list(self._pending):
                request = self._pending[task_id]
                if request.stack_id == stack_id \
                        and request.graph_revision < graph_revision:
                    del self._pending[task_id]
                    self._cancelled.add(task_id)
                    cancelled.append(task_id)
        return cancelled

    def cancel_for_source(self, source_content_revision: int) -> list[str]:
        """源内容变更：取消全部更低源 revision 的在途任务。"""
        cancelled: list[str] = []
        with self._lock:
            for task_id in list(self._pending):
                request = self._pending[task_id]
                if request.source_content_revision < source_content_revision:
                    del self._pending[task_id]
                    self._cancelled.add(task_id)
                    cancelled.append(task_id)
        return cancelled

    # ── 接受（晚到守卫） ───────────────────────────────────────────
    def accept_result(self, task_id: str, payload: Any,
                      verify: Callable[[], bool] | None = None) -> Any | None:
        """接受结果：任务在途 + verify 通过才发布；否则丢弃返回 None。

        verify 由宿主提供（R04 ``verify_async_token``：会话/代际/栈/节点/
        实例仍匹配）。已取消/未知/已接受的任务一律丢弃（幂等防护）。
        """
        with self._lock:
            request = self._pending.get(task_id)
            if request is None:
                return None
            del self._pending[task_id]
            if verify is not None and not verify():
                self._cancelled.add(task_id)
                return None
            self._accepted[task_id] = payload
            return payload

    def was_cancelled(self, task_id: str) -> bool:
        with self._lock:
            return task_id in self._cancelled
