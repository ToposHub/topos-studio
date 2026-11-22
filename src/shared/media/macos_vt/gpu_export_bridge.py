"""Composable Metal-to-VideoToolbox export bridge for the M5 smoke path."""

from __future__ import annotations

import os
import threading
from collections.abc import Callable
from pathlib import Path
from tempfile import mkstemp
from typing import TYPE_CHECKING, Any

from src.shared.export.gpu_encode_surface import GpuEncodeSurface
from src.shared.media.macos_vt.gpu_encode_surface import (
    VideoToolboxGpuSurfaceProvider,
)
from src.shared.media.macos_vt.vt_compression import (
    VideoToolboxCompressionError,
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

    By default the finished movie replaces the destination atomically.
    ``expected_frames`` enables exact packet-count validation. An existing
    destination, an expected count, or a cancellation check requires staging.
    ``should_cancel`` must read caller state without blocking or calling back
    into the bridge.
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
        h264_profile: str | None = None,
        bit_rate: int | None = None,
        bt709_limited: bool = False,
        expected_frames: int | None = None,
        transactional: bool = True,
        should_cancel: Callable[[], bool] | None = None,
    ) -> None:
        self.output_path = str(output_path)
        self.width = int(width)
        self.height = int(height)
        self.fps = max(1, int(fps))
        self.codec = str(codec).strip().lower()
        if expected_frames is not None and (type(expected_frames) is not int or expected_frames <= 0):
            raise ValueError("expected_frames must be a positive integer")
        self._expected_frames = expected_frames
        self._should_cancel = should_cancel
        self._staged_path: Path | None = None
        self._closed = False
        self._abort_requested = False
        self._published = False
        self._close_lock = threading.Lock()

        target = Path(self.output_path)
        muxer = None
        session = None
        try:
            if (transactional or expected_frames is not None or should_cancel is not None
                    or os.path.lexists(target)):
                fd, temp = mkstemp(prefix=f".{target.stem}.topos-", suffix=target.suffix, dir=target.parent)
                self._staged_path = Path(temp)
                os.close(fd)
            muxer = VideoToolboxPacketMuxer(
                str(self._staged_path or target),
                width=self.width,
                height=self.height,
                fps=self.fps,
                codec=codec,
                pixel_format=pixel_format,
                container_format=container_format,
                bt709_limited=bt709_limited,
            )
            session = VideoToolboxCompressionSession(
                width=self.width,
                height=self.height,
                codec=codec,
                fps=self.fps,
                # T-P0-31：会话必须与 surface/muxer 同像素格式——此前漏传
                # 导致 P010 surface 喂进 8-bit 默认会话（HEVC Main 而非
                # Main10，probe 位深不符）。
                pixel_format=str(pixel_format).strip().lower(),
                on_packet=muxer.write_packet,
                require_hardware=require_hardware,
                h264_profile=h264_profile,
                bit_rate=bit_rate,
                bt709_limited=bt709_limited,
            )
            self._provider = VideoToolboxGpuSurfaceProvider(
                metal_context,
                submitter=session.submit,
            )
            self._session = session
            self._muxer = muxer
        except BaseException:
            try:
                if session is not None:
                    session.abort()
            finally:
                try:
                    if muxer is not None:
                        muxer.abort()
                finally:
                    self._discard_staged_output()
            raise

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

    def close(self) -> bool:
        """Drain the native encoder and return whether the movie was published."""
        if not self._begin_shutdown(abort=False):
            with self._close_lock:
                return self._published
        try:
            try:
                self._session.close()
            finally:
                self._provider.close()
            if self._session.last_error or self._session.dropped_packets:
                raise VideoToolboxCompressionError(
                    self._session.last_error or "videotoolbox_compression_dropped_packets")
            count = self.packet_count
            if self._expected_frames is not None and count != self._expected_frames:
                raise VideoToolboxCompressionError(
                    f"videotoolbox_packet_count_mismatch:{count}!={self._expected_frames}")
            self._muxer.close()
            with self._close_lock:
                if self._abort_requested or (self._should_cancel is not None and self._should_cancel()):
                    return False
                if count and self._staged_path is not None:
                    if self._staged_path.stat().st_size == 0:
                        raise VideoToolboxCompressionError("videotoolbox_empty_staged_output")
                    os.replace(self._staged_path, self.output_path)
                self._published = bool(count)
                return self._published
        finally:
            try:
                self._muxer.abort()
            finally:
                self._discard_staged_output()

    def abort(self) -> None:
        """Discard this export and keep any previously published destination."""
        if not self._begin_shutdown(abort=True):
            return
        try:
            self._session.abort()
        finally:
            try:
                self._provider.close()
            finally:
                try:
                    self._muxer.abort()
                finally:
                    self._discard_staged_output()

    def _begin_shutdown(self, *, abort: bool) -> bool:
        with self._close_lock:
            if abort:
                self._abort_requested = True
            if self._closed:
                return False
            self._closed = True
            return True

    def _discard_staged_output(self) -> None:
        if self._staged_path is not None:
            self._staged_path.unlink(missing_ok=True)
            self._staged_path = None

    def __enter__(self) -> "VideoToolboxGpuExportBridge":
        return self

    def __exit__(self, exc_type: type[BaseException] | None, *_exc: object) -> None:
        if exc_type is None:
            self.close()
        else:
            self.abort()


__all__ = ["VideoToolboxGpuExportBridge"]
