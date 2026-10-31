"""M11 Generation Provider 单元测试（§8.2 / §3.1 / §11.1 M11）。

覆盖：
- ProviderRegistry register/get/preferred_for/set_preferred
- DeterministicGenerationProvider 确定性产候选（相同 prompt → 相同 asset_id）
- 确定性原则（便于 eval）
- 不支持 modality → error
- max_candidates 约束
- provenance 完整（provider_id/model/version/hash/seed）

红线对照：
- provider adapter 可换（§3.1）
- 不绑 SDK（Protocol 只定义 generate）
- 确定性 stub（§8.2 G0-G1 测试）
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.edit_core

from src.features.edit.ai_editing.generation_models import (
    GenerationConsent,
    GenerationModality,
    GenerationRequest,
    LicenseStatus,
)
from src.features.edit.ai_editing.generation_provider import (
    DeterministicGenerationProvider,
    ProviderRegistry,
)


def _request(modality=GenerationModality.VIDEO, prompt="产品特写", **kw) -> GenerationRequest:
    base = dict(
        request_id="req1", modality=modality, prompt=prompt,
        consent=GenerationConsent(
            confirmed=True, provider_id="stub",
            cost_acknowledged=True, upload_acknowledged=True,
            copyright_acknowledged=True,
        ),
    )
    base.update(kw)
    return GenerationRequest(**base)


# ================================================================
# ProviderRegistry
# ================================================================


class TestRegistry:
    def test_register_and_get(self):
        reg = ProviderRegistry()
        p = DeterministicGenerationProvider()
        reg.register(p, preferred=True)
        assert reg.count == 1
        assert reg.get(p.provider_id) is p

    def test_preferred_for_modality(self):
        reg = ProviderRegistry()
        p = DeterministicGenerationProvider()
        reg.register(p, preferred=True)
        pref = reg.preferred_for(GenerationModality.VIDEO)
        assert pref is p

    def test_preferred_fallback_first_matching(self):
        """无 preferred 标记时，取第一个支持该 modality 的。"""
        reg = ProviderRegistry()
        p = DeterministicGenerationProvider()
        reg.register(p)  # 不标 preferred
        pref = reg.preferred_for(GenerationModality.MUSIC)
        assert pref is p

    def test_preferred_none_when_unregistered(self):
        reg = ProviderRegistry()
        assert reg.preferred_for(GenerationModality.VIDEO) is None

    def test_require_unknown_raises(self):
        reg = ProviderRegistry()
        with pytest.raises(KeyError):
            reg.require("bogus")

    def test_set_preferred(self):
        reg = ProviderRegistry()
        p = DeterministicGenerationProvider(provider_id="p1")
        reg.register(p)
        assert reg.set_preferred(GenerationModality.IMAGE, "p1") is True
        assert reg.preferred_for(GenerationModality.IMAGE) is p

    def test_set_preferred_unknown_provider_fails(self):
        reg = ProviderRegistry()
        assert reg.set_preferred(GenerationModality.IMAGE, "bogus") is False

    def test_empty_provider_id_rejected(self):
        reg = ProviderRegistry()

        class BadProvider:
            provider_id = ""
            supported_modalities = (GenerationModality.VIDEO,)
            def generate(self, r, **kw):
                ...

        with pytest.raises(ValueError):
            reg.register(BadProvider())  # type: ignore[arg-type]

    def test_list_providers(self):
        reg = ProviderRegistry()
        p1 = DeterministicGenerationProvider(provider_id="p1")
        p2 = DeterministicGenerationProvider(provider_id="p2")
        reg.register(p1)
        reg.register(p2)
        assert len(reg.list_providers()) == 2


# ================================================================
# DeterministicGenerationProvider
# ================================================================


class TestDeterministicProvider:
    def test_generates_candidates(self):
        p = DeterministicGenerationProvider()
        result = p.generate(_request())
        assert result.succeeded
        assert len(result.candidates) >= 1

    def test_deterministic_same_prompt_same_ids(self):
        """确定性：相同 prompt → 相同候选 asset_id（便于 eval）。"""
        p = DeterministicGenerationProvider()
        r1 = p.generate(_request(request_id="r1"))
        r2 = p.generate(_request(request_id="r1"))  # 相同 request_id + prompt
        ids1 = [c.asset_id for c in r1.candidates]
        ids2 = [c.asset_id for c in r2.candidates]
        assert ids1 == ids2

    def test_max_candidates_respected(self):
        p = DeterministicGenerationProvider()
        result = p.generate(_request())  # 默认 budget.max_candidates=4, stub 上限 3
        assert len(result.candidates) <= 3

    def test_unsupported_modality_error(self):
        p = DeterministicGenerationProvider(
            _modalities=(GenerationModality.VIDEO,),  # 只支持视频
        )
        result = p.generate(_request(modality=GenerationModality.MUSIC))
        assert not result.succeeded
        assert "UNSUPPORTED_MODALITY" in result.error_code

    def test_provenance_complete(self):
        """每个候选 provenance 完整（§1.2 #6）。"""
        p = DeterministicGenerationProvider()
        result = p.generate(_request(reference_asset_ids=("a1",)))
        for c in result.candidates:
            prov = c.provenance
            assert prov.provider_id == "deterministic-stub"
            assert prov.model_id  # 非空
            assert prov.model_version  # 非空
            assert prov.input_prompt_hash  # 非空（hash）
            assert prov.seed is not None
            assert "a1" in prov.reference_asset_ids

    def test_default_license_unknown(self):
        """stub 默认 license=UNKNOWN（需人工清权；§11.2 M11 step4）。"""
        p = DeterministicGenerationProvider()
        result = p.generate(_request())
        for c in result.candidates:
            assert c.license == LicenseStatus.UNKNOWN
            assert c.is_commercial_ready is False

    def test_safety_passed_default(self):
        p = DeterministicGenerationProvider()
        result = p.generate(_request())
        for c in result.candidates:
            assert c.safety.passed is True
            assert c.safety.blocked is False

    def test_progress_callback_invoked(self):
        p = DeterministicGenerationProvider()
        progresses: list[float] = []
        p.generate(_request(), progress=lambda frac, msg: progresses.append(frac))
        assert len(progresses) >= 2
        assert progresses[-1] == 1.0  # 最后到 100%

    def test_cost_zero_for_stub(self):
        p = DeterministicGenerationProvider()
        result = p.generate(_request())
        assert result.actual_cost_units == 0.0
