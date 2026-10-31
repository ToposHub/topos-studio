from pathlib import Path

import numpy as np
from PIL import Image

from src.shared.export.gif_encoder import GifVideoEncoder
from src.shared.export.video_encoder import FFmpegEncoderConfig
from src.shared.media.frame import DecodedFrame
from src.shared.media.metadata import Rational


def _frame(index: int, width: int = 24, height: int = 16) -> DecodedFrame:
    data = np.zeros((height, width, 3), dtype=np.uint8)
    data[..., 0] = (index * 60) % 256  # B
    data[..., 1] = np.linspace(0, 255, width, dtype=np.uint8)
    data[..., 2] = 255 - data[..., 0]  # R
    return DecodedFrame(
        frame_index=index,
        pts=index,
        time_seconds=index / 12.0,
        data=data,
        pixel_format="bgr24",
        width=width,
        height=height,
    )


def test_gif_encoder_writes_animation_with_requested_palette(tmp_path: Path) -> None:
    output = tmp_path / "preview.gif"
    config = FFmpegEncoderConfig(
        codec="gif",
        width=24,
        height=16,
        fps=Rational(12, 1),
        pix_fmt="rgb24",
        gif_color_count=16,
    )
    encoder = GifVideoEncoder(str(output), config)
    encoder.open()
    for index in range(3):
        encoder.encode_frame(_frame(index))
    encoder.close()

    assert not encoder.is_open
    assert encoder.frame_count == 3
    with Image.open(output) as animation:
        assert animation.size == (24, 16)
        assert animation.n_frames == 3
        assert animation.info["duration"] == 80
        colors = animation.convert("RGB").getcolors(256)
        assert colors is not None
        assert len(colors) <= 16


def test_gif_encoder_abort_does_not_publish_partial_output(tmp_path: Path) -> None:
    output = tmp_path / "cancelled.gif"
    config = FFmpegEncoderConfig(
        codec="gif",
        width=24,
        height=16,
        fps=Rational(24, 1),
        pix_fmt="rgb24",
    )
    encoder = GifVideoEncoder(str(output), config)
    encoder.open()
    encoder.encode_frame(_frame(0))
    encoder.abort()

    assert not output.exists()
    assert not Path(f"{output}.part").exists()


def test_decoded_frame_to_rgb_bgr_returns_contiguous() -> None:
    """AUD-34803：BGR 系 to_rgb 不得返回负 stride 的 flip 视图。

    负 stride 数组对按连续内存消费的下游（编码器写入/GL 纹理上传/
    buffer 协议序列化）是隐形坑；同时校验通道序确为 BGR→RGB。
    """
    frame = _frame(index=1)
    rgb = frame.to_rgb()

    assert rgb is not None
    assert rgb.flags["C_CONTIGUOUS"]
    assert all(stride > 0 for stride in rgb.strides)
    # 通道序：bgr24 数据的 B/R 两通道互换后应与源互补
    assert rgb[0, 0, 0] == frame.data[0, 0, 2]
    assert rgb[0, 0, 2] == frame.data[0, 0, 0]
