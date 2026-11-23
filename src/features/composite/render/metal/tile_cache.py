"""Metal 合成链的结果缓存（阶段 5.3 缓存键纪律）。

缓存键必须包含（计划原文）：**工程修订、帧、节点端口、尺寸、色彩描述**。
本文再加 tile 区域与参数哈希——任何会改变像素的输入都进键：

.. code-block:: text

    key = sha256( renderer_version
                | graph_revision      # 工程修订
                | frame               # 帧号
                | node_id : port      # 节点端口
                | width × height      # 尺寸
                | tile rect           # 分块区域
                | color_space, alpha_premultiplied, encode_srgb   # 色彩描述
                | params_digest )     # 全图参数内容哈希

缓存值为 numpy float16 tile（命中后重新上传 GPU），同样按字节计量在
``MetalTexturePool`` 之外单独记账，超预算 LRU 驱逐。命中与冷渲染的像素
一致性由测试保证（同键必同像素；键不同必不命中）。
"""

from __future__ import annotations

import hashlib
import threading
from collections import OrderedDict
from dataclasses import dataclass
from typing import Optional, Tuple

import numpy as np

# 渲染器实现变化（影响像素）时必须递增，否则命中旧实现的结果。
# v2（阶段 5.6）：constant gradient/radial 改为像素中心采样（对齐 GL 参考，
# 修正半像素偏差）。
METAL_COMPOSITE_RENDERER_VERSION = 2

DEFAULT_CACHE_BUDGET_BYTES = 256 * 1024 * 1024


def composite_cache_key(
    *,
    graph_revision: int,
    frame: int,
    node_id: str,
    port: str,
    width: int,
    height: int,
    tile_rect: Tuple[int, int, int, int],
    color_space: str,
    alpha_premultiplied: bool,
    encode_srgb: bool,
    params_digest: str,
) -> str:
    """构造内容寻址缓存键（工程修订/帧/端口/尺寸/色彩描述/参数）。"""
    parts = [
        f"v{METAL_COMPOSITE_RENDERER_VERSION}",
        f"rev={int(graph_revision)}",
        f"frame={int(frame)}",
        f"port={node_id}:{port}",
        f"size={int(width)}x{int(height)}",
        f"tile={tile_rect}",
        f"cs={color_space or 'unknown'}",
        f"premult={int(bool(alpha_premultiplied))}",
        f"srgb={int(bool(encode_srgb))}",
        f"params={params_digest}",
    ]
    digest = hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()
    return f"mcomp::{digest}"


def graph_params_digest(graph_data: dict) -> str:
    """全图参数内容哈希（节点参数 + 动画/表达式 + 连接拓扑的稳定序列化）。

    阶段 5.4：``keyframe_tracks`` / 已启用 ``expressions`` 一并进摘要——
    只差动画定义的两张图不得共享缓存键（预检会在未解析时回退；已解析
    接线后解析结果不同也各自成键）。
    """
    nodes = graph_data.get("nodes") or []
    connections = graph_data.get("connections") or []
    node_parts = []
    for nd in sorted(nodes, key=lambda n: str(n.get("id", ""))):
        params = nd.get("parameters") or {}
        items = sorted((str(k), repr(v)) for k, v in params.items())
        tracks = nd.get("keyframe_tracks") or {}
        track_items = sorted((str(k), repr(v)) for k, v in tracks.items())
        expressions = nd.get("expressions") or {}
        expr_items = sorted((str(k), repr(v)) for k, v in expressions.items()) \
            if bool(nd.get("expression_enabled", False)) else []
        node_parts.append(
            f"{nd.get('id', '')}|{nd.get('type', '')}|{items}"
            f"|kf:{track_items}|expr:{expr_items}")
    conn_parts = sorted(
        f"{c.get('src_node_id', '')}.{c.get('src_port_name', '')}->"
        f"{c.get('dst_node_id', '')}.{c.get('dst_port_name', '')}"
        for c in connections)
    payload = "\n".join(node_parts) + "\n#" + "\n".join(conn_parts)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]


@dataclass
class CachedTile:
    """一个缓存条目（float16 像素 + 键字段快照）。"""

    key: str
    pixels: np.ndarray            # (h, w, 4) float16
    bytes_size: int


class MetalTileCache:
    """键 → float16 tile 的 LRU 缓存（字节预算，线程安全）。"""

    def __init__(self, budget_bytes: int = DEFAULT_CACHE_BUDGET_BYTES) -> None:
        self._budget = int(budget_bytes)
        self._lock = threading.RLock()
        self._entries: "OrderedDict[str, CachedTile]" = OrderedDict()
        self._hits = 0
        self._misses = 0

    @property
    def budget_bytes(self) -> int:
        return self._budget

    @property
    def bytes_used(self) -> int:
        with self._lock:
            return sum(entry.bytes_size for entry in self._entries.values())

    @property
    def entry_count(self) -> int:
        with self._lock:
            return len(self._entries)

    def stats(self) -> Tuple[int, int]:
        with self._lock:
            return (self._hits, self._misses)

    def get(self, key: str) -> Optional[np.ndarray]:
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                self._misses += 1
                return None
            self._entries.move_to_end(key)
            self._hits += 1
            return entry.pixels

    def put(self, key: str, pixels: np.ndarray) -> None:
        data = np.ascontiguousarray(pixels, dtype=np.float16)
        size = int(data.nbytes)
        with self._lock:
            if size > self._budget:
                return
            while self.bytes_used + size > self._budget:
                self._entries.popitem(last=False)
            self._entries[key] = CachedTile(key=key, pixels=data,
                                            bytes_size=size)

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()

    def invalidate_revision(self, graph_revision: int) -> None:
        """保守失效：键里带修订号，正常无需调用；提供显式清空入口。"""
        self.clear()
