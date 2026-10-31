"""AiEditAssistantPanel P0 集成测试（plan §10.2 P0.3 / P0.6）。

验证面板（AiEditAssistantPanel）正确装配了 Composer footer + Run Drawer，
并暴露新的 signal 与 state 注入入口。

覆盖：

- 面板含 ``_composer_footer`` 与 ``_run_drawer``（装配成功）。
- 新 signal 存在：composer_scope_requested / permission_changed / new_session /
  run_cancel_requested 等。
- ``set_composer_footer`` / ``set_run_drawer`` 把 fake 状态灌进组件。
- footer 的 permission 切换透传到面板级 signal。
- Drawer 的 cancel 点击透传到面板级 ``run_cancel_requested``。

红线（plan §10.2 P0.3 / P0.6）：面板只发 signal；设置只作用于后续 invocation。
"""

from __future__ import annotations

import pytest
import time
from pathlib import Path
from PyQt6.QtGui import QImage
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QApplication

pytestmark = pytest.mark.edit_core

from src.features.edit.ui.ai_edit_assistant_panel import AiEditAssistantPanel
from src.features.edit.ai_editing.assistant_events import (
    AssistantState,
    AssistantStateSnapshot,
)
from src.features.edit.ui.ai_assistant_composer import (
    ComposerFooterState,
    RunDrawerItem,
    RunDrawerState,
)


def _wait_attachment_jobs_settled(panel, qapp, timeout_s: float = 5.0) -> None:
    """AUD-19511 后图片附件解码/编码为后台任务；驱动事件循环等待完成。"""
    deadline = time.monotonic() + timeout_s
    while panel._attachment_jobs_pending > 0 and time.monotonic() < deadline:
        QApplication.processEvents()
        time.sleep(0.01)
    QApplication.processEvents()


@pytest.fixture
def panel(qapp):
    p = AiEditAssistantPanel()
    yield p
    p.deleteLater()


class TestPanelP0Wiring:
    def test_panel_has_composer_footer(self, panel):
        assert panel._composer_footer is not None

    def test_panel_has_run_drawer(self, panel):
        assert panel._run_drawer is not None

    def test_panel_exposes_p0_signals(self, panel):
        for sig_name in (
            "composer_scope_requested",
            "composer_permission_changed",
            "composer_profile_requested",
            "context_compress_requested",
            "analyze_context_requested",
            "new_session_requested",
            "show_history_requested",
            "clear_history_requested",
            "frame_grab_requested",
            "run_cancel_requested",
            "run_retry_requested",
            "run_diagnostics_requested",
        ):
            assert hasattr(panel, sig_name), f"missing signal: {sig_name}"


class TestPanelComposerInjection:
    def test_set_composer_footer_updates_widget(self, panel):
        panel.set_composer_footer(ComposerFooterState(
            scope_label="当前选择", scope_asset_count=5,
            profile_label="本地 Qwen",
            context_limit=200_000,
        ))
        assert "当前选择" in panel._composer_footer._scope_btn.text()
        assert "本地 Qwen" in panel._composer_footer._profile_btn.text()
        assert "200k" in panel._context_budget_label.text()

    def test_configure_models_action_emits(self, panel):
        opened = []
        panel.configure_models_requested.connect(lambda: opened.append(True))
        action = next(
            action for action in panel._build_overflow_menu().actions()
            if action.text() in ("配置模型", "Configure models")
        )
        action.trigger()
        assert opened == [True]

    def test_permission_change_propagates_to_panel_signal(self, panel):
        received = []
        panel.composer_permission_changed.connect(lambda p: received.append(p))
        # 切 combo 到 read_only（index 0）
        panel._composer_footer._perm_combo.setCurrentIndex(0)
        assert received == ["read_only"]

    def test_add_menu_reuses_existing_panel_actions(self, panel):
        grabbed = []
        configured = []
        panel.frame_grab_requested.connect(lambda: grabbed.append(True))
        panel.configure_models_requested.connect(lambda: configured.append(True))

        panel._on_composer_add_action("current_frame")
        panel._on_composer_add_action("models")

        assert grabbed == [True]
        assert configured == [True]

    def test_real_files_can_be_dropped_into_prompt(self, panel, qapp, tmp_path: Path):
        """Exercise the same attachment path used by a desktop file drop."""
        image_path = tmp_path / "真实图片.png"
        image = QImage(8, 8, QImage.Format.Format_RGB32)
        image.fill(0xFF8844)
        assert image.save(str(image_path))

        files = [
            image_path,
            tmp_path / "真实视频.mp4",
            tmp_path / "真实音频.wav",
            tmp_path / "真实文档.pdf",
            tmp_path / "真实文本.md",
        ]
        files[1].write_bytes(b"\x00\x00\x00\x18ftypmp42")
        files[2].write_bytes(b"RIFF\x24\x00\x00\x00WAVEfmt ")
        files[3].write_bytes(b"%PDF-1.7\n1 0 obj\n<<>>\nendobj\n")
        files[4].write_text("# 媒体池测试\n", encoding="utf-8")

        panel._on_files_dropped({"paths": tuple(str(path) for path in files)})
        _wait_attachment_jobs_settled(panel, qapp)

        assert len(panel._pending_attachments) == len(files)
        # AUD-19511 后图片附件经后台线程编码完成才入列，同步文件附件先行
        # 入列 —— 附件集合语义不变，插入顺序不再保证，按多重集断言。
        assert sorted(panel.pending_attachment_kinds()) == sorted(
            ("image", "video", "audio", "file", "file"),
        )

    def test_at_picker_attaches_a_media_pool_file(self, panel, qapp, tmp_path: Path):
        image_path = tmp_path / "媒体池素材.png"
        image = QImage(8, 8, QImage.Format.Format_RGB32)
        image.fill(0x33AA66)
        assert image.save(str(image_path))
        panel.set_media_pool_provider(lambda: ({
            "asset_id": "asset-real-1",
            "path": str(image_path),
            "display_name": "媒体池素材.png",
            "media_type": "image",
        },))

        panel._request_edit.setPlainText("@")
        cursor = panel._request_edit.textCursor()
        cursor.setPosition(1)
        panel._request_edit.setTextCursor(cursor)
        panel._update_media_mentions()
        assert panel._media_popup is not None
        assert panel._media_popup.count() == 1
        panel._on_media_navigation("accept")
        _wait_attachment_jobs_settled(panel, qapp)

        assert len(panel._pending_attachments) == 1
        assert "@媒体池素材.png" in panel._request_edit.toPlainText()

    def test_motion_graphic_drag_is_an_explicit_target_not_a_file(self, panel):
        """Virtual Clips must keep their stable asset ID when dragged to chat."""
        panel.set_media_pool_provider(lambda: ({
            "asset_id": "teaching_animation:scene-a",
            "path": "topos://motion_graphic/scene-a",
            "display_name": "HTML 课程演示",
            "media_type": "motion_graphic",
        },))

        panel._on_files_dropped({
            "paths": (),
            "asset_ids": ("teaching_animation:scene-a",),
        })

        assert panel._pending_attachments == []
        assert panel.pending_target_asset_ids() == ("teaching_animation:scene-a",)
        assert panel.pending_attachment_kinds() == ("motion_graphic",)
        assert not panel._attachment_preview_row.isHidden()

        received = []
        panel.request_submitted.connect(received.append)
        panel.submit_request("把配色改成深色霓虹")
        assert len(received) == 1
        assert received[0].target_asset_ids == ("teaching_animation:scene-a",)
        assert received[0].attachments == ()

    def test_at_picker_selects_motion_graphic_target(self, panel):
        panel.set_media_pool_provider(lambda: ({
            "asset_id": "teaching_animation:scene-b",
            "path": "topos://motion_graphic/scene-b",
            "display_name": "产品发布 Clip",
            "media_type": "motion_graphic",
        },))

        panel._request_edit.setPlainText("@")
        cursor = panel._request_edit.textCursor()
        cursor.setPosition(1)
        panel._request_edit.setTextCursor(cursor)
        panel._update_media_mentions()
        panel._on_media_navigation("accept")

        assert panel.pending_target_asset_ids() == ("teaching_animation:scene-b",)
        assert "@产品发布 Clip" in panel._request_edit.toPlainText()
        assert panel._pending_attachments == []


class TestPanelRunDrawerInjection:
    def test_set_run_drawer_updates_widget(self, panel):
        panel.set_run_drawer(RunDrawerState(items=[
            RunDrawerItem(item_id="i1", label="转录", status="running", can_cancel=True),
        ]))
        assert panel._run_drawer._items_layout.count() == 1  # single-line mode keeps no detail rows
        assert "转录" in panel._run_drawer._title.text()
        assert panel._messages_layout.indexOf(panel._run_section) >= 0
        assert panel._run_section.parentWidget() is panel._messages_container

    def test_cancel_propagates_to_panel_signal(self, panel):
        cancelled = []
        panel.run_cancel_requested.connect(lambda iid: cancelled.append(iid))
        panel.set_run_drawer(RunDrawerState(items=[
            RunDrawerItem(item_id="run-1", label="ASR", status="running", can_cancel=True),
        ]))
        from PyQt6.QtWidgets import QPushButton
        assert isinstance(panel._run_drawer._single_line_action, QPushButton)
        panel._run_drawer._single_line_action.click()
        assert cancelled == ["run-1"]

    def test_activity_status_is_visible_in_header_and_fixed_footer(self, panel):
        panel.refresh_state(AssistantStateSnapshot(state=AssistantState.PLANNING))
        assert not panel._thinking_indicator.isHidden()
        panel.set_run_drawer(RunDrawerState(items=[
            RunDrawerItem(item_id="run-1", label="探索", status="running"),
        ]))
        assert panel._thinking_indicator.isHidden()
        assert panel._state_label.isHidden()
        assert panel._activity_status_bar.isHidden()
        assert panel._run_drawer._single_line is True
        assert panel._run_drawer._toggle_btn.isHidden()
        assert "探索" in panel._run_drawer._title.text()
        panel.set_run_drawer(RunDrawerState())
        assert panel._activity_status_bar.isHidden()

    def test_activity_status_shimmer_moves_without_scrolling(self, panel):
        panel.set_activity_status(
            "正在搜索项目文件",
            detail="已找到 12 个相关文件",
            active=True,
        )
        first = panel._run_drawer._title.text()
        first_angle = panel._run_drawer._activity_spinner.angle
        first_phase = panel._run_drawer._title.shimmer_phase
        panel._animate_activity_status()
        second = panel._run_drawer._title.text()
        second_angle = panel._run_drawer._activity_spinner.angle
        second_phase = panel._run_drawer._title.shimmer_phase
        # The ring/highlight animate in place; changing the status glyph is
        # deliberately avoided because it made the entire sentence jump.
        assert first == second
        assert (first_angle, first_phase) != (second_angle, second_phase)
        assert panel._activity_shimmer_timer.isActive()
        panel.set_activity_status(
            "已完成",
            detail="耗时 3 秒",
            active=False,
            status="succeeded",
        )
        assert not panel._activity_shimmer_timer.isActive()
        assert "✓" in panel._run_drawer._title.text()
        assert "耗时 3 秒" in panel._run_drawer._title.toolTip()

    def test_message_body_uses_markdown_and_cancel_preserves_stream(self, panel):
        from src.features.edit.ai_editing.assistant_events import AssistantMessage
        from PyQt6.QtWidgets import QTextBrowser

        panel.refresh_state(AssistantStateSnapshot(messages=(AssistantMessage(
            message_id="markdown-1", role="assistant",
            text="**结果**\n\n```python\nprint('ok')\n```",
        ),)))
        bodies = [
            browser for browser in panel._messages_container.findChildren(QTextBrowser)
            if "结果" in browser.toPlainText()
        ]
        assert bodies
        assert "print('ok')" in bodies[0].toPlainText()
        assert bodies[0].verticalScrollBar().maximum() == 0

        panel.preserve_cancelled_streaming_reply("部分回复")
        assert "部分回复" in panel._streaming_label.text()
        assert "已停止生成" in panel._streaming_label.text()

    def test_retry_propagates(self, panel):
        retried = []
        panel.run_retry_requested.connect(lambda iid: retried.append(iid))
        panel.set_run_drawer(RunDrawerState(items=[
            RunDrawerItem(item_id="run-2", label="OCR", status="failed", can_retry=True),
        ]))
        from PyQt6.QtWidgets import QPushButton
        assert isinstance(panel._run_drawer._single_line_action, QPushButton)
        panel._run_drawer._single_line_action.click()
        assert retried == ["run-2"]


class TestPanelConversationUx:
    def test_slash_command_palette_filters_and_executes(self, panel):
        triggered = []
        panel.analyze_context_requested.connect(lambda: triggered.append(True))

        panel._request_edit.setPlainText("/")
        assert panel._slash_popup is not None
        assert not panel._slash_popup.isHidden()
        assert panel._slash_popup.count() >= 8

        panel._request_edit.setPlainText("/ana")
        assert panel._slash_popup.count() == 1
        assert panel._slash_popup.item(0).data(Qt.ItemDataRole.UserRole) == "analyze"
        panel._on_slash_navigation("accept")

        assert triggered == [True]
        assert panel._request_edit.toPlainText() == ""
        assert panel._slash_popup.isHidden()

    def test_slash_command_palette_supports_keyboard_navigation(self, panel):
        panel._request_edit.setPlainText("/")
        assert panel._slash_popup.currentRow() == 0
        panel._on_slash_navigation("down")
        assert panel._slash_popup.currentRow() == 1
        panel._on_slash_navigation("up")
        assert panel._slash_popup.currentRow() == 0
        panel._on_slash_navigation("dismiss")
        assert panel._slash_popup.isHidden()

    def test_send_button_uses_arrow_and_square_busy_states(self, panel):
        assert panel._send_btn is panel._cancel_btn
        assert panel._send_btn.text() == "↑"
        assert panel._send_btn.size().width() == 24
        assert panel._send_btn.size().height() == 24
        assert panel._request_edit.parentWidget() is panel._composer_input_frame
        assert panel._composer_footer.parentWidget() is panel._composer_input_frame

        panel.refresh_state(AssistantStateSnapshot(state=AssistantState.PLANNING))
        assert panel._send_btn.text() == "■"
        assert panel._send_btn.isEnabled() is True
        assert panel._send_btn.toolTip() == "停止生成"

        panel.refresh_state(AssistantStateSnapshot(state=AssistantState.IDLE))
        assert panel._send_btn.text() == "↑"
        assert panel._send_btn.toolTip() == "发送"

    def test_busy_send_button_emits_cancel(self, panel):
        cancelled = []
        panel.cancel_requested.connect(lambda: cancelled.append(True))
        panel.refresh_state(AssistantStateSnapshot(state=AssistantState.PLANNING))

        panel._send_btn.click()

        assert cancelled == [True]

    def test_messages_area_is_scrollable_and_thinking_is_visible(self, panel):
        from PyQt6.QtCore import Qt

        assert panel._messages_area.verticalScrollBarPolicy() == Qt.ScrollBarPolicy.ScrollBarAsNeeded
        panel.set_thinking(True)
        assert not panel._thinking_indicator.isHidden()
        assert "思考中" in panel._thinking_indicator.text()
        panel.set_thinking(False)
        assert panel._thinking_indicator.isHidden()

    def test_history_is_a_clickable_submenu(self, panel):
        selected = []
        panel.history_session_requested.connect(selected.append)
        panel.set_history_sessions([{
            "session_id": "session-1",
            "title": "第一次对话",
            "messages": [{"role": "user", "text": "帮我剪一个片头"}],
        }])
        menu = panel._build_overflow_menu()
        history_action = next(
            action for action in menu.actions()
            if action.text() in ("历史会话", "History")
        )
        actions = history_action.menu().actions()
        assert len(actions) == 1
        actions[0].trigger()
        assert selected == ["session-1"]

    def test_header_uses_active_conversation_title(self, panel):
        from src.features.edit.ai_editing.assistant_events import AssistantMessage

        panel.refresh_state(AssistantStateSnapshot(messages=(AssistantMessage(
            message_id="m-title",
            role="user",
            text="优化 AI assistant 页面布局并统一发送按钮",
        ),)))

        assert panel._conversation_title_btn.text() == "优化 AI a…"
        assert len(panel._conversation_title_btn.text()) <= 8
        assert "DeepSeek" not in panel._conversation_title_btn.text()

        panel.set_history_sessions([{
            "session_id": "session-title",
            "title": "我手动命名的会话标题",
            "messages": [],
        }], active_session_id="session-title")
        assert panel._conversation_title_btn.text() == "我手动命名的会…"
        assert "我手动命名的会话标题" in panel._conversation_title_btn.toolTip()
        assert panel._conversation_title_btn.sizePolicy().horizontalPolicy().name == "Maximum"

    def test_bottom_overflow_and_frame_button_are_removed(self, panel):
        assert not hasattr(panel._composer_footer, "_more_btn")
        assert not hasattr(panel, "_grab_frame_btn")

    def test_frame_grab_is_available_from_context_menu(self, panel):
        requested = []
        panel.frame_grab_requested.connect(lambda: requested.append(True))
        menu = panel._build_overflow_menu(include_frame_grab=True)
        action = next(
            action for action in menu.actions()
            if action.text() in ("抓取当前画面作为图片附件", "Capture current frame")
        )
        action.trigger()
        assert requested == [True]

    def test_prompt_enter_submits_and_shift_enter_remains_multiline(self, panel):
        submitted = []
        panel.request_submitted.connect(lambda request: submitted.append(request.text))
        panel._request_edit.setPlainText("第一行")
        panel._request_edit.submitted.emit()
        assert submitted == ["第一行"]
        assert panel._request_edit.toPlainText() == ""

        panel._request_edit.setPlainText("第一行")
        panel._request_edit.insertPlainText("\n第二行")
        assert "第二行" in panel._request_edit.toPlainText()

    def test_refresh_keeps_dynamic_agent_cards(self, panel):
        from src.features.edit.ai_editing.assistant_events import (
            AssistantMessage,
            AssistantStateSnapshot,
        )

        panel.set_agent_notice("分析已完成")
        panel.refresh_state(AssistantStateSnapshot(messages=(AssistantMessage(
            message_id="m1", role="user", text="继续处理",
        ),)))
        from PyQt6.QtWidgets import QLabel
        labels = [label.text() for label in panel.findChildren(QLabel)]
        assert any("分析已完成" in text for text in labels)
        assert not panel._messages_area.isHidden()


class TestPanelGracefulDegration:
    """composer/drawer 装配失败时面板不崩（_build_ui 内已 try/except）。"""

    def test_setters_safe_when_footer_none(self):
        # 直接构造面板后强制 footer None 验证 setter 静默跳过
        p = AiEditAssistantPanel()
        p._composer_footer = None
        p._run_drawer = None
        # 不应抛错
        p.set_composer_footer(ComposerFooterState())
        p.set_run_drawer(RunDrawerState())
        p.deleteLater()
