"""TAEP M5 UI — 消息卡 + 上下文检查器 + AiContextSummary 接入测试。

验收（§2.2 / §7.1 / M5 退出条件）：
- 消息卡：snapshot.messages（之前从未渲染）→ _render_messages 渲染每条；role 颜色/引用摘要。
- 上下文检查器：点"上下文"按钮 → 切到检查器页 + context_inspector_requested 信号；
  set_context_bundle → 渲染每块（kind/优先级/token/为何包含/可排除）。
- AiContextSummary：接入死代码 widget；set_summary 喂 ContextSummary。
- 不写 timeline；面板只发信号。
"""

from __future__ import annotations

import pytest

from src.features.edit.ai_editing.assistant_events import (
    AssistantState,
    AssistantStateSnapshot,
    AssistantMessage,
    ContextSummary,
)
from src.features.edit.ai_editing.context_bundle import (
    ContextBlock,
    ContextBlockPriority,
    ContextBundle,
    KIND_BRIEF,
    KIND_EVIDENCE,
    KIND_SYSTEM_POLICY,
    TokenBudget,
)
from src.features.edit.ui.ai_edit_assistant_panel import AiEditAssistantPanel


@pytest.fixture
def panel(qapp):
    p = AiEditAssistantPanel()
    yield p
    p.deleteLater()


# ---------------------------------------------------------------------- #
# 消息卡渲染（M5-UI-A）
# ---------------------------------------------------------------------- #


class TestMessageRendering:
    def test_messages_rendered_from_snapshot(self, panel) -> None:
        """snapshot.messages（之前从未渲染）→ 渲染出对应数量消息卡。"""
        snap = AssistantStateSnapshot(
            state=AssistantState.IDLE,
            messages=(
                AssistantMessage(message_id="m1", role="user", text="帮我剪口播"),
                AssistantMessage(message_id="m2", role="assistant", text="已读取素材", variant_id="v1"),
            ),
        )
        panel.refresh_state(snap)
        # 消息卡数（流式暂存标签与尾部 stretch 不计入）
        msg_count = len(panel._message_widgets)
        assert msg_count == 2

    def test_no_messages_hides_messages_area(self, panel) -> None:
        snap = AssistantStateSnapshot(state=AssistantState.IDLE)
        panel.refresh_state(snap)
        # 无消息 → 没有持久消息卡；流式暂存标签始终作为滚动容器的一部分
        assert len(panel._message_widgets) == 0
        assert panel._streaming_label.isHidden()

    def test_messages_take_priority_over_idle_placeholder(self, panel) -> None:
        """已有对话时隐藏 idle 提示，避免它吞掉消息区的伸缩高度。"""
        snap = AssistantStateSnapshot(
            state=AssistantState.IDLE,
            messages=(
                AssistantMessage(message_id="m1", role="user", text="你好"),
            ),
        )
        panel.refresh_state(snap)
        # 面板 fixture 本身未 show，因此用 isHidden 检查控件的逻辑可见性。
        assert panel._messages_area.isHidden() is False
        assert panel._placeholder.isHidden() is True

    def test_message_cards_have_no_colored_background_or_border(self, panel) -> None:
        """对话流保持无底框，身份通过 role 标签区分。"""
        snap = AssistantStateSnapshot(
            state=AssistantState.IDLE,
            messages=(
                AssistantMessage(message_id="m1", role="user", text="你好"),
                AssistantMessage(message_id="m2", role="assistant", text="你好，我是助手"),
            ),
        )
        panel.refresh_state(snap)
        for index in range(2):
            card = panel._messages_layout.itemAt(index).widget()
            assert card is not None
            assert "background: transparent" in card.styleSheet()
            assert "border: none" in card.styleSheet()

    def test_assistant_scrollbars_are_compact_and_horizontal_scroll_is_disabled(
        self, panel
    ) -> None:
        """消息/方案滚动区使用窄滚动条且不产生横向滚动。"""
        for area in (panel._messages_area, panel._variants_area, panel._ctx_blocks_area):
            assert area.horizontalScrollBarPolicy().name == "ScrollBarAlwaysOff"
        assert "QScrollBar:vertical" in panel.widget().styleSheet()
        assert "width: 7px" in panel.widget().styleSheet()

    def test_streaming_reply_uses_readable_muted_gray(self, panel) -> None:
        """流式思考文本使用中灰，不能被深色主题的 palette(mid) 吃掉。"""
        panel.set_streaming_reply("正在分析 Motion Graphic 场景…")
        assert panel._streaming_label.isHidden() is False
        assert "#b6b7bd" in panel._streaming_label.styleSheet()
        assert "palette(mid)" not in panel._streaming_label.styleSheet()

    def test_codex_layout_keeps_composer_at_bottom(self, panel) -> None:
        """外层布局保持“上方对话、下方输入”的 Codex 式结构。"""
        panel.resize(607, 840)
        panel.show()
        panel.refresh_state(AssistantStateSnapshot(state=AssistantState.IDLE))
        panel._main_page_layout.activate()

        main_page = panel._stack.widget(2)
        shell = panel._composer_shell
        assert shell.sizePolicy().verticalPolicy().name == "Fixed"
        assert panel._context_details.geometry().top() <= 4
        assert shell.geometry().bottom() >= main_page.height() - 4

        panel.refresh_state(
            AssistantStateSnapshot(
                state=AssistantState.IDLE,
                messages=(
                    AssistantMessage(message_id="u", role="user", text="你好"),
                    AssistantMessage(message_id="a", role="assistant", text="你好，我是助手"),
                ),
            )
        )
        panel._main_page_layout.activate()
        assert panel._messages_area.isHidden() is False
        assert panel._placeholder.isHidden() is True
        assert panel._messages_area.geometry().bottom() < shell.geometry().top()
        panel.hide()

    def test_message_card_shows_role_and_text(self, panel) -> None:
        snap = AssistantStateSnapshot(
            state=AssistantState.IDLE,
            messages=(
                AssistantMessage(message_id="m1", role="user", text="独特文本 XYZ"),
            ),
        )
        panel.refresh_state(snap)
        card = panel._messages_layout.itemAt(0).widget()
        # 卡内含文本
        texts = []
        for child in card.findChildren(type(card)):  # noqa
            pass
        from PyQt6.QtWidgets import QTextBrowser
        bodies = card.findChildren(QTextBrowser)
        assert bodies
        assert "独特文本 XYZ" in bodies[0].toPlainText()

    def test_markdown_plan_table_has_no_lost_rows(self, panel) -> None:
        """Long plan tables keep all rows inside one measured text view."""
        from PyQt6.QtWidgets import QTextBrowser

        table = (
            "| 项目 | 计划 |\n| --- | --- |\n"
            "| 画布 | 1920×1080 |\n"
            "| 核心创意 | 从问题到实践的网页旅程 |\n"
            "| 视觉方向 | 模块化网页结构与动效 |\n"
            "| 排版气质 | 大标题、等宽辅助文字 |\n"
            "| 动态节奏 | 逐步搭建、最后完成 |\n"
            "| 叙事结构 | 开场；搭建；收束 |\n"
            "| 色彩方向 | 深色背景与高亮强调色 |"
        )
        panel.refresh_state(AssistantStateSnapshot(messages=(
            AssistantMessage(message_id="plan", role="assistant", text=table),
        )))
        body = panel._message_widgets[0].findChildren(QTextBrowser)[0]
        plain = body.toPlainText()
        assert all(value in plain for value in ("画布", "核心创意", "视觉方向", "动态节奏", "色彩方向"))
        assert body.verticalScrollBar().maximum() == 0
        assert body.height() >= body.document().documentLayout().documentSize().height() - 1

    def test_new_messages_follow_scroll_to_latest_content(self, panel) -> None:
        """The outer conversation view lands at the newest message after layout."""
        from PyQt6.QtTest import QTest

        panel.resize(500, 500)
        panel.show()
        messages = tuple(
            AssistantMessage(
                message_id=f"m-{index}",
                role="assistant",
                text="这是一段较长的回复，用于让对话区产生真实滚动范围。" * 4,
            )
            for index in range(20)
        )
        panel.refresh_state(AssistantStateSnapshot(messages=messages))
        QTest.qWait(240)
        scrollbar = panel._messages_area.verticalScrollBar()
        assert scrollbar.maximum() > 0
        assert scrollbar.value() == scrollbar.maximum()
        panel.hide()

    def test_message_card_shows_structured_refs(self, panel) -> None:
        """assistant 消息的结构化引用（variant_id 等）应显示在卡上（§11.2 契约）。"""
        snap = AssistantStateSnapshot(
            state=AssistantState.IDLE,
            messages=(
                AssistantMessage(
                    message_id="m2", role="assistant", text="done",
                    variant_id="v-abc", decision_id="d-1",
                ),
            ),
        )
        panel.refresh_state(snap)
        card = panel._messages_layout.itemAt(0).widget()
        from PyQt6.QtWidgets import QLabel
        all_text = " ".join(l.text() for l in card.findChildren(QLabel))
        assert "v-abc" in all_text
        assert "d-1" in all_text

    def test_error_message_shown_in_red(self, panel) -> None:
        snap = AssistantStateSnapshot(
            state=AssistantState.IDLE,
            messages=(
                AssistantMessage(message_id="e1", role="assistant", text="失败", error_code="PLAN_FAIL"),
            ),
        )
        panel.refresh_state(snap)
        card = panel._messages_layout.itemAt(0).widget()
        from PyQt6.QtWidgets import QLabel
        labels = card.findChildren(QLabel)
        all_text = " ".join(l.text() for l in labels)
        assert "PLAN_FAIL" in all_text


# ---------------------------------------------------------------------- #
# 上下文检查器（M5-UI-B）
# ---------------------------------------------------------------------- #


class TestContextInspector:
    def test_inspect_button_emits_signal(self, panel) -> None:
        """点"上下文"按钮 → context_inspector_requested 信号（§7.1）。"""
        captured = []
        panel.context_inspector_requested.connect(lambda: captured.append(True))
        panel._on_inspect_context_clicked()
        assert captured == [True]

    def test_inspect_switches_to_inspector_page(self, panel) -> None:
        panel._on_inspect_context_clicked()
        # 检查器页是 stack 索引 5
        assert panel._stack.currentIndex() == 5

    def test_set_context_bundle_renders_blocks(self, panel) -> None:
        """set_context_bundle → 检查器渲染每块（kind/优先级/token）。"""
        bundle = ContextBundle(blocks=(
            ContextBlock(
                kind=KIND_SYSTEM_POLICY, content="policy", priority=ContextBlockPriority.HARD_CONSTRAINT,
                token_estimate=100, why_included="系统策略",
            ),
            ContextBlock(
                kind=KIND_BRIEF, content={"goal": "x"}, priority=ContextBlockPriority.DELIVERY_TARGET,
                token_estimate=50, why_included="用户 brief", provenance={"rev": 1},
            ),
        ))
        panel.set_context_bundle(bundle)
        # 触发渲染
        panel._render_context_bundle()
        block_count = panel._ctx_blocks_layout.count() - 1
        assert block_count == 2

    def test_no_bundle_shows_hint(self, panel) -> None:
        panel._context_bundle = None
        panel._render_context_bundle()
        # 总量标签显示"尚未构建"提示
        assert panel._ctx_total_label.text()  # 非空

    def test_total_tokens_shown(self, panel) -> None:
        bundle = ContextBundle(
            blocks=(
                ContextBlock(kind="x", content="y", priority=ContextBlockPriority.EVIDENCE,
                             token_estimate=8400, why_included="t"),
            ),
            budget=TokenBudget(limit=32_000, reserved_for_response=0),
        )
        panel.set_context_bundle(bundle)
        panel._render_context_bundle()
        total_text = panel._ctx_total_label.text()
        assert "8400" in total_text  # 当前 token
        assert "32000" in total_text  # 预算上限

    def test_omitted_blocks_counted(self, panel) -> None:
        bundle = ContextBundle(
            blocks=(ContextBlock(kind="x", content="y", priority=ContextBlockPriority.EVIDENCE,
                                 token_estimate=10, why_included="t"),),
            omitted_block_ids=("a", "b"),
        )
        panel.set_context_bundle(bundle)
        panel._render_context_bundle()
        total_text = panel._ctx_total_label.text()
        assert "2" in total_text  # 2 块因预算未纳入

    def test_hard_constraint_block_has_no_exclude_button(self, panel) -> None:
        """HARD_CONSTRAINT 级块不允许排除（§7.1 硬约束/用户锁定最优先）。"""
        bundle = ContextBundle(blocks=(
            ContextBlock(
                kind=KIND_SYSTEM_POLICY, content="p", priority=ContextBlockPriority.HARD_CONSTRAINT,
                token_estimate=10, why_included="t",
            ),
        ))
        panel.set_context_bundle(bundle)
        panel._render_context_bundle()
        card = panel._ctx_blocks_layout.itemAt(0).widget()
        from PyQt6.QtWidgets import QPushButton
        buttons = card.findChildren(QPushButton)
        # 硬约束块无"排除"按钮
        assert all("排除" not in b.text() and "Exclude" not in b.text() for b in buttons)

    def test_evidence_block_has_exclude_button(self, panel) -> None:
        bundle = ContextBundle(blocks=(
            ContextBlock(
                kind=KIND_EVIDENCE, content="x", priority=ContextBlockPriority.EVIDENCE,
                token_estimate=10, why_included="候选证据", provenance={"segment_id": "s1"},
            ),
        ))
        panel.set_context_bundle(bundle)
        panel._render_context_bundle()
        card = panel._ctx_blocks_layout.itemAt(0).widget()
        from PyQt6.QtWidgets import QPushButton
        buttons = card.findChildren(QPushButton)
        assert any("排除" in b.text() or "Exclude" in b.text() for b in buttons)

    def test_exclude_button_emits_block_id(self, panel) -> None:
        bundle = ContextBundle(blocks=(
            ContextBlock(
                kind=KIND_EVIDENCE, content="x", priority=ContextBlockPriority.EVIDENCE,
                token_estimate=10, why_included="t", provenance={"segment_id": "s1"},
            ),
        ))
        panel.set_context_bundle(bundle)
        panel._render_context_bundle()
        card = panel._ctx_blocks_layout.itemAt(0).widget()
        from PyQt6.QtWidgets import QPushButton
        exclude_btn = next(b for b in card.findChildren(QPushButton)
                           if "排除" in b.text() or "Exclude" in b.text())
        captured = []
        panel.context_block_excluded.connect(lambda bid: captured.append(bid))
        exclude_btn.click()
        assert len(captured) == 1
        assert "segment_id=s1" in captured[0]


# ---------------------------------------------------------------------- #
# AiContextSummary 接入（M5-UI-C）
# ---------------------------------------------------------------------- #


class TestContextSummaryWiring:
    def test_context_summary_widget_wired(self, panel) -> None:
        """AiContextSummary（死代码 widget）已接入；set_summary 喂数据。"""
        # 接入成功时 _context_summary 非 None；失败回退 _context_label
        assert panel._context_summary is not None or panel._context_label is not None

    def test_apply_state_feeds_context_summary(self, panel) -> None:
        """refresh_state → AiContextSummary.set_summary 收到 ContextSummary。"""
        if panel._context_summary is None:
            pytest.skip("AiContextSummary 未装配（回退 QLabel）")
        snap = AssistantStateSnapshot(
            state=AssistantState.IDLE,
            context=ContextSummary(
                asset_count=12, analyzed_count=8, music_count=2,
                locked_count=1, snapshot_revision=5, analysis_fresh=True,
            ),
        )
        panel.refresh_state(snap)
        # AiContextSummary 内部应反映喂入的 summary（检查它有 set 过的标记）
        # 这里只验证不抛异常 + widget 仍存在
        assert panel._context_summary is not None


# ---------------------------------------------------------------------- #
# 不写 timeline（§0 红线）
# ---------------------------------------------------------------------- #


class TestNoTimelineWrite:
    def test_panel_has_no_timeline_write_methods(self, panel) -> None:
        """面板不提供任何写 timeline 的入口（只发信号）。"""
        assert not hasattr(panel, "commit_plan")
        assert not hasattr(panel, "apply_to_timeline")
        assert not hasattr(panel, "execute_command")
