"""P5-T6：frame_provider 专测。

ExportFrameProvider / ExportRange 及辅助函数的公共 API 覆盖。
MediaSource 与 VideoMetadata 均为鸭子类型替身（probe/is_open/open/
read_frame/seek_frame），不依赖真实媒体与解码器。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import List, Optional

import pytest

from src.shared.export.frame_provider import (
    ExportFrameProvider,
    ExportRange,
    create_export_provider,
    estimate_export_duration,
)


class FakeFPS:
    def __init__(self, fps: float) -> None:
        self._fps = fps

    def to_fps(self) -> float:
        return self._fps


class FakeMetadata:
    def __init__(self, width: int = 64, height: int = 48, fps: float = 24.0,
                 total_frames: Optional[int] = 10, bit_depth: int = 8,
                 codec_name: str = "h264") -> None:
        self.width = width
        self.height = height
        self.fps = FakeFPS(fps)
        self.total_frames = total_frames
        self.bit_depth = bit_depth
        self.codec_name = codec_name


class FakeFrame:
    def __init__(self, index: int) -> None:
        self.frame_index = index


class FakeSource:
    """MediaSource Protocol 替身：记录调用、可注入解码失败。"""

    def __init__(self, metadata: Optional[FakeMetadata] = None,
                 fail_read_at: Optional[int] = None,
                 probe_error: bool = False) -> None:
        self._metadata = metadata
        self._probe_error = probe_error
        self._fail_read_at = fail_read_at
        self.is_open = False
        self.read_calls: List[int] = []
        self.seek_calls: List[int] = []

    def probe(self) -> FakeMetadata:
        if self._probe_error or self._metadata is None:
            raise RuntimeError("probe failed")
        return self._metadata

    def open(self) -> None:
        self.is_open = True

    def read_frame(self, frame_index: int) -> FakeFrame:
        self.read_calls.append(frame_index)
        if self._fail_read_at is not None and frame_index == self._fail_read_at:
            raise RuntimeError(f"decode boom at {frame_index}")
        return FakeFrame(frame_index)

    def seek_frame(self, frame_index: int) -> None:
        self.seek_calls.append(frame_index)


class TestExportRange:
    def test_valid_range_count_and_duration(self) -> None:
        r = ExportRange(start_frame=2, end_frame=5)
        assert r.frame_count == 3
        assert r.duration_seconds == 3.0  # 仅返回帧数（见 docstring）

    def test_negative_start_rejected(self) -> None:
        with pytest.raises(ValueError, match="negative"):
            ExportRange(start_frame=-1, end_frame=5)

    def test_end_before_start_rejected(self) -> None:
        with pytest.raises(ValueError, match="end_frame"):
            ExportRange(start_frame=5, end_frame=4)

    def test_empty_range_allowed(self) -> None:
        assert ExportRange(3, 3).frame_count == 0


class TestConstruction:
    def test_end_frame_defaults_to_total_frames(self) -> None:
        src = FakeSource(FakeMetadata(total_frames=12))
        provider = ExportFrameProvider(src, start_frame=0, end_frame=None)
        assert provider.get_frame_range() == (0, 12)
        assert provider.get_frame_count() == 12

    def test_end_frame_none_without_metadata_raises(self) -> None:
        src = FakeSource(FakeMetadata(), probe_error=True)
        with pytest.raises(ValueError, match="end_frame"):
            ExportFrameProvider(src)

    def test_end_frame_none_with_unknown_total_raises(self) -> None:
        src = FakeSource(FakeMetadata(total_frames=None))
        with pytest.raises(ValueError, match="total_frames"):
            ExportFrameProvider(src)

    def test_negative_start_rejected(self) -> None:
        src = FakeSource(FakeMetadata())
        with pytest.raises(ValueError, match="negative"):
            ExportFrameProvider(src, start_frame=-2, end_frame=5)

    def test_end_before_start_rejected(self) -> None:
        src = FakeSource(FakeMetadata())
        with pytest.raises(ValueError, match="end_frame"):
            ExportFrameProvider(src, start_frame=5, end_frame=2)

    def test_end_clamped_to_total_frames(self) -> None:
        src = FakeSource(FakeMetadata(total_frames=10))
        provider = ExportFrameProvider(src, start_frame=0, end_frame=100)
        assert provider.get_frame_range() == (0, 10)

    def test_explicit_end_kept_when_total_unknown(self) -> None:
        src = FakeSource(FakeMetadata(total_frames=None))
        provider = ExportFrameProvider(src, start_frame=3, end_frame=7)
        assert provider.get_frame_range() == (3, 7)

    def test_properties_expose_source_and_metadata(self) -> None:
        src = FakeSource(FakeMetadata())
        provider = ExportFrameProvider(src, 0, 5)
        assert provider.source is src
        assert provider.metadata is src.probe()

    def test_create_export_provider_returns_instance(self) -> None:
        src = FakeSource(FakeMetadata())
        provider = create_export_provider(src, 0, 4)
        assert isinstance(provider, ExportFrameProvider)
        assert provider.get_frame_count() == 4


class TestIteration:
    def test_iterates_every_frame_in_order_without_drops(self) -> None:
        src = FakeSource(FakeMetadata(total_frames=6))
        provider = ExportFrameProvider(src, 0, None)
        indexes = [f.frame_index for f in provider]
        assert indexes == [0, 1, 2, 3, 4, 5]
        assert src.read_calls == indexes  # 确定性逐帧，无跳帧

    def test_iteration_opens_source_once(self) -> None:
        src = FakeSource(FakeMetadata())
        provider = ExportFrameProvider(src, 0, 3)
        assert src.is_open is False
        list(provider)
        assert src.is_open is True

    def test_partial_range_iteration(self) -> None:
        src = FakeSource(FakeMetadata(total_frames=10))
        provider = ExportFrameProvider(src, start_frame=4, end_frame=7)
        assert [f.frame_index for f in provider] == [4, 5, 6]

    def test_decode_failure_propagates(self) -> None:
        src = FakeSource(FakeMetadata(), fail_read_at=2)
        provider = ExportFrameProvider(src, 0, 5)
        with pytest.raises(RuntimeError, match="decode boom"):
            list(provider)

    def test_current_frame_tracks_progress(self) -> None:
        src = FakeSource(FakeMetadata(total_frames=4))
        provider = ExportFrameProvider(src, 1, 4)
        assert provider.current_frame == 1
        seen = []
        for _ in provider:
            seen.append(provider.current_frame)
        assert seen == [1, 2, 3]


class TestQueries:
    def test_get_duration_seconds_with_metadata(self) -> None:
        src = FakeSource(FakeMetadata(fps=24.0, total_frames=12))
        provider = ExportFrameProvider(src, 0, 12)
        assert provider.get_duration_seconds() == pytest.approx(0.5)

    def test_get_duration_seconds_without_metadata_falls_back(self) -> None:
        src = FakeSource(FakeMetadata(), probe_error=True)
        provider = ExportFrameProvider(src, 0, 7)
        assert provider.get_duration_seconds() == 7.0

    def test_get_progress_bounds(self) -> None:
        src = FakeSource(FakeMetadata())
        provider = ExportFrameProvider(src, 0, 10)
        assert provider.get_progress() == 0.0
        provider.seek_to_frame(5)
        assert provider.get_progress() == pytest.approx(0.5)

    def test_get_progress_empty_range_is_complete(self) -> None:
        src = FakeSource(FakeMetadata())
        provider = ExportFrameProvider(src, 3, 3)
        assert provider.get_progress() == 1.0


class TestSeekAndRandomRead:
    def test_seek_to_frame_within_range(self) -> None:
        src = FakeSource(FakeMetadata())
        provider = ExportFrameProvider(src, 2, 8)
        provider.seek_to_frame(4)
        assert provider.current_frame == 4
        assert src.seek_calls == [4]

    @pytest.mark.parametrize("frame", [-1, 1, 8])
    def test_seek_out_of_range_rejected(self, frame: int) -> None:
        src = FakeSource(FakeMetadata())
        provider = ExportFrameProvider(src, 2, 8)
        with pytest.raises(ValueError, match="out of export range"):
            provider.seek_to_frame(frame)

    def test_read_frame_at_returns_frame_and_restores_position(self) -> None:
        src = FakeSource(FakeMetadata())
        provider = ExportFrameProvider(src, 0, 10)
        provider.seek_to_frame(2)  # 当前位置 2
        frame = provider.read_frame_at(6)
        assert frame.frame_index == 6
        assert 6 in src.read_calls
        # 随机读后源位置恢复到保存的当前帧
        assert src.seek_calls[-1] == 2
        assert provider.current_frame == 2

    @pytest.mark.parametrize("frame", [-1, 0, 10])
    def test_read_frame_at_out_of_range_rejected(self, frame: int) -> None:
        src = FakeSource(FakeMetadata())
        provider = ExportFrameProvider(src, 2, 10)
        with pytest.raises(ValueError, match="out of export range"):
            provider.read_frame_at(frame)


class TestSizeEstimation:
    def _provider(self, tmp_metadata: FakeMetadata) -> ExportFrameProvider:
        return ExportFrameProvider(FakeSource(tmp_metadata), 0, 10)

    def test_prores_estimation_formula(self) -> None:
        md = FakeMetadata(width=64, height=48, fps=24.0, bit_depth=10)
        provider = self._provider(md)
        factor = 0.45  # 10-bit → ProRes 422 档
        data_rate = 64 * 48 * 24 * factor
        expected = int(int(data_rate * 10 / 24) * 1.01)
        assert provider.estimate_output_size(codec="prores") == expected

    def test_dnxhr_estimation_formula(self) -> None:
        md = FakeMetadata(width=64, height=48, fps=24.0, bit_depth=10)
        provider = self._provider(md)
        factor = 0.5
        data_rate = 64 * 48 * 24 * factor
        expected = int(int(data_rate * 10 / 24) * 1.01)
        assert provider.estimate_output_size(codec="dnxhr") == expected

    def test_gop_codec_with_explicit_bitrate(self) -> None:
        md = FakeMetadata(fps=24.0)
        provider = self._provider(md)
        expected = int(int(1_000_000 * 10 / 24 / 8) * 1.02)
        assert provider.estimate_output_size(
            codec="h264", bitrate=1_000_000) == expected

    def test_gop_default_bitrate_hd_hevc_vs_h264(self) -> None:
        md = FakeMetadata(width=1920, height=1080, fps=24.0)
        hevc = self._provider(md).estimate_output_size(codec="hevc")
        h264 = self._provider(md).estimate_output_size(codec="h264")
        # HEVC 默认码率(8Mbps) < H.264(12Mbps) → 同帧数下估算更小
        assert hevc < h264

    def test_no_metadata_returns_zero(self) -> None:
        src = FakeSource(FakeMetadata(), probe_error=True)
        provider = ExportFrameProvider(src, 0, 10)
        assert provider.estimate_output_size(codec="prores") == 0


class TestHelpers:
    def test_estimate_export_duration(self) -> None:
        assert estimate_export_duration(48, fps=24.0) == pytest.approx(2.0)

    def test_estimate_export_duration_zero_fps_raises(self) -> None:
        with pytest.raises(ZeroDivisionError):
            estimate_export_duration(48, fps=0.0)
