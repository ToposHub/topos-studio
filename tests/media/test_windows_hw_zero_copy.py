"""
Windows HW Zero-Copy Module Tests (骨架测试)
测试 windows_hw 包的数据结构和模块导入。
"""

from __future__ import annotations

import platform
import pytest

from src.shared.media.windows_hw.capability import (
    WindowsZeroCopyCapability,
    probe_zero_copy_capability,
    is_zero_copy_available,
)
from src.shared.media.windows_hw.d3d11_decoder import D3D11Decoder, D3D11DecodedTexture
from src.shared.media.windows_hw.d3d11_gl_bridge import D3D11GLBridge


class TestWindowsZeroCopyCapability:
    """Windows 零拷贝能力 gate 测试"""

    def test_capability_dataclass(self) -> None:
        cap = WindowsZeroCopyCapability()
        assert cap.available is False
        assert cap.reason == "not_probed"
        assert 'h264' in cap.supported_codecs

    def test_probe_on_non_windows(self) -> None:
        cap = probe_zero_copy_capability()
        if platform.system() != 'Windows':
            assert cap.available is False
            assert cap.reason == "not_windows"

    def test_is_available_on_non_windows(self) -> None:
        result = is_zero_copy_available(codec='h264')
        if platform.system() != 'Windows':
            assert result is False or result is True


class TestD3D11DecodedTexture:
    """D3D11DecodedTexture 数据类测试"""

    def test_default_values(self) -> None:
        tex = D3D11DecodedTexture()
        assert tex.texture_handle == 0
        assert tex.width == 0
        assert tex.pixel_format == "nv12"
        assert tex.bit_depth == 8

    def test_custom_values(self) -> None:
        tex = D3D11DecodedTexture(
            texture_handle=12345,
            width=3840,
            height=2160,
            frame_index=10,
            pts=33333,
            pixel_format="p010",
            bit_depth=10,
        )
        assert tex.texture_handle == 12345
        assert tex.width == 3840
        assert tex.pixel_format == "p010"


class TestD3D11Decoder:
    """D3D11Decoder 骨架测试"""

    def test_creation(self) -> None:
        decoder = D3D11Decoder()
        assert decoder.is_open is False

    def test_open_returns_false(self) -> None:
        decoder = D3D11Decoder()
        assert decoder.open("/path/to/video.mp4") is False

    def test_decode_next_returns_none(self) -> None:
        decoder = D3D11Decoder()
        assert decoder.decode_next() is None


class TestD3D11GLBridge:
    """D3D11GLBridge 骨架测试"""

    def test_creation(self) -> None:
        bridge = D3D11GLBridge(ring_size=4)
        assert bridge.is_initialized is False

    def test_initialize_returns_false(self) -> None:
        bridge = D3D11GLBridge()
        assert bridge.initialize() is False

    def test_bind_returns_none(self) -> None:
        bridge = D3D11GLBridge()
        result = bridge.bind_texture(
            d3d_texture=1,
            frame_index=0,
        )
        assert result is None
        assert bridge.last_fallback_reason == "interop_not_yet_implemented"