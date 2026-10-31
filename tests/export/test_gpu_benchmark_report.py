from __future__ import annotations

import json
import platform

import pytest

from src.shared.export.gpu_benchmark_report import (
    MacOSGpuBenchmarkReport,
    build_input_manifest,
)
from src.shared.export.gpu_trail import ExportGpuTrail, TrailStage


def _trail() -> object:
    trail = ExportGpuTrail("report-test")
    for name in ("decode", "render", "yuv_conversion", "encode_submit"):
        trail.record_stage(TrailStage(name=name, backend="metal", gpu_submit_ms=1.0))
    return trail.snapshot()


@pytest.mark.skipif(platform.system() != "Darwin", reason="report is macOS-only")
def test_report_contains_required_sections_and_round_trips(tmp_path) -> None:
    source = tmp_path / "input.bin"
    source.write_bytes(b"gpu-plan")
    report = MacOSGpuBenchmarkReport(
        environment={"system": "Darwin"},
        inputs=[build_input_manifest(source)],
        gpu_trail=_trail(),
    )
    output = report.write_json(tmp_path / "report.json")
    report.write_markdown(tmp_path / "report.md")
    payload = json.loads(output.read_text())
    assert payload["full_gpu"] is True
    assert payload["full_gpu_claim_reason"] == ""
    assert "cpu_evidence" in payload


def test_report_fails_closed_without_trail() -> None:
    report = MacOSGpuBenchmarkReport(
        environment={"system": "Darwin"}, inputs=[{"file_name": "x"}]
    )
    assert report.full_gpu_claim_reason == "gpu_trail_missing"
    report.validate()
    assert report.is_full_gpu is False
