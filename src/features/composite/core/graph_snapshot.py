"""
Graph Snapshot - Immutable copy of node graph state for thread-safe evaluation.

Captures topology, parameters, and evaluation order from the UI thread's NodeGraph.
The render thread evaluates against this snapshot without holding any locks.

Port-level revision tracking enables fine-grained dirty propagation:
- Each output port has its own revision number
- Upstream port revisions are tracked per-input
- Dirty propagation stops when revisions match cached values
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple


@dataclass
class PortRevision:
    """Revision tracking for a single port.

    Enables fine-grained dirty detection at the port level.
    When an output port's content changes, its revision increments.
    Downstream nodes track upstream revisions to detect changes.
    """
    port_name: str
    revision: int = 0  # Content version — bumped when output changes
    # For input ports: snapshot of upstream output port revision at last evaluation
    upstream_revision: int = 0  # Cached upstream revision (for dirty detection)


@dataclass(frozen=True)
class NodeSnapshot:
    """Immutable snapshot of a single node's state.

    Includes keyframe animation data for animated parameters.
    The render thread uses this to interpolate parameter values
    based on the current frame number.

    AUD-48006: the "immutable snapshot" contract is now enforced via
    ``frozen=True`` — attribute rebinding after construction raises.
    """
    node_id: str
    node_type: str
    parameters: Dict[str, Any]       # deep copy of node.parameters
    disabled: bool
    input_ports: List[Tuple[str, str]]   # (name, data_type)
    output_ports: List[Tuple[str, str]]  # (name, data_type)
    position: Tuple[float, float] = (0.0, 0.0)
    state: str = "dirty"                 # NodeState.name.lower()
    progress: float = 0.0
    error_message: str = ""
    param_revision: int = 0              # bumped when node parameters change

    # ── Port-level revision tracking ──
    # Maps port_name → revision for fine-grained dirty detection
    port_revisions: Dict[str, int] = field(default_factory=dict)
    # Maps input_port_name → upstream output port revision (cached from last eval)
    upstream_port_revisions: Dict[str, int] = field(default_factory=dict)

    # ── Animation Support ──
    # Keyframe tracks for animated parameters (from the node instance — P1)
    # Dict mapping param_name → KeyframeTrack serialized dict
    # Used by GraphEvaluator to interpolate parameter values at current frame
    keyframe_tracks: Dict[str, Dict[str, Any]] = field(default_factory=dict)

    # Parameter metadata (min/max values for interpolation)
    param_defs_meta: Dict[str, Dict[str, Any]] = field(default_factory=dict)

    # Expressions are preserved in the immutable snapshot for diagnostics and
    # frame-dependency tracking. ``parameters`` already contains the evaluated
    # value for the snapshot frame, so GPU nodes do not need to parse code.
    expressions: Dict[str, str] = field(default_factory=dict)
    expression_errors: Dict[str, str] = field(default_factory=dict)

    # ── Per-node timing + color (P1) ──
    # Effective time range of this node within the composition (None = full range)
    time_range: Optional[Dict[str, Any]] = None
    # Resolved header color (user override or class default) — used by the
    # Keyframes panel range bars and for color sync across the NodeGraph.
    node_color: str = "#555555"

    # ── Group/SubGraph boundary contract (P1-01) ──
    # Group nodes remain visible in the snapshot even though their boundary
    # connections are flattened for evaluation. Keeping the contract here
    # lets diagnostics, persistence, and future nested-graph tooling inspect
    # the original macro interface without touching live UI state.
    group_member_node_ids: Tuple[str, ...] = ()
    group_input_mappings: Dict[str, Tuple[str, str]] = field(default_factory=dict)
    group_output_mappings: Dict[str, Tuple[str, str]] = field(default_factory=dict)

    # N-05（2026-10-14 审计）：``parameters`` 是否已经是引擎在快照帧
    # 解析后的最终值（关键帧插值 + 表达式 + 父级世界变换）。生产
    # ``NodeGraph.snapshot()`` 恒为 True——渲染侧不得再对原始轨道插值
    # 覆盖世界参数。手工构造/测试桩快照可置 False 走渲染侧插值。
    parameters_resolved: bool = True


@dataclass(frozen=True)
class ConnectionSnapshot:
    """Immutable snapshot of a single connection (AUD-48006: frozen)."""
    src_node_id: str
    src_port_name: str
    dst_node_id: str
    dst_port_name: str


@dataclass(frozen=True)
class GraphSnapshot:
    """
    Immutable snapshot of the entire node graph.

    Created on the UI thread under NodeGraph._lock, then passed to the
    render thread for lock-free evaluation.

    AUD-48006: frozen=True enforces the documented immutability contract on
    all three snapshot dataclasses. The 3D viewer scene injection
    (composite_page._inject_viewer3d_scene_snapshot) builds a *replacement*
    snapshot via ``dataclasses.replace`` instead of rebinding fields.

    Animation Support:
    - frame_number: Current frame for parameter interpolation (video/animation)
    - Nodes with keyframe_tracks will have their parameters interpolated
      based on this frame number before evaluation.
    """
    generation: int                              # monotonically increasing
    nodes: Dict[str, NodeSnapshot] = field(default_factory=dict)
    connections: List[ConnectionSnapshot] = field(default_factory=list)
    eval_order: List[str] = field(default_factory=list)

    # COLOR node runtime state (texture ID from ColorOutput)
    color_texture_id: int = 0
    color_width: int = 0
    color_height: int = 0
    color_revision: int = 0       # incremented each time ColorOutput texture changes
    topology_revision: int = 0    # incremented on connect/disconnect

    # ── Animation Support ──
    # Current frame number for parameter interpolation (video/animation)
    # Used by GraphEvaluator to interpolate animated parameters
    frame_number: int = 0

    # ── Time semantics (plan P0-04 / P0-05) ──
    # Layer in/out frame gates: {node_id: (in_frame, out_frame)} — inclusive,
    # None on a side = unbounded. Derived from the graph's layer stacks at
    # snapshot time; the evaluator hides gated nodes outside the range.
    layer_frame_gates: Dict[str, Tuple[Optional[int], Optional[int]]] = field(
        default_factory=dict)
    # Downstream time-node remaps: {node_id: TimeRemap} — the frame each
    # node is effectively evaluated at (sources remap the playhead before
    # resolving a decode frame). Only non-identity remaps are recorded.
    frame_overrides: Dict[str, Any] = field(default_factory=dict)
    # N-07（2026-10-14 审计）：共享源被多条时间链要求不同帧时的冲突
    # 诊断文本——不再静默 first-wins（当前取向：反向拓扑序第一条生效）。
    # 空元组 = 无冲突。完整按分支求值属于 (node,frame) 请求模型。
    time_conflicts: Tuple[str, ...] = ()

    # R-14（2026-10-14 审计）：持久化的全局 3D 场景成品设置（
    # viewer_state["default_3d_scene"] 原始 dict）。预览经页面注入
    # CompositeEvalContext.global_3d_scene_settings；快照携带同一来源，
    # 使所有导出入口（图片/序列/视频/multi-pass/headless）与预览同合同，
    # 不再回落 Scene3D 旧默认。临时导航/选择/isolate 不在此字段内。
    global_3d_scene_settings: Dict[str, Any] = field(default_factory=dict)

    # ── Lookup helpers (no lock needed) ──

    def get_input_connections(self, node_id: str) -> List[ConnectionSnapshot]:
        """Get all connections feeding into a node's input ports."""
        return [c for c in self.connections if c.dst_node_id == node_id]

    def get_output_connections(self, node_id: str) -> List[ConnectionSnapshot]:
        """Get all connections from a node's output ports."""
        return [c for c in self.connections if c.src_node_id == node_id]

    # ── Port revision helpers ──

    def get_upstream_port_revision(self, node_id: str, input_port_name: str) -> int:
        """Get the revision of the upstream output port connected to this input.

        Returns 0 if the input is not connected or upstream node not found.
        """
        for conn in self.connections:
            if conn.dst_node_id == node_id and conn.dst_port_name == input_port_name:
                upstream_ns = self.nodes.get(conn.src_node_id)
                if upstream_ns:
                    return upstream_ns.port_revisions.get(conn.src_port_name, 0)
        return 0

    def get_cached_upstream_revision(self, node_id: str, input_port_name: str) -> int:
        """Get the cached upstream revision from the node's last evaluation.

        Returns 0 if no cached revision exists.
        """
        ns = self.nodes.get(node_id)
        if ns:
            return ns.upstream_port_revisions.get(input_port_name, 0)
        return 0

    def is_input_port_dirty(self, node_id: str, input_port_name: str) -> bool:
        """Check if an input port's upstream content has changed.

        Compares current upstream revision against cached revision from last eval.
        """
        current = self.get_upstream_port_revision(node_id, input_port_name)
        cached = self.get_cached_upstream_revision(node_id, input_port_name)
        return current != cached

    def get_downstream_nodes(self, node_id: str) -> List[str]:
        """Get all node IDs that directly depend on this node."""
        return [c.dst_node_id for c in self.connections if c.src_node_id == node_id]
