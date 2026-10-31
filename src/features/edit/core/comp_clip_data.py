"""
Comp Clip Data Model - Comp Clip as first-class media pool asset.

Comp Clip 是媒体池中的一等素材，代表一个可剪辑的合成片段。
它的画面来自 Composite node graph 的输出，不一定有真实磁盘源文件。

Comp Clip 可以有两种来源：
1. 从时间线普通 clip 升级而来 (timeline_clip_upgrade)
2. 在媒体池中新建空白合成片段 (empty_comp_clip)
3. 图片进入 Composite 后生成 (image_comp_clip)

设计原则：
- 不可变数据类（frozen=True）
- 完整类型注解
- 支持序列化/反序列化
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict
import uuid
import time


# 允许的 source_kind 类型
SOURCE_KIND_TIMELINE_CLIP_UPGRADE = "timeline_clip_upgrade"
SOURCE_KIND_EMPTY_COMP_CLIP = "empty_comp_clip"
SOURCE_KIND_IMAGE_COMP_CLIP = "image_comp_clip"
SOURCE_KIND_EXTERNAL_MEDIA_NODE = "external_media_node"

VALID_SOURCE_KINDS = (
    SOURCE_KIND_TIMELINE_CLIP_UPGRADE,
    SOURCE_KIND_EMPTY_COMP_CLIP,
    SOURCE_KIND_IMAGE_COMP_CLIP,
    SOURCE_KIND_EXTERNAL_MEDIA_NODE,
)


@dataclass(frozen=True)
class CompClipData:
    """
    Comp Clip 数据模型。

    Attributes:
        comp_clip_id: Comp Clip 唯一标识符
        asset_id: 对应的 MediaAsset ID（media_type="comp_clip"）
        graph_id: 对应的 CompositeGraphData composite_id
        name: 显示名称
        width: 合成输出宽度
        height: 合成输出高度
        fps: 帧率
        duration_frames: 总帧数
        source_kind: 来源类型
        original_asset_id: 原始素材 ID（从 clip 升级时记录）
        original_timeline_id: 原始时间线 ID
        original_clip_id: 原始 clip ID
        created_at: 创建时间戳
        modified_at: 最后修改时间戳
    """
    comp_clip_id: str
    asset_id: str
    graph_id: str
    name: str
    width: int = 1920
    height: int = 1080
    fps: float = 24.0
    duration_frames: int = 240
    source_kind: str = SOURCE_KIND_EMPTY_COMP_CLIP
    original_asset_id: str | None = None
    original_timeline_id: str | None = None
    original_clip_id: str | None = None
    created_at: float = 0.0
    modified_at: float = 0.0

    def __post_init__(self) -> None:
        """验证数据有效性"""
        if not self.comp_clip_id:
            raise ValueError("comp_clip_id cannot be empty")
        if not self.asset_id:
            raise ValueError("asset_id cannot be empty")
        if not self.graph_id:
            raise ValueError("graph_id cannot be empty")
        if not self.name:
            raise ValueError("name cannot be empty")
        if self.source_kind not in VALID_SOURCE_KINDS:
            raise ValueError(f"Invalid source_kind: {self.source_kind}")
        if self.width <= 0 or self.height <= 0:
            raise ValueError(f"Invalid resolution: {self.width}x{self.height}")
        if self.fps <= 0:
            raise ValueError(f"Invalid fps: {self.fps}")
        if self.duration_frames < 0:
            raise ValueError(f"duration_frames cannot be negative: {self.duration_frames}")

    # ==================== 工厂方法 ====================

    @staticmethod
    def create_empty(
        asset_id: str,
        graph_id: str,
        name: str = "Comp Clip",
        width: int = 1920,
        height: int = 1080,
        fps: float = 24.0,
        duration_frames: int = 240,
    ) -> CompClipData:
        """创建空白 Comp Clip"""
        now = time.time()
        return CompClipData(
            comp_clip_id=str(uuid.uuid4()),
            asset_id=asset_id,
            graph_id=graph_id,
            name=name,
            width=width,
            height=height,
            fps=fps,
            duration_frames=duration_frames,
            source_kind=SOURCE_KIND_EMPTY_COMP_CLIP,
            created_at=now,
            modified_at=now,
        )

    @staticmethod
    def create_from_timeline_clip(
        asset_id: str,
        graph_id: str,
        name: str,
        width: int,
        height: int,
        fps: float,
        duration_frames: int,
        original_asset_id: str,
        original_timeline_id: str,
        original_clip_id: str,
    ) -> CompClipData:
        """从时间线 clip 升级创建 Comp Clip"""
        now = time.time()
        return CompClipData(
            comp_clip_id=str(uuid.uuid4()),
            asset_id=asset_id,
            graph_id=graph_id,
            name=name,
            width=width,
            height=height,
            fps=fps,
            duration_frames=duration_frames,
            source_kind=SOURCE_KIND_TIMELINE_CLIP_UPGRADE,
            original_asset_id=original_asset_id,
            original_timeline_id=original_timeline_id,
            original_clip_id=original_clip_id,
            created_at=now,
            modified_at=now,
        )

    @staticmethod
    def create_from_image(
        asset_id: str,
        graph_id: str,
        name: str,
        width: int,
        height: int,
        fps: float,
        duration_frames: int,
        original_asset_id: str,
        original_clip_id: str | None = None,
        original_timeline_id: str | None = None,
    ) -> CompClipData:
        """从图片创建 Comp Clip"""
        now = time.time()
        return CompClipData(
            comp_clip_id=str(uuid.uuid4()),
            asset_id=asset_id,
            graph_id=graph_id,
            name=name,
            width=width,
            height=height,
            fps=fps,
            duration_frames=duration_frames,
            source_kind=SOURCE_KIND_IMAGE_COMP_CLIP,
            original_asset_id=original_asset_id,
            original_timeline_id=original_timeline_id,
            original_clip_id=original_clip_id,
            created_at=now,
            modified_at=now,
        )

    # ==================== with_* 方法 ====================

    def with_name(self, name: str) -> CompClipData:
        """创建新实例，修改名称"""
        return CompClipData(
            comp_clip_id=self.comp_clip_id,
            asset_id=self.asset_id,
            graph_id=self.graph_id,
            name=name,
            width=self.width,
            height=self.height,
            fps=self.fps,
            duration_frames=self.duration_frames,
            source_kind=self.source_kind,
            original_asset_id=self.original_asset_id,
            original_timeline_id=self.original_timeline_id,
            original_clip_id=self.original_clip_id,
            created_at=self.created_at,
            modified_at=time.time(),
        )

    def with_duration(self, duration_frames: int) -> CompClipData:
        """创建新实例，修改时长"""
        return CompClipData(
            comp_clip_id=self.comp_clip_id,
            asset_id=self.asset_id,
            graph_id=self.graph_id,
            name=self.name,
            width=self.width,
            height=self.height,
            fps=self.fps,
            duration_frames=duration_frames,
            source_kind=self.source_kind,
            original_asset_id=self.original_asset_id,
            original_timeline_id=self.original_timeline_id,
            original_clip_id=self.original_clip_id,
            created_at=self.created_at,
            modified_at=time.time(),
        )

    def with_graph_id(self, graph_id: str) -> CompClipData:
        """创建新实例，修改关联 graph ID"""
        return CompClipData(
            comp_clip_id=self.comp_clip_id,
            asset_id=self.asset_id,
            graph_id=graph_id,
            name=self.name,
            width=self.width,
            height=self.height,
            fps=self.fps,
            duration_frames=self.duration_frames,
            source_kind=self.source_kind,
            original_asset_id=self.original_asset_id,
            original_timeline_id=self.original_timeline_id,
            original_clip_id=self.original_clip_id,
            created_at=self.created_at,
            modified_at=time.time(),
        )

    # ==================== 序列化 ====================

    def to_dict(self) -> Dict[str, Any]:
        """序列化为字典"""
        return {
            "comp_clip_id": self.comp_clip_id,
            "asset_id": self.asset_id,
            "graph_id": self.graph_id,
            "name": self.name,
            "width": self.width,
            "height": self.height,
            "fps": self.fps,
            "duration_frames": self.duration_frames,
            "source_kind": self.source_kind,
            "original_asset_id": self.original_asset_id,
            "original_timeline_id": self.original_timeline_id,
            "original_clip_id": self.original_clip_id,
            "created_at": self.created_at,
            "modified_at": self.modified_at,
        }

    @staticmethod
    def from_dict(data: Dict[str, Any]) -> CompClipData:
        """从字典反序列化"""
        return CompClipData(
            comp_clip_id=data.get("comp_clip_id") or str(uuid.uuid4()),
            asset_id=data.get("asset_id") or str(uuid.uuid4()),
            graph_id=data.get("graph_id") or str(uuid.uuid4()),
            name=data.get("name", "Comp Clip"),
            width=data.get("width", 1920),
            height=data.get("height", 1080),
            fps=data.get("fps", 24.0),
            duration_frames=data.get("duration_frames", 240),
            source_kind=data.get("source_kind", SOURCE_KIND_EMPTY_COMP_CLIP),
            original_asset_id=data.get("original_asset_id"),
            original_timeline_id=data.get("original_timeline_id"),
            original_clip_id=data.get("original_clip_id"),
            created_at=data.get("created_at", 0.0),
            modified_at=data.get("modified_at", 0.0),
        )

    # ==================== 辅助方法 ====================

    @property
    def is_from_timeline_clip(self) -> bool:
        """是否从时间线 clip 升级而来"""
        return self.source_kind == SOURCE_KIND_TIMELINE_CLIP_UPGRADE

    @property
    def is_empty(self) -> bool:
        """是否为空白 Comp Clip"""
        return self.source_kind == SOURCE_KIND_EMPTY_COMP_CLIP

    @property
    def is_from_image(self) -> bool:
        """是否从图片创建"""
        return self.source_kind == SOURCE_KIND_IMAGE_COMP_CLIP

    @property
    def resolution_str(self) -> str:
        """分辨率字符串"""
        return f"{self.width}x{self.height}"

    @property
    def duration_seconds(self) -> float:
        """时长（秒）"""
        if self.fps <= 0:
            return 0.0
        return self.duration_frames / self.fps

    def __hash__(self) -> int:
        return hash(self.comp_clip_id)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, CompClipData):
            return NotImplemented
        return self.comp_clip_id == other.comp_clip_id

    def __repr__(self) -> str:
        return (
            f"CompClipData(comp_clip_id={self.comp_clip_id!r}, "
            f"name={self.name!r}, "
            f"source_kind={self.source_kind!r}, "
            f"resolution={self.resolution_str}, "
            f"duration_frames={self.duration_frames})"
        )
