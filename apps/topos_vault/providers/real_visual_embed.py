"""真实 visual_embed provider —— SigLIP 2（画面向量，视频+图片共用）。

用 sentence-transformers 的多模态模型加载 SigLIP 2（或 CLIP 备选）。
画面理解是视频镜头和图片共用的能力——同一个模型 + 同一个 visual.faiss 索引。

降级安全：sentence-transformers/模型未装 → is_available()=False → stub 兜底。
"""

from __future__ import annotations

import logging
from typing import Any

from apps.topos_vault.providers.base import Capability, ProviderDescriptor, get_registry
from apps.topos_vault.providers.contracts import EmbeddingResult
from apps.topos_vault.providers.model_loader import LazyModelLoader, is_model_pack_installed

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------- #
# 辅助：把 sentence-transformers/torch 的向量输出统一成 list[float] + dim
# ---------------------------------------------------------------------- #

def _vec_to_list(vec: Any) -> list[float]:
    """把 numpy/torch/tensor 向量转成 list[float]。"""
    if isinstance(vec, list):
        return [float(x) for x in vec]
    # numpy / torch 都有 .tolist()
    if hasattr(vec, "tolist"):
        return [float(x) for x in vec.tolist()]
    return [float(x) for x in list(vec)]


def _vec_dim(vec: Any) -> int:
    """取向量维度（兼容 1D / 带 batch 维）。"""
    if hasattr(vec, "shape") and vec.shape:
        return int(vec.shape[-1])
    return len(vec)


class SigLIP2EmbeddingProvider(LazyModelLoader):
    """SigLIP 2 画面向量 provider（视频镜头和图片共用）。

    SigLIP 2 是 2026 年最强开源图文编码器。
    用 sentence-transformers 加载（支持 CLIP/SigLIP 系列多模态模型）。
    """

    provider_id = "siglip2_base"
    capability = Capability.VISUAL_EMBED

    # sentence-transformers 的 SigLIP 2 模型 id
    # （官方 pack 是 google/siglip2-base-patch16-224，sentence-transformers 的封装版
    # 自动配好 pooling/router，可直接 encode 图片和文本）
    MODEL_HF_ID = "sentence-transformers/siglip2-base-patch16-224"
    MODEL_PACK_ID = "siglip2_base"
    # base-patch16-224 的 hidden_size = 768（giant/so400m 才是 1152）
    DIM = 768

    def __init__(self):
        super().__init__(provider_id=self.provider_id, model_pack_id=self.MODEL_PACK_ID)

    def _check_dependencies(self) -> bool:
        try:
            import sentence_transformers  # noqa: F401
        except ImportError:
            return False
        # 模型文件检查：本地 pack 目录 或 HF 缓存（避免 is_available=True 但加载崩溃）
        return is_model_pack_installed(self.MODEL_PACK_ID) or self._is_in_hf_cache()

    def _is_in_hf_cache(self) -> bool:
        """SigLIP2 是否在 HuggingFace 缓存里。"""
        from pathlib import Path
        cache = Path.home() / ".cache" / "huggingface" / "hub"
        if not cache.exists():
            return False
        # 模型 id sentence-transformers/siglip2-base-patch16-224 → 缓存目录名
        org, name = self.MODEL_HF_ID.split("/", 1)
        return (cache / f"models--{org}--{name}").is_dir()

    def _load_model(self) -> Any:
        from sentence_transformers import SentenceTransformer
        logger.info("加载 SigLIP 2 模型...")
        # sentence-transformers 5.x 内置 modal router，会按输入是 PIL/路径/文本
        # 自动分派到图文双塔对应的编码器。优先用 HF id（落到本地缓存或 models/）
        model = SentenceTransformer(self._resolve_local_or_hf(self.MODEL_HF_ID))
        dim = model.get_sentence_embedding_dimension()
        logger.info("SigLIP 2 加载完成，维度=%d", dim)
        if dim != self.DIM:
            logger.warning(
                "SigLIP2 实际维度 %d 与预期 %d 不符，以实际为准", dim, self.DIM,
            )
        return model

    def embed_image(self, image_path: str) -> EmbeddingResult:
        """图片 → 向量。"""
        if self.model is None:
            raise RuntimeError(f"provider {self.provider_id} 模型未加载")
        # sentence-transformers 5.x：encode 直接吃文件路径字符串（内部用 PIL 读）。
        # 兜底：若版本不支持路径，改用 PIL.Image 传入。
        try:
            vec = self.model.encode(image_path, normalize_embeddings=True)
        except Exception:
            from PIL import Image
            img = Image.open(image_path).convert("RGB")
            vec = self.model.encode(img, normalize_embeddings=True)
        return EmbeddingResult(
            vector=_vec_to_list(vec),
            dim=int(_vec_dim(vec)),
            model=self.provider_id,
        )

    def embed_text(self, text: str) -> EmbeddingResult:
        """文本 → 向量（与图片同空间，用于文本搜画面）。

        SigLIP/CLIP 是双塔模型：图片和文本编码到同一空间，
        所以"微笑"的文本向量能匹配微笑画面的图片向量。
        """
        if self.model is None:
            raise RuntimeError(f"provider {self.provider_id} 模型未加载")
        vec = self.model.encode(text, normalize_embeddings=True)
        return EmbeddingResult(
            vector=_vec_to_list(vec), dim=int(_vec_dim(vec)), model=self.provider_id,
        )


class CLIPEmbeddingProvider(LazyModelLoader):
    """OpenAI CLIP 备选 provider（中文支持弱于 SigLIP 2，但兼容性好）。"""

    provider_id = "clip_chinese"
    capability = Capability.VISUAL_EMBED

    MODEL_HF_ID = "OFA-Sys/chinese-clip-vit-base-patch16"
    MODEL_PACK_ID = "clip_chinese"

    def __init__(self):
        super().__init__(provider_id=self.provider_id, model_pack_id=self.MODEL_PACK_ID)

    def _check_dependencies(self) -> bool:
        try:
            import sentence_transformers  # noqa: F401
            return True
        except ImportError:
            return False

    def _load_model(self) -> Any:
        from sentence_transformers import SentenceTransformer
        return SentenceTransformer(self._resolve_local_or_hf(self.MODEL_HF_ID))

    def embed_image(self, image_path: str) -> EmbeddingResult:
        if self.model is None:
            raise RuntimeError(f"provider {self.provider_id} 模型未加载")
        try:
            vec = self.model.encode(image_path, normalize_embeddings=True)
        except Exception:
            from PIL import Image
            img = Image.open(image_path).convert("RGB")
            vec = self.model.encode(img, normalize_embeddings=True)
        return EmbeddingResult(
            vector=_vec_to_list(vec), dim=int(_vec_dim(vec)), model=self.provider_id,
        )

    def embed_text(self, text: str) -> EmbeddingResult:
        if self.model is None:
            raise RuntimeError(f"provider {self.provider_id} 模型未加载")
        vec = self.model.encode(text, normalize_embeddings=True)
        return EmbeddingResult(
            vector=_vec_to_list(vec), dim=int(_vec_dim(vec)), model=self.provider_id,
        )


def register_real_visual_embed() -> None:
    """注册真实的 visual_embed provider。"""
    reg = get_registry()
    if reg.get("siglip2_base") is None:
        reg.register(
            SigLIP2EmbeddingProvider(),
            ProviderDescriptor(
                provider_id="siglip2_base",
                capability=Capability.VISUAL_EMBED,
                display_name="SigLIP 2 base（1152维）",
                requires_gpu=False, min_vram_mb=0,
                model_pack_id="siglip2_base",
                notes="2026 最强开源图文编码器。视频镜头和图片共用。",
            ),
            is_default=True,
        )
    if reg.get("clip_chinese") is None:
        reg.register(
            CLIPEmbeddingProvider(),
            ProviderDescriptor(
                provider_id="clip_chinese",
                capability=Capability.VISUAL_EMBED,
                display_name="Chinese-CLIP（备选）",
                requires_gpu=False, min_vram_mb=0,
                model_pack_id="clip_chinese",
                notes="备选。中文支持弱于 SigLIP 2，但兼容性好。",
            ),
            is_default=False,
        )
