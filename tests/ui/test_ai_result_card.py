"""AiResultCard UI 测试（plan §10.3 P1.5）。

覆盖：

- 各 kind 渲染（variant/subtitle_draft/error/generated_media）。
- 未审批 apply 禁用 + tooltip；approved 后启用。
- 不可信结果（untrusted）显示警告徽标 + apply 禁用。
- applied 状态显示"已应用"。
- 按钮 emit action_triggered(result_id, action_kind)。
- 证据计数显示。

红线（plan §10.3 P1.5）：不把未审批 proposal 显示成已应用；不可信标注。
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.edit_core

from PyQt6.QtWidgets import QLabel, QPushButton

from src.features.ai_harness.result_card import (
    ResultEvidenceRef,
    default_actions,
    error_result,
    generated_media_result,
    subtitle_draft_result,
    variant_result,
)
from src.features.edit.ui.ai_result_card import AiResultCard


def _labels_text(card) -> str:
    return " ".join(l.text() for l in card.findChildren(QLabel))


def _buttons(card) -> list[QPushButton]:
    return card.findChildren(QPushButton)


def _btn(card, label: str) -> QPushButton:
    return next(b for b in _buttons(card) if b.text() == label)


@pytest.fixture
def variant_proposed(qapp):
    c = AiResultCard(variant_result(
        "r1", "口播精剪 A", "30 秒竖版",
        evidence_refs=(ResultEvidenceRef("e1", "transcript", "1:23"),),
    ))
    yield c
    c.deleteLater()


class TestVariantRender:
    def test_title_and_kind_shown(self, variant_proposed):
        txt = _labels_text(variant_proposed)
        assert "口播精剪 A" in txt
        assert "方案" in txt

    def test_proposed_status(self, variant_proposed):
        txt = _labels_text(variant_proposed)
        assert "提议（未审批）" in txt

    def test_evidence_count_shown(self, variant_proposed):
        txt = _labels_text(variant_proposed)
        assert "1" in txt and "证据" in txt

    def test_apply_disabled_when_proposed(self, variant_proposed):
        """红线：未审批 apply 禁用。"""
        apply_btn = _btn(variant_proposed, "应用")
        assert apply_btn.isEnabled() is False
        assert "审批" in apply_btn.toolTip()

    def test_approve_enabled_when_proposed(self, variant_proposed):
        approve_btn = _btn(variant_proposed, "审批")
        assert approve_btn.isEnabled() is True


class TestVariantApproved:
    def test_apply_enabled_when_approved(self, qapp):
        c = AiResultCard(variant_result(
            "r1", "A", "x", approved=True,
            evidence_refs=(ResultEvidenceRef("e1"),),
        ))
        assert _btn(c, "应用").isEnabled() is True
        c.deleteLater()

    def test_status_approved(self, qapp):
        c = AiResultCard(variant_result("r1", "A", "x", approved=True))
        assert "已审批" in _labels_text(c)
        c.deleteLater()


class TestVariantApplied:
    def test_status_applied(self, qapp):
        c = AiResultCard(variant_result(
            "r1", "A", "x", approved=True, applied=True,
        ))
        assert "已应用" in _labels_text(c)
        c.deleteLater()


class TestUntrustedResult:
    def test_warning_badge_shown(self, qapp):
        c = AiResultCard(variant_result(
            "r1", "OCR 字幕方案", "x", trust="untrusted", approved=True,
        ))
        txt = _labels_text(c)
        assert "不可信" in txt or "待核" in txt
        # 即使 approved，apply 仍禁用
        assert _btn(c, "应用").isEnabled() is False
        c.deleteLater()


class TestErrorRender:
    def test_error_kind_label(self, qapp):
        c = AiResultCard(error_result(
            "r1", "失败", "MODEL_MISSING", "Qwen3-ASR 未安装",
            diagnostics=({"code": "MODEL_MISSING"},),
        ))
        txt = _labels_text(c)
        assert "错误" in txt
        assert "MODEL_MISSING" in txt
        assert "失败" in txt
        c.deleteLater()

    def test_error_has_retry_and_diagnostics(self, qapp):
        c = AiResultCard(error_result(
            "r1", "失败", "X", "y", diagnostics=({"code": "x"},),
        ))
        labels = {b.text() for b in _buttons(c)}
        assert "重试" in labels
        assert "查看诊断" in labels
        # error 不应有 apply/approve
        assert "应用" not in labels
        assert "审批" not in labels
        c.deleteLater()


class TestSubtitleDraft:
    def test_unverified_trust_default(self, qapp):
        c = AiResultCard(subtitle_draft_result("r1", "字幕草稿", "3 段"))
        assert "待核" in _labels_text(c)
        c.deleteLater()

    def test_has_open_in_editor(self, qapp):
        c = AiResultCard(subtitle_draft_result("r1", "字幕", "x"))
        labels = {b.text() for b in _buttons(c)}
        assert "在字幕编辑器打开" in labels
        c.deleteLater()


class TestGeneratedMedia:
    def test_locate_in_pool(self, qapp):
        # P1-UI-02 规格裁决：locate_in_pool 动作文案随统一生成素材箱（e12b789c）
        # 改为"查看生成素材箱"；kind 不变，仍为 locate_in_pool。
        c = AiResultCard(generated_media_result("r1", "TTS 配音", "30 秒"))
        labels = {b.text() for b in _buttons(c)}
        assert "查看生成素材箱" in labels
        c.deleteLater()


class TestActionSignal:
    def test_action_emits_result_id_and_kind(self, qapp):
        c = AiResultCard(variant_result(
            "run-1", "A", "x",
            evidence_refs=(ResultEvidenceRef("e1"),),
        ))
        received = []
        c.action_triggered.connect(lambda rid, ak: received.append((rid, ak)))
        # 点"审批"（enabled）
        _btn(c, "审批").click()
        assert received == [("run-1", "approve")]
        c.deleteLater()

    def test_disabled_action_does_not_emit(self, qapp):
        c = AiResultCard(variant_result(
            "r1", "A", "x", evidence_refs=(ResultEvidenceRef("e1"),),
        ))
        received = []
        c.action_triggered.connect(lambda rid, ak: received.append((rid, ak)))
        apply_btn = _btn(c, "应用")
        # 按钮禁用，无法 click 触发
        assert apply_btn.isEnabled() is False
        c.deleteLater()

    def test_retry_action(self, qapp):
        c = AiResultCard(variant_result(
            "r2", "A", "x", evidence_refs=(ResultEvidenceRef("e1"),),
        ))
        received = []
        c.action_triggered.connect(lambda rid, ak: received.append((rid, ak)))
        _btn(c, "重试").click()
        assert received == [("r2", "retry")]
        c.deleteLater()

    def test_view_evidence_action(self, qapp):
        c = AiResultCard(variant_result(
            "r3", "A", "x", evidence_refs=(ResultEvidenceRef("e1"),),
        ))
        received = []
        c.action_triggered.connect(lambda rid, ak: received.append((rid, ak)))
        _btn(c, "查看依据").click()
        assert received == [("r3", "view_evidence")]
        c.deleteLater()
