"""
T-03 平面直通批次测试。

覆盖：
- Topos GBR 平面分类（此前 topos_gbrap* 误落 yuv420 兜底）
- FrameTraceCollector.annotate 迟到证据合并（planar 上传字节/模式）
- EditGpuPreviewRenderer planar 上传统计的按帧存取（多轨合并、有界）
- RenderCacheEncoder.write_frame_bgr 直入（无 BGR→RGB 翻转拷贝）
- RenderCacheEncoder.write_frame_planar 直入（平面不打包）
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import numpy as np
import pytest

from src.features.edit.core.timeline_clip_frame_provider import (
    TimelineClipFrameProvider,
)


# ---------------------------------------------------------------------------
# GBR 平面分类（T-03 修正：topos_gbrap* 此前不含 "gbrp" 子串）
# ---------------------------------------------------------------------------


class TestGbrPlanarClassification:
    def test_topos_gbrap_classified_as_rgb_planar(self):
        assert TimelineClipFrameProvider._classify_planar_upload_mode(
            "topos_gbrap10le", 4, 10
        ) == "rgb_planar"

    def test_topos_gbrap12_classified_as_rgb_planar(self):
        assert TimelineClipFrameProvider._classify_planar_upload_mode(
            "topos_gbrap12le", 4, 12
        ) == "rgb_planar"

    def test_gbrp_classified_as_rgb_planar(self):
        assert TimelineClipFrameProvider._classify_planar_upload_mode(
            "gbrp10le", 3, 10
        ) == "rgb_planar"

    def test_yuv_formats_unaffected(self):
        assert TimelineClipFrameProvider._classify_planar_upload_mode(
            "topos_yuva422p10a16", 4, 10
        ) == "yuv422"
        assert TimelineClipFrameProvider._classify_planar_upload_mode(
            "yuv444p10le", 3, 10
        ) == "yuv444"
        assert TimelineClipFrameProvider._classify_planar_upload_mode(
            "yuv420p", 3, 8
        ) == "yuv420"
        assert TimelineClipFrameProvider._classify_planar_upload_mode(
            "p010", 2, 10
        ) == "p010"


# ---------------------------------------------------------------------------
# FrameTraceCollector.annotate（GL 完成后迟到合并上传证据）
# ---------------------------------------------------------------------------


class TestFrameTraceCollectorAnnotate:
    def _make_trace(self, trace_id: int):
        from src.shared.video.playback_frame_trace import EditPlaybackFrameTrace

        return EditPlaybackFrameTrace(
            frame_index=100,
            page='edit_timeline',
            trace_id=trace_id,
        )

    def test_annotate_merges_fields_by_trace_id(self):
        from src.shared.video.playback_frame_trace import (
            FrameTraceCollector,
        )

        collector = FrameTraceCollector(max_frames=8)
        trace = self._make_trace(trace_id=42)
        collector.record(trace)

        ok = collector.annotate(
            42,
            cpu_upload_bytes=1234,
            texture_write_bytes=1234,
            import_upload_ms=3.5,
            extra={"planar_upload": {"mode": "r16ui_direct"}},
        )
        assert ok is True
        assert trace.cpu_upload_bytes == 1234
        assert trace.texture_write_bytes == 1234
        assert trace.import_upload_ms == 3.5
        assert trace.extra["planar_upload"]["mode"] == "r16ui_direct"

    def test_annotate_unknown_trace_id_returns_false(self):
        from src.shared.video.playback_frame_trace import FrameTraceCollector

        collector = FrameTraceCollector(max_frames=8)
        collector.record(self._make_trace(trace_id=1))
        assert collector.annotate(999, cpu_upload_bytes=1) is False

    def test_annotate_zero_trace_id_returns_false(self):
        from src.shared.video.playback_frame_trace import FrameTraceCollector

        collector = FrameTraceCollector(max_frames=8)
        collector.record(self._make_trace(trace_id=0))
        assert collector.annotate(0, cpu_upload_bytes=1) is False

    def test_annotate_extra_merges_without_clobbering(self):
        from src.shared.video.playback_frame_trace import FrameTraceCollector

        collector = FrameTraceCollector(max_frames=8)
        trace = self._make_trace(trace_id=7)
        trace.extra["request_kind"] = "playhead"
        collector.record(trace)

        collector.annotate(7, extra={"planar_upload": {"tracks": 2}})
        assert trace.extra["request_kind"] == "playhead"
        assert trace.extra["planar_upload"]["tracks"] == 2

    def test_annotate_reevaluates_deadline_when_unattributed(self):
        """import_upload_ms 补录后，未归因帧补跑分段归因。"""
        from src.shared.video.playback_frame_trace import FrameTraceCollector

        collector = FrameTraceCollector(max_frames=8)
        trace = self._make_trace(trace_id=9)
        trace.deadline_ns = 1_000_000  # 1ms 总预算
        collector.record(trace)
        # record() 时各段全 0，无归因
        assert trace.deadline_missed_segment == ""
        collector.annotate(9, import_upload_ms=50.0)
        assert trace.import_upload_ms == 50.0
        assert trace.deadline_missed_segment == "import_upload"

    def test_annotate_does_not_override_existing_segment(self):
        from src.shared.video.playback_frame_trace import FrameTraceCollector

        collector = FrameTraceCollector(max_frames=8)
        trace = self._make_trace(trace_id=10)
        trace.deadline_ns = 1_000_000  # 1ms
        trace.total_ms = 50.0  # record 时 total 越界 → 'total'
        collector.record(trace)
        assert trace.deadline_missed_segment == "total"
        collector.annotate(10, import_upload_ms=99.0)
        assert trace.deadline_missed_segment == "total"


# ---------------------------------------------------------------------------
# EditGpuPreviewRenderer planar 上传统计（按帧存取，不建 GL 实例）
# ---------------------------------------------------------------------------


class TestPlanarUploadStatsStore:
    def _make_renderer(self):
        from src.features.edit.core.edit_gpu_preview_renderer import (
            EditGpuPreviewRenderer,
        )

        renderer = SimpleNamespace()
        renderer._store_planar_upload_stats = (
            EditGpuPreviewRenderer._store_planar_upload_stats.__get__(renderer)
        )
        renderer.consume_planar_upload_stats = (
            EditGpuPreviewRenderer.consume_planar_upload_stats.__get__(renderer)
        )
        return renderer

    def test_store_and_consume_roundtrip(self):
        r = self._make_renderer()
        r._store_planar_upload_stats(10, {"bytes": 100, "ms": 1.0, "tracks": 1,
                                          "mode": "r16ui_direct", "pixel_format":
                                          "yuv422p10le", "gbr": False})
        stats = r.consume_planar_upload_stats(10)
        assert stats is not None
        assert stats["bytes"] == 100
        assert stats["mode"] == "r16ui_direct"
        # 取走即删
        assert r.consume_planar_upload_stats(10) is None

    def test_multi_track_merge_accumulates_bytes(self):
        r = self._make_renderer()
        r._store_planar_upload_stats(20, {"bytes": 100, "ms": 1.0, "tracks": 1,
                                          "mode": "r16ui_direct", "pixel_format":
                                          "yuv422p10le", "gbr": False})
        r._store_planar_upload_stats(20, {"bytes": 200, "ms": 2.0, "tracks": 1,
                                          "mode": "r16ui_direct", "pixel_format":
                                          "yuv444p10le", "gbr": False})
        stats = r.consume_planar_upload_stats(20)
        assert stats["bytes"] == 300
        assert stats["ms"] == pytest.approx(3.0)
        assert stats["tracks"] == 2
        assert stats["pixel_format"] == "yuv444p10le"  # 最后一轨

    def test_store_bounded_at_16_frames(self):
        r = self._make_renderer()
        for f in range(24):
            r._store_planar_upload_stats(f, {"bytes": f, "ms": 0.0, "tracks": 1,
                                             "mode": "r16ui_direct",
                                             "pixel_format": "", "gbr": False})
        # 最早的 8 帧被挤出
        assert r.consume_planar_upload_stats(0) is None
        assert r.consume_planar_upload_stats(23) is not None

    def test_consume_missing_frame_returns_none(self):
        r = self._make_renderer()
        assert r.consume_planar_upload_stats(404) is None


# ---------------------------------------------------------------------------
# RenderCacheEncoder.write_frame_bgr：BGR 直入（无翻转拷贝）
# ---------------------------------------------------------------------------


class TestWriteFrameBgrPassthrough:
    def test_bgr_data_passes_through_without_flip(self):
        from src.features.edit.core.render_cache_encoder import (
            RenderCacheEncoder,
            RenderCacheEncoderConfig,
        )

        enc = object.__new__(RenderCacheEncoder)
        enc._is_open = True
        enc._config = RenderCacheEncoderConfig(
            output_path="out.mov", temp_path="tmp.mov", width=3, height=2
        )
        captured: list = []
        enc._encoder = MagicMock()
        enc._encoder.encode_frame.side_effect = lambda f: captured.append(f)

        frame = np.arange(2 * 3 * 3, dtype=np.uint8).reshape(2, 3, 3)
        enc.write_frame_bgr(frame)

        assert len(captured) == 1
        decoded = captured[0]
        # 同一对象（无 flip copy）+ bgr 标注
        assert decoded.data is frame
        assert decoded.pixel_format == 'bgr'
        assert decoded.data.shape == (2, 3, 3)

    def test_write_frame_noop_when_not_open(self):
        from src.features.edit.core.render_cache_encoder import RenderCacheEncoder

        enc = object.__new__(RenderCacheEncoder)
        enc._is_open = False
        enc._encoder = None
        # 不抛异常即可
        enc.write_frame_bgr(np.zeros((2, 2, 3), dtype=np.uint8))


class TestWriteFramePlanarPassthrough:
    def test_planar_frame_passes_to_encoder_without_packing(self):
        from src.features.edit.core.render_cache_encoder import (
            RenderCacheEncoder,
            RenderCacheEncoderConfig,
        )
        from src.shared.media.frame import DecodedFrame

        enc = object.__new__(RenderCacheEncoder)
        enc._is_open = True
        enc._config = RenderCacheEncoderConfig(
            output_path="out.mov", temp_path="tmp.mov", width=4, height=2
        )
        enc._encoder = MagicMock()
        planes = tuple(np.full(shape, 512, dtype=np.uint16)
                       for shape in ((2, 4), (2, 2), (2, 2)))
        frame = DecodedFrame(
            frame_index=3, pts=None, time_seconds=0.0,
            planes=planes, pixel_format="yuv422p10le",
            width=4, height=2,
        )

        enc.write_frame_planar(frame)

        enc._encoder.encode_frame.assert_called_once_with(frame)

    def test_planar_geometry_mismatch_is_explicit(self):
        from src.features.edit.core.render_cache_encoder import (
            RenderCacheEncoder,
            RenderCacheEncoderConfig,
        )
        from src.shared.media.frame import DecodedFrame

        enc = object.__new__(RenderCacheEncoder)
        enc._is_open = True
        enc._config = RenderCacheEncoderConfig(
            output_path="out.mov", temp_path="tmp.mov", width=4, height=2
        )
        enc._encoder = MagicMock()
        frame = DecodedFrame(
            frame_index=0, pts=None, time_seconds=0.0,
            planes=tuple(np.zeros(shape, dtype=np.uint16)
                         for shape in ((1, 4), (1, 2), (1, 2))),
            pixel_format="yuv422p10le", width=4, height=1,
        )

        with pytest.raises(ValueError, match="geometry"):
            enc.write_frame_planar(frame)
        enc._encoder.encode_frame.assert_not_called()
