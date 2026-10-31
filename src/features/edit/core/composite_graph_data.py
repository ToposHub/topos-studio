"""
Composite Graph Data Model - P9.2 Edit → Composite Page Linkage.

存储从 Timeline Clip 创建的合成图数据。

设计原则:
- 不可变数据类（frozen=True）
- 完整类型注解
- 支持序列化/反序列化
- 与 NodeGraph 兼容

功能:
- 存储合成图的节点和连接数据
- 存储合成图的来源 clip 信息
- 支持与 CompositePage 的双向传递
- 完整保存 viewer_state、viewport_state、backdrops、sticky_notes、groups

序列化格式 (composite/graphs.json):
{
  "composite_id": "...",
  "name": "...",
  "nodes": [{id, type, position, parameters, disabled, expressions}],
  "connections": [{src_node_id, src_port_name, dst_node_id, dst_port_name}],
  "viewer_state": {zoom, pan_x, pan_y, viewing_node_id, ...},
  "viewport_state": {zoom, pan_x, pan_y, ...},
  "backdrops": {...},
  "sticky_notes": {...},
  "groups": {...},
  "width": 1920,
  "height": 1080,
  ...
}

注意：节点格式与 NodeEngine.to_dict() 保持一致：
- 使用 "id" 和 "type" (而非 "node_id" 和 "node_type")
- position 为列表 [x, y]
- 包含 expressions 字段
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
import uuid
import time
import logging

logger = logging.getLogger(__name__)


def _field_or_default(data: Dict[str, Any], key: str, default: Any) -> Any:
    """AUD-16305: JSON 显式 ``null`` 时 ``data.get(key, default)`` 返回 None
    而非默认值，None 会击穿下游迭代/比较（如 nodes=None 进
    ``_normalize_nodes_format`` 抛 TypeError、width=None 进 ``<= 0`` 比较
    抛 TypeError）。null 与缺键同等对待，回落默认值。
    """
    value = data.get(key)
    return default if value is None else value

# Private node parameter used to distinguish the composition background
# Constant from user-added Constant nodes.  Project Settings and the Comp Clip
# settings dialog both target this node, so changing the project background
# cannot unexpectedly recolor generator nodes used elsewhere in the graph.
PROJECT_BACKGROUND_PARAM = "_topos_project_background"
# Persisted Constant RGB channels use scene-linear values.  Kept alongside the
# background marker so graph factories and loaders share one canonical key.
CONSTANT_COLOR_SPACE_PARAM = "_topos_color_space"


@dataclass(frozen=True)
class CompositeGraphData:
    """
    合成图数据模型（P9.2 + P1 序列化补齐）。

    存储 Timeline Clip 关联的合成图数据。
    当 clip 设置了 composite_id，Composite 页面编辑此数据，
    输出作为 clip 的渲染源。

    Attributes:
        composite_id: 唯一标识符（UUID）
        source_clip_id: 来源 clip ID
        source_asset_id: 来源素材 ID（用于初始化 ImageInputNode）
        source_asset_path: 来源素材路径
        name: 合成名称
        nodes: 节点数据列表（NodeEngine 序列化格式）
        connections: 连接数据列表
        viewer_state: 合成查看器状态（zoom, pan, viewing_node_id）
        viewport_state: 节点编辑器视口状态（zoom, pan）
        backdrops: 背景框数据
        sticky_notes: 便签数据
        groups: 节点组数据
        created_at: 创建时间戳
        modified_at: 最后修改时间戳
        width: 合成输出宽度
        height: 合成输出高度
    """
    composite_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    source_clip_id: str = ""
    source_asset_id: str = ""
    source_asset_path: str = ""
    name: str = ""
    nodes: List[Dict[str, Any]] = field(default_factory=list)
    connections: List[Dict[str, Any]] = field(default_factory=list)
    # P1 序列化补齐：新增字段
    viewer_state: Dict[str, Any] = field(default_factory=dict)
    viewport_state: Dict[str, Any] = field(default_factory=dict)
    backdrops: Dict[str, Any] = field(default_factory=dict)
    sticky_notes: Dict[str, Any] = field(default_factory=dict)
    groups: Dict[str, Any] = field(default_factory=dict)
    created_at: float = 0.0
    modified_at: float = 0.0
    width: int = 1920
    height: int = 1080
    # Comp Clip 关联字段
    comp_clip_id: str | None = None
    comp_asset_id: str | None = None
    source_kind: str = "timeline_clip_upgrade"
    timeline_id: str | None = None
    clip_id: str | None = None
    source_refs: Dict[str, Any] = field(default_factory=dict)
    fps: float = 24.0
    duration_frames: int = 0

    def __post_init__(self) -> None:
        """验证数据有效性"""
        if not self.composite_id:
            raise ValueError("composite_id cannot be empty")
        # source_clip_id 和 source_asset_id 对空白 Comp Clip 以及已经从
        # Media Pool 脱钩的合成可以为空。脱钩图仍可通过 source_asset_path
        # 或其节点自己的 file_path 渲染，不应因为原项目 asset 被移除而
        # 在加载项目时失败。
        if self.source_kind not in ("empty_comp_clip", "detached_asset"):
            if not self.source_clip_id:
                raise ValueError("source_clip_id cannot be empty")
            if not self.source_asset_id:
                raise ValueError("source_asset_id cannot be empty")
        if self.width <= 0 or self.height <= 0:
            raise ValueError(f"Invalid resolution: {self.width}x{self.height}")

    # ==================== 工厂方法 ====================

    def _rebuild(
        self,
        *,
        name: Optional[str] = None,
        nodes: Optional[List[Dict[str, Any]]] = None,
        connections: Optional[List[Dict[str, Any]]] = None,
        viewer_state: Optional[Dict[str, Any]] = None,
        viewport_state: Optional[Dict[str, Any]] = None,
        backdrops: Optional[Dict[str, Any]] = None,
        sticky_notes: Optional[Dict[str, Any]] = None,
        groups: Optional[Dict[str, Any]] = None,
        width: Optional[int] = None,
        height: Optional[int] = None,
    ) -> CompositeGraphData:
        """派生新实例（全部 with_* 的统一重建入口，AUD-16304）。

        ``frozen=True`` 只阻止属性重绑定，不阻止容器内容突变。旧 with_*
        实现把 nodes/connections/viewer_state 等可变容器按引用传给新实例，
        新旧快照（undo 栈 / 缓存键 / 历史数据类实例）共享同一底层容器，
        任何一侧突变会"幽灵修改"全部快照。此入口对容器字段（含调用方
        显式传入的值）做 ``copy.deepcopy``，快照之间完全隔离，兑现模块
        docstring 的不可变契约。``modified_at`` 与旧 with_* 一律刷新。
        """
        return CompositeGraphData(
            composite_id=self.composite_id,
            source_clip_id=self.source_clip_id,
            source_asset_id=self.source_asset_id,
            source_asset_path=self.source_asset_path,
            name=self.name if name is None else name,
            nodes=copy.deepcopy(self.nodes if nodes is None else nodes),
            connections=copy.deepcopy(
                self.connections if connections is None else connections),
            viewer_state=copy.deepcopy(
                self.viewer_state if viewer_state is None else viewer_state),
            viewport_state=copy.deepcopy(
                self.viewport_state if viewport_state is None else viewport_state),
            backdrops=copy.deepcopy(
                self.backdrops if backdrops is None else backdrops),
            sticky_notes=copy.deepcopy(
                self.sticky_notes if sticky_notes is None else sticky_notes),
            groups=copy.deepcopy(self.groups if groups is None else groups),
            created_at=self.created_at,
            modified_at=time.time(),
            width=self.width if width is None else width,
            height=self.height if height is None else height,
            comp_clip_id=self.comp_clip_id,
            comp_asset_id=self.comp_asset_id,
            source_kind=self.source_kind,
            timeline_id=self.timeline_id,
            clip_id=self.clip_id,
            source_refs=copy.deepcopy(self.source_refs),
            fps=self.fps,
            duration_frames=self.duration_frames,
        )

    def with_name(self, name: str) -> CompositeGraphData:
        """创建新实例，修改名称"""
        return self._rebuild(name=name)

    def with_nodes(self, nodes: List[Dict[str, Any]]) -> CompositeGraphData:
        """创建新实例，更新节点数据"""
        return self._rebuild(nodes=nodes)

    def with_connections(self, connections: List[Dict[str, Any]]) -> CompositeGraphData:
        """创建新实例，更新连接数据"""
        return self._rebuild(connections=connections)

    def with_resolution(self, width: int, height: int) -> CompositeGraphData:
        """创建新实例，更新分辨率"""
        return self._rebuild(width=width, height=height)

    def with_graph_state(
        self,
        nodes: List[Dict[str, Any]],
        connections: List[Dict[str, Any]],
    ) -> CompositeGraphData:
        """创建新实例，更新完整图状态"""
        return self._rebuild(nodes=nodes, connections=connections)

    def with_viewer_state(self, viewer_state: Dict[str, Any]) -> CompositeGraphData:
        """创建新实例，更新查看器状态"""
        return self._rebuild(viewer_state=viewer_state)

    def with_viewport_state(self, viewport_state: Dict[str, Any]) -> CompositeGraphData:
        """创建新实例，更新节点编辑器视口状态"""
        return self._rebuild(viewport_state=viewport_state)

    def with_full_graph_data(
        self,
        nodes: Optional[List[Dict[str, Any]]] = None,
        connections: Optional[List[Dict[str, Any]]] = None,
        viewer_state: Optional[Dict[str, Any]] = None,
        viewport_state: Optional[Dict[str, Any]] = None,
        backdrops: Optional[Dict[str, Any]] = None,
        sticky_notes: Optional[Dict[str, Any]] = None,
        groups: Optional[Dict[str, Any]] = None,
    ) -> CompositeGraphData:
        """
        创建新实例，更新完整的图数据（从 NodeGraph.to_dict()）。

        用于从 CompositePage/NodeEngine 保存完整数据。

        Args:
            nodes: 节点列表（可选，None 表示保持原值）
            connections: 连接列表（可选）
            viewer_state: 查看器状态（可选）
            viewport_state: 视口状态（可选）
            backdrops: 背景框数据（可选）
            sticky_notes: 便签数据（可选）
            groups: 节点组数据（可选）

        Returns:
            新的 CompositeGraphData 实例
        """
        return self._rebuild(
            nodes=nodes,
            connections=connections,
            viewer_state=viewer_state,
            viewport_state=viewport_state,
            backdrops=backdrops,
            sticky_notes=sticky_notes,
            groups=groups,
        )

    # ==================== 序列化 ====================

    def to_dict(self) -> Dict[str, Any]:
        """
        序列化为字典（composite/graphs.json 格式）。

        格式与 NodeEngine.to_dict() 保持一致，确保完整保存：
        - nodes 使用 "id" 和 "type" 字段名
        - 包含 viewer_state、viewport_state
        - 包含 backdrops、sticky_notes、groups
        - 包含 Comp Clip 关联字段
        """
        d = {
            "composite_id": self.composite_id,
            "source_clip_id": self.source_clip_id,
            "source_asset_id": self.source_asset_id,
            "source_asset_path": self.source_asset_path,
            "name": self.name,
            "nodes": self.nodes,
            "connections": self.connections,
            # P1 序列化补齐：完整保存 UI 状态
            "viewer_state": self.viewer_state,
            "viewport_state": self.viewport_state,
            "backdrops": self.backdrops,
            "sticky_notes": self.sticky_notes,
            "groups": self.groups,
            "created_at": self.created_at,
            "modified_at": self.modified_at,
            "width": self.width,
            "height": self.height,
        }
        # Comp Clip 关联字段（仅在有值时包含）
        if self.comp_clip_id is not None:
            d["comp_clip_id"] = self.comp_clip_id
        if self.comp_asset_id is not None:
            d["comp_asset_id"] = self.comp_asset_id
        if self.source_kind != "timeline_clip_upgrade":
            d["source_kind"] = self.source_kind
        if self.timeline_id is not None:
            d["timeline_id"] = self.timeline_id
        if self.clip_id is not None:
            d["clip_id"] = self.clip_id
        if self.source_refs:
            d["source_refs"] = self.source_refs
        if self.fps != 24.0:
            d["fps"] = self.fps
        if self.duration_frames != 0:
            d["duration_frames"] = self.duration_frames
        return d

    @staticmethod
    def from_dict(data: Dict[str, Any]) -> CompositeGraphData:
        """
        从字典反序列化。

        支持向后兼容：
        - 旧格式节点（node_id/node_type）自动转换为新格式
        - 缺少的字段使用默认值
        - Comp Clip 关联字段向后兼容
        """
        # 节点格式转换：旧格式 node_id/node_type -> 新格式 id/type
        # AUD-16305: 各字段经 _field_or_default 兜底——显式 null 与缺键
        # 同等对待，nodes=None 不再进 _normalize_nodes_format 迭代。
        nodes = _field_or_default(data, "nodes", [])
        normalized_nodes = CompositeGraphData._normalize_nodes_format(nodes)
        source_refs = data.get("source_refs") or {}
        timeline_input_params: Dict[str, Any] = {}
        for node in normalized_nodes:
            if node.get("type") == "timeline_clip_input":
                params = node.get("parameters", {})
                if isinstance(params, dict):
                    timeline_input_params = params
                    break

        source_clip_id = (
            data.get("source_clip_id")
            or data.get("clip_id")
            or source_refs.get("original_clip_id")
            or timeline_input_params.get("clip_id")
            or ""
        )
        source_asset_id = (
            data.get("source_asset_id")
            or source_refs.get("original_asset_id")
            or timeline_input_params.get("asset_id")
            or ""
        )
        source_kind = data.get("source_kind", "timeline_clip_upgrade")
        if (
            source_kind not in ("empty_comp_clip", "detached_asset")
            and not source_clip_id
            and not source_asset_id
        ):
            has_timeline_input = any(n.get("type") == "timeline_clip_input" for n in normalized_nodes)
            if not has_timeline_input:
                source_kind = "empty_comp_clip"

        return CompositeGraphData(
            composite_id=data.get("composite_id") or str(uuid.uuid4()),
            source_clip_id=source_clip_id,
            source_asset_id=source_asset_id,
            source_asset_path=_field_or_default(data, "source_asset_path", ""),
            name=_field_or_default(data, "name", ""),
            nodes=normalized_nodes,
            connections=_field_or_default(data, "connections", []),
            # P1 序列化补齐：加载新增字段（向后兼容）
            viewer_state=_field_or_default(data, "viewer_state", {}),
            viewport_state=_field_or_default(data, "viewport_state", {}),
            backdrops=_field_or_default(data, "backdrops", {}),
            sticky_notes=_field_or_default(data, "sticky_notes", {}),
            groups=_field_or_default(data, "groups", {}),
            created_at=_field_or_default(data, "created_at", 0.0),
            modified_at=_field_or_default(data, "modified_at", 0.0),
            width=_field_or_default(data, "width", 1920),
            height=_field_or_default(data, "height", 1080),
            # Comp Clip 关联字段
            comp_clip_id=data.get("comp_clip_id"),
            comp_asset_id=data.get("comp_asset_id"),
            source_kind=source_kind,
            timeline_id=data.get("timeline_id") or source_refs.get("original_timeline_id") or timeline_input_params.get("timeline_id"),
            clip_id=data.get("clip_id") or source_clip_id,
            source_refs=source_refs,
            fps=_field_or_default(data, "fps", 24.0),
            duration_frames=_field_or_default(data, "duration_frames", 0),
        )

    @staticmethod
    def _normalize_nodes_format(nodes: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        规范化节点格式。

        将旧格式 (node_id/node_type) 转换为新格式 (id/type)，
        与 NodeEngine.to_dict() 保持一致。

        Args:
            nodes: 节点列表（可能是旧格式或新格式）

        Returns:
            规范化后的节点列表
        """
        normalized = []
        for node in nodes:
            normalized_node = dict(node)

            # 字段名转换：node_id/node_type -> id/type
            if "node_id" in normalized_node and "id" not in normalized_node:
                normalized_node["id"] = normalized_node.pop("node_id")
            if "node_type" in normalized_node and "type" not in normalized_node:
                normalized_node["type"] = normalized_node.pop("node_type")

            # position 格式转换：{x, y} -> [x, y]
            if "position" in normalized_node:
                pos = normalized_node["position"]
                if isinstance(pos, dict):
                    normalized_node["position"] = [pos.get("x", 0), pos.get("y", 0)]

            normalized.append(normalized_node)

        return normalized

    # ==================== 初始化默认图 ====================

    @staticmethod
    def create_default_for_clip(
        clip_id: str,
        asset_id: str,
        asset_path: str,
        width: int = 1920,
        height: int = 1080,
        name: str = "",
        composite_id: str | None = None,
        comp_clip_id: str | None = None,
        comp_asset_id: str | None = None,
        timeline_id: str | None = None,
        fps: float = 24.0,
        duration_frames: int = 0,
        color_stack_id: str | None = None,
    ) -> CompositeGraphData:
        """
        为 clip 创建默认合成图数据。

        默认图始终为 3 节点链：
        - TimelineClipInput -> Topos Color -> Output

        Topos Color 节点始终存在：
        - 有 color_stack_id 时：正常执行调色
        - 无 color_stack_id 时：bypass=True，直接透传图像

        Args:
            clip_id: 来源 clip ID
            asset_id: 来源素材 ID
            asset_path: 来源素材路径
            width: 输出宽度
            height: 输出高度
            name: 合成名称（默认为 clip 名称）
            composite_id: 指定 composite_id
            comp_clip_id: 关联的 CompClipData ID
            comp_asset_id: 关联的 Comp Clip MediaAsset ID
            timeline_id: 来源时间线 ID
            fps: 帧率
            duration_frames: 时长帧数
            color_stack_id: 调色栈 ID（有值时 Topos Color 正常执行，无值时透传）

        Returns:
            初始化的 CompositeGraphData
        """
        import time as _time

        actual_composite_id = composite_id or str(uuid.uuid4())

        # 创建默认 3 节点：TimelineClipInput -> Topos Color -> Output
        input_node_id = str(uuid.uuid4())[:8]
        topos_color_node_id = str(uuid.uuid4())[:8]
        output_node_id = str(uuid.uuid4())[:8]

        nodes = [
            {
                "id": input_node_id,
                "type": "timeline_clip_input",
                "position": [100, 200],
                "parameters": {
                    "timeline_id": timeline_id or "",
                    "clip_id": clip_id,
                    "asset_id": asset_id,
                    "input_stage": "source_raw",
                },
                "disabled": False,
            },
            {
                "id": topos_color_node_id,
                "type": "topos_color",
                "position": [400, 200],
                "parameters": {
                    "stack_source": "clip_stack",
                    "color_stack_id": color_stack_id or "",
                    "role": "primary_clip_grade",
                    "source_clip_id": clip_id,
                    "source_timeline_id": timeline_id or "",
                    "linked": True,
                    "preset_id": "",
                    "embedded_stack": None,
                    "bypass": not bool(color_stack_id),
                    "intensity": 1.0,
                },
                "disabled": False,
            },
            {
                "id": output_node_id,
                "type": "output",
                "position": [700, 200],
                # format_mode="custom" makes the Output node the authoritative
                # resolution source (AE-style composition container size),
                # independent of upstream node dimensions.
                "parameters": {
                    "format_mode": "custom",
                    "width": width,
                    "height": height,
                },
                "disabled": False,
            },
        ]

        # TimelineClipInput -> Topos Color -> Output
        connections = [
            {
                "src_node_id": input_node_id,
                "src_port_name": "output",
                "dst_node_id": topos_color_node_id,
                "dst_port_name": "image",
            },
            {
                "src_node_id": topos_color_node_id,
                "src_port_name": "result",
                "dst_node_id": output_node_id,
                "dst_port_name": "image",
            },
        ]

        return CompositeGraphData(
            composite_id=actual_composite_id,
            source_clip_id=clip_id,
            source_asset_id=asset_id,
            source_asset_path=asset_path,
            name=name or f"Composite_{clip_id[:8]}",
            nodes=nodes,
            connections=connections,
            viewer_state={},
            viewport_state={},
            backdrops={},
            sticky_notes={},
            groups={},
            created_at=_time.time(),
            modified_at=_time.time(),
            width=width,
            height=height,
            comp_clip_id=comp_clip_id,
            comp_asset_id=comp_asset_id,
            source_kind="timeline_clip_upgrade",
            timeline_id=timeline_id,
            clip_id=clip_id,
            source_refs={
                "original_asset_id": asset_id,
                "original_clip_id": clip_id,
            },
            fps=fps,
            duration_frames=duration_frames,
        )

    @staticmethod
    def create_default_empty_comp_clip(
        composite_id: str | None = None,
        comp_clip_id: str | None = None,
        comp_asset_id: str | None = None,
        name: str = "Comp Clip",
        width: int = 1920,
        height: int = 1080,
        fps: float = 24.0,
        duration_frames: int = 240,
        background_color: tuple[float, float, float, float] = (1.0, 1.0, 1.0, 1.0),
    ) -> CompositeGraphData:
        """
        为空白 Comp Clip 创建默认合成图数据。

        默认图：Constant → Output

        Args:
            composite_id: 指定 composite_id
            comp_clip_id: 关联的 CompClipData ID
            comp_asset_id: 关联的 Comp Clip MediaAsset ID
            name: 合成名称
            width: 输出宽度
            height: 输出高度
            fps: 帧率
            duration_frames: 时长帧数
            background_color: Constant 节点默认背景色 (R, G, B, A)，
                              各通道 0.0~1.0，默认不透明白色。

        Returns:
            初始化的 CompositeGraphData
        """
        import time as _time

        actual_composite_id = composite_id or str(uuid.uuid4())

        # 创建空白 Comp Clip 默认节点
        constant_node_id = str(uuid.uuid4())[:8]
        output_node_id = str(uuid.uuid4())[:8]

        bg_r, bg_g, bg_b, bg_a = background_color
        nodes = [
            {
                "id": constant_node_id,
                "type": "constant",
                "position": [100, 200],
                "parameters": {
                    # ConstantNode 读取的是分通道参数 color_r/g/b/a
                    # （见 constant_node.py），不是 color 列表。
                    "mode": "solid",
                    "color_r": float(bg_r),
                    "color_g": float(bg_g),
                    "color_b": float(bg_b),
                    "color_a": float(bg_a),
                    PROJECT_BACKGROUND_PARAM: True,
                    CONSTANT_COLOR_SPACE_PARAM: "linear",
                    "width": width,
                    "height": height,
                },
                "disabled": False,
            },
            {
                "id": output_node_id,
                "type": "output",
                "position": [400, 200],
                # format_mode="custom" makes the Output node the authoritative
                # composition resolution; the Constant node acts purely as the
                # background layer (its width/height tracks the composition size).
                "parameters": {
                    "format_mode": "custom",
                    "width": width,
                    "height": height,
                },
                "disabled": False,
            },
        ]

        connections = [
            {
                "src_node_id": constant_node_id,
                "src_port_name": "result",
                "dst_node_id": output_node_id,
                "dst_port_name": "image",
            },
        ]

        return CompositeGraphData(
            composite_id=actual_composite_id,
            source_clip_id="__empty_comp_clip__",
            source_asset_id="",
            source_asset_path="",
            name=name,
            nodes=nodes,
            connections=connections,
            viewer_state={},
            viewport_state={},
            backdrops={},
            sticky_notes={},
            groups={},
            created_at=_time.time(),
            modified_at=_time.time(),
            width=width,
            height=height,
            comp_clip_id=comp_clip_id,
            comp_asset_id=comp_asset_id,
            source_kind="empty_comp_clip",
            fps=fps,
            duration_frames=duration_frames,
        )

    # ==================== NodeGraph 转换 ====================

    @staticmethod
    def from_node_graph(
        node_graph_dict: Dict[str, Any],
        composite_id: str,
        source_clip_id: str,
        source_asset_id: str,
        source_asset_path: str,
        name: str = "",
        width: int = 1920,
        height: int = 1080,
    ) -> CompositeGraphData:
        """
        从 NodeGraph.to_dict() 创建 CompositeGraphData。

        直接使用 NodeGraph 的序列化数据，确保格式完全一致。

        Args:
            node_graph_dict: NodeGraph.to_dict() 的结果
            composite_id: 合成 ID
            source_clip_id: 来源 clip ID
            source_asset_id: 来源素材 ID
            source_asset_path: 来源素材路径
            name: 合成名称
            width: 输出宽度
            height: 输出高度

        Returns:
            CompositeGraphData 实例
        """
        return CompositeGraphData(
            composite_id=composite_id,
            source_clip_id=source_clip_id,
            source_asset_id=source_asset_id,
            source_asset_path=source_asset_path,
            name=name,
            nodes=node_graph_dict.get("nodes", []),
            connections=node_graph_dict.get("connections", []),
            viewer_state=node_graph_dict.get("viewer_state", {}),
            viewport_state=node_graph_dict.get("viewport_state", {}),
            backdrops=node_graph_dict.get("backdrops", {}),
            sticky_notes=node_graph_dict.get("sticky_notes", {}),
            groups=node_graph_dict.get("groups", {}),
            created_at=time.time(),
            modified_at=time.time(),
            width=width,
            height=height,
            comp_clip_id=node_graph_dict.get("comp_clip_id"),
            comp_asset_id=node_graph_dict.get("comp_asset_id"),
            source_kind=node_graph_dict.get("source_kind", "timeline_clip_upgrade"),
            timeline_id=node_graph_dict.get("timeline_id"),
            clip_id=node_graph_dict.get("clip_id"),
            source_refs=node_graph_dict.get("source_refs", {}),
            fps=node_graph_dict.get("fps", 24.0),
            duration_frames=node_graph_dict.get("duration_frames", 0),
        )

    def to_node_graph_dict(self) -> Dict[str, Any]:
        """
        转换为 NodeGraph.to_dict() 格式。

        用于加载到 NodeEngine/NodeGraph。

        Returns:
            NodeGraph 格式的字典
        """
        return {
            "nodes": self.nodes,
            "connections": self.connections,
            "viewer_state": self.viewer_state,
            "viewport_state": self.viewport_state,
            "backdrops": self.backdrops,
            "sticky_notes": self.sticky_notes,
            "groups": self.groups,
        }

    # ==================== 辅助方法 ====================

    def __hash__(self) -> int:
        """哈希值，基于 composite_id"""
        return hash(self.composite_id)

    def __eq__(self, other: object) -> bool:
        """相等比较，基于 composite_id"""
        if not isinstance(other, CompositeGraphData):
            return NotImplemented
        return self.composite_id == other.composite_id

    def __repr__(self) -> str:
        """详细表示"""
        return (
            f"CompositeGraphData(composite_id={self.composite_id!r}, "
            f"name={self.name!r}, "
            f"source_clip_id={self.source_clip_id!r}, "
            f"nodes_count={len(self.nodes)}, "
            f"connections_count={len(self.connections)}, "
            f"has_viewer_state={bool(self.viewer_state)}, "
            f"has_viewport_state={bool(self.viewport_state)})"
        )

    # ==================== 验证方法 ====================

    def validate(self) -> List[str]:
        """
        验证数据完整性。

        Returns:
            错误消息列表（空列表表示验证通过）
        """
        errors = []

        # 检查基本字段
        if not self.composite_id:
            errors.append("composite_id is empty")
        if not self.source_clip_id:
            errors.append("source_clip_id is empty")
        if not self.source_asset_id:
            errors.append("source_asset_id is empty")

        # 检查分辨率
        if self.width <= 0 or self.height <= 0:
            errors.append(f"Invalid resolution: {self.width}x{self.height}")

        # 检查节点格式
        for node in self.nodes:
            if "id" not in node and "node_id" not in node:
                errors.append(f"Node missing id: {node}")
            if "type" not in node and "node_type" not in node:
                errors.append(f"Node missing type: {node}")

        # 检查连接格式
        for conn in self.connections:
            required_fields = ["src_node_id", "src_port_name", "dst_node_id", "dst_port_name"]
            for field_name in required_fields:
                if field_name not in conn:
                    errors.append(f"Connection missing {field_name}: {conn}")

        return errors

    def is_valid(self) -> bool:
        """检查数据是否有效"""
        return len(self.validate()) == 0

    # ==================== 统计方法 ====================

    @property
    def node_count(self) -> int:
        """节点数量"""
        return len(self.nodes)

    @property
    def connection_count(self) -> int:
        """连接数量"""
        return len(self.connections)

    def get_node_types(self) -> Dict[str, int]:
        """
        统计节点类型分布。

        Returns:
            {node_type: count} 字典
        """
        type_counts: Dict[str, int] = {}
        for node in self.nodes:
            node_type = node.get("type") or node.get("node_type", "unknown")
            type_counts[node_type] = type_counts.get(node_type, 0) + 1
        return type_counts
