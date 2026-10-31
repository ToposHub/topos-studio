"""UI 测试共享工具（P1-UI-02 规格裁决配套）。

AiEditAssistantPanel 的消息流自「消息优先排序」（_insert_message_flow_widget）
起不再保证动态卡片位于 stretch 前最后一项：streaming 标签与 run 活动区
（QFrame）会排在新插入卡片之后。测试必须按类型/身份查找卡片，禁止依赖
布局下标（itemAt(count-2) 一类的写法在新排序下取到的是 run 活动区）。
"""
from __future__ import annotations

from typing import Optional, Type, TypeVar

from PyQt6.QtWidgets import QWidget

W = TypeVar("W", bound=QWidget)


def find_flow_widget(owner: object, widget_type: Type[W], *, which: str = "last") -> Optional[W]:
    """在 ``owner._messages_layout`` 消息流中按类型查找控件。

    Args:
        owner: 持有 ``_messages_layout`` 的 panel（或持有 ``_panel`` 的
            controller——两者都支持，方便各测试直接传手头的对象）。
        widget_type: 目标控件类型（如 ``AiResultCard`` / ``AiSkillCard``）。
        which: ``"last"``（默认，取最新插入的一张）或 ``"first"``。

    Returns:
        匹配的控件；找不到返回 ``None``（由调用方断言）。
    """
    panel = getattr(owner, "_panel", owner)
    layout = panel._messages_layout
    found: list[W] = []
    for index in range(layout.count()):
        item = layout.itemAt(index)
        widget = item.widget() if item is not None else None
        if isinstance(widget, widget_type):
            found.append(widget)
    if not found:
        return None
    return found[-1] if which == "last" else found[0]
