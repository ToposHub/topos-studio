"""ASR 转写步骤（视频专用）。

从视频提取音频，转写成台词写入 transcript_line 表。

设计决策：
    主项目有成熟的 transcript_provider（Qwen3-ASR + Sortformer，已接真实推理）。
    但直接 import 会触发 src/ 完整 import 链。

    本 step 用 **可选 import** 策略：
        - 能 import 主项目（集成模式）→ 用真实 ASR（QwenASRProvider）
        - 不能 import（独立模式）→ 用 vault 自己的 ASR provider（stub 兜底）

    环境变量切换后端：
        TOPOS_VAULT_ASR_BACKEND=main_project  强制用主项目 ASR（集成模式）
        TOPOS_VAULT_ASR_BACKEND=vault         强制用 vault ASR provider
        （未设置）→ 自动：能 import 主项目就用主项目，否则 vault

    主项目 ASR 的真实/桩切换：TOPOS_ASR_REAL=0 用桩（确定性输出，测试用）。

降级安全：任一环节失败 → 跳过（视频不会有台词，但其他步骤仍跑）。
"""

from __future__ import annotations

import logging
import os
import subprocess

from apps.topos_vault.db.repository import MediaSource, TranscriptLine, VaultRepository
from apps.topos_vault.pipeline.steps import PipelineStep, StepSkipped

logger = logging.getLogger(__name__)


def _find_ffmpeg() -> str | None:
    """定位 ffmpeg（自包含版：打包目录 → PATH，不依赖 src/）。

    vault 保持独立可运行，不 import src.shared.ffmpeg_locator；
    此处保持与其一致的候选顺序（TOPOS_FFMPEG_HOME / 冻结目录 bin/ / PATH）。
    """
    import shutil
    import sys
    from pathlib import Path

    exe = "ffmpeg.exe" if sys.platform == "win32" else "ffmpeg"
    candidates: list[Path] = []
    env_home = os.environ.get("TOPOS_FFMPEG_HOME", "").strip()
    if env_home:
        candidates.append(Path(env_home))
    if getattr(sys, "frozen", False):
        candidates.append(Path(sys.executable).resolve().parent)
        meipass = getattr(sys, "_MEIPASS", "")
        if meipass:
            candidates.append(Path(meipass))
    candidates.append(Path(__file__).resolve().parents[3])
    for directory in candidates:
        for sub in (directory, directory / "bin"):
            path = sub / exe
            if path.is_file():
                return str(path)
    return shutil.which(exe)


# 集成模式下的 ASR fps（主项目用帧坐标，vault 转秒；24fps 是影视常用，精度足够）
_MAIN_PROJECT_ASR_FPS = "24000/1001"


class AsrStep(PipelineStep):
    """ASR 转写步骤。

    流程：
        1. 用 ffmpeg 从视频提取音频
        2. 调 ASR provider（主项目 or vault stub）转写
        3. 台词写入 transcript_line 表（关联到 clip）

    前置依赖：scene_cut（需要 clip 记录才能关联台词）。
    如果没 clip，台词关联到 source 的第一个 clip（或创建一个整片 clip）。
    """

    step_name = "asr"
    applies_to = ("video",)

    def __init__(self, repo: VaultRepository):
        super().__init__(repo)

    def _run(self, source: MediaSource) -> str:
        path = source.path_current or source.path_original
        if not os.path.exists(path):
            raise StepSkipped(self.step_name, source.fingerprint, "文件不存在")

        backend = os.environ.get("TOPOS_VAULT_ASR_BACKEND", "auto").strip().lower()

        # 主项目 ASR 自己抽音，集成模式下跳过 vault 的抽音（省一次 ffmpeg）
        audio_path: str | None = None
        if backend in ("auto", "vault"):
            # auto 模式：先试主项目（它自己抽音），失败再降级到 vault（需 vault 抽的音）
            lines = []
            if backend == "auto":
                lines = self._try_main_project_asr(source)

            # 主项目没出结果（或强制 vault）→ 抽音 + 用 vault provider
            if not lines:
                audio_path = self._extract_audio(path, source.fingerprint)
                if audio_path is None:
                    if backend == "vault":
                        raise StepSkipped(self.step_name, source.fingerprint,
                                          "音频提取失败（ffmpeg 未装或无音轨）")
                    # auto：主项目也失败、抽音也失败 → 无台词
                    raise StepSkipped(self.step_name, source.fingerprint,
                                      "无台词（ASR 不可用 + 无音轨）")
                lines = self._transcribe_vault(audio_path)
        else:
            # main_project 强制模式
            lines = self._try_main_project_asr(source)

        if not lines:
            raise StepSkipped(self.step_name, source.fingerprint, "无台词（静音或 ASR 不可用）")

        try:
            # 关联到 clip 并写入
            self._write_transcript_lines(source, lines)
            return f"{len(lines)} 句台词"
        finally:
            # 清理临时音频文件
            if audio_path and os.path.exists(audio_path):
                try:
                    os.remove(audio_path)
                except OSError:
                    pass

    def is_done(self, source: MediaSource) -> bool:
        """该视频是否已有台词。"""
        clips = self.repo.list_clips(source.fingerprint)
        for clip in clips:
            if self.repo.list_transcript_lines(clip.clip_id):
                return True
        return False

    def _extract_audio(self, video_path: str, fingerprint: str) -> str | None:
        """提取 16kHz 单声道 wav（PyAV 主路径，ffmpeg CLI 兜底）。"""
        # PyAV 主路径：进程内解码，打包后无需系统 ffmpeg
        wav = self._extract_audio_with_av(video_path, fingerprint)
        if wav is not None:
            return wav

        ffmpeg = _find_ffmpeg()
        if ffmpeg is None:
            return None

        from apps.topos_vault.paths import get_cache_dir
        audio_path = str(get_cache_dir(fingerprint) / "audio.wav")

        try:
            result = subprocess.run(
                [
                    ffmpeg, "-y", "-i", video_path,
                    "-vn",                    # 不要视频
                    "-acodec", "pcm_s16le",   # 16-bit PCM
                    "-ar", "16000",           # 16kHz（ASR 标准）
                    "-ac", "1",               # 单声道
                    audio_path,
                ],
                capture_output=True, timeout=300,
            )
            if result.returncode != 0:
                return None
            return audio_path
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            return None

    def _extract_audio_with_av(self, video_path: str, fingerprint: str) -> str | None:
        """用 PyAV 解码并重采样为 16kHz mono wav（无需系统 ffmpeg）。"""
        try:
            import av
            # numpy 探测：PyAV frame.to_ndarray() 运行时依赖 numpy，缺失时走回退
            import numpy as np  # noqa: F401
            import wave
        except ImportError:
            return None

        from apps.topos_vault.paths import get_cache_dir
        audio_path = str(get_cache_dir(fingerprint) / "audio.wav")

        try:
            container = av.open(video_path)
            try:
                if not container.streams.audio:
                    return None

                # 重采样到 ASR 标准格式（16kHz mono s16），与 CLI `-ar 16000 -ac 1` 对齐
                resampler = av.AudioResampler(
                    format="s16", layout="mono", rate=16000,
                )
                chunks: list[bytes] = []
                for frame in container.decode(audio=0):
                    for resampled in self._resample(resampler, frame):
                        chunks.append(resampled.to_ndarray().tobytes())
            finally:
                container.close()

            pcm = b"".join(chunks)
            if not pcm:
                return None

            # 写 wav
            with wave.open(audio_path, "wb") as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(16000)
                wf.writeframes(pcm)

            return audio_path
        except Exception as e:
            logger.debug("PyAV 音频提取失败：%s", e)
            return None

    @staticmethod
    def _resample(resampler, frame) -> list:
        """兼容不同 PyAV 版本的 resample 返回值（list / 单帧 / None）。"""
        result = resampler.resample(frame)
        if result is None:
            return []
        if isinstance(result, list):
            return result
        return [result]

    def _transcribe_vault(self, audio_path: str) -> list[TranscriptLine]:
        """用 vault 自己的 ASR provider 转写（stub 兜底 / WhisperX 等备选）。

        Args:
            audio_path: vault 已抽好的 wav 路径（16kHz 单声道）。
        """
        from apps.topos_vault.providers import get_provider, Capability
        try:
            provider = get_provider(Capability.ASR)
            if provider.is_available():
                result = provider.transcribe(audio_path)
                return [
                    TranscriptLine(
                        line_id=None, clip_id="",  # 后面 _write 会填
                        text=line.text,
                        start_sec=line.start_sec,
                        end_sec=line.end_sec,
                        speaker=line.speaker,
                    )
                    for line in result.lines
                ]
        except Exception as e:
            logger.debug("vault ASR provider 失败：%s", e)

        return []

    def _try_main_project_asr(self, source: MediaSource) -> list[TranscriptLine]:
        """尝试用主项目的 transcript_provider（集成模式）。

        本方法延迟 import src/——失败说明 vault 在独立模式运行（没有 src/ 可用）。

        集成路径：
            1. ModelPackManager()（零参，BUILTIN_SPECS 已含 qwen3_asr）
            2. QwenASRProvider(model_pack_manager=None, asset_path_provider=...)
               —— 传 None 跳过 pack 校验（engine 仍能从本地/HF 加载，最短路径）
            3. provider.analyze(asset_id, fingerprint, fps=...)
               —— 返回 (AssetAnalysis, segments)；analysis.transcript 是帧坐标
            4. 帧 → 秒转换（fps_value = 24/1.001）

        主项目 ASR 的 asset_path_provider 必须返回**源视频路径**（它会自己抽音），
        所以本方法不用 vault 已抽好的 audio_path。
        """
        try:
            from src.features.edit.ai_editing.transcript_provider import QwenASRProvider
            from src.features.edit.ai_editing.analysis_models import AssetFingerprint
            from src.shared.media.metadata import Rational
        except ImportError:
            return []
        except Exception as e:
            logger.debug("主项目 ASR 模块不可用：%s", e)
            return []

        # 源视频路径（主项目 ASR 自己抽音）
        source_path = source.path_current or source.path_original
        if not source_path or not os.path.exists(source_path):
            return []

        # 指纹（缓存失效用，ASR 不强校验，给个最小值即可）
        try:
            st = os.stat(source_path)
            fingerprint = AssetFingerprint(size=st.st_size, mtime_ns=st.st_mtime_ns)
        except OSError:
            fingerprint = AssetFingerprint()

        # 构造 provider：model_pack_manager=None 跳过 pack 校验（engine 仍可加载）
        # asset_path_provider 必须能按 asset_id 返回源路径
        try:
            provider = QwenASRProvider(
                model_pack_manager=None,
                asset_path_provider=lambda aid: source_path,
            )
        except Exception as e:
            logger.debug("主项目 QwenASRProvider 构造失败：%s", e)
            return []

        # 解析 fps（主项目用 Rational 字符串，如 "24000/1001"）
        try:
            fps_value = Rational.from_string(_MAIN_PROJECT_ASR_FPS).to_fps()
        except Exception:
            fps_value = 24.0  # 兜底
        if fps_value <= 0:
            fps_value = 24.0

        # 调 analyze（内部抽音 + ASR + 可选 diarization）
        try:
            analysis, _segments = provider.analyze(
                asset_id=source.fingerprint,
                fingerprint=fingerprint,
                fps=_MAIN_PROJECT_ASR_FPS,
            )
        except Exception as e:
            logger.debug("主项目 ASR analyze 失败：%s", e)
            return []

        # analysis.transcript 是帧坐标 → 转秒
        transcript = getattr(analysis, "transcript", ()) or ()
        lines: list[TranscriptLine] = []
        for seg in transcript:
            text = getattr(seg, "text", "") or ""
            if not text.strip():
                continue
            start_sec = getattr(seg, "start_frame", 0) / fps_value
            end_sec = getattr(seg, "end_frame", start_sec) / fps_value
            lines.append(TranscriptLine(
                line_id=None, clip_id="",  # _write_transcript_lines 会填
                text=text,
                start_sec=start_sec,
                end_sec=end_sec,
                speaker=getattr(seg, "speaker", None),
            ))

        return lines

    def _write_transcript_lines(self, source: MediaSource, lines: list[TranscriptLine]) -> None:
        """把台词写入 DB，关联到 clip。

        台词按时间戳分配到对应时间段的 clip。如果没 clip，创建一个整片 clip。
        """
        clips = self.repo.list_clips(source.fingerprint)
        if not clips:
            # 无 clip → 创建整片 clip
            duration = source.duration_sec or 0
            from apps.topos_vault.db.repository import Clip
            self.repo.insert_clip(Clip(
                clip_id="", fingerprint=source.fingerprint,
                clip_index=0, start_sec=0, end_sec=duration,
                duration_sec=duration,
            ))
            clips = self.repo.list_clips(source.fingerprint)

        # 按时间分配台词到 clip
        for line in lines:
            # 找包含这句台词时间范围的 clip
            assigned = False
            mid = (line.start_sec or 0 + (line.end_sec or 0)) / 2
            for clip in clips:
                if clip.start_sec <= mid <= clip.end_sec:
                    line.clip_id = clip.clip_id
                    assigned = True
                    break
            if not assigned and clips:
                line.clip_id = clips[0].clip_id  # 兜底分到第一个

        # 批量写入
        valid_lines = [l for l in lines if l.clip_id]
        if valid_lines:
            self.repo.insert_transcript_lines(valid_lines)
