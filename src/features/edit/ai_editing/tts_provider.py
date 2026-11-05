"""Qwen3-TTS speech synthesis for subtitle-to-voice workflows.

The provider is intentionally UI-agnostic: it receives subtitle text, timeline
frame positions, and a :class:`TtsSettings`, synthesizes one waveform per cue,
then lays those waveforms into a single WAV while preserving the cue timing gaps.

Two synthesis modes (selected by ``TtsSettings.mode``):
  - ``custom_voice``: 9 built-in speakers + optional instruct (CustomVoice model)
  - ``voice_clone``:  reference-audio voice cloning (Base model)
"""

from __future__ import annotations

import logging
import tempfile
import threading
from dataclasses import dataclass
from typing import Any, Sequence

from src.features.edit.ai_editing.tts_settings import DEFAULT_TTS_SETTINGS, TtsSettings

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SpeechCue:
    text: str
    start_frame: int


@dataclass(frozen=True)
class SynthesizedSpeech:
    path: str
    sample_rate: int
    duration_seconds: float
    first_frame: int
    model: str
    cue_durations: tuple[float, ...] = ()
    """每条 cue 的实际音频时长（秒），按输入 cues 顺序。

    controller 用它按 fps 换算帧数，调整对应字幕片段的 timeline_duration，
    使字幕与语音精确对齐（而非固定时长）。
    """


class Qwen3TTSProvider:
    """Lazy Qwen3-TTS provider supporting built-in voices and voice cloning.

    Real inference is the default; set ``TOPOS_TTS_REAL=0`` to force a stub
    (tests only).  Two model engines are loaded lazily and cached:
      - CustomVoice engine (for ``generate_custom_voice``)
      - Base engine (for ``generate_voice_clone``)
    Only the engine needed for the current ``TtsSettings.mode`` is loaded.
    """

    provider_id = "tts_qwen3"

    def __init__(self, *, model_pack_manager: Any | None = None) -> None:
        self._model_pack_manager = model_pack_manager
        # 惰性引擎缓存：按 model_kind 分别缓存，避免重复加载 3.8GB 权重。
        self._engines: dict[str, Any] = {}  # model_kind → engine
        self._model_paths: dict[str, str] = {}  # model_kind → path
        # AUD-14918：缓存无锁时，并发 synthesize 会同时看到 miss 并各自加载
        # 一份 3.8GB 权重（显存/内存翻倍且互相覆盖）。双检锁保证每个 kind
        # 恰好加载一次；命中路径不进锁，热路径无额外开销。
        self._engines_lock = threading.Lock()

    # ------------------------------------------------------------------ #
    # 可用性
    # ------------------------------------------------------------------ #

    def is_available(self) -> bool:
        """是否可用（qwen_tts 可 import 且推理开关未关闭）。"""
        from src.features.edit.ai_editing.transcript_provider import _real_inference_enabled
        if not _real_inference_enabled("TOPOS_TTS_REAL"):
            return False
        try:
            import importlib.util
            return importlib.util.find_spec("qwen_tts") is not None
        except Exception:
            return False

    def is_model_ready(self, settings: TtsSettings) -> bool:
        """当前模式所需模型 pack 是否已安装。"""
        if self._model_pack_manager is None:
            return False
        pack_id = "qwen3_tts_base" if settings.mode == "voice_clone" else "qwen3_tts_customvoice"
        try:
            path = self._model_pack_manager.require_usable(pack_id)
            return bool(path)
        except Exception:
            return False

    # ------------------------------------------------------------------ #
    # 合成入口
    # ------------------------------------------------------------------ #

    def synthesize(
        self,
        cues: Sequence[SpeechCue],
        *,
        fps: float,
        settings: TtsSettings | None = None,
        output_path: str | None = None,
    ) -> SynthesizedSpeech:
        """合成字幕为配音 WAV。

        Args:
            cues: 字幕提示（text + start_frame）。
            fps: 时间线帧率，用于 frame→秒换算。
            settings: 合成设置（音色/语言/instruct/克隆/高级参数）；None 用默认。
            output_path: 输出 wav 路径；None 用临时文件。
        """
        settings = settings or DEFAULT_TTS_SETTINGS
        clean_cues = tuple(c for c in cues if c.text and c.text.strip())
        if not clean_cues:
            raise ValueError("没有可合成的字幕文本")
        if fps <= 0:
            raise ValueError("fps must be positive")
        if not self.is_available():
            raise RuntimeError(
                "Qwen3-TTS 未启用：请安装 qwen-tts 并下载模型权重"
            )

        # 按模式分派
        if settings.mode == "voice_clone":
            waves = self._synthesize_voice_clone(clean_cues, settings)
        else:
            waves = self._synthesize_custom_voice(clean_cues, settings)

        return self._lay_down_waves(waves, clean_cues, fps, settings, output_path)

    # ------------------------------------------------------------------ #
    # 内置音色合成
    # ------------------------------------------------------------------ #

    def _synthesize_custom_voice(
        self, cues: tuple[SpeechCue, ...], settings: TtsSettings,
    ) -> list[tuple[int, Any, int]]:
        """用 CustomVoice 模型的 generate_custom_voice 逐 cue 合成。

        Returns:
            [(start_frame, waveform_np, sample_rate), ...]
        """
        import numpy as np  # noqa: F401  # 确保依赖提示清晰
        engine = self._get_engine("custom_voice")
        gen_kwargs = settings.to_generate_kwargs()

        results: list[tuple[int, Any, int]] = []
        for cue_index, cue in enumerate(cues):
            text = cue.text.strip()
            # language="Auto" 直接透传给模型，由模型自动检测（不再用粗糙启发式）
            cv_kwargs: dict[str, Any] = {
                "text": text,
                "speaker": settings.speaker,
                "language": settings.language or "Auto",
                **gen_kwargs,
            }
            # instruct 仅在有内容时传入（空串会让某些版本报错）
            if settings.instruct and settings.instruct.strip():
                cv_kwargs["instruct"] = settings.instruct.strip()

            wavs, sr = engine.generate_custom_voice(**cv_kwargs)
            if not wavs:
                # AUD-14913：失败 cue 不能 continue 跳过——waves/cue_durations 与
                # 输入 cues 按索引一一对位（controller 按位置配对字幕），跳过会让
                # 其后所有 cue 的时长安到前一条字幕上。以 0 时长空波形占位。
                logger.warning(
                    "TTS cue %d produced no waveform; keeping 0-duration placeholder",
                    cue_index,
                )
                results.append((
                    int(cue.start_frame),
                    np.zeros(0, dtype=np.float32),
                    self._OUTPUT_SAMPLE_RATE,
                ))
                continue
            results.append((int(cue.start_frame), wavs[0], int(sr)))
        return results

    # ------------------------------------------------------------------ #
    # 音色克隆合成
    # ------------------------------------------------------------------ #

    def _synthesize_voice_clone(
        self, cues: tuple[SpeechCue, ...], settings: TtsSettings,
    ) -> list[tuple[int, Any, int]]:
        """用 Base 模型做参考音频音色克隆。

        预计算一次 voice_clone_prompt，复用到所有 cue（避免重复提取特征）。
        """
        if not settings.ref_audio_path:
            raise ValueError("音色克隆模式需要指定参考音频文件")
        if not settings.x_vector_only_mode and not settings.ref_text.strip():
            raise ValueError(
                "ICL 音色克隆模式需要填写参考音频的转写文本（ref_text）；"
                "或勾选 x_vector_only_mode 只用声纹特征"
            )
        import os
        if not os.path.isfile(settings.ref_audio_path):
            raise FileNotFoundError(f"参考音频不存在: {settings.ref_audio_path}")

        engine = self._get_engine("base")

        # 预计算 clone prompt（含 ref_code + speaker embedding），所有 cue 复用
        prompt_items = engine.create_voice_clone_prompt(
            ref_audio=settings.ref_audio_path,
            ref_text=settings.ref_text.strip() or None,
            x_vector_only_mode=settings.x_vector_only_mode,
        )
        prompt = prompt_items[0] if prompt_items else None
        if prompt is None:
            raise RuntimeError("无法从参考音频提取音色特征")

        gen_kwargs = settings.to_generate_kwargs()
        import numpy as np
        results: list[tuple[int, Any, int]] = []
        for cue_index, cue in enumerate(cues):
            text = cue.text.strip()
            wavs, sr = engine.generate_voice_clone(
                text=text,
                language=settings.language or "Auto",
                voice_clone_prompt=prompt,
                **gen_kwargs,
            )
            if not wavs:
                # AUD-14913：同 _synthesize_custom_voice——失败 cue 以 0 时长
                # 空波形占位，保证 cue_durations 与输入 cues 按索引对位。
                logger.warning(
                    "TTS voice-clone cue %d produced no waveform; "
                    "keeping 0-duration placeholder",
                    cue_index,
                )
                results.append((
                    int(cue.start_frame),
                    np.zeros(0, dtype=np.float32),
                    self._OUTPUT_SAMPLE_RATE,
                ))
                continue
            results.append((int(cue.start_frame), wavs[0], int(sr)))
        return results

    # ------------------------------------------------------------------ #
    # 波形铺设（frame 偏移 → 连续 wav）
    # ------------------------------------------------------------------ #

    # 输出采样率：模型原生 24kHz，升频到 48kHz 改善听感（高质量重采样）。
    _OUTPUT_SAMPLE_RATE = 48000

    def _lay_down_waves(
        self,
        waves: list[tuple[int, Any, int]],
        cues: tuple[SpeechCue, ...],
        fps: float,
        settings: TtsSettings,
        output_path: str | None,
    ) -> SynthesizedSpeech:
        """把各 cue 的波形**顺序拼接**成一个连续 WAV。

        每条音频按实际长度首尾相接（不是按 frame offset 定位），句间加
        0.3 秒静音停顿。这样：

        - 音频天然连续，无重叠/截断（"上一句没说完下一句就开始"不会发生）。
        - ``cue_durations`` 返回每条的实际时长，controller 据此调整字幕时间码
          （字幕 start_frame/duration 从音频顺序推导，精确对齐）。
        - ``first_frame`` 取第一条 cue 的 start_frame（保持时间线偏移语义）。
        """
        import numpy as np

        # AUD-14913：失败的 cue 以 0 时长空波形占位（保持索引对位），全部失败
        # 仍然视为未产出任何音频。
        if not waves or not any(len(w[1]) > 0 for w in waves):
            raise RuntimeError("合成未产生任何音频波形")

        import soundfile as sf
        from scipy.signal import resample_poly

        out_sr = self._OUTPUT_SAMPLE_RATE
        first_frame = min(w[0] for w in waves)

        # 重采样每条音频到统一采样率，并记录实际时长
        pause_samples = int(0.3 * out_sr)  # 句间 0.3s 静音
        processed: list[np.ndarray] = []
        cue_durations: list[float] = []
        for idx, (start_frame, waveform, sr) in enumerate(waves):
            # 1. 重采样到输出采样率（占位空波形已按 out_sr 标注，直接跳过）
            if int(sr) != out_sr:
                from math import gcd
                g = gcd(int(sr), out_sr)
                waveform = resample_poly(waveform, up=out_sr // g, down=int(sr) // g)

            seg = np.asarray(waveform, dtype=np.float32)
            # 2. 头部 50ms 淡入（减少拼接处爆音）
            fade = min(int(0.05 * out_sr), len(seg))
            if fade > 0:
                seg[:fade] *= np.linspace(0, 1, fade, dtype=np.float32)

            processed.append(seg)
            cue_durations.append(float(len(seg)) / out_sr)

            # 3. 句间加静音停顿（最后一条/0 时长占位不加）
            if idx < len(waves) - 1 and len(seg) > 0:
                processed.append(np.zeros(pause_samples, dtype=np.float32))
                cue_durations[-1] += 0.3  # 停顿计入该 cue 时长

        # 4. 顺序拼接 + 尾部 0.5s 余量
        layout = np.concatenate(processed) if processed else np.zeros(0, dtype=np.float32)
        layout = np.concatenate([layout, np.zeros(int(0.5 * out_sr), dtype=np.float32)])

        duration = len(layout) / out_sr
        if output_path is None:
            tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
            output_path = tmp.name
            tmp.close()
        sf.write(output_path, layout, out_sr, subtype="PCM_16")

        model_name = (
            "Qwen3-TTS-12Hz-1.7B-Base"
            if settings.mode == "voice_clone"
            else "Qwen3-TTS-12Hz-1.7B-CustomVoice"
        )
        # 提取每条 cue 的实际音频时长（用于 controller 调整字幕片段 duration）
        cue_durations = tuple(
            float(len(w[1])) / float(w[2]) for w in waves
        )

        return SynthesizedSpeech(
            path=output_path,
            sample_rate=out_sr,
            duration_seconds=duration,
            first_frame=first_frame,
            model=model_name,
            cue_durations=cue_durations,
        )

    # ------------------------------------------------------------------ #
    # 惰性模型加载
    # ------------------------------------------------------------------ #

    _MODEL_PACK_FOR_KIND = {
        "custom_voice": "qwen3_tts_customvoice",
        "base": "qwen3_tts_base",
    }

    # 候选目录名（兼容用户早期下载到 qwen3_tts/ 的情况）
    _PACK_ALIASES: dict[str, tuple[str, ...]] = {
        "qwen3_tts_customvoice": ("qwen3_tts_customvoice", "qwen3_tts"),
        "qwen3_tts_base": ("qwen3_tts_base",),
    }

    def _get_engine(self, model_kind: str) -> Any:
        """惰性加载并缓存指定 kind 的 TTS 引擎。

        Args:
            model_kind: ``"custom_voice"`` 或 ``"base"``。

        AUD-14918：双检锁——无锁命中（已加载）直接返回；miss 时进锁二次确认
        再加载，保证并发 synthesize 不会重复加载 3.8GB 权重。
        """
        if model_kind in self._engines:
            return self._engines[model_kind]

        with self._engines_lock:
            if model_kind in self._engines:
                return self._engines[model_kind]
            pack_id = self._MODEL_PACK_FOR_KIND[model_kind]
            model_path = self._resolve_model_path(pack_id)
            engine = self._load_model(model_path, model_kind)
            self._engines[model_kind] = engine
            self._model_paths[model_kind] = model_path
            logger.info("Qwen3-TTS [%s] engine loaded (%s)", model_kind, model_path)
            return engine

    def _resolve_model_path(self, pack_id: str) -> str:
        """解析模型 pack 的本地安装路径；缺失抛 RuntimeError。

        支持多个候选目录名（``_PACK_ALIASES``），兼容用户早期下载到
        ``qwen3_tts/`` 而非 ``qwen3_tts_customvoice/`` 的情况。
        """
        candidates = self._PACK_ALIASES.get(pack_id, (pack_id,))
        # 1. 通过 ModelPackManager 查（manifest 登记的 pack）
        if self._model_pack_manager is not None:
            for cand in candidates:
                try:
                    path = self._model_pack_manager.require_usable(cand)
                    if path and self._has_model_weights(path):
                        return path
                except Exception:
                    continue
        # 2. 直接查候选目录（manager 未登记或无 manager 时）
        from src.features.edit.ai_editing.model_pack_manager import default_cache_dir
        base_dir = default_cache_dir()
        for cand in candidates:
            cand_path = str(base_dir / cand)
            if self._has_model_weights(cand_path):
                return cand_path
        raise RuntimeError(
            f"模型 pack '{pack_id}' 未安装或权重文件缺失。\n"
            f"已尝试目录: {', '.join(candidates)}\n"
            f"请下载模型到 models/ai_editing/{candidates[0]}/"
        )

    @staticmethod
    def _has_model_weights(path: str) -> bool:
        """目录是否存在且包含模型权重文件（safetensors/bin）。"""
        import os
        if not path or not os.path.isdir(path):
            return False
        weight_names = ("model.safetensors", "pytorch_model.bin",
                        "model.safetensors.index.json")
        return any(os.path.isfile(os.path.join(path, n)) for n in weight_names)

    def _load_model(self, model_path: str, model_kind: str) -> Any:
        """加载 Qwen3-TTS 模型（带 MPS/CUDA → CPU 回退）。"""
        try:
            import torch  # noqa: F401 — 依赖可用性探测：缺失时须在此抛安装提示（勿删）
            from qwen_tts import Qwen3TTSModel
        except ImportError as exc:
            raise RuntimeError("请安装 qwen-tts、torch、soundfile") from exc

        from src.features.edit.ai_editing.transcript_provider import (
            _cpu_fallback_kwargs,
            _resolve_torch_device,
        )
        device_map, dtype = _resolve_torch_device()
        kwargs: dict[str, Any] = {"dtype": dtype}
        if device_map is not None:
            kwargs["device_map"] = device_map
        # sdpa 注意力实现（不依赖 flash-attn，跨平台兼容）
        kwargs["attn_implementation"] = "sdpa"

        try:
            return Qwen3TTSModel.from_pretrained(model_path, **kwargs)
        except Exception as exc:
            if device_map in ("mps", "cuda:0"):
                fb_device, fb_dtype = _cpu_fallback_kwargs()
                logger.warning(
                    "Qwen3-TTS [%s] init failed on %s (%s); retrying on CPU.",
                    model_kind, device_map, exc,
                )
                fb_kwargs: dict[str, Any] = {
                    "dtype": fb_dtype,
                    "device_map": fb_device,
                    "attn_implementation": "sdpa",
                }
                return Qwen3TTSModel.from_pretrained(model_path, **fb_kwargs)
            raise

    # ------------------------------------------------------------------ #
    # 语言猜测（保留兼容，但 synthesize 默认不再调用——透传 Auto 给模型）
    # ------------------------------------------------------------------ #

    @staticmethod
    def _guess_language(text: str) -> str:
        """粗糙的中英文启发式（仅作后备；生产应透传 ``Auto`` 给模型）。"""
        cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
        return "Chinese" if cjk > len(text) * 0.3 else "English"

    @staticmethod
    def _supported_speakers() -> list[str] | None:
        """查询已加载 CustomVoice 引擎的内置音色清单（UI 可用于填充下拉）。"""
        from src.features.edit.ai_editing.tts_settings import BUILTIN_SPEAKERS
        return [s[0] for s in BUILTIN_SPEAKERS]
