"""P1 · 交付预设与自动 QC（2026-11-19 计划第二阶段）。

验收：预设按客户规范分版本（广播/影院/流媒体各自目标，不用一个固定
LUFS 覆盖）；BWF stem 清单覆盖 Full Mix/DX/ADR/Foley/FX/MX/M&E；
清单携带起始 TC、帧率/DF、声道顺序、峰值与校验摘要；实测超容差/
越界/缺 stem/过量静音按阻断或警示上报。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.features.edit.core.delivery_preset import (
    FULL_STEM_SET,
    BUILTIN_PRESETS,
    DeliveryPreset,
    LoudnessTarget,
    build_delivery_manifest,
    evaluate_delivery,
    preset_by_id,
)


class TestPresetModel:
    def test_builtin_presets_cover_three_standards(self) -> None:
        by_standard = {p.standard: p for p in BUILTIN_PRESETS}
        assert {"EBU R 128", "流媒体", "影院"} <= set(by_standard)
        # 目标各不相同（固定 LUFS 不覆盖所有场景）。
        targets = {p.loudness.target_lufs for p in BUILTIN_PRESETS}
        assert len(targets) == len(BUILTIN_PRESETS)
        assert by_standard["EBU R 128"].loudness.target_lufs == -23.0
        assert by_standard["影院"].channel_order == (
            "L", "R", "C", "LFE", "Ls", "Rs")

    def test_round_trip_and_stem_validation(self) -> None:
        preset = preset_by_id("broadcast-ebu-r128-v1")
        assert preset is not None
        restored = DeliveryPreset.from_dict(preset.to_dict())
        assert restored == preset
        assert preset.stems == FULL_STEM_SET
        with pytest.raises(ValueError):
            preset.with_stems(("FullMix", "Narration"))
        trimmed = preset.with_stems(("FullMix", "DX", "ME"))
        assert trimmed.stems == ("FullMix", "DX", "ME")


class TestManifest:
    def test_manifest_carries_tc_fps_channels_checksum(self) -> None:
        preset = DeliveryPreset(
            preset_id="p", name="P", standard="EBU R 128", version="v2",
            loudness=LoudnessTarget(-23.0, 1.0, -2.0),
            stems=("FullMix", "DX"),
            start_timecode="01:00:00:00", fps="25/1", drop_frame=False,
            channel_order=("L", "R"))
        manifest = build_delivery_manifest(
            preset,
            checksums={"FullMix": "deadbeef"},
            measured={"FullMix": {"integrated_lufs": -23.2,
                                  "true_peak_dbtp": -3.1,
                                  "peak_dbfs": -4.0}})
        assert manifest["standard_source"] == "EBU R 128 v2"
        full = next(e for e in manifest["stems"] if e["stem"] == "FullMix")
        assert full["start_timecode"] == "01:00:00:00"
        assert full["fps"] == "25/1" and full["drop_frame"] is False
        assert full["channel_order"] == ["L", "R"]
        assert full["sha256"] == "deadbeef"
        assert full["measured"]["integrated_lufs"] == -23.2
        dx = next(e for e in manifest["stems"] if e["stem"] == "DX")
        assert "sha256" not in dx and "measured" not in dx


class TestEvaluateDelivery:
    def _measured(self, **overrides):
        values = {"integrated_lufs": -23.0, "true_peak_dbtp": -3.0,
                  "peak_dbfs": -4.0, "max_silence_seconds": 0.0}
        values.update(overrides)
        return values

    def test_pass_within_tolerance(self) -> None:
        preset = preset_by_id("broadcast-ebu-r128-v1")
        findings, passed = evaluate_delivery(preset, {
            stem: self._measured() for stem in preset.stems})
        assert passed and findings == ()

    def test_loudness_out_of_tolerance_blocks(self) -> None:
        preset = preset_by_id("broadcast-ebu-r128-v1")
        measured = {stem: self._measured() for stem in preset.stems}
        measured["DX"] = self._measured(integrated_lufs=-18.0)
        findings, passed = evaluate_delivery(preset, measured)
        assert not passed
        loud = [f for f in findings if f["check_id"] == "delivery.DX.loudness"]
        assert loud and loud[0]["severity"] == "blocking"

    def test_true_peak_and_sample_peak_ceiling(self) -> None:
        preset = preset_by_id("broadcast-ebu-r128-v1")
        measured = {stem: self._measured() for stem in preset.stems}
        measured["MX"] = self._measured(true_peak_dbtp=-0.5,
                                        peak_dbfs=-0.1)
        findings, passed = evaluate_delivery(preset, measured)
        assert not passed
        ids = {f["check_id"] for f in findings}
        assert "delivery.MX.true_peak" in ids
        assert "delivery.MX.sample_peak" in ids

    def test_missing_stem_blocks(self) -> None:
        preset = preset_by_id("broadcast-ebu-r128-v1").with_stems(
            ("FullMix", "DX"))
        findings, passed = evaluate_delivery(
            preset, {"FullMix": self._measured()})
        assert not passed
        assert any(f["check_id"] == "delivery.DX.missing" for f in findings)

    def test_excess_silence_warns_but_passes(self) -> None:
        preset = preset_by_id("broadcast-ebu-r128-v1").with_stems(
            ("FullMix",))
        findings, passed = evaluate_delivery(
            preset, {"FullMix": self._measured(max_silence_seconds=9.0)})
        assert passed
        assert any(f["check_id"] == "delivery.FullMix.excess_silence"
                   and f["severity"] == "warning" for f in findings)
