"""Windows factory routing tests for the real NV12/P010 preview path."""

from __future__ import annotations

import sys
from unittest.mock import MagicMock, patch

import numpy as np

from src.shared.media import factory as factory_module
from src.shared.media.decode_policy import (
    DecodePolicyManager,
    DecodeTarget,
    HardwareDecodeStatus,
)
from src.shared.media.hwaccel import FFmpegHwaccelSource, HardwareDecoder
from src.shared.media.metadata import FPS_24, VideoMetadata


def _metadata() -> VideoMetadata:
    return VideoMetadata(
        width=1920,
        height=1080,
        fps=FPS_24,
        total_frames=120,
        codec_name="h264",
        codec_profile="high",
        pixel_format="yuv420p",
        bit_depth=8,
    )


def test_windows_preview_preserve_planes_uses_hw_planar_source(monkeypatch):
    """preserve_planes 不应再被硬解工厂改成 packed RGB。"""
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.delenv("TOPOS_DISABLE_WIN_HW_DECODE", raising=False)
    monkeypatch.setattr(
        factory_module.MediaSourceFactory,
        "probe_video_metadata",
        staticmethod(lambda _path: _metadata()),
    )

    from src.shared.media import hwaccel as hw_module

    monkeypatch.setattr(
        hw_module.HardwareCapability,
        "detect_available_decoders",
        staticmethod(lambda: [HardwareDecoder.NVDEC]),
    )
    monkeypatch.setattr(
        hw_module.HardwareCapability,
        "get_best_decoder_for_codec",
        staticmethod(lambda _codec: HardwareDecoder.NVDEC),
    )

    fake_source = object()
    with patch(
        "src.shared.media.windows_hw.hw_planar_source.WindowsHwPlanarSource",
        return_value=fake_source,
    ) as constructor:
        source, status = factory_module.MediaSourceFactory._try_create_hwaccel_source(
            "C:/media/clip.mp4",
            DecodePolicyManager.get_policy(DecodeTarget.PREVIEW),
            target_size=None,
            backend="ffmpeg",
            preserve_planes=True,
        )

    assert source is fake_source
    constructor.assert_called_once_with(
        "C:/media/clip.mp4", decoder=HardwareDecoder.NVDEC
    )
    assert status.hwaccel_enabled is True
    assert status.decoder_type == "nvdec"
    assert status.decode_path == "hwaccel_download_planar_yuv"


def test_windows_preview_scaled_target_keeps_generic_source(monkeypatch):
    """硬解 planar 源不假装支持 target_size。"""
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.delenv("TOPOS_DISABLE_WIN_HW_DECODE", raising=False)
    monkeypatch.setattr(
        factory_module.MediaSourceFactory,
        "probe_video_metadata",
        staticmethod(lambda _path: _metadata()),
    )

    from src.shared.media import hwaccel as hw_module

    monkeypatch.setattr(
        hw_module.HardwareCapability,
        "detect_available_decoders",
        staticmethod(lambda: [HardwareDecoder.NVDEC]),
    )
    monkeypatch.setattr(
        hw_module.HardwareCapability,
        "get_best_decoder_for_codec",
        staticmethod(lambda _codec: HardwareDecoder.NVDEC),
    )

    with patch(
        "src.shared.media.hwaccel.FFmpegHwaccelSource",
        return_value=object(),
    ), patch(
        "src.shared.media.windows_hw.hw_planar_source.WindowsHwPlanarSource",
    ) as planar_source:
        factory_module.MediaSourceFactory._try_create_hwaccel_source(
            "C:/media/clip.mp4",
            DecodePolicyManager.get_policy(DecodeTarget.PREVIEW),
            target_size=(1280, 720),
            backend="ffmpeg",
            preserve_planes=True,
        )

    planar_source.assert_not_called()


def test_auto_open_hardware_failure_falls_back_and_keeps_reason(tmp_path):
    """首帧/驱动打开失败不能阻断预览，且诊断 reason 不应丢失。"""
    media_path = tmp_path / "clip.mp4"
    media_path.write_bytes(b"placeholder")
    hardware_source = MagicMock()
    hardware_source.open.side_effect = RuntimeError("driver rejected profile")
    cpu_source = MagicMock()

    hw_status = HardwareDecodeStatus(
        hwaccel_enabled=True,
        decoder_type="nvdec",
        decode_path="hwaccel_download_planar_yuv",
    )
    with patch.object(
        factory_module.MediaSourceFactory,
        "_try_create_hwaccel_source",
        return_value=(hardware_source, hw_status),
    ), patch.object(
        factory_module.MediaSourceFactory,
        "create",
        return_value=cpu_source,
    ):
        source, status = factory_module.MediaSourceFactory.create_with_hwaccel(
            str(media_path),
            decode_target=DecodeTarget.PREVIEW,
            auto_open=True,
        )

    assert source is cpu_source
    assert status.decoder_type == "cpu"
    assert status.fallback_occurred is True
    assert "driver rejected profile" in (status.fallback_reason or "")
    hardware_source.close.assert_called_once()
    cpu_source.open.assert_called_once()


def test_generic_hwaccel_packed_path_honors_rgb_contract():
    """通用硬解源不能把 NV12 stacked ndarray 冒充 packed RGB。"""

    class _ConvertedFrame:
        def to_ndarray(self):
            return np.zeros((1080, 1920, 3), dtype=np.uint8)

    class _HardwareFrame:
        pts = 0
        width = 1920
        height = 1080
        format = type("_Format", (), {"name": "nv12"})()

        def reformat(self, **kwargs):
            assert kwargs == {"format": "rgb24"}
            return _ConvertedFrame()

        def to_ndarray(self):
            raise AssertionError("raw NV12 ndarray leaked from hwaccel path")

    source = FFmpegHwaccelSource("C:/media/clip.mp4")
    source._metadata = _metadata()
    source._video_stream = None

    decoded = source._convert_hwaccel_frame(_HardwareFrame())

    assert decoded.pixel_format == "rgb24"
    assert decoded.data.shape == (1080, 1920, 3)
    assert len(decoded.planes) == 0
