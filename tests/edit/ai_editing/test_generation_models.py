"""M11 生成媒体数据合同 单元测试（§8.2 / §10.1 / §11.1 M11 / §1.2 #6）。

覆盖：
- GenerationModality 四类（video/music/voice/image）
- LicenseStatus + commercial_ready gate（许可未知不能商用就绪）
- ContentSafetyResult + blocked gate
- GenerationConsent is_complete（四项 acknowledge）
- GenerationBudget 硬上限
- GenerationProvenance 不记 secret/路径（只 hash + asset_id）
- GenerationRequest 校验（request_id/prompt 非空）
- GeneratedAsset 状态机 + importable/is_commercial_ready gating
- GenerationResult succeeded

红线对照：
- 许可未知不能表述为"可商用"（§1.2 #6 / §11.2 M11 step4）
- 内容安全阻断不可导入（§8.2）
- 不记 secret / 绝对路径（§0 第四条）
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.edit_core

from src.features.edit.ai_editing.generation_models import (
    ContentSafetyResult,
    GeneratedAsset,
    GeneratedAssetState,
    GenerationBudget,
    GenerationConsent,
    GenerationModality,
    GenerationProvenance,
    GenerationRequest,
    GenerationResult,
    LicenseStatus,
)


# ================================================================
# GenerationModality
# ================================================================


class TestModality:
    def test_four_modalities(self):
        mods = {m.value for m in GenerationModality}
        assert mods == {"video", "music", "voice", "image"}


# ================================================================
# LicenseStatus
# ================================================================


class TestLicenseStatus:
    def test_licensed_commercial_ready(self):
        assert LicenseStatus.KNOWN_LICENSED.commercial_ready is True
        assert LicenseStatus.CLEARED.commercial_ready is True

    def test_unknown_not_commercial_ready(self):
        """红线（§1.2 #6）：许可未知不能表述为可商用。"""
        assert LicenseStatus.UNKNOWN.commercial_ready is False
        assert LicenseStatus.PENDING.commercial_ready is False


# ================================================================
# ContentSafetyResult
# ================================================================


class TestContentSafety:
    def test_default_passed(self):
        s = ContentSafetyResult()
        assert s.passed is True
        assert s.blocked is False

    def test_blocked_when_not_passed(self):
        s = ContentSafetyResult(passed=False, categories=("nsfw",))
        assert s.blocked is True


# ================================================================
# GenerationConsent
# ================================================================


def _full_consent(**kw) -> GenerationConsent:
    base = dict(
        confirmed=True, provider_id="stub",
        cost_acknowledged=True, upload_acknowledged=True,
        copyright_acknowledged=True, target_location="generation_bin",
    )
    base.update(kw)
    return GenerationConsent(**base)


class TestConsent:
    def test_complete_when_all_acknowledged(self):
        assert _full_consent().is_complete is True

    def test_incomplete_when_missing_any(self):
        """§0 红线第一条：未完整确认 → gate 未过。"""
        for missing in ("confirmed", "cost_acknowledged",
                        "upload_acknowledged", "copyright_acknowledged"):
            kw = {missing: False}
            assert _full_consent(**kw).is_complete is False


# ================================================================
# GenerationBudget
# ================================================================


class TestBudget:
    def test_defaults(self):
        b = GenerationBudget()
        assert b.max_cost_units is None
        assert b.max_candidates == 4

    def test_custom(self):
        b = GenerationBudget(max_cost_units=1.5, max_candidates=2)
        assert b.max_cost_units == 1.5
        assert b.max_candidates == 2


# ================================================================
# GenerationProvenance
# ================================================================


class TestProvenance:
    def test_no_secret_or_path(self):
        """红线（§0 第四条）：provenance 只记 hash/asset_id，不记 secret/路径。"""
        p = GenerationProvenance(
            provider_id="stub", model_id="stub-video",
            input_prompt_hash="abc123",
            reference_asset_ids=("a1", "a2"),
        )
        d = p.to_dict()
        assert "input_prompt_hash" in d
        assert d["input_prompt_hash"] == "abc123"  # hash，非全文
        assert d["reference_asset_ids"] == ["a1", "a2"]  # id，非路径
        # 不应有任何 key/path/secret 字段
        for key in ("api_key", "token", "path", "absolute_path", "secret"):
            assert key not in d


# ================================================================
# GenerationRequest
# ================================================================


class TestRequest:
    def test_valid_request(self):
        r = GenerationRequest(
            request_id="req1", modality=GenerationModality.VIDEO,
            prompt="产品特写", consent=_full_consent(),
        )
        assert r.modality == GenerationModality.VIDEO
        assert r.budget.max_candidates == 4

    def test_empty_request_id_rejected(self):
        with pytest.raises(ValueError):
            GenerationRequest(
                request_id="", modality=GenerationModality.IMAGE,
                prompt="x", consent=_full_consent(),
            )

    def test_empty_prompt_rejected(self):
        with pytest.raises(ValueError):
            GenerationRequest(
                request_id="r1", modality=GenerationModality.IMAGE,
                prompt="", consent=_full_consent(),
            )

    def test_to_dict_json_safe(self):
        import json
        r = GenerationRequest(
            request_id="r1", modality=GenerationModality.VOICE,
            prompt="旁白", consent=_full_consent(),
        )
        json.dumps(r.to_dict())  # 不抛

    def test_to_dict_redacts_prompt_by_default(self):
        """红线修复（§0 第四条 / M12）：默认不采集 prompt 原文。

        to_dict() 默认输出 prompt_hash 摘要而非原文，对齐 GenerationProvenance
        的 input_prompt_hash 模式 + provider_profiles.DataPolicy.retain_prompts 开关。
        """
        import hashlib
        r = GenerationRequest(
            request_id="r1", modality=GenerationModality.VIDEO,
            prompt="产品特写", negative_prompt="模糊",
            consent=_full_consent(),
        )
        d = r.to_dict()
        # prompt 原文不在序列化结果
        assert d["prompt"] == hashlib.sha256("产品特写".encode("utf-8")).hexdigest()[:16]
        assert "产品特写" not in repr(d)
        assert d["prompt_redacted"] is True
        # negative_prompt 同理（非空时也输出 hash）
        assert d["negative_prompt"] == hashlib.sha256("模糊".encode("utf-8")).hexdigest()[:16]
        assert "模糊" not in repr(d)

    def test_to_dict_retain_prompts_outputs_plaintext(self):
        """retain_prompts=True 时输出原文（仅在受信任、显式开启持久化场景）。"""
        r = GenerationRequest(
            request_id="r1", modality=GenerationModality.VIDEO,
            prompt="产品特写", negative_prompt="模糊",
            consent=_full_consent(),
        )
        d = r.to_dict(retain_prompts=True)
        assert d["prompt"] == "产品特写"
        assert d["negative_prompt"] == "模糊"
        assert d["prompt_redacted"] is False

    def test_to_dict_empty_negative_prompt_stays_empty(self):
        """negative_prompt 为空时 hash 也为空（不产假 hash）。"""
        r = GenerationRequest(
            request_id="r1", modality=GenerationModality.VIDEO,
            prompt="x", consent=_full_consent(),
        )
        assert r.to_dict()["negative_prompt"] == ""

    def test_prompt_hash_matches_provenance_pattern(self):
        """request.prompt_hash 与 GenerationProvenance.input_prompt_hash 同算法（sha256[:16]）。"""
        r = GenerationRequest(
            request_id="r1", modality=GenerationModality.VOICE,
            prompt="旁白", consent=_full_consent(),
        )
        assert r.prompt_hash == r.to_dict()["prompt"]


# ================================================================
# GeneratedAsset 状态机 + gating
# ================================================================


def _make_asset(**kw) -> GeneratedAsset:
    base = dict(
        asset_id="gen-1", request_id="req1",
        modality=GenerationModality.VIDEO,
        provenance=GenerationProvenance(provider_id="stub", model_id="stub-v"),
    )
    base.update(kw)
    return GeneratedAsset(**base)


class TestGeneratedAsset:
    def test_default_state_generated(self):
        a = _make_asset()
        assert a.state == GeneratedAssetState.GENERATED
        assert a.license == LicenseStatus.UNKNOWN

    def test_importable_default(self):
        a = _make_asset()
        assert a.importable is True  # safety passed + state=GENERATED

    def test_not_importable_when_safety_blocked(self):
        a = _make_asset(safety=ContentSafetyResult(passed=False, categories=("nsfw",)))
        assert a.importable is False

    def test_not_importable_when_already_imported(self):
        a = _make_asset(state=GeneratedAssetState.IMPORTED)
        assert a.importable is False

    def test_not_importable_when_discarded(self):
        a = _make_asset(state=GeneratedAssetState.DISCARDED)
        assert a.importable is False

    def test_commercial_ready_requires_license_and_safety(self):
        a = _make_asset(license=LicenseStatus.KNOWN_LICENSED)
        assert a.is_commercial_ready is True
        a2 = _make_asset(license=LicenseStatus.KNOWN_LICENSED,
                         safety=ContentSafetyResult(passed=False))
        assert a2.is_commercial_ready is False

    def test_unknown_license_not_commercial_ready(self):
        a = _make_asset()  # 默认 UNKNOWN
        assert a.is_commercial_ready is False

    def test_with_state_immutable_transition(self):
        a = _make_asset()
        b = a.with_state(GeneratedAssetState.PREVIEWED)
        assert a.state == GeneratedAssetState.GENERATED  # 原实例不变
        assert b.state == GeneratedAssetState.PREVIEWED

    def test_frozen(self):
        a = _make_asset()
        with pytest.raises(Exception):
            a.asset_id = "changed"  # type: ignore[misc]


# ================================================================
# GeneratedAssetState
# ================================================================


class TestAssetState:
    def test_terminal_states(self):
        assert GeneratedAssetState.IMPORTED.terminal is True
        assert GeneratedAssetState.DISCARDED.terminal is True
        assert GeneratedAssetState.GENERATED.terminal is False
        assert GeneratedAssetState.PREVIEWED.terminal is False


# ================================================================
# GenerationResult
# ================================================================


class TestGenerationResult:
    def test_succeeded_with_candidates(self):
        r = GenerationResult(
            request_id="r1",
            candidates=(_make_asset(asset_id="c1"),),
            actual_cost_units=0.5,
        )
        assert r.succeeded is True

    def test_failed_with_error_code(self):
        r = GenerationResult(request_id="r1", error_code="PROVIDER_DOWN")
        assert r.succeeded is False

    def test_empty_candidates_not_succeeded(self):
        r = GenerationResult(request_id="r1")
        assert r.succeeded is False
