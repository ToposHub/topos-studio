"""真实 transcript_embed provider —— Qwen3-Embedding / sentence-transformers。

用 sentence-transformers 加载模型（统一接口，支持 BGE-M3/Qwen3-Embedding 等）。
模型懒加载，首次 embed 时才加载进内存。

降级安全：
    - sentence-transformers 未装 → is_available()=False → 系统用 stub 兜底
    - 模型文件不存在 → is_available()=False
    - 加载失败 → is_available()=False，记录错误
"""

from __future__ import annotations

import logging
from typing import Any

from apps.topos_vault.providers.base import Capability, ProviderDescriptor, get_registry
from apps.topos_vault.providers.contracts import EmbeddingResult
from apps.topos_vault.providers.model_loader import (
    LazyModelLoader, is_model_pack_installed,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------- #
# 辅助：把 sentence-transformers/torch 的向量输出统一成 list[float] + dim
# ---------------------------------------------------------------------- #

def _to_float_list(vec: Any) -> list[float]:
    """把 numpy/torch/tensor 向量转成 list[float]。"""
    if isinstance(vec, list):
        return [float(x) for x in vec]
    if hasattr(vec, "tolist"):
        return [float(x) for x in vec.tolist()]
    return [float(x) for x in list(vec)]


def _vec_dim(vec: Any) -> int:
    """取向量维度（兼容单条 / 批量结果）。"""
    if hasattr(vec, "shape") and vec.shape:
        return int(vec.shape[-1])
    return len(vec)


class Qwen3EmbeddingProvider(LazyModelLoader):
    """Qwen3-Embedding 真实 provider。

    用 sentence-transformers 加载 Qwen3-Embedding-0.6B（1024 维）。
    支持批量编码（batch encode）加速。
    """

    provider_id = "qwen3_embedding_0_6b"
    capability = Capability.TRANSCRIPT_EMBED

    # 模型 HuggingFace id（sentence-transformers 加载用）
    MODEL_HF_ID = "Qwen/Qwen3-Embedding-0.6B"
    MODEL_PACK_ID = "qwen3_embedding_0_6b"
    DIM = 1024

    def __init__(self):
        super().__init__(
            provider_id=self.provider_id,
            model_pack_id=self.MODEL_PACK_ID,
        )

    def _check_dependencies(self) -> bool:
        """检查 sentence-transformers 是否可导入 + 模型是否已装。"""
        try:
            import sentence_transformers  # noqa: F401
        except ImportError:
            return False
        # 模型文件检查（本地 models/ai_editing/ 或 HF 缓存）
        return is_model_pack_installed(self.MODEL_PACK_ID) or self._is_in_hf_cache()

    def _is_in_hf_cache(self) -> bool:
        """模型是否在 HuggingFace 默认缓存里（已下载过）。

        sentence-transformers 第一次加载会下载到 ~/.cache/huggingface/hub/。
        粗判：缓存目录有 models--Qwen--Qwen3-Embedding-0.6B 子目录。
        """
        from pathlib import Path
        cache = Path.home() / ".cache" / "huggingface" / "hub"
        if not cache.exists():
            return False
        target = "models--Qwen--Qwen3-Embedding-0.6B"
        return (cache / target).is_dir()

    def _load_model(self) -> Any:
        """加载 sentence-transformers 模型。"""
        from sentence_transformers import SentenceTransformer
        logger.info("加载 Qwen3-Embedding 模型...")
        model = SentenceTransformer(self._resolve_local_or_hf(self.MODEL_HF_ID))
        logger.info("Qwen3-Embedding 加载完成，维度=%d",
                    model.get_sentence_embedding_dimension())
        return model

    def embed_text(self, text: str) -> EmbeddingResult:
        """单条文本转向量。"""
        if self.model is None:
            raise RuntimeError(f"provider {self.provider_id} 模型未加载")
        vec = self.model.encode(text, normalize_embeddings=True)
        return EmbeddingResult(
            vector=_to_float_list(vec), dim=_vec_dim(vec), model=self.provider_id,
        )

    def embed_texts(self, texts: list[str]) -> list[EmbeddingResult]:
        """批量文本转向量（sentence-transformers 内部批处理加速）。"""
        if self.model is None:
            raise RuntimeError(f"provider {self.provider_id} 模型未加载")
        vecs = self.model.encode(texts, normalize_embeddings=True, batch_size=32)
        return [
            EmbeddingResult(vector=_to_float_list(v), dim=_vec_dim(v), model=self.provider_id)
            for v in vecs
        ]


# ---------------------------------------------------------------------- #
# 备选：BGE-M3（sentence-transformers 也支持）
# ---------------------------------------------------------------------- #

class BGEM3EmbeddingProvider(LazyModelLoader):
    """BGE-M3 备选 provider（能力同 transcript_embed，作为 fallback）。

    Qwen3-Embedding 不可用时的备选。同等参数量下精度略低，
    但生态成熟、下载更稳定。
    """

    provider_id = "bge_m3"
    capability = Capability.TRANSCRIPT_EMBED

    MODEL_HF_ID = "BAAI/bge-m3"
    MODEL_PACK_ID = "bge_m3"
    DIM = 1024

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

    def embed_text(self, text: str) -> EmbeddingResult:
        if self.model is None:
            raise RuntimeError(f"provider {self.provider_id} 模型未加载")
        vec = self.model.encode(text, normalize_embeddings=True)
        return EmbeddingResult(
            vector=_to_float_list(vec), dim=_vec_dim(vec), model=self.provider_id,
        )

    def embed_texts(self, texts: list[str]) -> list[EmbeddingResult]:
        if self.model is None:
            raise RuntimeError(f"provider {self.provider_id} 模型未加载")
        vecs = self.model.encode(texts, normalize_embeddings=True, batch_size=32)
        return [
            EmbeddingResult(vector=_to_float_list(v), dim=_vec_dim(v), model=self.provider_id)
            for v in vecs
        ]


# ---------------------------------------------------------------------- #
# 注册（真实 provider 覆盖 stub 默认，但只有 is_available 时才生效）
# ---------------------------------------------------------------------- #

def register_real_transcript_embed() -> None:
    """注册真实的 transcript_embed provider。

    在 vault 应用启动时调用。真实 provider 注册后成为默认（is_default=True），
    但只有 is_available()=True 时才会被选中——否则 ProviderConfig.select
    会 fallback 到第一个可用的（即 stub）。
    """
    reg = get_registry()
    if reg.get("qwen3_embedding_0_6b") is None:
        reg.register(
            Qwen3EmbeddingProvider(),
            ProviderDescriptor(
                provider_id="qwen3_embedding_0_6b",
                capability=Capability.TRANSCRIPT_EMBED,
                display_name="Qwen3-Embedding 0.6B（1024维）",
                requires_gpu=False, min_vram_mb=0,
                model_pack_id="qwen3_embedding_0_6b",
                notes="默认台词向量模型。比 BGE-M3 强 7.9%。CPU 也能跑。",
            ),
            is_default=True,
        )
    if reg.get("bge_m3") is None:
        reg.register(
            BGEM3EmbeddingProvider(),
            ProviderDescriptor(
                provider_id="bge_m3",
                capability=Capability.TRANSCRIPT_EMBED,
                display_name="BGE-M3（1024维，备选）",
                requires_gpu=False, min_vram_mb=0,
                model_pack_id="bge_m3",
                notes="备选。生态成熟，下载稳定。",
            ),
            is_default=False,
        )
