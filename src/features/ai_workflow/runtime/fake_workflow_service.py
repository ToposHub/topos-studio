"""P9.1 FakeWorkflowService —— 合成运行（fake provider，plan §10.1）。

> 本服务**不是**真实 LLM / Comfy 推理。它按节点拓扑序逐个 sleep + 回调发
> ``started/succeeded`` 事件，作为 Run Drawer 节点状态色的**合成**数据源。
> 真实推理留 P9+ 真实环境接线（plan §10.1 fake-provider-first）。

职责（plan §10.7 P9）：

- 模仿 ``AssistantService.run_workflow`` 的接口（``run_workflow(wf_input)``），
  让 :class:`LegacyHarnessAdapter` 透明调用，无需改 adapter 的 workflow 路径。
- 按节点拓扑序：``emit_node_event("started", node_id)`` → ``time.sleep(duration)``
  → ``emit_node_event("succeeded", node_id)``；中途 cancel 则跳过剩余节点，
  并返回**不带 artifact** 的取消结果（fake provider 诚实红线）。
- 产一个 :class:`FakeWorkflowResult`，字段兼容
  ``LegacyHarnessAdapter._record_workflow_result`` 的 getattr 探测（
  ``diagnostics`` / ``degraded`` / ``artifact_id`` / ``variants`` /
  ``missing_evidence`` / ``degradation``）。

红线：

- **不引入** ``moderngl`` / ``composite.core``；**不写 timeline**。
- ``emit_node_event`` 是纯函数注入（签名 ``(kind: str, node_id: str) -> None``），
  本服务不依赖 Qt / runtime，便于纯 Python 单测。
- 节点 id 从 ``wf_input.node_ids`` 读（``WorkflowCompiler._build_inputs`` 写入
  ``workflow_node_ids``，``invocation_to_workflow_input`` 回填）；无节点信息时
  退化为单节点 ``"workflow"``（与 ``_record_workflow_result`` 的
  ``producer_node_id="workflow"`` 一致，聊天入口即此形态）。
"""
from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Tuple

_LOG = logging.getLogger(__name__)

#: emit_node_event 回调签名：(kind, node_id) -> None。kind 为
#: "started" / "succeeded" / "skipped" / "cancelled"。
NodeEventEmitter = Callable[[str, str], None]

#: cancel token：() -> bool。True 表示用户已请求取消。
CancelToken = Callable[[], bool]


# ---------------------------------------------------------------------- #
# 结果（兼容 LegacyHarnessAdapter._record_workflow_result 的 getattr 探测）
# ---------------------------------------------------------------------- #


@dataclass(frozen=True)
class FakeWorkflowResult:
    """合成运行结果。

    字段对齐 ``AssistantService.WorkflowResult`` 的 getattr 探测面（
    ``legacy_adapters.py:_record_workflow_result`` 用 ``getattr(result, ...,
    default)`` 读取），让 fake 服务无缝走 adapter 的 workflow 记录路径。
    """

    artifact_id: str = ""
    variants: Tuple[Any, ...] = ()
    diagnostics: Tuple[dict, ...] = ()
    degraded: bool = False
    degradation: str = ""
    missing_evidence: Tuple[str, ...] = ()

    def __post_init__(self) -> None:
        # 保证 artifact_id 是 str（_record_workflow_result 用 str(art_id)）
        if not isinstance(self.artifact_id, str):
            object.__setattr__(self, "artifact_id", str(self.artifact_id))


# ---------------------------------------------------------------------- #
# 合成执行服务
# ---------------------------------------------------------------------- #


class FakeWorkflowService:
    """**合成运行**（fake provider）。

    按 wf_input 的节点列表逐个：发 started → sleep(duration) → 发 succeeded。
    不调用任何真实模型 / 网络 / GPU；不写 timeline。

    Args:
        node_duration_s: 每个节点的合成运行时长（秒）。0 表示不 sleep（测试用）。
    """

    def __init__(self, *, node_duration_s: float = 0.3) -> None:
        if node_duration_s < 0:
            raise ValueError("node_duration_s must be >= 0")
        self._duration = float(node_duration_s)

    # ------------------------------------------------------------------ #
    # 公共 API（兼容 AssistantService.run_workflow 调用约定）
    # ------------------------------------------------------------------ #

    def run_workflow(
        self,
        wf_input: Any,
        *,
        emit_node_event: NodeEventEmitter | None = None,
        cancel_requested: CancelToken | None = None,
    ) -> FakeWorkflowResult:
        """按拓扑序逐节点合成运行。

        Args:
            wf_input: 携带节点列表的 workflow 输入。读取其 ``node_ids`` 属性
                （``Tuple[str, ...]``）；无此属性或空时退化为单节点 ``"workflow"``。
            emit_node_event: 节点事件回调。``None`` 表示不回发事件（兼容既有
                ``run_workflow`` 无此参数的调用）。
            cancel_requested: cancel token。``None`` 视为永不取消。

        Returns:
            :class:`FakeWorkflowResult`。
        """
        node_ids = self._derive_node_ids(wf_input)
        cancel = cancel_requested or (lambda: False)
        emit = emit_node_event  # may be None
        _LOG.debug("fake workflow run: nodes=%s", node_ids)

        cancelled = False
        for node_id in node_ids:
            # 进入每个节点前先查 cancel（协作 token，plan §10.2 P0.6）。
            if cancel():
                _LOG.debug("fake workflow cancelled before node %s", node_id)
                cancelled = True
                break
            if emit is not None:
                emit("started", node_id)
            if self._duration > 0:
                time.sleep(self._duration)
            # sleep 后再查一次 cancel：正在运行的节点不发 succeeded（半截状态）。
            if cancel():
                _LOG.debug("fake workflow cancelled during node %s", node_id)
                cancelled = True
                break
            if emit is not None:
                emit("succeeded", node_id)

        if cancelled:
            # AUD-23400（fake provider 诚实红线）：取消的运行**不产出 artifact**。
            # 旧实现无条件返回非空 ``fake-wf-<uuid>`` artifact_id，使取消的 run
            # 被记成"成功 + 附产物"。取消结果只带可观测的 CANCELLED 诊断。
            return FakeWorkflowResult(
                artifact_id="",
                variants=(),
                diagnostics=({"code": "CANCELLED",
                              "message": "fake workflow cancelled by user"},),
                degraded=False,
                degradation="",
                missing_evidence=(),
            )

        return FakeWorkflowResult(
            artifact_id=f"fake-wf-{uuid.uuid4().hex[:12]}",
            variants=(),
            diagnostics=(),
            degraded=False,
            degradation="",
            missing_evidence=(),
        )

    # ------------------------------------------------------------------ #
    # 内部
    # ------------------------------------------------------------------ #

    @staticmethod
    def _derive_node_ids(wf_input: Any) -> Tuple[str, ...]:
        """从 wf_input 读节点 id 列表；无则退化为 ``("workflow",)``。"""
        node_ids = getattr(wf_input, "node_ids", None)
        if node_ids:
            # 过滤空串并去重保序
            seen: set[str] = set()
            ordered: list[str] = []
            for nid in node_ids:
                nid = str(nid)
                if nid and nid not in seen:
                    seen.add(nid)
                    ordered.append(nid)
            if ordered:
                return tuple(ordered)
        return ("workflow",)


__all__ = [
    "FakeWorkflowResult",
    "FakeWorkflowService",
    "NodeEventEmitter",
    "CancelToken",
]
