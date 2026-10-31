"""WindowsHwPlanarSource 测试（计划 Phase 8 P8-08）。

`hw_planar_source.py` 此前零测试（plan §2.5）。本机是 macOS——真实
NVDEC/D3D11VA 硬解无法运行；测试聚焦**数据契约层**（与
MacOSVtPlanarSource 的 DecodedFrame 契约一致、格式路由表、首帧验证
门控、转换产物的 extra 元数据），真实硬解验证留给 Windows 机器。
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
import pytest

from src.shared.media.frame_path import DecodePath
from src.shared.media.hwaccel import HardwareDecoder
from src.shared.media.source import MediaOpenError
from src.shared.media.windows_hw.hw_planar_source import (
    _DEVICE_TYPE_NAMES,
    _HW_SEMI_PLANAR_FORMATS,
    WindowsHwPlanarSource,
)


# =========================================================================
# 契约表
# =========================================================================


class TestFormatTables:
    def test_semi_planar_formats(self):
        # nv12 → nv12 契约 8bit；p010le/p016le → p016le 契约（MSB 对齐原样上传）
        assert _HW_SEMI_PLANAR_FORMATS["nv12"] == ("nv12", np.dtype(np.uint8), 8)
        assert _HW_SEMI_PLANAR_FORMATS["p010le"][0] == "p016le"
        assert _HW_SEMI_PLANAR_FORMATS["p010le"][1] == np.dtype(np.uint16)
        assert "p016le" in _HW_SEMI_PLANAR_FORMATS

    def test_device_type_names(self):
        assert _DEVICE_TYPE_NAMES["nvdec"] == "cuda"
        assert _DEVICE_TYPE_NAMES["d3d11va"] == "d3d11va"
        assert _DEVICE_TYPE_NAMES["qsv"] == "qsv"


# =========================================================================
# 构造与状态
# =========================================================================


class TestConstruction:
    def test_defaults(self):
        src = WindowsHwPlanarSource("fake.mp4")
        assert src.using_hwaccel is False
        assert src.active_decoder is None
        assert src.hw_output_format is None

    def test_explicit_decoder_kept(self):
        src = WindowsHwPlanarSource("fake.mp4", decoder=HardwareDecoder.NVDEC)
        assert src.active_decoder == HardwareDecoder.NVDEC


# =========================================================================
# open() 门控（全部 mock，不触碰真实 PyAV HWAccel）
# =========================================================================


class TestOpenGating:
    def _make_source(self) -> WindowsHwPlanarSource:
        src = WindowsHwPlanarSource.__new__(WindowsHwPlanarSource)
        src._path = "fake.mp4"
        src._hw_decoder = None
        src._hw_active = False
        src._hw_output_format = None
        src._is_planar_mode = False
        src._is_open = False
        src._container = None
        src._video_stream = None
        src._pending_av_frame = None
        return src

    def test_no_decoder_raises(self):
        from unittest.mock import MagicMock

        src = self._make_source()
        src._metadata = MagicMock()
        src._metadata.codec_name = "h264"
        with patch(
            "src.shared.media.ffmpeg_source.FFmpegMediaSource.open"
        ), patch(
            "src.shared.media.windows_hw.hw_planar_source.HardwareCapability"
        ) as cap:
            cap.get_best_decoder_for_codec.return_value = None
            with pytest.raises(MediaOpenError, match="No hardware decoder"):
                src.open()

    def test_verify_accepts_nv12_and_caches_first_frame(self):
        src = self._make_source()
        first = SimpleNamespace(
            format=SimpleNamespace(name="nv12"), pts=0
        )
        src._container = SimpleNamespace(
            decode=lambda stream: iter([first])
        )
        src._video_stream = object()
        src._verify_hw_output()
        assert src._hw_output_format == "nv12"
        assert src._pending_av_frame is first
        assert src._current_frame == 0

    def test_verify_rejects_software_fallback_format(self):
        """硬解静默回退软件解码（yuv422p10le）→ 显式 MediaOpenError。"""
        src = self._make_source()
        first = SimpleNamespace(
            format=SimpleNamespace(name="yuv422p10le"), pts=0
        )
        src._container = SimpleNamespace(
            decode=lambda stream: iter([first])
        )
        src._video_stream = object()
        with pytest.raises(MediaOpenError, match="not hardware-decodable"):
            src._verify_hw_output()

    def test_verify_no_frames_raises(self):
        src = self._make_source()
        src._container = SimpleNamespace(decode=lambda stream: iter([]))
        src._video_stream = object()
        with pytest.raises(MediaOpenError, match="No decodable frames"):
            src._verify_hw_output()


# =========================================================================
# _convert_frame_planar 契约
# =========================================================================


class _FakeAvFrame:
    def __init__(self, fmt: str, width: int = 8, height: int = 8, pts: int = 42):
        self.format = SimpleNamespace(name=fmt)
        self.width = width
        self.height = height
        self.pts = pts
        self.planes = [SimpleNamespace(index=i) for i in range(2)]


def _make_convert_source(decoder: HardwareDecoder, stream_time_base=None):
    src = WindowsHwPlanarSource.__new__(WindowsHwPlanarSource)
    src._path = "fake.mp4"
    src._hw_decoder = decoder
    src._hw_active = True
    src._hw_output_format = "nv12"
    src._current_frame = 7
    src._metadata = object()  # DecodedFrame 仅透传引用
    src._video_stream = SimpleNamespace(time_base=stream_time_base)
    # __del__/close() 需要的属性（__new__ 构造不带父类 init）
    src._is_open = False
    src._container = None
    src._decoder_buffer = []
    src._frame_iterator = None
    src._pending_av_frame = None
    return src


class TestConvertFramePlanar:
    def test_nv12_contract(self):
        src = _make_convert_source(HardwareDecoder.NVDEC)
        y = np.zeros((8, 8), dtype=np.uint8)
        uv = np.zeros((4, 8), dtype=np.uint8)
        with patch.object(
            src, "_extract_visible_plane_data",
            side_effect=[y, uv],
        ):
            decoded = src._convert_frame_planar(_FakeAvFrame("nv12"))
        # 与 MacOSVtPlanarSource 同契约：data=None + 恰好两个紧凑平面
        assert decoded.data is None
        assert decoded.pixel_format == "nv12"
        assert len(decoded.planes) == 2
        assert decoded.frame_index == 7
        assert decoded.width == 8 and decoded.height == 8
        extra = decoded.extra
        assert extra["decode_path"] == DecodePath.HWACCEL_DOWNLOAD_PLANAR_YUV.value
        assert extra["source"] == "windows_hw_planar"
        assert extra["hw_decoder"] == "nvdec"
        assert extra["hw_output_format"] == "nv12"
        assert extra["msb_aligned_p010"] is False
        # 颜色缺省与渲染器一致
        assert decoded.color_range == "limited"
        assert decoded.color_matrix == "bt709"

    def test_p010_msbs_aligned_contract(self):
        src = _make_convert_source(HardwareDecoder.D3D11VA)
        y = np.zeros((8, 8), dtype=np.uint16)
        uv = np.zeros((4, 8), dtype=np.uint16)
        with patch.object(
            src, "_extract_visible_plane_data",
            side_effect=[y, uv],
        ):
            decoded = src._convert_frame_planar(_FakeAvFrame("p010le"))
        # p010 按 p016le 契约原样上传（不做 CPU >>6——计划注释的实测取舍）
        assert decoded.pixel_format == "p016le"
        assert decoded.extra["msb_aligned_p010"] is True
        assert decoded.plane_infos[0].bit_depth == 16
        assert decoded.plane_infos[1].stride == 8 * 2  # UV 行字节数 = w * itemsize

    def test_time_seconds_from_time_base(self):
        tb = 1 / 90000
        src = _make_convert_source(HardwareDecoder.NVDEC, stream_time_base=tb)
        with patch.object(
            src, "_extract_visible_plane_data",
            side_effect=[np.zeros((8, 8), np.uint8), np.zeros((4, 8), np.uint8)],
        ):
            decoded = src._convert_frame_planar(_FakeAvFrame("nv12", pts=90000))
        assert decoded.time_seconds == pytest.approx(1.0)

    def test_unexpected_format_falls_back_to_parent(self):
        src = _make_convert_source(HardwareDecoder.NVDEC)
        sentinel = object()
        with patch.object(
            src, "_extract_visible_plane_data"
        ) as ex, patch(
            "src.shared.media.ffmpeg_source.FFmpegMediaSource._convert_frame_planar",
            return_value=sentinel,
        ) as parent:
            out = src._convert_frame_planar(_FakeAvFrame("yuv420p"))
        assert out is sentinel  # 硬解中途失效 → 父类通用 planar 兜底
        parent.assert_called_once()
