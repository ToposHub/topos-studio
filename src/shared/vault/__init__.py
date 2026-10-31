"""vault 与主项目的共享层。

主项目（src/）通过本包与 vault（apps/topos_vault/）交互：
    - client.py：只读 HTTP 客户端（连 vault 查询服务）
    - bridge.py：TAEP 桥接（vault 检索结果 → SemanticSegment，喂给 AI 剪辑 Executor）

本包是 vault 与主项目之间**唯一**的耦合点，保持两边可独立演进。
"""

from src.shared.vault.client import VaultClient, VaultStats
from src.shared.vault.bridge import (
    clip_to_semantic_segment,
    face_track_to_segments,
    transcript_line_to_segment,
    search_hit_to_segments,
)

__all__ = [
    "VaultClient",
    "VaultStats",
    "clip_to_semantic_segment",
    "face_track_to_segments",
    "transcript_line_to_segment",
    "search_hit_to_segments",
]
