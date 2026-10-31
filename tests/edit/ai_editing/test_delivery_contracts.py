"""P8 platform and subtitle delivery-contract tests."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.edit_core

from src.features.edit.ai_editing.delivery_contracts import (
    BrandElementRequirement,
    PlatformDeliverySpec,
    build_subtitle_safe_area,
    materialize_brand_elements_from_timeline,
    validate_platform_delivery,
    validate_brand_elements,
    validate_subtitle_cues,
)
from src.features.edit.ai_editing.delivery_quality_gate import DeliveryQualityInput
from src.features.edit.ai_editing.export_service import ExportService
from src.features.edit.ai_editing.jobs import JobKind, JobRegistry, JobStatus
from src.features.edit.ai_editing.media_probe import MediaProbeResult


def _horizontal_metadata() -> dict[str, object]:
    return {
        "resolution": [1920, 1080],
        "fps": "30/1",
        "duration_seconds": 60.0,
        "file_size_bytes": 2_000_000,
        "video_bitrate_kbps": 8_000.0,
        "video_codec": "h264",
        "container": "mov,mp4,m4a,3gp,3g2,mj2",
        "audio_sample_rate": 48_000,
        "audio_present": True,
    }


def test_builtin_platform_contract_passes_measured_metadata() -> None:
    result = validate_platform_delivery("horizontal_social", metadata=_horizontal_metadata())
    assert result.passed
    assert result.errors == ()


def test_platform_contract_fails_closed_on_resolution_and_missing_measurements() -> None:
    result = validate_platform_delivery(
        PlatformDeliverySpec(
            spec_id="strict",
            allowed_resolutions=((1080, 1920),),
            allowed_fps=("30/1",),
            allowed_containers=("mp4",),
            max_video_bitrate_kbps=1_000,
        ),
        metadata={"resolution": [1920, 1080], "fps": "24/1"},
    )
    assert not result.passed
    assert any("分辨率" in error for error in result.errors)
    assert any("帧率" in error for error in result.errors)
    assert any("封装" in error for error in result.errors)
    assert any("码率" in error for error in result.errors)


def test_subtitle_contract_counts_range_overlap_empty_speed_line_and_safe_area() -> None:
    result = validate_subtitle_cues(
        (
            {"start_frame": 0, "end_frame": 10, "text": "hello"},
            {
                "start_frame": 5,
                "end_frame": 20,
                "text": "fast caption",
                "safe_area_violation": True,
            },
            {"start_frame": 90, "end_frame": 110, "text": ""},
        ),
        timeline_duration_frames=100,
        fps=10.0,
        max_words_per_second=1.0,
        max_chars_per_line=5,
    )
    assert result.cue_count == 3
    assert result.out_of_range == 1
    assert result.overlaps == 1
    assert result.empty_text == 1
    assert result.over_speed == 1
    assert result.long_lines == 1
    assert result.safe_area_violations == 1
    assert not result.passed


def test_subtitle_contract_accepts_subtitle_clip_shape() -> None:
    result = validate_subtitle_cues(
        (SimpleNamespace(timeline_start=0, timeline_duration=24, text="你好"),),
        timeline_duration_frames=48,
        fps=24.0,
    )
    assert result.passed
    assert result.max_words_per_second == 2.0


def test_subtitle_contract_checks_measured_bbox_against_safe_area() -> None:
    safe = build_subtitle_safe_area(1080, 1920, inset=0.10)
    inside = {"start_frame": 0, "end_frame": 48, "text": "你好 Hello",
              "bbox": [safe[0], safe[1], 200.0, 80.0], "safe_area": safe}
    outside = {"start_frame": 48, "end_frame": 96, "text": "世界 World",
               "bbox": [safe[0] - 1.0, safe[1], 200.0, 80.0], "safe_area": safe}

    result = validate_subtitle_cues(
        (inside, outside),
        timeline_duration_frames=120,
        fps=24.0,
    )

    assert safe == pytest.approx((108.0, 192.0, 972.0, 1728.0))
    assert result.safe_area_violations == 1
    assert not result.passed


def test_subtitle_safe_area_rejects_invalid_inset() -> None:
    with pytest.raises(ValueError):
        build_subtitle_safe_area(1920, 1080, inset=0.5)


def test_brand_contract_requires_measured_element_and_checks_duration_and_safe_area() -> None:
    safe = build_subtitle_safe_area(1920, 1080, inset=0.05)
    requirements = (
        BrandElementRequirement(
            element_id="logo-main",
            kind="logo",
            min_duration_frames=48,
        ),
        {"element_id": "end-card", "kind": "end_card", "min_duration_frames": 24},
    )
    result = validate_brand_elements(
        (
            {
                "element_id": "logo-main",
                "kind": "logo",
                "start_frame": 0,
                "end_frame": 36,
                "bbox": [safe[0], safe[1], 240.0, 80.0],
                "safe_area": safe,
            },
            {
                "element_id": "end-card",
                "kind": "end_card",
                "start_frame": 90,
                "end_frame": 120,
                "bbox": [safe[2] - 100.0, safe[3] - 40.0, 140.0, 60.0],
                "safe_area": safe,
            },
        ),
        requirements,
        timeline_duration_frames=120,
    )

    assert not result.passed
    assert result.missing == ()
    assert result.duration_violations == 1
    assert result.safe_area_violations == 1


def test_brand_contract_fails_closed_when_required_measurement_is_missing() -> None:
    result = validate_brand_elements(
        (),
        ({"element_id": "disclaimer", "kind": "disclaimer"},),
        timeline_duration_frames=120,
    )
    assert not result.passed
    assert result.missing == ("disclaimer",)


def test_brand_annotations_materialize_from_asset_source_range_to_timeline_bbox() -> None:
    clip = SimpleNamespace(
        clip_id="clip-logo",
        asset_id="asset-logo",
        track_id="track-overlay",
        timeline_start=100,
        timeline_duration=80,
        source_start=10,
        source_duration=40,
        speed=SimpleNamespace(reverse=False),
        is_enabled=True,
    )
    track = SimpleNamespace(
        track_id="track-overlay",
        is_enabled=True,
        is_visible=True,
        clips=(clip,),
    )
    sequence = SimpleNamespace(video_tracks=(track,))
    assets = {
        "asset-logo": SimpleNamespace(metadata={
            "brand_elements": ({
                "element_id": "logo-main",
                "kind": "logo",
                "source_start_frame": 20,
                "source_end_frame": 30,
                "bbox": [0.80, 0.10, 0.10, 0.20],
                "bbox_space": "canvas_normalized",
            },),
        }),
    }

    records = materialize_brand_elements_from_timeline(
        sequence,
        assets,
        expected_resolution=(1920, 1080),
        safe_area=(96.0, 54.0, 1824.0, 1026.0),
    )

    assert len(records) == 1
    assert records[0]["start_frame"] == 120
    assert records[0]["end_frame"] == 140
    assert records[0]["bbox"] == pytest.approx([1536.0, 108.0, 192.0, 216.0])
    assert records[0]["measurement_source"] == "asset_annotation"
    assert validate_brand_elements(
        records,
        ({"element_id": "logo-main", "kind": "logo"},),
        timeline_duration_frames=240,
    ).passed


def test_brand_materialization_preserves_bad_annotation_as_invalid_measurement() -> None:
    clip = SimpleNamespace(
        clip_id="clip-bad-logo", asset_id="asset-bad", track_id="track-overlay",
        timeline_start=0, timeline_duration=48, source_start=0, source_duration=48,
        speed=SimpleNamespace(reverse=False), is_enabled=True,
    )
    sequence = SimpleNamespace(video_tracks=(SimpleNamespace(
        track_id="track-overlay", is_enabled=True, is_visible=True, clips=(clip,),
    ),))
    records = materialize_brand_elements_from_timeline(
        sequence,
        {"asset-bad": SimpleNamespace(metadata={"brand_elements": ({
            "element_id": "logo-main", "kind": "logo", "bbox": [0.5, 0.5],
            "bbox_space": "canvas_normalized",
        },)})},
        expected_resolution=(1920, 1080),
        safe_area=(96.0, 54.0, 1824.0, 1026.0),
    )

    result = validate_brand_elements(
        records,
        ({"element_id": "logo-main", "kind": "logo"},),
        timeline_duration_frames=48,
    )
    assert result.invalid_measurements == 1
    assert not result.passed


def test_export_service_applies_platform_and_subtitle_contracts_before_success(tmp_path) -> None:
    output = tmp_path / "out.mp4"
    output.write_bytes(b"fixture")

    class Probe:
        def probe_and_evaluate(self, *_args, **_kwargs):
            quality = DeliveryQualityInput(
                output_exists=True,
                output_reopenable=True,
                output_hash="valid-hash",
            )
            return MediaProbeResult(
                True,
                quality,
                metadata=_horizontal_metadata(),
            ), SimpleNamespace(passed=True)

    registry = JobRegistry()
    service = ExportService(registry)
    job = registry.submit(JobKind.EXPORT, label="contract gate")
    accepted = service.finalize_export_with_probe(
        job,
        {"output": str(output)},
        Probe(),
        platform_spec="horizontal_social",
        subtitle_cues=({"start_frame": 0, "end_frame": 24, "text": "hello"},),
        subtitle_timeline_duration_frames=48,
        subtitle_fps=24.0,
    )
    assert accepted
    assert job.status == JobStatus.SUCCEEDED


def test_export_service_blocks_missing_measured_brand_element(tmp_path) -> None:
    output = tmp_path / "out.mp4"
    output.write_bytes(b"fixture")

    class Probe:
        def probe_and_evaluate(self, *_args, **_kwargs):
            quality = DeliveryQualityInput(
                output_exists=True,
                output_reopenable=True,
                output_hash="valid-hash",
            )
            return MediaProbeResult(
                True,
                quality,
                metadata=_horizontal_metadata(),
            ), SimpleNamespace(passed=True)

    registry = JobRegistry()
    service = ExportService(registry)
    job = registry.submit(JobKind.EXPORT, label="brand contract gate")
    accepted = service.finalize_export_with_probe(
        job,
        {"output": str(output)},
        Probe(),
        brand_requirements=({"element_id": "logo-main", "kind": "logo"},),
        brand_timeline_duration_frames=48,
    )

    assert not accepted
    assert job.status == JobStatus.FAILED
    assert "品牌必需元素" in (job.error_message or "")
