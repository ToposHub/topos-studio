"""Isolate the native Main 10 encoder and atomically publish its result."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
import multiprocessing
from multiprocessing.connection import Connection
from multiprocessing.process import BaseProcess
from multiprocessing.shared_memory import SharedMemory
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, TYPE_CHECKING

import numpy as np

if TYPE_CHECKING:
    from src.shared.export.video_encoder import FFmpegEncoderConfig
    from src.shared.media.frame import DecodedFrame


def _encode_main10(
    channel: Connection, buffer_name: str, path: str, config: FFmpegEncoderConfig,
    total_frames: int | None,
) -> None:
    from src.shared.export.video_encoder import FFmpegVideoEncoder
    from src.shared.media.frame import DecodedFrame

    buffer = SharedMemory(name=buffer_name)
    encoder = FFmpegVideoEncoder(
        path, replace(config, hwaccel=None, pipelined_conversion=False,
                      gop_size=1 if total_frames in (1, 2) else config.gop_size),
        isolated_hdr_main10=True,
    )
    try:
        encoder.open()
        channel.send(None)
        while True:
            operation, arguments = channel.recv()
            if operation == "video":
                frame_index, pts, seconds = arguments
                pixels = np.ndarray((config.height, config.width, 3), np.uint16, buffer=buffer.buf)
                encoder.encode_frame(DecodedFrame(
                    frame_index=frame_index, pts=pts, time_seconds=seconds, data=pixels,
                    width=config.width, height=config.height, pixel_format="bgr48le",
                ))
                del pixels
            elif operation == "audio_stream":
                encoder.add_audio_stream(**arguments)
            elif operation == "audio":
                encoder.write_audio_samples(*arguments)
            elif operation == "close":
                encoder.close(expected_frames=arguments)
                channel.send(None)
                return
            else:
                raise ValueError(f"Unknown HDR encoder operation {operation!r}")
            channel.send(None)
    except EOFError:
        return
    except Exception as error:
        channel.send(f"{type(error).__name__}: {error}")
    finally:
        buffer.close()
        channel.close()


class HdrHevcEncoder:
    """Timeline encoder using a single shared BGR16 frame and a control pipe."""

    def __init__(
        self, output_path: str, config: FFmpegEncoderConfig, *,
        should_cancel: Callable[[], bool] | None = None, total_frames: int | None = None,
    ) -> None:
        self.output_path = Path(output_path)
        self.config = config
        self.frame_count = 0
        self._stage: TemporaryDirectory[str] | None = None
        self._buffer: SharedMemory | None = None
        self._channel: Connection | None = None
        self._process: BaseProcess | None = None
        self._should_cancel = should_cancel
        self._total_frames = total_frames

    def open(self) -> None:
        if self._process is not None:
            raise RuntimeError("HDR HEVC encoder is already open")
        self.frame_count = 0
        context = multiprocessing.get_context("spawn")
        self.output_path.parent.mkdir(parents=True, exist_ok=True)
        self._stage = TemporaryDirectory(prefix=".topos-hdr-", dir=self.output_path.parent)
        self._staged_file = Path(self._stage.name) / self.output_path.name
        try:
            self._buffer = SharedMemory(create=True, size=self.config.width * self.config.height * 6)
            self._channel, child = context.Pipe()
            try:
                self._process = context.Process(
                    target=_encode_main10,
                    args=(child, self._buffer.name, str(self._staged_file), self.config, self._total_frames),
                    name="Topos HDR HEVC",
                    daemon=True,
                )
                self._process.start()
            finally:
                child.close()
            if not self._receive(self._should_cancel):
                self.abort()
        except BaseException:
            self.abort()
            raise

    def _receive(self, should_cancel: Callable[[], bool] | None = None) -> bool:
        from time import monotonic

        channel, process = self._channel, self._process
        if channel is None or process is None:
            raise RuntimeError("HDR HEVC encoder is closed")
        deadline = monotonic() + 120.0
        while True:
            if should_cancel is not None and should_cancel():
                return False
            if channel.poll(0.1):
                break
            if process.exitcode is not None or monotonic() >= deadline:
                raise RuntimeError(f"HDR HEVC encoder stopped responding (exit {process.exitcode})")
        if should_cancel is not None and should_cancel():
            return False
        try:
            error = channel.recv()
        except EOFError as error:
            process.join(timeout=0.1)
            raise RuntimeError(f"HDR HEVC encoder exited ({process.exitcode})") from error
        if error is not None:
            raise RuntimeError(f"HDR HEVC encode failed: {error}")
        return True

    def _send(self, operation: str, arguments: Any) -> None:
        if self._channel is None:
            raise RuntimeError("HDR HEVC encoder is closed")
        try:
            self._channel.send((operation, arguments))
        except (BrokenPipeError, OSError) as error:
            raise RuntimeError(f"HDR HEVC encoder exited ({self._process.exitcode})") from error

    def encode_frame(self, frame: DecodedFrame) -> None:
        expected_shape = (self.config.height, self.config.width, 3)
        if (frame.data is None or frame.data.dtype != np.uint16 or frame.data.shape != expected_shape
                or frame.pixel_format != "bgr48le"):
            raise ValueError("HDR HEVC requires a full-size BGR16 frame")
        if self._buffer is None:
            raise RuntimeError("HDR HEVC encoder is closed")
        pixels = np.ndarray(expected_shape, dtype=np.uint16, buffer=self._buffer.buf)
        pixels[:] = frame.data
        del pixels
        self._send("video", (frame.frame_index, frame.pts, frame.time_seconds))
        if self._receive(self._should_cancel):
            self.frame_count += 1
        else:
            self.abort()

    def add_audio_stream(self, **settings: Any) -> None:
        self._send("audio_stream", settings)
        if not self._receive(self._should_cancel):
            self.abort()

    def write_audio_samples(self, samples: np.ndarray, sample_rate: int = 48000) -> None:
        for start in range(0, samples.shape[-1], 65536):
            self._send("audio", (samples[..., start:start + 65536], sample_rate))
            if not self._receive(self._should_cancel):
                self.abort()
                return

    def close(
        self, *, should_cancel: Callable[[], bool] | None = None,
        expected_frames: int | None = None,
    ) -> bool:
        try:
            if should_cancel is not None and should_cancel():
                return False
            self._send("close", expected_frames or self.frame_count)
            if not self._receive(should_cancel) or (should_cancel is not None and should_cancel()):
                return False
            os.replace(self._staged_file, self.output_path)
            return True
        finally:
            self.abort()

    def abort(self) -> None:
        process, self._process = self._process, None
        if process is not None and process.pid is not None:
            process.join(timeout=0.1)
            if process.is_alive():
                process.terminate()
                process.join(timeout=2.0)
                if process.is_alive():
                    process.kill()
                    process.join()
            process.close()
        if self._channel is not None:
            self._channel.close()
            self._channel = None
        if self._buffer is not None:
            self._buffer.close()
            self._buffer.unlink()
            self._buffer = None
        if self._stage is not None:
            self._stage.cleanup()
            self._stage = None
