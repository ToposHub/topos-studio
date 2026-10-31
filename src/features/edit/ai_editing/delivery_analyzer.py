"""Provider-neutral post-export frame and audio measurements (P8/P10)."""

from __future__ import annotations

import math
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import numpy as np

from src.features.edit.core.loudness_analyzer import analyze_loudness
from src.shared.ffmpeg_locator import find_ffmpeg


@dataclass(frozen=True)
class DeliveryAnalysisResult:
    """Measured output health; contains no source path or raw media."""

    succeeded: bool
    black_frame_count: int = 0
    frozen_frame_count: int = 0
    corrupt_frame_count: int = 0
    sampled_frame_count: int = 0
    audio_present: bool | None = None
    peak_db: float | None = None
    silence_seconds: float | None = None
    integrated_lufs: float | None = None
    loudness_range_lu: float | None = None
    loudness_backend_available: bool | None = None
    error_code: str = ""

    def to_dict(self) -> dict[str, Any]:
        peak_db = (
            round(self.peak_db, 3)
            if self.peak_db is not None and math.isfinite(self.peak_db)
            else None
        )
        return {
            "succeeded": self.succeeded,
            "black_frame_count": self.black_frame_count,
            "frozen_frame_count": self.frozen_frame_count,
            "corrupt_frame_count": self.corrupt_frame_count,
            "sampled_frame_count": self.sampled_frame_count,
            "audio_present": self.audio_present,
            "peak_db": peak_db,
            "silence_seconds": round(self.silence_seconds, 3) if self.silence_seconds is not None else None,
            "integrated_lufs": round(self.integrated_lufs, 3) if self.integrated_lufs is not None else None,
            "loudness_range_lu": round(self.loudness_range_lu, 3) if self.loudness_range_lu is not None else None,
            "loudness_backend_available": self.loudness_backend_available,
            "error_code": self.error_code,
        }


class FfmpegDeliveryAnalyzer:
    """Sample final media for black/frozen frames and audio health.

    两条测量路径，同一结果契约：

    - **PyAV（主路径，生产默认）**：``runner=None`` 时进程内解码采样，
      打包后无需系统 ffmpeg。视频按 ``sample_fps`` 依据 pts 抽帧并缩放到
      固定灰度尺寸（内存有界），音频解码为 mono float PCM。
    - **ffmpeg CLI（兜底/测试）**：注入 ``runner``（测试桩）或 PyAV 不可用时，
      走有界非 shell 的 ffmpeg 子进程；视频经 fps/scale/format 滤镜后从
      stdout 读取 rawvideo，音频读取 f32le。

    Audio is decoded to mono float PCM for peak and longest-silence
    measurements.
    """

    def __init__(
        self,
        *,
        executable: str = "ffmpeg",
        timeout_seconds: float = 60.0,
        sample_fps: float = 2.0,
        frame_size: tuple[int, int] = (64, 36),
        black_mean_threshold: float = 8.0,
        black_max_threshold: float = 32.0,
        frozen_diff_threshold: float = 0.5,
        frozen_run_samples: int = 5,
        silence_db: float = -60.0,
        audio_window_seconds: float = 0.1,
        runner: Callable[..., Any] | None = None,
    ) -> None:
        if timeout_seconds <= 0 or sample_fps <= 0:
            raise ValueError("timeout_seconds and sample_fps must be positive")
        if frame_size[0] <= 0 or frame_size[1] <= 0:
            raise ValueError("frame_size must be positive")
        if frozen_run_samples < 2 or audio_window_seconds <= 0:
            raise ValueError("frozen_run_samples and audio_window_seconds are invalid")
        self._executable = executable
        self._timeout = timeout_seconds
        self._sample_fps = sample_fps
        self._frame_width, self._frame_height = frame_size
        self._black_mean_threshold = black_mean_threshold
        self._black_max_threshold = black_max_threshold
        self._frozen_diff_threshold = frozen_diff_threshold
        self._frozen_run_samples = frozen_run_samples
        self._silence_amplitude = 10.0 ** (silence_db / 20.0)
        self._audio_window_seconds = audio_window_seconds
        self._runner = runner

    def analyze(
        self,
        output_path: str | Path,
        *,
        audio_present: bool | None = None,
        audio_sample_rate: int | None = None,
    ) -> DeliveryAnalysisResult:
        path = Path(output_path)
        if not path.is_file():
            return DeliveryAnalysisResult(False, corrupt_frame_count=1, error_code="OUTPUT_NOT_FOUND")

        video = self._analyze_video(path)
        if not video.succeeded:
            return video
        if audio_present is not True:
            return video
        if not audio_sample_rate or audio_sample_rate <= 0:
            return DeliveryAnalysisResult(
                False,
                black_frame_count=video.black_frame_count,
                frozen_frame_count=video.frozen_frame_count,
                corrupt_frame_count=video.corrupt_frame_count,
                sampled_frame_count=video.sampled_frame_count,
                audio_present=False,
                error_code="AUDIO_SAMPLE_RATE_UNAVAILABLE",
            )
        return self._merge_audio(video, path, audio_sample_rate)

    def _analyze_video(self, path: Path) -> DeliveryAnalysisResult:
        if self._runner is None:
            result = self._analyze_video_pyav(path)
            if result is not None:
                return result
            return DeliveryAnalysisResult(
                False, corrupt_frame_count=1, error_code="FRAME_ANALYZER_UNAVAILABLE",
            )
        return self._analyze_video_cli(path)

    def _analyze_video_pyav(self, path: Path) -> DeliveryAnalysisResult | None:
        """PyAV 进程内抽帧分析；PyAV 不可用时返回 None 交由 CLI 兜底。"""
        try:
            import av
        except ImportError:
            return None
        try:
            frames: list[np.ndarray] = []
            sample_interval = 1.0 / self._sample_fps
            next_sample_time = 0.0
            with av.open(str(path)) as container:
                if not container.streams.video:
                    return DeliveryAnalysisResult(
                        False, corrupt_frame_count=1, error_code="FRAME_DECODE_FAILED",
                    )
                for frame in container.decode(video=0):
                    if float(frame.time) + 1e-9 >= next_sample_time:
                        gray = frame.reformat(
                            width=self._frame_width,
                            height=self._frame_height,
                            format="gray",
                        )
                        frames.append(
                            gray.to_ndarray().reshape(
                                self._frame_height, self._frame_width,
                            )
                        )
                        next_sample_time += sample_interval
        except Exception:
            return DeliveryAnalysisResult(
                False, corrupt_frame_count=1, error_code="FRAME_DECODE_FAILED",
            )
        if not frames:
            return DeliveryAnalysisResult(
                False, corrupt_frame_count=1, error_code="FRAME_PAYLOAD_INVALID",
            )
        stacked = np.stack(frames)
        black_count = 0
        frozen_count = 0
        frozen_run = 0
        previous: np.ndarray | None = None
        for frame in stacked:
            if float(frame.mean()) <= self._black_mean_threshold and float(frame.max()) <= self._black_max_threshold:
                black_count += 1
            if previous is not None:
                diff = float(np.mean(np.abs(frame.astype(np.float32) - previous.astype(np.float32))))
                if diff <= self._frozen_diff_threshold:
                    frozen_run += 1
                    if frozen_run >= self._frozen_run_samples - 1:
                        frozen_count += 1
                else:
                    frozen_run = 0
            previous = frame
        return DeliveryAnalysisResult(
            True,
            black_frame_count=black_count,
            frozen_frame_count=frozen_count,
            sampled_frame_count=int(stacked.shape[0]),
        )

    def _analyze_video_cli(self, path: Path) -> DeliveryAnalysisResult:
        vf = (
            f"fps={self._sample_fps:g},"
            f"scale={self._frame_width}:{self._frame_height},"
            "format=gray"
        )
        executable = self._executable
        if executable == "ffmpeg":
            executable = find_ffmpeg() or executable
        try:
            completed = self._runner(
                [
                    executable,
                    "-hide_banner",
                    "-loglevel", "error",
                    "-xerror",
                    "-i", str(path),
                    "-vf", vf,
                    "-an",
                    "-f", "rawvideo",
                    "-pix_fmt", "gray",
                    "pipe:1",
                ],
                capture_output=True,
                text=False,
                timeout=self._timeout,
                check=False,
            )
        except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
            return DeliveryAnalysisResult(False, corrupt_frame_count=1, error_code="FRAME_ANALYZER_UNAVAILABLE")
        if int(getattr(completed, "returncode", 1)) != 0:
            return DeliveryAnalysisResult(False, corrupt_frame_count=1, error_code="FRAME_DECODE_FAILED")

        raw = getattr(completed, "stdout", b"") or b""
        frame_bytes = self._frame_width * self._frame_height
        if not raw or len(raw) % frame_bytes != 0:
            return DeliveryAnalysisResult(False, corrupt_frame_count=1, error_code="FRAME_PAYLOAD_INVALID")
        frames = np.frombuffer(raw, dtype=np.uint8).reshape(-1, self._frame_height, self._frame_width)
        black_count = 0
        frozen_count = 0
        frozen_run = 0
        previous: np.ndarray | None = None
        for frame in frames:
            if float(frame.mean()) <= self._black_mean_threshold and float(frame.max()) <= self._black_max_threshold:
                black_count += 1
            if previous is not None:
                diff = float(np.mean(np.abs(frame.astype(np.float32) - previous.astype(np.float32))))
                if diff <= self._frozen_diff_threshold:
                    frozen_run += 1
                    if frozen_run >= self._frozen_run_samples - 1:
                        frozen_count += 1
                else:
                    frozen_run = 0
            previous = frame
        return DeliveryAnalysisResult(
            True,
            black_frame_count=black_count,
            frozen_frame_count=frozen_count,
            sampled_frame_count=int(frames.shape[0]),
        )

    def _merge_audio(
        self,
        video: DeliveryAnalysisResult,
        path: Path,
        sample_rate: int,
    ) -> DeliveryAnalysisResult:
        raw: bytes | None = None
        if self._runner is None:
            raw = self._decode_audio_pyav(path, sample_rate)
            if raw is None:
                return self._audio_failed(video, "AUDIO_DECODE_FAILED")
        else:
            executable = self._executable
            if executable == "ffmpeg":
                executable = find_ffmpeg() or executable
            try:
                completed = self._runner(
                    [
                        executable,
                        "-hide_banner",
                        "-loglevel", "error",
                        "-xerror",
                        "-i", str(path),
                        "-vn", "-sn", "-dn",
                        "-ac", "1",
                        "-ar", str(sample_rate),
                        "-f", "f32le",
                        "pipe:1",
                    ],
                    capture_output=True,
                    text=False,
                    timeout=self._timeout,
                    check=False,
                )
            except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
                return self._audio_failed(video, "AUDIO_ANALYZER_UNAVAILABLE")
            if int(getattr(completed, "returncode", 1)) != 0:
                return self._audio_failed(video, "AUDIO_DECODE_FAILED")
            raw = getattr(completed, "stdout", b"") or b""
        if not raw or len(raw) % np.dtype(np.float32).itemsize != 0:
            return self._audio_failed(video, "AUDIO_PAYLOAD_INVALID")
        samples = np.frombuffer(raw, dtype=np.float32)
        peak = float(np.max(np.abs(samples))) if samples.size else 0.0
        peak_db = 20.0 * math.log10(peak) if peak > 1e-12 else -math.inf
        try:
            loudness = analyze_loudness(
                samples.reshape(1, -1),
                sample_rate=float(sample_rate),
            )
        except Exception:
            return self._audio_failed(video, "LOUDNESS_ANALYZER_FAILED")
        window = max(1, int(round(sample_rate * self._audio_window_seconds)))
        silence_windows = 0
        max_silence_windows = 0
        for start in range(0, samples.size, window):
            chunk = samples[start:start + window]
            if chunk.size == 0:
                continue
            rms = float(np.sqrt(np.mean(np.square(chunk, dtype=np.float64))))
            if rms <= self._silence_amplitude:
                silence_windows += 1
                max_silence_windows = max(max_silence_windows, silence_windows)
            else:
                silence_windows = 0
        return DeliveryAnalysisResult(
            True,
            black_frame_count=video.black_frame_count,
            frozen_frame_count=video.frozen_frame_count,
            corrupt_frame_count=video.corrupt_frame_count,
            sampled_frame_count=video.sampled_frame_count,
            audio_present=True,
            peak_db=peak_db,
            silence_seconds=max_silence_windows * self._audio_window_seconds,
            integrated_lufs=loudness.integrated_lufs,
            loudness_range_lu=loudness.loudness_range_lu,
            loudness_backend_available=loudness.backend_available,
        )

    def _decode_audio_pyav(self, path: Path, sample_rate: int) -> bytes | None:
        """PyAV 解码音频为 mono float32 PCM；无音频流或失败返回 None。"""
        try:
            import av

            chunks: list[bytes] = []
            with av.open(str(path)) as container:
                if not container.streams.audio:
                    return None
                resampler = av.AudioResampler(
                    format="flt", layout="mono", rate=sample_rate,
                )
                for frame in container.decode(audio=0):
                    for resampled in self._resample(resampler, frame):
                        array = resampled.to_ndarray()
                        chunks.append(array.astype(np.float32).tobytes())
            return b"".join(chunks)
        except Exception:
            return None

    @staticmethod
    def _resample(resampler: Any, frame: Any) -> list:
        """兼容不同 PyAV 版本的 resample 返回值（list / 单帧 / None）。"""
        result = resampler.resample(frame)
        if result is None:
            return []
        if isinstance(result, list):
            return result
        return [result]

    @staticmethod
    def _audio_failed(video: DeliveryAnalysisResult, error_code: str) -> DeliveryAnalysisResult:
        return DeliveryAnalysisResult(
            False,
            black_frame_count=video.black_frame_count,
            frozen_frame_count=video.frozen_frame_count,
            corrupt_frame_count=video.corrupt_frame_count,
            sampled_frame_count=video.sampled_frame_count,
            audio_present=False,
            error_code=error_code,
        )


__all__ = ["DeliveryAnalysisResult", "FfmpegDeliveryAnalyzer"]
