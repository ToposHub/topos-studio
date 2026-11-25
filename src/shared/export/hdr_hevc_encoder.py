"""Isolate the native Main 10 encoder and atomically publish its result."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
import multiprocessing
from multiprocessing.connection import Connection
from multiprocessing.process import BaseProcess
from multiprocessing.shared_memory import SharedMemory
from mmap import PAGESIZE
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic
from typing import Any, TYPE_CHECKING

import numpy as np

if TYPE_CHECKING:
    from src.shared.export.video_encoder import FFmpegEncoderConfig
    from src.shared.media.frame import DecodedFrame


ArrayLayout = tuple[tuple[int, ...], str, int]


def _encode_main10(
    channel: Connection, buffer_name: str, path: str, config: FFmpegEncoderConfig,
    total_frames: int | None, centered_chroma: bool,
) -> None:
    from src.shared.export.video_encoder import FFmpegVideoEncoder
    from src.shared.media.frame import DecodedFrame

    buffer = SharedMemory(name=buffer_name)
    pix_fmt = config.pix_fmt
    profile = config.profile
    if pix_fmt.lower() == "p010le":
        # P010 is the hardware surface layout. libx265 accepts the same
        # ten-bit samples in planar 4:2:0 storage.
        pix_fmt = "yuv420p10le"
        if profile == "main":
            profile = "main10"
    encoder = FFmpegVideoEncoder(
        path, replace(config, hwaccel=None, pipelined_conversion=False,
                      pix_fmt=pix_fmt, profile=profile,
                      gop_size=1 if total_frames in (1, 2) else config.gop_size),
        isolated_hdr_main10=True, centered_main10_chroma=centered_chroma,
    )
    try:
        # POSIX shared memory can map successfully with insufficient backing
        # space and fault on the first write. Touch each page here so the
        # worker owns that failure and the editor never writes an unbacked page.
        page_count = (buffer.size + PAGESIZE - 1) // PAGESIZE
        buffer.buf[::PAGESIZE] = bytes(page_count)
        encoder.open()
        channel.send(None)
        while True:
            operation, arguments = channel.recv()
            if operation == "video":
                frame_index, pts, seconds, width, height, pixel_format, packed, layouts = arguments
                arrays = tuple(np.ndarray(shape, dtype, buffer=buffer.buf, offset=offset)
                               for shape, dtype, offset in layouts)
                encoder.encode_frame(DecodedFrame(
                    frame_index=frame_index, pts=pts, time_seconds=seconds,
                    width=width, height=height, pixel_format=pixel_format,
                    data=arrays[0] if packed else None, planes=() if packed else arrays,
                ))
                del arrays
            elif operation == "audio_stream":
                encoder.add_audio_stream(**arguments)
            elif operation == "audio":
                (shape, dtype, offset), sample_rate = arguments
                samples = np.ndarray(shape, dtype, buffer=buffer.buf, offset=offset)
                encoder.write_audio_samples(samples, sample_rate)
                del samples
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
        try:
            channel.send(f"{type(error).__name__}: {error}")
        except OSError:
            pass
    finally:
        buffer.close()
        channel.close()


class HdrHevcEncoder:
    """Isolated encoder using one shared media buffer and a control pipe."""

    def __init__(
        self, output_path: str, config: FFmpegEncoderConfig, *,
        should_cancel: Callable[[], bool] | None = None, total_frames: int | None = None,
        centered_chroma: bool = True,
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
        self._centered_chroma = centered_chroma
        self._cancelled = False

    @property
    def is_open(self) -> bool:
        return self._process is not None

    def open(self) -> None:
        if self._process is not None:
            raise RuntimeError("HDR HEVC encoder is already open")
        self.frame_count = 0
        self._cancelled = False
        if self._should_cancel is not None and self._should_cancel():
            self._cancelled = True
            return
        context = multiprocessing.get_context("spawn")
        self.output_path.parent.mkdir(parents=True, exist_ok=True)
        self._stage = TemporaryDirectory(prefix=".topos-hdr-", dir=self.output_path.parent)
        self._staged_file = Path(self._stage.name) / self.output_path.name
        try:
            self._buffer = SharedMemory(
                create=True, size=max(4096, self.config.width * self.config.height * 8),
            )
            self._channel, child = context.Pipe()
            try:
                self._process = context.Process(
                    target=_encode_main10,
                    args=(child, self._buffer.name, str(self._staged_file), self.config,
                          self._total_frames, self._centered_chroma),
                    name="Topos HDR HEVC",
                    daemon=True,
                )
                self._process.start()
            finally:
                child.close()
            if not self._receive(self._should_cancel):
                self._cancelled = True
                self.abort()
        except BaseException:
            self.abort()
            raise

    def _receive(self, should_cancel: Callable[[], bool] | None = None) -> bool:
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
            exitcode = self._process.exitcode if self._process is not None else None
            raise RuntimeError(f"HDR HEVC encoder exited ({exitcode})") from error

    def _exchange(self, operation: str, arguments: Any) -> bool:
        try:
            self._send(operation, arguments)
            if self._receive(self._should_cancel):
                return True
        except BaseException:
            self.abort()
            raise
        self._cancelled = True
        self.abort()
        return False

    def _write_arrays(self, arrays: tuple[np.ndarray, ...]) -> tuple[ArrayLayout, ...]:
        buffer = self._buffer
        if buffer is None:
            raise RuntimeError("HDR HEVC encoder is closed")
        layouts = []
        offset = 0
        for array in arrays:
            if array.dtype.kind not in ("u", "i", "f"):
                raise ValueError("HDR HEVC requires numeric media samples")
            alignment = array.dtype.alignment
            offset += (-offset) % alignment
            if offset + array.nbytes > buffer.size:
                raise ValueError("HDR HEVC input exceeds the shared media buffer")
            view = np.ndarray(array.shape, array.dtype, buffer=buffer.buf, offset=offset)
            np.copyto(view, array, casting="no")
            layouts.append((array.shape, array.dtype.str, offset))
            offset += array.nbytes
        return tuple(layouts)

    def encode_frame(self, frame: DecodedFrame) -> None:
        if self._cancelled:
            return
        packed = frame.data is not None
        arrays = (frame.data,) if frame.data is not None else frame.planes
        if not arrays:
            raise ValueError("HDR HEVC frame has no pixels")
        layouts = self._write_arrays(arrays)
        if self._exchange("video", (
            frame.frame_index, frame.pts, frame.time_seconds, frame.width, frame.height,
            frame.pixel_format, packed, layouts,
        )):
            self.frame_count += 1

    def add_audio_stream(self, **settings: Any) -> None:
        if not self._cancelled:
            self._exchange("audio_stream", settings)

    def write_audio_samples(self, samples: np.ndarray, sample_rate: int = 48000) -> None:
        if self._cancelled:
            return
        if self._buffer is None:
            raise RuntimeError("HDR HEVC encoder is closed")
        if samples.dtype != np.float32 or samples.ndim not in (1, 2):
            raise ValueError("HDR HEVC audio requires float32 samples in mono or planar layout")
        channels = samples.shape[0] if samples.ndim == 2 else 1
        if channels < 1 or channels * samples.itemsize > self._buffer.size:
            raise ValueError("Invalid HDR HEVC audio channel count")
        batch = min(65536, self._buffer.size // (channels * samples.itemsize))
        for start in range(0, samples.shape[-1], batch):
            (layout,) = self._write_arrays((samples[..., start:start + batch],))
            if not self._exchange("audio", (layout, sample_rate)):
                return

    def close(
        self, *, should_cancel: Callable[[], bool] | None = None,
        expected_frames: int | None = None,
    ) -> bool:
        cancel = should_cancel if should_cancel is not None else self._should_cancel
        try:
            if self._cancelled or (cancel is not None and cancel()):
                return False
            count = self.frame_count if expected_frames is None else expected_frames
            self._send("close", count)
            if not self._receive(cancel):
                return False
            process = self._process
            assert process is not None
            deadline = monotonic() + 120.0
            while process.exitcode is None:
                if cancel is not None and cancel():
                    return False
                if monotonic() >= deadline:
                    raise RuntimeError("HDR HEVC encoder did not exit after closing output")
                process.join(timeout=0.1)
            if process.exitcode != 0:
                raise RuntimeError(f"HDR HEVC encoder exited ({process.exitcode})")
            if self._staged_file.stat().st_size == 0:
                raise RuntimeError("The staged HEVC output was empty")
            if cancel is not None and cancel():
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
