"""Palette-based animated GIF encoder for timeline delivery.

GIF is deliberately implemented outside :mod:`video_encoder`: FFmpeg/PyAV
video encoders are designed around streaming encoded packets, while Pillow
gives us the deterministic palette-size control required by the Deliver and
Quick Export settings.  Rendered frames are spooled to a temporary directory
so a long export does not retain the complete animation in RAM.
"""

from __future__ import annotations

import logging
import os
import shutil
import tempfile
from typing import Any, Iterator, Optional

import numpy as np
from PIL import Image


logger = logging.getLogger(__name__)


class GifVideoEncoder:
    """Encode BGR/RGB ``DecodedFrame`` objects as an animated GIF."""

    supports_audio = False
    hw_fallback_reason = ""
    hw_encoder_name = "pillow_gif"

    def __init__(self, output_path: str, config: Any) -> None:
        self.output_path = os.fspath(output_path)
        self._config = config
        self._color_count = int(getattr(config, "gif_color_count", 256))
        if not 2 <= self._color_count <= 256:
            raise ValueError("GIF color count must be between 2 and 256")

        fps = getattr(config, "fps", None)
        self._fps = float(fps.to_fps()) if hasattr(fps, "to_fps") else float(fps or 0.0)
        if self._fps <= 0.0:
            raise ValueError("GIF frame rate must be positive")
        # GIF stores frame delays in centiseconds.  Values below 10 ms are not
        # representable, so reject an accidental request that would silently
        # become a different animation speed.
        if self._fps > 100.0:
            raise ValueError("GIF frame rate cannot exceed 100 fps")
        self._duration_ms = max(
            10, int(round((1000.0 / self._fps) / 10.0) * 10)
        )

        self._frame_dir: Optional[str] = None
        self._frame_count = 0
        self._is_open = False

    @property
    def is_open(self) -> bool:
        """Whether this encoder is accepting frames."""
        return self._is_open

    @property
    def frame_count(self) -> int:
        """Number of frames accepted by the encoder."""
        return self._frame_count

    def open(self) -> None:
        """Create the temporary frame spool."""
        if self._is_open:
            return
        parent = os.path.dirname(os.path.abspath(self.output_path)) or "."
        if not os.path.isdir(parent):
            raise FileNotFoundError(f"GIF output directory does not exist: {parent}")
        partial_path = self._partial_output_path()
        if os.path.exists(partial_path):
            os.remove(partial_path)
        self._frame_dir = tempfile.mkdtemp(prefix="topos-gif-", dir=parent)
        self._frame_count = 0
        self._is_open = True

    def encode_frame(self, frame: Any) -> None:
        """Quantize and spool one rendered frame."""
        if not self._is_open or self._frame_dir is None:
            raise RuntimeError("GIF encoder is not open")

        array, pixel_format = self._frame_array(frame)
        image = self._image_from_array(array, pixel_format)
        if image.size != (int(self._config.width), int(self._config.height)):
            raise ValueError(
                "GIF frame dimensions do not match encoder config: "
                f"{image.width}x{image.height} != "
                f"{self._config.width}x{self._config.height}"
            )

        if image.mode == "RGBA":
            # FASTOCTREE is the Pillow quantizer that supports RGBA input.
            palette = image.quantize(
                colors=self._color_count,
                method=Image.Quantize.FASTOCTREE,
                dither=Image.Dither.FLOYDSTEINBERG,
            )
        else:
            palette = image.quantize(
                colors=self._color_count,
                method=Image.Quantize.MEDIANCUT,
                dither=Image.Dither.FLOYDSTEINBERG,
            )

        frame_path = os.path.join(
            self._frame_dir, f"frame_{self._frame_count:08d}.png"
        )
        palette.save(frame_path, format="PNG", optimize=False)
        self._frame_count += 1

    def add_audio_stream(self, **_kwargs: Any) -> None:
        """Reject audio explicitly; GIF containers have no audio track."""
        raise ValueError("GIF output does not support audio")

    def close(self) -> None:
        """Assemble the spooled frames and atomically publish the GIF."""
        if not self._is_open:
            return
        if self._frame_dir is None or self._frame_count == 0:
            self.abort()
            raise ValueError("Cannot write an animated GIF without frames")

        partial_path = self._partial_output_path()
        try:
            # AUD-33205：逐帧惰性回读（生成器）——此前把全部已落盘帧
            # 一次性解码进 images 列表再交给 Pillow，模块 docstring 声明
            # 的"不在 RAM 保留完整动画"在收尾阶段被违背（4K P 模式约
            # 8.3MB/帧 × 数百帧可达数 GB 的突发峰值）。Pillow 的
            # append_images 接受可迭代对象，逐帧回读后仅由 Pillow 持有
            # 其编码所需的帧，本模块不再额外整表驻留。
            with Image.open(self._spooled_frame_path(0)) as first:
                first.load()  # 解码完成后即可释放文件句柄
                first.save(
                    partial_path,
                    format="GIF",
                    save_all=True,
                    append_images=self._iter_spooled_frames(1),
                    duration=self._duration_ms,
                    loop=0,
                    disposal=2,
                    optimize=True,
                )
            os.replace(partial_path, self.output_path)
        except Exception:
            try:
                if os.path.exists(partial_path):
                    os.remove(partial_path)
            finally:
                self._cleanup_spool()
                self._is_open = False
            raise

        self._cleanup_spool()
        self._is_open = False

    def abort(self) -> None:
        """Discard temporary frames and any incomplete output."""
        partial_path = self._partial_output_path()
        try:
            if os.path.exists(partial_path):
                os.remove(partial_path)
        finally:
            self._cleanup_spool()
            self._is_open = False

    def _partial_output_path(self) -> str:
        return f"{self.output_path}.part"

    def _spooled_frame_path(self, index: int) -> str:
        """Return the spool path of one quantized frame."""
        if self._frame_dir is None:
            raise RuntimeError("GIF encoder spool directory is not available")
        return os.path.join(self._frame_dir, f"frame_{index:08d}.png")

    def _iter_spooled_frames(self, start: int) -> Iterator[Image.Image]:
        """Lazily re-read spooled frames starting at *start* (AUD-33205).

        每帧 ``load()`` 后即让文件句柄随 ``with`` 退出关闭，同一时刻至多
        一个帧文件处于打开状态；供 Pillow ``append_images`` 惰性消费。
        """
        for index in range(start, self._frame_count):
            with Image.open(self._spooled_frame_path(index)) as frame:
                frame.load()
                yield frame

    def _cleanup_spool(self) -> None:
        if self._frame_dir is not None:
            shutil.rmtree(self._frame_dir, ignore_errors=True)
            self._frame_dir = None

    @staticmethod
    def _frame_array(frame: Any) -> tuple[np.ndarray, str]:
        data = getattr(frame, "data", frame)
        if data is None:
            raise TypeError("GIF encoder requires a host-visible frame")
        array = np.asarray(data)
        if array.ndim != 3 or array.shape[2] not in (3, 4):
            raise TypeError(f"GIF encoder requires HxWx3/HxWx4 data, got {array.shape}")

        if array.dtype != np.uint8:
            if np.issubdtype(array.dtype, np.integer):
                shift = max(0, array.dtype.itemsize * 8 - 8)
                array = (array >> shift).astype(np.uint8)
            else:
                finite = np.nan_to_num(array, nan=0.0, posinf=1.0, neginf=0.0)
                if float(np.max(finite)) <= 1.0:
                    finite = finite * 255.0
                array = np.clip(finite, 0.0, 255.0).astype(np.uint8)

        pixel_format = str(getattr(frame, "pixel_format", "bgr24") or "bgr24").lower()
        return np.ascontiguousarray(array), pixel_format

    @staticmethod
    def _image_from_array(array: np.ndarray, pixel_format: str) -> Image.Image:
        if pixel_format.startswith("bgra") or pixel_format == "bgra":
            return Image.fromarray(array[..., [2, 1, 0, 3]], mode="RGBA")
        if pixel_format.startswith("bgr") or pixel_format == "bgr24":
            return Image.fromarray(array[..., :3][..., ::-1], mode="RGB")
        if pixel_format.startswith("rgba"):
            return Image.fromarray(array, mode="RGBA")
        return Image.fromarray(array[..., :3], mode="RGB")


__all__ = ["GifVideoEncoder"]
