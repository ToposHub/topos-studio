"""阶段 5.6 · Metal 合成导出桥（多 pass / 图像序列 / 无头输出）。

把 ``composite_export_renderer.py`` 承担的三类输出接到**同一份 Metal GPU
计划**（``graph_plan.build_execution_plan``，与预览共用），仍在
``TOPOS_COMPOSITE_METAL=1`` 之后：

- **多 pass**（:meth:`MetalCompositeExportBridge.render_passes`）：
  ``beauty`` 走 Metal 链；**AOV pass（depth/normal/object_id/motion
  等）没有 Metal 产出**——Scene3D 系节点在能力表是未迁移（阶段 5.5 只
  完成了矩阵/裁剪空间/深度约定的验证 pass，未接入图链），因此每个请求
  的 AOV pass 返回显式回退诊断，调用方回退 GL 导出路径（GL 的
  ``render_and_readback_passes`` 才有 AOV 纹理）。绝不输出「beauty 冒充
  AOV」或空数据。
- **图像序列 / 无头输出**（:meth:`MetalCompositeExportBridge.render_sequence`）：
  逐帧走 ``renderer.render_async`` + 有界等待（驱动 ``poll_completions``，
  不 ``waitUntilCompleted``）；段边界先重置时域历史（阶段 5.4）；支持
  协作式取消——取消后不再发起新帧，在途帧完成即回收，不产生半成品。
- **缓存**：与预览共用渲染器的 ``MetalTileCache``（内容寻址键含帧号，
  分段导出与整段导出对相同输入得到相同像素）。

输出是场景线性 RGBA float32（与 GL 导出的 ``linear_f32`` 同域；BGR 编码
和目标色彩变换属于既有导出链（``composite_render_output``），不在本桥
重复）。帧键使用合成局部帧号（``timeline_frame``）。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, Iterable, Optional, Tuple

import numpy as np

logger = logging.getLogger(__name__)

# 完成等待预算（秒）：与渲染器的完成超时一致；超时按帧显式失败。
FRAME_WAIT_TIMEOUT_S = 10.0


def _metal_export_enabled() -> Tuple[bool, str]:
    """导出桥的启用判定（与预览同一开关族，默认关闭）。"""
    from src.features.composite.render.composite_graph_render_service import (
        composite_metal_enabled,
    )
    if not composite_metal_enabled():
        return False, "TOPOS_COMPOSITE_METAL 未启用"
    from src.shared.gpu.metal import MetalComputeContext
    if not MetalComputeContext.is_supported():
        return False, "Metal compute 不可用"
    return True, ""


@dataclass
class MetalPassesResult:
    """一次多 pass 请求的结果（beauty + 各 pass 的成败）。"""

    frame: int = 0
    # pass 名 → 场景线性 RGBA float32（h, w, 4）。
    passes: Dict[str, np.ndarray] = field(default_factory=dict)
    # pass 名 → 显式回退诊断（调用方对该 pass 回退 GL 路径）。
    fallbacks: Dict[str, str] = field(default_factory=dict)
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error and bool(self.passes)


@dataclass
class MetalSequenceResult:
    """一次序列渲染的结果。"""

    # 帧号 → 场景线性 RGBA float32（h, w, 4）。
    frames: Dict[int, np.ndarray] = field(default_factory=dict)
    cancelled: bool = False
    error: str = ""
    elapsed_ms: float = 0.0

    @property
    def ok(self) -> bool:
        return not self.error and not self.cancelled and bool(self.frames)


class MetalCompositeExportBridge:
    """Metal 合成链的无头导出入口（预览/导出共用同一 GPU 计划）。"""

    def __init__(self, renderer=None) -> None:
        self._renderer = renderer

    @property
    def renderer(self):
        return self._renderer

    @property
    def available(self) -> bool:
        """开关与本机设备判定（未启用时调用方走既有 GL/CPU 路径）。"""
        ok, _ = _metal_export_enabled()
        return ok

    @property
    def unavailable_reason(self) -> str:
        _, reason = _metal_export_enabled()
        return reason

    def _ensure_renderer(self):
        if self._renderer is None:
            from .renderer import MetalCompositeRenderer
            self._renderer = MetalCompositeRenderer()
        return self._renderer

    def cleanup(self) -> None:
        if self._renderer is not None:
            self._renderer.cleanup()
            self._renderer = None

    # ── 多 pass ──

    def render_passes(
        self,
        graph_data: dict,
        timeline_frame: int = 0,
        passes: Iterable[str] = ("beauty",),
        *,
        tile_size: Optional[Tuple[int, int]] = None,
        segment_id: str = "export",
        params_resolved: bool = False,
        trace_id: str = "",
    ) -> MetalPassesResult:
        """一次求值读回多个 pass：beauty 走 Metal，AOV 显式回退。"""
        result = MetalPassesResult(frame=int(timeline_frame))
        requested = [str(name) for name in passes]
        if not requested:
            result.error = "未请求任何 pass"
            return result
        if "beauty" not in requested:
            # beauty 之外的 pass 全部是 AOV：直接给出显式回退，不做任何
            # GPU 工作（Scene3D/AOV 未迁移，见模块 docstring）。
            for name in requested:
                result.fallbacks[name] = _aov_fallback_reason(name)
            return result
        if any(name != "beauty" for name in requested):
            for name in requested:
                if name != "beauty":
                    result.fallbacks[name] = _aov_fallback_reason(name)

        renderer = self._ensure_renderer()
        pending = renderer.render_async(
            graph_data, timeline_frame=timeline_frame,
            tile_size=tile_size, use_cache=True, trace_id=trace_id,
            segment_id=segment_id, params_resolved=params_resolved)
        from src.shared.gpu.composite_gpu_frame_handle import (
            CompositeGpuFrameHandle,
        )
        if isinstance(pending, CompositeGpuFrameHandle):
            handle = pending
        else:
            handle = pending.wait(timeout_s=FRAME_WAIT_TIMEOUT_S)
        if not handle.available:
            result.error = handle.fallback_reason
            return result
        try:
            result.passes["beauty"] = renderer.readback(handle)
        finally:
            handle.release()
        return result

    # ── 图像序列 / 无头输出 ──

    def render_sequence(
        self,
        graph_data: dict,
        frame_range: Iterable[int],
        *,
        tile_size: Optional[Tuple[int, int]] = None,
        segment_id: str = "export",
        params_resolved: bool = False,
        should_cancel: Optional[Callable[[], bool]] = None,
        on_frame: Optional[Callable[[int, np.ndarray], None]] = None,
        use_cache: bool = True,
    ) -> MetalSequenceResult:
        """逐帧渲染一个片段（图像序列/无头视频帧源）。

        - 段边界先重置时域历史（阶段 5.4：分段导出前重置）。
        - 协作式取消：``should_cancel`` 返回真后不再发起新帧；在途帧完成
          即回收，不产生半成品，``cancelled=True`` 返回。
        - 同一输入序列与整段导出得到相同像素（缓存内容寻址）。
        """
        from src.shared.gpu.composite_gpu_frame_handle import (
            CompositeGpuFrameHandle,
        )

        frames_list = [int(f) for f in frame_range]
        result = MetalSequenceResult()
        started = time.monotonic()
        if not frames_list:
            result.error = "帧范围为空"
            return result
        renderer = self._ensure_renderer()
        # 阶段 5.4：分段导出前重置时域历史（seek/段边界同规则）。
        renderer_history = getattr(renderer, "_history", None)
        if renderer_history is not None:
            renderer_history.reset("分段导出段边界")
        for frame in frames_list:
            if should_cancel is not None and should_cancel():
                result.cancelled = True
                break
            pending = renderer.render_async(
                graph_data, timeline_frame=frame, tile_size=tile_size,
                use_cache=use_cache, segment_id=segment_id,
                params_resolved=params_resolved,
                trace_id=f"metal-export:{segment_id}:{frame}")
            if isinstance(pending, CompositeGpuFrameHandle):
                handle = pending
            else:
                handle = pending.wait(timeout_s=FRAME_WAIT_TIMEOUT_S)
            if not handle.available:
                result.error = handle.fallback_reason
                break
            try:
                pixels = renderer.readback(handle)
            finally:
                handle.release()
            result.frames[frame] = pixels
            if on_frame is not None:
                on_frame(frame, pixels)
        result.elapsed_ms = (time.monotonic() - started) * 1000.0
        return result


def _aov_fallback_reason(pass_name: str) -> str:
    from .node_support import METAL_3D_NODE_TYPES
    return (
        f"AOV pass {pass_name!r} 无 Metal 产出：Scene3D 系节点"
        f"（{sorted(METAL_3D_NODE_TYPES)}）未迁移到 Metal 链"
        "（阶段 5.5 仅完成矩阵/裁剪空间/深度约定的验证 pass），"
        "该 pass 回退 GL 导出路径（render_and_readback_passes）")
