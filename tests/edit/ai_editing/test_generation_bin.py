"""M11 生成素材箱 单元测试（§8.2 G1-G2 / §11.1 M11 / §11.2 M11 step 2）。

覆盖：
- GenerationBin add_candidates / get / by_state / by_request
- 状态机：generated → previewed → imported（经 import_to_media_pool）
- 状态机：generated → discarded
- importable gating（safety blocked / 已 imported / 已 discarded 不可导入）
- import_to_media_pool 经 MediaPoolImporter 注入点
- 已 imported 不可 discard
- provenance_report（导入事件追溯 + commercial_ready 统计 + any_license_unknown）
- 删除候选不影响原素材（discard 只移素材箱条目）

红线对照：
- 候选入媒体池后才可选择（§11.1 M11 退出条件）
- 删除生成候选不影响原素材（§11.2 M11 完成检查）
- 安全阻断不可导入（§8.2）
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.edit_core

from src.features.edit.ai_editing.generation_bin import (
    GenerationBin,
    ImportEvent,
    MediaPoolImporter,
)
from src.features.edit.ai_editing.generation_models import (
    ContentSafetyResult,
    GeneratedAsset,
    GeneratedAssetState,
    GenerationModality,
    GenerationProvenance,
    LicenseStatus,
)


def _asset(aid="gen-1", request_id="req1", **kw) -> GeneratedAsset:
    base = dict(
        asset_id=aid, request_id=request_id,
        modality=GenerationModality.VIDEO,
        provenance=GenerationProvenance(provider_id="stub", model_id="stub-v"),
    )
    base.update(kw)
    return GeneratedAsset(**base)


class _StubImporter:
    """MediaPoolImporter stub：记录导入，可配置失败。"""

    def __init__(self, *, fail_on=None) -> None:
        self.imported: list[GeneratedAsset] = []
        self._fail_on = set(fail_on or ())
        self._counter = 0

    def import_asset(self, asset: GeneratedAsset) -> tuple[str | None, list]:
        if asset.asset_id in self._fail_on:
            return None, [{"severity": "error", "code": "TAEP_ASSET_OFFLINE",
                           "message": "import failed"}]
        self._counter += 1
        media_id = f"media-{self._counter}"
        self.imported.append(asset)
        return media_id, []


# ================================================================
# 基本查询
# ================================================================


class TestBinBasics:
    def test_empty_bin(self):
        bin = GenerationBin()
        assert bin.count == 0
        assert bin.all_assets() == ()

    def test_add_candidates(self):
        bin = GenerationBin()
        bin.add_candidates((_asset("g1"), _asset("g2")))
        assert bin.count == 2

    def test_add_idempotent(self):
        """重复 add 同 asset_id 不覆盖（幂等）。"""
        bin = GenerationBin()
        a1 = _asset("g1", license=LicenseStatus.UNKNOWN)
        bin.add_candidates((a1,))
        a2 = _asset("g1", license=LicenseStatus.KNOWN_LICENSED)  # 同 id 不同字段
        bin.add_candidates((a2,))
        assert bin.count == 1
        assert bin.get("g1").license == LicenseStatus.UNKNOWN  # 不覆盖

    def test_get_unknown_returns_none(self):
        bin = GenerationBin()
        assert bin.get("bogus") is None

    def test_require_unknown_raises(self):
        bin = GenerationBin()
        with pytest.raises(KeyError):
            bin.require("bogus")

    def test_by_state(self):
        bin = GenerationBin()
        bin.add_candidates((_asset("g1"), _asset("g2", state=GeneratedAssetState.DISCARDED)))
        assert len(bin.by_state(GeneratedAssetState.GENERATED)) == 1
        assert len(bin.by_state(GeneratedAssetState.DISCARDED)) == 1

    def test_by_request(self):
        bin = GenerationBin()
        bin.add_candidates((_asset("g1", request_id="r1"), _asset("g2", request_id="r2")))
        assert len(bin.by_request("r1")) == 1

    def test_pending_preview(self):
        bin = GenerationBin()
        bin.add_candidates((_asset("g1"), _asset("g2")))  # 都 GENERATED
        assert len(bin.pending_preview()) == 2

    def test_importable_filter(self):
        bin = GenerationBin()
        bin.add_candidates((
            _asset("g1"),  # importable
            _asset("g2", safety=ContentSafetyResult(passed=False)),  # blocked
        ))
        imp = bin.importable()
        assert len(imp) == 1
        assert imp[0].asset_id == "g1"


# ================================================================
# 状态迁移
# ================================================================


class TestStateTransitions:
    def test_mark_previewed(self):
        bin = GenerationBin()
        bin.add_candidates((_asset("g1"),))
        updated = bin.mark_previewed("g1")
        assert updated.state == GeneratedAssetState.PREVIEWED
        assert bin.get("g1").state == GeneratedAssetState.PREVIEWED

    def test_mark_previewed_idempotent(self):
        """已 previewed 不降级。"""
        bin = GenerationBin()
        bin.add_candidates((_asset("g1", state=GeneratedAssetState.PREVIEWED),))
        bin.mark_previewed("g1")  # 再调不报错
        assert bin.get("g1").state == GeneratedAssetState.PREVIEWED

    def test_discard(self):
        bin = GenerationBin()
        bin.add_candidates((_asset("g1"),))
        updated = bin.discard("g1")
        assert updated.state == GeneratedAssetState.DISCARDED

    def test_cannot_discard_imported(self):
        """已 imported 不可 discard（已进 Media Pool，需走 Media Pool 删除）。"""
        bin = GenerationBin()
        bin.add_candidates((_asset("g1", state=GeneratedAssetState.IMPORTED),))
        result = bin.discard("g1")
        assert result.state == GeneratedAssetState.IMPORTED  # 不变

    def test_discard_unknown_returns_none(self):
        bin = GenerationBin()
        assert bin.discard("bogus") is None


# ================================================================
# import_to_media_pool（注入点）
# ================================================================


class TestImport:
    def test_import_via_injection_point(self):
        """§8.2 G2：导入经 MediaPoolImporter 注入点。"""
        bin = GenerationBin()
        importer = _StubImporter()
        bin.add_candidates((_asset("g1"),))
        media_id, diags = bin.import_to_media_pool("g1", importer=importer, approved_by="user")
        assert media_id is not None
        assert diags == []
        assert bin.get("g1").state == GeneratedAssetState.IMPORTED
        assert len(importer.imported) == 1

    def test_import_records_event(self):
        """导入事件可追溯（§11.2 M11 完成检查）。"""
        bin = GenerationBin()
        bin.add_candidates((_asset("g1"),))
        bin.import_to_media_pool("g1", importer=_StubImporter(), approved_by="alice")
        events = bin.import_events()
        assert len(events) == 1
        assert events[0].asset_id == "g1"
        assert events[0].approved_by == "alice"
        assert events[0].imported_at.endswith("Z")
        assert events[0].license_at_import == LicenseStatus.UNKNOWN.value

    def test_import_blocked_when_safety_fails(self):
        """安全阻断不可导入（§8.2）。"""
        bin = GenerationBin()
        importer = _StubImporter()
        bin.add_candidates((_asset("g1", safety=ContentSafetyResult(passed=False, categories=("nsfw",))),))
        media_id, diags = bin.import_to_media_pool("g1", importer=importer)
        assert media_id is None
        assert any(d.code.value == "TAEP_PERMISSION_DENIED" for d in diags)
        assert bin.get("g1").state == GeneratedAssetState.GENERATED  # 状态不变

    def test_import_blocked_when_already_imported(self):
        bin = GenerationBin()
        bin.add_candidates((_asset("g1", state=GeneratedAssetState.IMPORTED),))
        media_id, diags = bin.import_to_media_pool("g1", importer=_StubImporter())
        assert media_id is None
        assert any("already imported" in d.message for d in diags)

    def test_import_blocked_when_discarded(self):
        bin = GenerationBin()
        bin.add_candidates((_asset("g1", state=GeneratedAssetState.DISCARDED),))
        media_id, diags = bin.import_to_media_pool("g1", importer=_StubImporter())
        assert media_id is None
        assert any("already discarded" in d.message for d in diags)

    def test_import_unknown_asset(self):
        bin = GenerationBin()
        media_id, diags = bin.import_to_media_pool("bogus", importer=_StubImporter())
        assert media_id is None
        assert any(d.code.value == "TAEP_ASSET_NOT_FOUND" for d in diags)

    def test_importer_failure_passes_diagnostics(self):
        """importer.import_asset 失败 → 透传诊断，状态不变。"""
        bin = GenerationBin()
        importer = _StubImporter(fail_on=["g1"])
        bin.add_candidates((_asset("g1"),))
        media_id, diags = bin.import_to_media_pool("g1", importer=importer)
        assert media_id is None
        assert bin.get("g1").state == GeneratedAssetState.GENERATED  # 未迁移


# ================================================================
# provenance report
# ================================================================


class TestProvenanceReport:
    def test_empty_report(self):
        bin = GenerationBin()
        report = bin.provenance_report()
        assert report["imported_count"] == 0
        assert report["any_license_unknown"] is False

    def test_report_includes_imported(self):
        bin = GenerationBin()
        bin.add_candidates((
            _asset("g1", license=LicenseStatus.KNOWN_LICENSED, cost_units=0.5),
            _asset("g2", license=LicenseStatus.UNKNOWN, cost_units=0.3),
        ))
        bin.import_to_media_pool("g1", importer=_StubImporter(), approved_by="u1")
        bin.import_to_media_pool("g2", importer=_StubImporter(), approved_by="u2")
        report = bin.provenance_report()
        assert report["imported_count"] == 2
        assert report["commercial_ready_count"] == 1  # g1 KNOWN_LICENSED
        assert report["total_cost_units"] == 0.8
        assert report["any_license_unknown"] is True  # g2 UNKNOWN
        assert len(report["entries"]) == 2

    def test_report_exposes_incomplete_commercial_audit(self):
        bin = GenerationBin()
        bin.add_candidates((_asset("g1", license=LicenseStatus.KNOWN_LICENSED),))
        bin.import_to_media_pool("g1", importer=_StubImporter(), approved_by="")

        report = bin.provenance_report()

        assert report["audit_complete"] is False
        assert report["missing_approval_count"] == 1
        assert report["missing_model_identity_count"] == 1
        assert report["missing_prompt_hash_count"] == 1
        assert report["missing_license_evidence_count"] == 1
        assert report["license_region_missing_count"] == 1
        assert report["entries"][0]["audit_complete"] is False

    def test_report_marks_complete_commercial_audit(self):
        bin = GenerationBin()
        bin.add_candidates((_asset(
            "g1",
            license=LicenseStatus.KNOWN_LICENSED,
            provenance=GenerationProvenance(
                provider_id="stub",
                model_id="stub-video",
                model_version="2026.08",
                input_prompt_hash="prompt-hash",
                license_evidence_ref="license:stub-2026",
                license_region="global",
                license_checked_at="2026-08-10T00:00:00Z",
            ),
        ),))
        bin.import_to_media_pool("g1", importer=_StubImporter(), approved_by="u1")

        report = bin.provenance_report()

        assert report["audit_complete"] is True
        assert report["entries"][0]["audit_complete"] is True

    def test_report_excludes_non_imported(self):
        bin = GenerationBin()
        bin.add_candidates((
            _asset("g1"),  # 未导入
            _asset("g2", state=GeneratedAssetState.DISCARDED),
        ))
        bin.import_to_media_pool("g1", importer=_StubImporter())
        # g1 imported, g2 discarded
        report = bin.provenance_report()
        # 只有 imported 的进 report
        assert report["imported_count"] == 1


# ================================================================
# 清理
# ================================================================


class TestCleanup:
    def test_clear_discarded(self):
        bin = GenerationBin()
        bin.add_candidates((
            _asset("g1"),
            _asset("g2", state=GeneratedAssetState.DISCARDED),
            _asset("g3", state=GeneratedAssetState.DISCARDED),
        ))
        removed = bin.clear_discarded()
        assert removed == 2
        assert bin.count == 1  # 只剩 g1
