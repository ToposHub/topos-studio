"""
Render Scheduler - Request tracking, plan generation, and evaluation diagnostics.

Provides the scheduling layer between the UI thread (CompositePage) and the
render thread (CompositeRenderer). Separates scheduling concerns from the
core evaluation logic in GraphEvaluatorMixin.

Key classes:
- RenderRequest: Immutable request with snapshot, request_id, and timestamp.
- PrefetchRequest: Request for prefetching predicted parameter values.
- NodePlan: Per-node execution plan (static analysis, no GPU).
- RenderPlan: Complete execution plan for a graph evaluation pass.
- EvalStats: Diagnostics for a completed evaluation pass.
- RequestIdGenerator: Thread-safe monotonic counter for unique request IDs.
- RenderScheduler: Coordinates request creation, plan generation, and diagnostics.
"""

import hashlib
import json
import logging
import threading
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple, TYPE_CHECKING

from ..core.graph_snapshot import GraphSnapshot
from ..core.node_capabilities import (
    is_output_node_type,
    is_placeholder_node_type,
    is_source_node_type,
)
from ..core.quality_profile import (
    CompositeQualityProfile,
    normalize_quality_profile,
    quality_profile_for_request,
)
from ..core.output_targets import (
    DEFAULT_OUTPUT_NAME,
    normalize_output_name,
)

if TYPE_CHECKING:
    from ..core.format import BBox
    from ..core.graph_snapshot import NodeSnapshot

logger = logging.getLogger(__name__)


def _snapshot_node_needs_frame(ns: "NodeSnapshot") -> bool:
    """P0-2: does this node depend on the snapshot frame_number?

    Mirrors BaseEvaluatorMixin._node_needs_frame_cache so the scheduler's
    dirty-detection and the evaluator's cache key agree on which nodes are
    frame-dependent. Keep this policy in lockstep with the evaluator: a
    mismatch is particularly dangerous because the evaluator's dirty-skip
    path can reuse a previous frame's output before it gets a chance to
    compute the more precise cache key.
    """
    if getattr(ns, "keyframe_tracks", None):
        return True
    # A node's active/inactive state can change at a composition range
    # boundary even when its parameters are static. The evaluator includes
    # the frame in the cache key for these nodes, so the scheduler must mark
    # them dirty on a playhead move as well.
    if getattr(ns, "time_range", None):
        return True
    if getattr(ns, "expressions", None):
        return True
    try:
        from ..core.node_capabilities import requires_frame_number
        if requires_frame_number(ns.node_type):
            return True
    except Exception:
        pass
    # Some nodes read the playhead from their evaluation context without
    # declaring keyframes or a capability-level frame requirement. Ask the
    # registered node class for this explicit cache contract. The import is
    # intentionally lazy: node_engine imports node_capabilities while it
    # builds the registry, so importing it at module load would create a
    # circular dependency.
    params = getattr(ns, "parameters", None)
    if isinstance(params, dict) and params:
        try:
            from ..core.node_engine import NODE_REGISTRY
            cls = NODE_REGISTRY.get(ns.node_type)
            if cls is not None and cls.params_require_frame_cache(params):
                return True
        except Exception:
            # A missing/optional node implementation must not prevent the
            # scheduler from producing a plan. Capability and source checks
            # above still provide the safe baseline.
            pass
    if ns.node_type in DYNAMIC_SOURCE_NODE_TYPES:
        return True
    return False


# ── Node type classifications (used for consistency check) ──

# Legacy set for consistency validation with node_capabilities system
# BUG-13: Updated to include all source types from capability table
# scene3d 是 source 节点（自管 GL 资源），必须在此列表中，否则调度器会误当 effect 处理
# 阶段七：camera3d/light3d/geo3d/model3d 同为 source 节点（scene 数据源）
_GPU_STATEFUL_TYPES = frozenset({"color_input", "constant", "image_input", "timeline_clip_input", "video_frame", "motion_graphic", "text", "scene3d", "camera3d", "light3d", "geo3d", "model3d", "deformer3d"})

# Source nodes whose pixels depend on runtime state outside the static graph
# snapshot. They must be re-evaluated on every request so a failed first decode
# or a playhead/frame change cannot leave a stale cached placeholder on screen.
DYNAMIC_SOURCE_NODE_TYPES = frozenset({"timeline_clip_input", "video_frame", "motion_graphic"})


def _is_gpu_stateful(node_type: str) -> bool:
    """Check if a node type is GPU-stateful using node_capabilities.

    Args:
        node_type: Node type string (e.g., "color_input", "blur").

    Returns:
        True if the node is a source/GPU-stateful node.
    """
    return is_source_node_type(node_type)


def _check_capability_consistency() -> None:
    """Validate consistency between legacy set and node_capabilities.

    Raises AssertionError if source types in capability table don't match
    the legacy _GPU_STATEFUL_TYPES set. Called once at module load.
    """
    from ..core.node_capabilities import SOURCE_NODE_TYPES

    # Check that all legacy types are in capability source types
    missing_in_cap = _GPU_STATEFUL_TYPES - SOURCE_NODE_TYPES
    if missing_in_cap:
        raise AssertionError(
            f"Consistency error: types in _GPU_STATEFUL_TYPES but not in "
            f"SOURCE_NODE_TYPES: {missing_in_cap}. "
            f"Update _NODE_CAPABILITY_TABLE in node_capabilities.py"
        )


# Run consistency check at module load (only in debug/development)
# Commented out for production; enable for CI validation
# _check_capability_consistency()


class RenderMode:
    """Render mode determines quality and caching behavior."""
    PREVIEW = "preview"   # Viewer resolution, fast, cancellable
    FULL = "full"         # Full resolution, high quality
    EXPORT = "export"     # Deterministic, no preview cache reuse


@dataclass
class RenderRequest:
    """Immutable render request submitted to the render thread.

    Attributes:
        snapshot: Immutable graph state to evaluate.
        request_id: Monotonically increasing unique ID.
        timestamp: Creation time (time.monotonic) for latency measurement.
        render_mode: Quality mode — affects cache key and quality params.
        viewer_roi: Optional region of interest for the output (viewer visible region).
        graph_generation: Graph topology generation for stale detection.
        color_revision: Color input revision for stale detection.
        interaction_active: True while a viewer handle is being dragged;
            enables raised eval-cache budget to avoid allocation jitter.
        viewer_node_id: Node whose output should be presented to the viewer
            instead of the Output node's ``display`` port. None (default)
            reproduces the legacy "always show Output" behaviour. When set,
            ``_evaluate_snapshot`` returns that node's first image output after
            evaluating the whole graph (output pipeline and preview pipeline
            share one evaluation; only the picked result differs). This is the
            key enabler for Fusion/Nuke-style "preview any node" — see
            ``docs/composite_node_preview_pipeline_plan_2026-06-29.md``.
        viewer_node_port: Explicit output port name for the previewed node.
            None (default) ⇒ auto-pick the first image output port.
    """
    snapshot: GraphSnapshot
    request_id: int
    timestamp: float
    render_mode: str = RenderMode.PREVIEW
    viewer_roi: Optional["BBox"] = None  # ROI for viewer preview
    proxy_scale: float = 1.0  # 1.0=full-res, 0.5=half-res preview
    # P2-04: semantic quality label. An empty value derives from render_mode
    # and proxy_scale; explicit labels are preserved for project-level policy.
    quality_profile: str = ""
    # P2-05: terminal output target. Empty keeps the legacy display target;
    # output_node_id disambiguates duplicate names when required.
    output_name: str = DEFAULT_OUTPUT_NAME
    output_node_id: Optional[str] = None
    graph_generation: int = 0  # Snapshot.generation at request creation
    color_revision: int = 0    # Snapshot.color_revision at request creation

    # Node-preview pipeline: present an arbitrary node's output to the viewer.
    # None ⇒ Output node's display (legacy behaviour). See design doc above.
    viewer_node_id: Optional[str] = None
    viewer_node_port: Optional[str] = None

    # View signature: opaque string that changes whenever view-dependent
    # overrides change (view_mode + preview target). The renderer includes it in
    # its request-dedup signature so a Tab toggle / "1"-preview switch forces a
    # re-evaluate even when graph topology (generation) is unchanged — otherwise
    # baked-into-output overrides like Scene3D show_grid/bg_opaque would be masked
    # by the generation-only skip check. Set by CompositePage; "" ⇒ no view state.
    view_signature: str = ""

    # Force one full interactive refresh. Used for pure viewer-context changes
    # (2D/3D Tab switch) where snapshot overrides change but persistent node
    # revisions do not, so the render thread's dirty-skip cache must be reset.
    force_refresh: bool = False

    # Optional business-level context for timeline clip source nodes
    runtime_context: Any = None          # CompositeRuntimeContext
    timeline_clip_gpu_provider: Any = None  # TimelineClipFrameProvider
    edit_project: Any = None             # EditProject for ColorStack lookup

    # Interaction hint: True while a viewer handle is being dragged.
    # Tells the evaluator to raise the eval-cache budget (CacheManager
    # interaction mode) so per-frame parameter changes don't thrash the
    # cache → texture-pool eviction → real GL allocation. Set by
    # CompositePage from its _interaction_active flag; defaults False so
    # non-drag renders are unaffected.
    interaction_active: bool = False

    # Real-time timeline playback is throughput-oriented: once an evaluation
    # has started, presenting that completed frame is preferable to cancelling
    # it merely because the playhead advanced again.  The renderer still
    # coalesces queued work to the newest request, but the in-flight request
    # is allowed to finish and becomes the next displayable frame.
    cancel_on_supersede: bool = True

    # 渲染线程自驱播放（独立 comp clip 播放专用）：非 None 时，渲染线程
    # 完成本请求的求值后进入自驱循环——按 wall-clock 自行推进帧号并连续
    # 渲染上屏，UI 不再逐帧提交（消灭"解码→notify→UI 快照→重提交"的
    # 双往返）。dict 字段：start_time(perf_counter 基准)、start_frame、
    # fps、in_frame、out_frame、loop。UI 侧暂停/seek/参数修改通过提交新
    # 请求（supersede）或 stop_playback_drive() 令循环退出。
    # 仅适用于无关键帧动画的图（动画参数由 UI 侧按帧号预插值）。
    playback_drive: Optional[dict] = None

    # M2: 四视图模式开关。True 时 Scene3D 额外渲染 TOP/RIGHT/FRONT 三个正交视图纹理，
    # 供 viewport 在 quad 布局里分象限显示。False = 仅透视（默认）。由 viewport
    # 的 quad 切换状态驱动，进 view_signature 确保切换触发重渲染。
    quad_view: bool = False

    # M2: 单视图正交 override（0/None=perspective，1=top，2=right，3=front）。
    # SINGLE 模式下，若非 0，Scene3D 用对应正交投影渲染主纹理。
    single_ortho_view: Optional[int] = None

    # Milestone C / P2：四视图活动视图代码（0=perspective，1=top，2=right，3=front）。
    # quad 模式下鼠标所在象限=活动视图。Scene3D 据此对非活动视图降质/降频：
    # 导航态时非活动正交视图可跳过或用 solid（保活动视图流畅）。None/0=无活动信息
    # （向后兼容，退化为全部正交视图同质量）。由 CompositePage 从 viewer 状态填入。
    quad_active_view: Optional[int] = None

    # P7：当前选中的 3D 节点 id（Scene3D outline pass 据此画描边）。None=无选中。
    selected_3d_node_id: Optional[str] = None
    # P7：隔离模式节点 id——只渲染该节点的 mesh。None=不隔离。Shift+I toggle。
    isolate_3d_node_id: Optional[str] = None

    # 全局 3D 场景默认设置（来自 viewer_state["default_3d_scene"]，已与
    # DEFAULT_VIEWER3D_SCENE_SETTINGS 合并）。Scene3D 作为纯渲染器时，所有
    # 场景功能（默认相机/灯光/背景/IBL/网格/输出控制/活动相机）统一从此处读取。
    # None = 未注入（向后兼容）。由 CompositePage 构建 request 时填入。
    global_3d_scene_settings: Any = None  # Optional[dict]

    # 预览上下文覆盖：{node_id: {show_grid/bg_opaque/show_axes: bool}}。
    # 由 CompositePage._apply_view_mode_overrides 构建。3D 预览的 Scene3D 用
    # 预览参考开关，合成输出的 Scene3D 用 output_* 开关。因为场景参数已移至
    # 全局设置（不在节点上），改为经此字典按 node_id 精确覆盖。
    view_mode_overrides: Any = None  # Optional[Dict[str, Dict[str, Any]]]

    def __post_init__(self) -> None:
        """Normalize quality without breaking legacy request constructors."""

        if self.quality_profile:
            profile = normalize_quality_profile(self.quality_profile)
        else:
            profile = quality_profile_for_request(
                self.render_mode, self.proxy_scale)
        if str(self.render_mode).lower() in {"full", "export"}:
            profile = CompositeQualityProfile.FINAL.value
        self.quality_profile = profile
        self.output_name = normalize_output_name(self.output_name)


@dataclass
class PrefetchRequest:
    """Request for prefetching predicted parameter values.

    Used during UI debounce period to predict and prefetch
    likely downstream node results before user settles on value.

    Attributes:
        snapshot: Base graph snapshot (with current params).
        request_id: Unique ID for this prefetch request.
        node_id: Node ID whose parameter is being prefetched.
        param_name: Parameter name to prefetch.
        predicted_value: Predicted value to use for prefetch.
        confidence: Confidence level of prediction (0.0-1.0).
        target_node_ids: Optional list of specific node IDs to prefetch
                         (downstream nodes). If None, prefetch all downstream.
        timestamp: Creation time for timing analysis.
    """
    snapshot: GraphSnapshot
    request_id: int
    node_id: str
    param_name: str
    predicted_value: float
    confidence: float
    target_node_ids: Optional[List[str]] = None  # Downstream nodes to prefetch
    timestamp: float = 0.0

    def __post_init__(self):
        if self.timestamp == 0.0:
            import time
            self.timestamp = time.monotonic()


@dataclass
class NodePlan:
    """Per-node execution plan entry — static analysis, no GPU.

    Generated by RenderScheduler.plan() before any GPU evaluation.
    Contains everything that CAN be known without upstream TextureResults.

    Attributes:
        node_id: Unique node identifier.
        node_type: Node type string (e.g., "blur", "color_input").
        action: Planned execution action:
            "source" — GPU-stateful node (color_input, constant, image_input)
            "effect" — pure-effect node needing GPU evaluation
            "output" — output node
            "passthrough" — disabled node, pass through first input
            "placeholder" — unimplemented node type, skip
        params_hash: Pre-computed MD5[:12] of node parameters.
        inputs: Pre-resolved input mapping: port_name → upstream_node_id.
        input_rois: ROI needed from each input port (computed during ROI propagation).
        output_roi: ROI this node should produce (computed during ROI propagation).
        roi_expansion: Extra pixels needed beyond output_roi for this node type.
        estimated_bbox: Estimated output bbox (computed from input bboxes).
        lod_level: Level of Detail for LOD rendering (0=full-res, 2=quarter-res).
        dependency_level: Depth in the dependency graph (0=source, 1=depends on source, etc.)
    """
    node_id: str
    node_type: str
    action: str
    params_hash: str
    inputs: Dict[str, str] = field(default_factory=dict)

    # ── ROI fields (computed during plan generation with ROI propagation) ──
    input_rois: Dict[str, Optional["BBox"]] = field(default_factory=dict)
    output_roi: Optional["BBox"] = None
    roi_expansion: int = 0  # Extra pixels needed on all sides
    estimated_bbox: Optional["BBox"] = None  # Estimated output data window

    # ── LOD (Level of Detail) for viewport-based optimization ──
    lod_level: int = 0  # 0=full resolution, 2=quarter resolution (proxy 0.25x)

    # ── Parallel evaluation (computed during plan generation) ──
    dependency_level: int = 0  # Depth in dependency graph (0=source)


@dataclass
class RenderPlan:
    """Complete execution plan for a graph evaluation pass.

    Generated by RenderScheduler.plan() from a GraphSnapshot.
    Contains static analysis results and is filled with runtime
    diagnostics during execution.

    Attributes:
        snapshot: The graph snapshot this plan was generated from.
        render_mode: "preview", "full", or "export".
        eval_order: Node IDs in topological evaluation order.
        node_plans: Per-node execution plans keyed by node_id.
        total_nodes: Total number of nodes in the plan.
        source_count: Number of source nodes.
        effect_count: Number of effect nodes.
        passthrough_count: Number of disabled (passthrough) nodes.
        placeholder_count: Number of unimplemented nodes.
        viewer_roi: The region requested for the final output.
        roi_enabled: Whether ROI optimization is active for this plan.
        parallel_groups: List of parallel execution groups, each group is a list of node IDs.
                         Groups are ordered by dependency level (group 0 = level 0, etc.)
                         Nodes in the same group can be executed in parallel.
    """
    snapshot: GraphSnapshot
    render_mode: str
    eval_order: List[str]
    node_plans: Dict[str, NodePlan]
    total_nodes: int
    source_count: int = 0
    effect_count: int = 0
    passthrough_count: int = 0
    placeholder_count: int = 0
    output_count: int = 0

    # ── ROI configuration ──
    viewer_roi: Optional["BBox"] = None  # User-specified output ROI
    roi_enabled: bool = False  # Whether ROI propagation was performed

    # ── Dirty propagation (pre-computed by plan generation) ──

    dirty_node_ids: List[str] = field(default_factory=list)

    # ── Parallel evaluation (pre-computed by plan generation) ──

    parallel_groups: List[List[str]] = field(default_factory=list)  # Groups of node IDs by dependency level
    max_parallelism: int = 1  # Maximum number of nodes in a single group

    # ── Tiling configuration (v3 - auto-detect large images) ──

    needs_tiling: bool = False  # Whether tile rendering is required
    tile_size: int = 1024  # Recommended tile size for tiling
    estimated_padding: int = 0  # Padding needed for spatial effects
    estimated_memory_mb: float = 0.0  # Estimated GPU memory for full-frame
    output_dimensions: Optional[Tuple[int, int]] = None  # (width, height) of output

    # ── Runtime diagnostics (filled by executor) ──

    cache_hits: int = 0
    cache_misses: int = 0
    eval_time_ms: float = 0.0
    cached_node_ids: List[str] = field(default_factory=list)

    # ── M08.1c：烘焙陈旧守卫（plan 期审计结果，供预览/导出 UI 徽标） ──
    # audit_bake_staleness 条目（{bake_node_id, target_id, stale, reason}；
    # stale=None = 旧工程无签名烘焙——不冒称新鲜也不冒称过期）。
    bake_staleness: List[Dict[str, Any]] = field(default_factory=list)

    @property
    def stale_bake_count(self) -> int:
        """显式陈旧（stale=True）的烘焙数（无签名 None 不计入——
        旧工程徽标语义单独呈现，见 UI）。"""
        return sum(1 for e in self.bake_staleness if e.get("stale") is True)

    @property
    def hit_rate(self) -> float:
        total = self.cache_hits + self.cache_misses
        return self.cache_hits / total if total > 0 else 0.0

    def get_node_roi(self, node_id: str) -> Optional["BBox"]:
        """Get the output ROI for a specific node."""
        np = self.node_plans.get(node_id)
        return np.output_roi if np else None

    def get_input_roi(self, node_id: str, port_name: str) -> Optional["BBox"]:
        """Get the input ROI needed from a specific port."""
        np = self.node_plans.get(node_id)
        if np:
            return np.input_rois.get(port_name)
        return None


@dataclass
class EvalStats:
    """Diagnostics for a completed evaluation pass.

    Deprecated: Use RenderPlan diagnostics instead.
    """
    request_id: int
    cache_hits: int = 0
    cache_misses: int = 0
    eval_time_ms: float = 0.0
    node_count: int = 0

    @property
    def hit_rate(self) -> float:
        total = self.cache_hits + self.cache_misses
        return self.cache_hits / total if total > 0 else 0.0


class RequestIdGenerator:
    """Thread-safe monotonic request ID generator.

    Used by CompositePage (UI thread) to assign unique IDs to render requests.
    """

    def __init__(self) -> None:
        self._counter: int = 0
        self._lock = threading.Lock()

    def next_id(self) -> int:
        with self._lock:
            self._counter += 1
            return self._counter

    @property
    def current(self) -> int:
        return self._counter


class RenderScheduler:
    """Coordinates render request creation, plan generation, and diagnostics.

    Lives on the render thread. Tracks the latest request being processed
    so that mid-evaluation cancellation can detect stale requests.

    Also tracks per-node revisions to enable fine-grained dirty propagation:
    only nodes whose parameters, connections, or upstream outputs changed
    are marked dirty in the RenderPlan.
    """

    def __init__(self) -> None:
        self._latest_request_id: int = 0
        self._latest_graph_generation: int = 0
        self._latest_color_revision: int = 0
        self._latest_plan: Optional[RenderPlan] = None
        self._id_lock = threading.Lock()

        # ── Dirty propagation tracking ──
        self._prev_param_revisions: Dict[str, int] = {}
        # P1-8: previous parameter CONTENT hash per node. A freshly
        # deserialized graph (the timeline comp-clip path rebuilds
        # snapshots via from_dict on graph replacement) restarts
        # param_revision at 1, so revision comparison alone cannot see an
        # edited parameter value — the content hash makes downstream-cone
        # invalidation automatic without any explicit graph-dirty flag.
        self._prev_params_hash: Dict[str, str] = {}
        # AUD-7502: per-node memo of (param_revision, params_hash,
        # parameters-ref). json.dumps(sort_keys)+md5 of a node's parameters
        # ran for EVERY node on EVERY plan(); for nodes carrying large
        # hidden payloads (tracker_node's track_data JSON, potentially MBs)
        # that is O(payload bytes) per node per frame during playback.
        # A memo hit requires BOTH a param_revision match AND content
        # equality against the parameters the entry was computed from
        # (C-level dict compare — for the big payloads that's a memcmp,
        # far cheaper than re-serialization). Content changes that slip
        # past revision tracking (P1-8: freshly deserialized graphs
        # restart param_revision) therefore still recompute, preserving
        # the dirty-detection contract. The generation check in plan()
        # clears the memo on graph replacement (frees the held parameter
        # references); reset_dirty_tracking()/node removal also clear.
        self._params_hash_memo: Dict[str, Tuple[int, str, Dict[str, Any]]] = {}
        self._params_hash_memo_generation: int = -1
        # AUD-8419: per-node cache of the temporary Node instances that
        # _propagate_roi instantiates (twice per effect node per plan under
        # the old code: forward bbox pass + reverse ROI pass). Keyed by
        # node_id → (param_revision, parameters_ref, Node); identity check
        # on the parameters dict mirrors the AUD-7502 memo contract.
        self._roi_node_cache: Dict[str, Tuple[int, Dict[str, Any], Any]] = {}
        self._prev_disabled: Dict[str, bool] = {}
        self._prev_inputs: Dict[str, Dict[str, str]] = {}
        self._prev_topology_revision: int = -1
        self._prev_color_revision: int = -1
        # P0-2: previous snapshot frame_number. When the playhead moves, only
        # frame-dependent nodes (animated / time-dependent / dynamic source)
        # need re-evaluation; static nodes keep their cached result.
        self._prev_frame_number: Optional[int] = None
        # B1: previous effective viewer ROI key. An ROI change alters every
        # node's crop geometry (and cache key); the dirty-skip path must
        # never serve outputs computed under a different ROI.
        self._prev_roi_key: Optional[tuple] = None

        # ── Port-level revision tracking for fine-grained dirty detection ──
        # Maps node_id -> {port_name -> revision}
        self._prev_port_revisions: Dict[str, Dict[str, int]] = {}
        # Maps node_id -> {input_port_name -> upstream_revision}
        self._prev_upstream_revisions: Dict[str, Dict[str, int]] = {}

    # ── Plan Generation ──

    def plan(self, snapshot: GraphSnapshot,
             render_mode: str = "preview",
             viewer_roi: Optional["BBox"] = None,
             force_lod: int = 0,
             update_tracking: bool = True,
             viewer_node_id: Optional[str] = None) -> RenderPlan:
        """Generate an execution plan from a snapshot (pure analysis, no GPU).

        Pre-computes per-node classification, params hash, input resolution,
        dirty propagation, and ROI propagation if viewer_roi is specified.

        Args:
            snapshot: Immutable graph state to analyze.
            render_mode: "preview", "full", or "export".
            update_tracking: Advance the dirty-comparison tracking state.
                Must stay True for the plan call that DRIVES evaluation.
                Analysis-only callers (e.g. the readback renderer's tiling
                probe) must pass False: consuming the dirty state here made
                the evaluator's own plan() compare equal against just-stored
                state and skip every node that had cached outputs — param
                edits never re-rendered on the readback path (P1-8).
            viewer_roi: Optional region of interest for the output.
                        When provided, ROI propagation is performed to
                        optimize rendering to only the visible region.
            force_lod: 强制全局 LOD 等级(预览质量切换用)。
                        0=不强制(默认),1=半分辨率,2=1/4 分辨率。
                        仅预览路径从 RenderRequest.proxy_scale 推导后传入;
                        导出路径恒为 0,确保导出始终原始分辨率。
            viewer_node_id: "preview any node" 目标节点（RenderRequest.
                        viewer_node_id）。ROI 传播除 Output 节点外也以该
                        节点为起点（AUD-8412），否则其上游分支拿不到
                        output_roi 会被整体降到 1/4 分辨率。None=仅 Output。

        Returns:
            RenderPlan with eval_order, per-node plans, classification counts,
            dirty_node_ids, and ROI information if viewer_roi was provided.
        """
        # M08.1（S10 后续）：导出前烘焙陈旧守卫——正在消费陈旧/无签名
        # 烘焙时显式告警（不静默播放旧烘焙）。M08.1c：审计结果挂
        # plan.bake_staleness（preview/export 两模式都跑——预览 UI 徽标
        # 与导出 WARNING 同一数据源；审计轻量）。
        bake_staleness: List[Dict[str, Any]] = []
        if render_mode in ("export", "preview"):
            try:
                from src.features.composite.bake_service import (
                    audit_bake_staleness,
                )
                bake_staleness = list(audit_bake_staleness(snapshot))
                if render_mode == "export":
                    for entry in bake_staleness:
                        if entry["stale"] is not False:
                            logger.warning(
                                "Export consumes bake node %s (target %s): %s",
                                entry["bake_node_id"], entry["target_id"],
                                entry["reason"],
                            )
            except Exception:  # noqa: BLE001 — 守卫失败不阻断渲染计划
                logger.debug("bake staleness audit unavailable", exc_info=True)

        # AUD-7502 memo guard: a graph rebuilt via from_dict restarts both
        # param_revision and generation counters. generation only ever
        # increases within one graph instance, so seeing it move backwards
        # is a reliable new-graph signal — drop the whole memo.
        if snapshot.generation < self._params_hash_memo_generation:
            self._params_hash_memo.clear()
            # AUD-8419: graph replacement also invalidates the ROI temp-node
            # cache (frees the held Node instances).
            self._roi_node_cache.clear()
        self._params_hash_memo_generation = snapshot.generation
        node_plans: Dict[str, NodePlan] = {}
        counts = {"source": 0, "effect": 0, "passthrough": 0,
                  "placeholder": 0, "output": 0}

        for node_id in snapshot.eval_order:
            ns = snapshot.nodes.get(node_id)
            if ns is None:
                continue

            # Pre-resolve input connections: port_name → upstream_node_id
            inputs: Dict[str, str] = {}
            for conn in snapshot.get_input_connections(node_id):
                inputs[conn.dst_port_name] = conn.src_node_id

            # Pre-compute params hash (AUD-7502 memo — see __init__).
            params_hash: Optional[str] = None
            memo = self._params_hash_memo.get(node_id)
            if memo is not None and memo[0] == ns.param_revision:
                try:
                    if ns.parameters is memo[2] or ns.parameters == memo[2]:
                        params_hash = memo[1]
                except (TypeError, ValueError):
                    # Exotic parameter values (e.g. ndarray elements make
                    # dict == ambiguous) → fall through to full recompute.
                    params_hash = None
            if params_hash is None:
                params_str = json.dumps(ns.parameters, sort_keys=True, default=str)
                params_hash = hashlib.md5(params_str.encode()).hexdigest()[:12]
                self._params_hash_memo[node_id] = (
                    ns.param_revision, params_hash, ns.parameters)

            # Classify node action using node_capabilities system
            # BUG-13 fix: Source and output nodes should not be overridden by placeholder
            # IS_PLACEHOLDER is for UI indication, not for blocking action classification
            from ..core.node_capabilities import is_output_node_type

            # First check explicit output types (file_output, output)
            if is_output_node_type(ns.node_type):
                action = "output"
            # Then check source types (generators like text, color_input, etc.)
            elif _is_gpu_stateful(ns.node_type):
                action = "source"
            # Group boundaries are flattened in GraphSnapshot; the group
            # marker itself is therefore a non-GPU passthrough node.
            elif ns.node_type == "group":
                action = "passthrough"
            # Placeholder check only applies to effect types
            elif is_placeholder_node_type(ns.node_type):
                action = "placeholder"
            elif ns.disabled:
                action = "passthrough"
            else:
                action = "effect"

            counts[action] = counts.get(action, 0) + 1

            node_plans[node_id] = NodePlan(
                node_id=node_id,
                node_type=ns.node_type,
                action=action,
                params_hash=params_hash,
                inputs=inputs,
            )

        # ── Compute dependency levels for parallel evaluation ──
        self._compute_dependency_levels(node_plans)

        # ── Build parallel groups from dependency levels ──
        parallel_groups, max_parallelism = self._build_parallel_groups(node_plans)

        # ── Dirty propagation ──
        # B1: the effective ROI participates in dirty detection.  When it
        # changes (set / moved / cleared), force a full-dirty plan exactly
        # like a first plan — otherwise the evaluator's dirty-skip would
        # replay outputs cached under the previous ROI (or full frame), and
        # the viewer would keep showing the old region.
        effective_roi_key: Optional[tuple] = None
        if (viewer_roi is not None and not viewer_roi.is_empty
                and render_mode == RenderMode.PREVIEW and force_lod == 0):
            effective_roi_key = (viewer_roi.x, viewer_roi.y,
                                 viewer_roi.width, viewer_roi.height)
        if effective_roi_key != self._prev_roi_key:
            self._prev_param_revisions = {}
        self._prev_roi_key = effective_roi_key
        dirty = self._compute_dirty_nodes(snapshot, node_plans)

        # ── ROI propagation (if viewer_roi specified) ──
        # Proxy quality (force_lod) evaluates at reduced resolution where a
        # full-res ROI rect would crop the wrong area — ROI stays off there.
        roi_enabled = (viewer_roi is not None
                       and render_mode == RenderMode.PREVIEW
                       and force_lod == 0)
        if roi_enabled:
            self._propagate_roi(snapshot, node_plans, viewer_roi,
                                viewer_node_id=viewer_node_id)

        # ── 强制全局 LOD(预览质量切换)──
        # 仅由预览路径(base_evaluator 从 request.proxy_scale 推导)传入;
        # 导出路径不传 force_lod,恒为 0,保证导出始终原始分辨率。
        if force_lod > 0:
            for np in node_plans.values():
                np.lod_level = max(np.lod_level, force_lod)

        # ── Tiling analysis (v3 - auto-detect large images) ──
        # Determine output dimensions and check if tiling is needed
        output_w, output_h = self._get_output_dimensions(snapshot, node_plans)
        output_dimensions = (output_w, output_h)

        # Compute padding needed for spatial effects
        estimated_padding = self._compute_total_padding(snapshot, node_plans)

        # Estimate memory and determine if tiling is needed
        needs_tiling, tile_size, estimated_memory_mb = self._analyze_tiling_needs(
            output_w, output_h,
            counts["source"], counts["effect"],
            len(node_plans),
            render_mode
        )

        # Update tracking state for next plan generation — only for the
        # plan call that drives evaluation (see update_tracking docstring).
        if update_tracking:
            self._update_tracking(snapshot, node_plans)

        return RenderPlan(
            snapshot=snapshot,
            render_mode=render_mode,
            eval_order=list(snapshot.eval_order),
            node_plans=node_plans,
            total_nodes=len(node_plans),
            source_count=counts["source"],
            effect_count=counts["effect"],
            passthrough_count=counts["passthrough"],
            placeholder_count=counts["placeholder"],
            output_count=counts["output"],
            dirty_node_ids=dirty,
            parallel_groups=parallel_groups,
            max_parallelism=max_parallelism,
            viewer_roi=viewer_roi,
            roi_enabled=roi_enabled,
            needs_tiling=needs_tiling,
            tile_size=tile_size,
            estimated_padding=estimated_padding,
            estimated_memory_mb=estimated_memory_mb,
            output_dimensions=output_dimensions,
            bake_staleness=bake_staleness,
        )

    # ── Dependency Level Computation ──

    def _compute_dependency_levels(self, node_plans: Dict[str, NodePlan]) -> None:
        """Compute dependency level for each node based on upstream dependencies.

        Dependency level indicates how many "hops" a node is from its source nodes:
        - Source nodes (color_input, constant, image_input) have level 0
        - Nodes depending only on source nodes have level 1
        - Nodes depending on level 1 nodes have level 2
        - etc.

        Algorithm:
        - Traverse nodes in topological order (snapshot.eval_order)
        - For each node: level = max(upstream_node_levels) + 1
        - If no upstream dependencies: level = 0

        Args:
            node_plans: Per-node plans to update with dependency_level.
        """
        for node_id, np in node_plans.items():
            # Source nodes always have level 0
            if _is_gpu_stateful(np.node_type):
                np.dependency_level = 0
                continue

            # Compute level from upstream dependencies
            if np.inputs:
                # Get max level from all upstream nodes
                max_upstream_level = 0
                for port_name, upstream_id in np.inputs.items():
                    upstream_np = node_plans.get(upstream_id)
                    if upstream_np:
                        max_upstream_level = max(max_upstream_level, upstream_np.dependency_level)
                np.dependency_level = max_upstream_level + 1
            else:
                # No inputs: treat as source (level 0)
                np.dependency_level = 0

    def _build_parallel_groups(self, node_plans: Dict[str, NodePlan]) -> Tuple[List[List[str]], int]:
        """Build parallel execution groups from dependency levels.

        Groups nodes by their dependency level. Nodes in the same group
        can be executed in parallel since they have no data dependencies
        (they only depend on lower-level nodes).

        Args:
            node_plans: Per-node plans with dependency_level computed.

        Returns:
            Tuple of (parallel_groups, max_parallelism):
            - parallel_groups: List of node ID lists, indexed by level
            - max_parallelism: Maximum number of nodes in any single group
        """
        # Empty graph: no groups, zero parallelism
        if not node_plans:
            return [], 0

        # Group nodes by dependency level
        level_groups: Dict[int, List[str]] = {}
        for node_id, np in node_plans.items():
            level = np.dependency_level
            if level not in level_groups:
                level_groups[level] = []
            level_groups[level].append(node_id)

        # Convert to sorted list (level 0 first)
        max_level = max(level_groups.keys()) if level_groups else 0
        parallel_groups: List[List[str]] = []
        for level in range(max_level + 1):
            group = level_groups.get(level, [])
            parallel_groups.append(group)

        # Compute max parallelism
        max_parallelism = max(len(group) for group in parallel_groups) if parallel_groups else 1

        return parallel_groups, max_parallelism

    # ── Tiling Analysis (v3) ──

    def _get_output_dimensions(self, snapshot: GraphSnapshot,
                               node_plans: Dict[str, NodePlan]) -> Tuple[int, int]:
        """Determine output dimensions from the graph snapshot.

        Checks OutputNode parameters for custom dimensions, or falls back
        to color input dimensions.

        Args:
            snapshot: Graph state.
            node_plans: Per-node plans.

        Returns:
            (width, height) tuple in pixels.
        """
        for nid, np in node_plans.items():
            if is_output_node_type(np.node_type):
                ns = snapshot.nodes.get(nid)
                if ns:
                    # Prefer format_mode, fall back to legacy resolution_mode
                    mode = ns.parameters.get("format_mode",
                                             ns.parameters.get("resolution_mode", "auto"))
                    if mode in ("custom", "project"):
                        w = max(1, int(ns.parameters.get("width", 1920)))
                        h = max(1, int(ns.parameters.get("height", 1080)))
                        return w, h
                    else:
                        # Auto / input: use color input dimensions
                        w = snapshot.color_width or 1920
                        h = snapshot.color_height or 1080
                        return max(1, w), max(1, h)
        # No output node found, use color dimensions
        w = snapshot.color_width or 1920
        h = snapshot.color_height or 1080
        return max(1, w), max(1, h)

    def _compute_total_padding(self, snapshot: GraphSnapshot,
                               node_plans: Dict[str, NodePlan]) -> int:
        """Compute total padding needed for spatial effects in the graph.

        Uses TilePaddingComputer from tile_padding_computer.py to analyze
        the graph and determine the maximum padding needed at source level.
        Also computes recommended tile size based on GPU memory budget.

        Args:
            snapshot: Graph state.
            node_plans: Per-node plans (for node type lookup).

        Returns:
            Total padding in pixels.
        """
        from ..core.tile_padding_computer import TilePaddingComputer

        padding_info = TilePaddingComputer.compute(snapshot)
        return padding_info.total_padding

    def _analyze_tiling_needs(
        self,
        output_w: int,
        output_h: int,
        source_count: int,
        effect_count: int,
        node_count: int,
        render_mode: str
    ) -> Tuple[bool, int, float]:
        """Analyze whether tiling is needed for the given output dimensions.

        Uses GPUResourcePool's memory estimation to determine if full-frame
        rendering exceeds the memory budget. If so, calculates optimal tile size.

        Args:
            output_w: Output width in pixels.
            output_h: Output height in pixels.
            source_count: Number of source nodes.
            effect_count: Number of effect nodes.
            node_count: Total number of nodes.
            render_mode: "preview", "full", or "export".

        Returns:
            Tuple of (needs_tiling, tile_size, estimated_memory_mb).
        """
        from .gpu_resource_pool import GPUResourcePool

        # Default threshold for tiling (can be overridden by resource pool)
        TILE_THRESHOLD = 2048  # Images > 2048px may need tiling
        DEFAULT_TILE_SIZE = 1024

        # Simple threshold check first (fast, no GPU query needed)
        max_dim = max(output_w, output_h)
        if max_dim <= TILE_THRESHOLD:
            # Small image, no tiling needed
            estimated_mb = GPUResourcePool.estimate_render_memory(
                output_w, output_h, node_count, effect_count, source_count,
                render_mode
            ) / (1024 * 1024)
            return (False, DEFAULT_TILE_SIZE, estimated_mb)

        # Large image: estimate memory and check budget
        estimated_bytes = GPUResourcePool.estimate_render_memory(
            output_w, output_h, node_count, effect_count, source_count,
            render_mode
        )
        estimated_mb = estimated_bytes / (1024 * 1024)

        # Use a conservative budget of 1024MB (most GPUs can handle this)
        # Real budget will be queried at runtime by GPUResourcePool
        DEFAULT_BUDGET_MB = 1024

        if estimated_mb > DEFAULT_BUDGET_MB:
            # Memory exceeds budget, need tiling
            # Calculate optimal tile size based on budget
            budget_bytes = int(DEFAULT_BUDGET_MB * 1024 * 1024)
            tile_sizes = [1024, 512, 256] if max_dim > 4096 else [2048, 1024, 512, 256]
            for tile_size in tile_sizes:
                tile_estimated = GPUResourcePool.estimate_render_memory(
                    tile_size, tile_size, node_count, effect_count, source_count,
                    render_mode
                )
                if tile_estimated <= budget_bytes:
                    return (True, tile_size, estimated_mb)
            # Fallback to smallest tile size
            return (True, 256, estimated_mb)

        # Memory within budget but large image - use 2048 tile size as precaution
        return (True, 2048, estimated_mb)

    # ── ROI Propagation ──

    def _propagate_roi(self, snapshot: GraphSnapshot,
                       node_plans: Dict[str, NodePlan],
                       viewer_roi: "BBox",
                       viewer_node_id: Optional[str] = None):
        """Propagate ROI backwards from Output node to all upstream nodes.

        This determines the minimum region each node needs to compute
        to produce the requested viewer ROI, reducing unnecessary GPU work.

        Algorithm:
        1. Find the Output node and set its output_roi = viewer_roi
        2. Traverse backwards in reverse topological order
        3. For each node, use compute_input_roi() to find input regions needed
        4. Set upstream node's output_roi to the needed input region
        5. Add expansion margin for nodes that need extra pixels (Blur)

        Args:
            snapshot: Graph state.
            node_plans: Per-node plans to fill with ROI info.
            viewer_roi: The region requested for final output.
            viewer_node_id: Optional "preview any node" target. The previewed
                node is presented to the viewer just like the Output node
                (AUD-8412), so it (and by backwards propagation its whole
                upstream chain) is seeded with the viewer ROI too. Without
                this seed, nodes upstream of the preview target but not of
                the Output node get output_roi=None and fall into the
                lod_level=2 bucket meant for viewport-irrelevant nodes.
        """
        from ..core.format import BBox

        # AUD-8419: drop cache entries for nodes removed from the graph so
        # the temp-node memo cannot accumulate across graph edits.
        if len(self._roi_node_cache) > len(snapshot.nodes):
            live_ids = set(snapshot.nodes.keys())
            for stale_id in [nid for nid in self._roi_node_cache
                             if nid not in live_ids]:
                self._roi_node_cache.pop(stale_id, None)

        # Find Output node(s) - they are the starting point for ROI propagation
        output_node_ids = [
            nid for nid, np in node_plans.items()
            if is_output_node_type(np.node_type)
        ]

        if not output_node_ids:
            return

        # Build reverse dependency map: node_id -> list of downstream nodes that depend on it
        downstream_deps: Dict[str, List[Tuple[str, str]]] = {}  # node_id -> [(downstream_id, port_name)]
        for node_id, np in node_plans.items():
            for port_name, upstream_id in np.inputs.items():
                downstream_deps.setdefault(upstream_id, []).append((node_id, port_name))

        # Build forward bbox propagation cache: node_id -> estimated output bbox
        # (Forward pass to compute estimated bboxes for all nodes)
        estimated_bboxes: Dict[str, Optional[BBox]] = {}
        for node_id in snapshot.eval_order:
            np = node_plans.get(node_id)
            if np is None:
                continue

            # Gather input bboxes
            input_bboxes: Dict[str, BBox] = {}
            for port_name, upstream_id in np.inputs.items():
                upstream_bbox = estimated_bboxes.get(upstream_id)
                if upstream_bbox:
                    input_bboxes[port_name] = upstream_bbox

            # Estimate output bbox using node's compute_output_bbox method
            ns = snapshot.nodes.get(node_id)
            if ns:
                # Handle source nodes specially - they don't have compute_output_bbox
                if ns.node_type == "color_input":
                    # COLOR node bbox comes from snapshot color dimensions
                    bbox = BBox.full_frame(snapshot.color_width, snapshot.color_height)
                    estimated_bboxes[node_id] = bbox
                    np.estimated_bbox = bbox
                    np.roi_expansion = 0
                elif ns.node_type == "constant":
                    # Constant node bbox from parameters
                    width = ns.parameters.get("width", 1920)
                    height = ns.parameters.get("height", 1080)
                    bbox = BBox.full_frame(width, height)
                    estimated_bboxes[node_id] = bbox
                    np.estimated_bbox = bbox
                    np.roi_expansion = 0
                elif ns.node_type == "image_input":
                    # ImageInput node bbox from parameters
                    width = ns.parameters.get("width") or 1920
                    height = ns.parameters.get("height") or 1080
                    bbox = BBox.full_frame(width, height)
                    estimated_bboxes[node_id] = bbox
                    np.estimated_bbox = bbox
                    np.roi_expansion = 0
                else:
                    # Effect/output nodes: use compute_output_bbox method
                    node = self._create_temp_node(ns)
                    if node:
                        bbox = node.compute_output_bbox(input_bboxes, ns.parameters)
                        estimated_bboxes[node_id] = bbox
                        np.estimated_bbox = bbox
                        np.roi_expansion = node.requires_roi_expansion(ns.parameters)

        # Set Output node's output_roi to viewer_roi
        for output_id in output_node_ids:
            np = node_plans.get(output_id)
            if np:
                np.output_roi = viewer_roi

        # AUD-8412: seed the "preview any node" target with the viewer ROI
        # as well (union if it already has one from the Output chain).
        if viewer_node_id:
            preview_np = node_plans.get(viewer_node_id)
            if preview_np is not None:
                if preview_np.output_roi is None:
                    preview_np.output_roi = viewer_roi
                else:
                    preview_np.output_roi = preview_np.output_roi.union(viewer_roi)

        # Backwards ROI propagation (reverse topological order)
        for node_id in reversed(snapshot.eval_order):
            np = node_plans.get(node_id)
            if np is None or np.output_roi is None:
                continue

            ns = snapshot.nodes.get(node_id)
            if ns is None:
                continue

            # Create temp node instance to call compute_input_roi
            node = self._create_temp_node(ns)
            if node is None:
                continue

            # Gather input bboxes for this node
            input_bboxes: Dict[str, BBox] = {}
            for port_name, upstream_id in np.inputs.items():
                upstream_bbox = estimated_bboxes.get(upstream_id)
                if upstream_bbox:
                    input_bboxes[port_name] = upstream_bbox

            # Compute input ROIs needed to produce output_roi
            input_rois = node.compute_input_roi(np.output_roi, ns.parameters, input_bboxes)
            np.input_rois = input_rois

            # Propagate input ROIs to upstream nodes' output_roi
            for port_name, upstream_id in np.inputs.items():
                input_roi = input_rois.get(port_name)
                if input_roi is None or input_roi.is_empty:
                    continue

                # NOTE: DO NOT expand ROI here - compute_input_roi() already handles expansion.
                # Secondary expansion would cause Blur chains to double-expand.

                # Merge with existing ROI (union - may have multiple downstream consumers)
                upstream_np = node_plans.get(upstream_id)
                if upstream_np:
                    if upstream_np.output_roi is None:
                        upstream_np.output_roi = input_roi
                    else:
                        # Union: upstream node must provide region for all downstream needs
                        upstream_np.output_roi = upstream_np.output_roi.union(input_roi)

        # ── LOD (Level of Detail) assignment ──
        # Assign LOD level based on whether node's output_roi intersects viewer_roi.
        # Nodes outside the viewport can be rendered at lower resolution (proxy scale).
        # LOD levels:
        #   0 = full resolution (output_roi intersects viewer_roi, or is Output node)
        #   2 = quarter resolution (output_roi outside viewer_roi)
        for node_id, np in node_plans.items():
            if is_output_node_type(np.node_type):
                # Output node always full-res (displayed to user)
                np.lod_level = 0
                continue

            if np.output_roi is None or np.output_roi.is_empty:
                # No ROI assigned (not needed for current viewport) → low-res
                np.lod_level = 2
                continue

            # Check if output_roi intersects viewer_roi
            if np.output_roi.intersects(viewer_roi):
                # Inside or near viewport → full resolution
                np.lod_level = 0
            else:
                # Outside viewport → quarter resolution
                np.lod_level = 2

    def _create_temp_node(self, ns):
        """Create a temporary node instance from a NodeSnapshot for ROI computation.

        Uses the unified create_node_by_type factory from node_engine.
        This ensures all registered node types are automatically supported,
        including plugins and newly registered node types.

        AUD-8419: instances are memoized per (param_revision, parameters
        identity) — ROI planning used to instantiate a fresh Node (factory
        call + full parameter copy) for every effect node on every plan,
        twice (forward bbox pass + reverse ROI pass). Callers only read the
        instance, so reuse is safe as long as the parameter snapshot is
        unchanged.
        """
        from ..core.node_engine import create_node_by_type

        cached = self._roi_node_cache.get(ns.node_id)
        if cached is not None:
            cached_revision, cached_params, cached_node = cached
            try:
                same_params = ns.parameters is cached_params or \
                    ns.parameters == cached_params
            except (TypeError, ValueError):
                same_params = False
            if cached_revision == ns.param_revision and same_params:
                return cached_node

        node = create_node_by_type(ns.node_type, node_id=ns.node_id)
        if node is None:
            return None

        for key, val in ns.parameters.items():
            node.parameters[key] = val
        self._roi_node_cache[ns.node_id] = (
            ns.param_revision, ns.parameters, node)
        return node

    def _compute_dirty_nodes(self, snapshot: GraphSnapshot,
                             node_plans: Dict[str, NodePlan]) -> List[str]:
        """Determine which nodes need re-evaluation by comparing revisions.

        A node is dirty if:
        1. It is new (not in previous plan)
        2. Its param_revision changed
        3. Its disabled flag changed
        4. Its input connections changed (topology change)
        5. It depends on color_input and color_revision changed
        6. Any upstream output port revision changed (port-level dirty)
        7. Any upstream node is dirty (propagation)
        """
        # First plan: everything is dirty
        if not self._prev_param_revisions:
            return list(node_plans.keys())

        dirty = set()

        # 1. New or removed nodes
        for node_id in node_plans:
            if node_id not in self._prev_param_revisions:
                dirty.add(node_id)

        # 2. Param revision changes
        for node_id, ns in snapshot.nodes.items():
            if ns.param_revision != self._prev_param_revisions.get(node_id, 0):
                dirty.add(node_id)

        # 2b. P1-8: Parameter CONTENT changes. Catches edited values that
        # revision comparison cannot see (freshly deserialized graphs
        # restart param_revision at 1); unaffected branches stay clean.
        for node_id, np in node_plans.items():
            if node_id in dirty:
                continue
            if np.params_hash != self._prev_params_hash.get(node_id):
                dirty.add(node_id)

        # 3. Disabled flag changes
        for node_id, ns in snapshot.nodes.items():
            if ns.disabled != self._prev_disabled.get(node_id, False):
                dirty.add(node_id)

        # 4. Input connection changes (only when topology changed)
        if snapshot.topology_revision != self._prev_topology_revision:
            for node_id, np in node_plans.items():
                prev = self._prev_inputs.get(node_id, {})
                if np.inputs != prev:
                    dirty.add(node_id)

        # 5. Color revision changes → dirty color_input nodes
        if snapshot.color_revision != self._prev_color_revision:
            for node_id, np in node_plans.items():
                if np.node_type == "color_input":
                    dirty.add(node_id)

        # Runtime-driven source nodes depend on timeline frame, clip context,
        # decode readiness, and provider state. Those signals live outside the
        # immutable graph snapshot, so revision checks alone are insufficient.
        for node_id, np in node_plans.items():
            if np.node_type in DYNAMIC_SOURCE_NODE_TYPES:
                dirty.add(node_id)

        # P0-2: when the playhead frame changed since the last plan, mark all
        # frame-dependent nodes dirty so their animated result is recomputed.
        # Static nodes are NOT touched here — their cache survives frame moves
        # (the evaluator also keeps frame_number out of their cache key).
        # P0-04/P0-05: nodes carrying a frame remap (downstream time nodes)
        # or a layer in/out gate are frame-dependent by construction.
        if (self._prev_frame_number is not None
                and snapshot.frame_number != self._prev_frame_number):
            _frame_overrides = getattr(snapshot, "frame_overrides", None) or {}
            _frame_gates = getattr(snapshot, "layer_frame_gates", None) or {}
            for node_id, np in node_plans.items():
                if node_id in dirty:
                    continue
                if node_id in _frame_overrides or node_id in _frame_gates:
                    dirty.add(node_id)
                    continue
                ns = snapshot.nodes.get(node_id)
                if ns is not None and _snapshot_node_needs_frame(ns):
                    dirty.add(node_id)

        # 6. Port-level revision changes (fine-grained dirty detection)
        # Check if upstream output port revisions changed since last evaluation
        for node_id, ns in snapshot.nodes.items():
            if node_id in dirty:
                continue  # Already marked dirty, skip port check

            # Check each input port for upstream revision change
            for conn in snapshot.get_input_connections(node_id):
                upstream_ns = snapshot.nodes.get(conn.src_node_id)
                if upstream_ns is None:
                    continue

                # Current upstream output port revision
                current_rev = upstream_ns.port_revisions.get(conn.src_port_name, 0)

                # Cached revision from this node's last evaluation
                cached_rev = ns.upstream_port_revisions.get(conn.dst_port_name, 0)

                if current_rev != cached_rev:
                    dirty.add(node_id)
                    break  # No need to check other ports for this node

        # 7. Propagate dirty downstream via BFS
        downstream = {}
        for conn in snapshot.connections:
            downstream.setdefault(conn.src_node_id, []).append(conn.dst_node_id)

        queue = list(dirty)
        while queue:
            node_id = queue.pop()
            for child_id in downstream.get(node_id, []):
                if child_id not in dirty:
                    dirty.add(child_id)
                    queue.append(child_id)

        return sorted(dirty)

    def _update_tracking(self, snapshot: GraphSnapshot,
                         node_plans: Dict[str, NodePlan]):
        """Store current revisions for comparison in next plan generation.

        Updates port-level revision tracking for fine-grained dirty detection.
        """
        # Clean up removed nodes
        current_ids = set(snapshot.nodes.keys())
        for nid in list(self._prev_param_revisions.keys()):
            if nid not in current_ids:
                del self._prev_param_revisions[nid]
                self._prev_params_hash.pop(nid, None)
                self._params_hash_memo.pop(nid, None)
                self._prev_disabled.pop(nid, None)
                self._prev_inputs.pop(nid, None)
                self._prev_port_revisions.pop(nid, None)
                self._prev_upstream_revisions.pop(nid, None)

        # Update current state
        for node_id, ns in snapshot.nodes.items():
            self._prev_param_revisions[node_id] = ns.param_revision
            self._prev_disabled[node_id] = ns.disabled
            # Update port-level revisions
            self._prev_port_revisions[node_id] = dict(ns.port_revisions)
            self._prev_upstream_revisions[node_id] = dict(ns.upstream_port_revisions)
        for node_id, np in node_plans.items():
            self._prev_inputs[node_id] = dict(np.inputs)
            self._prev_params_hash[node_id] = np.params_hash
        self._prev_topology_revision = snapshot.topology_revision
        self._prev_color_revision = snapshot.color_revision
        # P0-2: remember the frame so the next plan can detect playhead moves.
        self._prev_frame_number = snapshot.frame_number

    def reset_dirty_tracking(self):
        """Clear all dirty tracking state. Next plan will mark everything dirty."""
        self._prev_param_revisions.clear()
        self._prev_params_hash.clear()
        # AUD-7502: dirty-state reset also invalidates the params-hash memo
        # (graph replacement makes old revisions meaningless).
        self._params_hash_memo.clear()
        self._params_hash_memo_generation = -1
        self._prev_disabled.clear()
        self._prev_inputs.clear()
        self._prev_topology_revision = -1
        self._prev_color_revision = -1
        # Clear port-level revision tracking
        self._prev_port_revisions.clear()
        self._prev_upstream_revisions.clear()
        # P0-2: forget the frame so the next plan does not mis-detect a move.
        self._prev_frame_number = None

    def prepare_for_export(self, source_scheduler: "RenderScheduler"):
        """Seed dirty tracking from an existing scheduler.

        Copies tracking state so that the first plan() call in an export
        pass can detect which nodes have actually changed since the last
        interactive render, rather than marking everything dirty.

        One-way snapshot: no shared mutable state after this call.

        Args:
            source_scheduler: The interactive renderer's scheduler to
                              copy tracking state from.
        """
        self._prev_param_revisions = dict(source_scheduler._prev_param_revisions)
        self._prev_params_hash = dict(source_scheduler._prev_params_hash)
        self._prev_disabled = dict(source_scheduler._prev_disabled)
        self._prev_inputs = {k: dict(v) for k, v in source_scheduler._prev_inputs.items()}
        self._prev_topology_revision = source_scheduler._prev_topology_revision
        self._prev_color_revision = source_scheduler._prev_color_revision
        # Copy port-level revision tracking
        self._prev_port_revisions = {k: dict(v) for k, v in source_scheduler._prev_port_revisions.items()}
        self._prev_upstream_revisions = {k: dict(v) for k, v in source_scheduler._prev_upstream_revisions.items()}
        # P0-2: copy frame tracking so the export pass inherits the current frame.
        self._prev_frame_number = source_scheduler._prev_frame_number

    # ── Request Tracking ──

    def advance_request_id(self, request_id: int):
        """Thread-safe: immediately update latest_request_id.

        Called from submit() (UI thread) so that the running evaluation
        on the render thread can see the new request_id via
        should_cancel_evaluation() and stop mid-evaluation.
        """
        with self._id_lock:
            if request_id > self._latest_request_id:
                self._latest_request_id = request_id

    @property
    def latest_request_id(self) -> int:
        """Thread-safe read of the latest request id (AUD-8206).

        External consumers (e.g. composite_renderer's AI-result handler)
        must use this accessor instead of touching the private
        ``_latest_request_id`` behind the scheduler's ``_id_lock``.
        """
        with self._id_lock:
            return self._latest_request_id

    def mark_current(self, request_or_id):
        """Record that we are now processing this request.

        Supports both RenderRequest objects and plain request_id integers
        for backward compatibility.

        Args:
            request_or_id: RenderRequest object or request_id (int).
        """
        if isinstance(request_or_id, RenderRequest):
            request = request_or_id
            # AUD-8416: all three _latest_* fields are guarded by _id_lock —
            # writing generation/color_revision outside it let readers
            # observe a torn "new request_id, old generation" combination.
            with self._id_lock:
                # Never lower: a newer request may have been submitted
                # via advance_request_id() while this old one was queued.
                self._latest_request_id = max(self._latest_request_id,
                                               request.request_id)
                self._latest_graph_generation = request.graph_generation
                self._latest_color_revision = request.color_revision
        else:
            # Legacy: just an int
            request_id = request_or_id
            with self._id_lock:
                self._latest_request_id = max(self._latest_request_id,
                                               request_id)

    def is_stale(self, request_id: int) -> bool:
        """Check if a request has been superseded by a newer one."""
        with self._id_lock:
            return request_id < self._latest_request_id

    def is_stale_request(self, request: RenderRequest) -> bool:
        """Check if a request is stale by comparing all tracking fields.

        A request is stale if:
        1. Its request_id is lower than the latest
        2. OR its graph_generation is lower than the latest
        3. OR its color_revision is lower than the latest

        Args:
            request: The RenderRequest to check.

        Returns:
            True if the request is stale and should be cancelled.
        """
        # AUD-8416: hold _id_lock for the whole comparison so the three
        # _latest_* fields are read as one consistent snapshot (matches
        # is_stale / should_cancel_evaluation locking style).
        with self._id_lock:
            if request.request_id < self._latest_request_id:
                return True
            # Graph generation: stale if newer graph was submitted
            if request.graph_generation < self._latest_graph_generation:
                return True
            # Color revision: stale if newer color state was submitted
            if request.color_revision < self._latest_color_revision:
                return True
            return False

    def should_cancel_evaluation(self, request_id: int) -> bool:
        """Check if current evaluation should be cancelled.

        Called during evaluation to detect if a newer request
        has arrived while we're processing an older one.

        Thread-safe: _latest_request_id may be updated from submit()
        on the UI thread concurrently.

        Args:
            request_id: The request_id we're currently processing.

        Returns:
            True if we should cancel and stop processing.
        """
        with self._id_lock:
            return request_id < self._latest_request_id

    def record_plan_stats(self, plan: RenderPlan):
        """Record evaluation diagnostics from a completed plan."""
        self._latest_plan = plan

    def record_stats(self, stats: EvalStats):
        """Record evaluation diagnostics (deprecated, use record_plan_stats)."""
        pass

    @property
    def latest_plan(self) -> Optional[RenderPlan]:
        return self._latest_plan

    @property
    def latest_stats(self) -> Optional[EvalStats]:
        """Deprecated: Use latest_plan instead."""
        return None
