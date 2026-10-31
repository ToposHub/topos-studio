"""真实 audio_understand provider —— Qwen2.5-Omni（音乐/音频语义理解）。

用 transformers 加载 Qwen2.5-Omni-7B（4bit AWQ）。
生成情绪/乐器/风格描述。

注：BPM/调性等**数值**特征由 librosa 单独算（大模型做不了精确 BPM），
本 provider 只负责语义理解。

降级安全：transformers/模型未装 → is_available()=False → stub 兜底。
"""

from __future__ import annotations

import logging
from typing import Any

from apps.topos_vault.providers.base import Capability, ProviderDescriptor, get_registry
from apps.topos_vault.providers.contracts import AudioUnderstandingResult
from apps.topos_vault.providers.model_loader import LazyModelLoader, is_model_pack_installed

logger = logging.getLogger(__name__)


class Qwen25OmniAudioProvider(LazyModelLoader):
    """Qwen2.5-Omni-7B 音频理解 provider。"""

    provider_id = "qwen2_5_omni_7b_awq"
    capability = Capability.AUDIO_UNDERSTAND

    MODEL_HF_ID = "Qwen/Qwen2.5-Omni-7B-AWQ"
    MODEL_PACK_ID = "qwen2_5_omni_7b_awq"

    AUDIO_PROMPT = (
        "请分析这段音频，输出：\n"
        "情绪：<happy/sad/tense/calm/energetic>\n"
        "乐器：<逗号分隔>\n"
        "类型：<instrumental/vocal/mixed>\n"
        "描述：<一句话中文描述>"
    )

    def __init__(self):
        super().__init__(provider_id=self.provider_id, model_pack_id=self.MODEL_PACK_ID)

    def _check_dependencies(self) -> bool:
        try:
            import transformers  # noqa: F401
            import torch  # noqa: F401
            import soundfile  # noqa: F401
        except ImportError:
            return False
        # 模型文件检查（避免 is_available=True 但加载时联网/崩溃）
        return is_model_pack_installed(self.MODEL_PACK_ID) or _is_in_hf_cache(self.MODEL_HF_ID)

    def _load_model(self) -> Any:
        from transformers import AutoProcessor, Qwen2_5OmniForConditionalGeneration
        from apps.topos_vault.providers.model_loader import resolve_torch_device

        device, dtype = resolve_torch_device()
        logger.info("加载 Qwen2.5-Omni-7B AWQ（device=%s）...", device)

        local_path = self._resolve_local_or_hf(self.MODEL_HF_ID)
        processor = AutoProcessor.from_pretrained(local_path)
        # enable_audio_output=False：我们只要文本理解，不要合成语音，省显存
        model = Qwen2_5OmniForConditionalGeneration.from_pretrained(
            local_path, torch_dtype=dtype, device_map=device,
        )
        return {"model": model, "processor": processor, "device": device}

    def understand_audio(self, audio_path: str) -> AudioUnderstandingResult:
        """理解音频，生成情绪/乐器/描述。"""
        if self.model is None:
            raise RuntimeError(f"provider {self.provider_id} 模型未加载")

        import soundfile as sf
        import torch
        import numpy as np

        model = self.model["model"]
        processor = self.model["processor"]
        device = self.model["device"]

        # 读取音频（float32 单声道 numpy 数组）
        audio, sr = sf.read(audio_path)
        if audio.ndim > 1:
            audio = audio.mean(axis=1)  # 转单声道
        audio = audio.astype(np.float32)

        # 构造输入：content 里的 audio 块给文件路径（processor 内部读取）
        # 官方 README 模式：messages 里给 path，processor(audio=[...]) 给原始波形
        messages = [{"role": "user", "content": [
            {"type": "audio", "audio": audio_path},
            {"type": "text", "text": self.AUDIO_PROMPT},
        ]}]

        text = processor.apply_chat_template(
            messages, tokenize=False, add_generation_prompt=True,
        )
        # 注意：processor 的参数名是 audio=（不是 audios=），传 numpy 波形列表
        inputs = processor(
            text=text, audio=[audio], sampling_rate=sr,
            return_tensors="pt", padding=True,
        ).to(device)

        with torch.no_grad():
            output_ids = model.generate(
                **inputs, max_new_tokens=200, use_audio_in_video=False,
            )

        generated = processor.batch_decode(
            output_ids[:, inputs.input_ids.shape[1]:], skip_special_tokens=True,
        )[0]

        mood, instruments, vocal_type, description = _parse_audio_output(generated)
        return AudioUnderstandingResult(
            description=description,
            mood=mood,
            instruments=instruments,
            vocal_type=vocal_type,
            model=self.provider_id,
        )


def _parse_audio_output(text: str) -> tuple[str | None, list[str], str | None, str]:
    """解析模型输出为 (mood, instruments, vocal_type, description)。"""
    mood = None
    instruments: list[str] = []
    vocal_type = None
    description = text.strip()

    for line in text.strip().splitlines():
        line = line.strip()
        if line.startswith("情绪"):
            mood = line.split("：", 1)[-1].split(":", 1)[-1].strip().lower()
        elif line.startswith("乐器"):
            tags_str = line.split("：", 1)[-1].split(":", 1)[-1].strip()
            instruments = [t.strip() for t in tags_str.replace("，", ",").split(",") if t.strip()]
        elif line.startswith("类型"):
            vocal_type = line.split("：", 1)[-1].split(":", 1)[-1].strip().lower()
        elif line.startswith("描述"):
            description = line.split("：", 1)[-1].split(":", 1)[-1].strip()

    return mood, instruments, vocal_type, description


def _is_in_hf_cache(hf_id: str) -> bool:
    """模型是否在 HuggingFace 缓存里（org/name → models--org--name）。"""
    from pathlib import Path
    cache = Path.home() / ".cache" / "huggingface" / "hub"
    if not cache.exists():
        return False
    org, name = hf_id.split("/", 1)
    return (cache / f"models--{org}--{name}").is_dir()


def register_real_audio_understand() -> None:
    """注册真实的 audio_understand provider。"""
    reg = get_registry()
    if reg.get("qwen2_5_omni_7b_awq") is None:
        reg.register(
            Qwen25OmniAudioProvider(),
            ProviderDescriptor(
                provider_id="qwen2_5_omni_7b_awq",
                capability=Capability.AUDIO_UNDERSTAND,
                display_name="Qwen2.5-Omni 7B（AWQ 4bit）",
                requires_gpu=True, min_vram_mb=8000,
                model_pack_id="qwen2_5_omni_7b_awq",
                notes="音乐情绪/乐器/风格理解。BPM 用 librosa 单独算。",
            ),
            is_default=True,
        )
