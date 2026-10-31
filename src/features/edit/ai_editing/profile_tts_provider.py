"""Profile-routed TTS adapter for subtitle-to-speech."""

from __future__ import annotations

import subprocess
import tempfile
import wave
import uuid
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from typing import Any, Sequence

from src.features.edit.ai_editing.generation_models import (
    GenerationBudget,
    GenerationConsent,
    GenerationModality,
    GenerationRecipe,
    GenerationRequest,
)
from src.features.edit.ai_editing.provider_profiles import AiProviderProfile
from src.features.edit.ai_editing.tts_provider import (
    DEFAULT_TTS_SETTINGS,
    Qwen3TTSProvider,
    SpeechCue,
    SynthesizedSpeech,
)


class ProfileAwareTtsProvider:
    """Choose local Qwen3-TTS or a configured remote voice adapter.

    The subtitle action remains compatible with the existing Qwen
    ``synthesize(cues, fps, settings)`` contract.  Remote providers are used
    one cue at a time and decoded into the same timed WAV output, so the rest
    of the timeline pipeline stays provider-neutral.
    """

    provider_id = "tts_profile"

    #: AUD-14117：远程 provider.generate 同步等待的上限（秒）。远程挂起时调用
    #: 线程最多再等这么久即报错返回，不再无限期阻塞整段合成。ffmpeg 解码
    #: （``_decode_audio``）自身已有 ``timeout=60``，故整条 cue 的阻塞有界。
    REMOTE_CUE_TIMEOUT_SECONDS = 120.0

    def __init__(self, local_provider: Qwen3TTSProvider, registry: Any, credential_store: Any) -> None:
        self._local = local_provider
        self._registry = registry
        self._credential_store = credential_store
        self._remote_by_profile: dict[str, Any] = {}

    @property
    def active_profile(self) -> AiProviderProfile | None:
        getter = getattr(self._registry, "active_profile_for_capability", None)
        if callable(getter):
            profile = getter("tts")
            if profile is not None:
                return profile
        for profile in getattr(self._registry, "list", lambda: [])():
            capabilities = set(getattr(profile, "capabilities", ()) or ())
            if "tts" in capabilities or "voice" in getattr(profile, "modalities", ()):
                return profile
        return None

    def _remote(self, profile: AiProviderProfile) -> Any:
        provider = self._remote_by_profile.get(profile.profile_id)
        if provider is None:
            from src.features.edit.ai_editing.volcengine_generation_provider import (
                make_volcengine_generation_provider,
            )
            provider = make_volcengine_generation_provider(profile, self._credential_store)
            if provider is None:
                raise RuntimeError(
                    f"Profile {profile.display_name!r} 的 TTS 协议暂未接入；"
                    "可先选择本地 Qwen3-TTS 或火山方舟 TTS 预设"
                )
            self._remote_by_profile[profile.profile_id] = provider
        return provider

    @staticmethod
    def _is_remote(profile: AiProviderProfile | None) -> bool:
        if profile is None:
            return False
        return profile.source_kind not in {"local", "ollama"} and profile.provider_kind != "local_qwen_tts"

    def is_model_ready(self, settings: Any = DEFAULT_TTS_SETTINGS) -> bool:
        profile = self.active_profile
        if not self._is_remote(profile):
            return self._local.is_model_ready(settings)
        return bool(
            profile is not None
            and profile.endpoint
            and self._credential_store.get_secret(profile.credential_ref)
        )

    def synthesize(
        self,
        cues: Sequence[SpeechCue],
        *,
        fps: float,
        settings: Any | None = None,
        output_path: str | None = None,
    ) -> SynthesizedSpeech:
        settings = settings or DEFAULT_TTS_SETTINGS
        profile = self.active_profile
        if not self._is_remote(profile):
            return self._local.synthesize(
                cues, fps=fps, settings=settings, output_path=output_path,
            )
        assert profile is not None
        provider = self._remote(profile)
        clean_cues = tuple(cue for cue in cues if cue.text and cue.text.strip())
        if not clean_cues:
            raise ValueError("没有可合成的字幕文本")
        waves: list[tuple[int, Any, int]] = []
        for index, cue in enumerate(clean_cues):
            request = GenerationRequest(
                request_id=f"subtitle-tts-{uuid.uuid4().hex[:12]}",
                modality=GenerationModality.VOICE,
                prompt=cue.text.strip(),
                consent=GenerationConsent(
                    confirmed=True,
                    provider_id=profile.profile_id,
                    cost_acknowledged=True,
                    upload_acknowledged=True,
                    copyright_acknowledged=True,
                    target_location="subtitle_to_speech",
                ),
                budget=GenerationBudget(max_candidates=1),
                requested_by="subtitle_to_speech",
                recipe=GenerationRecipe(
                    recipe_id=f"subtitle-tts-{index + 1}",
                    modality=GenerationModality.VOICE,
                    output_format="mp3",
                    output_constraints={
                        "speaker": self._remote_speaker(settings),
                        "sample_rate": 24000,
                    },
                ),
            )
            # AUD-14117：provider.generate 是同步 HTTP 往返，任何一次远程挂起
            # 都会让整段（可能上百 cue 的）合成无限期阻塞调用线程。这里把
            # "等待结果"限制在 REMOTE_CUE_TIMEOUT_SECONDS 内：超时立即报错，
            # 让调用方可以重试/降级/提示用户（后台 worker 线程仍在运行无法
            # 强杀，但调用线程不再被无限占用）。
            result = self._generate_bounded(provider, request)
            if result.error_code or not result.candidates:
                raise RuntimeError(result.error_message or "远程 TTS 未返回音频")
            handle = result.candidates[0].preview_handle
            waveform, sample_rate = self._decode_audio(handle)
            waves.append((int(cue.start_frame), waveform, sample_rate))

        return self._write_remote_wav(
            waves,
            first_frame=min(int(cue.start_frame) for cue in clean_cues),
            output_path=output_path,
            model=f"{profile.display_name} / {profile.model_id}",
        )

    def _generate_bounded(self, provider: Any, request: Any) -> Any:
        """带超时上限地等待一次 ``provider.generate``（AUD-14117）。

        在单工作线程里执行同步 generate，调用线程最多等待
        ``REMOTE_CUE_TIMEOUT_SECONDS``；超时抛 ``RuntimeError``（带 cue 请求
        标识），不再无限期阻塞。超时后后台线程无法强杀（Python 线程语义），
        但 ``shutdown(wait=False)`` 保证调用线程立即返回、不二次等待。
        """
        pool = ThreadPoolExecutor(max_workers=1)
        try:
            future = pool.submit(provider.generate, request)
            try:
                return future.result(timeout=self.REMOTE_CUE_TIMEOUT_SECONDS)
            except FutureTimeoutError as exc:
                raise RuntimeError(
                    f"远程 TTS 合成超过 {self.REMOTE_CUE_TIMEOUT_SECONDS:.0f}s "
                    f"未返回（request_id={request.request_id}），已中止等待；"
                    "请检查网络或改用本地 Qwen3-TTS"
                ) from exc
        finally:
            pool.shutdown(wait=False)

    @staticmethod
    def _remote_speaker(settings: Any) -> str:
        speaker = str(getattr(settings, "speaker", "") or "")
        if speaker.startswith("zh_"):
            return speaker
        return "zh_female_vv_uranus_bigtts"

    @staticmethod
    def _decode_audio(path: str) -> tuple[Any, int]:
        if not path:
            raise RuntimeError("远程 TTS 返回了空音频句柄")
        from src.shared.ffmpeg_locator import find_ffmpeg

        ffmpeg = find_ffmpeg()
        if ffmpeg is None:
            raise RuntimeError(
                "无法解码远程 TTS 音频：未找到 ffmpeg（打包 bin/ 目录或 PATH）"
            )
        try:
            result = subprocess.run(
                [ffmpeg, "-v", "error", "-i", path, "-f", "f32le",
                 "-ac", "1", "-ar", "48000", "pipe:1"],
                capture_output=True,
                check=True,
                timeout=60,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise RuntimeError("无法解码远程 TTS 音频，请确认 ffmpeg 已安装") from exc
        import numpy as np
        wave = np.frombuffer(result.stdout, dtype=np.float32).copy()
        if wave.size == 0:
            raise RuntimeError("远程 TTS 音频为空")
        return wave, 48000

    @staticmethod
    def _write_remote_wav(
        waves: list[tuple[int, Any, int]],
        *,
        first_frame: int,
        output_path: str | None,
        model: str,
    ) -> SynthesizedSpeech:
        """Lay out decoded remote waves without optional soundfile/scipy."""
        import numpy as np

        output_rate = 48000
        pause = np.zeros(int(0.3 * output_rate), dtype=np.float32)
        pieces: list[Any] = []
        cue_durations: list[float] = []
        for index, (_start_frame, waveform, sample_rate) in enumerate(waves):
            segment = np.asarray(waveform, dtype=np.float32).reshape(-1)
            if int(sample_rate) != output_rate:
                raise RuntimeError("远程 TTS 解码采样率异常")
            fade = min(int(0.05 * output_rate), len(segment))
            if fade:
                segment[:fade] *= np.linspace(0.0, 1.0, fade, dtype=np.float32)
            pieces.append(segment)
            cue_durations.append(float(len(segment)) / output_rate)
            if index < len(waves) - 1:
                pieces.append(pause)
        layout = np.concatenate(pieces) if pieces else np.zeros(0, dtype=np.float32)
        layout = np.concatenate([layout, np.zeros(int(0.5 * output_rate), dtype=np.float32)])
        if output_path is None:
            handle = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
            output_path = handle.name
            handle.close()
        pcm = (np.clip(layout, -1.0, 1.0) * 32767.0).astype("<i2").tobytes()
        with wave.open(output_path, "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(output_rate)
            wav.writeframes(pcm)
        return SynthesizedSpeech(
            path=output_path,
            sample_rate=output_rate,
            duration_seconds=float(len(layout)) / output_rate,
            first_frame=first_frame,
            model=model,
            cue_durations=tuple(cue_durations),
        )


__all__ = ["ProfileAwareTtsProvider"]
