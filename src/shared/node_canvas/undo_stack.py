"""P10.1a UndoStack —— 画布编辑的精简撤销栈（plan §2.1 shared/node_canvas）。

> 画布编辑（节点拖拽/连线/参数）需要 undo/redo。Composite 的 ``undo_stack.py``
> （1168 行，含 branch/coalesce/persist）太重，且红线（plan §2.2 line256）禁止
> ``ai_workflow`` 引 ``composite.core``。本模块在 ``shared/node_canvas/`` 建精简
> 线性 UndoStack：纯 ``QObject``，无 GPU / 无 composite 依赖，在 ai_workflow 的
> 合法 import 路径内。

设计（plan §10.x P10）：

- **EditAction**：一次可撤销编辑，持 ``description`` + 两个闭包
  ``undo_fn`` / ``redo_fn``。闭包由调用方（host page/panel）提供，负责同步
  **视觉 + 模型**（例如 move_node：undo_fn 把节点 pos 设回 old + graph 回旧）。
- **UndoStack**：线性双栈（``_undo_list`` / ``_redo_list``）。
  - ``push(action)``：append undo_list，**清空 redo_list**（丢弃 redo 分支，与多数
    编辑器一致）。
  - ``undo()``：pop undo_list 末尾 → 调 ``undo_fn`` → push 进 redo_list。
  - ``redo()``：pop redo_list 末尾 → 调 ``redo_fn`` → push 进 undo_list。
  - ``changed`` signal：push/undo/redo/clear 后 emit，供 host 刷新按钮态。
  - 容错（AUD-36302）：``undo_fn`` / ``redo_fn`` 抛错只记日志、不向调用方
    传播，且 **action 留在原栈**（不转移）——闭包失败意味着模型可能未
    同步，若 action 仍转移到对侧栈，会出现"栈说已撤销、模型没回滚"的
    栈/模型状态失配；留在原栈保持动作可重试。

红线（plan §1.4.1 / §2.2）：

- 纯 ``QObject``，无 GPU/OpenGL 依赖；不引入任何 GPU 库或 composite 内部模块
  （源码扫描断言）。
- 无 OpenGL context / GPU 资源。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Callable, List

from PyQt6.QtCore import QObject, pyqtSignal

_LOG = logging.getLogger(__name__)


@dataclass
class EditAction:
    """一次可撤销编辑（闭包式；调用方负责同步视觉+模型）。

    Attributes:
        description: 人读描述（如 ``"move nodes"`` / ``"connect"``）；用于调试/
            未来 UI 的 undo 提示。可为空。
        undo_fn: 撤销闭包 ``() -> None``。
        redo_fn: 重做闭包 ``() -> None``。
    """

    description: str = ""
    undo_fn: Callable[[], None] = field(default=lambda: None)
    redo_fn: Callable[[], None] = field(default=lambda: None)


class UndoStack(QObject):
    """精简线性撤销栈（plan §10 P10）。

    双栈：``_undo_list``（可 undo 的动作，末尾为最近）+ ``_redo_list``（已 undo、
    可 redo 的动作）。``push`` 清空 redo 分支。

    信号：
        changed：push/undo/redo/clear 后 emit（无 payload；host 据此刷新按钮态）。
    """

    changed = pyqtSignal()

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._undo_list: List[EditAction] = []
        self._redo_list: List[EditAction] = []

    # ------------------------------------------------------------------ #
    # 查询
    # ------------------------------------------------------------------ #

    def can_undo(self) -> bool:
        return bool(self._undo_list)

    def can_redo(self) -> bool:
        return bool(self._redo_list)

    def depth(self) -> int:
        """undo 栈深度（测试/调试用）。"""
        return len(self._undo_list)

    # ------------------------------------------------------------------ #
    # 操作
    # ------------------------------------------------------------------ #

    def push(self, action: EditAction) -> None:
        """压入一个动作；清空 redo 分支。"""
        self._undo_list.append(action)
        self._redo_list.clear()
        self.changed.emit()

    def undo(self) -> None:
        """撤销最近动作；空栈 no-op。

        AUD-36302：``undo_fn`` 抛错被吞（只记日志），但 action **留在
        undo 栈**不再转移到 redo 栈——闭包失败时模型状态未知，转移会
        造成栈与模型失配；留在原栈让用户可以重试撤销。
        """
        if not self._undo_list:
            return
        action = self._undo_list.pop()
        try:
            action.undo_fn()
        except Exception:  # noqa: BLE001 — 闭包由调用方提供，失败不向 UI 传播
            _LOG.exception("undo_fn raised; keeping action on undo stack (action=%r)", action.description)
            self._undo_list.append(action)
            self.changed.emit()
            return
        self._redo_list.append(action)
        self.changed.emit()

    def redo(self) -> None:
        """重做最近 undo 的动作；空 redo 栈 no-op。

        AUD-36302：``redo_fn`` 抛错被吞（只记日志），但 action **留在
        redo 栈**不再转移回 undo 栈（理由同 ``undo``）。
        """
        if not self._redo_list:
            return
        action = self._redo_list.pop()
        try:
            action.redo_fn()
        except Exception:  # noqa: BLE001
            _LOG.exception("redo_fn raised; keeping action on redo stack (action=%r)", action.description)
            self._redo_list.append(action)
            self.changed.emit()
            return
        self._undo_list.append(action)
        self.changed.emit()

    def clear(self) -> None:
        """清空 undo + redo 栈。"""
        self._undo_list.clear()
        self._redo_list.clear()
        self.changed.emit()


__all__ = ["EditAction", "UndoStack"]
