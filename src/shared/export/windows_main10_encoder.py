"""Windows video-only Main 10 delivery through a SIMD-enabled FFmpeg process."""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from fractions import Fraction
from functools import lru_cache
import logging
import os
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
from time import monotonic
from typing import BinaryIO, TYPE_CHECKING

import numpy as np

if TYPE_CHECKING:
    from src.shared.export.video_encoder import FFmpegVideoEncoder
    from src.shared.media.frame import DecodedFrame


logger = logging.getLogger(__name__)
_PROCESS_TIMEOUT = 120.0


@lru_cache(maxsize=8)
def _simd_capabilities(executable: str, mtime_ns: int, size: int, container: str) -> str:
    """Probe the rawvideo-to-Main 10 path and cache it for this binary revision."""
    try:
        result = subprocess.run([
            executable, "-nostdin", "-hide_banner", "-loglevel", "warning",
            "-f", "rawvideo", "-pixel_format", "yuv420p10le", "-video_size", "64x64",
            "-framerate", "25", "-i", "pipe:0",
            "-frames:v", "1", "-c:v", "libx265", "-profile:v", "main10",
            "-preset", "ultrafast", "-x265-params", "pools=1:frame-threads=1:log-level=info",
            "-f", container, "-movflags", "+frag_keyframe+empty_moov", "pipe:1",
        ], input=bytes(64 * 64 * 3), stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            creationflags=subprocess.CREATE_NO_WINDOW, timeout=5, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    if result.returncode or b"Main 10 profile" not in result.stderr:
        return ""
    for line in result.stderr.decode(errors="replace").splitlines():
        _, marker, capabilities = line.partition("using cpu capabilities:")
        if marker and set(capabilities.split()).intersection({"SSSE3", "AVX", "AVX2", "AVX512"}):
            return capabilities.strip()
    return ""


class WindowsMain10Encoder:
    """Stream constant-rate YUV to FFmpeg and publish only a verified movie."""

    def __init__(self, converter: FFmpegVideoEncoder, executable: str) -> None:
        self._converter = converter
        self._config = converter._config
        self._output = Path(converter._output_path)
        self._should_cancel = converter._should_cancel
        self._executable = executable
        self._stage: TemporaryDirectory[str] | None = None
        self._process: subprocess.Popen[bytes] | None = None
        self._stderr: BinaryIO | None = None
        self._writer: ThreadPoolExecutor | None = None
        self._cancelled = False
        self.frame_count = 0

    @classmethod
    def create(cls, converter: FFmpegVideoEncoder) -> WindowsMain10Encoder | None:
        from src.shared.export.video_encoder import ColorMetadataConfig
        from src.shared.ffmpeg_locator import find_ffmpeg

        config = converter._config
        container = converter._infer_container_format()
        if (container not in ("mov", "mp4")
                or config.pix_fmt != "yuv420p10le" or config.has_alpha
                or config.profile not in (None, "main10")
                or config.color_metadata != ColorMetadataConfig.sdr()
                or config.width < 64 or config.height < 64
                or config.width % 2 or config.height % 2
                or not converter._total_frames or converter._total_frames < 3):
            return None
        executable = find_ffmpeg()
        if not executable:
            return None
        try:
            stat = Path(executable).stat()
        except OSError:
            return None
        capabilities = _simd_capabilities(executable, stat.st_mtime_ns, stat.st_size, container)
        if not capabilities:
            return None
        encoder = cls(converter, executable)
        try:
            encoder.open()
        except OSError as error:
            logger.info("Could not start SIMD Main 10 encoder: %s", error)
            return None
        logger.info("Main 10 software encoding via %s (libx265 SIMD: %s)", executable, capabilities)
        return encoder

    @property
    def is_open(self) -> bool:
        return self._process is not None

    def open(self) -> None:
        if self._should_cancel is not None and self._should_cancel():
            self._cancelled = True
            return
        config = self._config
        args = [
            self._executable, "-nostdin", "-hide_banner", "-loglevel", "warning", "-nostats", "-xerror",
            "-f", "rawvideo", "-pixel_format", config.pix_fmt,
            "-video_size", f"{config.width}x{config.height}",
            "-framerate", f"{config.fps.num}/{config.fps.den}", "-i", "pipe:0",
            "-an", "-c:v", "libx265", "-pix_fmt", config.pix_fmt,
            "-profile:v", "main10", "-tag:v", "hvc1", "-threads:v", str(config.threads),
            "-color_primaries", "bt709", "-color_trc", "bt709",
            "-colorspace", "bt709", "-color_range", "tv",
        ]
        options = ({"crf": str(config.crf)} if config.crf is not None
                   else self._converter._build_rate_control_options("hevc"))
        x265_params = options.get("x265-params", "")
        options["x265-params"] = ":".join(filter(None, (
            x265_params, "colorprim=bt709:transfer=bt709:colormatrix=bt709:range=limited",
        )))
        if config.preset:
            options["preset"] = config.preset
        if config.gop_size:
            options["g"] = str(config.gop_size)
        for name, value in options.items():
            args.extend((f"-{name}", value))

        self._output.parent.mkdir(parents=True, exist_ok=True)
        self._stage = TemporaryDirectory(prefix=".topos-hdr-", dir=self._output.parent)
        stage = Path(self._stage.name)
        self._staged_file = stage / self._output.name
        try:
            self._stderr = (stage / "ffmpeg.log").open("w+b")
            self._process = subprocess.Popen(
                [*args, str(self._staged_file)], stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL, stderr=self._stderr,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            self._writer = ThreadPoolExecutor(max_workers=1, thread_name_prefix="Main10FFmpeg")
        except BaseException:
            self.abort()
            raise

    def _wait(
        self, operation: Future[None], should_cancel: Callable[[], bool] | None,
    ) -> bool:
        deadline = monotonic() + _PROCESS_TIMEOUT
        while should_cancel is None or not should_cancel():
            try:
                operation.result(timeout=0.05)
                return True
            except TimeoutError:
                if monotonic() >= deadline:
                    raise RuntimeError("Main 10 FFmpeg encoder stopped responding")
        self._cancelled = True
        return False

    def _write_frame(self, frame: DecodedFrame) -> None:
        process = self._process
        assert process is not None and process.stdin is not None
        if frame.pixel_format == "yuv420p10le" and frame.is_planar and len(frame.planes) == 3:
            planes = frame.planes
        else:
            av_frame = self._converter._create_av_frame(frame)
            planes = tuple(np.ndarray(
                (plane.height, plane.width), dtype="<u2", buffer=plane,
                strides=(plane.line_size, 2),
            ) for plane in av_frame.planes)
        width, height = self._config.width, self._config.height
        if tuple(plane.shape for plane in planes) != (
            (height, width), (height // 2, width // 2), (height // 2, width // 2),
        ) or any(plane.dtype != np.dtype("<u2") for plane in planes):
            raise ValueError("Invalid yuv420p10le input to the Main 10 encoder")
        for plane in planes:
            raw = memoryview(np.ascontiguousarray(plane)).cast("B")
            if process.stdin.write(raw) != raw.nbytes:
                raise OSError("Incomplete Main 10 pipe write")
        process.stdin.flush()

    def _process_error(self) -> RuntimeError:
        assert self._process is not None and self._stderr is not None
        self._stderr.seek(max(0, os.fstat(self._stderr.fileno()).st_size - 4096))
        detail = self._stderr.read().decode(errors="replace").strip()
        return RuntimeError(f"Main 10 FFmpeg exited ({self._process.poll()}): {detail}")

    def encode_frame(self, frame: DecodedFrame) -> None:
        if self._cancelled:
            return
        assert self._writer is not None
        operation = self._writer.submit(self._write_frame, frame)
        try:
            if self._wait(operation, self._should_cancel):
                self.frame_count += 1
            else:
                self.abort()
        except OSError as error:
            failure = self._process_error()
            self.abort()
            raise failure from error
        except BaseException:
            self.abort()
            raise

    def add_audio_stream(self, **settings: object) -> None:
        raise RuntimeError("The Main 10 FFmpeg encoder requires video-only delivery")

    def write_audio_samples(self, samples: np.ndarray, sample_rate: int = 48000) -> None:
        raise RuntimeError("The Main 10 FFmpeg encoder requires video-only delivery")

    def close(
        self, *, should_cancel: Callable[[], bool] | None = None,
        expected_frames: int | None = None,
    ) -> bool:
        import av

        cancel = should_cancel if should_cancel is not None else self._should_cancel
        try:
            if self._cancelled or (cancel is not None and cancel()):
                return False
            count = self.frame_count if expected_frames is None else expected_frames
            if count <= 0 or self.frame_count != count:
                raise RuntimeError(f"Main 10 submitted {self.frame_count} frames, expected {count}")
            process = self._process
            assert process is not None and process.stdin is not None
            process.stdin.close()
            deadline = monotonic() + _PROCESS_TIMEOUT
            while process.poll() is None:
                if cancel is not None and cancel():
                    return False
                if monotonic() >= deadline:
                    raise RuntimeError("Main 10 FFmpeg did not finish writing the movie")
                try:
                    process.wait(timeout=0.05)
                except subprocess.TimeoutExpired:
                    continue
            if process.returncode:
                raise self._process_error()
            with av.open(str(self._staged_file)) as movie:
                if len(movie.streams) != 1 or len(movie.streams.video) != 1:
                    raise RuntimeError("Main 10 movie does not contain exactly one video stream")
                stream = movie.streams.video[0]
                codec = stream.codec_context
                config = self._config
                if (stream.frames != count or codec.name != "hevc"
                        or codec.profile != "Main 10" or codec.pix_fmt != "yuv420p10le"
                        or (codec.width, codec.height) != (config.width, config.height)
                        or stream.average_rate != Fraction(config.fps.num, config.fps.den)
                        or (codec.color_primaries, codec.color_trc, codec.colorspace, codec.color_range)
                        != (1, 1, 1, 1)):
                    raise RuntimeError(f"Main 10 movie does not match the requested {count} frames or stream format")
            if cancel is not None and cancel():
                return False
            os.replace(self._staged_file, self._output)
            return True
        finally:
            self.abort()

    def abort(self) -> None:
        process = self._process
        if process is not None and process.poll() is None:
            process.kill()
            process.wait()
        if self._writer is not None:
            self._writer.shutdown(cancel_futures=True)
            self._writer = None
        if process is not None and process.stdin is not None:
            try:
                process.stdin.close()
            except BrokenPipeError:
                pass
        self._process = None
        if self._stderr is not None:
            self._stderr.close()
            self._stderr = None
        if self._stage is not None:
            self._stage.cleanup()
            self._stage = None
