"""阶段 4.6 —— 节点缩略图的 Metal 评估服务（供 R08 评估器接线的契约实现）。

现有实现 ``src/app/controllers/node_thumbnail_evaluator.py``（GL 独立
上下文 + ``ColorGraphExecutor``）**保持不动**；本模块在 color 域提供可被
它调用的 Metal 路径（阶段 4 共享图入口 ``SharedColorGraphEntry.render_frame``
的低分辨率变体），并落实验收「缓存记录格式、后端和工作色彩语义」：

- :class:`ThumbnailCacheRecord`：缓存键 = (node_id, graph_revision,
  resource_fingerprint, source_identity, size, **backend**,
  **working_color**)——后端（metal_texture/cpu_compat）与工作色彩语义
  （working_mode/signal|linear + 输入 transfer/primaries）参与身份：
  换后端或改工作色彩语义的缓存**不互认**（不会把 GL 直通图当 Metal 线性
  图复用，反之亦然）；改上游节点 → 依赖节点的 resource_fingerprint
  变化自然失效；更换 LUT → resource_fingerprint 变化失效（无旧图）。
- :class:`MetalNodeThumbnailService`：demand 驱动、同步评估（调用方在
  worker 线程持有 Metal 上下文）；逐节点以 viewer 目标编译依赖锥计划
  （与 GL 评估器同一 ``freeze_graph(viewer_target=...)`` 契约），执行后
  读回小图 (H, W, 4) uint8（sRGB 展示编码在输出边界施加一次）。

待接线（本阶段不改 app 域文件）：``node_thumbnail_evaluator`` 的
``_evaluate_job`` 增加 backend 分支调用本服务；接线前生产路径仍走 GL。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional, Tuple

import numpy as np

from src.features.color.core.frame_color_description import ColorFrameDescription


@dataclass(frozen=True)
class ThumbnailCacheRecord:
    """一条缩略图缓存记录（键含后端与工作色彩语义）。"""

    node_id: str
    graph_revision: int
    resource_fingerprint: str
    source_identity: str
    size: Tuple[int, int]
    backend: str                       # metal_texture / cpu_compat
    working_color: str                 # 形如 "signal:bt709" / "linear:pq"

    def key(self) -> tuple:
        return (self.node_id, self.graph_revision, self.resource_fingerprint,
                self.source_identity, self.size, self.backend,
                self.working_color)


def working_color_identity(description: ColorFrameDescription,
                           working_mode: str) -> str:
    """工作色彩语义串（缓存身份的一部分；语义变则缓存失效）。"""
    return f"{working_mode}:{description.transfer}:{description.primaries}"


def display_encode_thumbnail(linear_or_signal: np.ndarray,
                             description: ColorFrameDescription,
                             working_mode: str) -> np.ndarray:
    """Thumbnail display encoding: ``(H, W, 3)`` float32 in [0, 1].

    The same order as ``MetalColorIO.encode_for_display``, applied once at
    the output boundary: a signal working mode passes through; linear with
    SDR goes linear -> sRGB; linear with HDR is tone-mapped to SDR first.
    """
    arr = np.asarray(linear_or_signal, dtype=np.float32)[..., :3]
    if working_mode == "linear":
        if description.is_hdr:
            from src.shared.color.color_transform_provider import (
                _linear_to_srgb_np,
                _tonemap_aces_np,
            )

            mapped = _tonemap_aces_np(arr, target_peak=100.0)
            return np.clip(_linear_to_srgb_np(mapped), 0.0, 1.0)
        from src.shared.color.color_transform_provider import (
            _linear_to_srgb_np,
        )

        return np.clip(_linear_to_srgb_np(arr), 0.0, 1.0)
    return np.clip(arr, 0.0, 1.0)


def _why(diagnostics: Tuple[str, ...]) -> str:
    """The diagnostic to show for a node without a picture.

    The resource preflight lists its notes first (a plugin that is not in
    the registry, ...); why the frame was not produced -- the domain the
    executors refused, a compile error -- is the more specific answer.
    """
    for item in diagnostics:
        if not item.startswith("resource:"):
            return item
    return diagnostics[0] if diagnostics else "not_published"


class MetalNodeThumbnailService:
    """节点输出缩略图的 Metal 评估（worker 线程独占；无 Qt）。

    与 GL 评估器（R08）同 demand 契约：输入图 payload + 低分辨率源 +
    节点 id 列表 → 每节点 (node_id, RGBA8 展示图) 或 (node_id, 错误码)。
    """

    def __init__(self, entry: Any, color_io: Any = None) -> None:
        """``entry``：``SharedColorGraphEntry``（Metal 纹理执行优先，
        ``run_plan_cpu`` 兼容回退——回退单元为一次完整图请求）。"""
        self._entry = entry
        self._io = color_io
        self.stats: Dict[str, int] = {"evaluated": 0, "failed": 0,
                                      "fallback_cpu": 0}

    def evaluate(
        self,
        graph: Any,
        source_rgba16f: np.ndarray,
        node_ids: Tuple[str, ...],
        *,
        identity: Tuple[str, str, int] = ("", "", 0),
        resource_fingerprint: str = "",
        source_identity: str = "",
        working_mode: str = "signal",
        input_description: Optional[ColorFrameDescription] = None,
        layer_param_provider: Optional[Callable[[str], Any]] = None,
    ) -> Dict[str, Tuple[Optional[np.ndarray], str]]:
        """Thumbnails of the given nodes (synchronously; the caller owns the context).

        Returns ``{node_id: (RGBA8 (H, W, 4) uint8 | None, backend | diagnostic)}``.
        失败显式返回错误码（不冒充、不用源图顶替——视图层已有源缩略图
        fallback）。
        """
        from src.features.color.core.color_graph_snapshot import freeze_graph  # noqa: F401 —— 契约同源（render_frame 内部同参冻结）

        height, width = source_rgba16f.shape[:2]
        if input_description is None:
            input_description = ColorFrameDescription(
                "rgba16f", width, height, bit_depth=16)
        results: Dict[str, Tuple[Optional[np.ndarray], str]] = {}
        for requested in node_ids:
            result = self._entry.render_frame(
                graph, 0, source_rgba16f,
                media_id=identity[1] or "metal-node-thumb",
                working_color_context=input_description.working_color_context(
                    working_mode),
                request_id=f"metal-thumb-{requested}",
                size=(width, height),
                viewer_target=f"viewer:{requested}")
            self.stats["evaluated"] += 1
            if not result.published or result.output is None:
                self.stats["failed"] += 1
                results[requested] = (None, _why(result.diagnostics))
                continue
            if result.backend != "metal_texture":
                self.stats["fallback_cpu"] += 1
            encoded = display_encode_thumbnail(
                result.output, input_description, working_mode)
            # An opaque RGBA8 thumbnail: node outputs are shown over the UI,
            # so the working alpha is not carried into the picture.
            rgb8 = (encoded * 255.0 + 0.5).astype(np.uint8)
            rgba8 = np.concatenate(
                [rgb8, np.full(rgb8.shape[:2] + (1,), 255, np.uint8)], axis=-1)
            results[requested] = (rgba8, result.backend)
        return results

    def cache_record(
        self, node_id: str, identity: Tuple[str, str, int],
        resource_fingerprint: str, source_identity: str,
        size: Tuple[int, int], backend: str,
        description: ColorFrameDescription, working_mode: str,
    ) -> ThumbnailCacheRecord:
        """构造缓存记录（键含后端/工作色彩语义——见模块 docstring）。"""
        return ThumbnailCacheRecord(
            node_id=node_id, graph_revision=int(identity[2]),
            resource_fingerprint=resource_fingerprint,
            source_identity=source_identity, size=tuple(size),
            backend=backend,
            working_color=working_color_identity(description, working_mode))


__all__ = [
    "MetalNodeThumbnailService",
    "ThumbnailCacheRecord",
    "display_encode_thumbnail",
    "working_color_identity",
]
