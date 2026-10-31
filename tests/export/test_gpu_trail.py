"""ExportGpuTrail 契约测试（计划 2026-09-15 §3.3 / §4.3）。

验收标准：
- 计数线程安全、只增不减、负数/重复登记显式报错；
- "全 GPU" 声明必须由 trail 证明：像素驻留段缺失、GPU 时间未测量、
  字节非零、存在回退 reason 时，claim 必须给出机器可读 reason；
- snapshot <-> dict 往返稳定，未知字段与错误 schema_version 拒绝。
"""

from __future__ import annotations

import threading

import pytest

from src.shared.export.gpu_trail import (
    PIXEL_RESIDENT_STAGE_NAMES,
    TRAIL_SCHEMA_VERSION,
    ExportGpuTrail,
    ExportGpuTrailSnapshot,
    TrailStage,
)


def _full_gpu_stages() -> list:
    return [
        TrailStage(
            name="decode", backend="d3d11", gpu_submit_ms=1.0,
            gpu_wait_ms=0.1,
        ),
        TrailStage(
            name="render", backend="d3d11", gpu_submit_ms=2.0,
            gpu_wait_ms=0.2,
        ),
        TrailStage(
            name="yuv_conversion", backend="d3d11", gpu_submit_ms=0.5,
            gpu_wait_ms=0.05,
        ),
        TrailStage(
            name="encode_submit", backend="amf", gpu_submit_ms=1.5,
            gpu_wait_ms=0.3,
        ),
        TrailStage(
            name="mux", backend="cpu_mp4_mux", note="压缩码流回 CPU 封装",
        ),
    ]


class TestTrailStageValidation:
    def test_empty_name_or_backend_rejected(self) -> None:
        with pytest.raises(ValueError):
            TrailStage(name="", backend="d3d11")
        with pytest.raises(ValueError):
            TrailStage(name="decode", backend="")

    def test_negative_bytes_and_times_rejected(self) -> None:
        with pytest.raises(ValueError):
            TrailStage(name="decode", backend="d3d11", gpu_submit_ms=-1.0)
        with pytest.raises(ValueError):
            TrailStage(name="decode", backend="d3d11", cpu_download_bytes=-1)
        with pytest.raises(ValueError):
            TrailStage(name="decode", backend="d3d11", cpu_upload_bytes=-4)

    def test_round_trip(self) -> None:
        stage = TrailStage(
            name="render", backend="d3d11", gpu_submit_ms=2.0,
            gpu_wait_ms=0.2, note="ok",
        )
        assert TrailStage.from_dict(stage.to_dict()) == stage

    def test_from_dict_unknown_field_rejected(self) -> None:
        with pytest.raises(ValueError, match="未知字段"):
            TrailStage.from_dict({
                "name": "render", "backend": "d3d11", "bogus": 1,
            })


class TestExportGpuTrailAccounting:
    def test_add_bytes_accumulates(self) -> None:
        trail = ExportGpuTrail("export-1", adapter_luid="L1")
        trail.add_cpu_upload(10)
        trail.add_cpu_download(20)
        snap = trail.snapshot()
        assert snap.cpu_upload_bytes == 10
        assert snap.cpu_download_bytes == 20

    def test_negative_bytes_rejected(self) -> None:
        trail = ExportGpuTrail("export-1")
        with pytest.raises(ValueError):
            trail.add_cpu_upload(-1)
        with pytest.raises(ValueError):
            trail.add_cpu_download(-1)

    def test_duplicate_stage_rejected(self) -> None:
        trail = ExportGpuTrail("export-1")
        trail.record_stage(TrailStage(name="decode", backend="d3d11"))
        with pytest.raises(ValueError, match="重复登记"):
            trail.record_stage(TrailStage(name="decode", backend="d3d11"))

    def test_stage_bytes_roll_up_into_totals(self) -> None:
        trail = ExportGpuTrail("export-1")
        trail.record_stage(TrailStage(
            name="decode", backend="cpu", cpu_download_bytes=100,
        ))
        trail.add_cpu_download(50)
        assert trail.snapshot().cpu_download_bytes == 150

    def test_concurrent_accounting_is_thread_safe(self) -> None:
        trail = ExportGpuTrail("export-1")
        threads = [
            threading.Thread(
                target=trail.add_cpu_upload, args=(1,), name=f"t{i}",
            )
            for i in range(8)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert trail.snapshot().cpu_upload_bytes == 8

    def test_empty_fallback_reason_rejected(self) -> None:
        trail = ExportGpuTrail("export-1")
        with pytest.raises(ValueError):
            trail.set_fallback_reason("")


class TestFullGpuClaim:
    def test_provable_claim_returns_empty_reason(self) -> None:
        trail = ExportGpuTrail("export-1", adapter_luid="0-0")
        for stage in _full_gpu_stages():
            trail.record_stage(stage)
        snap = trail.snapshot()
        assert snap.zero_pixel_transfer
        assert snap.full_gpu_claim_reason() == ""

    def test_missing_pixel_stage_blocks_claim(self) -> None:
        trail = ExportGpuTrail("export-1")
        for stage in _full_gpu_stages():
            if stage.name == "render":
                continue
            trail.record_stage(stage)
        assert trail.snapshot().full_gpu_claim_reason() == (
            "trail_stage_missing:render"
        )

    def test_unmeasured_gpu_time_blocks_claim(self) -> None:
        trail = ExportGpuTrail("export-1")
        for stage in _full_gpu_stages():
            if stage.name == "encode_submit":
                stage = TrailStage(
                    name="encode_submit", backend="amf",
                    gpu_submit_ms=None,
                )
            trail.record_stage(stage)
        assert trail.snapshot().full_gpu_claim_reason() == (
            "gpu_time_unmeasured:encode_submit"
        )

    def test_positive_download_blocks_claim(self) -> None:
        trail = ExportGpuTrail("export-1")
        for stage in _full_gpu_stages():
            trail.record_stage(stage)
        trail.add_cpu_download(4096)
        assert trail.snapshot().full_gpu_claim_reason() == (
            "cpu_download_bytes_positive"
        )

    def test_fallback_reason_blocks_claim(self) -> None:
        trail = ExportGpuTrail("export-1")
        for stage in _full_gpu_stages():
            trail.record_stage(stage)
        trail.set_fallback_reason("interop_not_yet_implemented")
        assert trail.snapshot().full_gpu_claim_reason() == (
            "fallback:interop_not_yet_implemented"
        )

    def test_mux_stage_does_not_block_claim(self) -> None:
        """压缩码流回 CPU 封装是计划 §4.3 明确允许的边界。"""
        trail = ExportGpuTrail("export-1")
        for stage in _full_gpu_stages():
            trail.record_stage(stage)
        assert "mux" not in PIXEL_RESIDENT_STAGE_NAMES
        assert trail.snapshot().full_gpu_claim_reason() == ""

    def test_stage_samples_materialize_percentiles(self) -> None:
        trail = ExportGpuTrail("sampled")
        for value in (1.0, 2.0, 3.0, 4.0):
            trail.record_stage_sample(
                name="decode", backend="videotoolbox",
                gpu_submit_ms=value,
            )
        stage = trail.snapshot().stage("decode")
        assert stage is not None
        assert stage.sample_count == 4
        assert stage.gpu_submit_ms == 2.5
        assert stage.gpu_submit_p50_ms == 2.0
        assert stage.gpu_submit_p95_ms == 3.0


class TestSnapshotSerialization:
    def test_round_trip_preserves_evidence(self) -> None:
        trail = ExportGpuTrail("export-9", adapter_luid="0x1-0x2")
        for stage in _full_gpu_stages():
            trail.record_stage(stage)
        snap = trail.snapshot()
        revived = ExportGpuTrailSnapshot.from_dict(snap.to_dict())
        assert revived == snap
        assert revived.full_gpu_claim_reason() == ""

    def test_wrong_schema_version_rejected(self) -> None:
        snap = ExportGpuTrail("export-1").snapshot()
        data = snap.to_dict()
        data["schema_version"] = TRAIL_SCHEMA_VERSION + 1
        with pytest.raises(ValueError, match="schema_version"):
            ExportGpuTrailSnapshot.from_dict(data)

    def test_duplicate_stages_in_dict_rejected(self) -> None:
        trail = ExportGpuTrail("export-1")
        trail.record_stage(TrailStage(name="decode", backend="d3d11"))
        snap = trail.snapshot()
        data = snap.to_dict()
        data["stages"] = [data["stages"][0], data["stages"][0]]
        with pytest.raises(ValueError, match="重复段名"):
            ExportGpuTrailSnapshot.from_dict(data)

    def test_snapshot_is_stable_view(self) -> None:
        trail = ExportGpuTrail("export-1")
        trail.record_stage(TrailStage(name="decode", backend="d3d11"))
        before = trail.snapshot()
        trail.add_cpu_upload(5)
        after = trail.snapshot()
        assert before.cpu_upload_bytes == 0
        assert after.cpu_upload_bytes == 5
