"""M11 Media Generation Service 单元测试（§3.1 / §8.2 / §11.1 M11 / §11.2 M11 step 1）。

覆盖：
- GenerationService.request_generation 主闭环
- 同意 gate（未完整确认 → 不创建 Job）
- provider 查找（无注册 → 诊断）
- 成本上限 gate（max_cost_units <= 0 → 拒绝）
- 成功路径：Job SUCCEEDED + 候选入素材箱
- provider 失败：Job FAILED + 候选不入素材箱（不污染工程）
- provider 异常：Job FAILED + 错误诊断
- CostLedger 累计 + alert
- cancel 已 terminal Job

红线对照：
- 未经用户确认不得生成付费素材（§0 第一条）
- 生成取消/失败不污染工程（§11.1 M11 退出条件）
- 候选入媒体池后才可选择（§11.1 M11）
- 成本上限硬 gate（§10.2 / §11.2 M11 step1）
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.edit_core

from src.features.edit.ai_editing.generation_bin import GenerationBin
from src.features.edit.ai_editing.generation_models import (
    GeneratedAsset,
    GenerationBudget,
    GenerationConsent,
    GenerationModality,
    GenerationProvenance,
    GenerationRequest,
    GenerationResult,
    LicenseStatus,
)
from src.features.edit.ai_editing.generation_provider import (
    DeterministicGenerationProvider,
    ProviderRegistry,
)
from src.features.edit.ai_editing.jobs import JobKind, JobRegistry, JobStatus
from src.features.edit.ai_editing.media_generation_service import (
    CostLedger,
    GenerationService,
)


def _full_consent(**kw) -> GenerationConsent:
    base = dict(
        confirmed=True, provider_id="deterministic-stub",
        cost_acknowledged=True, upload_acknowledged=True,
        copyright_acknowledged=True,
    )
    base.update(kw)
    return GenerationConsent(**base)


def _request(modality=GenerationModality.VIDEO, prompt="产品特写", **kw) -> GenerationRequest:
    base = dict(
        request_id="req1", modality=modality, prompt=prompt,
        consent=_full_consent(),
    )
    base.update(kw)
    return GenerationRequest(**base)


def _service(*, ledger=None, registry=None, auto_register=True) -> GenerationService:
    reg = registry or ProviderRegistry()
    if auto_register and reg.count == 0:
        reg.register(DeterministicGenerationProvider(), preferred=True)
    return GenerationService(
        registry=reg, bin=GenerationBin(), jobs=JobRegistry(), cost_ledger=ledger,
    )


# ================================================================
# 同意 gate
# ================================================================


class TestConsentGate:
    def test_incomplete_consent_rejected(self):
        """红线（§0 第一条）：未完整确认 → 不创建 Job。"""
        svc = _service()
        req = _request(consent=_full_consent(confirmed=False))
        job, diags = svc.request_generation(req)
        assert job is None
        assert any(d.code.value == "TAEP_PERMISSION_DENIED" for d in diags)

    def test_missing_cost_ack_rejected(self):
        svc = _service()
        req = _request(consent=_full_consent(cost_acknowledged=False))
        job, diags = svc.request_generation(req)
        assert job is None
        assert any("cost_acknowledged" in d.message for d in diags)

    def test_missing_all_four_ack_rejected(self):
        svc = _service()
        req = _request(consent=GenerationConsent())  # 全 False
        job, diags = svc.request_generation(req)
        assert job is None
        msg = diags[0].message
        for m in ("confirmed", "cost_acknowledged", "upload_acknowledged", "copyright_acknowledged"):
            assert m in msg


# ================================================================
# provider 查找 gate
# ================================================================


class TestProviderGate:
    def test_no_provider_registered(self):
        # consent.provider_id 为空 → 退化 preferred_for → 无 provider → UNSUPPORTED_OPERATION
        svc = _service(registry=ProviderRegistry(), auto_register=False)  # 空 registry
        job, diags = svc.request_generation(_request(consent=_full_consent(provider_id="")))
        assert job is None
        assert any(d.code.value == "TAEP_UNSUPPORTED_OPERATION" for d in diags)

    def test_consent_provider_id_mismatch_rejected(self):
        """红线修复（§2.3 防偷换）：consent.provider_id 与 registry 实际 provider 不符 → 拒。

        用户确认 provider A，registry 换成 B（或 A 被反注册）→ 必须拒绝，不能静默走 B。
        """
        # registry 里只有 deterministic-stub，consent 却声明 "seedance" → 拒
        svc = _service()
        job, diags = svc.request_generation(
            _request(consent=_full_consent(provider_id="seedance")),
        )
        assert job is None
        assert any(d.code.value == "TAEP_PERMISSION_DENIED" for d in diags)
        assert any("偷换" in d.message or "provider" in d.message.lower() for d in diags)

    def test_consent_provider_id_wrong_modality_rejected(self):
        """consent.provider_id 存在但不支持该 modality → 拒（防偷换维度二）。"""
        class ImageOnlyProvider:
            provider_id = "image-only"
            supported_modalities = (GenerationModality.IMAGE,)
            def generate(self, r, **kw):
                raise AssertionError("不应被调用：consent/provider 不匹配应早拒")

        reg = ProviderRegistry()
        reg.register(ImageOnlyProvider(), preferred=True)  # type: ignore[arg-type]
        svc = _service(registry=reg)
        # consent 确认 image-only，但请求 VIDEO modality → 不支持 → 拒
        job, diags = svc.request_generation(
            _request(modality=GenerationModality.VIDEO,
                     consent=_full_consent(provider_id="image-only")),
        )
        assert job is None
        assert any(d.code.value == "TAEP_PERMISSION_DENIED" for d in diags)

    def test_empty_consent_provider_id_falls_back_to_preferred(self):
        """consent.provider_id 为空 → 退化 preferred_for（兼容旧调用方）。"""
        svc = _service()  # 注册了 deterministic-stub（preferred）
        job, diags = svc.request_generation(
            _request(consent=_full_consent(provider_id="")),
        )
        assert job is not None
        assert job.status == JobStatus.SUCCEEDED
        assert diags == []


# ================================================================
# 成本上限 gate
# ================================================================


class TestCostGate:
    def test_zero_budget_rejected(self):
        """§10.2：max_cost_units <= 0 → 拒绝。"""
        svc = _service()
        req = _request(budget=GenerationBudget(max_cost_units=0))
        job, diags = svc.request_generation(req)
        assert job is None
        assert any(d.code.value == "TAEP_QUOTA_EXCEEDED" for d in diags)

    def test_negative_budget_rejected(self):
        svc = _service()
        req = _request(budget=GenerationBudget(max_cost_units=-1))
        job, diags = svc.request_generation(req)
        assert job is None
        assert any(d.code.value == "TAEP_QUOTA_EXCEEDED" for d in diags)


# ================================================================
# 成功路径
# ================================================================


class TestSuccessPath:
    def test_job_succeeded_candidates_in_bin(self):
        """§8.2 G1：成功 → Job SUCCEEDED + 候选入素材箱。"""
        svc = _service()
        job, diags = svc.request_generation(_request())
        assert job is not None
        assert job.status == JobStatus.SUCCEEDED
        assert diags == []
        assert job.kind == JobKind.GENERATION
        # 候选入素材箱
        candidates = svc.generation_bin.by_request("req1")
        assert len(candidates) >= 1

    def test_result_manifest_has_candidate_ids(self):
        svc = _service()
        job, _ = svc.request_generation(_request())
        assert job.result_manifest is not None
        assert "candidate_ids" in job.result_manifest
        assert len(job.result_manifest["candidate_ids"]) >= 1

    def test_progress_tracked(self):
        svc = _service()
        job, _ = svc.request_generation(_request())
        assert job.progress == 1.0  # stub 同步完成

    def test_candidates_default_license_unknown(self):
        """候选默认 license=UNKNOWN（需人工清权）。"""
        svc = _service()
        svc.request_generation(_request())
        for c in svc.generation_bin.by_request("req1"):
            assert c.license == LicenseStatus.UNKNOWN

    def test_async_generation_uses_shared_worker_and_job_registry(self):
        """Remote-style work leaves the caller immediately and completes through Job events."""
        svc = _service()
        job, diags = svc.request_generation_async(_request())
        assert job is not None
        assert diags == []
        future = svc.worker_pool.future(job.job_id)
        assert future is not None
        future.result(timeout=2)
        assert job.status == JobStatus.SUCCEEDED
        assert [event.kind for event in svc.job_registry.events_since(0)][-1] == "succeeded"
        svc.close()


# ================================================================
# 失败/取消不污染工程
# ================================================================


class TestFailureIsolation:
    def test_provider_failure_no_candidates_in_bin(self):
        """红线（§11.1 M11 退出条件）：provider 失败 → 候选不入素材箱。"""
        class FailingProvider:
            provider_id = "failing"
            supported_modalities = (GenerationModality.VIDEO,)
            def generate(self, r, **kw):
                from src.features.edit.ai_editing.generation_models import GenerationResult
                return GenerationResult(
                    request_id=r.request_id, error_code="PROVIDER_DOWN",
                    error_message="model offline",
                )

        reg = ProviderRegistry()
        reg.register(FailingProvider(), preferred=True)  # type: ignore[arg-type]
        svc = _service(registry=reg)
        # consent.provider_id 必须匹配实际 provider（防偷换 gate）
        job, diags = svc.request_generation(_request(consent=_full_consent(provider_id="failing")))
        assert job is not None
        assert job.status == JobStatus.FAILED
        assert job.error_code == "PROVIDER_DOWN"
        assert "model offline" in job.error_message
        # 候选不入素材箱（工程不污染）
        assert svc.generation_bin.count == 0
        # 失败诊断可见（error 级）
        assert any(d.is_error for d in diags)

    def test_provider_exception_job_failed(self):
        """provider 抛异常 → Job FAILED + 错误诊断（不吞）。"""
        class CrashingProvider:
            provider_id = "crash"
            supported_modalities = (GenerationModality.VIDEO,)
            def generate(self, r, **kw):
                raise RuntimeError("network timeout")

        reg = ProviderRegistry()
        reg.register(CrashingProvider(), preferred=True)  # type: ignore[arg-type]
        svc = _service(registry=reg)
        # consent.provider_id 必须匹配实际 provider（防偷换 gate）
        job, diags = svc.request_generation(_request(consent=_full_consent(provider_id="crash")))
        assert job.status == JobStatus.FAILED
        assert "network timeout" in job.error_message
        assert svc.generation_bin.count == 0  # 不污染

    def test_cancel_terminal_job_is_noop(self):
        """已 terminal（SUCCEEDED）的 Job 取消是 no-op（状态不变）。

        ``JobRegistry.cancel`` 找到 job 即返回 True（取消请求被接受），
        但 ``request_cancel`` 对 terminal job 不改状态（``status.is_active`` 为 False）。
        v1（同步 stub）：Job 已完成；真实异步 worker 取消会中断进行中的 provider 调用。
        """
        svc = _service()
        job, _ = svc.request_generation(_request())  # 同步完成（已 terminal）
        # cancel 找到 job（True），但状态不变（terminal no-op）
        svc.cancel(job.job_id)
        refreshed = svc.job_registry.get(job.job_id)
        assert refreshed.status == JobStatus.SUCCEEDED  # 取消未改变 terminal 状态


class TestProviderResultIntegrity:
    @staticmethod
    def _asset(request_id: str, *, asset_id: str = "candidate-1", provider_id: str = "checked"):
        return GeneratedAsset(
            asset_id=asset_id,
            request_id=request_id,
            modality=GenerationModality.VIDEO,
            provenance=GenerationProvenance(
                provider_id=provider_id,
                model_id="model-v1",
                model_version="2026-08",
                input_prompt_hash="prompt-hash",
            ),
        )

    def _service_with_provider(self, provider):
        registry = ProviderRegistry()
        registry.register(provider, preferred=True)  # type: ignore[arg-type]
        return _service(registry=registry)

    def test_mismatched_candidate_identity_is_rejected_before_bin(self):
        class Provider:
            provider_id = "checked"
            supported_modalities = (GenerationModality.VIDEO,)

            def generate(self, request, **_kwargs):
                return GenerationResult(
                    request_id=request.request_id,
                    candidates=(self_asset,),
                )

        self_asset = self._asset("other-request")
        svc = self._service_with_provider(Provider())
        job, diagnostics = svc.request_generation(
            _request(consent=_full_consent(provider_id="checked")),
        )
        assert job.status == JobStatus.FAILED
        assert job.error_code == "PROVIDER_RESULT_INVALID"
        assert svc.generation_bin.count == 0
        assert diagnostics[0].code.value == "TAEP_SCHEMA_INVALID"

    def test_candidate_count_over_budget_is_rejected(self):
        class Provider:
            provider_id = "checked"
            supported_modalities = (GenerationModality.VIDEO,)

            def generate(self, request, **_kwargs):
                return GenerationResult(
                    request_id=request.request_id,
                    candidates=(
                        TestProviderResultIntegrity._asset(request.request_id),
                        TestProviderResultIntegrity._asset(request.request_id, asset_id="candidate-2"),
                    ),
                )

        svc = self._service_with_provider(Provider())
        job, _ = svc.request_generation(_request(
            consent=_full_consent(provider_id="checked"),
            budget=GenerationBudget(max_candidates=1),
        ))
        assert job.status == JobStatus.FAILED
        assert svc.generation_bin.count == 0

    def test_duplicate_candidate_ids_are_rejected(self):
        class Provider:
            provider_id = "checked"
            supported_modalities = (GenerationModality.VIDEO,)

            def generate(self, request, **_kwargs):
                asset = TestProviderResultIntegrity._asset(request.request_id)
                return GenerationResult(
                    request_id=request.request_id,
                    candidates=(asset, asset),
                )

        svc = self._service_with_provider(Provider())
        job, _ = svc.request_generation(
            _request(consent=_full_consent(provider_id="checked")),
        )
        assert job.status == JobStatus.FAILED
        assert svc.generation_bin.count == 0


# ================================================================
# CostLedger
# ================================================================


class TestCostLedger:
    def test_accumulate_and_total(self):
        ledger = CostLedger()
        ledger.add(provider_id="stub", modality=GenerationModality.VIDEO, cost_units=0.5)
        ledger.add(provider_id="stub", modality=GenerationModality.VIDEO, cost_units=0.3)
        assert ledger.total_for("stub") == 0.8
        assert ledger.grand_total() == 0.8

    def test_alert_when_exceeded(self):
        ledger = CostLedger(monthly_cost_alert=1.0)
        ledger.add(provider_id="stub", modality=GenerationModality.VIDEO, cost_units=1.2)
        alert = ledger.check_alert("stub")
        assert alert is not None
        assert "1.2" in alert

    def test_no_alert_below_threshold(self):
        ledger = CostLedger(monthly_cost_alert=10.0)
        ledger.add(provider_id="stub", modality=GenerationModality.VIDEO, cost_units=0.5)
        assert ledger.check_alert("stub") is None

    def test_reset(self):
        ledger = CostLedger()
        ledger.add(provider_id="stub", modality=GenerationModality.VIDEO, cost_units=1.0)
        ledger.reset()
        assert ledger.grand_total() == 0.0
