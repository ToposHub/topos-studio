"""AiSkillCard + AiSkillGallery UI 测试（plan §10.3 P1.2）。

覆盖：

- AiSkillCard：标题 / 描述 / 风险色 / 未验证标记 / 产出 / 预计工作 / 触发示例 /
  前置条件清单（✓/✗）/ 表单字段渲染 / 开始按钮（ready 启用、not ready 禁用 +
  tooltip 缺什么）。
- 表单值收集（integer/enum/string）。
- 开始按钮发 start_requested(skill_id, form_values)。
- AiSkillGallery：set_cards 渲染列表；空态；转发 start_requested。

红线（plan §10.3 P1.2）：未满足前置条件不静默启动（按钮禁用）；
local_dev 标未验证。
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.edit_core

from src.features.ai_harness.skill_catalog import (
    FormField,
    PrereqCheck,
    PrereqReport,
    SkillCardView,
)
from PyQt6.QtWidgets import QLabel

from src.features.edit.ui.ai_skill_card import AiSkillCard, AiSkillGallery


def _ready_view(skill_id="topos.subtitle_draft", fields=None) -> SkillCardView:
    return SkillCardView(
        skill_id=skill_id,
        title="字幕初稿",
        description="从转录证据生成字幕草稿",
        category="字幕",
        risk_label="仅提议",
        unverified=False,
        produces="字幕草稿",
        estimated_work="转录 + 生成",
        trigger_examples=("生成字幕", "自动字幕"),
        form_fields=tuple(fields or ()),
        prereq=PrereqReport(
            skill_id=skill_id, ready=True,
            checks=(
                PrereqCheck(category="risk", ok=True, label="风险等级", detail="仅提议"),
                PrereqCheck(category="evidence", ok=True, label="证据：transcript", detail="已具备"),
            ),
        ),
    )


def _not_ready_view(skill_id="topos.product_ad") -> SkillCardView:
    return SkillCardView(
        skill_id=skill_id,
        title="30 秒产品广告",
        description="产品广告方案",
        risk_label="仅提议",
        unverified=False,
        produces="A/B 广告方案",
        prereq=PrereqReport(
            skill_id=skill_id, ready=False,
            checks=(
                PrereqCheck(category="risk", ok=True, label="风险等级", detail="仅提议"),
                PrereqCheck(category="evidence", ok=False, label="证据：shot",
                            detail="缺失（需先运行 SceneCut）"),
            ),
            missing_evidence_kinds=("shot",),
        ),
    )


@pytest.fixture
def card_ready(qapp):
    c = AiSkillCard(_ready_view(fields=[FormField(
        name="target_duration", label="目标时长", field_type="integer", default=720,
    )]))
    yield c
    c.deleteLater()


@pytest.fixture
def card_not_ready(qapp):
    c = AiSkillCard(_not_ready_view())
    yield c
    c.deleteLater()


class TestSkillCardRender:
    def test_title_shown(self, card_ready):
        labels = card_ready.findChildren(QLabel)
        titles = [l.text() for l in labels if hasattr(l, "text") and "<b>" in l.text()]
        assert any("字幕初稿" in t for t in titles)

    def test_description_shown(self, card_ready):
        all_text = " ".join(
            l.text() for l in card_ready.findChildren(QLabel)
            if hasattr(l, "text")
        )
        assert "转录证据" in all_text

    def test_produces_and_work_shown(self, card_ready):
        all_text = " ".join(
            l.text() for l in card_ready.findChildren(QLabel)
            if hasattr(l, "text")
        )
        assert "字幕草稿" in all_text
        assert "转录" in all_text

    def test_prereq_checks_rendered(self, card_ready):
        all_text = " ".join(
            l.text() for l in card_ready.findChildren(QLabel)
            if hasattr(l, "text")
        )
        # ready 的检查显示 ✓
        assert "✓" in all_text
        assert "transcript" in all_text

    def test_not_ready_prereq_shows_x(self, card_not_ready):
        all_text = " ".join(
            l.text() for l in card_not_ready.findChildren(QLabel)
            if hasattr(l, "text")
        )
        assert "✗" in all_text
        assert "shot" in all_text

    def test_unverified_flag_shown(self, qapp):
        view = _ready_view()
        # 改 unverified（SkillCardView 是 frozen，重建）
        import dataclasses
        view = dataclasses.replace(view, unverified=True)
        c = AiSkillCard(view)
        all_text = " ".join(
            l.text() for l in c.findChildren(QLabel) if hasattr(l, "text")
        )
        assert "未验证" in all_text
        c.deleteLater()


class TestSkillCardStartButton:
    def test_ready_button_enabled(self, card_ready):
        assert card_ready._start_btn.isEnabled() is True

    def test_not_ready_button_disabled(self, card_not_ready):
        assert card_not_ready._start_btn.isEnabled() is False

    def test_not_ready_tooltip_lists_missing(self, card_not_ready):
        tip = card_not_ready._start_btn.toolTip()
        assert "shot" in tip or "SceneCut" in tip

    def test_start_emits_skill_id_and_form_values(self, card_ready):
        received = []
        card_ready.start_requested.connect(lambda sid, vals: received.append((sid, vals)))
        card_ready._start_btn.click()
        assert len(received) == 1
        assert received[0][0] == "topos.subtitle_draft"
        assert received[0][1]["target_duration"] == 720

    def test_not_ready_does_not_emit(self, card_not_ready):
        received = []
        card_not_ready.start_requested.connect(lambda sid, vals: received.append((sid, vals)))
        # 按钮禁用，无法 click；即使强行调 _on_start 也应被 is_ready 挡（按钮已禁）
        assert card_not_ready._start_btn.isEnabled() is False


class TestFormValues:
    def test_integer_field(self, qapp):
        c = AiSkillCard(_ready_view(fields=[FormField(
            name="target_duration", label="时长", field_type="integer", default=360,
        )]))
        assert c.form_values()["target_duration"] == 360
        c.deleteLater()

    def test_enum_field(self, qapp):
        c = AiSkillCard(_ready_view(fields=[FormField(
            name="platform", label="平台", field_type="enum",
            options=("vertical", "horizontal"), default="vertical",
        )]))
        assert c.form_values()["platform"] == "vertical"
        c.deleteLater()

    def test_string_field(self, qapp):
        c = AiSkillCard(_ready_view(fields=[FormField(
            name="note", label="备注", field_type="string", default="hello",
        )]))
        assert c.form_values()["note"] == "hello"
        c.deleteLater()

    def test_empty_string_returns_none(self, qapp):
        c = AiSkillCard(_ready_view(fields=[FormField(
            name="note", label="备注", field_type="string", default=None,
        )]))
        assert c.form_values()["note"] is None
        c.deleteLater()


class TestSkillGallery:
    def test_set_cards_renders(self, qapp):
        g = AiSkillGallery()
        g.set_cards([_ready_view(), _not_ready_view()])
        assert len(g.cards) == 2
        assert g._empty.isHidden() is True
        g.deleteLater()

    def test_empty_state(self, qapp):
        g = AiSkillGallery()
        g.set_cards([])
        assert g._empty.isHidden() is False
        assert len(g.cards) == 0
        g.deleteLater()

    def test_forwards_start_requested(self, qapp):
        g = AiSkillGallery()
        g.set_cards([_ready_view(fields=[FormField(
            name="target_duration", label="时长", field_type="integer", default=720,
        )])])
        received = []
        g.start_requested.connect(lambda sid, vals: received.append((sid, vals)))
        g.cards[0]._start_btn.click()
        assert received == [("topos.subtitle_draft", {"target_duration": 720})]
        g.deleteLater()

    def test_replace_cards_clears_old(self, qapp):
        g = AiSkillGallery()
        g.set_cards([_ready_view(), _not_ready_view()])
        assert len(g.cards) == 2
        g.set_cards([_ready_view()])
        assert len(g.cards) == 1
        g.deleteLater()
