"""Capability Registry（P1-17）测试。

验收口径（计划表）：
- 任何用户可见的"支持"都能指向测试证据；
- 状态由运行时探测决定（BWF writer 探测、效果注册健康）；
- 与 interchange 静态表的分歧可机器检测，而不是靠单测碰巧绑定。
"""

from __future__ import annotations

import json

import pytest

from src.features.edit.core import capability_registry as cr
from src.features.edit.core.capability_registry import (
    CapabilityStatus,
    build_capability_registry,
    find_status_drift,
)


@pytest.fixture()
def registry():
    return build_capability_registry()


class TestRegistryStructure:
    def test_all_entries_have_evidence_and_status(self, registry):
        assert registry.capabilities
        for cap in registry.capabilities:
            assert cap.capability_id
            assert isinstance(cap.status, CapabilityStatus)
            assert cap.evidence, f"{cap.capability_id} 缺少证据链"
            for ev in cap.evidence:
                assert isinstance(ev, str) and ev

    def test_generated_at_present(self, registry):
        assert registry.generated_at.endswith("+00:00") or "T" in registry.generated_at

    def test_get_by_id(self, registry):
        cap = registry.get("edit.interchange.bwf")
        assert cap is not None
        assert registry.get("edit.no.such") is None

    def test_by_status(self, registry):
        dormant = registry.by_status(CapabilityStatus.DORMANT)
        ids = {c.capability_id for c in dormant}
        # P1-16 接线后 fcpxml 不再 dormant；P1-01 接线后 multicam 同样升为 PARTIAL；
        # FE-B14 接线后 stem 导出（File 菜单入口）也不再 dormant
        assert "edit.interchange.fcpxml" not in ids
        assert "edit.multicam" not in ids
        assert "edit.interchange.audio_stems" not in ids

    def test_to_dict_json_serializable(self, registry):
        data = registry.to_dict()
        assert data["schema"] == "topos.capability_registry.v1"
        json.dumps(data)  # 不抛异常

    def test_is_healthy_with_all_evidence(self, registry):
        assert registry.is_healthy


class TestLiveProbes:
    def test_bwf_available_by_default(self, registry):
        cap = registry.get("edit.interchange.bwf")
        assert cap.status is CapabilityStatus.AVAILABLE

    def test_bwf_degrades_honestly_when_writer_unavailable(self, monkeypatch):
        """BWF 声明必须跟随 writer 实际能力（P0-06/P1-17 语义）。"""
        from src.features.edit.core import bwf
        monkeypatch.setattr(bwf, "is_true_bwf_supported", lambda: False)
        reg = build_capability_registry()
        cap = reg.get("edit.interchange.bwf")
        assert cap.status is CapabilityStatus.PARTIAL
        assert "RIFF INFO" in cap.notes

    def test_audio_effects_available_when_healthy(self, registry):
        cap = registry.get("edit.audio_effects")
        assert cap.status is CapabilityStatus.AVAILABLE

    def test_audio_effects_partial_when_probe_fails(self, monkeypatch):
        from src.features.edit.core import effect_registry
        monkeypatch.setattr(
            effect_registry,
            "get_effect_registry_health",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")),
        )
        reg = build_capability_registry()
        cap = reg.get("edit.audio_effects")
        assert cap.status is CapabilityStatus.PARTIAL
        assert "probe error" in cap.notes

    def test_preserve_pitch_available_realtime_and_offline(self, registry):
        """F3.2 落地后：离线整段 WSOLA + 实时流式 WSOLA 同参数（AVAILABLE）。"""
        cap = registry.get("edit.audio.preserve_pitch")
        assert cap.status is CapabilityStatus.AVAILABLE
        assert "WSOLA" in cap.notes and "流式" in cap.notes

    def test_otio_notes_optional_dependency_state(self, registry):
        cap = registry.get("edit.interchange.otio")
        # P1-16 接线后为 PARTIAL（受控子集 + 可选依赖预检）
        assert cap.status is CapabilityStatus.PARTIAL
        assert "opentimelineio" in cap.notes


class TestDormantInventory:
    def test_dormant_capabilities_declared(self, registry):
        # P1-01/P1-02 接线后 edit.multicam / edit.nested_sequence 升为
        # PARTIAL；FE-B14 接线后 audio_stems（File 菜单入口）升为
        # AVAILABLE——注册表不再持有过期的休眠声明。
        dormant_ids = {c.capability_id for c in registry.by_status(CapabilityStatus.DORMANT)}
        assert "edit.interchange.audio_stems" not in dormant_ids

    def test_stem_export_promoted(self, registry):
        """FE-B14: Stem 导出已接生产链路（File 菜单 + 对话框 + 渲染链）。"""
        cap = registry.get("edit.interchange.audio_stems")
        assert cap.status is CapabilityStatus.AVAILABLE
        evidence = " ".join(cap.evidence)
        assert "export_stem_tracks" in evidence
        assert "StemExportDialog" in evidence

    def test_multicam_promoted(self, registry):
        """P1-01: 多机位已接生产链路（创建/面板/键盘切换）。"""
        cap = registry.get("edit.multicam")
        assert cap.status is CapabilityStatus.PARTIAL
        assert "MulticamPanel" in " ".join(cap.evidence)
        assert "P1-01" in cap.notes

    def test_nested_sequence_promoted(self, registry):
        """P1-02: 嵌套序列已接生产链路（创建/进入/导出/resolver）。"""
        cap = registry.get("edit.nested_sequence")
        assert cap.status is CapabilityStatus.PARTIAL
        assert "build_nested_from_selection" in " ".join(cap.evidence)
        assert "P1-02" in cap.notes

    def test_aaf_omf_unavailable(self, registry):
        assert registry.get("edit.interchange.aaf").status is CapabilityStatus.UNAVAILABLE
        assert registry.get("edit.interchange.omf").status is CapabilityStatus.UNAVAILABLE

    def test_drop_frame_available(self, registry):
        cap = registry.get("edit.timecode.drop_frame")
        assert cap.status is CapabilityStatus.AVAILABLE
        assert any("test_interchange.py" in ev for ev in cap.evidence)


class TestStatusDrift:
    def test_stem_no_longer_drifts(self):
        """FE-B14：注册表与生产入口一致——stem 不再出现在漂移报告。"""
        drifts = find_status_drift()
        ids = {d.capability_id for d in drifts}
        assert "edit.interchange.audio_stems" not in ids

    def test_no_drift_for_consistent_entries(self, registry):
        drifts = find_status_drift(registry)
        ids = {d.capability_id for d in drifts}
        assert "edit.interchange.csv_cue_sheet" not in ids
        assert "edit.interchange.aaf" not in ids

    def test_bwf_degrades_to_partial_when_writer_unavailable(self, monkeypatch):
        """AUD-15700 + F1.3/R1：writer 不可用→PARTIAL（交付降级 WAV+RIFF INFO）；
        PARTIAL 与静态表 available=True 语义一致，不计漂移。"""
        monkeypatch.setattr(cr, "probe_bwf_supported", lambda errors=None: False)
        reg = build_capability_registry()
        cap = reg.get("edit.interchange.bwf")
        assert cap.status is cr.CapabilityStatus.PARTIAL
        assert "RIFF INFO" in cap.notes
        drifts = find_status_drift(reg)
        ids = {d.capability_id for d in drifts}
        assert "edit.interchange.bwf" not in ids

    def test_drift_to_dict(self):
        drifts = find_status_drift()
        if drifts:
            data = drifts[0].to_dict()
            assert data["registry_status"]
            assert isinstance(data["interchange_available"], bool)


class TestF13Consistency:
    """F1.3：注册表与静态表零漂移 + 新增真实能力条目在册。"""

    def test_zero_drift_all_formats(self):
        drifts = find_status_drift()
        assert drifts == (), f"仍有漂移: {[d.to_dict() for d in drifts]}"

    def test_audio_domain_entries_registered(self):
        reg = build_capability_registry()
        expected = {
            "edit.audio.loudness": CapabilityStatus.AVAILABLE,
            "edit.audio.recording_adr": CapabilityStatus.AVAILABLE,
            "edit.audio.graph_validation": CapabilityStatus.AVAILABLE,
            "edit.audio.health_report": CapabilityStatus.DORMANT,
            "edit.interchange.csv_cue_sheet": CapabilityStatus.DORMANT,
        }
        for cap_id, status in expected.items():
            cap = reg.get(cap_id)
            assert cap is not None, f"缺少条目 {cap_id}"
            assert cap.status is status, f"{cap_id} 应为 {status.value}"
            assert cap.evidence, f"{cap_id} 缺证据"
