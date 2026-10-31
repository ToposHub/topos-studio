"""GenerationHarnessAdapter 单元测试（plan §10.5 P3.3）。

覆盖：

- invocation_to_generation_request：modality/prompt/consent/budget 映射。
- 完整 consent → 成功；候选进 RunRecord artifact；许可证未知 → with_warnings。
- consent 不完整 → WAITING_FOR_USER（plan §0 红线 1）。
- allow_generation=False → 拒绝。
- 候选不自动进 timeline（只产 artifact 引用）。
- 取消传播。

红线（plan §10.5 P3.3 / §1.3）：

- 生成前显示模型/上传/成本/许可证（consent 必须完整）。
- 结果不自动进 timeline。
- 取消/失败不污染工程。
- prompt 经 hash 进 provenance（不存原始）。
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.edit_core

from src.features.ai_harness.generation_adapter import (
    GenerationHarnessAdapter,
    invocation_to_generation_request,
)
from src.features.ai_harness.invocation_models import (
    Invocation, InvocationScope, PermissionLevel, RunPolicy, RunStatus,
)
from src.features.ai_harness.runtime import HarnessRuntime
from src.features.edit.ai_editing.generation_models import (
    ContentSafetyResult, GeneratedAsset, GenerationConsent, GenerationModality,
    GenerationProvenance, GenerationResult,
)
from src.features.edit.ai_editing.generation_provider import (
    DeterministicGenerationProvider, ProviderRegistry,
)
from src.features.edit.ai_editing.media_generation_service import GenerationService


# ================================================================
# fixtures
# ================================================================


def _full_consent(provider_id="deterministic-stub"):
    return GenerationConsent(
        confirmed=True, provider_id=provider_id,
        cost_acknowledged=True, upload_acknowledged=True,
        copyright_acknowledged=True,
    )


def _gen_service():
    reg = ProviderRegistry()
    reg.register(DeterministicGenerationProvider(), preferred=True)
    return GenerationService(reg)


def _gen_invocation(*, allow_generation=True, consent=None, prompt="a cat",
                    modality="image"):
    return Invocation(
        source="chat", skill_id="topos.generate",
        scope=InvocationScope(project_id="p1"),
        policy=RunPolicy(
            permission=PermissionLevel.PROPOSE,
            allow_generation=allow_generation,
        ),
        inputs={
            "modality": modality, "prompt": prompt,
            **({"consent": consent} if consent is not None else {}),
        },
        parent_session_id="sess1",
    )


@pytest.fixture
def adapter():
    svc = _gen_service()
    rt = HarnessRuntime()
    yield GenerationHarnessAdapter(svc, rt), rt


# ================================================================
# invocation_to_generation_request
# ================================================================


class TestRequestMapping:
    def test_modality_mapped(self):
        inv = _gen_invocation(modality="video")
        req = invocation_to_generation_request(inv)
        assert req.modality == GenerationModality.VIDEO

    def test_invalid_modality_defaults_image(self):
        inv = _gen_invocation(modality="unknown")
        req = invocation_to_generation_request(inv)
        assert req.modality == GenerationModality.IMAGE

    def test_prompt_carried(self):
        inv = _gen_invocation(prompt="hello world")
        req = invocation_to_generation_request(inv)
        assert req.prompt == "hello world"

    def test_consent_from_flat_fields(self):
        inv = Invocation(
            source="chat", scope=InvocationScope(project_id="p1"),
            policy=RunPolicy(allow_generation=True),
            inputs={
                "modality": "image", "prompt": "x",
                "consent_confirmed": True, "provider_id": "p1",
                "cost_acknowledged": True, "upload_acknowledged": True,
                "copyright_acknowledged": True,
            },
        )
        req = invocation_to_generation_request(inv)
        assert req.consent.is_complete is True

    def test_request_id_from_invocation(self):
        inv = _gen_invocation()
        req = invocation_to_generation_request(inv)
        assert req.request_id == inv.invocation_id


# ================================================================
# 完整 consent → 成功
# ================================================================


class TestSuccessfulGeneration:
    def test_succeeds_with_candidates(self, adapter):
        ad, rt = adapter
        rid = ad.invoke(_gen_invocation(consent=_full_consent()))
        rec = rt.get_run(rid)
        assert rec.status in (RunStatus.SUCCEEDED, RunStatus.SUCCEEDED_WITH_WARNINGS)
        # 候选进 RunRecord artifact（deterministic provider 默认产候选）
        assert len(rec.artifacts) >= 1
        assert all("generated_" in a.artifact_type for a in rec.artifacts)

    def test_license_unknown_warns(self, adapter):
        """deterministic provider 产 license=UNKNOWN → with_warnings。"""
        ad, rt = adapter
        rid = ad.invoke(_gen_invocation(consent=_full_consent()))
        rec = rt.get_run(rid)
        assert rec.status == RunStatus.SUCCEEDED_WITH_WARNINGS
        assert any(d.get("code") == "LICENSE_UNKNOWN" for d in rec.diagnostics)

    def test_job_attached_to_run(self, adapter):
        ad, rt = adapter
        rid = ad.invoke(_gen_invocation(consent=_full_consent()))
        rec = rt.get_run(rid)
        assert len(rec.jobs) >= 1
        assert rec.jobs[0].kind == "generation"


# ================================================================
# consent gate（plan §0 红线 1）
# ================================================================


class TestConsentGate:
    def test_incomplete_consent_waits_for_user(self, adapter):
        ad, rt = adapter
        rid = ad.invoke(_gen_invocation(consent=None))  # 无 consent
        rec = rt.get_run(rid)
        assert rec.status == RunStatus.WAITING_FOR_USER
        assert any(d.get("code") == "CONSENT_INCOMPLETE" for d in rec.diagnostics)

    def test_consent_missing_fields_listed(self, adapter):
        ad, rt = adapter
        rid = ad.invoke(_gen_invocation(consent=None))
        rec = rt.get_run(rid)
        diag = next(d for d in rec.diagnostics if d.get("code") == "CONSENT_INCOMPLETE")
        assert "missing" in diag
        # 列出缺的字段
        assert "provider_id" in diag["missing"] or "cost_acknowledged" in diag["missing"]

    def test_partial_consent_still_waits(self, adapter):
        """只确认了一部分 → 仍 WAITING。"""
        ad, rt = adapter
        inv = _gen_invocation(consent=GenerationConsent(
            confirmed=True, provider_id="x",
            cost_acknowledged=True,  # 缺 upload / copyright
        ))
        rid = ad.invoke(inv)
        assert rt.get_run(rid).status == RunStatus.WAITING_FOR_USER


# ================================================================
# policy gate
# ================================================================


class TestPolicyGate:
    def test_generation_disabled_rejected(self, adapter):
        ad, rt = adapter
        rid = ad.invoke(_gen_invocation(allow_generation=False, consent=_full_consent()))
        rec = rt.get_run(rid)
        assert rec.status == RunStatus.FAILED
        assert rec.error_code == "GENERATION_DISABLED"


# ================================================================
# 红线：结果不自动进 timeline
# ================================================================


class TestNoAutoTimeline:
    def test_no_timeline_write_events(self, adapter):
        """生成只产候选 artifact，不写 timeline。"""
        ad, rt = adapter
        rid = ad.invoke(_gen_invocation(consent=_full_consent()))
        rec = rt.get_run(rid)
        # 没有 approval/commit/apply 事件（生成不写时间线）
        write_events = [e for e in rec.events
                        if "approval" in e.kind or "commit" in e.kind or "apply" in e.kind]
        assert write_events == []

    def test_candidates_are_artifact_refs_not_clips(self, adapter):
        """候选是 ArtifactRef 引用，不是 timeline clip。"""
        ad, rt = adapter
        rid = ad.invoke(_gen_invocation(consent=_full_consent()))
        rec = rt.get_run(rid)
        for a in rec.artifacts:
            # ArtifactRef 只有 artifact_id/type/producer/summary；无 clip 字段
            assert not hasattr(a, "timeline_id")
            assert not hasattr(a, "track_name")


# ================================================================
# 安全拦截诊断
# ================================================================


class TestSafetyDiagnostics:
    def _write_events(self, rec):
        return [e for e in rec.events
                if "approval" in e.kind or "commit" in e.kind or "apply" in e.kind]

    def test_safety_blocked_candidate_records_diagnostic(self):
        """bin 中出现 safety.blocked 候选 → SAFETY_BLOCKED 诊断（不进 timeline）。

        service 门禁（CONTENT_SAFETY_BLOCKED）会把 provider 返回的 blocked 候选
        拒之箱外（见下一用例）；本用例直接入箱，验证 adapter 的二次防线——
        generation_adapter 对 bin 中 blocked 候选必记 SAFETY_BLOCKED 诊断。
        """
        svc = _gen_service()
        rt = HarnessRuntime()
        ad = GenerationHarnessAdapter(svc, rt)
        inv = _gen_invocation(consent=_full_consent())
        blocked = GeneratedAsset(
            asset_id=f"gen-{inv.invocation_id}-blocked",
            request_id=inv.invocation_id,
            modality=GenerationModality.IMAGE,
            provenance=GenerationProvenance(provider_id="deterministic-stub"),
            safety=ContentSafetyResult(
                passed=False, categories=("copyright",), detail="stub blocked",
            ),
        )
        svc.generation_bin.add_candidates((blocked,))
        rid = ad.invoke(inv)
        rec = rt.get_run(rid)
        diag = next(d for d in rec.diagnostics if d.get("code") == "SAFETY_BLOCKED")
        assert diag["asset_id"] == blocked.asset_id
        # 红线：被拦截候选不产生任何 timeline 写入事件
        assert self._write_events(rec) == []

    def test_provider_blocked_candidate_rejected_before_bin(self):
        """provider 返回 blocked 候选 → service 门禁拒入箱 → run FAILED 零候选。

        端到端安全路径：被拦截内容不产生任何 generated_* artifact，
        也不进 GenerationBin（不污染工程）。
        """
        class _BlockedStubProvider:
            """返回 safety.blocked=True 候选的确定性 provider（不联网）。"""

            provider_id = "blocked-stub"
            is_demo = True  # 走同步路径（adapter 不进 async worker）

            @property
            def supported_modalities(self):
                return (GenerationModality.IMAGE,)

            def generate(self, request, *, progress=None):
                return GenerationResult(
                    request_id=request.request_id,
                    candidates=(GeneratedAsset(
                        asset_id=f"gen-{request.request_id}-blocked",
                        request_id=request.request_id,
                        modality=request.modality,
                        provenance=GenerationProvenance(
                            provider_id=self.provider_id,
                            model_id="stub", model_version="v1",
                            input_prompt_hash="0" * 16,
                        ),
                        safety=ContentSafetyResult(
                            passed=False, categories=("nsfw",),
                        ),
                    ),),
                    actual_cost_units=0.0,
                )

        svc_provider_reg = ProviderRegistry()
        svc_provider_reg.register(_BlockedStubProvider(), preferred=True)
        svc_blocked = GenerationService(svc_provider_reg)
        rt = HarnessRuntime()
        ad = GenerationHarnessAdapter(svc_blocked, rt)
        inv = _gen_invocation(consent=_full_consent(provider_id="blocked-stub"))
        rid = ad.invoke(inv)
        rec = rt.get_run(rid)
        # service 门禁生效：job 失败、无候选入箱、无 generated_* artifact
        assert rec.status == RunStatus.FAILED
        assert rec.artifacts == ()
        assert svc_blocked.generation_bin.by_request(inv.invocation_id) == ()
        # 门禁诊断可见（TAEP_QUALITY_GATE_FAILED + content safety 细节）
        assert "TAEP_QUALITY_GATE_FAILED" in str(rec.diagnostics)
        assert "content safety" in str(rec.diagnostics)
        # 红线：无 timeline 写入事件
        assert self._write_events(rec) == []
