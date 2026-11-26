"""SourceManager master 源 planar 输出（v3.1）测试。

4K 10-bit ProRes/DNxHR 拖拽预览的 CPU 瓶颈是解码后 swscale 单线程转
rgb48le（实测 4K ProRes 33fps → planar 105fps）。master 源对 YUV 素材
启用 planar 输出（保留 YUV planes），由渲染器 YUVGPUUploader 做 GPU 色彩
转换；非 YUV 素材由源侧 set_preserve_planes 判定保持 packed RGB。
"""

from __future__ import annotations

import sys
import threading
import weakref
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.shared.media.cache import CachePolicy, FrameCache
from src.shared.media.ffmpeg_source import FFmpegMediaSource
from src.shared.media.frame import DecodedFrame
from src.shared.media.metadata import Rational, VideoMetadata
from src.shared.video.playback_clock import SourceManager


class PlanarProbeSource:
    """最小 MediaSource 桩：记录 set_preserve_planes 调用。"""

    def __init__(self, pixel_format: str = "yuv422p10le") -> None:
        self._metadata = VideoMetadata(
            width=3840,
            height=2160,
            fps=Rational(24, 1),
            total_frames=100,
            codec_name="prores",
            codec_profile="HQ",
            pixel_format=pixel_format,
            bit_depth=10,
        )
        self.preserve_planes_calls: list[bool] = []

    def open(self) -> bool:
        return True

    def close(self) -> None:
        return None

    def probe(self) -> VideoMetadata:
        return self._metadata

    def set_preserve_planes(self, value: bool) -> None:
        self.preserve_planes_calls.append(bool(value))

    @property
    def is_planar_mode(self) -> bool:
        if not self.preserve_planes_calls:
            return False
        # 模拟 FFmpegMediaSource：非 YUV 源 set_preserve_planes(True) 不生效
        return self.preserve_planes_calls[-1] and self._metadata.pixel_format.startswith("yuv")


class NoPlanarApiSource(PlanarProbeSource):
    """不支持 set_preserve_planes 的源（如测试替身/其他后端）。"""

    set_preserve_planes = None  # type: ignore[assignment]


def _make_manager(source: PlanarProbeSource, **kwargs) -> SourceManager:
    def factory(path, target_size=None, preserve_planes=False):
        return source

    return SourceManager(path="/mock/4k.mov", source_factory=factory, **kwargs)


class TestPlanarMaster:
    def test_yuv_master_enables_planar_by_default(self) -> None:
        source = PlanarProbeSource("yuv422p10le")
        manager = _make_manager(source)
        assert manager.open() is True
        assert source.preserve_planes_calls == [True]
        assert source.is_planar_mode is True
        manager.close()

    def test_planar_master_disabled_keeps_packed(self) -> None:
        source = PlanarProbeSource("yuv422p10le")
        manager = _make_manager(source, enable_planar_master=False)
        assert manager.open() is True
        assert source.preserve_planes_calls == []
        manager.close()

    def test_rgb_master_stays_packed(self) -> None:
        source = PlanarProbeSource("rgb48le")
        manager = _make_manager(source)
        assert manager.open() is True
        # 决策委托给源侧：set_preserve_planes 被调用，但非 YUV 保持 packed
        assert source.preserve_planes_calls == [True]
        assert source.is_planar_mode is False
        manager.close()

    def test_source_without_planar_api_opens_fine(self) -> None:
        source = NoPlanarApiSource("yuv422p10le")
        manager = _make_manager(source)
        assert manager.open() is True
        manager.close()


@pytest.mark.parametrize('shared', [False, True])
def test_interactive_master_history_is_scoped_to_the_open_source(shared: bool) -> None:
    class ScrubSource(PlanarProbeSource):
        def tune_interactive_scrub(self, *, retain_history: bool) -> bool:
            assert retain_history is (not shared)
            return retain_history

    master = ScrubSource('yuv420p')

    def factory(path: str, target_size: tuple[int, int] | None = None,
                preserve_planes: bool = False) -> PlanarProbeSource:
        return master if target_size is None or shared else ScrubSource('yuv420p')

    manager = SourceManager('/mock/h264.mov', source_factory=factory,
                            viewport_size=(640, 360), interactive_scrub=True)
    assert manager.open()
    try:
        frame = DecodedFrame(0, 0, 0.0, planes=(np.zeros((4, 4), np.uint8),))
        ref = weakref.ref(frame)
        manager.cache.put_master(0, frame)
        del frame
        for index in range(1, 6):
            manager.cache.put_master(index, DecodedFrame(index, index, 0.0,
                                     planes=(np.ones((4, 4), np.uint8),)))
        assert manager.cache.has_master(0) is (not shared)
        if not shared:
            assert ref() is not None and manager.get_master_frame(0) is ref()
    finally:
        manager.close()
    assert ref() is None
    assert manager.open()
    try:
        assert not manager.cache.has_master(0)
    finally:
        manager.close()


def test_interactive_history_charges_native_planes_and_owned_chroma() -> None:
    av = pytest.importorskip('av')
    # Luma is a contiguous view of the AVFrame. Both chroma rows are padded
    # and get their own compact NumPy arrays during the planar conversion.
    native = av.VideoFrame(272, 1024, 'yuv420p')
    source = FFmpegMediaSource('memory', preserve_planes=True)
    source._metadata = PlanarProbeSource('yuv420p').probe()
    # Memory accounting must still work if duplicate PTS disabled GOP history.
    assert source._scrub_seek_frame_limit == 0
    frame = source._convert_frame_planar(native)
    assert [p.flags.owndata for p in frame.planes] == [False, True, True]
    retained = sum(p.buffer_size for p in native.planes) + sum(p.nbytes for p in frame.planes[1:])
    cache = FrameCache(CachePolicy(max_frames=24, max_memory_mb=1))
    assert cache.put(0, frame)
    assert cache.get_stats()['memory_mb'] == retained / (1 << 20)
    assert cache.put(1, source._convert_frame_planar(av.VideoFrame(272, 1024, 'yuv420p')))
    assert not cache.contains(0)


def test_scrub_history_rejects_expired_wrong_and_busy_entries() -> None:
    cache = FrameCache()
    pixels = np.ones((4, 4), np.uint8)
    cache.put(1, DecodedFrame(9, 1, 0.0, planes=(pixels,)))
    cache.put(2, DecodedFrame(2, 2, 0.0, planes=(pixels,), is_proxy=True))
    cache.put(3, DecodedFrame(3, 3, 0.0, planes=(pixels,)))
    cache._cache[3].timestamp -= cache.policy.ttl_seconds + 1
    assert cache.try_get_scrub_frame(1, 1, 3) is None
    frame = DecodedFrame(4, 4, 0.0, planes=(pixels,))
    cache.put(4, frame)
    results = []
    with cache._lock:
        reader = threading.Thread(target=lambda: results.append(cache.try_get_scrub_frame(4, 1, 4)))
        reader.start()
        reader.join(timeout=1)
        assert not reader.is_alive() and results == [None]
    assert cache.try_get_scrub_frame(4, 1, 4) is frame
