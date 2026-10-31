"""result_card 单元测试（plan §10.3 P1.5）。

覆盖：

- default_actions：apply 仅 approved 后启用（红线：不把未审批 proposal 显示成
  已应用）；不可信结果禁用直接 apply；retry / view_evidence / adjust 等可用性。
- 各工厂构造器（variant / subtitle_draft / evidence / generated_media / qa / error）。
- trust_label / status_label 文案。

红线（plan §10.3 P1.5 / §1.3）：

- 不把未审批 proposal 显示成已应用。
- 不将 OCR/网页不可信文本当作字幕事实（trust=untrusted 标注 + 禁 apply）。
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.edit_core

from src.features.ai_harness.result_card import (
    default_actions,
    error_result,
    evidence_result,
    generated_media_result,
    qa_result,
    subtitle_draft_result,
    variant_result,
    ResultEvidenceRef,
)


def _action_kinds(state) -> set[str]:
    return {a.kind for a in state.available_actions}


def _action(state, kind):
    return next(a for a in state.available_actions if a.kind == kind)


# ================================================================
# default_actions —— 红线核心
# ================================================================


class TestDefaultActions:
    def test_apply_disabled_when_not_approved(self):
        """红线：未审批 → apply 禁用。"""
        actions = default_actions("variant", approved=False, applied=False)
        apply = next(a for a in actions if a.kind == "apply")
        assert apply.enabled is False
        assert "审批" in apply.disabled_reason

    def test_apply_enabled_when_approved(self):
        actions = default_actions("variant", approved=True, applied=False)
        apply = next(a for a in actions if a.kind == "apply")
        assert apply.enabled is True

    def test_apply_disabled_when_applied(self):
        actions = default_actions("variant", approved=True, applied=True)
        apply = next(a for a in actions if a.kind == "apply")
        assert apply.enabled is False

    def test_untrusted_blocks_apply(self):
        """不可信结果禁用直接 apply（需人工核对）。"""
        actions = default_actions(
            "variant", approved=True, applied=False, trust="untrusted",
        )
        apply = next(a for a in actions if a.kind == "apply")
        assert apply.enabled is False
        assert "核对" in apply.disabled_reason

    def test_retry_always_available_until_applied(self):
        actions = default_actions("variant", applied=False)
        retry = next(a for a in actions if a.kind == "retry")
        assert retry.enabled is True
        actions2 = default_actions("variant", applied=True)
        retry2 = next(a for a in actions2 if a.kind == "retry")
        assert retry2.enabled is False

    def test_view_evidence_only_when_has_evidence(self):
        actions = default_actions("variant", has_evidence=True)
        assert any(a.kind == "view_evidence" for a in actions)
        actions2 = default_actions("variant", has_evidence=False)
        assert not any(a.kind == "view_evidence" for a in actions2)

    def test_error_only_has_retry_and_diagnostics(self):
        actions = default_actions("error", has_diagnostics=True)
        kinds = {a.kind for a in actions}
        assert "retry" in kinds
        assert "view_diagnostics" in kinds
        assert "apply" not in kinds
        assert "approve" not in kinds

    def test_subtitle_draft_has_open_in_editor(self):
        actions = default_actions("subtitle_draft")
        assert any(a.kind == "open_in_editor" for a in actions)

    def test_generated_media_has_locate_in_pool(self):
        actions = default_actions("generated_media")
        assert any(a.kind == "locate_in_pool" for a in actions)

    def test_adjust_params_disabled_when_applied(self):
        actions = default_actions("variant", applied=True)
        adjust = next(a for a in actions if a.kind == "adjust_params")
        assert adjust.enabled is False


# ================================================================
# 工厂构造器
# ================================================================


class TestVariantResult:
    def test_proposed_status(self):
        r = variant_result("r1", "口播精剪 A", "30 秒")
        assert r.status_label == "提议（未审批）"
        assert r.trust == "trusted"

    def test_approved_status(self):
        r = variant_result("r1", "A", "x", approved=True)
        assert r.status_label == "已审批（待应用）"

    def test_applied_status(self):
        r = variant_result("r1", "A", "x", approved=True, applied=True)
        assert r.status_label == "已应用"


class TestSubtitleDraftResult:
    def test_default_trust_unverified(self):
        """字幕草稿默认待核（不当作事实）。"""
        r = subtitle_draft_result("r1", "字幕草稿", "3 段")
        assert r.trust == "unverified"
        assert r.trust_label == "待核"

    def test_ocr_untrusted(self):
        """OCR 来源字幕标 untrusted（红线：不可信文本不当字幕事实）。"""
        r = subtitle_draft_result("r1", "OCR 字幕", "x", trust="untrusted")
        assert r.trust == "untrusted"
        # untrusted 即使 approved，apply 也禁用
        apply = next(a for a in r.available_actions if a.kind == "apply")
        assert apply.enabled is False


class TestEvidenceResult:
    def test_trusted_by_default(self):
        r = evidence_result("r1", "转录", "1:23")
        assert r.trust == "trusted"


class TestGeneratedMediaResult:
    def test_default_trust_unverified(self):
        r = generated_media_result("r1", "TTS 配音", "30 秒")
        assert r.trust == "unverified"

    def test_locate_in_pool_available(self):
        r = generated_media_result("r1", "TTS", "x")
        assert "locate_in_pool" in _action_kinds(r)

    def test_place_on_timeline_requires_explicit_action(self):
        r = generated_media_result("r1", "TTS", "x")
        assert "place_on_timeline" in _action_kinds(r)


class TestQaResult:
    def test_has_diagnostics_action_when_diagnostics(self):
        r = qa_result("r1", "QA", "通过", diagnostics=({"code": "x"},))
        assert "view_diagnostics" in _action_kinds(r)


class TestErrorResult:
    def test_error_kind_and_code(self):
        r = error_result("r1", "失败", "MODEL_MISSING", "Qwen3-ASR 未安装",
                         diagnostics=({"code": "MODEL_MISSING"},))
        assert r.kind == "error"
        assert r.error_code == "MODEL_MISSING"
        assert r.status_label == "失败"
        assert "retry" in _action_kinds(r)
        assert "view_diagnostics" in _action_kinds(r)


# ================================================================
# evidence refs
# ================================================================


class TestEvidenceRefs:
    def test_view_evidence_available_with_refs(self):
        refs = (ResultEvidenceRef(evidence_id="e1", kind="transcript", summary="1:23"),)
        r = variant_result("r1", "A", "x", evidence_refs=refs)
        assert "view_evidence" in _action_kinds(r)
        assert r.evidence_refs[0].evidence_id == "e1"
