"""HelpAssistant —— 无模型帮助 Skill（plan §10.3 P1.4）。

把 :class:`HelpIndex` 接入聊天路由：``HelpIndex.search`` 作为无模型可用 Skill
（plan §10.3 P1.4：``HelpIndex.search()`` 设为无模型可用 Skill）。

核心约束（plan §10.3 P1.4 验收）：

- 模型回答必须带 ``entry_id``、页面位置、当前 feature 状态、下一步。
- **不从模型记忆编造产品功能**：答案只能引用 :class:`HelpIndex` 已索引的
  :class:`HelpEntry`；找不到时明示"未在帮助索引中找到"，绝不编。
- **不可用功能必须说未安装 / 未启用**：``HelpEntry.enabled=False`` 或
  ``feature_id`` 对应功能未启用时，答案标注。
- 无 profile 状态下也能搜索帮助（plan §10.3 P1.4：``无 profile 状态下也能搜索
  帮助``）。

本模块纯 Python（不依赖 Qt / 模型）；UI 渲染由 ``ai_help_answer.py`` 负责。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# HelpEntry / HelpIndex 是现有模块
from src.features.edit.ai_editing.help_index import HelpEntry, HelpIndex


# ---------------------------------------------------------------------- #
# HelpAnswer（结构化回答，UI 据此渲染）
# ---------------------------------------------------------------------- #


@dataclass(frozen=True)
class HelpAnswerItem:
    """单条帮助引用（对应一个 HelpEntry）。"""

    entry_id: str
    title: str
    body: str
    page_ref: str = ""
    shortcut_hint: str = ""
    next_steps: tuple[str, ...] = ()
    feature_id: str | None = None
    feature_enabled: bool = True     # 当前是否可用（受 feature flag 影响）
    category: str = ""

    @property
    def available(self) -> bool:
        return self.feature_enabled

    @property
    def availability_label(self) -> str:
        if not self.feature_enabled:
            return "该功能当前未启用 / 未安装"
        return "可用"


@dataclass(frozen=True)
class HelpAnswer:
    """一次帮助查询的结构化回答。"""

    query: str
    items: tuple[HelpAnswerItem, ...] = ()
    found: bool = True
    #: 当 HelpIndex 为空（未装配）时的友好提示。
    index_empty: bool = False

    @property
    def summary(self) -> str:
        """一句话总结（UI 头部用）。"""
        if self.index_empty:
            return "帮助索引未装配（无法查询本地帮助）。"
        if not self.found or not self.items:
            return f"未在帮助索引中找到关于「{self.query}」的内容。"
        n = len(self.items)
        return f"找到 {n} 条相关帮助：" if n > 1 else "找到 1 条相关帮助："


# ---------------------------------------------------------------------- #
# HelpAssistant
# ---------------------------------------------------------------------- #


class HelpAssistant:
    """无模型帮助 Skill（plan §10.3 P1.4）。

    把 :class:`HelpIndex` 包成聊天可调的"无模型 Skill"——

    - :meth:`answer`：自然语言查询 → :class:`HelpAnswer`（结构化，绝不编造）。
    - :meth:`entry_ids_for_query`：router 用它把"功能怎么用"路由到 entry_id 列表
      （供 InvocationRouter.help_search 注入）。
    - :meth:`explain_diagnostic`：把诊断码翻译成可读解释 + 修复步骤。

    无 profile 也可用（不依赖模型 profile）。
    """

    def __init__(self, index: HelpIndex | None = None) -> None:
        self._index = index

    @property
    def available(self) -> bool:
        """HelpIndex 是否已装配。未装配时 answer 返回友好提示（不崩）。"""
        return self._index is not None and self._index.count > 0

    def attach(self, index: HelpIndex) -> None:
        """宿主装配 HelpIndex 后注入。"""
        self._index = index

    # ---------------- 主入口 ---------------- #

    def answer(self, query: str, *, limit: int = 5) -> HelpAnswer:
        """自然语言查询 → 结构化回答。

        绝不从模型记忆编造：所有内容只能引用已索引的 HelpEntry；找不到时
        ``found=False``，UI 显示"未找到"而非捏造步骤。
        """
        if not query or not query.strip():
            return HelpAnswer(query=query, items=(), found=False)
        if self._index is None or self._index.count == 0:
            return HelpAnswer(query=query, items=(), found=False, index_empty=True)
        entries = self._index.search(query.strip(), limit=limit)
        items = tuple(self._entry_to_item(e) for e in entries)
        return HelpAnswer(
            query=query,
            items=items,
            found=bool(items),
        )

    def entry_ids_for_query(self, query: str, *, limit: int = 10) -> list[str]:
        """供 InvocationRouter.help_search 注入：query → entry_id 列表。"""
        if self._index is None:
            return []
        return [e.entry_id for e in self._index.search(query, limit=limit)]

    def explain_diagnostic(self, code: str) -> HelpAnswer:
        """把诊断码翻译成可读解释（plan §0.3 / §9.1）。"""
        if self._index is None:
            return HelpAnswer(query=code, items=(), found=False, index_empty=True)
        entry = self._index.explain_diagnostic(code)
        if entry is None:
            return HelpAnswer(query=code, items=(), found=False)
        return HelpAnswer(
            query=code,
            items=(self._entry_to_item(entry),),
            found=True,
        )

    # ---------------- 内部 ---------------- #

    def _entry_to_item(self, e: HelpEntry) -> HelpAnswerItem:
        return HelpAnswerItem(
            entry_id=e.entry_id,
            title=e.title,
            body=e.body,
            page_ref=e.page_ref,
            shortcut_hint=e.shortcut_hint,
            next_steps=e.next_steps,
            feature_id=e.feature_id,
            feature_enabled=e.enabled,
            category=e.category,
        )


# ---------------------------------------------------------------------- #
# 工厂：从 HelpSources 装配（解耦，host 调用）
# ---------------------------------------------------------------------- #


def build_help_assistant(sources: Any = None) -> HelpAssistant:
    """从 HelpSources 装配 HelpAssistant（host 启动时调）。

    ``sources`` 满足 ``HelpSources`` Protocol；为 None 时返回空 assistant
   （answer 给 index_empty 提示，不崩）。
    """
    if sources is None:
        return HelpAssistant(index=None)
    try:
        index = HelpIndex.build(sources)
        return HelpAssistant(index=index)
    except Exception:  # noqa: BLE001 — 帮助装配失败不应阻断启动
        return HelpAssistant(index=None)


__all__ = [
    "HelpAnswer",
    "HelpAnswerItem",
    "HelpAssistant",
    "build_help_assistant",
]
