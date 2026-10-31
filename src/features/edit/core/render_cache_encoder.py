"""Render Cache encoder — wraps FFmpegVideoEncoder for cache file creation."""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass

import numpy as np

logger = logging.getLogger(__name__)


@dataclass
class RenderCacheEncoderConfig:
    """Configuration for the render cache encoder."""
    output_path: str
    temp_path: str
    width: int
    height: int
    fps_num: int = 24
    fps_den: int = 1
    codec: str = "topos_proxy"


class RenderCacheEncoder:
    """Encodes rendered frames to a cache video file.

    Wraps FFmpegVideoEncoder with atomic write (temp → final rename).
    Thread-safe: intended for use from background render thread.
    """

    def __init__(self, config: RenderCacheEncoderConfig) -> None:
        self._config = config
        self._encoder = None
        self._is_open = False

    def open(self) -> None:
        """Open encoder and prepare output file."""
        # Determine codec and profile from the cache codec string
        codec, profile, pix_fmt = self._parse_codec(self._config.codec)

        # Rational lives with the media metadata model; keep the cache
        # encoder on the same public media type used by the export layer.
        from src.shared.media.metadata import Rational

        # Parse fps
        fps = Rational(self._config.fps_num, self._config.fps_den)

        # FFmpegEncoderConfig 是两种编码器共用的运行时配置类型
        from src.shared.export.video_encoder import FFmpegEncoderConfig

        if codec == "topos":
            # P8-Topos：渲染缓存可选自研中间片（无 PyAV 依赖）
            from src.shared.export.topos_encoder import ToposVideoEncoder
            enc_config = FFmpegEncoderConfig(
                codec=codec,
                width=self._config.width,
                height=self._config.height,
                fps=fps,
                profile=profile,
                pix_fmt=pix_fmt,
                has_alpha=False,
                threads=2,
            )
            os.makedirs(os.path.dirname(self._config.temp_path) or ".", exist_ok=True)
            # R6 调度策略：渲染缓存为后台任务——codec 池限 1 线程
            # （避免与前台导出/预览解码争抢；池上限保证总线程有界）
            self._encoder = ToposVideoEncoder(
                self._config.temp_path, enc_config, slice_threads=1)
            self._encoder.open()
            self._is_open = True
            logger.info(
                "RenderCacheEncoder opened (topos): %s (%dx%d @ %d/%d fps)",
                self._config.temp_path, self._config.width, self._config.height,
                self._config.fps_num, self._config.fps_den,
            )
            return

        try:
            import av  # noqa: F401 — PyAV 可用性探测，缺失时抛安装提示
        except ImportError:
            raise RuntimeError("PyAV is required for render cache encoding")

        from src.shared.export.video_encoder import FFmpegVideoEncoder

        # H.264 缓存仍可由用户显式选择并保留其硬件编码路径；默认的
        # Topos Proxy 在上面的分支走自研帧内编码器，便于 CPU fallback
        # 逐帧读取和快速随机访问。
        enc_config = FFmpegEncoderConfig(
            codec=codec,
            width=self._config.width,
            height=self._config.height,
            fps=fps,
            profile=profile,
            pix_fmt=pix_fmt,
            has_alpha=False,
            threads=2,
        )

        # Ensure temp directory exists
        os.makedirs(os.path.dirname(self._config.temp_path) or ".", exist_ok=True)

        self._encoder = FFmpegVideoEncoder(self._config.temp_path, enc_config)
        self._encoder.open()
        self._is_open = True
        logger.info(
            "RenderCacheEncoder opened: %s (%dx%d @ %d/%d fps, %s)",
            self._config.temp_path, self._config.width, self._config.height,
            self._config.fps_num, self._config.fps_den, self._config.codec,
        )

    def write_frame_bgr(self, frame: np.ndarray) -> None:
        """Write a BGR uint8 frame to the cache file.

        BGR 数据原样直入：ToposVideoEncoder 的 native 融合转换与
        FFmpegVideoEncoder 的 swscale 直填路径都原生接受 bgr 输入
        （此前在这里整帧 BGR→RGB 翻转拷贝是纯浪费，T-03 消除）。

        Args:
            frame: BGR numpy array, shape (H, W, 3), dtype uint8
        """
        if not self._is_open or self._encoder is None:
            return

        from src.shared.media.frame import DecodedFrame

        decoded = DecodedFrame(
            frame_index=0,
            pts=None,
            time_seconds=0.0,
            data=frame,
            pixel_format='bgr',
            dtype=np.dtype('uint8'),
            width=frame.shape[1],
            height=frame.shape[0],
        )
        self.write_frame(decoded)

    def write_frame(self, frame: object) -> None:
        """Write a decoded frame without forcing a packed CPU representation.

        Topos render-cache output can already be a validated planar
        ``DecodedFrame`` (Y/U/V[/A]).  Pass that object straight to the
        encoder so the Topos path can preserve the planes; callers that still
        render packed BGR continue to use :meth:`write_frame_bgr`.
        """
        if not self._is_open or self._encoder is None:
            return
        width = int(getattr(frame, "width", 0) or 0)
        height = int(getattr(frame, "height", 0) or 0)
        if width != int(self._config.width) or height != int(self._config.height):
            raise ValueError(
                "RenderCacheEncoder: decoded frame geometry "
                f"{width}x{height} != cache {self._config.width}x{self._config.height}"
            )
        self._encoder.encode_frame(frame)

    def write_frame_planar(self, frame: object) -> None:
        """Explicit alias for the planar render-cache fast path."""
        self.write_frame(frame)

    def close(self) -> bool:
        """Close encoder and atomically rename temp to final path.

        Returns:
            True if file was successfully written and renamed.
        """
        if not self._is_open or self._encoder is None:
            return False

        try:
            self._encoder.close()
            self._is_open = False

            # Atomic rename
            if os.path.exists(self._config.temp_path):
                # ``replace`` keeps the old cache visible until the new file
                # is complete, then swaps it atomically on supported local
                # filesystems.  This avoids a brief missing-cache window
                # during re-render and preserves the last good cache if an
                # encode fails before this point.
                os.replace(self._config.temp_path, self._config.output_path)
                logger.info("Render cache file written: %s", self._config.output_path)
                return True
            else:
                logger.error("Temp file missing after close: %s", self._config.temp_path)
                return False
        except Exception as e:
            logger.error("Failed to finalize render cache: %s", e)
            return False

    def abort(self) -> None:
        """Abort encoding and clean up temp file."""
        encoder = self._encoder
        self._is_open = False
        self._encoder = None

        # FFmpegVideoEncoder owns the PyAV container/file descriptor.  Close
        # it before deleting the temporary path so cancellation and failures
        # do not leak a handle in the background render thread.  Topos 视频
        # 编码器自带 abort（丢弃未定稿数据 + 删自己的临时文件）。
        if encoder is not None:
            try:
                if hasattr(encoder, "abort"):
                    encoder.abort()
                elif getattr(encoder, "is_open", False):
                    encoder.close()
            except Exception as e:
                logger.debug("Render cache encoder close during abort failed: %s", e)

        # Clean up temp file
        try:
            if os.path.exists(self._config.temp_path):
                os.remove(self._config.temp_path)
        except Exception as e:
            logger.warning("Failed to remove temp cache file: %s", e)

    @staticmethod
    def _parse_codec(codec_str: str) -> tuple:
        """Parse codec string into (codec, profile, pix_fmt) for FFmpeg."""
        if str(codec_str or "").lower() in {
            "vp8", "vp8_proxy", "vp8_high_quality", "vp9", "vp9_proxy",
            "vp9_high_quality",
        }:
            raise ValueError(
                "VP8/VP9 Motion Graphic caches must use the direct realtime recorder"
            )
        mapping = {
            # DNxHR selects its profile through the private ``profile``
            # option; the PyAV pixel format must remain a real FFmpeg format.
            "dnxhr_lb": ("dnxhr", "lb", "yuv422p"),
            "dnxhr_sq": ("dnxhr", "sq", "yuv422p"),
            "dnxhr_hq": ("dnxhr", "hq", "yuv422p"),
            "dnxhr_hqx": ("dnxhr", "hqx", "yuv422p10le"),
            "dnxhr_444": ("dnxhr", "444", "yuva444p10le"),
            # Use the logical codec name here so FFmpegEncoderConfig can apply
            # its normal H.264 rate-control/preset handling.
            "h264_proxy": ("h264", None, "yuv420p"),
            # P8-Topos：自研中间片缓存（默认使用 Proxy 档）。
            "topos_proxy": ("topos", "proxy", "yuv422p10le"),
            "topos": ("topos", "standard", "yuv422p10le"),
        }
        # 复验 P1-08 同类：未知 codec 显式失败，避免缓存编码静默切换档位。
        if codec_str not in mapping:
            raise ValueError(
                f"RenderCacheEncoder: 未知缓存 codec {codec_str!r}"
                f"（合法：{sorted(mapping)}）——拒绝静默回退代理档")
        return mapping[codec_str]
