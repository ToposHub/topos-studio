"""Composable Metal-to-VideoToolbox export bridge for the M5 smoke path."""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING, Any

from src.shared.export.gpu_encode_surface import GpuEncodeSurface
from src.shared.media.macos_vt.gpu_encode_surface import (
    VideoToolboxGpuSurfaceProvider,
)
from src.shared.media.macos_vt.vt_compression import (
    VideoToolboxCompressionSession,
    VideoToolboxPacketMuxer,
)

if TYPE_CHECKING:
    import numpy as np


class VideoToolboxGpuExportBridge:
    """Own the native submitter, compressed packet muxer, and surface pool.

    This is intentionally a small composable bridge.  It can be passed as the
    ``gpu_encode_surface_provider`` to ``TimelineExporter`` through its
    ``acquire`` method, while the caller owns the bridge lifetime and calls
    ``close`` after the export loop.  It supports single-layer Metal NV12 or
    P010 output into VideoToolbox H.264/HEVC MP4/MOV when the caller selects
    a matching pixel format and codec.
    """

    # TimelineExporter may be given this bridge directly.  The bridge owns the
    # native compression session, muxer, and surface bridge, so it is safe for
    # the exporter to close it after the encoder has flushed.
    close_on_export = True
    handles_timeline_video = True

    def __init__(
        self,
        metal_context: Any,
        output_path: str,
        *,
        width: int,
        height: int,
        fps: int = 30,
        codec: str = "h264",
        pixel_format: str = "nv12",
        container_format: str | None = None,
        require_hardware: bool = True,
    ) -> None:
        self.output_path = str(output_path)
        self.width = int(width)
        self.height = int(height)
        self.fps = max(1, int(fps))
        self.codec = str(codec).strip().lower()
        self._muxer = VideoToolboxPacketMuxer(
            output_path,
            width=self.width,
            height=self.height,
            fps=self.fps,
            codec=codec,
            pixel_format=pixel_format,
            container_format=container_format,
        )
        session = None
        try:
            session = VideoToolboxCompressionSession(
                width=self.width,
                height=self.height,
                codec=codec,
                fps=self.fps,
                # T-P0-31：会话必须与 surface/muxer 同像素格式——此前漏传
                # 导致 P010 surface 喂进 8-bit 默认会话（HEVC Main 而非
                # Main10，probe 位深不符）。
                pixel_format=str(pixel_format).strip().lower(),
                on_packet=self._muxer.write_packet,
                require_hardware=require_hardware,
            )
            self._provider = VideoToolboxGpuSurfaceProvider(
                metal_context,
                submitter=session.submit,
            )
            self._session = session
        except Exception:
            if session is not None:
                session.close()
            self._muxer.close()
            raise
        self._closed = False
        # AUD-35010：close() 防重入标志的锁
        self._close_lock = threading.Lock()

    @property
    def provider(self) -> VideoToolboxGpuSurfaceProvider:
        """Surface provider suitable for TimelineExporter injection."""
        return self._provider

    @property
    def packet_count(self) -> int:
        return int(self._muxer.packet_count)

    def add_audio_stream(
        self,
        codec: str = "aac",
        sample_rate: int = 48000,
        channels: int = 2,
        bitrate: int | None = None,
    ) -> None:
        """Configure TimelineExporter audio before the first video packet."""
        self._muxer.add_audio_stream(
            codec=codec,
            sample_rate=sample_rate,
            channels=channels,
            bitrate=bitrate,
        )

    def write_audio_samples(
        self,
        samples: np.ndarray,  # AUD-35010：float-planar 音频样本，弱类型 Any 收紧
        sample_rate: int = 48000,
    ) -> None:
        self._muxer.write_audio_samples(samples, sample_rate=sample_rate)

    def acquire(self, **kwargs: Any) -> GpuEncodeSurface:
        return self._provider.acquire(**kwargs)

    def submit(self, surface: GpuEncodeSurface) -> dict[str, Any]:
        return self._session.submit(surface)

    def close(self) -> None:
        # AUD-35010：防重入标志的检查-置位放入实例锁，两个线程并发
        # close 不再都执行关闭链
        with self._close_lock:
            if self._closed:
                return
            self._closed = True
        try:
            self._session.close()
        finally:
            try:
                self._provider.close()
            finally:
                self._muxer.close()

    def __enter__(self) -> "VideoToolboxGpuExportBridge":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


__all__ = ["VideoToolboxGpuExportBridge"]
