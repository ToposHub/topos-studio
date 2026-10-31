"""
Composite Runtime Context - P3 Comp Clip integration.

为 Composite 节点求值提供运行时上下文信息。
TimelineClipInputNode 通过此上下文获取当前帧数据。

设计原则：
- frozen dataclass，不可变
- 包含足够的帧定位信息
- 不包含 GPU texture（texture 由 Provider 提供）
- clip_effects / caption_input 为边界只读快照（AUD-5210）：调用方传入
  副本/快照，消费方不得原地修改元素
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CompositeRuntimeContext:
    """
    Composite 运行时上下文。

    每次图求值时创建，携带当前帧的定位信息。
    Composite 节点通过此上下文获取正确的帧数据。

    Attributes:
        project_id: 项目 ID
        graph_id: 当前求值的图 ID
        comp_clip_id: 关联的 Comp Clip ID（可选）
        timeline_id: 来源时间线 ID（可选）
        clip_id: 来源 clip ID（可选）
        asset_id: 来源素材 ID（可选）
        timeline_frame: 时间线帧号
        clip_local_frame: clip 本地帧号（相对于 clip 起始）
        source_frame: 素材帧号（经 speed 映射后）
        fps: 帧率
        width: 输出宽度
        height: 输出高度
        quality: 渲染质量 (preview, proxy, master)
        input_stage: 输入阶段 (source_raw, after_color, after_clip_effects)
        duration_frames: 片段实际帧数（用于 transport bar 总帧数显示）
        clip_timeline_start: 片段在时间线上的起始帧（用于全局帧↔clip本地帧映射）
    """
    project_id: str = ""
    graph_id: str = ""
    comp_clip_id: str | None = None
    timeline_id: str | None = None
    clip_id: str | None = None
    asset_id: str | None = None
    timeline_frame: int = 0
    clip_local_frame: int = 0
    source_frame: int = 0
    fps: float = 24.0
    width: int = 1920
    height: int = 1080
    quality: str = "preview"
    input_stage: str = "source_raw"
    duration_frames: int = 0
    clip_timeline_start: int = 0
    # Phase 4: the clip's EffectInstance chain, supplied when
    # input_stage == "after_clip_effects" so the provider can run them
    # on the after_color texture. Empty tuple by default (no effects).
    #
    # AUD-5210 ownership contract: the context is created once and then
    # shared across threads, so the caller MUST pass a snapshot of the
    # effect chain (new effect objects or copies), and every consumer MUST
    # treat the elements as read-only for the lifetime of this context.
    # The context itself only normalises the container to a tuple — it
    # deliberately does NOT deep-copy the elements, because EffectInstance
    # may reference non-copyable GPU-backed state.
    clip_effects: tuple = ()
    # Read-only-at-the-boundary caption snapshot supplied by the Edit timeline.
    caption_input: object | None = None

    def __post_init__(self) -> None:
        # AUD-5210: guarantee the effects chain is stored as an immutable
        # tuple even when a caller hands in a list, so the frozen context
        # never carries a mutable container.
        if not isinstance(self.clip_effects, tuple):
            object.__setattr__(
                self, "clip_effects", tuple(self.clip_effects or ()))

    @staticmethod
    def for_timeline_clip(
        project_id: str,
        graph_id: str,
        timeline_id: str,
        clip_id: str,
        timeline_frame: int,
        clip_local_frame: int,
        source_frame: int,
        asset_id: str | None = None,
        fps: float = 24.0,
        width: int = 1920,
        height: int = 1080,
        quality: str = "preview",
        comp_clip_id: str | None = None,
        input_stage: str = "source_raw",
        clip_effects: tuple = (),
        duration_frames: int = 0,
        clip_timeline_start: int = 0,
        caption_input: object | None = None,
    ) -> CompositeRuntimeContext:
        """Create context for timeline clip composite evaluation."""
        return CompositeRuntimeContext(
            project_id=project_id,
            graph_id=graph_id,
            comp_clip_id=comp_clip_id,
            timeline_id=timeline_id,
            clip_id=clip_id,
            asset_id=asset_id,
            timeline_frame=timeline_frame,
            clip_local_frame=clip_local_frame,
            source_frame=source_frame,
            fps=fps,
            width=width,
            height=height,
            quality=quality,
            input_stage=input_stage,
            clip_effects=clip_effects,
            duration_frames=duration_frames,
            clip_timeline_start=clip_timeline_start,
            caption_input=caption_input,
        )

    @staticmethod
    def for_empty_comp_clip(
        project_id: str,
        graph_id: str,
        comp_clip_id: str,
        timeline_frame: int = 0,
        fps: float = 24.0,
        width: int = 1920,
        height: int = 1080,
        quality: str = "preview",
    ) -> CompositeRuntimeContext:
        """Create context for empty comp clip (no timeline source)."""
        return CompositeRuntimeContext(
            project_id=project_id,
            graph_id=graph_id,
            comp_clip_id=comp_clip_id,
            timeline_frame=timeline_frame,
            fps=fps,
            width=width,
            height=height,
            quality=quality,
        )

    def __repr__(self) -> str:
        return (
            f"CompositeRuntimeContext("
            f"graph={self.graph_id[:8]}, "
            f"frame={self.timeline_frame}, "
            f"quality={self.quality})"
        )
