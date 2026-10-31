"""T-P1-11：TOPOS CPU 编码器并行策略与 native 阶段统计聚焦测试。

任务卡验证要求：
1. slice_rows 默认策略——T-P1-11 编码阶段矩阵（entropy 为第一大阶段，
   rows=8 是 4:2:2 的稳定默认）；Windows 4K 4:4:4 实测后自适应到 rows=4；
   显式覆盖保持；图片序列编码器维持 16（无矩阵证据不随动）。
2. native ``encode_stage_stats`` 分组口径——worker CPU 累计与墙钟分桶
   显式区分（多线程下 CPU 累计可超墙钟，禁止混比）。
3. telemetry 报告承载 native 统计（``native_encode_stage_stats`` 字段，
   缺省 None 不破坏旧 schema 消费方）。
4. 默认策略真实编码 + native 解码 roundtrip（码值平面均值不回退）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.shared.codec.topos_binding import (
    ENCODE_STAGE_CPU_FIELDS,
    ENCODE_STAGE_WALL_FIELDS,
    group_native_encode_stage_stats,
)
from src.shared.export.export_stage_telemetry import ExportTelemetryRecorder
from src.shared.export.topos_encoder import (
    ToposImageSequenceEncoder,
    ToposVideoEncoder,
)
from src.shared.export.video_encoder import FFmpegEncoderConfig
from src.shared.media.metadata import Rational

REPO_ROOT = Path(__file__).resolve().parents[2]
TOPOS_LIB = REPO_ROOT / "native" / "topos_codec" / "build" / "release" / \
    "topos_codec.0.1.0.dylib"
TOPOS_SOURCE = REPO_ROOT / "tests" / "test_videos" / "2K_topos_422_Proxy.mov"

_native_available = TOPOS_LIB.is_file()
_source_available = TOPOS_SOURCE.is_file()


def _video_config(
    *,
    width: int = 64,
    height: int = 64,
    pix_fmt: str = "yuv422p10le",
    profile: str = "proxy",
) -> FFmpegEncoderConfig:
    return FFmpegEncoderConfig(
        codec="topos",
        width=width,
        height=height,
        fps=Rational(24, 1),
        pix_fmt=pix_fmt,
        profile=profile,
        crf=20,
        threads=0,
        pipelined_conversion=False,
        color_metadata=None,
    )


class TestSliceRowsDefaultPolicy:
    def test_video_encoder_defaults_to_rows_8(self) -> None:
        encoder = ToposVideoEncoder("/tmp/tp111_unused.mov", _video_config())
        assert encoder._slice_rows == 8

    def test_none_falls_back_to_rows_8(self) -> None:
        encoder = ToposVideoEncoder("/tmp/tp111_unused.mov", _video_config(),
                                    slice_rows=None)
        assert encoder._slice_rows == 8

    def test_4k_4444_adopts_rows_4(self) -> None:
        encoder = ToposVideoEncoder(
            "/tmp/tp111_unused.mov",
            _video_config(
                width=3840,
                height=2160,
                pix_fmt="topos_yuva444p10a16",
                profile="4444",
            ),
        )
        assert encoder._slice_rows == 4

    def test_4k_422_keeps_rows_8(self) -> None:
        encoder = ToposVideoEncoder(
            "/tmp/tp111_unused.mov",
            _video_config(width=3840, height=2160),
        )
        assert encoder._slice_rows == 8

    def test_explicit_override_is_preserved(self) -> None:
        encoder = ToposVideoEncoder("/tmp/tp111_unused.mov", _video_config(),
                                    slice_rows=32)
        assert encoder._slice_rows == 32

    def test_image_sequence_encoder_keeps_rows_16(self) -> None:
        # 图片序列容器未纳入 T-P1-11 矩阵——无证据不随动默认。
        encoder = ToposImageSequenceEncoder("/tmp/tp111_seq", _video_config())
        assert encoder._slice_rows == 16


class TestNativeStatsGrouping:
    def test_groups_cpu_wall_and_counters(self) -> None:
        raw = {"pad_ns": 1, "fill_ns": 2, "entropy_ns": 3, "crc_ns": 4,
               "copy_ns": 5, "asm_ns": 6, "dct_ns": 7, "probe_fill_ns": 8,
               "prep_wall_ns": 9, "probe_wall_ns": 10, "final_wall_ns": 11,
               "check_wall_ns": 12, "slices": 13, "blocks": 14}
        grouped = group_native_encode_stage_stats(raw)
        assert grouped["worker_cpu_ns"] == {
            "pad_ns": 1, "fill_ns": 2, "entropy_ns": 3, "crc_ns": 4,
            "copy_ns": 5, "asm_ns": 6, "dct_ns": 7, "probe_fill_ns": 8}
        assert grouped["wall_ns"] == {
            "prep_wall_ns": 9, "probe_wall_ns": 10, "final_wall_ns": 11,
            "check_wall_ns": 12}
        assert grouped["counters"] == {"slices": 13, "blocks": 14}
        assert grouped["worker_cpu_ns_total"] == 36
        assert grouped["wall_ns_total"] == 42

    def test_missing_fields_default_to_zero(self) -> None:
        grouped = group_native_encode_stage_stats({})
        assert grouped["worker_cpu_ns_total"] == 0
        assert grouped["wall_ns_total"] == 0
        assert set(grouped["worker_cpu_ns"]) == set(ENCODE_STAGE_CPU_FIELDS)
        assert set(grouped["wall_ns"]) == set(ENCODE_STAGE_WALL_FIELDS)

    def test_accepts_stats_dataclass(self) -> None:
        from src.shared.codec.topos_binding import ToposEncodeStageStats

        stats = ToposEncodeStageStats(
            pad_ns=1, fill_ns=2, entropy_ns=3, crc_ns=4, copy_ns=5, asm_ns=6,
            slices=7, blocks=8, dct_ns=9, probe_fill_ns=10, prep_wall_ns=11,
            probe_wall_ns=12, final_wall_ns=13, check_wall_ns=14)
        grouped = group_native_encode_stage_stats(stats)
        assert grouped["worker_cpu_ns"]["entropy_ns"] == 3
        assert grouped["counters"] == {"slices": 7, "blocks": 8}


class TestTelemetryCarriesNativeStats:
    def test_default_report_field_is_none(self) -> None:
        report = ExportTelemetryRecorder("t").snapshot()
        assert report.native_encode_stage_stats is None
        assert "native_encode_stage_stats" in report.to_dict()

    def test_setter_flows_into_snapshot(self) -> None:
        rec = ExportTelemetryRecorder("t")
        rec.set_native_encode_stage_stats({"worker_cpu_ns_total": 1})
        snapshot = rec.snapshot()
        assert snapshot.native_encode_stage_stats == {"worker_cpu_ns_total": 1}
        assert snapshot.to_dict()["native_encode_stage_stats"] == \
            {"worker_cpu_ns_total": 1}


@pytest.mark.skipif(not (_native_available and _source_available),
                    reason="需要 native dylib 与 2K TOPOS 源")
class TestDefaultPolicyRealRoundtrip:
    def test_encode_decode_roundtrip_under_default_rows(self,
                                                        tmp_path: Path) -> None:
        """默认 rows=8 的真实编码 + native 解码对拍（码值均值守恒）。"""
        from src.shared.media.topos_source import ToposMediaSource

        output = tmp_path / "default_rows.mov"
        source = ToposMediaSource(str(TOPOS_SOURCE), decode_role="export")
        source.open()
        try:
            meta = source.probe()
            frame = source.read_frame(0)
            config = FFmpegEncoderConfig(
                codec="topos",
                width=int(meta.width),
                height=int(meta.height),
                fps=Rational(int(meta.fps.num), int(meta.fps.den)),
                pix_fmt="yuv422p10le",
                profile="proxy",
                crf=20,
                threads=0,
                pipelined_conversion=False,
                color_metadata=None,
            )
            encoder = ToposVideoEncoder(str(output), config)
            assert encoder._slice_rows == 8
            encoder.open()
            try:
                for _ in range(4):
                    encoder.encode_frame(frame)
            finally:
                encoder.close()
        finally:
            source.close()

        assert output.exists()
        decoded_source = ToposMediaSource(str(output), decode_role="export")
        decoded_source.open()
        try:
            decoded = decoded_source.read_frame(0)
        finally:
            decoded_source.close()
        for reference, produced in zip(frame.planes, decoded.planes):
            assert abs(float(reference.mean()) - float(produced.mean())) <= 0.5
