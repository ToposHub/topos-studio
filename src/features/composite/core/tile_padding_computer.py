"""
TilePaddingComputer - Computes padding requirements for tile rendering.

Analyzes the node graph to determine the maximum padding needed at source level
for seamless tile boundaries. Each spatial node (blur, transform, etc.) declares
its padding requirement via _tile_padding() method.

Architecture:
1. Walk the graph backward from output node
2. Accumulate padding from each spatial node
3. Compute total padding at source level
4. Adjust tile size based on GPU memory budget

Key improvements over requires_roi_expansion():
- Explicit padding declaration per node type
- Graph-wide padding chain analysis
- Memory-aware tile size computation
- Support for complex node chains (Blur -> Grade -> Blur)
"""

import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple, TYPE_CHECKING

from .node_capabilities import get_node_capability

if TYPE_CHECKING:
    from .graph_snapshot import GraphSnapshot, NodeSnapshot
    from .format import ImageFormat

logger = logging.getLogger(__name__)

# AUD-5401: node types that need a padding check but whose class could not
# be resolved through NODE_REGISTRY / the plugin manager.  Warned once per
# type to keep the log usable while still surfacing silent coverage gaps.
_unresolved_padding_types: set = set()


# ── Padding Check Logic (capability-based) ──
# Determines which node types need padding check.
# F2-04: Migrated from hand-written _PADDING_NODE_TYPES to capability system.
# Effect nodes always need check (dynamic padding from _tile_padding() method).
# Source/output/passthrough nodes don't need check.

# Legacy fallback set for testing/compatibility (removed in Phase 2)
_LEGACY_PADDING_NODE_TYPES = frozenset({
    "blur", "grade", "transform", "invert", "merge",
    "premult", "unpremult", "shuffle", "copy_channel",
    "cs_transform", "reformat", "crop", "clamp", "dissolve",
    "set_alpha", "remove_channels", "luma_keyer", "sharpen",
    "erode_dilate", "chroma_keyer", "defocus", "glow",
    "cornerpin", "lut", "huecorrect", "output",
})


def _needs_tile_padding_check(node_type: str) -> bool:
    """Check if a node type needs tile padding computation.

    Uses capability system to determine if node needs padding check:
    - Effect nodes: Always need check (may have dynamic padding)
    - Source/output/passthrough: No check needed

    Args:
        node_type: Node type string (e.g., "blur", "grade").

    Returns:
        True if this node type needs padding check.
    """
    cap = get_node_capability(node_type)
    if cap is None:
        # Unknown node type - use legacy fallback
        return node_type in _LEGACY_PADDING_NODE_TYPES

    # Effect nodes always need padding check (dynamic from _tile_padding() method)
    return cap.action_type == "effect"


@dataclass(frozen=True)
class PaddingInfo:
    """Detailed padding information for a graph.

    Attributes:
        total_padding: Maximum padding needed at source level (pixels each side).
        per_node_padding: Per-node accumulated padding (node_id -> padding).
        max_node_padding: Single node with highest padding requirement.
        padding_chain: The chain of nodes contributing to max padding (for diagnostics).
        recommended_tile_size: Tile size based on memory budget and padding.
        estimated_memory_mb: Estimated GPU memory for a single tile.
    """
    total_padding: int
    per_node_padding: Dict[str, int] = field(default_factory=dict)
    max_node_padding: Tuple[str, int] = ("", 0)  # (node_id, padding)
    padding_chain: List[str] = field(default_factory=list)  # Nodes contributing to max
    recommended_tile_size: int = 1024
    estimated_memory_mb: float = 0.0


@dataclass(frozen=True)
class TileSizeRecommendation:
    """Recommended tile size based on GPU memory constraints.

    Attributes:
        tile_size: Recommended tile dimension (pixels).
        padding: Required padding for spatial effects.
        effective_tile_size: tile_size + 2 * padding (actual tile render size).
        estimated_memory_mb: Memory needed for one tile at this size.
        memory_budget_mb: Available GPU memory budget.
        fits_in_budget: Whether the tile fits within memory budget.
        fallback_tile_sizes: Smaller fallback sizes if current doesn't fit.
    """
    tile_size: int
    padding: int
    effective_tile_size: int
    estimated_memory_mb: float
    memory_budget_mb: float
    fits_in_budget: bool
    fallback_tile_sizes: Tuple[int, ...] = (1024, 512, 256)


class TilePaddingComputer:
    """Computes padding requirements and tile size for tiled rendering.

    Walks the graph backward from the output node, accumulating the padding
    requirements of each spatial node that declares via _tile_padding().

    Algorithm:
    1. Build reverse adjacency: node_id -> set of downstream node_ids.
    2. Walk in reverse topological order (output -> sources).
    3. For each node:
       - node_padding = node._tile_padding(params) or requires_roi_expansion(params)
       - max_downstream = max padding from all downstream consumers
       - accumulated[node] = node_padding + max_downstream
    4. Track the padding chain for diagnostics.
    5. total_padding = max(accumulated) across source nodes.

    Example chain: Source -> Blur(r=10) -> Grade -> Blur(r=5) -> Output
    - Output: padding=0, accumulated=0
    - Blur(r=5): padding=5, accumulated=5 (no downstream)
    - Grade: padding=0, accumulated=5 (passes through)
    - Blur(r=10): padding=10, accumulated=15 (5 downstream + 10 own)
    - Source: accumulated=15 (15 downstream + 0 own)

    Result: total_padding=15px, padding_chain=[Blur(r=10), Blur(r=5)]
    """

    # AUD-5400: the former hand-written _SOURCE_TYPES frozenset drifted out
    # of sync with node_capabilities.py (video_frame/text/ramp/noise/... were
    # missing), so graphs rooted at those types got total_padding=0 and tiled
    # rendering produced seams.  There is no per-type list any more: Step 3
    # below derives the total from the accumulated-padding map itself, which
    # is data-driven and cannot omit a node type.

    # Default tile sizes to try (descending order)
    _DEFAULT_TILE_SIZES = (2048, 1024, 512, 256)

    # Default GPU memory budget (MB)
    _DEFAULT_MEMORY_BUDGET_MB = 1024

    @staticmethod
    def compute(snapshot: "GraphSnapshot",
                memory_budget_mb: Optional[float] = None,
                output_format: Optional["ImageFormat"] = None) -> PaddingInfo:
        """Compute padding and tile size for tiled rendering.

        Args:
            snapshot: Immutable graph snapshot.
            memory_budget_mb: Available GPU memory in MB. If None, uses default.
            output_format: Output image format for tile size estimation. If None,
                          uses snapshot's color dimensions.

        Returns:
            PaddingInfo with total padding, per-node breakdown, and tile size.
        """
        budget = memory_budget_mb or TilePaddingComputer._DEFAULT_MEMORY_BUDGET_MB

        # Step 1: Build reverse adjacency map
        downstream_map: Dict[str, List[str]] = {}
        for node_id in snapshot.eval_order:
            for conn in snapshot.get_input_connections(node_id):
                downstream_map.setdefault(conn.src_node_id, []).append(node_id)

        # Step 2: Walk in reverse topological order, accumulate padding
        accumulated: Dict[str, int] = {}
        node_padding: Dict[str, int] = {}
        padding_chain_tracker: Dict[str, List[str]] = {}  # Track chain for each node

        for node_id in reversed(snapshot.eval_order):
            ns = snapshot.nodes.get(node_id)
            if ns is None:
                continue

            # Get this node's own padding requirement
            own_padding = TilePaddingComputer._get_node_padding(ns)
            node_padding[node_id] = own_padding

            # Get max padding from downstream nodes and their chains
            downstream_nodes = downstream_map.get(node_id, [])
            max_downstream = 0
            downstream_chain: List[str] = []

            for ds_id in downstream_nodes:
                ds_padding = accumulated.get(ds_id, 0)
                if ds_padding > max_downstream:
                    max_downstream = ds_padding
                    downstream_chain = padding_chain_tracker.get(ds_id, [])

            # This node's accumulated padding = own + max downstream
            accumulated[node_id] = own_padding + max_downstream

            # Track the padding chain
            if own_padding > 0:
                # This node contributes to padding
                padding_chain_tracker[node_id] = [node_id] + downstream_chain
            else:
                # Pass-through node, inherit downstream chain
                padding_chain_tracker[node_id] = downstream_chain

        # Step 3: Find total padding at tile-entry (source) level.
        # accumulated[] is non-decreasing toward the graph roots
        # (accumulated[n] = own(n) + max(downstream) >= max(downstream)),
        # so its global maximum is always attained at a color-data root —
        # a source node OR a generator (ramp/noise are action_type="effect"
        # generators in the capability table).  Taking max over all nodes
        # therefore equals the source-level requirement while staying fully
        # data-driven: no node-type list that new sources/generators could
        # be silently left out of (AUD-5400).
        total = max(accumulated.values()) if accumulated else 0

        # Find the padding chain at a node attaining the maximum.  For a
        # root with no own padding the tracker inherits the downstream
        # chain, so this is the same chain the old source-based lookup
        # reported.
        max_chain: List[str] = []
        if total > 0:
            for nid, p in accumulated.items():
                if p == total:
                    max_chain = padding_chain_tracker.get(nid, [])
                    break

        # Step 4: Find the node with highest single padding
        max_single = ("", 0)
        for nid, p in node_padding.items():
            if p > max_single[1]:
                max_single = (nid, p)

        # Step 5: Compute recommended tile size
        output_w = output_format.width if output_format else snapshot.color_width or 1920
        output_h = output_format.height if output_format else snapshot.color_height or 1080
        node_count = len(snapshot.nodes)

        tile_rec = TilePaddingComputer._compute_tile_size(
            output_w, output_h, total, node_count, budget
        )

        return PaddingInfo(
            total_padding=total,
            per_node_padding=accumulated,
            max_node_padding=max_single,
            padding_chain=max_chain,
            recommended_tile_size=tile_rec.tile_size,
            estimated_memory_mb=tile_rec.estimated_memory_mb,
        )

    @staticmethod
    def _resolve_node_class(node_type: str) -> Optional[type]:
        """Resolve the node class for a type via the registries.

        AUD-5401: replaces the former hand-written if/elif import chain
        that silently returned 0 padding for every effect node it failed
        to enumerate (denoise / face_smooth / light_wrap / edge_detect /
        ... and anything added later).  The registry is the same source of
        truth the evaluator dispatches on, so a registered effect node can
        no longer be missing here.

        Args:
            node_type: Node type string (e.g., "blur", "denoise").

        Returns:
            The node class, or None if the type is unknown / deferred
            (registry placeholder) and no plugin registered it.
        """
        try:
            from .node_engine import NODE_REGISTRY
            cls = NODE_REGISTRY.get(node_type)
            if isinstance(cls, type):
                return cls

            # Deferred types (registry value None) and plugin types fall
            # through to the plugin manager (cheap lookup, no discovery).
            from .node_plugin import get_plugin_manager
            cls = get_plugin_manager().node_types.get(node_type)
            if isinstance(cls, type):
                return cls
        except Exception:
            logger.debug("Padding class resolution failed for %r",
                         node_type, exc_info=True)
        return None

    @staticmethod
    def _get_node_padding(ns: "NodeSnapshot") -> int:
        """Get the padding requirement for a node from its snapshot.

        Creates a temporary node instance (class resolved through
        NODE_REGISTRY / plugin manager) and calls _tile_padding() if
        available, falling back to requires_roi_expansion() for backward
        compatibility.  If no class can be resolved, or the instance-based
        computation raises, the capability system's declared static
        ``tile_padding`` is used so a declared requirement can never
        silently degrade to 0.

        Uses capability system (F2-04) to determine if node needs padding check.

        Args:
            ns: Node snapshot with type and parameters.

        Returns:
            Padding in pixels (non-negative).
        """
        node_type = ns.node_type

        # F2-04: Use capability-based check instead of hardcoded list
        if not _needs_tile_padding_check(node_type):
            return 0

        cls = TilePaddingComputer._resolve_node_class(node_type)
        if cls is None:
            if node_type not in _unresolved_padding_types:
                _unresolved_padding_types.add(node_type)
                logger.warning(
                    "TilePaddingComputer: no node class registered for "
                    "effect type %r; falling back to capability "
                    "tile_padding", node_type)
            # Data-driven fallback: the capability table's declared static
            # padding (0 when nothing was declared).
            declared = get_node_capability(node_type)
            return max(0, int(declared.tile_padding)) if declared else 0

        # Create temporary instance for padding computation
        try:
            node = cls.__new__(cls)
            node.__init__(node_id=ns.node_id)
            # Sync parameters
            for key, val in ns.parameters.items():
                node.parameters[key] = val

            # Try _tile_padding() first (new API)
            if hasattr(node, '_tile_padding'):
                padding = node._tile_padding(ns.parameters)
                return max(0, int(padding))

            # Fallback to requires_roi_expansion() (legacy API)
            if hasattr(node, 'requires_roi_expansion'):
                expansion = node.requires_roi_expansion(ns.parameters)
                return max(0, int(expansion))
        except Exception:
            # Instance-based computation failed: degrade to the capability
            # declaration instead of a silent 0 (0 would render seams).
            logger.warning(
                "TilePaddingComputer: dynamic padding computation failed "
                "for node type %r; falling back to capability tile_padding",
                node_type, exc_info=True)

        declared = get_node_capability(node_type)
        return max(0, int(declared.tile_padding)) if declared else 0

    @staticmethod
    def _compute_tile_size(
        output_w: int,
        output_h: int,
        padding: int,
        node_count: int,
        memory_budget_mb: float
    ) -> TileSizeRecommendation:
        """Compute recommended tile size based on memory constraints.

        Algorithm:
        1. Estimate memory for each candidate tile size
        2. Include padding in effective tile size
        3. Find the largest tile size that fits in budget
        4. Return recommendation with diagnostics

        Uses simple memory estimation formula:
        - Each RGBA float16 texture: 8 bytes per pixel
        - Assume ~3 textures per node (input, output, temp)
        - Total memory = effective_size^2 * 8 * 3 * node_count

        Args:
            output_w: Output width.
            output_h: Output height.
            padding: Required padding.
            node_count: Number of nodes in graph.
            memory_budget_mb: Available memory.

        Returns:
            TileSizeRecommendation with best tile size.
        """
        tile_sizes = TilePaddingComputer._DEFAULT_TILE_SIZES

        # Simple memory estimation (bytes per pixel for RGBA float16 = 8)
        BYTES_PER_PIXEL_RGBA16 = 8
        TEXTURES_PER_NODE_ESTIMATE = 3  # Conservative estimate

        for tile_size in tile_sizes:
            effective_size = tile_size + 2 * padding
            pixels = effective_size * effective_size

            # Estimate memory for one tile
            estimated_bytes = pixels * BYTES_PER_PIXEL_RGBA16 * TEXTURES_PER_NODE_ESTIMATE * node_count
            estimated_mb = estimated_bytes / (1024 * 1024)

            fits = estimated_mb <= memory_budget_mb

            if fits:
                return TileSizeRecommendation(
                    tile_size=tile_size,
                    padding=padding,
                    effective_tile_size=effective_size,
                    estimated_memory_mb=estimated_mb,
                    memory_budget_mb=memory_budget_mb,
                    fits_in_budget=True,
                    fallback_tile_sizes=tile_sizes,
                )

        # No tile size fits - use smallest with warning
        smallest = tile_sizes[-1]
        effective_size = smallest + 2 * padding
        pixels = effective_size * effective_size
        estimated_bytes = pixels * BYTES_PER_PIXEL_RGBA16 * TEXTURES_PER_NODE_ESTIMATE * node_count
        estimated_mb = estimated_bytes / (1024 * 1024)

        return TileSizeRecommendation(
            tile_size=smallest,
            padding=padding,
            effective_tile_size=effective_size,
            estimated_memory_mb=estimated_mb,
            memory_budget_mb=memory_budget_mb,
            fits_in_budget=False,
            fallback_tile_sizes=tile_sizes,
        )

    @staticmethod
    def get_effective_tile_size(tile_size: int, padding: int) -> int:
        """Compute the effective tile size including padding.

        The GPU actually renders tiles of size (tile_size + 2*padding).
        This method helps track actual memory usage.

        Args:
            tile_size: Base tile size.
            padding: Padding on each side.

        Returns:
            Effective tile dimension (tile_size + 2*padding).
        """
        return tile_size + 2 * padding

    @staticmethod
    def estimate_tile_count(output_w: int, output_h: int,
                            tile_size: int) -> Tuple[int, int]:
        """Estimate the number of tiles needed for the output.

        Args:
            output_w: Output width.
            output_h: Output height.
            tile_size: Base tile size (without padding).

        Returns:
            (tiles_x, tiles_y) - number of tiles in each dimension.
        """
        tiles_x = max(1, (output_w + tile_size - 1) // tile_size)
        tiles_y = max(1, (output_h + tile_size - 1) // tile_size)
        return (tiles_x, tiles_y)
