"""SourceManager master 源 planar 输出（v3.1）测试。

4K 10-bit ProRes/DNxHR 拖拽预览的 CPU 瓶颈是解码后 swscale 单线程转
rgb48le（实测 4K ProRes 33fps → planar 105fps）。master 源对 YUV 素材
启用 planar 输出（保留 YUV planes），由渲染器 YUVGPUUploader 做 GPU 色彩
转换；非 YUV 素材由源侧 set_preserve_planes 判定保持 packed RGB。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

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
