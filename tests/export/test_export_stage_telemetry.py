"""T-P0-20：统一导出阶段 telemetry 与 GPU truthfulness 聚焦测试。

覆盖总计划任务卡的验证要求（四类 fake/真实路径的 schema 判定）：
1. CPU-only 路径：full_gpu=false，blockers 指明每段 CPU backend 与
   ``cpu_encoder_surface_path`` 标记；
2. GPU render + CPU encode：full_gpu=false（encode CPU + 显式 CPU 路径标记）；
3. Native surface 全链路：full_gpu=true 且 JSON 可回读；
4. GPU 初始化失败：fallback 根因 first-wins（后续症状不覆盖），
   归因保留 frame index 与 origin。
另验证：TimelineExporter._record_gpu_stage/_record_gpu_fallback 同写
legacy trail 与统一 telemetry；未知阶段名拒绝；wall p50/p95 聚合。
"""

from __future__ import annotations

import json

import pytest

from src.shared.export.export_stage_telemetry import (
    EXPORT_STAGE_NAMES,
    ExportTelemetryRecorder,
)
from src.shared.export.gpu_trail import ExportGpuTrail


class TestCpuOnlyPath:
    def test_full_gpu_false_with_blockers(self) -> None:
        rec = ExportTelemetryRecorder("cpu-only", pipeline="timeline")
        rec.record_stage_sample("decode", "cpu_swscale", wall_ms=2.0,
                                frame_index=0)
        rec.record_stage_sample("render", "cpu", wall_ms=5.0)
        rec.record_stage_sample("encode_submit", "cpu_encoder")
        rec.record_stage_sample("encode", "h264", wall_ms=3.0)
        rec.record_stage_sample("mux", "container_writer", wall_ms=10.0)
        rec.set_fallback_root_cause("cpu_encoder_surface_path")
        report = rec.snapshot()
        assert report.full_gpu is False
        blockers = "\n".join(report.full_gpu_blockers)
        assert "stage_backend_cpu:decode:cpu_swscale" in blockers
        assert "stage_backend_cpu:render:cpu" in blockers
        assert "cpu_path_marker:cpu_encoder_surface_path" in blockers
        assert "encode_backend_cpu:h264" in blockers
        data = report.to_dict()
        assert data["fallback_root_cause"] == "cpu_encoder_surface_path"
        assert data["schema"] == "topos-export-stage-telemetry/1"


class TestGpuRenderCpuEncode:
    def test_mixed_path_not_full_gpu(self) -> None:
        rec = ExportTelemetryRecorder("mixed")
        rec.record_stage_sample("decode", "videotoolbox", wall_ms=1.0)
        rec.record_stage_sample("render", "metal", wall_ms=2.0)
        rec.record_stage_sample("encode_submit", "cpu_encoder")
        rec.record_stage_sample("encode", "h264", wall_ms=4.0)
        rec.set_fallback_root_cause("cpu_encoder_surface_path")
        report = rec.snapshot()
        assert report.full_gpu is False
        blockers = report.full_gpu_blockers
        assert "encode_backend_cpu:h264" in blockers
        assert "cpu_path_marker:cpu_encoder_surface_path" in blockers
        # decode/render 是 GPU 路径，不应被标记；encode_submit CPU 显式标记
        assert "stage_backend_cpu:encode_submit:cpu_encoder" in blockers
        assert not any(
            b.startswith("stage_backend_cpu:decode")
            or b.startswith("stage_backend_cpu:render")
            for b in blockers)


class TestNativeSurfacePath:
    def test_full_gpu_true_and_roundtrip(self) -> None:
        rec = ExportTelemetryRecorder("native")
        for i in range(4):
            rec.record_stage_sample("decode", "videotoolbox",
                                    wall_ms=2.0 + i,
                                    gpu_submit_ms=1.0, frame_index=i)
            rec.record_stage_sample("render", "metal", wall_ms=3.0,
                                    gpu_submit_ms=1.5)
            rec.record_stage_sample("encode_submit", "videotoolbox",
                                    gpu_submit_ms=0.5)
            rec.record_stage_sample("encode", "videotoolbox", wall_ms=2.0)
        rec.record_stage_sample("mux", "container_writer", wall_ms=12.0)
        report = rec.snapshot()
        assert report.full_gpu is True
        assert report.full_gpu_blockers == ()
        data = json.loads(report.to_json())
        assert data["full_gpu"] is True
        by_name = {s["name"]: s for s in data["stages"]}
        assert by_name["decode"]["sample_count"] == 4
        assert by_name["decode"]["wall_p50_ms"] is not None
        assert by_name["decode"]["wall_p95_ms"] is not None
        assert by_name["render"]["gpu_submit_p95_ms"] == pytest.approx(1.5)
        # 词表完备：9 个规范阶段都出现（未采样的也是显式 backend="none"）
        assert set(by_name) == set(EXPORT_STAGE_NAMES)


class TestFallbackRootCause:
    def test_first_cause_wins_with_attribution(self) -> None:
        rec = ExportTelemetryRecorder("gpu-fail")
        rec.set_fallback_root_cause(
            "metal_context_init_failed", stage="render",
            frame_index=3, origin="gpu_export_renderer",
        )
        rec.set_fallback_root_cause("cpu_fallback", stage="decode",
                                    frame_index=4)
        report = rec.snapshot()
        assert report.fallback_root_cause == "metal_context_init_failed"
        assert len(report.frame_attributions) == 2
        first = report.frame_attributions[0]
        assert first["frame_index"] == 3
        assert first["origin"] == "gpu_export_renderer"
        blockers = report.full_gpu_blockers
        assert "fallback_root_cause:metal_context_init_failed" in blockers

    def test_empty_reason_rejected(self) -> None:
        rec = ExportTelemetryRecorder("x")
        with pytest.raises(ValueError):
            rec.set_fallback_root_cause("")

    def test_unknown_stage_rejected(self) -> None:
        rec = ExportTelemetryRecorder("x")
        with pytest.raises(ValueError):
            rec.record_stage_sample("not_a_stage", "cpu")

    def test_transfer_bytes_rejected_negative(self) -> None:
        rec = ExportTelemetryRecorder("x")
        with pytest.raises(ValueError):
            rec.record_stage_sample("decode", "cpu", cpu_upload_bytes=-1)


class TestTimelineExporterWiring:
    def _exporter(self):
        from src.features.edit.core.timeline_export import TimelineExporter

        exporter = TimelineExporter.__new__(TimelineExporter)
        exporter._gpu_trail = ExportGpuTrail("unit")
        exporter._export_telemetry = ExportTelemetryRecorder("unit")
        exporter._current_frame = 7
        return exporter

    def test_record_stage_feeds_both_trails(self) -> None:
        exporter = self._exporter()
        exporter._record_gpu_stage("decode", "cpu_fallback", wall_ms=1.5)
        legacy = exporter.gpu_trail_snapshot
        assert legacy.stage("decode") is not None
        unified = exporter.export_stage_telemetry_snapshot
        decode = next(s for s in unified["stages"] if s["name"] == "decode")
        assert decode["backend"] == "cpu_fallback"
        assert decode["wall_p50_ms"] == pytest.approx(1.5)

    def test_fallback_recorded_once_in_telemetry(self) -> None:
        exporter = self._exporter()
        exporter._record_gpu_fallback("cpu_encoder_surface_path")
        exporter._record_gpu_fallback("metal_frame_not_available")
        unified = exporter.export_stage_telemetry_snapshot
        assert unified["fallback_root_cause"] == "cpu_encoder_surface_path"
        # legacy trail 保持原语义（末次写入），供 Windows 门禁兼容
        assert exporter.gpu_trail_snapshot.fallback_reason == (
            "metal_frame_not_available")
        attributions = unified["frame_attributions"]
        assert attributions and attributions[0]["frame_index"] == 7

    def test_diagnostic_stage_name_not_in_canonical_vocabulary(self) -> None:
        """trail 的诊断段（如 yuv_conversion）写入统一 telemetry 时被忽略。"""
        exporter = self._exporter()
        exporter._record_gpu_stage("yuv_conversion", "opengl", wall_ms=1.0)
        unified = exporter.export_stage_telemetry_snapshot
        names = {s["name"] for s in unified["stages"]}
        assert "yuv_conversion" not in names
        assert exporter.gpu_trail_snapshot.stage("yuv_conversion") is not None
