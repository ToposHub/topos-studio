"""
BaseEvaluatorMixin - Core graph evaluation logic for composite renderers.

Provides the core GPU-based node graph evaluation methods used by both:
- CompositeRenderer (async, thread-based, for interactive preview)
- CompositeExportRenderer (synchronous, one-shot, for full-res export)

This is the main mixin that other evaluator mixins extend.
"""

import time
import logging
import hashlib
import copy
import numpy as np
import moderngl
from typing import Any, Dict, Optional, Tuple, Set

logger = logging.getLogger(__name__)


def _typed_default(value_type: str) -> Any:
    """Return a safe fallback for a serialized animation value type."""
    if value_type == "color":
        return (0.0, 0.0, 0.0, 1.0)
    if value_type == "vec2":
        return (0.0, 0.0)
    if value_type == "vec3":
        return (0.0, 0.0, 0.0)
    if value_type == "bool":
        return False
    if value_type == "option":
        return ""
    return 0.0

from ..core.graph_snapshot import GraphSnapshot, NodeSnapshot
from ..nodes.base_node import TextureResult
from ..core.format import ImageFormat, BBox, MergeGeometry
from ..render.cache_manager import CacheManager, CacheEntry
from ..render.render_scheduler import (
    DYNAMIC_SOURCE_NODE_TYPES,
    RenderPlan,
)
# Phase 2 F2-02: Derive node type mappings from capability system
# Fallback imports for source/output nodes used in _sync_render_nodes
from src.features.composite.core.node_engine import NODE_REGISTRY
from ..core.node_capabilities import (
    CACHE_OWNED_TYPES,
    PASSTHROUGH_NODE_TYPES,
    get_node_contract_class,
    is_output_node_type,
    is_source_node_type,
)
# P0-04/P0-05: frame remap + layer gate helpers
from ..core.time_remap import gate_active
from ..core.quality_profile import (
    CompositeQualityProfile,
    normalize_quality_profile,
    quality_profile_for_request,
)
from ..core.output_targets import (
    normalize_output_name,
    output_name_for_node,
    selected_output_dependency_ids,
)
# GPU-stateful source nodes (still needed for _sync_render_nodes direct instantiation)
from ..nodes.color_node import COLORNode
from ..nodes.output_node import OutputNode


def _node_needs_frame_cache(ns: NodeSnapshot) -> bool:
    """P0-2: should this node's cache key include the snapshot frame_number?

    True when the node has animated parameters (keyframe_tracks), is a
    time-dependent node type (video/timeline/time-offset...), or is a dynamic
    source node. Static nodes return False so their cache survives frame
    changes during playback.
    """
    if ns.keyframe_tracks:
        return True
    # A time-ranged node changes its active state with the playhead; do not
    # reuse a cached result across the range boundary.
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
    # C3: node-specific params can make the output frame-dependent even with
    # no keyframes/expressions (Text per-character animation reads the
    # playhead via ctx.frame_number). Ask the registered node class.
    params = getattr(ns, "parameters", None)
    if isinstance(params, dict) and params:
        cls = NODE_REGISTRY.get(ns.node_type)
        if cls is not None:
            try:
                if cls.params_require_frame_cache(params):
                    return True
            except Exception:
                pass
    if ns.node_type in DYNAMIC_SOURCE_NODE_TYPES:
        return True
    return False


def _node_time_range_active(ns: NodeSnapshot, frame_number: int) -> bool:
    """Return whether a snapshot node is active at a composition frame."""
    if getattr(ns, "node_type", "") == "output":
        return True
    time_range = getattr(ns, "time_range", None)
    if not isinstance(time_range, dict) or not time_range.get("enabled", True):
        return True
    current = int(frame_number)
    try:
        start = int(time_range.get("comp_start", current))
    except (TypeError, ValueError):
        start = current
    try:
        end = int(time_range.get("comp_end", current))
    except (TypeError, ValueError):
        end = current
    return start <= current <= end


def _source_gate_frame(snapshot: Any, node_id: str) -> int:
    """Frame a source node is effectively asked for (P0-04).

    Downstream time nodes remap the composition playhead into the source's
    request frame; the source's own time_range gate must be evaluated
    against that request, not the raw playhead.
    """
    remap = (getattr(snapshot, "frame_overrides", None) or {}).get(node_id)
    if remap is None:
        return int(snapshot.frame_number)
    try:
        return int(remap.apply(snapshot.frame_number))
    except Exception:
        return int(snapshot.frame_number)


def _runtime_input_signature(ns: NodeSnapshot, eval_ctx: Any,
                             live_node: Any = None) -> str:
    """Return a stable cache dimension for controlled runtime node inputs.

    Caption text is intentionally not copied into graph parameters: it is
    owned by Edit's cue model and arrives through CompositeRuntimeContext.
    Text nodes opt in with ``caption_input_enabled``; all other nodes keep
    the existing cache behaviour.

    File-backed self-evaluating nodes (pointcloud3d/volume3d) opt in with
    ``runtime_file_signature()`` so an on-disk change (same path, new
    mtime/size) breaks the cache instead of serving a stale render.
    """
    file_sig = ""
    if live_node is not None and hasattr(live_node,
                                         "runtime_file_signature"):
        try:
            file_sig = str(live_node.runtime_file_signature())
        except Exception:
            file_sig = ""
    if file_sig:
        return hashlib.md5(file_sig.encode("utf-8")).hexdigest()[:16]
    if ns.node_type != "text" or not bool(ns.parameters.get("caption_input_enabled", False)):
        return ""
    runtime = getattr(eval_ctx, "runtime_context", None) if eval_ctx is not None else None
    value = getattr(runtime, "caption_input", None)
    if value is None and eval_ctx is not None:
        value = getattr(eval_ctx, "caption_input", None)
    if value is None:
        return ""
    if isinstance(value, dict):
        field = str(ns.parameters.get("caption_input_field", "text"))
        payload = (field, value.get("caption_cue_id", ""), value.get(field))
    else:
        field = str(ns.parameters.get("caption_input_field", "text"))
        payload = (field, getattr(value, "caption_cue_id", ""), getattr(value, field, None))
    return hashlib.md5(repr(payload).encode("utf-8")).hexdigest()[:16]


# ── Tile Context ──

class TileContext:
    """Tile rendering context passed through graph evaluation.

    When set, source node outputs are cropped to the padded ROI so that
    all downstream nodes operate at tile resolution instead of full-frame.

    Attributes:
        padded_roi: The region of the full image this tile covers (with padding).
        output_format: The full output image format (for coordinate reference).
        tile_id: Sequential tile index (for diagnostics).
        output_roi: The region that gets stitched into final output (no padding).
        padding: Number of pixels of padding on each side.
    """
    __slots__ = ('padded_roi', 'output_format', 'tile_id', 'output_roi', 'padding')

    def __init__(self, padded_roi: BBox, output_format: ImageFormat,
                 tile_id: int = 0, output_roi: Optional[BBox] = None,
                 padding: int = 0) -> None:
        self.padded_roi = padded_roi
        self.output_format = output_format
        self.tile_id = tile_id
        self.output_roi = output_roi or padded_roi  # Default: no padding info
        self.padding = padding

    @property
    def tile_width(self) -> int:
        """Width of the tile (including padding)."""
        return self.padded_roi.width

    @property
    def tile_height(self) -> int:
        """Height of the tile (including padding)."""
        return self.padded_roi.height

    @property
    def is_full_frame(self) -> bool:
        """Check if this tile covers the entire output."""
        return (self.padded_roi.x == 0 and
                self.padded_roi.y == 0 and
                self.padded_roi.width >= self.output_format.width and
                self.padded_roi.height >= self.output_format.height)

    def get_tile_offset(self) -> tuple:
        """Get the offset of this tile in the full-frame coordinates.

        Returns:
            (x_offset, y_offset) in pixels.
        """
        return (self.padded_roi.x, self.padded_roi.y)

    def __repr__(self) -> str:
        return f"TileContext(id={self.tile_id}, roi={self.padded_roi}, padding={self.padding})"


# Node types that require async AI computation
# Phase 2 F2-02: This list is now informational only; capability system provides primary source
_AI_NODE_TYPES = {"ai_generate", "ai_inpaint", "ai_expand", "ai_remove", "denoise"}

# Phase 2 F2-02: _NODE_TYPE_MAP removed - now uses NODE_REGISTRY from node_engine.py
# Phase 2 F2-02: _PASSTHROUGH_NODE_TYPES removed - now uses PASSTHROUGH_NODE_TYPES from node_capabilities.py


def _metadata_get(meta: Any, key: str, default: Any = None) -> Any:
    """Read a field from frame metadata that may be a dict OR an object.

    Source textures carry their color metadata on ``frame_metadata``, but the
    shape differs by origin:
      - ``TimelineClipFrameProvider`` writes a plain dict
        ``{"color_primaries": ..., "color_transfer": ...}``.
      - Some legacy source nodes and ColorOutput attach objects with real
        attributes (``meta.color_primaries``).

    Using ``getattr(meta, key)`` on a dict always returns ``None`` (dicts have
    no such attribute), which silently dropped wide-gamut SDR primaries
    (BT.2020/Display P3) to BT.709 in the IDT. This helper reads either form so
    the IDT sees the real source primaries regardless of the metadata shape.
    """
    if meta is None:
        return default
    if isinstance(meta, dict):
        return meta.get(key, default)
    return getattr(meta, key, default)


def _is_effect_node(node_type: str) -> bool:
    """Check if a node type should be evaluated as an effect (returns effect marker).

    Effect nodes return `_effect_type` marker in their `_do_evaluate()`,
    requiring GPU dispatch through `_dispatch_effect()`.

    Phase 2 F2-02: Uses NODE_REGISTRY from node_engine.py as single source of truth.

    Args:
        node_type: Node type string (e.g., "blur", "grade").

    Returns:
        True if the node should be evaluated as an effect.
    """
    # Passthrough nodes are NOT effect nodes (from capability system)
    if node_type in PASSTHROUGH_NODE_TYPES:
        return False

    # GPU-stateful source nodes are NOT effect nodes
    # BUG-01 fix: Use is_source_node_type() from node_capabilities instead of undefined _GPU_STATEFUL_TYPES
    if is_source_node_type(node_type):
        return False

    # Output nodes are NOT effect nodes
    if is_output_node_type(node_type):
        return False

    # Check if node type is registered and can be instantiated
    return node_type in NODE_REGISTRY and NODE_REGISTRY[node_type] is not None


class BaseEvaluatorMixin:
    """Base mixin providing core graph evaluation methods.

    This mixin provides:
    - Snapshot-driven evaluation
    - Node synchronization
    - Effect dispatch
    - GPU helpers
    - Fused chain execution

    Other evaluator mixins extend this with specific effect implementations.
    """

    # Phase 2 F2-02: _CACHE_OWNED_TYPES removed from class
    # Now uses CACHE_OWNED_TYPES from node_capabilities.py (derived from owns_color_texture)
    # This eliminates duplicate manual lists and ensures consistency when new nodes are added.

    # ── Snapshot-Driven Evaluation ──

    def _sync_render_nodes(self, snapshot: GraphSnapshot):
        """Synchronize render-side node instances with the current snapshot.

        Creates/updates/removes GPU-stateful node instances.
        Also syncs OutputNode instances which hold _resize_prog GPU resources.
        """
        # Remove nodes no longer in snapshot
        current_ids = set(snapshot.nodes.keys())
        for nid in list(self._render_nodes.keys()):
            if nid not in current_ids:
                old_node = self._render_nodes.pop(nid)
                if hasattr(old_node, 'cleanup'):
                    old_node.cleanup()
                self._invalidate_cache_for_node(nid)

        # DEPRECATED: Update COLOR node texture state from snapshot.
        # color_input nodes are kept for backward compatibility with old graphs.
        # New graphs use ToposColorNode instead.
        for nid, ns in snapshot.nodes.items():
            if ns.node_type == "color_input":
                logger.info(
                    "Deprecated color_input node detected in graph. "
                    "Consider migrating to ToposColorNode."
                )
                node = self._render_nodes.get(nid)
                if node is None:
                    node = COLORNode(node_id=nid)
                    self._render_nodes[nid] = node
                node.set_color_result(
                    snapshot.color_texture_id,
                    snapshot.color_width,
                    snapshot.color_height,
                    revision=snapshot.color_revision,
                )

        # Create/update other GPU-stateful source nodes (constant, image_input,
        # video_frame, timeline_clip_input, etc.)
        # Uses is_source_node_type() so that any new source node registered in
        # the capability table is automatically picked up without code changes.
        # color_input is handled separately above because it needs texture injection.
        for nid, ns in snapshot.nodes.items():
            if is_source_node_type(ns.node_type) and ns.node_type != "color_input":
                existing = self._render_nodes.get(nid)
                if existing is None:
                    # Phase 2 F2-02: Use NODE_REGISTRY instead of deprecated _NODE_TYPE_MAP
                    cls = NODE_REGISTRY.get(ns.node_type)
                    if cls:
                        existing = cls(node_id=nid)
                        self._render_nodes[nid] = existing
                # Sync parameters
                if existing:
                    for key, val in ns.parameters.items():
                        if existing.parameters.get(key) != val:
                            # fps=0 是 UI 侧节点的未初始化哨兵值（PARAM_DEFS
                            # 默认 0.0；拖拽 probe 失败/编程创建/旧工程的节点
                            # 都停在 0）。渲染实例打开素材后已回写真实帧率
                            # （_ensure_clip_open），若放任 0 覆盖，每次求值都
                            # 会把帧率打回 0，_compute_source_frame 随即把所有
                            # 播放请求映射到源第 0 帧（卡顿/停滞根因）。
                            if key == "fps" and not val:
                                continue
                            existing.set_parameter(key, val)
                    # VideoFrameNode resolves the playhead relative to its
                    # comp_start. Keep the render-side instance in lockstep
                    # with the immutable snapshot used for gating.
                    existing.time_range = copy.deepcopy(ns.time_range)
                    existing.disabled = bool(ns.disabled)

            # Sync terminal output instances. OutputNode and FileOutputNode
            # both own render-thread resources and must persist across frames.
            elif is_output_node_type(ns.node_type):
                existing = self._render_nodes.get(nid)
                if existing is None:
                    cls = (OutputNode if ns.node_type == "output"
                           else NODE_REGISTRY.get(ns.node_type))
                    if cls is not None:
                        existing = cls(node_id=nid)
                        self._render_nodes[nid] = existing
                # Sync parameters; invalidate cache if resolution params changed
                params_changed = False
                if existing is not None:
                    for key, val in ns.parameters.items():
                        if existing.parameters.get(key) != val:
                            existing.parameters[key] = val
                            params_changed = True
                    existing.disabled = ns.disabled
                if params_changed:
                    self._invalidate_cache_for_node(nid)

    def consume_node_diagnostics(self) -> list:
        """Take (and clear) node-level diagnostics from the last evaluation.

        T-P1-60：结构化条目形如
        ``{"code": "MODEL_UNAVAILABLE", "node_id": ..., "node_type": ...}``。
        由导出读回路径调用；交互路径不消费、随下次求值重置。
        """
        diagnostics = getattr(self, "_node_diagnostics", None)
        self._node_diagnostics = []
        return list(diagnostics or [])

    def _evaluate_snapshot(self, request_id: int = 0,
                           render_mode: str = "preview",
                           viewer_roi: Optional["BBox"] = None,
                           request_timestamp: float = 0.0,
                           tile_context: Optional["TileContext"] = None,
                           interaction_active: bool = False,
                           viewer_node_id: Optional[str] = None,
                           viewer_node_port: Optional[str] = None,
                           quad_view: bool = False,
                           single_ortho_view: Optional[int] = None,
                           quad_active_view: Optional[int] = None,
                           selected_3d_node_id: Optional[str] = None,
                           isolate_3d_node_id: Optional[str] = None,
                           proxy_scale: float = 1.0,
                           quality_profile: str = "",
                           output_name: str = "",
                           output_node_id: Optional[str] = None) -> Optional[TextureResult]:
        """Evaluate the graph from the current snapshot.

        Plan-driven: generates a RenderPlan first (static analysis),
        then executes it with content-addressable caching.

        Args:
            request_id: Request ID for diagnostics.
            render_mode: "preview", "full", or "export".
            viewer_roi: Optional region of interest for the output.
            request_timestamp: RenderRequest.timestamp for latency measurement.
            tile_context: When set, source nodes are cropped to the tile ROI
                          and all downstream nodes operate at tile resolution.
            interaction_active: True while a viewer handle is being dragged.
                Raises the eval-cache budget for this frame (idempotent toggle
                via CacheManager.begin/end_interaction) so per-frame parameter
                changes don't thrash the cache and trigger real GL texture
                allocation jitter. Defaults False; non-drag frames restore the
                baseline budget and reclaim any drag residue.
            viewer_node_id: If set, return this node's output instead of the
                Output node's ``display`` port (Fusion/Nuke-style node preview).
                The whole graph is still evaluated normally; only the returned
                result changes. None ⇒ legacy Output behaviour.
            viewer_node_port: Explicit output port name; None ⇒ first image
                output port of the previewed node.
            quad_view: M2 四视图开关。True 时本帧需额外渲染 TOP/RIGHT/FRONT 三个
                正交视图纹理（由 Scene3D 据此决定）。默认 False（仅透视）。
            single_ortho_view: M2 单视图正交 override（0/None=perspective，
                1=top，2=right，3=front）。非 0 时 Scene3D 用正交投影渲染主纹理。
            proxy_scale: 预览代理分辨率（1.0=全分辨率，0.5=半分辨率，0.25=1/4）。
                仅在非 EXPORT 模式下生效，推导为 force_lod 传入 plan()，降低整图
                渲染分辨率以加速交互。导出路径恒为 1.0，保证原始分辨率不降级。
            quality_profile: Semantic draft/preview/final policy label. Empty
                derives from render_mode and proxy_scale for legacy callers.
            output_name: Named terminal output target; empty means ``display``.
            output_node_id: Optional terminal node id used to disambiguate
                duplicate output names.
        """
        snapshot = self._snapshot
        if snapshot is None:
            return None

        # T-P1-60：本轮求值的节点级结构化诊断（如 AI 占位节点缺模型包）。
        # 导出读回路径经 consume_node_diagnostics() 收进 ExportResult；
        # 交互路径丢弃，无额外开销。
        self._node_diagnostics = []

        cache_mgr: CacheManager = self._cache_manager

        if quality_profile:
            quality_profile = normalize_quality_profile(quality_profile)
        else:
            quality_profile = quality_profile_for_request(
                render_mode, proxy_scale)
        if str(render_mode).lower() in {"full", "export"}:
            quality_profile = CompositeQualityProfile.FINAL.value
        output_name = normalize_output_name(output_name)

        # P2-05: named terminal outputs have independent dependency cones.
        # Resolve the selected cone once so unrelated branches do not consume
        # GPU time or populate cache entries during a targeted preview/export.
        # A missing target keeps the legacy full-graph fallback, allowing the
        # normal output-selection diagnostics to handle the invalid request.
        selected_dependencies = selected_output_dependency_ids(
            snapshot.nodes.values(), snapshot.connections,
            output_name=output_name, output_node_id=output_node_id)

        def _is_selected_output(ns: NodeSnapshot) -> bool:
            if output_node_id and ns.node_id != output_node_id:
                return False
            return output_name_for_node(ns) == output_name

        # Tell cache manager which nodes are current (for stale entry eviction)
        cache_mgr.set_current_nodes(set(snapshot.nodes.keys()))

        # P1-2 fix: Clear pinned set at frame start (prevents premature eviction)
        cache_mgr.begin_frame()

        # P1-8: stamp the real frame number so get()/put() tag entries with
        # last_used_frame. Without this the stamp stays 0 forever and the
        # frame-scoped sweep (retain_frame) cannot tell this frame's warm
        # entries from stale ones.
        cache_mgr.set_frame_number(
            int(getattr(snapshot, "frame_number", 0) or 0))

        # 阶段七双轨制：清空 scene 数据旁路。每帧重新填充——scene payload
        # 含 GL 对象引用，跨帧复用会读到已释放资源。Camera3D/Light3D/Geo3D/
        # Model3D 源节点在 _do_evaluate 内写入；Scene3D 聚合器读取。
        if hasattr(self, '_scene_payloads'):
            self._scene_payloads.clear()

        # P1.5 端口感知连接映射：从 snapshot 连接构建 {node_id: {port: [upstream_id, ...]}}。
        # 只记录 scene 类型输入端口（v8：单一 `in` 端口）。value 是**列表**，因为
        # 单个 `in` 端口可接多个源（摄影机+灯光+多个模型），单值会被覆盖丢连接。
        # Scene3D 用它做端口感知 payload 收集——断开的场景节点不再影响渲染。
        # 赋值到 eval_ctx（若有）；每帧重建（连接可能变了）。
        _scene_ports = ("in",)
        scene_connections: Dict[str, Dict[str, list]] = {}
        for conn in snapshot.connections:
            if conn.dst_port_name in _scene_ports:
                scene_connections.setdefault(conn.dst_node_id, {}) \
                                 .setdefault(conn.dst_port_name, []) \
                                 .append(conn.src_node_id)
        # New 3D architecture: every Scene3D is a strict renderer for its
        # connected scene sources. If a Scene3D has no scene connections, keep
        # an explicit empty entry so it does not fall back to globally scanning
        # every Camera3D/Light3D/Geo3D payload in the graph. That fallback was
        # the reason an unconnected newly-created Camera could unexpectedly
        # take over the virtual 3D Viewer camera.
        for node_id, ns in snapshot.nodes.items():
            if getattr(ns, "node_type", None) == "scene3d":
                scene_connections.setdefault(node_id, {})
        eval_ctx_ref = getattr(self, '_eval_ctx', None)
        if eval_ctx_ref is not None:
            eval_ctx_ref.frame_number = int(snapshot.frame_number)
            eval_ctx_ref.quality_profile = quality_profile
            eval_ctx_ref.scene_connections = scene_connections
            # P0-04: per-node frame remaps from downstream time nodes —
            # frame-reading sources (video_frame) apply these to the
            # playhead before resolving a decode frame.
            eval_ctx_ref.node_frame_overrides = (
                getattr(snapshot, "frame_overrides", None) or {})
            # P7: 同步交互态到 eval_ctx（Scene3D 据此做导航 LOD 降质）
            eval_ctx_ref.interaction_active = bool(interaction_active)
            # M2: 同步四视图开关（Scene3D 据此决定是否渲染正交视图纹理）
            eval_ctx_ref.quad_view = bool(quad_view)
            # M2: 同步单视图正交 override（Scene3D 据此决定主纹理用透视还是正交）
            eval_ctx_ref.single_ortho_view = (int(single_ortho_view)
                                              if single_ortho_view else None)
            # Milestone C / P2：同步四视图活动视图（Scene3D 据此对非活动正交视图
            # 降质/降频；导航态时非活动视图可跳过，保活动视图流畅）。
            eval_ctx_ref.quad_active_view = (int(quad_active_view)
                                             if quad_active_view else None)
            # P7：同步选中 / 隔离节点 id（Scene3D outline/isolate 渲染用）。
            eval_ctx_ref.selected_3d_node_id = selected_3d_node_id or None
            eval_ctx_ref.isolate_3d_node_id = isolate_3d_node_id or None
            # P7：每帧清空 ID picking 旁路（上帧的 glo/pick_map 可能已失效）
            eval_ctx_ref.id_pick_info = None
            # M2: 每帧清空正交纹理旁路（上帧的 glo 可能已失效）
            eval_ctx_ref.ortho_view_textures = None

        # Interaction mode: raise cache budget for the duration of a handle
        # drag. Idempotent — begin/end are no-ops if already in the matching
        # state, so per-frame hint-driven toggling is safe even when the UI
        # flag and the queued request are slightly out of phase.
        if interaction_active:
            cache_mgr.begin_interaction()
        else:
            cache_mgr.end_interaction()

        # Ensure eval cache is initialized
        if not hasattr(self, '_node_cache_keys'):
            self._node_cache_keys: Dict[str, str] = {}

        # Phase 1: Generate execution plan (pure analysis, no GPU)
        # 预览质量切换：proxy_scale → force_lod。守卫：EXPORT 恒走全分辨率。
        force_lod = 0
        if render_mode != "export" and proxy_scale < 1.0:
            force_lod = 1 if proxy_scale >= 0.4 else 2   # 0.5→lod1, 0.25→lod2
        plan: RenderPlan = self._scheduler.plan(
            snapshot, render_mode, viewer_roi, force_lod=force_lod,
            viewer_node_id=viewer_node_id)  # AUD-8412 接线（wd7-g39）

        # M08.1c：烘焙陈旧徽标——plan 审计结果（预览/导出同一数据源）
        # 推到 UI；按签名去重防每帧刷屏。
        bake_sig = tuple(
            (e.get("bake_node_id"), e.get("stale"))
            for e in plan.bake_staleness)
        if bake_sig != getattr(self, "_last_bake_staleness_sig", None):
            self._last_bake_staleness_sig = bake_sig
            emitter = getattr(self, "bake_staleness_changed", None)
            if emitter is not None:
                try:
                    emitter.emit(list(plan.bake_staleness))
                except Exception:  # noqa: BLE001 — 徽标失败不影响渲染
                    logger.debug("bake staleness badge emit failed",
                                 exc_info=True)

        # Phase 1.5: Fusion analysis — detect fusible chains
        from ..utils.pass_fusion import (
            analyze_fusion, get_fusion_mode_from_config,
        )
        # The user's pass_fusion_mode preference (off/conservative/
        # aggressive) arrives through the composite config manager —
        # honour it instead of silently pinning the default.
        fusion_cfg: dict = {}
        cfg_mgr = getattr(getattr(self, '_eval_ctx', None),
                          'config_manager', None)
        if cfg_mgr is not None:
            try:
                mode_val = cfg_mgr.get('pass_fusion_mode')
                if mode_val:
                    fusion_cfg['pass_fusion_mode'] = str(mode_val)
            except Exception:
                pass
        fusion_plan = analyze_fusion(
            plan, snapshot,
            fusion_mode=get_fusion_mode_from_config(fusion_cfg))
        fused_count = len(fusion_plan.fused_chains)
        fused_node_count = sum(len(c.node_ids) for c in fusion_plan.fused_chains)

        # Phase 2: Execute plan
        cache: Dict[str, Dict[str, TextureResult]] = {}
        output_result = None
        eval_start = time.perf_counter()
        dirty_set = set(plan.dirty_node_ids)
        dynamic_cache_bypass_nodes = self._compute_dynamic_cache_bypass_nodes(snapshot)
        self._dynamic_cache_bypass_nodes = dynamic_cache_bypass_nodes
        self._transient_texture_ids_to_release: Set[int] = set()

        # ── Port revision tracking for fine-grained dirty detection ──

        # Performance monitoring — begin frame
        perf = getattr(self, '_perf_monitor', None)
        if perf:
            perf.begin_frame(request_timestamp)

        # Tile context — stored for access by effect dispatch methods
        self._tile_context = tile_context

        for node_id in plan.eval_order:
            np = plan.node_plans.get(node_id)
            if np is None:
                continue

            ns = snapshot.nodes[node_id]
            if (selected_dependencies is not None
                    and node_id not in selected_dependencies):
                cache[node_id] = {}
                continue
            # ── Layer in/out frame gate (P0-05) ──
            # A node claimed by a layer whose [in_frame, out_frame] excludes
            # the current composition frame produces no output — the layer is
            # hidden. Applies to any node kind (root effect + media source),
            # mirroring the source time_range gate below.
            _gate = (getattr(snapshot, "layer_frame_gates", None) or {}).get(node_id)
            if _gate is not None and not gate_active(_gate, snapshot.frame_number):
                cache[node_id] = {}
                continue

            if (np.action == "source"
                    and not _node_time_range_active(
                        ns, _source_gate_frame(snapshot, node_id))):
                # Source nodes do not go through _create_ephemeral_node(), so
                # gate them explicitly before cached output can leak a
                # trimmed frame into the composition. The gate frame is the
                # remapped request frame (P0-04): the source is active only
                # while the frame actually being requested from it lies
                # inside its time_range.
                cache[node_id] = {}
                continue

            # ── Dirty-skip: Skip non-dirty nodes entirely ──
            if node_id not in dirty_set:
                cached_outputs = self._get_cached_outputs_for_node(node_id, snapshot)
                if cached_outputs is not None:
                    cache[node_id] = cached_outputs
                    plan.cache_hits += 1
                    plan.cached_node_ids.append(node_id)
                    # Re-pin cache entry to protect textures from eviction
                    # during this frame's evaluation
                    cache_key = self._node_cache_keys.get(node_id)
                    if cache_key:
                        cache_mgr.pin(cache_key)
                    if is_output_node_type(np.node_type):
                        output_result = cached_outputs.get("display")
                    continue

            # ── Stale request check ──
            if hasattr(self._scheduler, 'should_cancel_evaluation'):
                if self._scheduler.should_cancel_evaluation(request_id):
                    for tex in cache.get(node_id, {}).values():
                        if isinstance(tex, TextureResult) and tex.texture_id > 0:
                            if tex.texture_id not in self._get_source_texture_ids():
                                self._resource_pool.release_texture_by_id(tex.texture_id)
                    if perf:
                        perf.record_dropped()
                    logger.debug(f"req={request_id} cancelled (stale), stopping at {node_id}")
                    return None

            # Placeholder nodes — P0-3: pass the first valid input through
            # to the node's output ports instead of skipping with an empty
            # cache entry. An empty entry silently disconnected everything
            # downstream (black comp_clip output whenever an unimplemented
            # node — e.g. ai_generate without a model — sat in the graph).
            # Nodes with no valid input keep the old empty-entry behaviour.
            if np.action == "placeholder":
                # T-P1-60：AI 占位节点缺模型包 → 结构化 MODEL_UNAVAILABLE
                # 诊断，不再只留"无解释 None/黑框"让上层猜。
                if get_node_contract_class(np.node_type) == "ai_model":
                    self._node_diagnostics.append({
                        "code": "MODEL_UNAVAILABLE",
                        "node_id": node_id,
                        "node_type": np.node_type,
                    })
                placeholder_through: Optional[TextureResult] = None
                for conn in snapshot.get_input_connections(node_id):
                    tex = cache.get(conn.src_node_id, {}).get(
                        conn.src_port_name)
                    if (isinstance(tex, TextureResult) and tex.is_valid):
                        placeholder_through = tex
                        break
                if placeholder_through is not None:
                    ns_ph = snapshot.nodes.get(node_id)
                    out_ports = [name for name, *_ in (
                        getattr(ns_ph, "output_ports", None) or [])] \
                        or ["result"]
                    cache[node_id] = {
                        name: placeholder_through for name in out_ports}
                else:
                    cache[node_id] = {}
                continue

            # ── Fusion: skip nodes inside a fused chain ──
            chain = fusion_plan.node_to_chain.get(node_id)
            if chain is not None:
                if chain.node_ids[0] == node_id:
                    try:
                        chain_result = self._execute_fused_chain(
                            chain, snapshot, cache, plan,
                            render_mode, cache_mgr, request_id)
                        if chain_result is not None:
                            last_id = chain.node_ids[-1]
                            cache[last_id] = chain_result
                            last_np = plan.node_plans.get(last_id)
                            if last_np and is_output_node_type(last_np.node_type):
                                display = chain_result.get("display")
                                if display and isinstance(display, TextureResult) and display.is_valid:
                                    output_result = display
                    except Exception as e:
                        logger.warning(f"Fused chain error: {e}")
                        for nid in chain.node_ids:
                            cache[nid] = {}
                continue

            # Gather inputs
            input_textures: Dict[str, TextureResult] = {}
            for conn in snapshot.get_input_connections(node_id):
                src_outputs = cache.get(conn.src_node_id, {})
                if conn.src_port_name in src_outputs:
                    input_textures[conn.dst_port_name] = src_outputs[conn.src_port_name]

            # ── LOD application ──
            lod_level = np.lod_level
            working_inputs = input_textures
            if lod_level > 0:
                working_inputs = self._apply_lod_to_inputs(input_textures, lod_level)
            elif lod_level == 0 and input_textures:
                first_tex = next(iter(input_textures.values()), None)
                if first_tex and isinstance(first_tex, TextureResult) and first_tex.is_valid:
                    ref_bbox = np.estimated_bbox
                    if ref_bbox is not None and not ref_bbox.is_empty:
                        full_w, full_h = ref_bbox.width, ref_bbox.height
                        if first_tex.width < full_w * 0.5 or first_tex.height < full_h * 0.5:
                            working_inputs = {}
                            for port_name, tex in input_textures.items():
                                if isinstance(tex, TextureResult) and tex.is_valid:
                                    working_inputs[port_name] = self._upsample_texture(tex, full_w, full_h)
                                else:
                                    working_inputs[port_name] = tex

            # Compute cache key
            ns = snapshot.nodes[node_id]
            bypass_persistent_cache = node_id in dynamic_cache_bypass_nodes
            node_roi = np.output_roi if np.output_roi and not np.output_roi.is_empty else None
            # Scene 消费节点（Scene3D）：收集上游 scene payload 签名进 cache key，
            # 这样 Deformer3D/Model3D 参数变化 → payload signature 变 → cache miss
            # → 重渲染。修复 scene→scene 链路参数变化无反应的 bug。
            # 注：必须用节点实例（self._render_nodes）而非 NodePlan(np)，
            # 因为 _collect_connected_scene_payloads 是 Node 基类方法。
            scene_input_sig = ""
            if getattr(ns, "node_type", "") == "scene3d":
                scene_node = self._render_nodes.get(node_id)
                if scene_node is not None:
                    scene_input_sig = self._collect_scene_input_sig(scene_node)
            # P0-04/P0-05: nodes under a frame remap or a layer frame gate are
            # frame-dependent — their result changes with the playhead even
            # when the node itself is static. The remap signature must be in
            # the key so editing a time node's offset invalidates the source
            # cache at the same playhead frame.
            _node_remap = (getattr(snapshot, "frame_overrides", None) or {}) \
                .get(node_id)
            _node_gated = node_id in (
                getattr(snapshot, "layer_frame_gates", None) or {})
            cache_key = cache_mgr.compute_key(
                ns, working_inputs,
                render_mode=render_mode,
                snapshot_color_id=snapshot.color_texture_id,
                snapshot_color_rev=snapshot.color_revision,
                roi=node_roi,
                lod_level=lod_level,
                scene_input_sig=scene_input_sig,
                # P0-2: include frame_number ONLY for frame-dependent nodes so
                # their cached result is not reused across frames. Passing it
                # unconditionally would thrash the cache for every static node
                # on each playhead tick. requires_frame_number() covers
                # video/timeline/time-offset nodes; keyframe_tracks covers
                # animated parameters.
                frame_number=(snapshot.frame_number
                              if (_node_needs_frame_cache(ns)
                                  or _node_remap is not None or _node_gated)
                              else None),
                frame_remap_sig=(_node_remap.signature()
                                 if _node_remap is not None else ""),
                runtime_input_sig=_runtime_input_signature(
                    ns, getattr(self, '_eval_ctx', None),
                    live_node=self._render_nodes.get(node_id)),
                quality_profile=quality_profile,
            )

            # Cache hit — skip evaluation
            node_start = time.perf_counter()
            entry = None if bypass_persistent_cache else cache_mgr.get(cache_key)
            if entry is not None:
                plan.cache_hits += 1
                plan.cached_node_ids.append(node_id)
                cache[node_id] = entry.outputs
                self._node_cache_keys[node_id] = cache_key
                if is_output_node_type(np.node_type) and _is_selected_output(ns):
                    output_result = entry.outputs.get("display")
                node_ms = (time.perf_counter() - node_start) * 1000.0
                if perf:
                    perf.record_node_eval(node_id, np.node_type, node_ms, True, False)
                continue

            # ── A6: node disk-cache layer (read-back fill) ──
            # Opt-in via the node's cache_to_disk param; effect nodes only,
            # full-frame evaluations (tile/ROI results are not disk-safe).
            # A disk hit must not also count as a cache miss, so the miss
            # counter only advances once this layer declined the key.
            if (not bypass_persistent_cache and tile_context is None
                    and np.action == "effect"
                    and not (getattr(plan, "roi_enabled", False)
                             and plan.viewer_roi is not None)
                    and isinstance(ns.parameters, dict)
                    and ns.parameters.get("cache_to_disk")):
                disk_outputs = self._load_disk_cache_outputs(
                    ns, cache_key, working_inputs)
                if disk_outputs is not None:
                    plan.cache_hits += 1
                    if node_id not in plan.cached_node_ids:
                        plan.cached_node_ids.append(node_id)
                    cache[node_id] = disk_outputs
                    self._node_cache_keys[node_id] = cache_key
                    disk_entry = CacheEntry(
                        outputs=disk_outputs,
                        owned_texture_ids=self._collect_owned_texture_ids(
                            ns, disk_outputs, working_inputs))
                    cache_mgr.put(cache_key, disk_entry,
                                  render_mode=render_mode, roi=node_roi)
                    cache_mgr.pin(cache_key)
                    node_ms = (time.perf_counter() - node_start) * 1000.0
                    if perf:
                        perf.record_node_eval(node_id, np.node_type,
                                              node_ms, True, False)
                    continue

            plan.cache_misses += 1

            # AUD-5806: 求值前池内活跃纹理基线——异常路径据此做差集回滚
            # （_evaluate_node/tile 裁剪/IDT 已 acquire 的纹理此前在异常时
            # 无人释放，持续出错的节点每帧泄漏整幅纹理直到预算驱逐）。
            active_textures_before = self._snapshot_active_texture_ids()

            # Execute based on action
            try:
                if np.action == "passthrough":
                    first = next(iter(working_inputs.values()), None)
                    port_name = ns.output_ports[0][0] if ns.output_ports else "result"
                    outputs = {port_name: first} if first else {}
                else:
                    outputs = self._evaluate_node(ns, working_inputs, render_mode=render_mode)
                    outputs = self._resolve_output(ns, outputs)

                # ── Tile context: crop source node outputs ──
                # Baseline for evaluator-allocated source textures, captured
                # BEFORE tile/ROI cropping and IDT: both replace outputs with
                # freshly acquired pool textures.  Source-action nodes are
                # not in CACHE_OWNED_TYPES (their base output belongs to the
                # provider / self-managed node), so without this delta the
                # cache entry owns nothing and every frame-keyed eviction
                # leaks the crop/IDT textures as permanently-active pool
                # entries (full-frame RGBA16F per source node per frame).
                pre_source_texture_ids: Set[int] = set()
                if np.action == "source":
                    pre_source_texture_ids = self._collect_valid_texture_ids(outputs)
                if tile_context is not None and np.action == "source":
                    for port_name in list(outputs.keys()):
                        tex_result = outputs[port_name]
                        if isinstance(tex_result, TextureResult) and tex_result.is_valid:
                            outputs[port_name] = self._crop_to_tile_roi(
                                tex_result, tile_context.padded_roi)

                # ── B1: user viewer ROI — crop source outputs in preview ──
                # Mirrors the tile path with the node's propagated
                # output_roi: _propagate_roi expands it through
                # compute_input_roi() so spatial effects (blur, lens
                # distortion …) find their padding pixels inside the crop
                # instead of smearing the ROI border.  The cropped result's
                # bbox carries the (expanded) window and _render_to_output
                # composites it back into a full-frame output for display.
                # Preview only — export keeps full fidelity.
                elif (tile_context is None and np.action == "source"
                      and getattr(plan, "roi_enabled", False)
                      and plan.viewer_roi is not None):
                    crop_rect = np.output_roi
                    if crop_rect is None or crop_rect.is_empty:
                        crop_rect = plan.viewer_roi
                    for port_name in list(outputs.keys()):
                        tex_result = outputs[port_name]
                        if isinstance(tex_result, TextureResult) and tex_result.is_valid:
                            outputs[port_name] = self._crop_to_tile_roi(
                                tex_result, crop_rect)

                # ── IDT (Input Device Transform): decode sRGB source
                # outputs to scene-linear so every downstream effect and
                # merge runs in a single linear working space. This is the
                # central hook for composite color management (stage 1).
                # Only source nodes are transformed here; effect nodes
                # propagate color_space unchanged and stay linear once the
                # source is linearised.
                pre_idt_texture_ids: Set[int] = set()
                if np.action == "source":
                    pre_idt_texture_ids = self._collect_valid_texture_ids(outputs)
                    outputs = self._apply_idt_to_source_outputs(outputs)

                # P0-07: unary image operations may create a fresh colour
                # texture while leaving depth/AOV buffers unchanged. Restore
                # that auxiliary contract at the evaluator boundary so every
                # effect does not need to repeat the same metadata boilerplate.
                outputs = self._propagate_auxiliary_metadata(
                    working_inputs, outputs)

                # ── Cache validity check ──
                is_output_valid = False
                if outputs:
                    if (is_output_node_type(np.node_type)
                            and _is_selected_output(ns)):
                        display = outputs.get("display")
                        if display and isinstance(display, TextureResult) and display.is_valid:
                            is_output_valid = True
                            output_result = display
                    else:
                        for tex in outputs.values():
                            if isinstance(tex, TextureResult) and tex.is_valid:
                                is_output_valid = True
                                break

                if is_output_valid:
                    owned_ids = self._collect_owned_texture_ids(ns, outputs, working_inputs)
                    if np.action == "source" and pre_source_texture_ids:
                        # The tile/ROI crop and IDT textures are allocated by
                        # THIS evaluation (same delta the bypass branch
                        # releases via _transient_texture_ids_to_release).
                        # Attach them to the persistent entry so eviction
                        # returns them to the pool instead of leaking them.
                        post_source_ids = self._collect_valid_texture_ids(outputs)
                        owned_ids = set(owned_ids) | (
                            post_source_ids - pre_source_texture_ids
                        )
                    # Self-managed source nodes (e.g. constant) release and
                    # recreate their GL texture on content change. Stale cache
                    # entries from a previous mode/params still reference the
                    # released glo, so invalidate ALL prior entries for this
                    # node before storing the new one. Otherwise switching back
                    # to a previous mode (solid→radial→solid) would hit a stale
                    # entry whose glo now points at released/reused memory.
                    render_node = self._render_nodes.get(node_id)
                    if getattr(render_node, '_cache_invalidated_needed', False):
                        render_node._cache_invalidated_needed = False
                        # Invalidate this node AND every downstream consumer.
                        # The released glo may be referenced by cached outputs of
                        # downstream nodes — e.g. an Output node in passthrough
                        # ("input" format_mode) stores the Constant's glo verbatim
                        # in its own display TextureResult. Clearing only the
                        # source's cache entry leaves those downstream entries
                        # alive with a now-released glo; a cache hit on them
                        # (same glo+revision on a different render path, or any
                        # path where the downstream key doesn't capture the
                        # upstream texture_id) would bind the dead name and
                        # segfault. Walk the graph and drop all of them.
                        self._invalidate_cache_with_downstream(snapshot, node_id)
                    cache[node_id] = outputs
                    if bypass_persistent_cache:
                        self._transient_texture_ids_to_release.update(owned_ids)
                        if np.action == "source":
                            post_idt_ids = self._collect_valid_texture_ids(outputs)
                            self._transient_texture_ids_to_release.update(
                                tex_id for tex_id in post_idt_ids
                                if tex_id not in pre_idt_texture_ids
                            )
                        self._node_cache_keys.pop(node_id, None)
                    else:
                        new_entry = CacheEntry(outputs=outputs, owned_texture_ids=owned_ids)
                        cache_mgr.put(cache_key, new_entry, render_mode=render_mode, roi=node_roi)
                        cache_mgr.pin(cache_key)
                        # ── A6: node disk-cache write-through (opt-in) ──
                        if (tile_context is None
                                and np.action == "effect"
                                and not (getattr(plan, "roi_enabled", False)
                                         and plan.viewer_roi is not None)
                                and isinstance(ns.parameters, dict)
                                and ns.parameters.get("cache_to_disk")):
                            self._store_disk_cache_outputs(
                                ns, cache_key, outputs)
                        self._node_cache_keys[node_id] = cache_key
                else:
                    cache[node_id] = outputs

                node_ms = (time.perf_counter() - node_start) * 1000.0
                if perf:
                    perf.record_node_eval(node_id, np.node_type, node_ms, False, node_id in dirty_set)

            except Exception as e:
                node_ms = (time.perf_counter() - node_start) * 1000.0
                if perf:
                    perf.record_node_eval(node_id, np.node_type, node_ms, False, True, str(e))
                # AUD-5806: exception 级日志带堆栈（此前 warning 丢栈，
                # 违背"求值错误可定位不静默"）；并回滚本节点求值期间新
                # 获取的池纹理。
                logger.exception("Error evaluating %s (%s): %s",
                                 node_id, np.action, e)
                self._release_failed_node_textures(active_textures_before)
                cache[node_id] = {}

        # Record diagnostics
        plan.eval_time_ms = (time.perf_counter() - eval_start) * 1000.0
        self._scheduler.record_plan_stats(plan)

        # ── Update port revisions ──
        for node_id in cache:
            outputs = cache[node_id]
            ns = snapshot.nodes.get(node_id)
            if ns and outputs:
                for port_name, tex_result in outputs.items():
                    if isinstance(tex_result, TextureResult) and tex_result.is_valid:
                        current_rev = ns.port_revisions.get(port_name, 0)
                        ns.port_revisions[port_name] = current_rev + 1

                for conn in snapshot.get_input_connections(node_id):
                    upstream_ns = snapshot.nodes.get(conn.src_node_id)
                    if upstream_ns:
                        upstream_rev = upstream_ns.port_revisions.get(conn.src_port_name, 0)
                        ns.upstream_port_revisions[conn.dst_port_name] = upstream_rev

        # Performance monitoring
        if perf:
            cache_stats = cache_mgr.stats
            perf.record_frame(
                request_id=request_id or 0,
                total_eval_ms=plan.eval_time_ms,
                cache_hits=cache_stats.hits,
                cache_misses=cache_stats.misses,
                cache_evictions=cache_stats.evictions,
                active_textures=self._resource_pool.active_count(),
                vram_mb=self._resource_pool.memory_usage_mb(),
            )

        # Clean up stale cache keys
        current_ids = set(snapshot.nodes.keys())
        for nid in list(self._node_cache_keys.keys()):
            if nid not in current_ids:
                del self._node_cache_keys[nid]

        # ── F6-05: Enhanced Render Diagnostics ──
        # Log comprehensive diagnostics including dirty nodes, cache hits, node timing,
        # and texture counts for user to identify slow nodes.
        if plan.total_nodes > 0:
            # Build detailed node timing info from perf monitor
            node_timing_info = ""
            if perf and hasattr(perf, '_node_eval_records'):
                slow_nodes = []
                for record in perf._node_eval_records:
                    if record.get('eval_ms', 0) > 50:  # > 50ms is considered slow
                        slow_nodes.append(f"{record['node_type']}:{record['eval_ms']:.0f}ms")
                if slow_nodes:
                    node_timing_info = f"slow_nodes=[{', '.join(slow_nodes[:5])}] "

            fusion_info = f"fused_chains={fused_count} fused_nodes={fused_node_count} " if fused_count > 0 else ""
            tile_info = f"tile={tile_context.padded_roi} " if tile_context else ""
            lod0_count = sum(1 for np_val in plan.node_plans.values() if np_val.lod_level == 0)
            lod2_count = sum(1 for np_val in plan.node_plans.values() if np_val.lod_level == 2)
            lod_info = f"lod0={lod0_count} lod2={lod2_count} " if lod2_count > 0 else ""

            # Texture count from resource pool
            texture_info = ""
            if hasattr(self, '_resource_pool'):
                texture_info = f"active_tex={self._resource_pool.active_count()} "

            # Cache hit rate percentage
            total_cache_ops = plan.cache_hits + plan.cache_misses
            hit_rate_pct = (plan.cache_hits * 100 / total_cache_ops) if total_cache_ops > 0 else 0
            cache_info = f"hits={plan.cache_hits} misses={plan.cache_misses} rate={hit_rate_pct:.0f}% "

            logger.info(f"[Render] req={request_id} mode={render_mode} "
                        f"nodes={plan.total_nodes} dirty={len(plan.dirty_node_ids)} "
                        f"{cache_info}{texture_info}{fusion_info}{tile_info}{lod_info}"
                        f"{node_timing_info}"
                        f"total_time={plan.eval_time_ms:.1f}ms")

            # Detailed debug log for dirty and cached node IDs
            logger.debug(f"[Render] dirty_ids={plan.dirty_node_ids} cached={plan.cached_node_ids}")

        # ── Node-preview pipeline ──
        # The whole graph has been evaluated above (every node that produced a
        # valid result is in the local ``cache`` dict, including dynamic-bypass
        # nodes that intentionally bypass the persistent CacheManager). When the
        # viewer asked for a specific node's output, prefer it over the Output
        # node's ``display`` port. Falling back to ``output_result`` keeps the
        # legacy behaviour intact for missing/invalid preview targets.
        if viewer_node_id:
            vn_result = self._resolve_viewer_node_output(
                snapshot, cache, viewer_node_id, viewer_node_port,
            )
            if vn_result is not None:
                return vn_result
        return output_result

    def _resolve_viewer_node_output(
        self,
        snapshot: GraphSnapshot,
        cache: Dict[str, Dict[str, "TextureResult"]],
        viewer_node_id: str,
        viewer_node_port: Optional[str],
    ) -> Optional["TextureResult"]:
        """Return the texture to present for a node-preview request.

        Reads from the per-evaluation ``cache`` dict (covers all evaluated
        nodes, persistent-cache and bypass alike) rather than CacheManager, so
        dynamic-bypass nodes (timeline/video sources) remain previewable. Returns
        None on any miss so the caller can fall back to the Output result.
        """
        vn_outputs = cache.get(viewer_node_id)
        if not vn_outputs:
            return None
        ns = snapshot.nodes.get(viewer_node_id)
        if ns is None:
            return None
        port = viewer_node_port or self._first_image_port(ns)
        result = vn_outputs.get(port)
        if isinstance(result, TextureResult) and result.is_valid:
            return result
        return None

    def _compute_dynamic_cache_bypass_nodes(self, snapshot: GraphSnapshot) -> Set[str]:
        """Return dynamic source nodes that bypass the persistent cache.

        Timeline/video source pixels change outside the static graph snapshot,
        so the source nodes themselves must re-evaluate every request (their
        evaluation also has side effects: consuming VT pending frames and
        registering decode targets).

        Downstream consumers (blur/merge/output/...) are NOT bypassed: the
        cache key already incorporates the upstream texture id + revision, so
        when the decoded frame actually changes the key changes (cache miss,
        correct re-eval), and when playback holds the previous frame (decode
        lagging the playhead) the whole downstream chain hits cache instead
        of re-running identical GPU work. The previous "bypass everything
        downstream" behaviour re-evaluated the full chain on every tick even
        when nothing changed, which multiplied GPU load and UI submits during
        playback (~2 renders per displayed frame). Pool pressure from
        per-frame cache entries is bounded by the CacheManager LRU +
        GPUResourcePool budget eviction.
        """
        return {
            node_id
            for node_id, ns in snapshot.nodes.items()
            if ns.node_type in DYNAMIC_SOURCE_NODE_TYPES
        }

    def _collect_valid_texture_ids(self, outputs: dict) -> Set[int]:
        """Collect valid texture ids from a node output mapping."""
        texture_ids: Set[int] = set()
        for value in outputs.values():
            if isinstance(value, TextureResult) and value.is_valid:
                texture_ids.add(value.texture_id)
        return texture_ids

    def _snapshot_active_texture_ids(self) -> Set[int]:
        """AUD-5806: 当前资源池活跃纹理 glo 集合（异常回滚的差集基线）。

        池不可用/结构不符时返回空集——空基线会让差集为空（保守：不回滚
        也不误删），而不是抛错中断渲染。
        """
        pool = getattr(self, "_resource_pool", None)
        active = getattr(pool, "_active_textures", None) if pool is not None else None
        return set(active) if isinstance(active, dict) else set()

    def _release_failed_node_textures(self, active_before: Set[int]) -> None:
        """AUD-5806: 节点求值失败时归还其新获取的池纹理。

        对活跃集合求差集：本节点求值期间 acquire 且异常后仍活跃的纹理
        即为泄漏——输出已随 ``cache[node_id] = {}`` 丢弃，没有任何缓存
        条目持有它们。源节点自管纹理（image_input 等自建 GL 对象）与
        external 追踪纹理不参与回滚。
        """
        pool = getattr(self, "_resource_pool", None)
        if pool is None:
            return
        active = getattr(pool, "_active_textures", None)
        if not isinstance(active, dict):
            return
        try:
            source_ids = self._get_source_texture_ids()
        except Exception:
            # _render_nodes 不可用（组合类未初始化完成）时按空集处理：
            # 宁可少回滚（回到旧行为）也不在错误路径上再抛。
            source_ids = set()
        for tex_id in set(active) - set(active_before):
            if tex_id <= 0 or tex_id in source_ids:
                continue
            try:
                pool.release_texture_by_id(tex_id)
            except Exception as exc:
                logger.debug("Post-failure texture release failed for %s: %s",
                             tex_id, exc)

    def _release_effect_textures_on_error(self, *textures: Any) -> None:
        """AUD-5910/5930/5903: 效果方法求值中途抛错时归还已获取的池纹理。

        池纹理只有 ``release_texture*`` 能移出 active 集合（无错误路径
        清扫），uniform 类型错误/渲染异常若不在此归还，持续出错的节点
        每帧泄漏一张全幅纹理直到预算驱逐。成功路径的所有权不受影响
        （输出纹理随 TextureResult 交给缓存条目管理）。各效果 mixin 的
        except 分支调用本方法后 re-raise，由 _evaluate_snapshot 的
        AUD-5806 回滚兜底其余路径。
        """
        pool = getattr(self, "_resource_pool", None)
        if pool is None:
            return
        for tex in textures:
            if tex is None:
                continue
            try:
                pool.release_texture(tex)
            except Exception as exc:
                logger.warning("Failed to release texture after effect "
                               "failure: %s", exc)

    def _collect_scene_input_sig(self, node) -> str:
        """收集 Scene3D 上游 scene payload 的签名（进 cache key 用）。

        scene→scene 链路（如 Model3D→Deformer3D→Scene3D）中，上游参数变化
        （如 Deformer3D 的 strength）不会改变 Scene3D 的 TextureResult 输入
        （scene 端口不产 TextureResult），导致 Scene3D 的 cache key 不变、
        命中旧缓存、_do_evaluate 不被调用、viewport 无反应。

        本方法收集 Scene3D 所有上游 scene payload（Camera/Light/Geo/Model）
        的 signature() 拼成字符串，作为 cache key 的额外维度。上游参数变 →
        payload signature 变 → Scene3D key 变 → cache miss → 重渲染。

        用 payload 的 signature() 而非 payload 对象本身：signature 是稳定
        tuple（含 transform_sig/deformer 参数），可哈希；GL 对象不在其中。
        """
        eval_ctx = getattr(self, '_eval_ctx', None)
        if eval_ctx is None:
            return ""
        try:
            from src.features.composite.render3d.scene_payload import (
                CameraPayload, LightPayload, GeometryPayload, ModelPayload)
            sigs = []
            for payload_cls in (CameraPayload, LightPayload, GeometryPayload, ModelPayload):
                try:
                    payloads = node._collect_connected_scene_payloads(eval_ctx, payload_cls)
                    for p in payloads:
                        if p is not None and hasattr(p, 'signature'):
                            sigs.append(repr(p.signature()))
                except Exception:
                    pass
            return "|".join(sorted(sigs))
        except Exception:
            return ""

    def _release_transient_textures(self):
        """Release uncached textures created for a single dynamic preview frame."""
        texture_ids = getattr(self, '_transient_texture_ids_to_release', None)
        if not texture_ids:
            return

        source_ids = self._get_source_texture_ids()
        for tex_id in list(texture_ids):
            if tex_id <= 0 or tex_id in source_ids:
                continue
            try:
                self._resource_pool.release_texture_by_id(tex_id)
            except Exception as exc:
                logger.debug("Failed to release transient texture %s: %s", tex_id, exc)
        texture_ids.clear()

    def _get_cached_outputs_for_node(self, node_id: str,
                                      snapshot: GraphSnapshot) -> Optional[Dict[str, TextureResult]]:
        """Retrieve cached outputs for a non-dirty node."""
        if node_id in self._render_nodes:
            node = self._render_nodes[node_id]
            if hasattr(node, '_cached_outputs'):
                return node._cached_outputs

        cache_key = self._node_cache_keys.get(node_id)
        if cache_key:
            entry = self._cache_manager.get(cache_key)
            if entry is not None:
                return entry.outputs

        return None

    def _collect_owned_texture_ids(self, ns: NodeSnapshot, outputs: dict,
                                   input_textures: Dict[str, TextureResult]) -> set:
        """Collect texture IDs that should be owned by the cache entry.

        S1-D: Extended to include depth_texture_id ownership for deep compositing nodes.
        This ensures depth textures allocated by DeepMergeNode are properly tracked
        and released when the cache entry is evicted.

        Args:
            ns: Node snapshot with type and parameters.
            outputs: Output texture results from the node evaluation.
            input_textures: Input texture results (upstream textures).

        Returns:
            Set of texture IDs (glo) that this entry owns and should release on eviction.
            Includes both color texture IDs and depth texture IDs if present.
        """
        # Phase 2 F2-02: Use CACHE_OWNED_TYPES from capability system
        if ns.node_type not in CACHE_OWNED_TYPES:
            return set()

        # Collect upstream texture IDs (both color and depth)
        upstream_ids: Set[int] = set()
        for tex_result in input_textures.values():
            if isinstance(tex_result, TextureResult) and tex_result.is_valid:
                upstream_ids.add(tex_result.texture_id)
                # S1-D: Include upstream depth texture IDs
                if tex_result.has_depth() and tex_result.depth_texture_id is not None:
                    upstream_ids.add(tex_result.depth_texture_id)
                upstream_ids.update(
                    int(tex_id) for tex_id in tex_result.aov_texture_ids.values()
                    if int(tex_id) > 0
                )

        # Collect owned texture IDs from outputs (both color and depth)
        owned: Set[int] = set()
        for tex_result in outputs.values():
            if isinstance(tex_result, TextureResult) and tex_result.is_valid:
                # Color texture ownership
                tex_id = tex_result.texture_id
                if tex_id not in upstream_ids:
                    owned.add(tex_id)

                # S1-D: Depth texture ownership (for deep compositing)
                if tex_result.has_depth() and tex_result.depth_texture_id is not None:
                    depth_id = tex_result.depth_texture_id
                    if depth_id not in upstream_ids:
                        owned.add(depth_id)

                # Auxiliary passes are GPU resources too. Do not claim an
                # upstream Scene3D-owned AOV, but do release a pass allocated
                # by a cache-owned node if it is replaced by that node.
                for aov_id in tex_result.aov_texture_ids.values():
                    aov_id = int(aov_id)
                    if aov_id > 0 and aov_id not in upstream_ids:
                        owned.add(aov_id)

        return owned

    @staticmethod
    def _propagate_auxiliary_metadata(
            input_textures: Dict[str, TextureResult], outputs: dict) -> dict:
        """Carry depth/AOV metadata through a same-size unary image effect.

        A merge has ambiguous auxiliary-pass semantics and is intentionally
        excluded here. For unary effects, the pass textures remain in the same
        composition space as the colour result, so dropping them would make a
        downstream Output/FileOutput node silently fall back to beauty-only.
        Dimension-changing nodes are excluded because their passes need an
        explicit resampling implementation before they can be advertised.
        """
        primary = None
        for port_name in ("image", "input", "source", "input_a", "A"):
            candidate = input_textures.get(port_name)
            if isinstance(candidate, TextureResult) and candidate.is_valid:
                primary = candidate
                break
        if primary is None:
            candidates = [
                value for value in input_textures.values()
                if isinstance(value, TextureResult) and value.is_valid
            ]
            if len(candidates) == 1:
                primary = candidates[0]
        if primary is None:
            return outputs

        for port_name, output in list(outputs.items()):
            if not isinstance(output, TextureResult) or not output.is_valid:
                continue
            if (output.width, output.height) != (primary.width, primary.height):
                continue
            updates = {}
            if primary.has_depth() and not output.has_depth():
                updates.update(
                    depth_texture_id=primary.depth_texture_id,
                    has_deep_data=True,
                )
            if primary.aov_texture_ids and not output.aov_texture_ids:
                updates.update(
                    aov_texture_ids=dict(primary.aov_texture_ids),
                    aov_dimensions=dict(primary.aov_dimensions),
                )
            if primary.render_diagnostics and not output.render_diagnostics:
                updates["render_diagnostics"] = primary.render_diagnostics
            if updates:
                outputs[port_name] = output.with_updates(**updates)
        return outputs

    # ── B6: async AI node evaluation (ai_remove family) ──────────────────

    def _evaluate_ai_node(self, ns: NodeSnapshot,
                          input_textures: Dict[str, TextureResult]) -> dict:
        """Cache-first async evaluation for LaMa-family AI nodes.

        Loop: derive the cache key → on hit, composite the cached result
        over the input → on miss, hand the inputs to ``AIComputeManager``
        (deduped per cache key) and pass the input through meanwhile.  When
        the worker finishes, the page submits ``AI_RESULT_READY`` which
        re-evaluates the graph and lands on the cache-hit path.

        P2-9: the key is derived BEFORE any full-frame readback and the
        input is only read back when the masked-composite path or a fresh
        compute request actually needs CPU pixels — an in-flight request
        (or a hit with no mask) costs zero readback per frame.
        """
        image = input_textures.get("image")
        if image is None or not isinstance(image, TextureResult) or not image.is_valid:
            return {}
        mask = input_textures.get("mask")
        if mask is not None and not (isinstance(mask, TextureResult) and mask.is_valid):
            mask = None

        ai_mgr = getattr(self, '_ai_compute_manager', None)
        ai_cache = getattr(self, '_ai_cache_manager', None)
        if ai_mgr is None or ai_cache is None:
            # AI infrastructure unavailable — behave like the pass-through
            # fallback instead of dropping the node's output.
            return {"image": image} if ns.output_ports else {}

        out_name = ns.output_ports[0][0] if ns.output_ports else "image"

        params = dict(getattr(ns, "parameters", {}) or {})
        snapshot = getattr(self, '_snapshot', None)
        frame_number = getattr(snapshot, "frame_number", None)
        # P2-9: the key must not depend on reusable texture ids alone —
        # resolution, colour space and alpha semantics join
        # texture_id/revision so a recycled texture cannot alias an old
        # cache entry.
        upstream_key = (
            f"{image.texture_id}:{image.revision}"
            f":{int(image.width or 0)}x{int(image.height or 0)}"
            f":{getattr(image, 'color_space', None)}:"
            f"{'premult' if getattr(image, 'alpha_premultiplied', False) else 'straight'}"
        )
        if mask is not None:
            upstream_key += f":m{mask.texture_id}:{mask.revision}"
        cache_key = ai_cache.compute_key(
            ns.node_type, params, upstream_key=upstream_key,
            frame_number=frame_number)

        # ── Cache hit — composite the cached result over the input ──
        if ai_cache.has_cache(cache_key):
            # P2-9: only the masked-composite path needs the input pixels on
            # the CPU. Without a mask the readback served just the shape
            # check — the TextureResult dimensions cover that, so an
            # unmasked hit costs zero full-frame readback.
            img_np = None
            if mask is not None:
                img_np = self._readback_texture_fullres(image)  # BGR f32
                mask_np = self._readback_texture_fullres(mask, want_bgr=False)
                if img_np is None:
                    return {out_name: image}
                if mask_np is not None:
                    # The readback is RGB (H,W,3); every consumer (masked
                    # composite, LaMa 4-channel input) works on a single-
                    # channel (H,W) mask.
                    if mask_np.ndim == 3:
                        mask_np = mask_np[:, :, 0]
                    if mask_np.shape[:2] != img_np.shape[:2]:
                        # Model mismatch (e.g. proxy/ROI mismatch between
                        # ports) — resize rather than broadcasting garbage.
                        import cv2 as _cv2
                        mask_np = _cv2.resize(
                            mask_np, (img_np.shape[1], img_np.shape[0]),
                            interpolation=_cv2.INTER_LINEAR)
            result_np = self._read_ai_cache_result(ai_cache, cache_key,
                                                   img_np, mask_np,
                                                   ref_width=int(image.width or 0),
                                                   ref_height=int(image.height or 0))
            if result_np is not None:
                getattr(self, "_ai_read_failures", {}).pop(cache_key, None)
                tex = self._upload_ai_result_texture(result_np, image)
                if tex is None:
                    return {out_name: image}
                return {out_name: tex}
            # A cache entry that exists but cannot be read (corrupt EXR,
            # size mismatch) would otherwise re-request compute every frame
            # and re-fail forever — track and stop after two strikes for
            # this key (session lifetime).
            failures = getattr(self, "_ai_read_failures", None)
            if failures is None:
                failures = self._ai_read_failures = {}
            failures[cache_key] = failures.get(cache_key, 0) + 1

        if getattr(self, "_ai_read_failures", {}).get(cache_key, 0) >= 2:
            logger.warning("AI cache %s unreadable; skipping auto-recompute",
                           cache_key)
            return {out_name: image}
        if ai_mgr.is_processing(cache_key):
            # P2-9: the compute request for this exact key is already in
            # flight and holds the input pixels — pass through without
            # re-reading the frame.
            return {out_name: image}

        # Cache miss — read the inputs once, request async compute, and
        # pass the input through until the result lands.
        img_np = self._readback_texture_fullres(image)   # BGR float32 [0,1]
        mask_np = (self._readback_texture_fullres(mask, want_bgr=False)
                   if mask is not None else None)
        if img_np is None:
            return {out_name: image}
        if mask_np is not None:
            if mask_np.ndim == 3:
                mask_np = mask_np[:, :, 0]
            if mask_np.shape[:2] != img_np.shape[:2]:
                import cv2 as _cv2
                mask_np = _cv2.resize(
                    mask_np, (img_np.shape[1], img_np.shape[0]),
                    interpolation=_cv2.INTER_LINEAR)

        request_id = 0
        scheduler = getattr(self, '_scheduler', None)
        if scheduler is not None:
            request_id = getattr(scheduler, '_latest_request_id', 0)
        try:
            # AUD-5805: 模型输入域是 [0,1]——只在此处（送入模型前）对副本
            # clamp；掩码合成回写路径继续使用未 clamp 的线性读回值。
            ai_mgr.request_compute(
                cache_key, ns.node_type,
                {"image": np.clip(img_np, 0.0, 1.0),
                 "mask": (np.clip(mask_np, 0.0, 1.0)
                          if mask_np is not None else None)},
                params,
                request_id=request_id, node_id=ns.node_id)
        except Exception as exc:
            logger.warning("AI node %s request_compute failed: %s",
                           ns.node_id, exc)
        return {out_name: image}

    @staticmethod
    def _read_ai_cache_result(ai_cache, cache_key: str,
                              image_np: Optional["np.ndarray"],
                              mask_np: Optional["np.ndarray"],
                              ref_width: int = 0,
                              ref_height: int = 0) -> Optional["np.ndarray"]:
        """Read a cached LaMa result and apply the outside-mask guarantee.

        ``image_np`` may be None when no mask needs compositing; the cached
        frame is then validated against ``ref_width``/``ref_height`` (the
        input TextureResult's dimensions) instead. Returns BGR float32
        HxWx3 or None on any failure (treated as a cache miss by the caller).
        """
        import cv2
        path = ai_cache.get_cache_path(cache_key)
        if not path:
            return None
        cached = cv2.imread(path, cv2.IMREAD_UNCHANGED)
        if cached is None or cached.ndim != 3:
            return None
        if image_np is not None:
            ref_shape = image_np.shape[:2]
        elif ref_width > 0 and ref_height > 0:
            ref_shape = (ref_height, ref_width)
        else:
            return None
        if cached.shape[:2] != ref_shape:
            return None
        # AUD-5805: 磁盘缓存负载是模型输出域 [0,1]（写入侧即此域）——
        # 这里保留 clamp；掩码合成的另一侧 image_np 是未 clamp 的线性
        # HDR 读回值，掩码外高光不被压平。
        cached = np.clip(np.nan_to_num(cached.astype(np.float32),
                                      nan=0.0, posinf=1.0, neginf=0.0), 0.0, 1.0)
        if cached.shape[2] > 3:
            cached = cached[:, :, :3]
        if mask_np is None:
            return cached
        if mask_np.ndim == 3:
            mask_np = mask_np[:, :, 0]
        if image_np is None or mask_np.shape[:2] != image_np.shape[:2]:
            return None
        # Outside the mask the input pixels win, whatever LaMa returned.
        m = np.clip(mask_np, 0.0, 1.0)[:, :, np.newaxis]
        return image_np * (1.0 - m) + cached * m

    def _readback_texture_fullres(self, tex: TextureResult,
                                  want_bgr: bool = True,
                                  want_alpha: bool = False) -> Optional["np.ndarray"]:
        """Read a texture back to a CPU numpy array (top-left origin).

        Returns float32 HxWx3 BGR (default) or RGB when ``want_bgr=False``.
        AUD-5805: values stay in the pipeline's linear HDR domain — values
        above 1.0 are preserved (the composite working space is unclamped
        linear light; callers that need the [0,1] model domain must clamp
        their own copy). Returns None when the texture object cannot be
        resolved (caller passes the input through).
        """
        import cv2
        if tex is None or not tex.is_valid:
            return None
        w = int(tex.width) or 1
        h = int(tex.height) or 1
        gl_tex = self._get_texture_by_id(tex.texture_id)
        if gl_tex is None:
            return None
        fbo = self.fb_manager.get_fbo(gl_tex)
        fbo.use()
        self.ctx.viewport = (0, 0, w, h)
        raw = fbo.read(components=4, dtype='f4')
        arr = np.frombuffer(raw, dtype=np.float32).reshape(h, w, 4)
        if want_alpha:
            # LIB1 全帧 CPU 子门（C18/C19）：α 是 matte 源/计算载荷，
            # 4 通道直读不裁（RGB 域不变换）
            return np.flipud(arr).copy()
        arr = np.flipud(arr[:, :, :3]).copy()
        if want_bgr:
            arr = cv2.cvtColor(arr, cv2.COLOR_RGB2BGR)
        # AUD-5805: 不再无条件 np.clip(0,1)——掩码合成回写
        # (image * (1-m) + cached * m) 若使用 clamp 后的读回值，掩码内
        # >1.0 的 HDR 高光会被压平（"HDR 内部数据不得无理由 clamp"）。
        return arr

    def _upload_ai_result_texture(self, result_bgr: "np.ndarray",
                                  reference: TextureResult) -> Optional[TextureResult]:
        """Upload a BGR float32 result as an RGBA pipeline texture."""
        import cv2
        h, w = result_bgr.shape[:2]
        rgb = cv2.cvtColor(np.ascontiguousarray(result_bgr), cv2.COLOR_BGR2RGB)
        rgba = np.ones((h, w, 4), dtype=np.float32)
        rgba[:, :, :3] = rgb
        # CPU arrays are top-left; the GL pipeline samples bottom-left.
        data = np.ascontiguousarray(np.flipud(rgba))
        # R-01：统一 CPU→纹理上传——float32 写入默认 f2 纹理会
        # data-size mismatch，AI 缓存命中的结果回不到图里。
        from src.features.composite.render.gpu_resource_pool import (
            upload_pooled_texture,
        )
        try:
            tex = upload_pooled_texture(self._resource_pool, data, w, h)
        except Exception as exc:
            logger.warning("AI result upload failed: %s", exc)
            return None
        return TextureResult(
            texture_id=tex.glo,
            width=w,
            height=h,
            # AUD-5804: 透传输入 revision（此前恒为 1）——AI 结果随输入
            # 内容演进；固定 revision 下池 glo 回收复用会让下游
            # (glo, revision=1) 缓存键命中旧条目（陈旧 AI 合成结果）。
            revision=int(getattr(reference, "revision", 0) or 0),
            alpha_premultiplied=True,
        )

    def _upload_lib1_fullframe_texture(self, result_rgba: "np.ndarray",
                                       reference: TextureResult) -> Optional[TextureResult]:
        """LIB1 全帧 CPU 子门：RGBA float32 结果直传（α 即载荷，不重组）。

        CPU 数组 top-left；GL 管线 bottom-left → flipud（与 AI 上传同约定）。
        """
        h, w = result_rgba.shape[:2]
        data = np.ascontiguousarray(np.flipud(result_rgba).astype(np.float32))
        from src.features.composite.render.gpu_resource_pool import (
            upload_pooled_texture,
        )
        try:
            tex = upload_pooled_texture(self._resource_pool, data, w, h)
        except Exception as exc:
            logger.warning("LIB1 fullframe upload failed: %s", exc)
            return None
        return TextureResult(
            texture_id=tex.glo,
            width=w,
            height=h,
            revision=int(getattr(reference, "revision", 0) or 0),
            alpha_premultiplied=bool(
                getattr(reference, "alpha_premultiplied", False)),
        )

    def _evaluate_node(self, ns: NodeSnapshot, input_textures: Dict[str, TextureResult],
                       render_mode: str = "preview") -> dict:
        """Evaluate a single node from its snapshot.

        Node evaluation follows this order:
        1. GPU-stateful source nodes (color_input, constant, image_input, video_frame)
        2. Passthrough nodes (dot, noop, switch) — pass input through
        3. Effect marker nodes — create ephemeral instance for GPU dispatch
        4. Output node
        5. Unknown/unimplemented nodes — raise clear error or return empty

        Source nodes receive CompositeEvalContext (when available) instead of
        the plain moderngl.Context so they can access business-level context
        like runtime_context and timeline_clip_gpu_provider.
        """
        # GPU-stateful node (source nodes with persistent state)
        # BUG-01 fix: Use is_source_node_type() from node_capabilities instead of undefined _GPU_STATEFUL_TYPES
        if is_source_node_type(ns.node_type):
            node = self._render_nodes.get(ns.node_id)
            if node is None:
                return {}
            node.disabled = ns.disabled
            # Pass CompositeEvalContext to source nodes so they can access
            # business-level context (runtime_context, timeline_clip_gpu_provider).
            # Falls back to self.ctx when _eval_ctx is not set (backward compat).
            eval_ctx = getattr(self, '_eval_ctx', None)
            if eval_ctx is not None:
                ctx_arg = eval_ctx
            else:
                ctx_arg = self.ctx
            return node.evaluate(input_textures, ctx_arg, self.fb_manager,
                                 self._resource_pool, render_mode=render_mode)

        # Passthrough nodes — pass input through without GPU effect
        # Phase 2 F2-02: Use PASSTHROUGH_NODE_TYPES from capability system
        if ns.node_type in PASSTHROUGH_NODE_TYPES:
            # Create ephemeral node to handle parameters (e.g., switch which input)
            node = self._create_ephemeral_node(ns)
            if node:
                # Let the node's _do_evaluate handle passthrough logic
                # dot: returns first input
                # noop: returns first input
                # switch: returns selected input based on 'which' param
                return node.evaluate(input_textures, self.ctx, self.fb_manager,
                                     self._resource_pool, render_mode=render_mode)
            # Fallback: if node creation fails, return first input
            first_input = next(iter(input_textures.values()), None)
            if first_input and isinstance(first_input, TextureResult) and first_input.is_valid:
                port_name = ns.output_ports[0][0] if ns.output_ports else "result"
                return {port_name: first_input}
            return {}

        # B6: AI nodes — async cache-first compute. Reached only for AI
        # types whose placeholder flag has been lifted (ai_remove today).
        if ns.node_type in _AI_NODE_TYPES:
            return self._evaluate_ai_node(ns, input_textures)

        # Effect marker nodes — create ephemeral instance for GPU dispatch
        if _is_effect_node(ns.node_type):
            node = self._create_ephemeral_node(ns)
            if node:
                # AUD-7101: effect nodes used to receive the bare moderngl
                # context, so frame-aware nodes (noise's animate/u_time)
                # always resolved frame_number=0 and never animated in the
                # production preview path. Pass the same CompositeEvalContext
                # the source path above uses — it carries the live
                # frame_number (written per snapshot in _evaluate_snapshot).
                # Safe for every effect node: their _do_evaluate bodies are
                # marker packers that never touch GL on ctx (surveyed), and
                # getattr-based readers keep working on the bare fallback.
                eval_ctx = getattr(self, '_eval_ctx', None)
                ctx_arg = eval_ctx if eval_ctx is not None else self.ctx
                return node.evaluate(input_textures, ctx_arg, self.fb_manager,
                                     self._resource_pool, render_mode=render_mode)
            # Phase 2 F2-02: Node creation failed — shouldn't happen if registered in NODE_REGISTRY
            logger.warning(f"Failed to create ephemeral node for type '{ns.node_type}'")
            return {}

        # Output node
        if is_output_node_type(ns.node_type):
            node = self._render_nodes.get(ns.node_id)
            if node is None:
                return {}
            return node.evaluate(input_textures, self.ctx, self.fb_manager,
                                 self._resource_pool, render_mode=render_mode)

        # Unknown/unimplemented node type
        # S1-B: 对未实现节点不要静默跳过，而是 raise clear error 或返回空并记录警告
        logger.warning(f"Unknown/unimplemented node type '{ns.node_type}' for node '{ns.node_id}'")
        return {}

    def _resolve_output(self, ns: NodeSnapshot, outputs: dict) -> dict:
        """Resolve effect markers to clean TextureResult outputs.

        Supports both new `_effect_type` protocol (S1-C) and legacy `_input_a_id` protocol.

        New protocol (S1-C):
            - `_effect_type`: Dispatcher key (e.g., "merge", "blur", "grade")
            - `_dispatch_effect()` handles all effect types

        Legacy protocol (deprecated, TODO(S1-C): remove in next version):
            - `_input_a_id`, `_input_b_id`, `_input_mask_id`: Old merge marker keys
            - `_dispatch_blend()` handles legacy merge operations
        """
        effect_type = outputs.get("_effect_type")
        if effect_type:
            # A5: transform motion blur — sub-frame sampling of animated
            # transform parameters, averaged on the GPU.
            if effect_type == "transform":
                blurred = self._try_transform_motion_blur(ns, outputs)
                if blurred is not None:
                    return self._normalize_output_ports(
                        ns, self._filter_marker_keys(blurred))
            result = self._dispatch_effect(effect_type, outputs)
            # CP-044/045: universal mask gating + mix blend (single pass;
            # no-op unless the node carries _generic_mask or generic mix).
            result = self._apply_generic_mask_mix(ns, outputs, result)
            return self._normalize_output_ports(
                ns, self._filter_marker_keys(result))

        # TODO(S1-C): Legacy `_input_a_id` protocol — remove in next version
        # Old graphs may still use `_input_a_id`, `_input_b_id`, `_input_mask_id` markers.
        # This fallback ensures backward compatibility during transition.
        if "_input_a_id" in outputs or "_input_b_id" in outputs:
            result = self._dispatch_blend(ns, outputs)
            return self._normalize_output_ports(
                ns, self._filter_marker_keys(result))

        return self._normalize_output_ports(
            ns, self._filter_marker_keys(outputs))

    def _normalize_output_ports(self, ns, outputs: dict) -> dict:
        """Alias resolved outputs onto the node's declared output ports.

        Effect dispatchers return their texture under the conventional
        ``"result"`` key, but several node classes declare a differently
        named output port (``"image"``, ``"rgba"``, …), and some node
        passthroughs return the input under the declared port name while
        the dispatcher convention is ``"result"``. Downstream input
        gathering looks textures up by the *declared* port name recorded
        in the connection, so a key mismatch silently drops the edge: the
        node renders but nothing downstream receives it.

        When none of the declared port names is present and the dict
        carries exactly one TextureResult, alias that texture onto every
        declared port. Existing keys are preserved (``"result"`` consumers
        keep working); this only *adds* resolutions.
        """
        if not outputs:
            return outputs
        declared = [p[0] for p in (getattr(ns, "output_ports", None) or [])]
        if not declared:
            return outputs
        if any(name in outputs for name in declared):
            return outputs
        tex_keys = [k for k, v in outputs.items()
                    if isinstance(v, TextureResult)]
        if len(tex_keys) != 1:
            return outputs
        tex = outputs[tex_keys[0]]
        for name in declared:
            if name not in outputs:
                outputs[name] = tex
        return outputs

    def _filter_marker_keys(self, outputs: dict) -> dict:
        """Filter out internal marker keys."""
        return {
            k: v for k, v in outputs.items()
            if isinstance(v, TextureResult) and not k.startswith("_")
        }

    def _crop_to_tile_roi(self, tex: TextureResult, tile_roi: BBox) -> TextureResult:
        """Crop a source texture to a tile region."""
        src_w, src_h = tex.width, tex.height
        tw, th = tile_roi.width, tile_roi.height

        if tw <= 0 or th <= 0:
            return tex

        tile_tex = self._resource_pool.acquire_tile_texture(tw, th)
        tile_fbo = self.fb_manager.get_fbo(tile_tex)
        tile_fbo.use()
        self.ctx.viewport = (0, 0, tw, th)
        self.ctx.clear(0.0, 0.0, 0.0, 0.0)

        prog = self.utility_shaders.get('crop')
        if prog is None:
            self._resource_pool.release_texture(tile_tex)
            return tex

        self._bind_external_texture(tex.texture_id)
        prog['u_texture'].value = 0
        prog['u_src_resolution'].value = (float(src_w), float(src_h))
        prog['u_crop_rect'].value = (float(tile_roi.x), float(tile_roi.y),
                                     float(tw), float(th))
        prog['u_outside'].value = 0
        self._render_quad(prog)
        # P1-6: the source is a pooled texture that may be released while
        # this tile is still being composited — never leave unit 0 bound.
        self._unbind_texture_unit(0)

        return TextureResult(
            texture_id=tile_tex.glo, width=tw, height=th,
            format=tex.format, bbox=tile_roi,
            alpha_premultiplied=tex.alpha_premultiplied,
            color_space=tex.color_space,
            # 同 _convert_to_linear：透传 revision，否则下游缓存键冻结
            revision=tex.revision,
            bit_depth=tex.bit_depth,
            frame_metadata=tex.frame_metadata,
        )

    # ── Effect Dispatch ──

    # ── A5: transform motion blur ────────────────────────────────────

    _MOTION_BLUR_ANIMATED_PARAMS = ("translate_x", "translate_y",
                                    "rotation", "scale_x", "scale_y",
                                    "pivot_x", "pivot_y")

    @staticmethod
    def _motion_blur_sub_frames(shutter: float, samples: int,
                                offset: float, frame: float) -> list:
        """Closed-form sub-frame sample times (deterministic, cache-safe)."""
        half = max(0.0, float(shutter)) / 2.0
        n = max(2, int(samples))
        base = float(frame) + float(offset)
        if n == 1:
            return [base]
        step = (2.0 * half) / (n - 1)
        return [base - half + i * step for i in range(n)]

    def _try_transform_motion_blur(self, ns: NodeSnapshot, outputs: dict):
        """Multi-sample transform motion blur, or None to render normally.

        Zero-overhead guarantees: disabled flag, no animated transform
        parameters, or a missing input falls through to the plain path.
        """
        params = outputs.get("_effect_params")
        if not params or not params.get("motion_blur", False):
            return None
        effect_input = outputs.get("_effect_input")
        if not effect_input or not getattr(effect_input, "is_valid", False):
            return None
        animated = [name for name in self._MOTION_BLUR_ANIMATED_PARAMS
                    if name in (ns.keyframe_tracks or {})]
        if not animated:
            return None

        try:
            shutter = float(params.get("shutter", 0.5))
            samples = int(params.get("samples", 8))
            offset = float(params.get("shutter_offset", 0.0))
        except (TypeError, ValueError):
            return None
        # Zero shutter = no sub-frame excursion — plain single render
        # (renders N identical samples for nothing otherwise).
        if shutter <= 0.0:
            return None
        samples = max(2, min(16, samples))
        frame = getattr(getattr(self, '_snapshot', None), 'frame_number', 0)
        sub_frames = self._motion_blur_sub_frames(shutter, samples, offset,
                                                  frame)

        w = effect_input.width
        h = effect_input.height
        # Every sub-frame renders through _apply_transform in preview mode
        # (no tile ctx), which allocates the node-declared expanded canvas
        # (out_w/out_h) — the accumulator and the final result must live in
        # that same canvas, tagged with the moved bbox, or content is
        # clipped and downstream merges sample at the wrong position.
        out_w = int(params.get("out_w", w) or w)
        out_h = int(params.get("out_h", h) or h)
        out_bbox = params.get("out_bbox") or effect_input.effective_bbox()
        acc_tex_obj = None
        sample_ids: list = []
        try:
            prog = self.utility_shaders.get('motion_blur_accum')
            if prog is None:
                return None
            for i, sub_frame in enumerate(sub_frames):
                sub_params = dict(params)
                interpolated = self._interpolate_parameters(ns, sub_frame)
                sub_params.update(interpolated)
                # _apply_transform reads the list-form keys; the
                # interpolator emits per-component names — rebuild the
                # lists so sub-frame translate/scale actually apply.
                sub_params["translate"] = [
                    float(interpolated.get("translate_x",
                                           sub_params["translate"][0])),
                    float(interpolated.get("translate_y",
                                           sub_params["translate"][1])),
                ]
                sub_params["scale"] = [
                    float(interpolated.get("scale_x",
                                           sub_params["scale"][0])),
                    float(interpolated.get("scale_y",
                                           sub_params["scale"][1])),
                ]
                rendered = self._apply_transform(effect_input, sub_params,
                                                 w, h)
                sample = rendered.get("result") if rendered else None
                if sample is None or not sample.is_valid:
                    return None
                sample_ids.append(sample.texture_id)
                if acc_tex_obj is None:
                    acc_tex_obj = self._resource_pool.acquire_texture(out_w, out_h)
                    self._copy_texture(sample, acc_tex_obj)
                    continue
                # blend the new sample into the running average: the mix
                # result lands in a fresh texture which becomes the acc.
                weight = 1.0 / float(i + 1)
                new_acc = self._resource_pool.acquire_texture(out_w, out_h)
                fbo = self.fb_manager.get_fbo(new_acc)
                fbo.use()
                self.ctx.viewport = (0, 0, out_w, out_h)
                self.ctx.clear(0.0, 0.0, 0.0, 0.0)
                self._bind_texture_unit(acc_tex_obj.glo, 0)
                self._bind_texture_unit(sample.texture_id, 1)
                prog['u_accum'].value = 0
                prog['u_sample'].value = 1
                prog['u_weight'].value = weight
                self._render_quad(prog)
                # P1-6: acc_tex_obj is released right below — unbind both
                # units first or the released name stays sampled.
                self._unbind_texture_unit(0)
                self._unbind_texture_unit(1)
                self._resource_pool.release_texture(acc_tex_obj)
                acc_tex_obj = new_acc
            if acc_tex_obj is None:
                return None
            out = TextureResult(
                texture_id=acc_tex_obj.glo, width=out_w, height=out_h,
                format=effect_input.format,
                bbox=out_bbox,
                color_space=effect_input.color_space,
                alpha_premultiplied=effect_input.alpha_premultiplied,
                bit_depth=effect_input.bit_depth,
                revision=effect_input.revision,
            )
            acc_tex_obj = None  # ownership moved into the returned result
            return {"result": out}
        except Exception:
            logger.exception("transform motion blur failed — single-sample fallback")
            return None
        finally:
            # Sub-frame samples are intermediates; the accumulator is released
            # unless its ownership moved into the returned result.
            for tex_id in sample_ids:
                if tex_id and tex_id > 0:
                    self._resource_pool.release_texture_by_id(tex_id)
            if acc_tex_obj is not None:
                self._resource_pool.release_texture(acc_tex_obj)

    def _copy_texture(self, src: TextureResult, dst_tex) -> None:
        """Blit one texture into another via the passthrough shader."""
        prog = self.utility_shaders.get('passthrough')
        fbo = self.fb_manager.get_fbo(dst_tex)
        fbo.use()
        self.ctx.viewport = (0, 0, int(src.width), int(src.height))
        self.ctx.clear(0.0, 0.0, 0.0, 0.0)
        self._bind_external_texture(src.texture_id)
        prog['u_texture'].value = 0
        self._render_quad(prog)
        # P1-6: the source may be pooled and released by the caller right
        # after the copy — drop the bind before returning.
        self._unbind_texture_unit(0)

    def _dispatch_effect(self, effect_type: str, outputs: dict) -> dict:
        """Apply a GPU effect based on marker type.

        This method dispatches to specialized evaluator mixins.
        The actual implementations are provided by:
        - BlurEvaluatorMixin (blur, defocus, sharpen)
        - ColorEvaluatorMixin (grade, color_correct, huecorrect, normalize)
        - TransformEvaluatorMixin (transform, cornerpin, reformat, crop)
        - MergeEvaluatorMixin (dissolve, deep_merge, merge)
        - KeyerEvaluatorMixin (luma_keyer, chroma_keyer, difference_keyer)
        - ChannelEvaluatorMixin (shuffle, copy_channel, remove_channels, channel_merge)
        - FilterEvaluatorMixin (erode_dilate, glow, edge_detect, noise, ramp, mask, denoise)
        """
        # Dissolve — two inputs
        if effect_type == "dissolve":
            return self._apply_dissolve(outputs, 0, 0)

        # Deep merge — two inputs + depth
        if effect_type == "deep_merge":
            return self._apply_deep_merge(outputs)

        # CP-011 Keymix — two inputs + mask lerp
        if effect_type == "keymix":
            return self._apply_keymix(outputs)

        # S1-C: Merge — two inputs with blend modes (dispatches to _dispatch_blend)
        if effect_type == "merge":
            return self._dispatch_merge_effect(outputs)

        effect_input = outputs.get("_effect_input")
        effect_mask = outputs.get("_effect_mask")
        effect_params = outputs.get("_effect_params", {})
        effect_velocity = outputs.get("_effect_velocity")

        # Mask is deliberately dual-mode: it can be a standalone shape
        # generator or an image-masking effect.  Unlike regular effects, the
        # generator path is valid without ``_effect_input``.
        if effect_type == "mask":
            return self._apply_mask(effect_input, effect_params)
        if effect_type == "roto":
            return self._apply_roto(effect_input, effect_params)
        # C2: paint strokes (clone/erase/solid) — CPU raster in the mixin.
        if effect_type == "paint":
            return self._apply_paint(effect_input, effect_params)
        # C7: bilinear grid warp.
        if effect_type == "mesh_warp":
            w = effect_input.width if effect_input else 0
            h = effect_input.height if effect_input else 0
            return self._apply_mesh_warp(effect_input, effect_params, w, h)
        if effect_type == "mask_merge":
            return self._apply_mask_merge(
                outputs.get("_effect_input_a"),
                outputs.get("_effect_input_b"),
                effect_params)
        if effect_type == "compare":
            return self._apply_compare(outputs)
        if effect_type == "light_wrap":
            return self._apply_light_wrap(outputs)
        # A1: AOV re-lighting — three inputs (image + normal + optional
        # position), so it dispatches before the single-input branch below.
        if effect_type == "relight":
            w0 = effect_input.width if effect_input else 0
            h0 = effect_input.height if effect_input else 0
            return self._apply_relight(
                effect_input, outputs.get("_effect_normal"),
                outputs.get("_effect_position"),
                effect_params, w0, h0)
        # A2: Cryptomatte Select — ID texture → matte/isolate.
        if effect_type == "cryptomatte_select":
            w0 = effect_input.width if effect_input else 0
            h0 = effect_input.height if effect_input else 0
            return self._apply_cryptomatte_select(
                effect_input, effect_params, w0, h0)
        # A3: depth tools — image + depth texture pairs.
        if effect_type in ("zblur", "fog", "depth_matte"):
            w0 = effect_input.width if effect_input else 0
            h0 = effect_input.height if effect_input else 0
            return self._apply_depth_tool(
                effect_type, effect_input, outputs.get("_effect_depth"),
                effect_params, w0, h0)

        # ── Generators: no input required ──
        # These must dispatch BEFORE the single-input gate below: they have
        # no ``_effect_input`` by design, and their handlers take only
        # effect_params. Placing them below the gate made them unreachable
        # (the node evaluated but always produced {}).
        if effect_type == "noise":
            return self._apply_noise(effect_params)
        if effect_type == "ramp":
            return self._apply_ramp(effect_params)

        # ── Multi-input effects that don't use ``_effect_input`` ──
        # keyer/difference_keyer carry their operands as _effect_input_a/_b
        # (the unified keyer never sets _effect_input); channel_merge packs
        # its four operands inside _effect_params and communicates output
        # geometry through the marker's placeholder result.
        if effect_type == "keyer":
            return self._apply_keyer(outputs)
        if effect_type == "difference_keyer":
            input_a = outputs.get("_effect_input_a")
            input_b = outputs.get("_effect_input_b")
            return self._apply_difference_keyer(
                input_a, input_b, effect_params,
                input_a.width if input_a else 0,
                input_a.height if input_a else 0)
        if effect_type == "channel_merge":
            placeholder = outputs.get("result")
            w0 = int(getattr(placeholder, "width", 0) or 0)
            h0 = int(getattr(placeholder, "height", 0) or 0)
            if w0 <= 0:
                for ch_name in ("input_r", "input_g", "input_b", "input_a"):
                    ch_tex = effect_params.get(ch_name)
                    if ch_tex is not None and getattr(ch_tex, "is_valid", False):
                        w0, h0 = ch_tex.width, ch_tex.height
                        break
            return self._apply_channel_merge(effect_params, w0, h0)

        if not effect_input or not effect_input.is_valid:
            return {"result": effect_input} if effect_input else {}

        w = effect_input.width
        h = effect_input.height

        if effect_mask and effect_mask.is_valid:
            effect_params["_mask_tex"] = effect_mask
        if isinstance(effect_velocity, TextureResult) and effect_velocity.is_valid:
            effect_params["_velocity_tex"] = effect_velocity

        # Dispatch to specialized evaluators
        if effect_type == "blur":
            return self._apply_blur(effect_input, effect_params, w, h)
        elif effect_type == "grade":
            return self._apply_grade(effect_input, effect_params, w, h)
        elif effect_type == "topos_color":
            return self._apply_topos_color(effect_input, effect_params, w, h)
        elif effect_type == "transform":
            return self._apply_transform(effect_input, effect_params, w, h)
        elif effect_type == "invert":
            return self._apply_invert(effect_input, effect_params, w, h)
        elif effect_type == "premult":
            return self._apply_premult(effect_input, w, h)
        elif effect_type == "unpremult":
            return self._apply_unpremult(effect_input, w, h)
        elif effect_type == "shuffle":
            return self._apply_shuffle(effect_input, effect_params, w, h)
        elif effect_type == "copy_channel":
            return self._apply_copy_channel(effect_input, effect_params, w, h)
        elif effect_type == "cs_transform":
            return self._apply_cs_transform(effect_input, effect_params, w, h)
        elif effect_type == "reformat":
            return self._apply_reformat(effect_input, effect_params)
        elif effect_type == "crop":
            return self._apply_crop(effect_input, effect_params)
        elif effect_type == "clamp":
            return self._apply_clamp(effect_input, effect_params, w, h)
        elif effect_type == "set_alpha":
            return self._apply_set_alpha(effect_input, effect_params, w, h)
        elif effect_type == "remove_channels":
            return self._apply_remove_channels(effect_input, effect_params, w, h)
        elif effect_type == "luma_keyer":
            return self._apply_luma_keyer(effect_input, effect_params, w, h)
        elif effect_type == "matte":
            return self._apply_matte(effect_input, effect_params, w, h)
        elif effect_type == "matte_cleanup":
            return self._apply_matte_cleanup(effect_input, effect_params, w, h)
        elif effect_type == "color_correct":
            return self._apply_color_correct(effect_input, effect_params, w, h)
        elif effect_type == "huecorrect":
            return self._apply_huecorrect(effect_input, effect_params, w, h)
        elif effect_type == "sharpen":
            return self._apply_sharpen(effect_input, effect_params, w, h)
        elif effect_type == "erode_dilate":
            return self._apply_erode_dilate(effect_input, effect_params, w, h)
        elif effect_type == "chroma_keyer":
            return self._apply_chroma_keyer(effect_input, effect_params, w, h)
        # keyer / difference_keyer / channel_merge / noise / ramp dispatch
        # ABOVE the single-input gate — see the pre-gate block.
        elif effect_type == "normalize":
            return self._apply_normalize(effect_input, effect_params, w, h)
        elif effect_type == "defocus":
            return self._apply_defocus(effect_input, effect_params, w, h)
        elif effect_type == "glow":
            return self._apply_glow(effect_input, effect_params, w, h)
        elif effect_type == "cornerpin":
            return self._apply_cornerpin(effect_input, effect_params, w, h)
        elif effect_type == "lut3d":
            return self._apply_lut3d(effect_input, effect_params, w, h)
        elif effect_type == "edge_detect":
            return self._apply_edge_detect(effect_input, effect_params, w, h)
        elif effect_type == "lens_distortion":
            return self._apply_lens_distortion(effect_input, effect_params, w, h)
        elif effect_type == "denoise":
            return self._apply_denoise(effect_input, effect_params, w, h)
        elif effect_type == "godrays":
            return self._apply_godrays(effect_input, effect_params, w, h)
        elif effect_type == "chromatic_aberration":
            return self._apply_chromatic_aberration(
                effect_input, effect_params, w, h)
        elif effect_type == "vignette":
            return self._apply_vignette(effect_input, effect_params, w, h)
        elif effect_type == "lens_flare":
            return self._apply_lens_flare(effect_input, effect_params, w, h)
        # CP-002 node plugin effects (plugins/node_plugins/*)
        elif effect_type == "posterize":
            return self._apply_posterize(effect_input, effect_params, w, h)
        elif effect_type == "vintage_tone":
            return self._apply_vintage_tone(effect_input, effect_params, w, h)
        elif effect_type == "film_grain":
            return self._apply_film_grain(effect_input, effect_params, w, h)
        # CP-019: grain-gated smoothing (static degrain)
        elif effect_type == "degrain":
            return self._apply_degrain(effect_input, effect_params, w, h)
        # CP-020: motion-vector blur
        elif effect_type == "vectorblur":
            return self._apply_vectorblur(effect_input, effect_params, w, h)
        # CP-014: master/R/G/B curve lookup
        elif effect_type == "colorlookup":
            return self._apply_colorlookup(effect_input, effect_params, w, h)
        # CP-015: ASC CDL
        elif effect_type == "cdl":
            return self._apply_cdl(effect_input, effect_params, w, h)
        # LIB1 内容库节点效果（计划 L / L04：C01/C17）
        elif effect_type == "lib1_gradient_map":
            return self._apply_lib1_gradient_map(effect_input, effect_params, w, h)
        elif effect_type == "lib1_edge_extend":
            return self._apply_lib1_edge_extend(effect_input, effect_params, w, h)
        elif effect_type == "lib1_rgb_matrix":
            return self._apply_lib1_rgb_matrix(effect_input, effect_params, w, h)
        elif effect_type == "lib1_despill":
            return self._apply_lib1_despill(effect_input, effect_params, w, h)
        elif effect_type == "lib1_solarize":
            return self._apply_lib1_solarize(effect_input, effect_params, w, h)
        elif effect_type == "lib1_mosaic":
            return self._apply_lib1_mosaic(effect_input, effect_params, w, h)
        elif effect_type == "lib1_crt":
            return self._apply_lib1_crt(effect_input, effect_params, w, h)
        elif effect_type == "lib1_glitch":
            return self._apply_lib1_glitch(effect_input, effect_params, w, h)
        elif effect_type == "lib1_kaleidoscope":
            return self._apply_lib1_kaleidoscope(effect_input, effect_params, w, h)
        elif effect_type == "lib1_median":
            return self._apply_lib1_median(effect_input, effect_params, w, h)
        elif effect_type == "lib1_convolution":
            return self._apply_lib1_convolution(effect_input, effect_params, w, h)
        elif effect_type == "lib1_halation":
            return self._apply_lib1_halation(effect_input, effect_params, w, h)
        elif effect_type == "lib1_halftone":
            return self._apply_lib1_halftone(effect_input, effect_params, w, h)
        elif effect_type == "lib1_hatching":
            return self._apply_lib1_hatching(effect_input, effect_params, w, h)
        elif effect_type == "lib1_polar_coordinates":
            return self._apply_lib1_polar_coordinates(effect_input, effect_params, w, h)
        elif effect_type == "lib1_tile_array":
            return self._apply_lib1_tile_array(effect_input, effect_params, w, h)
        elif effect_type == "lib1_frequency_separation":
            # 多输出子门（L17 批3）：双占位 low/high → _normalize_output_ports
            # 别名到声明的双输出端口
            return self._apply_lib1_frequency_separation(outputs, w, h)
        elif effect_type == "lib1_vector_displace":
            # 多输入子门（L17 批3）：marker 携带 _effect_input_map/_effect_mask
            return self._apply_lib1_vector_displace(outputs, w, h)
        elif effect_type == "lib1_stmap":
            return self._apply_lib1_stmap(outputs, w, h)
        elif effect_type in ("lib1_distance_matte", "lib1_fill_holes"):
            # 全帧 CPU 子门（L17）：回读→CPU 计算→上传，非单 pass GLSL
            return self._apply_lib1_fullframe(effect_type, effect_input,
                                              effect_params, w, h)

        # ── Time Effects: pass-through by design (P0-04) ──
        # The remap is applied upstream: NodeGraph.snapshot() precomputes
        # {source_node_id: TimeRemap} (core/time_remap.py) and frame-reading
        # sources shift the playhead before decoding, so by the time the
        # texture reaches the time node the remapping has already happened.
        # These nodes don't create new textures (owns_color_texture=False).
        elif effect_type == "time_offset":
            return {"result": effect_input}
        elif effect_type == "frame_hold":
            return {"result": effect_input}
        elif effect_type == "retime":
            # use_optical_flow is metadata for a future resampling backend;
            # the speed remap itself already applies at the source.
            return {"result": effect_input}

        # S1-B: Placeholder effect types — pass through with warning
        # These nodes exist in NODE_REGISTRY but their GPU effect is not yet implemented.
        # Do not silently skip; return input and log warning for user visibility.
        # BUG-13: Removed text (source type) and file_output (output type)
        # Source/output nodes are handled by scheduler, not effect placeholder path
        PLACEHOLDER_EFFECT_TYPES = frozenset({
            "group",          # Group/macro node (needs subgraph execution)
        })
        if effect_type in PLACEHOLDER_EFFECT_TYPES:
            logger.warning(f"Effect type '{effect_type}' is placeholder — returning input unchanged")
            # For two-input effects (mask_merge), return first valid input
            input_a = outputs.get("_effect_input_a")
            input_b = outputs.get("_effect_input_b")
            if input_a and input_a.is_valid:
                return {"result": input_a}
            if input_b and input_b.is_valid:
                return {"result": input_b}
            return {"result": effect_input} if effect_input else {}

        # LIB1 纪律（计划 L / L04 红线）：未知 lib1_* marker 精确报错，
        # 不得警告后透传冒充成功（"类注册了、dispatch 没接"的静默 no-op）。
        # 报错经渲染诊断路径传播；节点与参数保留（上游 marker/参数不动）。
        if effect_type.startswith("lib1_"):
            raise RuntimeError(
                f"LIB1 effect type '{effect_type}' 注册了 marker 但没有对应"
                " dispatch 实现（node_type/shader/evaluator 未同步合入）——"
                "拒绝透传；请补齐 dispatch 或回退组件版本")

        # Unknown effect type — should not happen if node is properly registered
        logger.warning(f"Unknown effect type '{effect_type}' — returning input unchanged")
        return {"result": effect_input}

    # ── Merge Effect Methods (S1-C) ──

    def _dispatch_merge_effect(self, outputs: dict) -> dict:
        """Apply merge/blend operation from new `_effect_type: "merge"` marker (S1-C).

        This method is called from `_dispatch_effect()` when effect_type == "merge".
        It extracts inputs and parameters from the marker outputs and dispatches
        to `_apply_merge_blend()`.

        Marker shape (S1-C):
            {
                "_effect_type": "merge",
                "_effect_input_a": TextureResult,  # Background input (A)
                "_effect_input_b": TextureResult,  # Foreground input (B)
                "_effect_input_mask": TextureResult,  # Optional mask
                "_effect_params": {
                    "blend_mode": str,
                    "opacity": float,
                    "format_policy": str,
                    "bbox_policy": str,
                },
            }
        """
        input_a = outputs.get("_effect_input_a")
        input_b = outputs.get("_effect_input_b")
        input_mask = outputs.get("_effect_input_mask")
        effect_params = outputs.get("_effect_params", {})

        if not input_a or not input_b:
            result = outputs.get("result")
            return {"result": result} if result and isinstance(result, TextureResult) else {}

        # Extract parameters from _effect_params
        blend_mode = effect_params.get("blend_mode", "over")
        opacity = effect_params.get("opacity", 1.0)
        format_policy = effect_params.get("format_policy", "from_b")
        bbox_policy = effect_params.get("bbox_policy", "union")
        # S2-B: Alpha policy parameters
        alpha_policy = effect_params.get("alpha_policy", "auto")
        alpha_clamp = effect_params.get("alpha_clamp", True)
        # S3-A: Mix and mask parameters
        mix = effect_params.get("mix", 1.0)
        mask_channel = effect_params.get("mask_channel", 0)
        mask_invert = effect_params.get("mask_invert", False)

        return self._apply_merge_blend(input_a, input_b, input_mask,
                                        blend_mode, opacity, format_policy, bbox_policy,
                                        alpha_policy, alpha_clamp,
                                        mix, mask_channel, mask_invert)

    def _apply_merge_blend(self, input_a: TextureResult, input_b: TextureResult,
                           input_mask: Optional[TextureResult],
                           blend_mode: str, opacity: float,
                           format_policy: str, bbox_policy: str,
                           alpha_policy: str = "auto",
                           alpha_clamp: bool = True,
                           mix: float = 1.0,
                           mask_channel: int = 0,
                           mask_invert: bool = False) -> dict:
        """Apply GPU merge/blend operation.

        Core GPU rendering logic shared by:
        - _dispatch_merge_effect() (new `_effect_type: "merge"` protocol, S1-C)
        - _dispatch_blend() (legacy `_input_a_id` protocol, for backward compatibility)

        S2-B: Added alpha_policy and alpha_clamp parameters for alpha handling.
        S3-A: Added mix, mask_channel, mask_invert parameters.

        Args:
            input_a: Background input texture (A).
            input_b: Foreground input texture (B).
            input_mask: Optional mask texture.
            blend_mode: Blend mode string (e.g., "over", "add", "multiply").
            opacity: Blend opacity (0.0 - 1.0).
            format_policy: Output format policy ("from_b", "from_a", "larger", "smaller").
            bbox_policy: Output bbox policy ("union", "intersection", "a", "b").
            alpha_policy: S2-B alpha handling policy ("auto", "premult_force",
                         "straight_force", "output_straight").
            alpha_clamp: S2-B whether to clamp alpha in add/subtract modes.
            mix: S3-A overall effect strength (0.0=background, 1.0=full merge).
            mask_channel: S3-A mask channel index (0=luma, 1=R, 2=G, 3=B, 4=A).
            mask_invert: S3-A whether to invert the mask value.

        Returns:
            Dict with "result" TextureResult containing the merged output.
        """
        if not input_a or not input_b:
            return {}

        # S2-D: Auto-align color spaces before merge
        # AUD-5803: 记住对齐前的原输入——转换纹理（若有）只服务于本次
        # 绘制，渲染后需归还池（原输入仍由上游缓存持有）。
        pre_align_a, pre_align_b = input_a, input_b
        input_a, input_b = self._auto_align_color_spaces(input_a, input_b)

        prog = self.utility_shaders.get('blend')
        if prog is None:
            return {"result": input_a}

        # S2-C: Compute unified merge geometry (replaces separate _resolve_merge_* calls)
        geometry = self._compute_merge_geometry(input_a, input_b, input_mask,
                                                 format_policy, bbox_policy)

        w, h = geometry.output_width, geometry.output_height

        out_tex = self._resource_pool.acquire_texture(w, h)
        out_fbo = self.fb_manager.get_fbo(out_tex)
        out_fbo.use()
        self.ctx.viewport = (0, 0, w, h)
        self.ctx.clear(0.0, 0.0, 0.0, 0.0)

        self._bind_external_texture(input_a.texture_id)
        from ..core.gl_bind import bind_texture_unit
        bind_texture_unit(input_b.texture_id, 1)

        has_mask = input_mask is not None and input_mask.is_valid
        if has_mask:
            bind_texture_unit(input_mask.texture_id, 2)

        prog['u_texture_a'].value = 0
        prog['u_texture_b'].value = 1

        # S2-C: Pass resolution uniforms from geometry
        prog['u_resolution_a'].value = (float(max(1, geometry.resolution_a[0] or w)),
                                         float(max(1, geometry.resolution_a[1] or h)))
        prog['u_resolution_b'].value = (float(max(1, geometry.resolution_b[0] or w)),
                                         float(max(1, geometry.resolution_b[1] or h)))
        prog['u_output_resolution'].value = (float(w), float(h))
        prog['u_opacity'].value = opacity
        prog['u_has_mask'].value = has_mask
        if has_mask:
            prog['u_texture_mask'].value = 2
            # S2-C: Pass mask resolution from geometry
            if geometry.resolution_mask is not None:
                prog['u_resolution_mask'].value = (float(max(1, geometry.resolution_mask[0] or w)),
                                                   float(max(1, geometry.resolution_mask[1] or h)))
            else:
                prog['u_resolution_mask'].value = (float(max(1, input_mask.width or w)),
                                                   float(max(1, input_mask.height or h)))

        # S2-B: Determine a_straight and b_straight based on alpha_policy
        if alpha_policy == "premult_force":
            prog['u_a_straight'].value = False
            prog['u_b_straight'].value = False
        elif alpha_policy == "straight_force":
            prog['u_a_straight'].value = True
            prog['u_b_straight'].value = True
        else:  # "auto" or "output_straight" - read from metadata
            prog['u_a_straight'].value = input_a.alpha_premultiplied is False
            prog['u_b_straight'].value = input_b.alpha_premultiplied is False

        # S2-B: Pass alpha_clamp and output_unpremult uniforms
        prog['u_alpha_clamp'].value = alpha_clamp
        output_unpremult = alpha_policy == "output_straight"
        prog['u_output_unpremult'].value = output_unpremult

        # S2-C: Pass bbox uniforms from geometry for correct spatial sampling
        # Transform bboxes to output texture space (offset by output_bbox origin)
        # When output bbox starts at (x, y), we shift all input bboxes so that
        # output_bbox's origin becomes (0, 0) in the output texture.
        output_bbox = geometry.output_bbox
        bbox_a_in_output = (
            float(geometry.bbox_a.x - output_bbox.x),
            float(geometry.bbox_a.y - output_bbox.y),
            float(geometry.bbox_a.width),
            float(geometry.bbox_a.height),
        )
        bbox_b_in_output = (
            float(geometry.bbox_b.x - output_bbox.x),
            float(geometry.bbox_b.y - output_bbox.y),
            float(geometry.bbox_b.width),
            float(geometry.bbox_b.height),
        )
        output_bbox_vec = (0.0, 0.0, float(output_bbox.width), float(output_bbox.height))

        # Guard uniform access — shader may not contain all uniforms
        # (e.g., dead-code elimination or older cached program)
        if 'u_bbox_a' in prog:
            prog['u_bbox_a'].value = bbox_a_in_output
        if 'u_bbox_b' in prog:
            prog['u_bbox_b'].value = bbox_b_in_output
        if 'u_output_bbox' in prog:
            prog['u_output_bbox'].value = output_bbox_vec

        # Mask bbox (if present)
        if has_mask and geometry.bbox_mask is not None:
            bbox_mask_in_output = (
                float(geometry.bbox_mask.x - output_bbox.x),
                float(geometry.bbox_mask.y - output_bbox.y),
                float(geometry.bbox_mask.width),
                float(geometry.bbox_mask.height),
            )
            if 'u_bbox_mask' in prog:
                prog['u_bbox_mask'].value = bbox_mask_in_output
        else:
            if 'u_bbox_mask' in prog:
                prog['u_bbox_mask'].value = (0.0, 0.0, 0.0, 0.0)

        # S2-C / BUG-1 fix: Enable bbox mapping whenever inputs differ from
        # the output in resolution OR bbox origin. The previous check only
        # looked at `bbox.x/y != 0`, which meant two inputs of different
        # resolutions but both starting at origin fell through to
        # sampleAtOrigin() and got stretched/clipped by `output_res/tex_res`.
        # sampleLayer() correctly maps each input's bbox-relative pixels into
        # the output canvas regardless of origin, so it is always the right
        # choice when any spatial mismatch exists.
        out_res = (geometry.output_width, geometry.output_height)
        use_bbox_mapping = (
            (geometry.resolution_a[0], geometry.resolution_a[1]) != out_res or
            (geometry.resolution_b[0], geometry.resolution_b[1]) != out_res or
            geometry.bbox_a.x != output_bbox.x or geometry.bbox_a.y != output_bbox.y or
            geometry.bbox_b.x != output_bbox.x or geometry.bbox_b.y != output_bbox.y or
            (geometry.bbox_mask is not None and (
                geometry.bbox_mask.x != output_bbox.x or
                geometry.bbox_mask.y != output_bbox.y)) or
            # Also enable when output bbox itself doesn't start at origin
            output_bbox.x != 0 or output_bbox.y != 0
        )
        if 'u_use_bbox_mapping' in prog:
            prog['u_use_bbox_mapping'].value = use_bbox_mapping

        # S3-A: Set new uniforms (mix, mask_channel, mask_invert) safely —
        # they may be optimized out by the driver if unreachable.
        self._set_uniform_if_present(prog, 'u_mix', float(mix))
        self._set_uniform_if_present(prog, 'u_mask_channel', int(mask_channel))
        self._set_uniform_if_present(prog, 'u_mask_invert', bool(mask_invert))

        from ..nodes.blend_node import BLEND_MODES
        mode_idx = BLEND_MODES.index(blend_mode) if blend_mode in BLEND_MODES else 0
        prog['u_blend_mode'].value = mode_idx

        self._render_quad(prog)

        # P1-6 (AUD-5802): input_a 也是池化纹理——渲染后解绑单元 0，与
        # 同文件其余效果路径（_copy_texture/_apply_grade/_convert_to_linear…）
        # 的绑定纪律一致；此前残留绑定会在输入被回收后采样已释放 GL 名。
        self._unbind_texture_unit(0)
        if has_mask:
            bind_texture_unit(0, 2)
        bind_texture_unit(0, 1)

        # AUD-5803: 自动色彩空间对齐分配的转换纹理（A'/B'）不在
        # working_inputs 也不在 outputs 中，无任何所有权归属——此前每次
        # 触发转换的 merge 泄漏 1-2 张全幅池纹理直到预算驱逐。它们只被
        # 本次绘制采样，渲染完成后立即归还（release_texture 带围栏，不会
        # 在 GPU 仍读取时被复用；同 _blur_separable 的 temp 纹理纪律）。
        if input_a is not pre_align_a:
            self._resource_pool.release_texture_by_id(input_a.texture_id)
        if input_b is not pre_align_b:
            self._resource_pool.release_texture_by_id(input_b.texture_id)

        # S2-C: Use geometry for result metadata (already computed)
        # S2-B: Output alpha_premultiplied depends on alpha_policy
        output_premult = alpha_policy != "output_straight"

        return {
            "result": TextureResult(
                texture_id=out_tex.glo, width=w, height=h,
                format=geometry.output_format,
                bbox=geometry.output_bbox,
                alpha_premultiplied=output_premult,
                color_space=input_b.color_space or input_a.color_space,
            )
        }

    def _dispatch_blend(self, ns: NodeSnapshot, outputs: dict) -> dict:
        """Apply merge/blend operation from legacy marker data.

        TODO(S1-C): This legacy `_input_a_id` protocol is deprecated.
        Next version will remove this method and `_resolve_output()`'s fallback.
        New code should use `_effect_type: "merge"` protocol instead.
        """
        input_a = outputs.get("_input_a_id")
        input_b = outputs.get("_input_b_id")
        input_mask = outputs.get("_input_mask_id")

        if not input_a or not input_b:
            result = outputs.get("result")
            return {"result": result} if result and isinstance(result, TextureResult) else {}

        blend_mode = ns.parameters.get("blend_mode", "over")
        opacity = ns.parameters.get("opacity", 1.0)
        format_policy = ns.parameters.get("format_policy", "from_b")
        bbox_policy = ns.parameters.get("bbox_policy", "union")
        # S2-B: Alpha policy parameters (use defaults for legacy protocol)
        alpha_policy = ns.parameters.get("alpha_policy", "auto")
        alpha_clamp = ns.parameters.get("alpha_clamp", True)
        # S3-A: Mix and mask parameters (resolve mask_channel string→index)
        from ..nodes.blend_node import MASK_CHANNELS
        mask_channel_str = ns.parameters.get("mask_channel", "luma")
        mask_channel_idx = MASK_CHANNELS.index(mask_channel_str) if mask_channel_str in MASK_CHANNELS else 0

        # Delegate to shared _apply_merge_blend() (S1-C + S2-B + S3-A refactor)
        return self._apply_merge_blend(input_a, input_b, input_mask,
                                        blend_mode, opacity, format_policy, bbox_policy,
                                        alpha_policy, alpha_clamp,
                                        ns.parameters.get("mix", 1.0),
                                        mask_channel_idx,
                                        ns.parameters.get("mask_invert", False))

    @staticmethod
    def _canvas_dimensions(tex: TextureResult) -> tuple[int, int]:
        """Return the composition canvas dimensions represented by *tex*.

        A Transform may store a tightly packed data window in a smaller GPU
        texture while preserving the upstream ImageFormat as its full canvas.
        Merge allocation must follow that canvas, not the packed texture size,
        otherwise Transform connected to Merge's B input changes the merge
        frame on every scale/translation update.
        """
        if tex.format is not None:
            return max(1, tex.format.width), max(1, tex.format.height)
        return max(1, tex.width), max(1, tex.height)

    @staticmethod
    def _resolve_merge_dims(input_a: TextureResult, input_b: TextureResult,
                            format_policy: str) -> tuple:
        """Determine output dimensions based on format_policy."""
        wa, ha = BaseEvaluatorMixin._canvas_dimensions(input_a)
        wb, hb = BaseEvaluatorMixin._canvas_dimensions(input_b)
        if format_policy == "from_a":
            return max(1, wa), max(1, ha)
        elif format_policy == "larger":
            return max(1, max(wa, wb)), max(1, max(ha, hb))
        elif format_policy == "smaller":
            return max(1, min(wa, wb)), max(1, min(ha, hb))
        else:
            return max(1, wb), max(1, hb)

    @staticmethod
    def _resolve_merge_format(input_a: TextureResult, input_b: TextureResult,
                              format_policy: str) -> Optional["ImageFormat"]:
        """Determine output format based on format_policy."""
        if format_policy == "from_a":
            return input_a.format or input_b.format
        elif format_policy == "larger":
            canvas_a = BaseEvaluatorMixin._canvas_dimensions(input_a)
            canvas_b = BaseEvaluatorMixin._canvas_dimensions(input_b)
            area_a = canvas_a[0] * canvas_a[1]
            area_b = canvas_b[0] * canvas_b[1]
            if area_a >= area_b:
                return input_a.format or input_b.format
            return input_b.format or input_a.format
        elif format_policy == "smaller":
            canvas_a = BaseEvaluatorMixin._canvas_dimensions(input_a)
            canvas_b = BaseEvaluatorMixin._canvas_dimensions(input_b)
            area_a = canvas_a[0] * canvas_a[1]
            area_b = canvas_b[0] * canvas_b[1]
            if area_a <= area_b:
                return input_a.format or input_b.format
            return input_b.format or input_a.format
        else:
            return input_b.format if input_b.format else input_a.format

    @staticmethod
    def _resolve_merge_bbox(input_a: TextureResult, input_b: TextureResult,
                            bbox_policy: str) -> BBox:
        """Determine output bbox based on bbox_policy."""
        bbox_a = input_a.effective_bbox()
        bbox_b = input_b.effective_bbox()
        if bbox_policy == "intersection":
            return bbox_a.intersection(bbox_b)
        elif bbox_policy == "a":
            return bbox_a
        elif bbox_policy == "b":
            return bbox_b
        else:
            return bbox_a.union(bbox_b)

    @staticmethod
    def _compute_merge_geometry(input_a: TextureResult, input_b: TextureResult,
                                input_mask: Optional[TextureResult],
                                format_policy: str, bbox_policy: str) -> MergeGeometry:
        """Compute complete merge geometry in one unified call (S2-C).

        This method unifies `_resolve_merge_dims()`, `_resolve_merge_format()`,
        and `_resolve_merge_bbox()` into a single MergeGeometry object that
        contains all spatial information needed for shader sampling.

        The key insight: when inputs have different bboxes, we need to know
        where each input's data window is relative to the output canvas,
        not just the output dimensions.

        Args:
            input_a: Background input texture (A).
            input_b: Foreground input texture (B).
            input_mask: Optional mask input texture.
            format_policy: Output format policy ("from_b", "from_a", "larger", "smaller").
            bbox_policy: Output bbox policy ("union", "intersection", "a", "b").

        Returns:
            MergeGeometry containing:
            - Output canvas dimensions (output_width, output_height)
            - Output data window (output_bbox)
            - Input bboxes in output coordinate space (bbox_a, bbox_b, bbox_mask)
            - Input resolutions for shader sampling
            - Output format metadata
        """
        # Get physical input resolutions.  These are used for sampling the
        # packed textures, while canvas dimensions below decide the FBO size.
        wa, ha = input_a.width, input_a.height
        wb, hb = input_b.width, input_b.height

        canvas_a_w, canvas_a_h = BaseEvaluatorMixin._canvas_dimensions(input_a)
        canvas_b_w, canvas_b_h = BaseEvaluatorMixin._canvas_dimensions(input_b)

        # Compute output dimensions based on format_policy
        if format_policy == "from_a":
            out_w, out_h = canvas_a_w, canvas_a_h
        elif format_policy == "larger":
            out_w, out_h = max(canvas_a_w, canvas_b_w), max(canvas_a_h, canvas_b_h)
        elif format_policy == "smaller":
            out_w, out_h = min(canvas_a_w, canvas_b_w), min(canvas_a_h, canvas_b_h)
        else:  # "from_b" (default)
            out_w, out_h = canvas_b_w, canvas_b_h

        # Get input bboxes
        bbox_a = input_a.effective_bbox()
        bbox_b = input_b.effective_bbox()

        # Compute output bbox based on bbox_policy
        if bbox_policy == "intersection":
            output_bbox = bbox_a.intersection(bbox_b)
        elif bbox_policy == "a":
            output_bbox = bbox_a
        elif bbox_policy == "b":
            output_bbox = bbox_b
        else:  # "union" (default)
            output_bbox = bbox_a.union(bbox_b)

        # Clamp the output bbox to the output texture extents. The output
        # texture is allocated at (out_w, out_h) from format_policy, but
        # bbox_policy (e.g. union) can produce a bbox that starts at a
        # negative origin or extends past the canvas when an input's data
        # window moved outside the frame. Without clamping, the per-input
        # `bbox_in_output = bbox - output_bbox.xy` offset would shift every
        # input (including a stationary background) whenever one input moved,
        # making the background appear to follow the foreground. Clamping
        # anchors the output bbox to the canvas so each input is sampled at
        # its true position; content outside the canvas is clipped, which is
        # the expected behaviour for a fixed-size output frame.
        output_bbox = BBox(
            x=max(0, output_bbox.x),
            y=max(0, output_bbox.y),
            width=min(out_w, max(0, output_bbox.right)) - max(0, output_bbox.x),
            height=min(out_h, max(0, output_bbox.top)) - max(0, output_bbox.y),
        )
        if output_bbox.width <= 0 or output_bbox.height <= 0:
            # Degenerate (no overlap with the canvas) — fall back to the
            # full canvas so the shader still has a valid sampling window.
            output_bbox = BBox(x=0, y=0, width=out_w, height=out_h)

        # Handle mask bbox
        bbox_mask: Optional[BBox] = None
        resolution_mask: Optional[Tuple[int, int]] = None
        if input_mask is not None and input_mask.is_valid:
            bbox_mask = input_mask.effective_bbox()
            resolution_mask = (input_mask.width, input_mask.height)

        # Compute output format metadata
        output_format: Optional[ImageFormat] = None
        if format_policy == "from_a":
            output_format = input_a.format or input_b.format
        elif format_policy == "larger":
            area_a = canvas_a_w * canvas_a_h
            area_b = canvas_b_w * canvas_b_h
            if area_a >= area_b:
                output_format = input_a.format or input_b.format
            else:
                output_format = input_b.format or input_a.format
        elif format_policy == "smaller":
            area_a = canvas_a_w * canvas_a_h
            area_b = canvas_b_w * canvas_b_h
            if area_a <= area_b:
                output_format = input_a.format or input_b.format
            else:
                output_format = input_b.format or input_a.format
        else:  # "from_b"
            output_format = input_b.format if input_b.format else input_a.format

        return MergeGeometry(
            output_width=out_w,
            output_height=out_h,
            output_bbox=output_bbox,
            bbox_a=bbox_a,
            bbox_b=bbox_b,
            bbox_mask=bbox_mask,
            resolution_a=(wa, ha),
            resolution_b=(wb, hb),
            resolution_mask=resolution_mask,
            output_format=output_format,
        )

    def _auto_align_color_spaces(
        self, input_a: TextureResult, input_b: TextureResult
    ) -> tuple[TextureResult, TextureResult]:
        """Auto-convert inputs to a common linear working space before merge.

        The merge/blend shader performs Porter-Duff and photographic blending
        whose math is only correct in **linear light**. Two scenarios are
        handled:

        1. Mixed spaces (one sRGB, one linear): the sRGB input is decoded to
           linear so both inputs match.
        2. Both sRGB: BOTH are decoded to linear. Previously this case was
           skipped (``cs_a == cs_b`` returned early), which meant the over
           formula ran on gamma-encoded values, producing a visible darkening
           on semi-transparent edges (sRGB is a gamma curve; linear
           interpolation and gamma encoding do not commute).

        Both inputs end up linear, so the output is correctly linear too.
        Inputs already linear (or unknown) are passed through unchanged.

        Note: with the unified IDT hook (``_apply_idt_to_source_outputs``)
        now decoding every sRGB source to linear at the graph boundary,
        this method is mostly a defensive fallback — effect-node outputs
        propagate the (linear) source color_space unchanged, so merge
        inputs should already be linear. The sRGB branches here still
        catch edge cases where a cs_transform node explicitly converts
        data back to sRGB, or a legacy effect produces sRGB output.
        """
        from ..core.color_space import ColorSpace

        cs_a = input_a.color_space or ColorSpace.UNKNOWN
        cs_b = input_b.color_space or ColorSpace.UNKNOWN

        # Case: both already linear (or both unknown) — nothing to do.
        if cs_a == cs_b and cs_a in (ColorSpace.LINEAR, ColorSpace.UNKNOWN):
            return input_a, input_b

        # Decode any encoded input to linear. sRGB and Rec.709 ("bt709") are
        # both encoded spaces that must be decoded before merge math runs in
        # linear light. We decode BOTH when they're both encoded (the old code
        # skipped this and let the over math run on gamma-encoded values,
        # darkening semi-transparent merges). ACEScg ("aces_cg") is linear
        # but AP1-gamut — converted to the working-space gamut instead.
        def _encoded_transfer(cs: str) -> str | None:
            """Return the transfer name to decode, or None if already linear."""
            if cs == ColorSpace.SRGB:
                return "srgb"
            if cs == "bt709":
                return "bt709"
            return None

        def _decode_primaries(cs: str) -> str:
            """Source gamut for the decode (AP1 for ACEScg, else BT.709)."""
            return "ap1" if cs in ("aces_cg", "acescg") else "bt709"

        ta = _encoded_transfer(cs_a)
        if ta is not None or cs_a in ("aces_cg", "acescg"):
            logger.debug(f"Auto-converting merge input A from {cs_a} to linear")
            input_a = self._convert_to_linear(
                input_a, transfer=ta or "linear", primaries=_decode_primaries(cs_a))
        tb = _encoded_transfer(cs_b)
        if tb is not None or cs_b in ("aces_cg", "acescg"):
            logger.debug(f"Auto-converting merge input B from {cs_b} to linear")
            input_b = self._convert_to_linear(
                input_b, transfer=tb or "linear", primaries=_decode_primaries(cs_b))

        # If they're still mismatched after sRGB handling (e.g. rec709 vs
        # aces_cg), log once so the user knows the merge is not colour-managed
        # for those spaces, but proceed without conversion.
        cs_a2 = input_a.color_space or ColorSpace.UNKNOWN
        cs_b2 = input_b.color_space or ColorSpace.UNKNOWN
        if cs_a2 != cs_b2 and cs_a2 != ColorSpace.UNKNOWN and cs_b2 != ColorSpace.UNKNOWN:
            logger.warning(
                f"Color space mismatch in merge after sRGB decode: "
                f"A={cs_a2} ({input_a.width}x{input_a.height}), "
                f"B={cs_b2} ({input_b.width}x{input_b.height}). "
                f"Proceeding without further conversion."
            )

        return input_a, input_b

    def _convert_to_linear(self, tex: TextureResult, transfer: str = "srgb",
                           primaries: str = "bt709") -> TextureResult:
        """Convert an encoded texture to linear via the cs_transform shader.

        Args:
            tex: source texture (encoded in ``transfer``).
            transfer: "srgb" (IEC 61966-2-1) or "bt709" (BT.709 OETF). The
                two curves differ (~gamma 2.2 vs ~2.4), which is exactly the
                image-vs-video distinction the color system must preserve.
            primaries: source gamut ("bt709" / "bt2020" / "smpte431" /
                "smpte432" / "ap1" (ACEScg)). The shader gamut-converts source
                primaries to the BT.709 working space during decode, so
                wide-gamut HDR (BT.2020+PQ/HLG) and ACEScg sources enter the
                graph correctly. BT.709 (the common SDR case) is a no-op.
        """
        from ..core.color_space import ColorSpace

        prog = self.utility_shaders.get('cs_transform')
        if prog is None:
            logger.warning("cs_transform shader not available, skipping %s→linear conversion", transfer)
            return tex

        w, h = tex.width, tex.height
        out_tex = self._resource_pool.acquire_texture(w, h)
        out_fbo = self.fb_manager.get_fbo(out_tex)
        out_fbo.use()
        self.ctx.viewport = (0, 0, w, h)
        self.ctx.clear(0.0, 0.0, 0.0, 0.0)

        self._bind_external_texture(tex.texture_id)
        prog['u_texture'].value = 0
        prog['u_direction'].value = 0  # 0 = decode (encoded → linear)
        try:
            prog['u_input_premultiplied'].value = bool(tex.alpha_premultiplied)
        except (KeyError, AttributeError):
            pass
        # u_transfer uniform IDs (must match _CS_TRANSFORM_FRAGMENT):
        #   0=sRGB, 1=Rec.709, 2=Linear, 3=PQ, 4=HLG.
        _TRANSFER_ID = {
            "srgb": 0, "bt709": 1, "linear": 2,
            "pq": 3, "smpte2084": 3,
            "hlg": 4, "arib-std-b67": 4,
            # RC7：TRAW PWL-log12（_CS_TRANSFORM_FRAGMENT u_transfer=5）
            "traw-log0": 5, "topos-traw-log0": 5,
        }        # u_primaries uniform IDs: 0=BT.709 (no-op), 1=BT.2020, 2=DCI-P3,
        # 3=AP1/ACEScg (matches _CS_TRANSFORM_FRAGMENT).
        _PRIMARIES_ID = {
            "bt709": 0, "bt601": 0, "smpte170m": 0,
            "bt2020": 1, "bt2020nc": 1,
            "smpte431": 2, "smpte432": 2, "displayp3": 2,
            "ap1": 3, "acescg": 3,
        }
        try:
            prog['u_transfer'].value = _TRANSFER_ID.get(transfer, 0)
        except (KeyError, AttributeError):
            pass
        try:
            prog['u_primaries'].value = _PRIMARIES_ID.get(primaries, 0)
        except (KeyError, AttributeError):
            pass
        self._render_quad(prog)
        # P1-6: source texture is pooled — drop the bind before returning.
        self._unbind_texture_unit(0)

        return TextureResult(
            texture_id=out_tex.glo, width=w, height=h,
            format=tex.format,
            bbox=tex.bbox,
            alpha_premultiplied=tex.alpha_premultiplied,
            color_space=ColorSpace.LINEAR,
            # 透传源 revision/bit_depth/metadata：视频源逐帧复用同一 GL
            # 纹理对象（glo 不变），下游 cache key 靠 revision 区分新帧。
            # IDT 结果若固定 revision=0，下游（blur/merge/output…）缓存键
            # 永不变化 → 逐帧 cache hit → 播放画面冻结在首帧。
            revision=tex.revision,
            bit_depth=tex.bit_depth,
            frame_metadata=tex.frame_metadata,
        )

    def _apply_idt_to_source_outputs(self, outputs: dict) -> dict:
        """Apply the Input Device Transform to source-node outputs.

        This is the central IDT hook for composite color management. Every
        source node's output is normalised to the scene-linear working space
        before it enters the effect graph, so all downstream effects and
        merges run on linear light:

          - ``srgb`` outputs → decoded to linear via the sRGB inverse EOTF.
          - ``bt709`` outputs → decoded to linear via the BT.709 inverse
            OETF (distinct curve — video sources keep their transfer).
          - ``aces_cg`` outputs → gamut-only IDT: already scene-linear,
            AP1 primaries converted to the BT.709 working-space gamut.
          - ``linear`` / ``unknown`` / ``None`` → passed through unchanged.
            UNKNOWN is treated as already-linear on purpose: most generators
            (Constant/Ramp/Noise) produce linear data, and legacy nodes
            without an explicit tag should not be re-encoded.

        Called from ``_evaluate_snapshot`` for every node whose action is
        ``source``. Centralising the IDT here means source nodes stay
        tag-only (they declare the space their pixels are in) and the
        conversion happens once, uniformly, at the graph boundary.

        Args:
            outputs: Source node output dict (port_name -> TextureResult).
                May contain non-TextureResult marker entries; those are
                passed through untouched.

        Returns:
            A new dict with encoded TextureResults replaced by linear ones.
            Other entries are unchanged.
        """
        from ..core.color_space import ColorSpace

        if not outputs:
            return outputs

        new_outputs = outputs
        for port_name, value in outputs.items():
            if not isinstance(value, TextureResult):
                continue
            if not value.is_valid:
                continue
            cs = value.color_space
            if cs == ColorSpace.SRGB:
                transfer = "srgb"
            elif cs == "bt709":
                # Rec.709 video: decode with the BT.709 inverse OETF.
                transfer = "bt709"
            elif cs in ("aces_cg", "acescg"):
                # ACEScg: already scene-linear in AP1 primaries — no transfer
                # decode; the gamut matrix (AP1→BT.709 working space) is the
                # whole IDT. (C6: previously these tags fell through to the
                # pass-through branch and AP1 data entered the graph un-
                # converted, shifting every ACEScg source's hues.)
                transfer = "linear"
            elif cs in ("pq", "smpte2084"):
                # HDR10: decode with the PQ (ST.2084) inverse EOTF. Linear
                # result is in nits (0..10000) — HDR working space, unclamped.
                transfer = "pq"
            elif cs in ("hlg", "arib-std-b67"):
                # HLG broadcast HDR: decode with the HLG inverse OETF.
                transfer = "hlg"
            else:
                # LINEAR / UNKNOWN / None / others: pass through.
                # ("aces2065_1"/AP0 stays pass-through: no verified AP0
                # matrix yet — same as before this change.)
                continue
            # Source gamut (primaries). HDR transfers (PQ/HLG) are defined for
            # BT.2020 content; SDR video/stills are BT.709. The shader converts
            # the source gamut -> BT.709 working space during decode so
            # wide-gamut HDR enters the graph in the correct space (H-3 fix).
            primaries = "bt709"
            if transfer in ("pq", "hlg"):
                primaries = "bt2020"
            elif transfer == "linear" and cs in ("aces_cg", "acescg"):
                primaries = "ap1"
            else:
                # Best-effort: read color_primaries from embedded frame metadata
                # (video sources carry it; image sources don't). The metadata
                # may be a dict (TimelineClipFrameProvider writes
                # {"color_primaries": ...}) OR an object with attributes (legacy
                # ColorOutput / some source nodes) — use _metadata_get to read
                # either form. Without this, BT.2020/P3 SDR sources silently
                # fell back to BT.709 because getattr(dict, key) is always None.
                meta = getattr(value, "frame_metadata", None)
                meta_prim = _metadata_get(meta, "color_primaries")
                if meta_prim in ("bt2020", "bt2020nc"):
                    primaries = "bt2020"
                elif meta_prim in ("smpte431", "smpte432"):
                    primaries = "smpte432"
            # Lazy-create the replacement dict so we only allocate when
            # at least one conversion actually happens.
            if new_outputs is outputs:
                new_outputs = dict(outputs)
            try:
                new_outputs[port_name] = self._convert_to_linear(
                    value, transfer=transfer, primaries=primaries
                )
            except Exception as e:
                # Never let a colour-space conversion failure kill the
                # whole graph evaluation — fall back to the raw input
                # and log so the user can see something is off.
                logger.warning(
                    f"IDT {transfer}/{primaries}→linear conversion failed for "
                    f"port '{port_name}' (tex {value.texture_id}): {e}. "
                    f"Passing through unconverted."
                )
        return new_outputs

    def _check_color_space_mismatch(self, input_a: TextureResult, input_b: TextureResult,
                                     operation_name: str = "merge") -> str:
        """Check color space mismatch between two inputs and log warning (S2-D).

        Short-term behavior: If A/B color_space is inconsistent, log a warning.
        The output will inherit color_space from input_b (foreground) as fallback.
        Future: Insert explicit cs_transform node or use working space color management.

        Args:
            input_a: Background input texture (A).
            input_b: Foreground input texture (B).
            operation_name: Name of the operation for log message (e.g., "merge", "deep_merge").

        Returns:
            The output color_space to use (currently returns input_b.color_space or input_a.color_space).

        Note:
            This method only logs warnings, it does NOT modify the inputs.
            The actual color_space assignment happens in the output TextureResult creation.
        """
        from ..core.color_space import ColorSpace

        cs_a = input_a.color_space
        cs_b = input_b.color_space

        # Handle None/unknown color_space — treat as unspecified
        if cs_a is None:
            cs_a = ColorSpace.UNKNOWN
        if cs_b is None:
            cs_b = ColorSpace.UNKNOWN

        # Check for mismatch
        if cs_a != cs_b:
            # Log warning for mismatched color spaces
            logger.warning(
                f"Color space mismatch in {operation_name}: "
                f"A={cs_a} ({input_a.width}x{input_a.height}), "
                f"B={cs_b} ({input_b.width}x{input_b.height}). "
                f"Output will inherit {cs_b if input_b.color_space else cs_a}. "
                f"Consider inserting a CS Transform node for explicit conversion."
            )

        # Determine output color_space (inherit from B first, then A as fallback)
        output_cs = input_b.color_space or input_a.color_space
        return output_cs

    # ── GPU Helpers ──

    def _bind_texture_unit(self, texture_id: int, unit: int) -> None:
        """Bind an OpenGL 2D texture to a texture unit on the render thread."""
        from ..core.gl_bind import bind_texture_unit
        bind_texture_unit(texture_id, unit)

    def _unbind_texture_unit(self, unit: int) -> None:
        """Unbind a texture unit after a multi-input effect has rendered."""
        self._bind_texture_unit(0, unit)

    def _get_white_mask_texture(self):
        """Cached 1×1 all-white RGBA texture for absent mask inputs.

        Pool textures are never initialized (fresh allocations read as
        zero), so the old acquire-a-'white'-tex-from-the-pool pattern
        actually sampled black and turned every masked effect into a
        silent passthrough.  The texture is created once per evaluator
        and lives as long as the context.
        """
        tex = getattr(self, "_white_mask_tex", None)
        if tex is None:
            tex = self.ctx.texture((1, 1), 4, data=b"\xff" * 4, dtype="f1")
            self._white_mask_tex = tex
        return tex

    def _bind_external_texture(self, texture_id: int) -> None:
        """Bind an external texture to texture unit 0."""
        self._bind_texture_unit(texture_id, 0)

    def _bind_mask_texture(self, texture_id: int):
        """Bind a mask texture to texture unit 1."""
        self._bind_texture_unit(texture_id, 1)

    def _get_texture_by_id(self, texture_id: int) -> Optional[object]:
        """Get a ModernGL texture object by its OpenGL texture ID."""
        if hasattr(self.fb_manager, '_active_textures'):
            for tex in self.fb_manager._active_textures:
                if tex.glo == texture_id:
                    return tex

        if hasattr(self.fb_manager, '_texture_pool'):
            for tex_list in self.fb_manager._texture_pool.values():
                for tex in tex_list:
                    if tex.glo == texture_id:
                        return tex

        if hasattr(self._resource_pool, '_pooled_textures'):
            for tex_list in self._resource_pool._pooled_textures.values():
                for tex in tex_list:
                    if hasattr(tex, 'glo') and tex.glo == texture_id:
                        return tex

        return None

    def _render_quad(self, program):
        """Render a fullscreen quad."""
        if not hasattr(self, '_quad_vbo') or self._quad_vbo is None:
            self._quad_vbo = self.ctx.buffer(np.array([
                -1.0, -1.0, 0.0, 0.0,
                 1.0, -1.0, 1.0, 0.0,
                -1.0,  1.0, 0.0, 1.0,
                 1.0,  1.0, 1.0, 1.0,
            ], dtype='f4'))
        if not hasattr(self, '_vao_cache'):
            self._vao_cache = {}
        prog_id = id(program)
        if prog_id not in self._vao_cache:
            vao = self.ctx.vertex_array(
                program,
                [(self._quad_vbo, '2f 2f', 'in_vert', 'in_uv')],
            )
            self._vao_cache[prog_id] = vao
        self._vao_cache[prog_id].render(moderngl.TRIANGLE_STRIP)

    # ── Helper Methods ──

    def _create_ephemeral_node(self, ns: NodeSnapshot):
        """Create a temporary node instance from a snapshot.

        Phase 2 F2-02: Uses NODE_REGISTRY from node_engine.py as single source of truth.
        """
        cls = NODE_REGISTRY.get(ns.node_type)
        if cls is None:
            return None
        node = cls(node_id=ns.node_id)

        frame_number = getattr(self._snapshot, 'frame_number', 0) if hasattr(self, '_snapshot') else 0
        if getattr(ns, "parameters_resolved", True):
            # N-05：快照 parameters 已是引擎在求值帧解析后的最终值
            # （关键帧插值 + 表达式 + 父级世界变换）。渲染侧再对原始
            # 轨道插值会覆盖世界参数（父 x=100 + 子轨道 x 0→10，帧 5
            # 应为 105，被覆盖回 5），也会丢弃表达式/loopOut 结果。
            for key, val in ns.parameters.items():
                node.parameters[key] = val
        else:
            # 手工构造/测试桩快照（未解析）：渲染侧按帧插值原始轨道，
            # 快照静态参数只补插值未覆盖的键（历史行为）。
            interpolated_params = self._interpolate_parameters(ns, frame_number)
            for key, val in interpolated_params.items():
                node.parameters[key] = val
            for key, val in ns.parameters.items():
                if key not in interpolated_params:
                    node.parameters[key] = val

        node.disabled = ns.disabled
        # P3-1: honour the node's time_range. When the current frame is
        # outside [comp_start, comp_end], the node is inactive: source nodes
        # produce nothing (disabled → empty output), effect nodes bypass
        # (disabled → pass input through). Output nodes are never gated.
        if (not is_output_node_type(ns.node_type) and ns.time_range
                and ns.time_range.get("enabled", True)):
            cur = frame_number
            start = ns.time_range.get("comp_start", cur)
            end = ns.time_range.get("comp_end", cur)
            if cur < start or cur > end:
                node.disabled = True
        return node

    def _interpolate_parameters(self, ns: NodeSnapshot, frame: int) -> Dict[str, Any]:
        """Interpolate animated parameter values at the given frame.

        int parameters are rounded/clamped to int after interpolation so
        animated index/count params do not leak float values into shaders
        (P1.14). float parameters stay float.
        """
        from src.shared.keyframe import keyframe_track_from_dict

        result: Dict[str, Any] = {}

        if not ns.keyframe_tracks:
            return result

        # Build a quick lookup of per-param metadata for type-aware casting.
        meta = ns.param_defs_meta or {}

        for param_name, track_data in ns.keyframe_tracks.items():
            # S6: a single corrupted track (bad type, missing fields, ...) must
            # not abort interpolation for the whole node — skip it and keep the
            # node's static parameter value.
            try:
                if not isinstance(track_data, dict):
                    continue
                # S7: prefer the track's own bounds, but fall back to ParamDef
                # metadata when absent so values are not silently clamped to
                # [0,1] by KeyframeTrack's defaults (e.g. translate_x=100 must
                # not become 1.0 on a repaired/legacy track missing min/max).
                meta_entry = meta.get(param_name, {})
                min_val = track_data.get("min_val", meta_entry.get("min_val", 0.0))
                max_val = track_data.get("max_val", meta_entry.get("max_val", 1.0))
                ptype = meta_entry.get("ptype", "")
                value_type = ptype if ptype in {
                    "color", "vec2", "vec3", "bool", "option"
                } else track_data.get("value_type", "scalar")
                track = keyframe_track_from_dict({
                    "param_name": param_name,
                    "value_type": value_type,
                    "keyframes": track_data.get("keyframes", []),
                    "min_val": min_val,
                    "max_val": max_val,
                    "pre_extrap": track_data.get("pre_extrap", "constant"),
                    "post_extrap": track_data.get("post_extrap", "constant"),
                })
                if not track.keyframes:
                    continue
                default_val = ns.parameters.get(param_name, _typed_default(value_type))
                interpolated_val = track.get_value(frame, default=default_val)

                # Cast int params back to int (P1.14)
                if ptype == "int":
                    try:
                        interpolated_val = int(round(float(interpolated_val)))
                    except (TypeError, ValueError):
                        pass
                elif ptype == "bool":
                    interpolated_val = bool(interpolated_val)
                elif ptype == "option":
                    interpolated_val = str(interpolated_val)
                result[param_name] = interpolated_val
            except Exception:
                continue

        return result

    def _first_output_name(self, ns: NodeSnapshot) -> str:
        """Get the name of the first output port for a node."""
        if ns.output_ports:
            return ns.output_ports[0][0]
        return "result"

    def _first_image_port(self, ns: NodeSnapshot) -> str:
        """Pick the output port whose texture the viewer should show for a node.

        Preference order: the first port whose declared data type is ``image``;
        otherwise the first declared port; otherwise the legacy ``result``
        fallback. Used by the node-preview pipeline to present an arbitrary
        node's output to the viewer (mirrors the thumbnail-readback port logic
        in ``composite_renderer._handle_readback_node_thumbnail``).
        """
        if is_output_node_type(ns.node_type):
            return "display"
        if ns.output_ports:
            for name, dtype in ns.output_ports:
                if dtype == "image":
                    return name
            return ns.output_ports[0][0]
        return "result"

    # ── Fused Chain Execution ──

    def _execute_fused_chain(self, chain, snapshot, cache, plan,
                             render_mode, cache_mgr, request_id):
        """Execute a fused chain."""

        first_np = plan.node_plans.get(chain.node_ids[0])
        if first_np is None:
            return None

        input_tex = None
        for conn in snapshot.get_input_connections(chain.node_ids[0]):
            src_outputs = cache.get(conn.src_node_id, {})
            if conn.src_port_name in src_outputs:
                input_tex = src_outputs[conn.src_port_name]
                break

        if not input_tex or not isinstance(input_tex, TextureResult) or not input_tex.is_valid:
            return None

        params_list = []
        for nid in chain.node_ids:
            ns = snapshot.nodes.get(nid)
            params_list.append(ns.parameters if ns else {})

        input_meta = f"{input_tex.texture_id}:{input_tex.revision}:{input_tex.width}x{input_tex.height}"
        cache_key_str = f"fused:{chain.fusion_key}:{input_meta}:{render_mode}"
        cache_key = cache_mgr.compute_key_from_string(cache_key_str)
        bypass_persistent_cache = bool(
            set(chain.node_ids) & getattr(self, '_dynamic_cache_bypass_nodes', set())
        )

        entry = None if bypass_persistent_cache else cache_mgr.get(cache_key)
        if entry is not None:
            plan.cache_hits += 1
            last_id = chain.node_ids[-1]
            outputs = entry.outputs
            for nid in chain.node_ids:
                if nid != last_id:
                    cache[nid] = {}
            return outputs

        plan.cache_misses += 1

        w, h = input_tex.width, input_tex.height
        try:
            result = self._apply_fused_chain(chain, input_tex, params_list,
                                             w, h)
        except Exception as exc:
            # A fused-draw failure must degrade to the per-node passes,
            # never take the whole frame down (P1-9 failure diagnostics).
            logger.warning(
                "Fused chain %s failed (%s); falling back to individual "
                "evaluation", chain.fusion_key[:12], exc)
            result = None

        if result is None:
            result = self._fallback_individual_eval(chain, snapshot, cache, plan, render_mode)
            if bypass_persistent_cache:
                for tex_result in result.values():
                    if (
                        isinstance(tex_result, TextureResult)
                        and tex_result.is_valid
                        and tex_result.texture_id != input_tex.texture_id
                    ):
                        self._transient_texture_ids_to_release.add(tex_result.texture_id)
            return result

        last_id = chain.node_ids[-1]
        owned_ids = set()
        for tex_result in result.values():
            if isinstance(tex_result, TextureResult) and tex_result.is_valid:
                if tex_result.texture_id != input_tex.texture_id:
                    owned_ids.add(tex_result.texture_id)

        if bypass_persistent_cache:
            self._transient_texture_ids_to_release.update(owned_ids)
        else:
            new_entry = CacheEntry(outputs=result, owned_texture_ids=owned_ids)
            cache_mgr.put(cache_key, new_entry, render_mode=render_mode)
            cache_mgr.pin(cache_key)

        for nid in chain.node_ids:
            if nid != last_id:
                cache[nid] = {}

        return result

    def _fallback_individual_eval(self, chain, snapshot, cache, plan, render_mode):
        """Fallback: evaluate chain nodes individually."""
        for nid in chain.node_ids:
            ns = snapshot.nodes.get(nid)
            if ns is None:
                cache[nid] = {}
                continue

            input_textures = {}
            for conn in snapshot.get_input_connections(nid):
                src_outputs = cache.get(conn.src_node_id, {})
                if conn.src_port_name in src_outputs:
                    input_textures[conn.dst_port_name] = src_outputs[conn.src_port_name]

            outputs = self._evaluate_node(ns, input_textures, render_mode=render_mode)
            outputs = self._resolve_output(ns, outputs)
            cache[nid] = outputs

        last_id = chain.node_ids[-1]
        return cache.get(last_id, {})

    def _apply_fused_chain(self, chain, input_tex: TextureResult,
                           params_list: list, w: int, h: int):
        """Execute a fused chain as a single GPU pass."""
        prog = self.utility_shaders.get('fused')
        if prog is None:
            return None

        out_tex = self._resource_pool.acquire_texture(w, h)
        out_fbo = self.fb_manager.get_fbo(out_tex)
        out_fbo.use()
        self.ctx.viewport = (0, 0, w, h)
        self.ctx.clear(0.0, 0.0, 0.0, 0.0)

        self._bind_external_texture(input_tex.texture_id)
        prog['u_texture'].value = 0

        prog['u_enable_unpremult'].value = False
        prog['u_enable_cs_transform'].value = False
        prog['u_enable_grade'].value = False
        prog['u_enable_invert'].value = False
        prog['u_enable_shuffle'].value = False
        prog['u_enable_premult'].value = False

        from ..core.color_space import ColorSpace
        alpha_premultiplied = input_tex.alpha_premultiplied
        color_space = input_tex.color_space

        # Fused colour operations must run on straight RGB.  Explicit alpha
        # nodes still control the public contract, but when a chain starts
        # with premultiplied data and has no explicit unpremult stage, inject
        # the decode/encode pair around the fixed shader stage order.
        has_explicit_unpremult = "unpremult" in chain.node_types
        has_explicit_premult = "premult" in chain.node_types
        auto_unpremult = (input_tex.alpha_premultiplied is True
                          and not has_explicit_unpremult)
        auto_repremult = auto_unpremult and not has_explicit_premult
        if auto_unpremult:
            prog['u_enable_unpremult'].value = True
            alpha_premultiplied = False

        for node_type, params in zip(chain.node_types, params_list):
            if node_type == "unpremult":
                prog['u_enable_unpremult'].value = True
                alpha_premultiplied = False
            elif node_type == "cs_transform":
                prog['u_enable_cs_transform'].value = True
                # Raw node param is a mode LABEL; the fused shader takes
                # an int (0=srgb→linear, 1=linear→srgb). analyze_fusion
                # gates other modes out of chains — map defensively.
                mode = str(params.get("direction", "srgb_to_linear"))
                prog['u_cs_direction'].value = (
                    0 if mode == "srgb_to_linear" else 1)
                color_space = (ColorSpace.SRGB if mode == "linear_to_srgb"
                               else ColorSpace.LINEAR)
            elif node_type == "grade":
                prog['u_enable_grade'].value = True
                # Raw node params are per-channel scalars (lift_r/…);
                # the vec3 uniforms need the translated triples (same
                # translation the node's own effect marker performs).
                prog['u_grade_lift'].value = (
                    float(params.get("lift_r", 0.0)),
                    float(params.get("lift_g", 0.0)),
                    float(params.get("lift_b", 0.0)))
                prog['u_grade_gamma'].value = (
                    float(params.get("gamma_r", 1.0)),
                    float(params.get("gamma_g", 1.0)),
                    float(params.get("gamma_b", 1.0)))
                prog['u_grade_gain'].value = (
                    float(params.get("gain_r", 1.0)),
                    float(params.get("gain_g", 1.0)),
                    float(params.get("gain_b", 1.0)))
                prog['u_grade_offset'].value = (
                    float(params.get("offset_r", 0.0)),
                    float(params.get("offset_g", 0.0)),
                    float(params.get("offset_b", 0.0)))
            elif node_type == "invert":
                prog['u_enable_invert'].value = True
                # Raw node params are booleans (invert_r/…), not a
                # "channels" list — translate exactly like the node's
                # own effect marker does.
                prog['u_invert_channels'].value = tuple(
                    1.0 if bool(params.get(f"invert_{c}", default)) else 0.0
                    for c, default in (("r", True), ("g", True),
                                       ("b", True), ("a", False)))
            elif node_type == "shuffle":
                prog['u_enable_shuffle'].value = True
                # The fused chain reads RAW node parameters — channel
                # names ("r"/"luma"/…) arrive as strings while the shader
                # uniforms are int indices; assigning a str raises and
                # blackens the whole fused chain.
                from ..nodes.shuffle_node import CHANNEL_SOURCES as _ch_src

                def _shuffle_idx(name, default_idx: int) -> int:
                    if isinstance(name, int):
                        return name
                    try:
                        return _ch_src.index(str(name))
                    except ValueError:
                        return default_idx

                prog['u_shuffle_r_from'].value = _shuffle_idx(params.get("r_from", 0), 0)
                prog['u_shuffle_g_from'].value = _shuffle_idx(params.get("g_from", 1), 1)
                prog['u_shuffle_b_from'].value = _shuffle_idx(params.get("b_from", 2), 2)
                prog['u_shuffle_a_from'].value = _shuffle_idx(params.get("a_from", 3), 3)
            elif node_type == "premult":
                prog['u_enable_premult'].value = True
                alpha_premultiplied = True

        if auto_repremult:
            prog['u_enable_premult'].value = True
            alpha_premultiplied = True

        self._render_quad(prog)
        # P1-6: the chain input is an upstream pooled texture — never
        # leave unit 0 bound past the fused draw.
        self._unbind_texture_unit(0)

        last_ns = self._snapshot.nodes.get(chain.node_ids[-1]) if self._snapshot else None
        if last_ns and last_ns.output_ports:
            out_name = last_ns.output_ports[0][0]
        else:
            out_name = "result"

        return {
            out_name: TextureResult(
                texture_id=out_tex.glo, width=w, height=h,
                format=input_tex.format,
                bbox=input_tex.bbox,
                alpha_premultiplied=alpha_premultiplied,
                color_space=color_space,
                # AUD-5801: 透传链输入 revision。TextureResult.revision 默认 0，
                # 融合链缓存条目被驱逐、glo 归还池后被同尺寸复用时，恒 0 的
                # revision 会让下游 (glo, revision) 缓存键命中内容不同的旧
                # 条目（陈旧帧/冻结画面）。与 _convert_to_linear 的 IDT
                # 透传同一机制。
                revision=input_tex.revision,
            )
        }

    # ── Shader Loading ──

    def _load_shaders(self):
        """Load all utility and effect shaders via ShaderLibrary."""
        from src.features.composite.evaluators.shader_library import ShaderLibrary
        from ..render.compute_shader_manager import ComputeShaderManager

        self._shader_lib = ShaderLibrary(self.ctx)
        for name in ShaderLibrary.STANDARD_NAMES:
            prog = self._shader_lib.get_program(name)
            if prog:
                self.utility_shaders[name] = prog

        self._compute_shader_manager = ComputeShaderManager(self.ctx)
        if self._compute_shader_manager.is_available():
            logger.info("Compute shaders available for large-radius effects")
        else:
            logger.info("Compute shaders unavailable, using fragment shader fallback")
