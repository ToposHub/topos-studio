from __future__ import annotations

from src.features.teaching_animation.core.contracts import RenderContext
from src.features.teaching_animation.render.benchmark import (
    BenchmarkFixture,
    BenchmarkSegment,
    build_reference_scene,
    run_benchmark,
)


def test_reference_fixture_is_stable_and_explicit() -> None:
    fixture = BenchmarkFixture(width=64, height=48, duration_frames=8, element_count=4)
    first = build_reference_scene(fixture)
    second = build_reference_scene(fixture)
    assert first.to_dict() == second.to_dict()
    assert fixture.fixture_hash == BenchmarkFixture(
        width=64, height=48, duration_frames=8, element_count=4,
    ).fixture_hash
    assert first.scene_id == fixture.fixture_id


def test_benchmark_records_metrics_cache_evidence_and_output_hashes() -> None:
    fixture = BenchmarkFixture(width=64, height=48, duration_frames=8, element_count=4)
    scene = build_reference_scene(fixture)
    context = RenderContext(
        fps=fixture.fps,
        width=fixture.width,
        height=fixture.height,
        render_profile="preview",
        dpr=2.0,
    )
    report = run_benchmark(scene, context, (0, 1, 2), fixture=fixture, iterations=2)
    payload = report.to_dict()
    assert {sample.metric.value for sample in report.samples} == {
        "first_frame_ms",
        "interactive_frame_ms",
        "export_fps",
        "element_update_ms",
    }
    assert report.environment.dpr == 2.0
    assert report.environment.fixture_hash == fixture.fixture_hash
    assert report.cache_stats["hits"] >= 2
    assert report.cache_stats["misses"] >= 2
    assert "p50" in payload["series"]["first_frame_ms"]
    assert any(name == "frame_0" for name, _digest in report.output_hashes)
    assert report.gate.passed is True


def test_optional_segment_serializes_explicit_status_and_percentiles() -> None:
    segment = BenchmarkSegment("html_capture", "skipped", reason="browser unavailable")
    payload = segment.to_dict()
    assert payload["status"] == "skipped"
    assert payload["reason"] == "browser unavailable"
    assert payload["p50"] is None
