"""P8 measured delivery-quality gate tests."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.edit_core

from src.features.edit.ai_editing.delivery_quality_gate import (
    DeliveryQualityInput,
    SILENCE_POLICY_HARD_MAX,
    evaluate_delivery_quality,
)


def test_empty_probe_is_not_blocked_until_a_measurement_is_available() -> None:
    result = evaluate_delivery_quality(DeliveryQualityInput())
    assert result.passed
    assert result.blocking == ()


def test_video_audio_subtitle_and_export_failures_are_blocking() -> None:
    result = evaluate_delivery_quality(
        DeliveryQualityInput(
            project_references_valid=False,
            unresolved_provider_handles=1,
            black_frame_count=2,
            frozen_frame_count=1,
            audio_present=False,
            subtitle_overlaps=1,
            unknown_license_count=1,
            platform_errors=("file too large",),
            output_reopenable=False,
        )
    )
    assert not result.passed
    assert len(result.blocking) == 9
    assert all(item.code.value == "TAEP_QUALITY_GATE_FAILED" for item in result.diagnostics)


def test_target_specs_and_reading_speed_pass_when_within_limits() -> None:
    result = evaluate_delivery_quality(
        DeliveryQualityInput(
            resolution=(1920, 1080),
            expected_resolution=(1920, 1080),
            fps="24/1",
            expected_fps="24/1",
            duration_seconds=30.1,
            expected_duration_seconds=30.0,
            audio_present=True,
            audio_sample_rate=48000,
            expected_audio_sample_rate=48000,
            peak_db=-1.0,
            subtitle_reading_words_per_second=12.0,
            output_exists=True,
            output_reopenable=True,
            output_hash="abc123",
            manifest_present=True,
            thumbnail_present=True,
        )
    )
    assert result.passed


def test_subtitle_speed_and_unknown_license_fail() -> None:
    result = evaluate_delivery_quality(
        DeliveryQualityInput(
            subtitle_reading_words_per_second=21.0,
            unknown_license_count=1,
        )
    )
    assert not result.passed
    assert "字幕阅读速度" in result.blocking[0]


def test_target_lufs_is_optional_but_hard_when_requested() -> None:
    measured = evaluate_delivery_quality(
        DeliveryQualityInput(
            integrated_lufs=-23.2,
            loudness_backend_available=True,
            expected_lufs=-23.0,
            lufs_tolerance=0.5,
        )
    )
    assert measured.passed

    wrong_target = evaluate_delivery_quality(
        DeliveryQualityInput(
            integrated_lufs=-18.0,
            loudness_backend_available=True,
            expected_lufs=-23.0,
            lufs_tolerance=1.0,
        )
    )
    assert not wrong_target.passed
    assert "integrated LUFS" in wrong_target.blocking[0]


def test_target_lufs_fails_closed_without_backend() -> None:
    result = evaluate_delivery_quality(
        DeliveryQualityInput(
            integrated_lufs=-23.0,
            loudness_backend_available=False,
            expected_lufs=-23.0,
        )
    )
    assert not result.passed
    assert "LUFS 分析后端" in result.blocking[0]


def test_silence_policy_is_measure_only_by_default() -> None:
    result = evaluate_delivery_quality(
        DeliveryQualityInput(silence_seconds=120.0)
    )
    assert result.passed


def test_silence_hard_gate_requires_measurement_and_threshold() -> None:
    missing = evaluate_delivery_quality(
        DeliveryQualityInput(silence_policy=SILENCE_POLICY_HARD_MAX)
    )
    assert not missing.passed
    assert any("最大静音时长" in item for item in missing.blocking)
    assert "静音硬门缺少测量结果" in missing.blocking

    exceeded = evaluate_delivery_quality(
        DeliveryQualityInput(
            silence_policy=SILENCE_POLICY_HARD_MAX,
            max_silence_seconds=2.0,
            silence_seconds=2.1,
        )
    )
    assert not exceeded.passed
    assert "静音段超过允许时长" in exceeded.blocking

    accepted = evaluate_delivery_quality(
        DeliveryQualityInput(
            silence_policy=SILENCE_POLICY_HARD_MAX,
            max_silence_seconds=2.0,
            silence_seconds=2.0,
        )
    )
    assert accepted.passed


def test_brand_element_contract_failures_are_blocking() -> None:
    result = evaluate_delivery_quality(
        DeliveryQualityInput(
            missing_brand_elements=("logo-main",),
            brand_element_out_of_range=1,
            brand_element_safe_area_violations=1,
            brand_element_duration_violations=1,
            brand_element_invalid_measurements=1,
        )
    )

    assert not result.passed
    assert "缺少品牌必需元素" in result.blocking
    assert "品牌元素超出时间线范围" in result.blocking
    assert "品牌元素超出安全区" in result.blocking
    assert "品牌元素展示时长不足" in result.blocking
    assert "品牌元素缺少有效测量" in result.blocking
