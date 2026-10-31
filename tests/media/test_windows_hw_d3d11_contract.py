"""W0-02：d3d11_decoder / d3d11_gl_bridge 骨架契约测试。

沿用 gpu_capability 套件的假数据注入模式之外的纯契约钉法：骨架的
reason 枚举、ring 结构、失败计数路径全部在此钉死，W3 真机实现时
契约不得漂移（计划 Phase W0-02 验收标准）。
"""

from __future__ import annotations

import pytest

from src.shared.media.windows_hw.d3d11_decoder import D3D11Decoder, D3D11DecodedTexture
from src.shared.media.windows_hw.d3d11_gl_bridge import D3D11GLBridge
from src.shared.media.windows_hw.reasons import FallbackReason


class TestD3D11DecoderContract:
    def test_open_fails_honestly_with_reason(self):
        decoder = D3D11Decoder()
        assert decoder.is_open is False
        # 骨架 open() 必须显式失败 + reason，绝不伪装成功
        assert decoder.open("whatever.mp4", codec="h264") is False
        assert decoder.is_open is False
        assert decoder.last_fallback_reason == FallbackReason.NOT_YET_IMPLEMENTED.value

    def test_decode_next_returns_none_when_closed(self):
        decoder = D3D11Decoder()
        assert decoder.decode_next() is None
        assert decoder.last_fallback_reason == FallbackReason.NOT_PROBED.value

    def test_close_resets_state(self):
        decoder = D3D11Decoder()
        decoder.open("whatever.mp4")
        decoder.close()
        assert decoder.is_open is False
        assert decoder.decode_next() is None

    def test_decoded_texture_defaults(self):
        tex = D3D11DecodedTexture()
        assert tex.texture_handle == 0
        assert tex.frame_index == -1
        assert tex.pts is None
        assert tex.pixel_format == "nv12"
        assert tex.bit_depth == 8

    def test_supported_codec_set_includes_av1(self):
        """W2-01 路由评估的输入：AV1 在声明集合里（能力实测另做）。"""
        assert "av1" in D3D11Decoder._SUPPORTED_CODECS
        assert {"h264", "hevc", "h265", "vp9"} <= D3D11Decoder._SUPPORTED_CODECS


class TestD3D11GLBridgeContract:
    def test_ring_size_preserved(self):
        assert D3D11GLBridge().initialize() is False  # 默认构造不抛
        bridge = D3D11GLBridge(ring_size=4)
        assert bridge._ring_size == 4  # W3-01 落地时 ring_size=4 结构不得丢

    def test_initialize_reason(self):
        bridge = D3D11GLBridge()
        assert bridge.is_initialized is False
        assert bridge.initialize() is False
        assert bridge.is_initialized is False
        assert bridge.last_fallback_reason == (
            FallbackReason.INTEROP_NOT_YET_IMPLEMENTED.value)

    def test_bind_texture_counts_failures(self):
        bridge = D3D11GLBridge()
        assert bridge.bind_texture(0xDEAD, frame_index=0) is None
        assert bridge.bind_texture(0xBEEF, frame_index=1, pixel_format="p010",
                                   bit_depth=16) is None
        assert bridge._fail_count == 2
        assert bridge.last_fallback_reason == (
            FallbackReason.INTEROP_NOT_YET_IMPLEMENTED.value)

    def test_destroy_clears_initialized_only(self):
        bridge = D3D11GLBridge()
        bridge.bind_texture(1, 0)
        bridge.destroy()
        assert bridge.is_initialized is False
        # 失败计数与最后 reason 保留：destroy 不是诊断擦除
        assert bridge._fail_count == 1
        assert bridge.last_fallback_reason is not None
