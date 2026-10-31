"""音频提取器——从媒体素材提取 16kHz mono PCM（供 ASR / 说话人分离 provider）。

ASR（Qwen3-ASR）和说话人分离（NVIDIA Sortformer）都需要 16kHz 单声道音频输入。
本模块把视频/音频文件转为标准格式，避免每个 provider 各自处理解码。

设计（遵循 .clinerules）：

- **双路径**：优先 PyAV（进程内解码，打包后开箱即用，无需系统 ffmpeg）；
  PyAV 不可用时回退 subprocess ffmpeg（二进制经
  :mod:`src.shared.ffmpeg_locator` 定位：打包 ``bin/`` 目录 → PATH）。
- 优先输出到临时 wav 文件（Qwen3-ASR 和 Sortformer 都接受路径输入，最简链路）。
- 两条路径都不可用 / 无音频流 / 解码错误时抛 :class:`AudioExtractionError`
  （**不静默降级**，§11.1 红线沿用）。
- 无音频流的素材（纯视频/图片）抛 ``AudioExtractionError``——ASR 无意义，调用方据此
  在 provenance 标注 ``no_audio`` 而非假装转录成功。
- 纯 Python，无 Qt 依赖；可在任意线程（含 worker 线程）调用。
"""

from __future__ import annotations

import logging
import os
import subprocess
import tempfile
import time
import wave
from typing import Any

from src.shared.ffmpeg_locator import find_ffmpeg

logger = logging.getLogger(__name__)


# ASR / 分离模型约定的采样率（Qwen3-ASR / Sortformer / WhisperX 均为 16kHz）
ASR_SAMPLE_RATE = 16000


class AudioExtractionError(RuntimeError):
    """音频提取失败（无解码后端 / 无音频流 / 解码错误 / 超时）。

    provider 据此把 job 标 ``AUDIO_EXTRACTION_FAILED``，UI 提示用户。
    """


class AudioExtractor:
    """从媒体素材提取 16kHz mono wav（供 ASR / 分离 provider）。

    线程安全（无实例状态，每次调用独立临时文件）。可在 worker 线程调用。

    Args:
        ffmpeg_bin: CLI 回退路径使用的 ffmpeg 可执行文件；默认 ``"ffmpeg"``
            时先经 :mod:`src.shared.ffmpeg_locator` 解析（打包目录 → PATH），
            传入显式路径则原样使用。
    """

    def __init__(self, *, ffmpeg_bin: str = "ffmpeg") -> None:
        self._ffmpeg = ffmpeg_bin

    # ---------------- 公开 API ---------------- #

    def extract_wav(
        self,
        source_path: str,
        *,
        output_path: str | None = None,
        sample_rate: int = ASR_SAMPLE_RATE,
        timeout: float = 300.0,
    ) -> str:
        """提取 16kHz mono PCM wav 文件。

        Args:
            source_path: 源媒体文件路径（视频/音频均可）。
            output_path: 输出 wav 路径；None 时生成临时文件（调用方负责删除）。
            sample_rate: 目标采样率（默认 16000，ASR 标准）。
            timeout: 超时秒数（长视频需更久；对 PyAV 与 CLI 路径同样生效）。

        Returns:
            wav 文件路径。

        Raises:
            AudioExtractionError: 解码后端不可用 / 无音频流 / 解码失败 / 超时。
        """
        if not source_path or not os.path.isfile(source_path):
            raise AudioExtractionError(f"source not found: {source_path!r}")

        cleanup = False
        if output_path is None:
            fd, output_path = tempfile.mkstemp(suffix=".wav", prefix="topos_asr_")
            os.close(fd)
            cleanup = True

        try:
            pcm, rate = self._decode_pcm_s16(source_path, sample_rate, timeout)
        except AudioExtractionError:
            if cleanup:
                self._remove_quietly(output_path)
            raise

        try:
            with wave.open(output_path, "wb") as handle:
                handle.setnchannels(1)
                handle.setsampwidth(2)
                handle.setframerate(rate)
                handle.writeframes(pcm)
        except (OSError, wave.Error) as exc:
            if cleanup:
                self._remove_quietly(output_path)
            raise AudioExtractionError(
                f"failed writing wav for {source_path!r}: {exc}"
            ) from exc

        if not os.path.isfile(output_path) or os.path.getsize(output_path) == 0:
            if cleanup:
                self._remove_quietly(output_path)
            raise AudioExtractionError(
                f"empty/missing wav output for {source_path!r}"
            )

        logger.debug(
            "extracted %dHz mono wav from %s → %s (%d bytes)",
            rate, os.path.basename(source_path), output_path,
            os.path.getsize(output_path),
        )
        return output_path

    def extract_samples(
        self,
        source_path: str,
        *,
        sample_rate: int = ASR_SAMPLE_RATE,
        timeout: float = 300.0,
    ) -> tuple["Any", int]:
        """提取为 ``(ndarray[int16], sample_rate)``，不落盘（内存）。

        适用于支持 ndarray 输入的 provider（如 Qwen3-ASR 接受 ``(np.ndarray, sr)``）。
        落盘版本 :meth:`extract_wav` 适用于只接受文件路径的 provider（如 Sortformer）。

        Returns:
            ``(samples_int16_ndarray, sample_rate)``。

        Raises:
            AudioExtractionError: 同 :meth:`extract_wav`。
        """
        import numpy as np

        if not source_path or not os.path.isfile(source_path):
            raise AudioExtractionError(f"source not found: {source_path!r}")

        pcm, rate = self._decode_pcm_s16(source_path, sample_rate, timeout)
        if not pcm:
            raise AudioExtractionError(f"empty audio for {source_path!r}")
        return np.frombuffer(pcm, dtype=np.int16), rate

    # ---------------- 解码调度：PyAV 优先，CLI 兜底 ---------------- #

    def _decode_pcm_s16(
        self,
        source_path: str,
        sample_rate: int,
        timeout: float,
    ) -> tuple[bytes, int]:
        """解码为 ``(mono s16le PCM bytes, 实际采样率)``。"""
        try:
            return self._decode_pcm_s16_pyav(source_path, sample_rate, timeout)
        except AudioExtractionError:
            raise  # no_audio / 超时等已定语义，不再翻译
        except ImportError:
            logger.debug("PyAV unavailable, falling back to ffmpeg CLI")
        except Exception as exc:
            # PyAV 存在但解码失败：文件确实有问题时 CLI 大概率同样失败，
            # 直接透传语义（no_audio / decode failed），避免重复解码一遍。
            raise self._translate_pyav_error(exc, source_path) from exc
        return self._decode_pcm_s16_cli(source_path, sample_rate, timeout)

    def _decode_pcm_s16_pyav(
        self,
        source_path: str,
        sample_rate: int,
        timeout: float,
    ) -> tuple[bytes, int]:
        """PyAV 进程内解码（无需系统 ffmpeg）。"""
        import av

        container = av.open(source_path)
        pcm = bytearray()
        rate = sample_rate
        deadline = time.monotonic() + max(0.0, timeout)
        try:
            if not container.streams.audio:
                raise AudioExtractionError(
                    f"source has no audio stream: {source_path!r}"
                )
            stream = container.streams.audio[0]
            resampler = av.AudioResampler(
                format="s16", layout="mono", rate=sample_rate,
            )
            for packet in container.demux(stream):
                if time.monotonic() > deadline:
                    raise AudioExtractionError(
                        f"timed out after {timeout}s extracting audio "
                        f"from {source_path!r}"
                    )
                for frame in packet.decode():
                    for out_frame in self._resample(resampler, frame):
                        chunk = out_frame.to_ndarray().tobytes()
                        if chunk:
                            pcm.extend(chunk)
                            rate = int(out_frame.sample_rate)
        finally:
            container.close()

        if not pcm:
            raise AudioExtractionError(
                f"source has no decodable audio: {source_path!r}"
            )
        return bytes(pcm), rate

    @staticmethod
    def _resample(resampler: Any, frame: Any) -> list[Any]:
        """兼容不同 PyAV 版本的 resample 返回值（list / 单帧 / None）。"""
        result = resampler.resample(frame)
        if result is None:
            return []
        if isinstance(result, list):
            return result
        return [result]

    @staticmethod
    def _translate_pyav_error(
        exc: Exception, source_path: str,
    ) -> AudioExtractionError:
        """把 PyAV 异常翻译成与 CLI 路径一致的错误语义。"""
        message = str(exc).lower()
        if "no audio" in message or isinstance(exc, AudioExtractionError):
            return AudioExtractionError(
                f"source has no audio stream: {source_path!r}"
            )
        return AudioExtractionError(
            f"audio decode failed for {source_path!r}: {exc}"
        )

    def _decode_pcm_s16_cli(
        self,
        source_path: str,
        sample_rate: int,
        timeout: float,
    ) -> tuple[bytes, int]:
        """subprocess ffmpeg 回退（二进制经 ffmpeg_locator 定位）。"""
        ffmpeg = self._resolve_ffmpeg()
        cmd = [
            ffmpeg, "-i", source_path,
            "-vn", "-ac", "1", "-ar", str(sample_rate),
            "-c:a", "pcm_s16le", "-f", "s16le",  # raw PCM 16-bit little-endian
            "-loglevel", "error",
            "pipe:1",
        ]
        try:
            result = subprocess.run(
                cmd, capture_output=True, timeout=timeout, check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise AudioExtractionError(
                f"ffmpeg timed out after {timeout}s extracting audio from {source_path!r}"
            ) from exc
        except FileNotFoundError as exc:
            raise AudioExtractionError(
                f"ffmpeg binary vanished at {ffmpeg!r}"
            ) from exc

        if result.returncode != 0:
            stderr = result.stderr.decode("utf-8", errors="replace").strip()
            if "does not contain any stream" in stderr or "No audio streams" in stderr:
                raise AudioExtractionError(f"source has no audio stream: {source_path!r}")
            raise AudioExtractionError(
                f"ffmpeg failed (code {result.returncode}): {stderr[:500]}"
            )

        if not result.stdout:
            raise AudioExtractionError(f"ffmpeg produced empty audio for {source_path!r}")

        return result.stdout, sample_rate

    # ---------------- 内部 ---------------- #

    def _resolve_ffmpeg(self) -> str:
        """解析 CLI 回退用的 ffmpeg 二进制，不可用则抛错。"""
        if os.path.isfile(self._ffmpeg) or os.path.dirname(self._ffmpeg):
            return self._ffmpeg
        located = find_ffmpeg()
        if located is not None:
            return located
        raise AudioExtractionError(
            "ffmpeg binary not found (bundled bin/ dir, TOPOS_FFMPEG_HOME or "
            "PATH); install ffmpeg to enable ASR / diarization"
        )

    @staticmethod
    def _remove_quietly(path: str) -> None:
        try:
            os.remove(path)
        except OSError:
            pass
