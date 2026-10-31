"""真实 visual_caption provider —— Qwen3-VL（画面理解，视频+图片共用）。

用 transformers 加载 Qwen3-VL-8B（4bit AWQ 量化，24GB 显存主力）。
生成 caption + tags，写入 image_meta / clip。

降级安全：transformers/模型未装 → is_available()=False → stub 兜底。
"""

from __future__ import annotations

import logging
from typing import Any

from apps.topos_vault.providers.base import Capability, ProviderDescriptor, get_registry
from apps.topos_vault.providers.contracts import CaptionResult
from apps.topos_vault.providers.model_loader import LazyModelLoader, is_model_pack_installed

logger = logging.getLogger(__name__)


class Qwen3VLCaptioner(LazyModelLoader):
    """Qwen3-VL-8B 画面理解 provider（4bit AWQ 量化）。

    24GB 显存主力模型。生成中文 caption + 标签。
    """

    provider_id = "qwen3_vl_8b_awq"
    capability = Capability.VISUAL_CAPTION

    MODEL_HF_ID = "Qwen/Qwen3-VL-8B-Instruct-AWQ"
    MODEL_PACK_ID = "qwen3_vl_8b_awq"

    # 让模型输出结构化标签的 prompt
    CAPTION_PROMPT = (
        "请用中文描述这张图片，并提取 3-5 个关键词标签。"
        "格式：\n描述：<一句话描述>\n标签：<标签1>,<标签2>,..."
    )

    def __init__(self):
        super().__init__(provider_id=self.provider_id, model_pack_id=self.MODEL_PACK_ID)

    def _check_dependencies(self) -> bool:
        try:
            import transformers  # noqa: F401
            import torch  # noqa: F401
        except ImportError:
            return False
        # 模型文件检查（避免 is_available=True 但加载时联网/崩溃）
        return is_model_pack_installed(self.MODEL_PACK_ID) or _is_in_hf_cache(self.MODEL_HF_ID)

    def _load_model(self) -> Any:
        """加载 Qwen3-VL 模型 + processor。"""
        from transformers import AutoModelForCausalLM, AutoProcessor
        from apps.topos_vault.providers.model_loader import resolve_torch_device

        device, dtype = resolve_torch_device()
        logger.info("加载 Qwen3-VL-8B AWQ（device=%s）...", device)

        local_path = self._resolve_local_or_hf(self.MODEL_HF_ID)
        processor = AutoProcessor.from_pretrained(local_path)
        model = AutoModelForCausalLM.from_pretrained(
            local_path,
            torch_dtype=dtype,
            device_map=device,
        )
        logger.info("Qwen3-VL 加载完成")
        return {"model": model, "processor": processor, "device": device}

    def caption_image(self, image_path: str) -> CaptionResult:
        """理解图片，生成 caption + tags。"""
        if self.model is None:
            raise RuntimeError(f"provider {self.provider_id} 模型未加载")

        from PIL import Image

        model = self.model["model"]
        processor = self.model["processor"]
        device = self.model["device"]

        image = Image.open(image_path).convert("RGB")

        # 构造对话消息：content 里的 image 块给 PIL.Image（也支持路径字符串，按版本兼容）
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "image", "image": image_path},
                    {"type": "text", "text": self.CAPTION_PROMPT},
                ],
            }
        ]

        text = processor.apply_chat_template(
            messages, tokenize=False, add_generation_prompt=True,
        )
        inputs = processor(
            text=[text], images=[image], padding=True, return_tensors="pt",
        ).to(device)

        import torch
        with torch.no_grad():
            output_ids = model.generate(**inputs, max_new_tokens=200)

        generated = processor.batch_decode(
            output_ids[:, inputs.input_ids.shape[1]:],
            skip_special_tokens=True,
        )[0]

        # 解析输出（描述 + 标签）
        caption, tags = _parse_caption_output(generated)
        return CaptionResult(
            caption=caption,
            tags=tags,
            model=self.provider_id,
        )


def _parse_caption_output(text: str) -> tuple[str, list[str]]:
    """解析模型输出为 (caption, tags)。

    模型输出格式（由 CAPTION_PROMPT 约束）：
        描述：<一句话描述>
        标签：<标签1>,<标签2>,...
    """
    caption = text.strip()
    tags: list[str] = []

    lines = text.strip().splitlines()
    for line in lines:
        line = line.strip()
        if line.startswith("描述：") or line.startswith("描述:"):
            caption = line.split("：", 1)[-1].split(":", 1)[-1].strip()
        elif line.startswith("标签：") or line.startswith("标签:"):
            tags_str = line.split("：", 1)[-1].split(":", 1)[-1].strip()
            tags = [t.strip() for t in tags_str.replace("，", ",").split(",") if t.strip()]

    # 如果没解析出结构化输出，用整段文本做 caption
    if not caption:
        caption = text.strip()[:100]

    return caption, tags


def _is_in_hf_cache(hf_id: str) -> bool:
    """模型是否在 HuggingFace 缓存里（org/name → models--org--name）。"""
    from pathlib import Path
    cache = Path.home() / ".cache" / "huggingface" / "hub"
    if not cache.exists():
        return False
    org, name = hf_id.split("/", 1)
    return (cache / f"models--{org}--{name}").is_dir()


def register_real_visual_caption() -> None:
    """注册真实的 visual_caption provider。"""
    reg = get_registry()
    if reg.get("qwen3_vl_8b_awq") is None:
        reg.register(
            Qwen3VLCaptioner(),
            ProviderDescriptor(
                provider_id="qwen3_vl_8b_awq",
                capability=Capability.VISUAL_CAPTION,
                display_name="Qwen3-VL 8B（AWQ 4bit）",
                requires_gpu=True, min_vram_mb=6000,
                model_pack_id="qwen3_vl_8b_awq",
                notes="24GB 显存主力画面理解模型。中英双语 caption/tag。",
            ),
            is_default=True,
        )
