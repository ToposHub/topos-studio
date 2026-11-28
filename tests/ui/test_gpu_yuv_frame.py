"""M3：GPU YUV 帧与解码器 YUV 接口回归。

这些测试不要求 OpenGL context；真正的 GPU 画布渲染由
``tools/verify_native_gpu_preview.py`` 在带显示器的机器上验证。
"""
from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pytest

from src.shared.playback.gl_video_canvas import _h273_matrix_to_uploader
from src.shared.playback.yuv_frame import YuvFrame
from tests.real_media import require_video


def test_yuv_frame_geometry_validation():
    y = np.zeros((8, 10), np.uint16)
    u = np.zeros((8, 5), np.uint16)
    v = np.zeros((8, 5), np.uint16)
    frame = YuvFrame(y, u, v, bit_depth=10, subsampling='422',
                     pixel_format='422', source_width=10, source_height=8)
    assert frame.is_valid()
    assert frame.width == 10 and frame.height == 8
    assert not YuvFrame(y, u[:, :-1], v, 10, '422').is_valid()


def test_h273_matrix_maps_bt709_601_2020():
    # YUVGPUUploader constants: 0=BT709, 1=BT2020, 2=BT601
    assert _h273_matrix_to_uploader(1) == 0
    assert _h273_matrix_to_uploader(5) == 2
    assert _h273_matrix_to_uploader(6) == 2
    assert _h273_matrix_to_uploader(9) == 1
    assert _h273_matrix_to_uploader(2) == 0


@pytest.mark.parametrize(("divisor", "size"), [(1, None), (2, (1920, 1080)), (4, (960, 540))])
def test_topos_decode_prefers_gpu_yuv_frame(divisor, size):
    from src.shared.playback.topos_player import ToposPlayer

    player = ToposPlayer.__new__(ToposPlayer)
    player.canvas = SimpleNamespace(gpu_active=True)
    player._playback_resolution_divisor = divisor
    marker = object()

    class _Session:
        width, height = 3840, 2160

        def frame_yuv(self, target, **kwargs):
            assert target == 12 and kwargs["size"] == size
            return marker

        def frame_rgb(self, target, size=None):
            raise AssertionError('GPU path should not call frame_rgb')

    assert player._decode_frame(_Session(), 12) is marker


def test_topos_decode_rgb_fallback(qtbot):
    from src.shared.playback.topos_player import ToposPlayer

    player = ToposPlayer.__new__(ToposPlayer)
    player.canvas = SimpleNamespace(gpu_active=False)
    player._playback_resolution_divisor = 1
    rgb = np.zeros((360, 640, 3), np.uint8)
    rgb[:, :, 1] = 128

    class _Session:
        def frame_yuv(self, target, size=None):
            raise AssertionError('RGB fallback should not call frame_yuv')

        def frame_rgb(self, target, size=None):
            assert size is None
            return rgb

    image = player._decode_frame(_Session(), 0)
    assert image.width() == 640 and image.height() == 360


@pytest.mark.real_media
@pytest.mark.parametrize(
    ('name', 'subsampling', 'depth'),
    [
        ('2K_h264.mp4', '420', 8),
        ('2K_H265.mov', '420', 10),
        ('2K_prores422LT.mov', '422', 10),
        ('2K_DNxHR HQX 10bit.mov', '422', 10),
        ('4K_H264.mov', '420', 8),
    ],
)
def test_libav_yuv_preserves_source_bit_depth(name, subsampling, depth):
    from src.shared.playback.libav_player import _LibavSession

    session = _LibavSession(str(require_video(name)))
    try:
        frame = session.frame_yuv(0, size=(320, 180))
        assert frame.subsampling == subsampling
        assert frame.bit_depth == depth
        assert frame.y.dtype == (np.uint16 if depth > 8 else np.uint8)
        assert frame.u.dtype == frame.y.dtype
        assert frame.is_valid()
        assert frame.source_width > 0 and frame.source_height > 0
    finally:
        session.close()
