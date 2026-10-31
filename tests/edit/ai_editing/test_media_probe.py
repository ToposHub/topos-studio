"""P8 ffprobe adapter tests using a deterministic subprocess fixture."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.edit_core

from src.features.edit.ai_editing.export_service import ExportService
from src.features.edit.ai_editing.delivery_quality_gate import DeliveryQualityInput, DeliveryQualityResult
from src.features.edit.ai_editing.delivery_analyzer import DeliveryAnalysisResult
from src.features.edit.ai_editing.export_service import materialize_export_artifacts
from src.features.edit.ai_editing.jobs import JobKind, JobRegistry, JobStatus
from src.features.edit.ai_editing.media_probe import (
    FfprobeMediaProbe,
    MediaProbeResult,
    RealMediaMatrixSpec,
    build_real_matrix_case_from_probe,
    run_real_media_matrix,
)


def _runner(*_args, **_kwargs):
    return SimpleNamespace(
        returncode=0,
        stdout='{"streams": [{"codec_type": "video", "codec_name": "h264", "width": 1920, "height": 1080, "r_frame_rate": "24/1"}, {"codec_type": "audio", "sample_rate": "48000"}], "format": {"duration": "30.0"}}',
    )


def test_ffprobe_measurements_feed_delivery_gate(tmp_path) -> None:
    output = tmp_path / "out.mp4"
    output.write_bytes(b"fixture")
    probe = FfprobeMediaProbe(runner=_runner)

    result, quality = probe.probe_and_evaluate(
        output,
        expected_resolution=(1920, 1080),
        expected_fps="24/1",
        expected_duration_seconds=30.0,
        expected_audio_sample_rate=48000,
    )
    assert result.succeeded
    assert quality.passed
    assert len(result.quality_input.output_hash) == 64


def test_probe_failure_is_visible_and_cannot_finalize_export(tmp_path) -> None:
    output = tmp_path / "out.mp4"
    output.write_bytes(b"fixture")
    probe = FfprobeMediaProbe(
        runner=lambda *_args, **_kwargs: SimpleNamespace(returncode=1, stdout=""),
    )
    registry = JobRegistry()
    service = ExportService(registry)
    job = registry.submit(JobKind.EXPORT, label="fixture")
    assert not service.finalize_export_with_probe(
        job, {"output": str(output)}, probe,
    )
    assert job.status == JobStatus.FAILED
    assert job.error_code == "EXPORT_QUALITY_GATE_FAILED"


def test_probe_can_skip_hash_for_real_media_matrix(tmp_path) -> None:
    output = tmp_path / "out.mp4"
    output.write_bytes(b"fixture")
    probe = FfprobeMediaProbe(runner=_runner, include_output_hash=False)

    result = probe.probe(output)

    assert result.succeeded
    assert result.quality_input.output_hash is None
    assert result.metadata["video_codec"] == "h264"
    assert result.metadata["resolution"] == [1920, 1080]


def test_probe_success_flag_is_a_hard_requirement(tmp_path) -> None:
    output = tmp_path / "out.mp4"
    output.write_bytes(b"fixture")

    class LyingProbe:
        def probe_and_evaluate(self, *_args, **_kwargs):
            quality = DeliveryQualityInput(
                output_exists=True,
                output_reopenable=True,
                output_hash="valid-hash",
            )
            return MediaProbeResult(False, quality, "PROBE_FAILED"), DeliveryQualityResult(True)

    registry = JobRegistry()
    service = ExportService(registry)
    job = registry.submit(JobKind.EXPORT, label="lying probe")

    assert not service.finalize_export_with_probe(job, {"output": str(output)}, LyingProbe())
    assert job.status == JobStatus.FAILED
    assert job.error_code == "EXPORT_QUALITY_GATE_FAILED"


def test_real_matrix_case_uses_hash_and_quality_evidence(tmp_path) -> None:
    output = tmp_path / "out.mp4"
    output.write_bytes(b"fixture")
    case = build_real_matrix_case_from_probe(
        output,
        probe=FfprobeMediaProbe(runner=_runner),
        name="ad-web-h264",
        category="dataset",
        task_type="product_ad",
        platform="web",
        codec="h264",
        provider_id="export-service",
        expected_resolution=(1920, 1080),
        expected_fps="24/1",
        expected_duration_seconds=30.0,
        expected_audio_sample_rate=48000,
    )

    assert case.passed
    assert case.has_real_evidence
    assert case.evidence_ref.startswith("sha256:")
    assert str(output) not in case.to_dict()["error_message"]


def test_real_matrix_case_keeps_quality_failure_as_real_failed_case(tmp_path) -> None:
    output = tmp_path / "out.mp4"
    output.write_bytes(b"fixture")
    case = build_real_matrix_case_from_probe(
        output,
        probe=FfprobeMediaProbe(runner=_runner),
        name="ad-web-h264-bad-spec",
        category="fault",
        task_type="product_ad",
        platform="web",
        codec="h264",
        provider_id="export-service",
        expected_resolution=(3840, 2160),
    )

    assert not case.passed
    assert case.has_real_evidence
    assert case.error_code == "QUALITY_GATE_FAILED"


def test_real_matrix_case_blocks_codec_mismatch(tmp_path) -> None:
    output = tmp_path / "out.mp4"
    output.write_bytes(b"fixture")
    case = build_real_matrix_case_from_probe(
        output,
        probe=FfprobeMediaProbe(runner=_runner),
        name="ad-web-h265-mismatch",
        category="fault",
        task_type="product_ad",
        platform="web",
        codec="h265",
        provider_id="export-service",
    )

    assert not case.passed
    assert case.error_code == "CODEC_MISMATCH"


def test_real_matrix_case_probe_failure_has_no_fake_evidence(tmp_path) -> None:
    output = tmp_path / "out.mp4"
    output.write_bytes(b"fixture")
    case = build_real_matrix_case_from_probe(
        output,
        probe=FfprobeMediaProbe(
            runner=lambda *_args, **_kwargs: SimpleNamespace(returncode=1, stdout=""),
        ),
        name="ad-web-h264-failed-probe",
        category="fault",
        task_type="product_ad",
        platform="web",
        codec="h264",
        provider_id="export-service",
    )

    assert not case.passed
    assert not case.has_real_evidence
    assert case.error_code == "PROBE_FAILED"


def test_run_real_media_matrix_aggregates_full_commercial_coverage(tmp_path) -> None:
    specs = (
        RealMediaMatrixSpec(
            "ad-web-h264", "dataset", tmp_path / "ad-web-h264.mp4",
            "product_ad", "web", "h264", "export-service",
        ),
        RealMediaMatrixSpec(
            "interview-mobile-h265", "performance", tmp_path / "interview-mobile-h265.mp4",
            "interview", "mobile", "h265", "export-service",
        ),
        RealMediaMatrixSpec(
            "subtitle-web-h264", "fault", tmp_path / "subtitle-web-h264.mp4",
            "subtitle_multilingual", "web", "h264", "export-service",
        ),
        RealMediaMatrixSpec(
            "ad-mobile-h265", "security", tmp_path / "ad-mobile-h265.mp4",
            "product_ad", "mobile", "h265", "export-service",
        ),
    )
    for spec in specs:
        spec_path = Path(spec.output_path)
        spec_path.write_bytes(spec.name.encode("utf-8"))

    def matrix_runner(args, **_kwargs):
        filename = str(args[-1]).lower()
        codec = "hevc" if "h265" in filename else "h264"
        return SimpleNamespace(
            returncode=0,
            stdout=(
                '{"streams": [{"codec_type": "video", '
                f'"codec_name": "{codec}", "width": 1920, "height": 1080, '
                '"r_frame_rate": "24/1"}, {"codec_type": "audio", '
                '"sample_rate": "48000"}], "format": {"duration": "30.0"}}'
            ),
        )

    result = run_real_media_matrix(
        specs,
        probe=FfprobeMediaProbe(runner=matrix_runner),
    )

    assert result.passed
    assert result.real_evidence_complete
    assert result.missing_dimensions == ()
    assert all(case.passed for case in result.cases)


def test_run_real_media_matrix_preserves_failed_case_diagnostics(tmp_path) -> None:
    spec = RealMediaMatrixSpec(
        "missing-output", "fault", tmp_path / "missing.mp4",
        "product_ad", "web", "h264", "export-service",
    )

    result = run_real_media_matrix(
        (spec,),
        probe=FfprobeMediaProbe(runner=_runner),
    )

    assert not result.passed
    assert result.failed_cases == ("missing-output",)
    assert result.cases[0].error_code == "OUTPUT_NOT_FOUND"
    assert result.cases[0].evidence_ref == ""


def test_run_real_media_matrix_rejects_probe_without_hash(tmp_path) -> None:
    output = tmp_path / "out.mp4"
    output.write_bytes(b"fixture")
    spec = RealMediaMatrixSpec(
        "no-hash", "fault", output,
        "product_ad", "web", "h264", "export-service",
    )

    result = run_real_media_matrix(
        (spec,),
        probe=FfprobeMediaProbe(runner=_runner, include_output_hash=False),
    )

    assert not result.passed
    assert result.cases[0].error_code == "PROBE_HASH_MISSING"
    assert result.missing_real_evidence_cases == ("no-hash",)


def test_materialize_export_artifacts_writes_manifest_and_thumbnail(tmp_path) -> None:
    output = tmp_path / "out.mp4"
    output.write_bytes(b"fixture")

    def runner(args, **_kwargs):
        from pathlib import Path
        Path(args[-1]).write_bytes(b"jpeg")
        return SimpleNamespace(returncode=0, stdout="")

    artifacts = materialize_export_artifacts(
        output,
        metadata={"timeline_id": "t1", "output_path": str(output)},
        runner=runner,
    )

    assert artifacts.succeeded
    assert artifacts.manifest_path.endswith(".manifest.json")
    assert artifacts.thumbnail_path.endswith(".thumbnail.jpg")
    manifest_text = output.with_suffix(".manifest.json").read_text(encoding="utf-8")
    assert str(output) not in manifest_text


def test_missing_manifest_or_thumbnail_blocks_finalization(tmp_path) -> None:
    output = tmp_path / "out.mp4"
    output.write_bytes(b"fixture")

    class Probe:
        def probe_and_evaluate(self, *_args, **_kwargs):
            quality = DeliveryQualityInput(
                output_exists=True,
                output_reopenable=True,
                output_hash="valid-hash",
            )
            return MediaProbeResult(True, quality), DeliveryQualityResult(True)

    registry = JobRegistry()
    service = ExportService(registry)
    job = registry.submit(JobKind.EXPORT, label="missing artifacts")
    assert not service.finalize_export_with_probe(
        job,
        {
            "output": str(output),
            "manifest_path": str(tmp_path / "missing.manifest.json"),
            "thumbnail_path": str(tmp_path / "missing.thumbnail.jpg"),
        },
        Probe(),
    )
    assert job.error_code == "EXPORT_QUALITY_GATE_FAILED"


def test_delivery_analyzer_measurements_feed_quality_gate(tmp_path) -> None:
    output = tmp_path / "out.mp4"
    output.write_bytes(b"fixture")

    class Probe:
        def probe_and_evaluate(self, *_args, **_kwargs):
            quality = DeliveryQualityInput(
                output_exists=True,
                output_reopenable=True,
                output_hash="valid-hash",
            )
            return MediaProbeResult(True, quality), DeliveryQualityResult(True)

    class Analyzer:
        def analyze(self, *_args, **_kwargs):
            return DeliveryAnalysisResult(
                True,
                black_frame_count=1,
                sampled_frame_count=10,
            )

    registry = JobRegistry()
    service = ExportService(registry)
    job = registry.submit(JobKind.EXPORT, label="measured black frame")

    assert not service.finalize_export_with_probe(
        job, {"output": str(output)}, Probe(), quality_analyzer=Analyzer(),
    )
    assert job.error_code == "EXPORT_QUALITY_GATE_FAILED"


def test_delivery_analyzer_peak_measurement_blocks_clipping(tmp_path) -> None:
    output = tmp_path / "out.mp4"
    output.write_bytes(b"fixture")

    class Probe:
        def probe_and_evaluate(self, *_args, **_kwargs):
            quality = DeliveryQualityInput(
                output_exists=True,
                output_reopenable=True,
                output_hash="valid-hash",
            )
            return MediaProbeResult(True, quality), DeliveryQualityResult(True)

    class Analyzer:
        def analyze(self, *_args, **_kwargs):
            return DeliveryAnalysisResult(True, peak_db=1.0, sampled_frame_count=10)

    registry = JobRegistry()
    service = ExportService(registry)
    job = registry.submit(JobKind.EXPORT, label="measured clipping")

    assert not service.finalize_export_with_probe(
        job, {"output": str(output)}, Probe(), quality_analyzer=Analyzer(),
    )
    assert "削波" in (job.error_message or "")


def test_delivery_analyzer_lufs_target_blocks_wrong_mix(tmp_path) -> None:
    output = tmp_path / "out.mp4"
    output.write_bytes(b"fixture")

    class Probe:
        def probe_and_evaluate(self, *_args, **_kwargs):
            quality = DeliveryQualityInput(
                output_exists=True,
                output_reopenable=True,
                output_hash="valid-hash",
            )
            return MediaProbeResult(True, quality), DeliveryQualityResult(True)

    class Analyzer:
        def analyze(self, *_args, **_kwargs):
            return DeliveryAnalysisResult(
                True,
                integrated_lufs=-18.0,
                loudness_range_lu=3.0,
                loudness_backend_available=True,
            )

    registry = JobRegistry()
    service = ExportService(registry)
    job = registry.submit(JobKind.EXPORT, label="measured lufs")

    assert not service.finalize_export_with_probe(
        job,
        {"output": str(output)},
        Probe(),
        quality_analyzer=Analyzer(),
        expected_lufs=-23.0,
    )
    assert "integrated LUFS" in (job.error_message or "")


def test_delivery_analyzer_hard_silence_policy_blocks_long_gap(tmp_path) -> None:
    output = tmp_path / "out.mp4"
    output.write_bytes(b"fixture")

    class Probe:
        def probe_and_evaluate(self, *_args, **_kwargs):
            quality = DeliveryQualityInput(
                output_exists=True,
                output_reopenable=True,
                output_hash="valid-hash",
                audio_present=True,
                audio_sample_rate=48000,
            )
            return MediaProbeResult(True, quality), DeliveryQualityResult(True)

    class Analyzer:
        def analyze(self, *_args, **_kwargs):
            return DeliveryAnalysisResult(
                True,
                audio_present=True,
                silence_seconds=2.1,
            )

    registry = JobRegistry()
    service = ExportService(registry)
    job = registry.submit(JobKind.EXPORT, label="measured silence")

    assert not service.finalize_export_with_probe(
        job,
        {"output": str(output)},
        Probe(),
        quality_analyzer=Analyzer(),
        silence_policy="hard_max",
        max_silence_seconds=2.0,
    )
    assert "静音段超过允许时长" in (job.error_message or "")
