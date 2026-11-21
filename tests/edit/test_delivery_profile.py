"""B05 · 版本化客户 profile、结构化测量与交付包发布回归。

对应主计划 §11：profile 序列化/未知字段保留、legacy 迁移、required 集
合推导、MeasurementResult 验收链（缺测/NaN/静音规则/文件绑定）、manifest
v2 与原子发布（失败零污染）。
"""
from __future__ import annotations

import math
from pathlib import Path

import pytest

from src.features.edit.core.delivery_preset import preset_by_id
from src.features.edit.core.delivery_profile import (
    DeliveryProfile,
    MeasurementResult,
    StemRule,
    build_delivery_manifest_v2,
    measurement_rejection,
    publish_delivery_atomic,
)


def _profile(**overrides) -> DeliveryProfile:
    base = dict(
        profile_id="netflix-example-v1",
        name="示例客户规范",
        verification="unverified",
        measurement_mode="programme",
        target_lufs=-27.0,
        tolerance_lu=2.0,
        max_true_peak_dbtp=-2.0,
        max_sample_peak_dbfs=-0.3,
        channel_order=("L", "R"),
        sample_rate=48000,
        bit_depth=24,
        stems=(
            StemRule(stem_id="FullMix"),
            StemRule(stem_id="FX", allow_silence=True),
            StemRule(stem_id="ME", allow_silence=True,
                     target_lufs=-30.0),
        ),
    )
    base.update(overrides)
    return DeliveryProfile(**base)


def _complete(stem: str = "FullMix", **overrides) -> MeasurementResult:
    values = {
        "integrated_lufs": -27.0,
        "true_peak_dbtp": -2.4,
        "peak_dbfs": -1.2,
    }
    # 整体替换（测试需要能精确控制缺哪些键）
    values = dict(overrides.pop("measurements", values))
    base = dict(
        stem_id=stem,
        status="complete",
        file_sha256="abc123",
        sample_rate=48000,
        channel_order=("L", "R"),
        start_sample=0,
        sample_count=48000 * 60,
        algorithm_version="bs1770-5/1.0",
        measurements=values,
    )
    base.update(overrides)
    return MeasurementResult(**base)


class TestDeliveryProfileModel:
    def test_roundtrip_and_unknown_field_preserved(self) -> None:
        profile = _profile(extra={"vendor_note": "keep me",
                                  "nested": {"a": 1}})
        payload = profile.to_dict()
        assert payload["vendor_note"] == "keep me"
        restored = DeliveryProfile.from_dict(payload)
        assert restored.extra.get("vendor_note") == "keep me"
        assert restored.extra.get("nested") == {"a": 1}
        assert restored.to_dict() == payload

    def test_invalid_mode_and_verification_rejected(self) -> None:
        with pytest.raises(ValueError):
            _profile(measurement_mode="loud")
        with pytest.raises(ValueError):
            _profile(verification="certified-by-nobody")

    def test_cinema_profile_without_lufs_target(self) -> None:
        """影院约定：无节目 LUFS 目标 → integrated 不必测，峰值仍必测。"""
        profile = _profile(profile_id="cinema-customer-v2",
                           verification="customer_custom",
                           target_lufs=None)
        required = profile.required_measurements_for("FullMix")
        assert "integrated_lufs" not in required
        assert "true_peak_dbtp" in required and "peak_dbfs" in required

    def test_stem_rule_override_requires(self) -> None:
        profile = _profile()
        # ME 显式目标 -30 → 仍需 integrated；FX 无覆盖 → 跟随 profile。
        assert "integrated_lufs" in profile.required_measurements_for("ME")
        assert "integrated_lufs" in profile.required_measurements_for("FX")

    def test_legacy_preset_migration_keeps_targets(self) -> None:
        preset = preset_by_id("broadcast-ebu-r128-v1")
        profile = DeliveryProfile.from_preset(preset)
        assert profile.verification == "legacy"
        assert profile.target_lufs == -23.0
        assert profile.extra["legacy_preset"]["preset_id"] == \
            "broadcast-ebu-r128-v1"
        # FX/ME 允许静音（国际版规则），FullMix 不允许
        assert profile.allows_silence("FX") and profile.allows_silence("ME")
        assert not profile.allows_silence("FullMix")


class TestMeasurementRejection:
    def test_complete_result_accepted(self) -> None:
        profile = _profile()
        assert measurement_rejection(
            _complete(), profile, expected_sha256="abc123") is None

    def test_failed_pending_cancelled_unusable(self) -> None:
        profile = _profile()
        for status in ("failed", "cancelled", "pending"):
            result = _complete(status=status)
            reason = measurement_rejection(result, profile)
            assert reason is not None and status in reason

    def test_missing_key_rejected(self) -> None:
        profile = _profile()
        result = _complete(measurements={"integrated_lufs": -27.0})
        reason = measurement_rejection(result, profile)
        assert reason is not None and "true_peak_dbtp" in reason

    def test_nan_inf_bool_str_rejected(self) -> None:
        profile = _profile()
        for bad in (float("nan"), float("inf"), True, "-2.0"):
            result = _complete(measurements={
                "integrated_lufs": -27.0,
                "true_peak_dbtp": bad,
                "peak_dbfs": -1.2,
            })
            reason = measurement_rejection(result, profile)
            assert reason is not None and "true_peak_dbtp" in reason, bad

    def test_stale_file_hash_rejected(self) -> None:
        profile = _profile()
        reason = measurement_rejection(
            _complete(), profile, expected_sha256="deadbeef")
        assert reason is not None and "different file" in reason

    def test_layout_and_rate_mismatch_rejected(self) -> None:
        profile = _profile()
        assert measurement_rejection(
            _complete(sample_rate=44100), profile) is not None
        assert measurement_rejection(
            _complete(channel_order=("L", "R", "C")), profile) is not None

    def test_silence_rules(self) -> None:
        profile = _profile()
        # FullMix 不允许静音；FX 允许（样本数>0）；空样本不是静音是失败。
        silent = _complete(status="silence", measurements={})
        assert measurement_rejection(silent, profile) is not None
        fx_silent = _complete(stem="FX", status="silence", measurements={})
        assert measurement_rejection(fx_silent, profile) is None
        empty = _complete(stem="FX", status="silence", sample_count=0,
                          measurements={})
        reason = measurement_rejection(empty, profile)
        assert reason is not None and "zero samples" in reason


class TestManifestAndPublish:
    def test_manifest_v2_binds_profile_files_and_measurements(self) -> None:
        profile = _profile()
        manifest = build_delivery_manifest_v2(
            profile,
            sound_hash="snd-123",
            files={"FullMix": {"path": "FullMix.wav", "sha256": "abc",
                               "sample_count": 2880000, "bytes": 172800060}},
            measurements={"FullMix": _complete()},
            unsupported=("object_audio",),
        )
        assert manifest["manifest_schema"] == "topos.delivery_manifest.v2"
        assert manifest["profile"]["profile_id"] == "netflix-example-v1"
        assert manifest["sound_hash"] == "snd-123"
        entry = manifest["files"][0]
        assert entry["sha256"] == "abc"
        assert entry["measurement"]["status"] == "complete"
        assert manifest["unsupported"] == ["object_audio"]

    def test_publish_atomic_success(self, tmp_path: Path) -> None:
        src = tmp_path / "FullMix.wav"
        src.write_bytes(b"PCM")
        out_dir = tmp_path / "delivery"
        published = publish_delivery_atomic({"FullMix": src}, out_dir)
        assert len(published) == 1
        assert published[0].read_bytes() == b"PCM"
        assert not list(out_dir.glob(".delivery-tmp-*"))

    def test_publish_atomic_failure_leaves_output_clean(
            self, tmp_path: Path) -> None:
        good = tmp_path / "FullMix.wav"
        good.write_bytes(b"PCM")
        missing = tmp_path / "FX.wav"  # 不存在 → 校验失败
        out_dir = tmp_path / "delivery"

        def validator(path: Path) -> None:
            if path.read_bytes() != b"PCM":
                raise ValueError("corrupt payload")

        with pytest.raises(FileNotFoundError):
            publish_delivery_atomic(
                {"FullMix": good, "FX": missing}, out_dir,
                validator=validator)
        assert not list(out_dir.iterdir())  # 零污染
        # 校验器拒绝坏内容同样零污染
        bad = tmp_path / "bad.wav"
        bad.write_bytes(b"BAD")
        (out_dir / "FullMix.wav").write_bytes(b"OLD")  # 已有旧文件不被覆盖
        with pytest.raises(ValueError):
            publish_delivery_atomic({"FullMix": bad}, out_dir,
                                    validator=validator)
        assert (out_dir / "FullMix.wav").read_bytes() == b"OLD"
        assert not list(out_dir.glob(".delivery-tmp-*"))
