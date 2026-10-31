"""向量存储层——FAISS 优先，numpy 兜底。

四个独立索引（绝不合并，靠 media_id 关联）：
    transcript.faiss      台词向量（transcript_embed 能力产出）
    face.faiss            人脸向量（face_recognize 能力产出）
    visual.faiss          画面向量（visual_embed 能力产出，视频+图片共用）
    audio_semantic.faiss  音乐语义向量（audio_understand 能力产出）

设计要点：
- **faiss 优先**：装了 faiss-cpu 就用（快、省内存、支持 IVF 近似检索）
- **numpy 兜底**：faiss 没装时用 numpy 做暴力余弦相似度（慢但正确，CI/低配机可用）
- **降级透明**：上层调相同的 API，不知道底层是 faiss 还是 numpy
- **持久化**：faiss 写 .faiss 文件；numpy 写 .npy（向量）+ .json（id 映射）
- **懒加载**：索引按需加载（选定角色/能力后才加载对应索引）
"""

from apps.topos_vault.db.faiss_store import (
    VectorStore,
    VectorStoreFactory,
    SearchResult,
    get_vector_store,
)

__all__ = ["VectorStore", "VectorStoreFactory", "SearchResult", "get_vector_store"]
