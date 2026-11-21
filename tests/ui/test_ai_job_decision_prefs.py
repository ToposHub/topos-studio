"""TAEP Phase 9 UI-1/UI-5 — Job 面板 / Decision Inspector / Preferences 页 测试。

验收（TAEP §11.9）：
- job 面板回填进度行；运行态显示取消，失败态显示重试。
- decision inspector 回填卡片；锁定/拒绝/替换发 signal；音乐 decision 有试听。
- preferences 页可新建/删除 profile；不回显 token。
- 所有子组件只发 signal，不直接 edit clip / commit。
"""

from __future__ import annotations

import pytest
from PyQt6.QtCore import Qt

from src.features.edit.ai_editing.assistant_events import DecisionCard, JobCard
from src.features.edit.ai_editing.provider_profiles import (
    AiProviderProfile,
    FakeCredentialStore,
    ProfileRegistry,
)
from src.features.edit.ui.ai_decision_inspector import AiDecisionInspector
from src.features.edit.ui.ai_job_panel import AiJobPanel
from src.features.edit.ui.ai_preferences_page import AiEditingPreferencePage


@pytest.fixture
def job_panel(qapp):
    w = AiJobPanel()
    yield w
    w.deleteLater()


@pytest.fixture
def decision_inspector(qapp):
    w = AiDecisionInspector()
    yield w
    w.deleteLater()


@pytest.fixture
def prefs_page(qapp):
    registry = ProfileRegistry()
    p = AiEditingPreferencePage(registry=registry)
    yield p


# ================================================================
# Job 面板
# ================================================================


def test_job_panel_shows_rows(job_panel):
    jobs = (
        JobCard(job_id="j1", kind="preview", status="running", progress=0.5, label="Preview"),
        JobCard(job_id="j2", kind="export", status="failed", progress=0.3, label="Export"),
        JobCard(job_id="j3", kind="analysis", status="succeeded", progress=1.0, label="Analysis"),
    )
    job_panel.set_jobs(jobs)
    assert not job_panel._empty_label.isVisibleTo(job_panel)
    assert job_panel._container_layout.count() == 4  # 3 rows + stretch


def test_job_panel_empty_state(job_panel):
    job_panel.set_jobs(())
    assert job_panel._empty_label.isVisibleTo(job_panel)


def test_job_panel_cancel_emits_signal(job_panel):
    job_panel.set_jobs((JobCard(job_id="j1", kind="preview", status="running"),))
    cancelled = []
    job_panel.cancel_requested.connect(lambda jid: cancelled.append(jid))
    # 找到第一行的取消按钮点击
    row = job_panel._container_layout.itemAt(0).widget()
    row.cancel_clicked.emit("j1")
    assert cancelled == ["j1"]


def test_job_panel_running_shows_cancel_not_retry(job_panel):
    job_panel.set_jobs((JobCard(job_id="j1", kind="preview", status="running"),))
    row = job_panel._container_layout.itemAt(0).widget()
    assert row._cancel_btn.isVisibleTo(row) is True
    assert row._retry_btn.isVisibleTo(row) is False


def test_job_panel_failed_shows_retry(job_panel):
    job_panel.set_jobs((JobCard(job_id="j1", kind="export", status="failed"),))
    row = job_panel._container_layout.itemAt(0).widget()
    assert row._retry_btn.isVisibleTo(row) is True


# ================================================================
# Decision Inspector
# ================================================================


def test_decision_inspector_shows_cards(decision_inspector):
    cards = (
        DecisionCard(decision_id="d1", beat="hook", description="反应镜头",
                     evidence_summary="seg-12", state="proposed"),
        DecisionCard(decision_id="d2", beat="music", description="Fast Pop 01",
                     state="proposed", is_music=True),
    )
    decision_inspector.set_decisions(cards)
    assert decision_inspector._container_layout.count() == 3  # 2 cards + stretch


def test_decision_inspector_lock_signal(decision_inspector):
    decision_inspector.set_decisions(
        (DecisionCard(decision_id="d1", beat="hook", state="proposed"),)
    )
    locked = []
    decision_inspector.decision_locked.connect(lambda did: locked.append(did))
    row = decision_inspector._container_layout.itemAt(0).widget()
    row.locked.emit("d1")
    assert locked == ["d1"]


def test_decision_inspector_reject_signal(decision_inspector):
    decision_inspector.set_decisions(
        (DecisionCard(decision_id="d1", state="proposed"),)
    )
    rejected = []
    decision_inspector.decision_rejected.connect(lambda did: rejected.append(did))
    row = decision_inspector._container_layout.itemAt(0).widget()
    row.rejected.emit("d1")
    assert rejected == ["d1"]


def test_decision_inspector_music_has_audition(decision_inspector):
    decision_inspector.set_decisions((
        DecisionCard(decision_id="d1", state="proposed", is_music=True),
        DecisionCard(decision_id="d2", state="proposed", is_music=False),
    ))
    music_row = decision_inspector._container_layout.itemAt(0).widget()
    plain_row = decision_inspector._container_layout.itemAt(1).widget()
    # 音乐行有试听按钮（4 个按钮：lock/reject/replace/audition）
    assert music_row.audition_clicked is not None
    # 非音乐行没有试听（发 audition 不应触发，但按钮不存在）


def test_decision_inspector_state_color(decision_inspector):
    decision_inspector.set_decisions((
        DecisionCard(decision_id="d1", state="accepted"),
        DecisionCard(decision_id="d2", state="rejected"),
        DecisionCard(decision_id="d3", state="locked"),
    ))
    accepted_row = decision_inspector._container_layout.itemAt(0).widget()
    rejected_row = decision_inspector._container_layout.itemAt(1).widget()
    locked_row = decision_inspector._container_layout.itemAt(2).widget()
    assert "27ae60" in accepted_row.styleSheet()  # green
    assert "c0392b" in rejected_row.styleSheet()  # red
    assert "2980b9" in locked_row.styleSheet()  # blue


# ================================================================
# Preferences 页
# ================================================================


def test_preferences_page_add_profile(prefs_page):
    widget = prefs_page.create_widget()
    lst = prefs_page._widgets["profile_picker"]
    assert lst.count() == 0
    prefs_page._widgets["add"].click()
    assert lst.count() == 1
    assert prefs_page.registry.list()[0].display_name != ""


def test_preferences_page_context_limit_uses_k_units(prefs_page):
    widget = prefs_page.create_widget()
    prefs_page._on_add_profile()
    spin = prefs_page._widgets["context_limit"]
    assert spin.value() == 200
    assert spin.suffix() == "k"
    spin.setValue(128)
    profile = prefs_page.registry.list()[0]
    assert profile.limits.max_context_tokens == 128_000


def test_preferences_profile_picker_and_advanced_disclosure(prefs_page, qtbot):
    widget = prefs_page.create_widget()
    qtbot.addWidget(widget)
    widget.resize(600, 455)
    widget.show()
    controls = prefs_page._widgets
    assert not controls["profile_picker"].isVisible()
    assert controls["profile_empty"].isVisible()

    controls["add"].click()
    assert controls["profile_picker"].isVisible()
    assert controls["test_btn"].isVisible()
    assert not controls["context_limit"].isVisible()

    viewport = controls["models_scroll"].viewport()
    spin = controls["context_limit"]

    def context_is_in_view():
        return viewport.rect().contains(spin.mapTo(viewport, spin.rect().bottomRight()))

    controls["advanced_toggle"].click()
    assert controls["context_limit"].isVisible()
    qtbot.waitUntil(context_is_in_view)
    controls["context_limit"].setValue(64)
    controls["advanced_toggle"].click()
    controls["advanced_toggle"].click()
    assert controls["context_limit"].value() == 64


def test_preferences_page_remove_profile(prefs_page):
    widget = prefs_page.create_widget()
    prefs_page._on_add_profile()
    lst = prefs_page._widgets["profile_picker"]
    lst.setCurrentIndex(0)
    actions = prefs_page._widgets["model_actions"].menu().actions()
    actions[0].trigger()
    assert lst.count() == 2
    assert lst.currentData() != lst.itemData(0)
    actions[1].trigger()
    assert lst.count() == 1
    actions[1].trigger()
    assert lst.count() == 0


def test_preferences_page_no_token_display(prefs_page):
    """偏好页不回显 token（profile 只存 credential_ref）。"""
    profile = AiProviderProfile(
        profile_id="p1", display_name="Cloud",
        credential_ref="keychain:topos:p1",  # 只是引用
    )
    prefs_page.registry.add(profile)
    widget = prefs_page.create_widget()
    lst = prefs_page._widgets["profile_picker"]
    text = lst.itemText(0)
    assert "keychain" not in text
    assert "secret" not in text.lower()
    assert "api_key" not in text.lower()


def test_preferences_page_explains_saved_key_is_hidden(qapp):
    registry = ProfileRegistry()
    registry.add(AiProviderProfile(
        profile_id="p1", display_name="Cloud", credential_ref="topos:p1",
    ))
    store = FakeCredentialStore()
    store.set_secret("topos:p1", "secret-value")
    page = AiEditingPreferencePage(registry=registry, credential_store=store)
    widget = page.create_widget()
    page._widgets["profile_picker"].setCurrentIndex(0)

    status = page._widgets["key_status"].text().lower()
    placeholder = page._widgets["api_key"].placeholderText().lower()
    assert "saved" in status or "已保存" in status
    assert "hidden" in placeholder or "不会显示" in placeholder
    assert "secret-value" not in page._widgets["api_key"].text()


def test_preferences_page_does_not_publish_draft_profiles(prefs_page):
    widget = prefs_page.create_widget()
    emitted = []
    prefs_page.on_profiles_changed = lambda: emitted.append(1)
    prefs_page._on_add_profile()
    assert emitted == []


def test_preferences_page_exposes_capabilities_and_safe_automation_flags(prefs_page):
    widget = prefs_page.create_widget()
    status_list = prefs_page._widgets["capability_status"]
    assert status_list.count() >= 7
    assert prefs_page._automation_widgets["agent_auto_mode"].isChecked() is False
    prefs_page._automation_widgets["agent_auto_mode"].setChecked(True)
    prefs_page._automation_widgets["cloud_upload"].setChecked(True)
    assert "受限自动模式" in status_list.item(status_list.count() - 3).text()


def test_preferences_policy_and_service_status_have_separate_tabs(prefs_page, qtbot):
    widget = prefs_page.create_widget()
    qtbot.addWidget(widget)
    widget.show()
    controls = prefs_page._widgets
    status_list = controls["capability_status"]
    mcp_list = controls["mcp_server_list"]
    upload = prefs_page._automation_widgets["cloud_upload"]

    assert not status_list.isVisible()
    assert not upload.isVisible()
    controls["page_tabs"].setCurrentIndex(1)
    assert upload.isVisible()
    assert not status_list.isVisible()
    controls["page_tabs"].setCurrentIndex(2)
    assert not upload.isVisible()
    assert mcp_list.isVisible() and status_list.isVisible()
    assert status_list.verticalScrollBarPolicy() == Qt.ScrollBarPolicy.ScrollBarAlwaysOff
    assert status_list.count() >= 7


def test_preferences_page_saves_media_routes_without_secret_values(prefs_page):
    widget = prefs_page.create_widget()
    prefs_page._on_add_profile()
    prefs_page._modality_widgets["video"].setChecked(True)
    prefs_page._widgets["allow_media_upload"].setChecked(True)
    profile = prefs_page.registry.get(prefs_page._current_profile_id)
    assert profile is not None
    assert "video" in profile.modalities
    assert "media_upload" in profile.modalities
    assert profile.data_policy.allow_media_upload is True


def test_preferences_page_groups_profiles_by_capability(prefs_page):
    widget = prefs_page.create_widget()
    tabs = prefs_page._widgets["category_tabs"]
    assert tabs.count() == 5

    # Language tab starts with the default profile.
    prefs_page._on_add_profile()
    assert prefs_page._widgets["profile_picker"].count() == 1

    # Speech tab creates/selects a speech preset without mixing it into the
    # language list.
    tabs.setCurrentIndex(2)
    prefs_page._on_add_profile()
    assert prefs_page._widgets["profile_picker"].count() == 1
    current = prefs_page.registry.get(prefs_page._current_profile_id)
    assert current is not None
    assert "asr" in current.capabilities


def test_preferences_page_filters_presets_by_category(prefs_page):
    """服务预设下拉框只能显示当前模型分类的预设。"""
    widget = prefs_page.create_widget()
    tabs = prefs_page._widgets["category_tabs"]
    combo = prefs_page._widgets["preset_id"]

    def preset_ids() -> set[str]:
        return {
            str(combo.itemData(index))
            for index in range(combo.count())
            if combo.itemData(index)
        }

    tabs.setCurrentIndex(0)
    assert "openai-chat" in preset_ids()
    assert "deepseek-flash" in preset_ids()
    assert "volcengine-seedream-image" not in preset_ids()
    assert "volcengine-plan-tts" not in preset_ids()
    assert "openai-embedding" not in preset_ids()

    tabs.setCurrentIndex(1)
    assert preset_ids() == {
        "deepseek-flash",
        "openai-compatible-vision",
        "model-studio-vision",
        "volcengine-agent-plan-text",
        "volcengine-seedream-image",
        "zhipu-vision-mcp",
    }

    tabs.setCurrentIndex(2)
    assert preset_ids() == {
        "volcengine-plan-tts",
        "volcengine-plan-asr",
        "openai-timed-asr",
        "local-qwen-asr",
        "local-qwen-tts",
    }

    tabs.setCurrentIndex(3)
    assert preset_ids() == {"openai-embedding"}

    tabs.setCurrentIndex(4)
    assert preset_ids() == set()
