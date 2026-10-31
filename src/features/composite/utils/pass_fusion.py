"""
Pass Fusion - Analyze and combine consecutive fusible effect nodes.

Detects chains of single-input, per-pixel effect nodes in the evaluation order
that can be merged into a single GPU pass using the fused mega-shader.

V3 Enhancement:
- Extended FUSIBLE_TYPES for more color-only nodes
- Texture size compatibility check (fusion only when sizes match)
- Fusion mode configuration (aggressive/conservative/off)
- Two shader types: fused (basic) and fused_color (enhanced)

Fusion conditions:
- All nodes have exactly one input and one output
- All node types have fusible=True in capability (is_fusible(node_type))
- No fan-out (node output consumed by only one downstream)
- No ROI expansion (roi_expansion == 0)
- Node types appear in an order compatible with stage order
- Texture sizes must match (only in conservative mode)

Phase 2 F2-05: Capability Migration
- Fusion eligibility now uses capability.fusible (from node_capabilities)
- Stage order compatibility still uses shader-specific lists
- FUSIBLE_TYPES/NON_FUSIBLE_TYPES are DEPRECATED (kept for compatibility)
"""

import hashlib
import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional, Set, Tuple, TYPE_CHECKING

from ..core.node_capabilities import is_fusible as capability_is_fusible

if TYPE_CHECKING:
    from ..render.render_scheduler import NodePlan, RenderPlan
    from ..core.graph_snapshot import GraphSnapshot, NodeSnapshot


# ── Fusion Mode Configuration ──

class FusionMode(Enum):
    """Pass fusion behavior mode."""
    OFF = "off"           # No fusion, all nodes execute individually
    CONSERVATIVE = "conservative"  # Only fuse when texture sizes match
    AGGRESSIVE = "aggressive"  # Fuse even with size mismatch (auto-resize)


# ── Node type classifications for fusion ──
# DEPRECATED: These lists are kept for backward compatibility.
# Use capability_is_fusible(node_type) from node_capabilities for fusion eligibility.
# FUSIBLE_BASIC_TYPES and FUSIBLE_COLOR_TYPES are still used for shader type selection.

# Basic fusible types (supported by _FUSED_FRAGMENT)
# These types are handled by the basic fused shader
FUSIBLE_BASIC_TYPES = frozenset({
    "grade", "invert", "shuffle", "premult", "unpremult", "cs_transform"
})

# Extended fusible types for color-only operations (supported by _FUSED_COLOR_FRAGMENT)
# These types require the enhanced color shader with more stages
FUSIBLE_COLOR_TYPES = frozenset({
    "grade", "color_correct", "huecorrect", "clamp", "normalize",
    "invert", "shuffle", "premult", "unpremult", "cs_transform"
})

# DEPRECATED: All fusible types (union of basic and color)
# Use capability_is_fusible(node_type) instead
FUSIBLE_TYPES = FUSIBLE_BASIC_TYPES | FUSIBLE_COLOR_TYPES

# DEPRECATED: Node types that are NOT fusible
# Use capability_is_fusible(node_type) instead (returns False for these)
NON_FUSIBLE_TYPES = frozenset({
    "blur", "defocus", "sharpen", "erode_dilate", "glow",
    "transform", "cornerpin", "reformat", "crop",
    "merge", "blend", "dissolve", "switch",
    "luma_keyer", "chroma_keyer", "matte", "lut",
    "image_input", "color_input", "constant", "output",
})


# ── Stage order for shader compatibility ──

# Fixed stage order in _FUSED_FRAGMENT (basic)
FUSED_BASIC_STAGE_ORDER = [
    "unpremult", "cs_transform", "grade", "invert", "shuffle", "premult"
]

# Fixed stage order in _FUSED_COLOR_FRAGMENT (enhanced)
# This order is optimal for color correction workflow
FUSED_COLOR_STAGE_ORDER = [
    "unpremult",      # 1. Decode premultiplied alpha first
    "cs_transform",   # 2. Convert color space
    "grade",          # 3. Basic lift/gamma/gain
    "color_correct",  # 4. Professional color correction
    "huecorrect",     # 5. Selective hue adjustments
    "normalize",      # 6. Value range normalization
    "clamp",          # 7. Value clamping
    "invert",         # 8. Channel inversion
    "shuffle",        # 9. Channel remap
    "premult",        # 10. Re-encode premultiplied alpha
]

# Pre-compute position maps for order compatibility check
_BASIC_STAGE_POSITION = {t: i for i, t in enumerate(FUSED_BASIC_STAGE_ORDER)}
_COLOR_STAGE_POSITION = {t: i for i, t in enumerate(FUSED_COLOR_STAGE_ORDER)}


@dataclass
class FusedChain:
    """A sequence of consecutive fusible nodes executed as one GPU pass.

    Attributes:
        node_ids: Ordered list of node IDs in the chain (eval order).
        node_types: Corresponding node types.
        input_node_id: The upstream node that feeds the chain's first node.
        fusion_key: Hash of all node types and params for cache key.
        shader_type: "fused" or "fused_color" depending on node types.
        texture_sizes: Dict of node_id -> (width, height) for size compatibility.
        size_compatible: False when any two *known* texture sizes in the
            chain differ (unknown sizes are tolerated under the chain
            invariants — see _check_size_compatibility).
    """
    node_ids: List[str]
    node_types: List[str]
    input_node_id: str
    fusion_key: str
    shader_type: str  # "fused" or "fused_color"
    texture_sizes: Dict[str, Tuple[int, int]] = field(default_factory=dict)
    size_compatible: bool = True


@dataclass
class FusionPlan:
    """Result of fusion analysis on a RenderPlan.

    Attributes:
        fused_chains: List of FusedChain objects.
        node_to_chain: Maps node_id -> FusedChain for nodes inside a chain.
        unfused_nodes: Set of node_ids that execute individually.
        fusion_mode: The fusion mode used for analysis.
        stats: Statistics about fusion efficiency.
    """
    fused_chains: List[FusedChain] = field(default_factory=list)
    node_to_chain: Dict[str, FusedChain] = field(default_factory=dict)
    unfused_nodes: Set[str] = field(default_factory=set)
    fusion_mode: FusionMode = FusionMode.CONSERVATIVE
    stats: Dict[str, int] = field(default_factory=dict)


# ── Helper functions ──

def _is_order_compatible(types: List[str], shader_type: str = "fused_color") -> bool:
    """Check if node types appear as a subsequence of stage order.

    Args:
        types: List of node type strings from a candidate chain.
        shader_type: "fused" or "fused_color" to select stage order.

    Returns:
        True if the types appear in a valid order for the shader.
    """
    if not types:
        return False

    stage_position = _COLOR_STAGE_POSITION if shader_type == "fused_color" else _BASIC_STAGE_POSITION

    prev_pos = -1
    for t in types:
        pos = stage_position.get(t)
        if pos is None:
            return False  # Unknown type for this shader
        if pos <= prev_pos:
            return False  # Wrong order
        prev_pos = pos

    return True


def _determine_shader_type(types: List[str]) -> str:
    """Determine which shader to use for a chain.

    Returns:
        "fused_color" if any type requires enhanced shader,
        "fused" if only basic types are present.
    """
    # If any type is only in color types (not in basic), use fused_color
    for t in types:
        if t in FUSIBLE_COLOR_TYPES and t not in FUSIBLE_BASIC_TYPES:
            return "fused_color"
    # If all types are in basic, use fused (lighter shader)
    return "fused"


def _compute_fusion_key(node_ids: List[str], node_types: List[str],
                         params_list: List[dict]) -> str:
    """Compute a hash key for a fused chain.

    Incorporates node types (in order) and all node parameters.
    """
    parts = []
    for ntype, params in zip(node_types, params_list):
        parts.append(ntype)
        params_str = json.dumps(params, sort_keys=True, default=str)
        parts.append(params_str)

    combined = "|".join(parts)
    return hashlib.md5(combined.encode()).hexdigest()[:16]


def _get_texture_dimensions(node_id: str, snapshot: "GraphSnapshot",
                            cache: Dict[str, Dict],
                            node_plan: Optional["NodePlan"] = None,
                            node_snapshot: Optional["NodeSnapshot"] = None
                            ) -> Optional[Tuple[int, int]]:
    """Get texture dimensions for a node from cache, plan bbox or source params.

    Resolution order (AUD-11500 — the production caller analyses fusion
    *before* execution, so the eval cache is empty there and additional
    size sources are required to make the CONSERVATIVE gate meaningful):

    1. Evaluated texture in the cache (execution-time truth).
    2. The planner's estimated output bbox (available when ROI propagation
       ran, i.e. preview requests with a user ROI).
    3. Explicit dimensions declared by source-type nodes — the same
       parameters the scheduler's own bbox pass uses (``constant`` /
       ``image_input`` ``width``/``height`` params, ``color_input`` →
       snapshot color dimensions).

    Returns:
        (width, height) tuple or None if not available.
    """
    # 1. Try to get from cache first (evaluated nodes)
    node_cache = cache.get(node_id, {})
    for port_name, tex_result in node_cache.items():
        if hasattr(tex_result, 'width') and hasattr(tex_result, 'height'):
            return (tex_result.width, tex_result.height)

    # 2. Fall back to the estimated output bbox computed during planning.
    bbox = getattr(node_plan, "estimated_bbox", None)
    if bbox is not None and not getattr(bbox, "is_empty", True):
        return (int(bbox.width), int(bbox.height))

    # 3. Explicit source dimensions.
    node_type = getattr(node_snapshot, "node_type", "")
    params = getattr(node_snapshot, "parameters", None) or {}
    if node_type in ("constant", "image_input"):
        try:
            w = int(params.get("width") or 0)
            h = int(params.get("height") or 0)
        except (TypeError, ValueError):
            return None
        if w > 0 and h > 0:
            return (w, h)
    elif node_type == "color_input":
        w = int(getattr(snapshot, "color_width", 0) or 0)
        h = int(getattr(snapshot, "color_height", 0) or 0)
        if w > 0 and h > 0:
            return (w, h)

    # No size information available from any source
    return None


def _check_size_compatibility(chain_ids: List[str],
                               snapshot: "GraphSnapshot",
                               cache: Dict[str, Dict],
                               plan: Optional["RenderPlan"] = None
                               ) -> Tuple[Dict[str, Tuple[int, int]], bool]:
    """Check that the chain's texture sizes are compatible (AUD-11500).

    A chain is rejected when any two *known* sizes differ. Sizes are
    resolved per node from the eval cache, the planner's estimated bboxes,
    or explicit source dimensions (see :func:`_get_texture_dimensions`).

    Nodes whose size cannot be determined are tolerated: fusion
    eligibility already guarantees the chain is linear, single-input and
    built from per-pixel stages with ``roi_expansion == 0``, so every
    chain node preserves the chain input's size by construction. Refusing
    to fuse merely because a source's size is unknown at analysis time
    (e.g. timeline-backed clips) would disable fusion for the default
    CONSERVATIVE mode without adding any real safety.

    Returns:
        (texture_sizes_dict, is_compatible)
    """
    sizes: Dict[str, Tuple[int, int]] = {}
    reference_size: Optional[Tuple[int, int]] = None
    compatible = True

    for node_id in chain_ids:
        node_plan = plan.node_plans.get(node_id) if plan is not None else None
        node_snapshot = (snapshot.nodes.get(node_id)
                         if snapshot is not None else None)
        size = _get_texture_dimensions(
            node_id, snapshot, cache, node_plan, node_snapshot)
        if size is None:
            # Unknown size — tolerated under the chain invariant above.
            continue
        sizes[node_id] = size
        if reference_size is None:
            reference_size = size
        elif size != reference_size:
            compatible = False

    return sizes, compatible


# ── Main analysis function ──

def analyze_fusion(plan: "RenderPlan", snapshot: "GraphSnapshot",
                   fusion_mode: FusionMode = FusionMode.CONSERVATIVE,
                   cache: Optional[Dict[str, Dict]] = None) -> FusionPlan:
    """Identify fusible chains in the plan's eval_order.

    Algorithm:
    1. If fusion_mode is OFF, return empty plan (no fusion)
    2. Build a fan-out map: node_id -> count of downstream consumers
    3. Walk eval_order, accumulating consecutive fusible nodes
    4. A node breaks the chain if:
       a. Its type is not in FUSIBLE_TYPES
       b. action != "effect"
       c. len(inputs) != 1 or len(output_ports) != 1
       d. roi_expansion > 0
       e. fan-out > 1 (output consumed by multiple downstream)
       f. Order incompatible with stage order
    5. In CONSERVATIVE mode: break chain when known texture sizes differ
       (sizes resolve from the eval cache, estimated bboxes or source
       parameters — AUD-11500)
    6. Chains of length 1 are discarded (no fusion benefit)
    7. Determine shader type (fused or fused_color) based on node types

    Args:
        plan: The RenderPlan with eval_order and node_plans.
        snapshot: The GraphSnapshot with node details and connections.
        fusion_mode: Fusion behavior (off/conservative/aggressive).
        cache: Optional cache with evaluated node texture results.

    Returns:
        FusionPlan mapping fused chains and unfused nodes.
    """
    # Skip all fusion if mode is OFF
    if fusion_mode == FusionMode.OFF:
        all_nodes = set(plan.eval_order)
        return FusionPlan(
            fused_chains=[],
            node_to_chain={},
            unfused_nodes=all_nodes,
            fusion_mode=fusion_mode,
            stats={"total_nodes": len(all_nodes), "fused_chains": 0, "fused_nodes": 0}
        )

    # 1. Build fan-out map: node_id -> downstream consumer count
    fan_out: Dict[str, int] = {}
    for node_id in plan.eval_order:
        np = plan.node_plans.get(node_id)
        if np is None:
            continue
        for port_name, upstream_id in np.inputs.items():
            fan_out[upstream_id] = fan_out.get(upstream_id, 0) + 1

    # 2. Build output_ports count map from snapshot
    output_port_counts: Dict[str, int] = {}
    for node_id in plan.eval_order:
        ns = snapshot.nodes.get(node_id)
        if ns:
            output_port_counts[node_id] = len(ns.output_ports)

    # 3. Walk eval_order and collect fusible chains
    fused_chains: List[FusedChain] = []
    node_to_chain: Dict[str, FusedChain] = {}
    unfused_nodes: Set[str] = set()

    current_chain_ids: List[str] = []
    current_chain_types: List[str] = []
    # AUD-11503: current_shader_type 死状态已移除（维护但 flush_chain 从不读取；
    # 链的实际 shader type 由 _determine_shader_type(current_chain_types) 派生）

    def flush_chain():
        """Flush the accumulated chain if it qualifies."""
        nonlocal current_chain_ids, current_chain_types
        if len(current_chain_ids) >= 2:
            # Determine shader type for this chain
            shader_type = _determine_shader_type(current_chain_types)

            # Check order compatibility
            if _is_order_compatible(current_chain_types, shader_type):
                # Collect params for fusion key
                params_list = []
                for nid in current_chain_ids:
                    ns = snapshot.nodes.get(nid)
                    params_list.append(ns.parameters if ns else {})

                # Find the input node (upstream of first chain node)
                first_id = current_chain_ids[0]
                first_np = plan.node_plans.get(first_id)
                input_node_id = ""
                if first_np and first_np.inputs:
                    input_node_id = next(iter(first_np.inputs.values()))

                fusion_key = _compute_fusion_key(
                    current_chain_ids, current_chain_types, params_list)

                # Check size compatibility (for conservative mode). The plan
                # is passed so sizes can be resolved from estimated bboxes
                # even when the eval cache is still empty (AUD-11500).
                texture_sizes, size_compatible = _check_size_compatibility(
                    current_chain_ids, snapshot, cache or {}, plan)

                # In conservative mode, skip chain if sizes don't match
                if fusion_mode == FusionMode.CONSERVATIVE and not size_compatible:
                    for nid in current_chain_ids:
                        unfused_nodes.add(nid)
                else:
                    chain = FusedChain(
                        node_ids=list(current_chain_ids),
                        node_types=list(current_chain_types),
                        input_node_id=input_node_id,
                        fusion_key=fusion_key,
                        shader_type=shader_type,
                        texture_sizes=texture_sizes,
                        size_compatible=size_compatible,
                    )
                    fused_chains.append(chain)
                    for nid in current_chain_ids:
                        node_to_chain[nid] = chain
            else:
                # Order incompatible — treat as unfused
                for nid in current_chain_ids:
                    unfused_nodes.add(nid)
        else:
            # Chain too short — treat as unfused
            for nid in current_chain_ids:
                unfused_nodes.add(nid)

        current_chain_ids = []
        current_chain_types = []

    for node_id in plan.eval_order:
        np = plan.node_plans.get(node_id)
        if np is None:
            flush_chain()
            continue

        # Check if this node can be fused
        # Phase 2 F2-05: Use capability.fusible instead of hand-written list
        node_ns = snapshot.nodes.get(node_id)
        can_fuse = (
            capability_is_fusible(np.node_type)
            and np.action == "effect"
            and len(np.inputs) == 1
            and output_port_counts.get(node_id, 0) <= 1
            and fan_out.get(node_id, 0) <= 1
            and np.roi_expansion == 0
            # P1-9: node params must stay inside the fused shader's
            # implemented feature set, or the chain silently changes
            # pixels (grade blackpoint/multiply/…, non-sRGB cs modes).
            and (node_ns is None
                 or _params_fusable(np.node_type, node_ns.parameters or {}))
        )

        if can_fuse:
            # Determine which shader type this node needs
            node_shader_type = _determine_shader_type([np.node_type])

            # Check if we need to upgrade shader type
            if current_chain_types:
                # Merge shader type requirements
                merged_types = current_chain_types + [np.node_type]
                candidate_shader_type = _determine_shader_type(merged_types)
            else:
                candidate_shader_type = node_shader_type

            # Check order compatibility with accumulated chain
            candidate_types = current_chain_types + [np.node_type]
            if _is_order_compatible(candidate_types, candidate_shader_type):
                current_chain_ids.append(node_id)
                current_chain_types.append(np.node_type)
            else:
                # Order incompatible — flush current and start new
                flush_chain()
                # Try starting a new chain with just this node
                current_chain_ids = [node_id]
                current_chain_types = [np.node_type]
        else:
            # Node not fusible — flush any accumulated chain
            flush_chain()
            unfused_nodes.add(node_id)

    # Flush any remaining chain
    flush_chain()

    # Compute statistics
    fused_node_count = sum(len(c.node_ids) for c in fused_chains)
    stats = {
        "total_nodes": len(plan.eval_order),
        "fused_chains": len(fused_chains),
        "fused_nodes": fused_node_count,
        "unfused_nodes": len(unfused_nodes),
        "potential_savings": fused_node_count - len(fused_chains),  # Draw calls saved
    }

    return FusionPlan(
        fused_chains=fused_chains,
        node_to_chain=node_to_chain,
        unfused_nodes=unfused_nodes,
        fusion_mode=fusion_mode,
        stats=stats,
    )


# ── Utility functions for external use ──

def _params_fusable(node_type: str, params: dict) -> bool:
    """False when a node's parameters use features the fused mega-shader
    does not implement — the chain must fall back to per-node passes
    instead of silently changing pixels (P1-9 semantics guard).

    The fused shader's grade stage implements lift/gamma/gain/offset
    only; the cs_transform stage implements sRGB↔linear only.
    """
    if node_type == "grade":
        try:
            return (
                float(params.get("blackpoint", 0.0)) == 0.0
                and float(params.get("whitepoint", 1.0)) == 1.0
                and float(params.get("multiply", 1.0)) == 1.0
                and float(params.get("add", 0.0)) == 0.0
                and float(params.get("mix", 1.0)) == 1.0
                and str(params.get("mask_channels", "rgb")) == "rgb"
            )
        except (TypeError, ValueError):
            return False
    if node_type == "cs_transform":
        mode = str(params.get("direction", "srgb_to_linear"))
        return mode in ("srgb_to_linear", "linear_to_srgb")
    # CP-044/045: the generic mix post-pass lives in _resolve_output, which
    # fused chains bypass — a generic mix != 1 must defuse to the per-node
    # path or the blend silently drops. Nodes with their OWN mix param are
    # excluded here (grade handled above; the rest are not fusible anyway).
    _OWN_MIX_FUSIBLE = ("grade",)
    if node_type not in _OWN_MIX_FUSIBLE:
        try:
            if float(params.get("mix", 1.0)) != 1.0:
                return False
        except (TypeError, ValueError):
            return False
    return True


def get_fusion_mode_from_config(config: dict) -> FusionMode:
    """Get fusion mode from configuration dict.

    Args:
        config: Configuration dictionary with 'pass_fusion_mode' key.

    Returns:
        FusionMode enum value.
    """
    mode_str = config.get("pass_fusion_mode", "conservative")
    try:
        return FusionMode(mode_str.lower())
    except ValueError:
        return FusionMode.CONSERVATIVE


def is_node_fusible(node_type: str) -> bool:
    """Check if a node type is eligible for fusion.

    Phase 2 F2-05: Now uses capability.fusible as the source of truth.
    The hand-written FUSIBLE_TYPES list is deprecated.

    Args:
        node_type: Node type string.

    Returns:
        True if node can potentially be fused.
    """
    return capability_is_fusible(node_type)


def get_fusible_types() -> Set[str]:
    """Get all fusible node types.

    Phase 2 F2-05: Returns FUSIBLE_NODE_TYPES from capability system.
    The hand-written FUSIBLE_TYPES list is deprecated.

    Returns:
        Set of node type strings that can be fused.
    """
    from ..core.node_capabilities import FUSIBLE_NODE_TYPES
    return set(FUSIBLE_NODE_TYPES)


def get_color_fusible_types() -> Set[str]:
    """Get color-only fusible node types (requires fused_color shader).

    Returns:
        Set of node type strings that require fused_color shader.
    """
    return FUSIBLE_COLOR_TYPES
