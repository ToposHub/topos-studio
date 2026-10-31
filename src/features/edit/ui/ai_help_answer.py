"""AI 帮助回答卡片（plan §10.3 P1.4）。

渲染 :class:`HelpAnswer`：每条引用显示 entry_id / 标题 / 正文 / 页面位置 /
快捷键 / 下一步 / feature 状态。**不编造**——只展示 HelpIndex 真实命中。

约束（与既有面板一致）：只发 signal（``navigate_requested(entry_id)`` /
``open_panel_requested(page_ref)``），不直接跳转 / 不执行命令。
"""

from __future__ import annotations

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QFrame,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from src.features.ai_harness.help_assistant import HelpAnswer, HelpAnswerItem
from src.shared.i18n.i18n import tr


def _trf(key: str, default: str, **kwargs) -> str:
    text = tr(key, default, **kwargs)
    if "{" in default and kwargs:
        try:
            return text.format(**kwargs)
        except (KeyError, IndexError):
            return text
    return text


class AiHelpAnswerCard(QWidget):
    """渲染一条 :class:`HelpAnswer`（plan §10.3 P1.4）。

    Signals：

    - ``navigate_requested(str)``：用户点 entry_id 引用（UI 据此定位）。
    - ``open_panel_requested(str)``：用户点"打开页面"（page_ref）。
    """

    navigate_requested = pyqtSignal(str)
    open_panel_requested = pyqtSignal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._answer: HelpAnswer | None = None
        self._build_ui()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        self._summary = QLabel()
        self._summary.setStyleSheet("font-weight: bold; padding: 4px;")
        self._summary.setWordWrap(True)
        layout.addWidget(self._summary)

        self._items_host = QWidget()
        self._items_layout = QVBoxLayout(self._items_host)
        self._items_layout.setContentsMargins(0, 0, 0, 0)
        self._items_layout.setSpacing(6)
        layout.addWidget(self._items_host)

        self._empty = QLabel()
        self._empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._empty.setStyleSheet("color: gray; padding: 12px;")
        self._empty.setVisible(False)
        layout.addWidget(self._empty)

    def set_answer(self, answer: HelpAnswer) -> None:
        self._answer = answer
        self._summary.setText(answer.summary)

        # 清空旧条目
        while self._items_layout.count():
            it = self._items_layout.takeAt(0)
            w = it.widget()
            if w is not None:
                w.setParent(None)
                w.deleteLater()

        if not answer.items:
            self._empty.setText(
                tr("ai_help.empty", "没有匹配的帮助。试试换个关键词，或输入'生成字幕'等命令。")
                if not answer.index_empty
                else tr("ai_help.index_empty", "帮助索引未装配。")
            )
            self._empty.setVisible(True)
            self._items_host.setVisible(False)
            return

        self._empty.setVisible(False)
        self._items_host.setVisible(True)
        for item in answer.items:
            self._items_layout.addWidget(self._build_item_card(item))

    def _build_item_card(self, item: HelpAnswerItem) -> QWidget:
        card = QFrame()
        card.setFrameShape(QFrame.Shape.StyledPanel)
        border_color = "#27ae60" if item.feature_enabled else "#c0392b"
        card.setStyleSheet(
            "QFrame{background: palette(window);"
            f" border-left: 3px solid {border_color};"
            " border-top: 1px solid palette(mid);"
            " border-right: 1px solid palette(mid);"
            " border-bottom: 1px solid palette(mid);"
            " border-radius: 3px;}"
        )
        v = QVBoxLayout(card)
        v.setContentsMargins(8, 6, 8, 6)
        v.setSpacing(3)

        # 标题 + 可用性
        title_row = QLabel(
            f"<b>{item.title}</b> "
            f"<span style='color:{border_color}; font-size: small'>"
            f"({'可用' if item.feature_enabled else '未启用/未安装'})</span>"
        )
        title_row.setTextFormat(Qt.TextFormat.RichText)
        title_row.setWordWrap(True)
        v.addWidget(title_row)

        # 正文
        if item.body:
            body = QLabel(item.body)
            body.setWordWrap(True)
            body.setStyleSheet("color: palette(text);")
            v.addWidget(body)

        # 页面位置
        if item.page_ref:
            loc = QLabel(
                _trf("ai_help.location", "位置：{where}", where=item.page_ref)
            )
            loc.setStyleSheet("color: gray;")
            loc.setWordWrap(True)
            v.addWidget(loc)
            open_btn = QPushButton(tr("ai_help.open", "打开"))
            open_btn.setStyleSheet("padding: 2px 8px;")
            open_btn.clicked.connect(
                lambda _, p=item.page_ref: self.open_panel_requested.emit(p)
            )
            v.addWidget(open_btn)

        # 快捷键
        if item.shortcut_hint:
            sc = QLabel(
                _trf("ai_help.shortcut", "快捷键：{shortcut}", shortcut=item.shortcut_hint)
            )
            sc.setStyleSheet("color: gray;")
            v.addWidget(sc)

        # 下一步
        if item.next_steps:
            steps_txt = "\n".join(f"{i+1}. {s}" for i, s in enumerate(item.next_steps))
            steps = QLabel(
                _trf("ai_help.next_steps", "下一步：\n{steps}", steps=steps_txt)
            )
            steps.setWordWrap(True)
            steps.setStyleSheet("color: palette(text);")
            v.addWidget(steps)

        # entry_id 引用（点击定位）
        ref_btn = QPushButton(
            _trf("ai_help.entry_ref", "引用：{id}", id=item.entry_id)
        )
        ref_btn.setStyleSheet("padding: 1px 6px; color: gray; font-size: small;")
        ref_btn.setFlat(True)
        ref_btn.clicked.connect(
            lambda _, eid=item.entry_id: self.navigate_requested.emit(eid)
        )
        v.addWidget(ref_btn)

        return card

    @property
    def answer(self) -> HelpAnswer | None:
        return self._answer


__all__ = ["AiHelpAnswerCard"]
