"""AiHelpAnswerCard UI 测试（plan §10.3 P1.4）。

覆盖：

- 找到时渲染 summary + 每条 item（标题/可用性/正文/位置/快捷键/下一步/entry_id）。
- 找不到时显示 empty 提示，不编造。
- index 未装配时显示 index_empty 提示。
- 不可用功能标注"未启用"。
- 点 entry_id / "打开" 发对应 signal。
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.edit_core

from PyQt6.QtWidgets import QLabel, QPushButton

from src.features.ai_harness.help_assistant import HelpAnswer, HelpAnswerItem
from src.features.edit.ui.ai_help_answer import AiHelpAnswerCard


def _answer(found=True, items=None, index_empty=False) -> HelpAnswer:
    return HelpAnswer(
        query="时间线",
        items=tuple(items or ()),
        found=found,
        index_empty=index_empty,
    )


def _item(feature_enabled=True, page_ref="面板 > 时间线") -> HelpAnswerItem:
    return HelpAnswerItem(
        entry_id="panel:timeline", title="时间线面板",
        body="在时间线面板编辑片段。",
        page_ref=page_ref, shortcut_hint="Ctrl+T",
        next_steps=("打开面板", "拖入素材"),
        feature_enabled=feature_enabled,
        category="panel",
    )


@pytest.fixture
def card(qapp):
    c = AiHelpAnswerCard()
    yield c
    c.deleteLater()


class TestRender:
    def test_summary_shown(self, card):
        card.set_answer(_answer(items=[_item()]))
        assert card._summary.text()

    def test_found_renders_item(self, card):
        card.set_answer(_answer(items=[_item()]))
        labels = card.findChildren(QLabel)
        label_text = " ".join(l.text() for l in labels)
        btns = card.findChildren(QPushButton)
        btn_text = " ".join(b.text() for b in btns)
        all_text = label_text + " " + btn_text
        assert "时间线面板" in label_text
        assert "Ctrl+T" in label_text
        assert "打开面板" in label_text  # next_steps
        assert "panel:timeline" in btn_text  # entry_id 在按钮里

    def test_not_found_shows_empty(self, card):
        card.set_answer(_answer(found=False, items=[]))
        assert card._empty.isHidden() is False
        assert card._items_host.isHidden() is True

    def test_index_empty_message(self, card):
        card.set_answer(_answer(found=False, items=[], index_empty=True))
        assert "未装配" in card._empty.text()

    def test_disabled_feature_marked(self, card):
        card.set_answer(_answer(items=[_item(feature_enabled=False)]))
        labels = card.findChildren(QLabel)
        all_text = " ".join(l.text() for l in labels)
        assert "未启用" in all_text or "未安装" in all_text

    def test_no_page_ref_hides_open_button(self, card):
        card.set_answer(_answer(items=[_item(page_ref="")]))
        # 没有 page_ref → 不应渲染"打开"按钮
        btns = card.findChildren(QPushButton)
        open_btns = [b for b in btns if b.text() == "打开"]
        assert open_btns == []


class TestSignals:
    def test_entry_ref_click_emits_navigate(self, card):
        card.set_answer(_answer(items=[_item()]))
        received = []
        card.navigate_requested.connect(lambda eid: received.append(eid))
        btns = card.findChildren(QPushButton)
        ref_btn = next(b for b in btns if "panel:timeline" in b.text())
        ref_btn.click()
        assert received == ["panel:timeline"]

    def test_open_click_emits_page_ref(self, card):
        card.set_answer(_answer(items=[_item(page_ref="面板 > 时间线")]))
        received = []
        card.open_panel_requested.connect(lambda p: received.append(p))
        btns = card.findChildren(QPushButton)
        open_btn = next(b for b in btns if b.text() == "打开")
        open_btn.click()
        assert received == ["面板 > 时间线"]
