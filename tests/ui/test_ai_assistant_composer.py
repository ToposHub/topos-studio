"""AiAssistantComposer + AiRunDrawer UI 测试（plan §10.2 P0.3 / P0.5 / P0.6）。

P0 阶段（plan §14 step 5）：组件先用 fake 数据（ComposerFooterState /
RunDrawerState）渲染，确认布局后再接真实 HarnessRuntime。

覆盖：

- Composer：scope/权限/profile/上下文/成本/网络/新对话/历史 都显示；
  权限切换发 permission_changed；运行中禁用新对话；网络提示可见性。
- RunDrawer：活动条目渲染（label/状态色/进度/范围/输出/耗时/错误码）；
  cancel/retry/diagnostics 按钮发对应 signal；计数标签插值正确。
- 防递归：set_state 期间 ``is_updating`` 为 True。

红线对照（plan §0.3 / §10.2）：

- 面板只发 signal，不执行模型 / 不写 timeline。
- 设置只作用于后续 invocation，不改写正在运行的 job。
- 用户能看到逐项任务状态（不能只看到"规划中"）。
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.edit_core

from src.features.edit.ui.ai_assistant_composer import (
    AiAssistantComposer,
    AiRunDrawer,
    ComposerFooterState,
    RunDrawerItem,
    RunDrawerState,
)
from PyQt6.QtWidgets import QLabel, QPushButton


@pytest.fixture
def composer(qapp):
    c = AiAssistantComposer()
    yield c
    c.deleteLater()


@pytest.fixture
def drawer(qapp):
    d = AiRunDrawer()
    yield d
    d.deleteLater()


# ================================================================
# Composer
# ================================================================


class TestComposerRender:
    def test_scope_shows_label_and_count(self, composer):
        composer.set_state(ComposerFooterState(
            scope_label="当前选择", scope_asset_count=3,
        ))
        txt = composer._scope_btn.text()
        assert "当前选择" in txt
        assert "3" in txt

    def test_profile_and_model_displayed(self, composer):
        composer.set_state(ComposerFooterState(
            profile_label="本地 Qwen", model_label="qwen3-asr",
        ))
        txt = composer._profile_btn.text()
        assert "本地 Qwen" in txt
        assert "qwen3-asr" in txt

    def test_profile_empty_shows_placeholder(self, composer):
        composer.set_state(ComposerFooterState(profile_label="", model_label=""))
        assert "未配置" in composer._profile_btn.text()

    def test_profile_name_equal_model_is_not_repeated(self, composer):
        composer.set_state(ComposerFooterState(
            profile_label="DeepSeek-v4-flash",
            model_label="deepseek-v4-flash",
        ))
        assert composer._profile_btn.text() == "DeepSeek-v4-flash · 中 ⌄"

    def test_profile_and_reasoning_share_one_compact_selector(self, composer):
        composer.set_state(ComposerFooterState(
            profile_label="Local",
            model_label="qwen3",
            reasoning_effort="high",
        ))
        assert composer._profile_btn.text() == "Local · qwen3 · 高 ⌄"
        menus = [action.menu() for action in composer._profile_reasoning_menu.actions()]
        assert [menu.title() for menu in menus if menu is not None] == ["模型", "推理强度"]

    def test_conversation_mode_and_permission_share_one_selector(self, composer):
        composer.set_state(ComposerFooterState(
            mode="video", permission="read_only",
        ))
        assert "可提议" not in composer._conversation_btn.text()
        assert "仅建议" in composer._conversation_btn.text()
        assert "视频" in composer._conversation_btn.text()
        menus = [action.menu() for action in composer._conversation_menu.actions()]
        assert [menu.title() for menu in menus if menu is not None] == ["模式", "权限"]

    def test_profile_menu_lists_configured_profiles(self, composer):
        selected = []
        composer.profile_changed.connect(selected.append)
        composer.set_state(ComposerFooterState(
            profile_label="DeepSeek", model_label="deepseek-chat",
            active_profile_id="p1",
            profile_options=(
                ("p1", "DeepSeek", "deepseek-chat"),
                ("p2", "Local", "llama3.2"),
            ),
        ))
        assert composer._profile_menu.actions()[1].text() == "Local · llama3.2"
        composer._profile_menu.actions()[1].trigger()
        assert selected == ["p2"]

    def test_reasoning_effort_selection_emits(self, composer):
        selected = []
        composer.reasoning_changed.connect(selected.append)
        idx = composer._reasoning_combo.findData("high")
        composer._reasoning_combo.setCurrentIndex(idx)
        assert selected == ["high"]

    def test_mode_selection_emits_stable_route_id(self, composer):
        selected = []
        composer.mode_changed.connect(selected.append)
        idx = composer._mode_combo.findData("video")
        composer._mode_combo.setCurrentIndex(idx)
        assert selected == ["video"]

    def test_mode_is_restored_without_emitting(self, composer):
        selected = []
        composer.mode_changed.connect(selected.append)
        composer.set_state(ComposerFooterState(mode="music"))
        assert composer._mode_combo.currentData() == "music"
        assert selected == []

    def test_context_used_limit_displayed(self, composer):
        composer.set_state(ComposerFooterState(
            context_used=8400, context_limit=32768, context_compressed=False,
        ))
        txt = composer._context_label.text()
        assert "8.2k" in txt
        assert "32k" in txt
        assert "已压缩" not in txt

    def test_context_compressed_marker(self, composer):
        composer.set_state(ComposerFooterState(
            context_used=5000, context_limit=32768, context_compressed=True,
        ))
        assert "已压缩" in composer._context_label.text()

    def test_cost_displayed(self, composer):
        composer.set_state(ComposerFooterState(cost_estimate="约 ¥0.12"))
        assert composer._cost_label.text() == "约 ¥0.12"

    def test_network_hint_visible_when_set(self, composer):
        composer.set_state(ComposerFooterState(network_hint="将上传 2 个素材到云"))
        assert composer._network_label.isHidden() is False
        assert "将上传" in composer._network_label.text()

    def test_network_hint_hidden_when_empty(self, composer):
        composer.set_state(ComposerFooterState(network_hint=""))
        assert composer._network_label.isHidden() is True


class TestComposerSignals:
    def test_add_button_exposes_codex_style_menu(self, composer):
        # The menu is shown explicitly so Qt does not add a dropdown arrow to
        # the compact plus button.
        assert composer._add_btn.menu() is None
        action_ids = [
            action.data()
            for action in composer._add_menu.actions()
            if not action.isSeparator()
        ]
        assert action_ids == [
            "image_files",
            "file_attachments",
            "clipboard_image",
            "current_frame",
            "scope",
            "context",
            "compress",
            "new_session",
            "history",
            "models",
        ]

    def test_add_menu_emits_real_action_id(self, composer):
        received = []
        composer.add_action_requested.connect(received.append)
        action = next(
            action for action in composer._add_menu.actions()
            if action.data() == "scope"
        )
        action.trigger()
        assert received == ["scope"]

    def test_permission_change_emits_signal(self, composer):
        received = []
        composer.permission_changed.connect(lambda p: received.append(p))
        # 切到 read_only（index 0）
        composer._perm_combo.setCurrentIndex(0)
        assert received == ["read_only"]

    def test_permission_does_not_emit_during_update(self, composer):
        """set_state 期间切换 combo 不应发 signal（防递归）。"""
        received = []
        composer.permission_changed.connect(lambda p: received.append(p))
        composer.set_state(ComposerFooterState(permission="apply_with_each_approval"))
        assert received == []  # set_state 内 blockSignals

    def test_scope_button_emits(self, composer):
        fired = []
        composer.scope_clicked.connect(lambda: fired.append(True))
        composer._scope_btn.click()
        assert fired == [True]

    def test_profile_button_emits(self, composer):
        fired = []
        composer.profile_clicked.connect(lambda: fired.append(True))
        composer._profile_btn.click()
        assert fired == [True]

    def test_new_session_emits(self, composer):
        fired = []
        composer.new_session_clicked.connect(lambda: fired.append(True))
        composer._new_session_btn.click()
        assert fired == [True]

    def test_history_emits(self, composer):
        fired = []
        composer.history_clicked.connect(lambda: fired.append(True))
        composer._history_btn.click()
        assert fired == [True]


class TestComposerActiveRunGuards:
    def test_new_session_disabled_when_run_active(self, composer):
        composer.set_state(ComposerFooterState(has_active_run=True))
        assert composer._new_session_btn.isEnabled() is False

    def test_new_session_enabled_when_idle(self, composer):
        composer.set_state(ComposerFooterState(has_active_run=False))
        assert composer._new_session_btn.isEnabled() is True


# ================================================================
# Run Drawer
# ================================================================


class TestRunDrawerRender:
    def test_empty_state_shown_when_no_items(self, drawer):
        drawer.set_state(RunDrawerState())
        assert drawer._empty_label.isHidden() is False
        assert drawer._items_host.isHidden() is True

    def test_items_rendered(self, drawer):
        drawer.set_state(RunDrawerState(items=[
            RunDrawerItem(item_id="i1", label="转录", status="running", progress=0.5),
            RunDrawerItem(item_id="i2", label="字幕", status="succeeded"),
        ]))
        # items_layout count = 2 cards + 1 stretch
        assert drawer._items_layout.count() == 3
        assert drawer._empty_label.isHidden() is True

    def test_count_label_interpolates_n(self, drawer):
        drawer.set_state(RunDrawerState(active_count=2, completed_count=3, failed_count=1))
        txt = drawer._count_label.text()
        assert "运行 2" in txt
        assert "完成 3" in txt
        assert "失败 1" in txt

    def test_running_item_shows_progress(self, drawer):
        drawer.set_state(RunDrawerState(items=[
            RunDrawerItem(item_id="i1", label="转录", status="running", progress=0.6),
        ]))
        from PyQt6.QtWidgets import QProgressBar
        card = drawer._items_layout.itemAt(0).widget()
        bars = card.findChildren(QProgressBar)
        assert len(bars) == 1
        assert bars[0].value() == 60

    def test_failed_item_shows_error_code(self, drawer):
        drawer.set_state(RunDrawerState(items=[
            RunDrawerItem(item_id="i1", label="OCR", status="failed",
                          error_code="MODEL_MISSING"),
        ]))
        card = drawer._items_layout.itemAt(0).widget()
        labels_text = " ".join(l.text() for l in card.findChildren(type(card)) if hasattr(l, "text"))
        # 错误码渲染在某 label 里
        all_text = card.findChildren(type(card))
        found = any("MODEL_MISSING" in (lbl.text() if hasattr(lbl, "text") else "") for lbl in all_text)
        assert found

    def test_compact_mode_shows_latest_activity_and_toggles_details(self, qapp):
        drawer = AiRunDrawer(compact=True)
        drawer.set_state(RunDrawerState(items=[
            RunDrawerItem(item_id="i1", label="探索 · 4 个文件", status="running"),
        ]))
        assert "探索 · 4 个文件" in drawer._title.text()
        assert drawer._expanded is False
        assert drawer._items_host.isHidden() is True

        drawer._toggle_btn.click()
        assert drawer._expanded is True
        assert drawer._items_host.isHidden() is False
        drawer.deleteLater()

    def test_compact_completed_activity_moves_meta_to_header(self, qapp):
        drawer = AiRunDrawer(compact=True)
        drawer.set_state(RunDrawerState(items=[
            RunDrawerItem(
                item_id="run-1", label="已完成", status="succeeded",
                output_summary="1757 token", duration_ms=125000,
            ),
            RunDrawerItem(
                item_id="run-1:tool:read", label="探索 · read", status="succeeded",
                kind="node", parent_id="run-1",
            ),
        ]))
        drawer._toggle_btn.click()

        assert "已完成" in drawer._title.text()
        assert "1757 token" in drawer._title.text()
        assert "2分5秒" in drawer._title.text()
        # The terminal main run is already summarized in the header.
        assert drawer._items_layout.count() == 2
        assert "read" in drawer._items_layout.itemAt(0).widget().findChildren(
            QLabel,
        )[1].text()
        drawer.deleteLater()


class TestRunDrawerSignals:
    def test_cancel_button_emits_item_id(self, drawer):
        cancelled = []
        drawer.cancel_clicked.connect(lambda iid: cancelled.append(iid))
        drawer.set_state(RunDrawerState(items=[
            RunDrawerItem(item_id="run-asr", label="转录", status="running", can_cancel=True),
        ]))
        card = drawer._items_layout.itemAt(0).widget()
        cancel_btn = card.findChildren(QPushButton)[0]
        cancel_btn.click()
        assert cancelled == ["run-asr"]

    def test_retry_button_emits_item_id(self, drawer):
        retried = []
        drawer.retry_clicked.connect(lambda iid: retried.append(iid))
        drawer.set_state(RunDrawerState(items=[
            RunDrawerItem(item_id="run-ocr", label="OCR", status="failed", can_retry=True),
        ]))
        card = drawer._items_layout.itemAt(0).widget()
        retry_btn = card.findChildren(QPushButton)[0]
        retry_btn.click()
        assert retried == ["run-ocr"]

    def test_diagnostics_button_emits(self, drawer):
        viewed = []
        drawer.diagnostics_clicked.connect(lambda iid: viewed.append(iid))
        drawer.set_state(RunDrawerState(items=[
            RunDrawerItem(item_id="r1", label="字幕", status="succeeded",
                          can_view_diagnostics=True),
        ]))
        card = drawer._items_layout.itemAt(0).widget()
        diag_btn = card.findChildren(QPushButton)[0]
        diag_btn.click()
        assert viewed == ["r1"]

    def test_no_buttons_when_all_disabled(self, drawer):
        drawer.set_state(RunDrawerState(items=[
            RunDrawerItem(item_id="i1", label="x", status="succeeded"),  # 全 False
        ]))
        card = drawer._items_layout.itemAt(0).widget()
        assert card.findChildren(QPushButton) == []
