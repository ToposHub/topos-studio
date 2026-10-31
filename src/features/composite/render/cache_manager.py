"""
Cache Manager - Content-addressable evaluation cache with memory-aware eviction.

Extracts cache logic from GraphEvaluatorMixin into a dedicated class:

- Expanded cache key: includes render_mode, output_format, alpha_mode, proxy_scale, frame_number.
- ROI-aware: cache key includes requested ROI to prevent incorrect reuse.
- Cache category classification: preview/export/source/effect/ai with different strategies.
- OrderedDict for proper LRU eviction (access-ordered).
- Memory-aware eviction with configurable budget.
- Cumulative statistics (hits, misses, evictions, memory).

Usage:
    cache_mgr = CacheManager(resource_pool, max_entries=128, max_memory_mb=512)
    key = cache_mgr.compute_key(ns, inputs, render_mode="preview", roi=my_roi,
                                 output_format=my_fmt, alpha_mode="premult")
    entry = cache_mgr.get(key)
    if entry is None:
        entry = CacheEntry(...)
        cache_mgr.put(key, entry)
"""

import json
import hashlib
from collections import OrderedDict
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, Optional, Set, TYPE_CHECKING

from .gpu_resource_pool import GPUResourcePool
from ..core.graph_snapshot import NodeSnapshot
from ..nodes.base_node import TextureResult

if TYPE_CHECKING:
    from ..core.format import BBox, ImageFormat


# ── Cache Category Classification ──

class CacheCategory(Enum):
    """Cache entry category determines eviction strategy.

    Categories:
    - PREVIEW: Interactive preview cache, small and fast, aggressive LRU eviction.
    - EXPORT: Export cache, can be larger, cleared after task completion.
    - SOURCE: Source node cache (color_input, constant, image_input), persistent,
              not subject to automatic eviction (owner-managed).
    - EFFECT: Effect node cache (blur, grade, transform, etc.), LRU eviction.
    - AI: AI node cache (ai_generate, ai_inpaint, etc.), can be persisted to disk.
    - PREFETCH: Prefetched cache for predicted parameter changes, short-lived,
              promoted to EFFECT on hit, aggressive eviction if unused.
    """
    PREVIEW = "preview"
    EXPORT = "export"
    SOURCE = "source"
    EFFECT = "effect"
    AI = "ai"
    PREFETCH = "prefetch"


# ── Enhanced Cache Entry ──

@dataclass
class CacheEntry:
    """An eval cache entry with explicit texture ownership tracking and metadata.

    Attributes:
        outputs: Map of port name -> TextureResult.
        owned_texture_ids: Set of texture IDs (glo) that this entry owns
            and should release on eviction. Only includes temp textures
            allocated from fb_manager — NOT source node textures whose
            lifetime is managed by the node itself.
        memory_cost: Estimated GPU memory in bytes (sum of all output textures).
        last_used_frame: Frame number when this entry was last accessed (for video).
        render_mode: The render mode used to create this entry ("preview", "export").
        roi: The ROI requested when this entry was created.
        category: Cache category determining eviction strategy.
        dependency_revision: Hash of upstream texture revisions for invalidation.
    """
    outputs: Dict[str, TextureResult] = field(default_factory=dict)
    owned_texture_ids: Set[int] = field(default_factory=set)
    memory_cost: int = 0
    last_used_frame: int = 0
    render_mode: str = "preview"
    roi: Optional["BBox"] = None
    category: CacheCategory = CacheCategory.EFFECT
    dependency_revision: str = ""

    def compute_memory_cost(self) -> int:
        """Compute total GPU memory used by output textures."""
        total = 0
        for tex in self.outputs.values():
            if isinstance(tex, TextureResult) and tex.is_valid:
                total += tex.memory_bytes()
        return total


@dataclass
class CacheStats:
    """Cumulative cache statistics."""
    hits: int = 0
    misses: int = 0
    evictions: int = 0
    memory_released_bytes: int = 0


class CacheManager:
    """Content-addressable evaluation cache with memory-aware eviction.

    Features:
    - Content-addressable keys with full metadata (render_mode, ROI, format, alpha, etc.)
    - Cache category classification (preview/export/source/effect/ai)
    - Dual eviction: count-based (max_entries) and memory-based (max_memory_mb)
    - Category-aware eviction: SOURCE entries are protected, AI entries may be persisted
    - Frame pinning to prevent premature eviction during evaluation
    - Per-category memory tracking

    Thread safety: Used only from the render thread, no locking needed.
    """

    # Source node types that manage their own texture lifetimes.
    # These get SOURCE cache category (protected from eviction) because their
    # self-managed _gpu_texture glo is reused across frames when content is
    # unchanged — evicting their cache entry mid-drag would orphan the glo
    # downstream nodes still reference. Text joins this set now that it
    # self-manages its rasterized texture (like constant/image_input) and
    # applies transform on the GPU via _effect_type='transform'. Must stay in
    # sync with render_scheduler._GPU_STATEFUL_TYPES.
    _SOURCE_NODE_TYPES = frozenset({
        "color_input", "constant", "image_input", "motion_graphic", "text",
    })
    # AI node types that may be persisted to disk
    _AI_NODE_TYPES = frozenset({
        "ai_generate", "ai_inpaint", "ai_expand", "ai_remove", "denoise"
    })

    # ── Interaction Mode (drag/scroll optimization) ──
    # When the user drags an on-screen handle (e.g. Transform), the dragged
    # node's parameters change every frame, producing a new CacheEntry each
    # frame. At the normal 128-entry / 512MB budget these entries rapidly
    # trigger eviction → release textures with a GL fence → next frame's
    # acquire skips fence-unready textures → real glTexImage2D allocation
    # jitter. Temporarily raising the budget during the drag lets those
    # entries stay cached so the underlying texture pool keeps hitting,
    # eliminating the allocation jitter and keeping the drag at 60fps.
    _INTERACTION_MAX_ENTRIES = 512
    _INTERACTION_MAX_MEMORY_MB = 2048.0

    def __init__(self, resource_pool: GPUResourcePool,
                 max_entries: int = 128,
                 max_memory_mb: float = 512.0) -> None:
        self.resource_pool = resource_pool
        self.max_entries = max_entries
        self.max_memory_mb = max_memory_mb
        self._cache: OrderedDict[str, CacheEntry] = OrderedDict()
        self._stats = CacheStats()
        # P1-2 fix: Pin set for current frame's entries (prevents premature eviction)
        self._pinned_keys: Set[str] = set()
        # Current frame number for video/animation support
        self._current_frame: int = 0
        # Per-category memory tracking
        self._category_memory: Dict[CacheCategory, int] = {
            cat: 0 for cat in CacheCategory
        }
        # Interaction mode: temporarily raised limits during handle drag.
        # Idempotent begin/end; only the first begin stores the baseline.
        self._interaction_active: bool = False
        self._interaction_backup_max_entries: Optional[int] = None
        self._interaction_backup_max_memory: Optional[float] = None

    def set_frame_number(self, frame: int):
        """Set current frame number for video/animation cache tracking."""
        self._current_frame = frame

    # ── Cache Key ──

    def compute_key(self, ns: NodeSnapshot, inputs: Dict[str, TextureResult],
                    render_mode: str = "preview",
                    snapshot_color_id: int = 0,
                    snapshot_color_rev: int = 0,
                    roi: Optional["BBox"] = None,
                    output_format: Optional["ImageFormat"] = None,
                    alpha_mode: Optional[str] = None,
                    proxy_scale: float = 1.0,
                    frame_number: Optional[int] = None,
                    lod_level: int = 0,
                    scene_input_sig: str = "",
                    runtime_input_sig: str = "",
                    frame_remap_sig: str = "",
                    quality_profile: Optional[str] = None) -> str:
        """Compute a content-addressable cache key for a node evaluation.

        Key includes:
        - node_type, node_id
        - parameters hash
        - upstream texture metadata (id, revision, format, bbox, alpha,
          color_space, and auxiliary render-pass signature)
        - disabled flag
        - render_mode (preview vs export isolation)
        - color texture id/revision for color_input nodes
        - roi (region of interest requested, prevents cache misuse when zoomed)
        - output_format (requested output dimensions/format)
        - alpha_mode (requested alpha processing: "premult", "unpremult", "auto")
        - proxy_scale (preview proxy scale factor, e.g., 0.5 for half-res)
        - frame_number (video frame number for animation)
        - lod_level (Level of Detail for viewport-based optimization)
        - quality_profile (semantic draft/preview/final policy label)
        - scene_input_sig (上游 scene payload 签名；scene 消费节点用，
          确保 Model3D→Deformer3D→Scene3D 链路中上游参数变化触发 cache miss)

        Args:
            ns: Node snapshot with type, parameters, etc.
            inputs: Upstream texture results.
            render_mode: "preview", "full", or "export".
            snapshot_color_id: Color texture ID (for color_input nodes).
            snapshot_color_rev: Color texture revision.
            roi: Optional region of interest. When set, ensures cached result
                 matches the requested region, preventing incorrect reuse when
                 viewer is zoomed/panned.
            output_format: Optional requested output ImageFormat (dimensions, pixel_aspect).
            alpha_mode: Optional requested alpha processing mode.
            proxy_scale: Proxy scale factor for preview (1.0 = full-res, 0.5 = half-res).
            frame_number: Optional video frame number for animation caching.
            lod_level: LOD level for viewport-based optimization (0=full, 2=quarter).
            scene_input_sig: 上游 scene payload 签名字符串（scene 消费节点用，
                空串=非 scene 节点，不影响 key）。evaluator 对 Scene3D 收集其上游
                ModelPayload/CameraPayload 等的 signature() 拼成字符串传入，这样
                Deformer3D 参数变化 → payload signature 变 → Scene3D cache key 变
                → cache miss → 重渲染。修复 scene→scene 链路参数变化无反应的 bug。
            quality_profile: Semantic quality policy (``draft``, ``preview``,
                or ``final``). It is separate from proxy_scale so two policies
                with the same resolution cannot silently share a cache entry.
        """
        params_str = json.dumps(ns.parameters, sort_keys=True, default=str)
        params_hash = hashlib.md5(params_str.encode()).hexdigest()[:12]

        def _tex_meta(tex: TextureResult) -> tuple:
            fmt = tex.format
            fmt_str = ""
            if fmt:
                fmt_str = f"{fmt.width}x{fmt.height}:{fmt.pixel_aspect}:{fmt.origin_x},{fmt.origin_y}"
            bb = tex.bbox
            bb_str = ""
            if bb:
                bb_str = f"{bb.x},{bb.y},{bb.width},{bb.height}"
            return (
                tex.texture_id, tex.revision,
                fmt_str, bb_str,
                tex.alpha_premultiplied, tex.color_space,
                tex.aov_signature(),
            )

        upstream = tuple(sorted(
            (port_name,) + _tex_meta(tex)
            for port_name, tex in inputs.items()
        ))
        disabled_flag = "D" if ns.disabled else "E"

        # Color input nodes: include color texture state
        color_suffix = ""
        if ns.node_type == "color_input":
            color_suffix = f":ct{snapshot_color_id}:cr{snapshot_color_rev}"

        # ROI suffix: include requested region to prevent incorrect cache reuse
        roi_suffix = ""
        if roi is not None and not roi.is_empty:
            roi_suffix = f":roi{roi.x},{roi.y},{roi.width},{roi.height}"

        # Output format suffix: include requested output dimensions
        format_suffix = ""
        if output_format is not None:
            format_suffix = f":fmt{output_format.width}x{output_format.height}:{output_format.pixel_aspect}"

        # Alpha mode suffix: include requested alpha processing
        alpha_suffix = ""
        if alpha_mode is not None:
            alpha_suffix = f":am{alpha_mode}"

        # Proxy scale suffix: include preview scale factor
        proxy_suffix = ""
        if proxy_scale != 1.0:
            proxy_suffix = f":px{proxy_scale:.2f}"

        # Frame number suffix: include video frame for animation
        frame_suffix = ""
        if frame_number is not None:
            frame_suffix = f":fn{frame_number}"
            # P0-04: downstream time-node remap signature — the same playhead
            # frame maps to different source frames when a time node's
            # offset/speed/hold changes, so the remap must be part of the key.
            if frame_remap_sig:
                frame_suffix += f":{frame_remap_sig}"

        # LOD level suffix: include Level of Detail for viewport optimization
        lod_suffix = ""
        if lod_level > 0:
            lod_suffix = f":lod{lod_level}"

        # Scene input signature suffix: 上游 scene payload 签名（scene 消费节点用）。
        # Deformer3D/Model3D 参数变 → payload signature 变 → Scene3D key 变 → 重渲染。
        scene_suffix = ""
        if scene_input_sig:
            scene_suffix = f":ss{scene_input_sig}"

        runtime_suffix = ""
        if runtime_input_sig:
            runtime_suffix = f":ri{runtime_input_sig}"

        quality_suffix = ""
        if quality_profile:
            quality_suffix = f":qp{str(quality_profile).strip().lower()}"

        return (f"{ns.node_type}:{ns.node_id}:{params_hash}:{upstream}:"
                f"{disabled_flag}:rm={render_mode}{color_suffix}{roi_suffix}"
                f"{format_suffix}{alpha_suffix}{proxy_suffix}{frame_suffix}{lod_suffix}"
                f"{scene_suffix}{runtime_suffix}{quality_suffix}")

    def compute_key_from_string(self, key_string: str) -> str:
        """Create a cache key from an arbitrary string (e.g., for fused chains).

        Used by pass fusion to compute cache keys that incorporate multiple
        node types and parameters without going through the normal per-node path.
        """
        return hashlib.md5(key_string.encode()).hexdigest()[:32]

    # ── Access ──

    def get(self, key: str, promote_prefetch: bool = True) -> Optional[CacheEntry]:
        """Look up a cache entry. Returns None on miss.

        On hit, moves the entry to the end of the OrderedDict (LRU refresh)
        and updates last_used_frame.

        Args:
            key: Cache key to look up.
            promote_prefetch: If True, promote PREFETCH entries to EFFECT on hit.
                              This extends their lifetime when they prove useful.
        """
        entry = self._cache.get(key)
        if entry is not None:
            self._cache.move_to_end(key)  # LRU: recently accessed survives
            self._stats.hits += 1
            entry.last_used_frame = self._current_frame

            # Promote PREFETCH to EFFECT on hit (extends lifetime)
            if promote_prefetch and entry.category == CacheCategory.PREFETCH:
                self._category_memory[CacheCategory.PREFETCH] -= entry.memory_cost
                entry.category = CacheCategory.EFFECT
                self._category_memory[CacheCategory.EFFECT] += entry.memory_cost

            return entry
        self._stats.misses += 1
        return None

    def promote_prefetch_to_effect(self, key: str) -> bool:
        """Promote a PREFETCH entry to EFFECT category.

        Called when a prefetched entry is hit and should be kept longer.

        Args:
            key: Cache key of the entry to promote.

        Returns:
            True if promotion succeeded, False if entry not found or not PREFETCH.
        """
        entry = self._cache.get(key)
        if entry is None or entry.category != CacheCategory.PREFETCH:
            return False

        # Update category memory tracking
        self._category_memory[CacheCategory.PREFETCH] -= entry.memory_cost
        entry.category = CacheCategory.EFFECT
        self._category_memory[CacheCategory.EFFECT] += entry.memory_cost

        return True

    def put(self, key: str, entry: CacheEntry,
            render_mode: str = "preview",
            roi: Optional["BBox"] = None,
            category: Optional[CacheCategory] = None,
            frame_number: Optional[int] = None):
        """Insert a cache entry with full metadata.

        Args:
            key: Cache key from compute_key().
            entry: CacheEntry with outputs and owned_texture_ids.
            render_mode: The render mode used to create this entry.
            roi: The ROI requested when this entry was created.
            category: Cache category (auto-determined if None).
            frame_number: Frame number for video/animation.
        """
        # If key already exists, release old entry first
        if key in self._cache:
            old_entry = self._cache.pop(key)
            self._category_memory[old_entry.category] -= old_entry.memory_cost
            self._release_entry(old_entry)

        # Auto-determine category from key (node_type is first component)
        if category is None:
            node_type = key.split(":")[0]
            category = self._determine_category(node_type, render_mode)

        # Compute memory cost
        entry.memory_cost = entry.compute_memory_cost()
        entry.render_mode = render_mode
        entry.roi = roi
        entry.category = category
        entry.last_used_frame = frame_number if frame_number is not None else self._current_frame

        # Update category memory tracking
        self._category_memory[category] += entry.memory_cost

        self._cache[key] = entry
        self._evict()

    def _determine_category(self, node_type: str, render_mode: str) -> CacheCategory:
        """Determine cache category from node type and render mode."""
        if node_type in self._SOURCE_NODE_TYPES:
            return CacheCategory.SOURCE
        if node_type in self._AI_NODE_TYPES:
            return CacheCategory.AI
        if render_mode == "export":
            return CacheCategory.EXPORT
        if render_mode == "preview":
            return CacheCategory.PREVIEW
        return CacheCategory.EFFECT

    def invalidate_node(self, node_id: str):
        """Remove all cache entries for a specific node.

        Handles both properly-formatted keys ("node_type:node_id:...") and
        simple test keys (exact match).
        """
        keys_to_remove = []
        for k in self._cache:
            parts = k.split(":")
            if len(parts) >= 2:
                # Properly formatted key: check node_id component
                if parts[1] == node_id:
                    keys_to_remove.append(k)
            else:
                # Simple key: exact match (test case)
                if k == node_id:
                    keys_to_remove.append(k)
        for k in keys_to_remove:
            entry = self._cache.pop(k)
            self._category_memory[entry.category] -= entry.memory_cost
            self._release_entry(entry)

    def clear(self):
        """Clear the entire cache, releasing all owned GPU textures."""
        for key in list(self._cache.keys()):
            entry = self._cache.pop(key)
            self._category_memory[entry.category] -= entry.memory_cost
            self._release_entry(entry)

    def clear_export_cache(self):
        """Clear only EXPORT category entries after export task completion.

        This prevents export cache from polluting preview cache space.
        """
        keys_to_remove = [k for k, e in self._cache.items()
                          if e.category == CacheCategory.EXPORT]
        for k in keys_to_remove:
            entry = self._cache.pop(k)
            self._category_memory[CacheCategory.EXPORT] -= entry.memory_cost
            self._release_entry(entry)

    def clear_preview_cache(self):
        """Clear only PREVIEW category entries (e.g., before major parameter change)."""
        keys_to_remove = [k for k, e in self._cache.items()
                          if e.category == CacheCategory.PREVIEW]
        for k in keys_to_remove:
            entry = self._cache.pop(k)
            self._category_memory[CacheCategory.PREVIEW] -= entry.memory_cost
            self._release_entry(entry)

    def clear_prefetch_cache(self):
        """Clear only PREFETCH category entries.

        Called when predicted parameter changes didn't occur or
        prediction was incorrect.
        """
        keys_to_remove = [k for k, e in self._cache.items()
                          if e.category == CacheCategory.PREFETCH]
        for k in keys_to_remove:
            entry = self._cache.pop(k)
            self._category_memory[CacheCategory.PREFETCH] -= entry.memory_cost
            self._release_entry(entry)

    def retain_frame(self, min_frame: Optional[int] = None) -> int:
        """Frame-scoped sweep: evict entries not touched at/after ``min_frame``.

        Replaces the per-frame full clear on continuous-playback preview
        paths (TL-C03). Entries stamped with an older frame can never hit
        again — frame-dependent keys embed the frame number (P0-2) and a
        superseded parameter version gets a fresh key on its next
        evaluation — so keeping them only burns VRAM. Entries touched this
        frame survive: static branches (grade/LUT/mask/text) whose keys
        deliberately omit the frame number stay warm and stop re-rendering
        every frame.

        SOURCE-category entries are owner-managed and never swept here;
        pinned entries are respected. Entries stamped with the CURRENT
        frame (same frame number re-rendered, e.g. paused scrubbing) are
        retained and remain bounded by the put-time LRU budgets.

        Returns the number of entries evicted.
        """
        threshold = self._current_frame if min_frame is None else min_frame
        keys_to_remove = [
            k for k, e in self._cache.items()
            if e.category != CacheCategory.SOURCE
            and k not in self._pinned_keys
            and e.last_used_frame < threshold
        ]
        for k in keys_to_remove:
            entry = self._cache.pop(k)
            self._category_memory[entry.category] -= entry.memory_cost
            self._release_entry(entry)
        if keys_to_remove:
            self._stats.evictions += len(keys_to_remove)
        return len(keys_to_remove)

    # ── Frame Pinning (P1-2 fix) ──

    def begin_frame(self):
        """Clear pinned set at the start of a frame evaluation.

        This prevents premature eviction of entries that might be consumed
        later in the same frame (e.g., complex branch graphs where a Merge
        node needs outputs from multiple upstream branches).
        """
        self._pinned_keys.clear()

    def pin(self, key: str):
        """Pin a cache entry for the current frame.

        Pinned entries won't be evicted during the current frame's evaluation,
        even if the cache exceeds max_entries.
        """
        self._pinned_keys.add(key)

    def end_frame(self):
        """Clear pinned set at the end of a frame evaluation.

        Optional: begin_frame() on the next frame will also clear this.
        """
        self._pinned_keys.clear()

    # ── Interaction Mode (drag/scroll optimization) ──

    def begin_interaction(self) -> None:
        """Enter interaction (handle-drag) mode: temporarily raise cache limits.

        While a handle is being dragged, the dragged node's parameters change
        every frame and produce a fresh CacheEntry each frame. At the normal
        budget this causes constant eviction + texture release + real GL
        allocation jitter (see class docstring). Raising the limits for the
        drag duration lets those per-frame entries stay cached, so the
        underlying GPU texture pool keeps hitting and the drag stays smooth.

        Idempotent: only the first call stores the baseline limits; repeated
        calls while already active are a no-op so per-frame hint-driven
        toggling is safe.
        """
        if self._interaction_active:
            return
        self._interaction_active = True
        self._interaction_backup_max_entries = self.max_entries
        self._interaction_backup_max_memory = self.max_memory_mb
        self.max_entries = self._INTERACTION_MAX_ENTRIES
        self.max_memory_mb = self._INTERACTION_MAX_MEMORY_MB

    def end_interaction(self) -> None:
        """Exit interaction mode: restore baseline limits and evict residue.

        Restores the original entry/memory budget and immediately evicts down
        to it. PREVIEW entries (the category drag frames land in) have the
        highest eviction priority, so the per-frame drag residue is reclaimed
        right away — freeing the VRAM bump the drag temporarily took. Called
        on mouse release (and defensively on any non-interaction frame).

        Idempotent: no-op if not currently in interaction mode.
        """
        if not self._interaction_active:
            return
        self._interaction_active = False
        if self._interaction_backup_max_entries is not None:
            self.max_entries = self._interaction_backup_max_entries
        if self._interaction_backup_max_memory is not None:
            self.max_memory_mb = self._interaction_backup_max_memory
        self._interaction_backup_max_entries = None
        self._interaction_backup_max_memory = None
        # Immediately reclaim drag residue now that the budget is back to normal.
        self._evict()

    @property
    def interaction_active(self) -> bool:
        """Whether interaction-mode (raised cache limits) is currently active."""
        return self._interaction_active

    # ── Eviction ──

    def _evict(self):
        """Evict entries if over max_entries or max_memory_mb.

        Strategy (category-aware):
        1. Remove entries for nodes no longer in the graph (stale, excluding pinned/SOURCE).
        2. Check memory budget; if over, evict by priority:
           - Priority 1: PREFETCH (short-lived, aggressive eviction if unused)
           - Priority 2: PREVIEW (aggressive, viewer cache)
           - Priority 3: EXPORT (after export task completion)
           - Priority 4: EFFECT (LRU, normal effects)
           - Priority 5: AI (may be persisted, lower priority for eviction)
           - Protected: SOURCE (owner-managed, not evicted)
        3. Check entry count; if over, evict by same priority.

        P1-2 fix: Skip pinned entries to prevent premature eviction during
        the current frame's evaluation.
        """
        evicted = 0

        # Step 1: Remove stale node entries (excluding pinned and SOURCE)
        stale_keys = [k for k in self._cache
                      if not self._is_for_current_graph(k)
                      and k not in self._pinned_keys
                      and self._cache[k].category != CacheCategory.SOURCE]
        for k in stale_keys:
            entry = self._cache.pop(k)
            self._category_memory[entry.category] -= entry.memory_cost
            self._release_entry(entry)
            evicted += 1

        # Step 2: Memory budget eviction (category-aware priority)
        current_memory_mb = self.estimated_memory_mb()
        if current_memory_mb > self.max_memory_mb:
            evicted += self._evict_to_memory_budget()

        # Step 3: Entry count eviction (category-aware priority)
        if len(self._cache) > self.max_entries:
            evicted += self._evict_to_entry_count()

        if evicted > 0:
            self._stats.evictions += evicted

    # Eviction priority: PREFETCH > PREVIEW > EXPORT > AI > EFFECT (SOURCE protected)
    _EVICTION_ORDER = (
        CacheCategory.PREFETCH,
        CacheCategory.PREVIEW,
        CacheCategory.EXPORT,
        CacheCategory.AI,
        CacheCategory.EFFECT,
    )

    def _eviction_candidates(self) -> list:
        """Snapshot of evictable (key, entry) pairs in eviction order.

        AUD-7920: the previous implementation rescanned the whole cache
        after every single eviction and recomputed the O(n) memory total
        each round — O(n²) under pressure. Building the ordered candidate
        list once (priority category first, insertion/age order within a
        category) preserves the exact eviction sequence in a single O(n)
        pass.
        """
        candidates: list = []
        for category in self._EVICTION_ORDER:
            for key, entry in self._cache.items():
                if entry.category != category:
                    continue
                if key in self._pinned_keys:
                    continue
                candidates.append((key, entry))
        return candidates

    def _evict_to_memory_budget(self) -> int:
        """Evict entries to meet memory budget. Returns number evicted."""
        target_mb = self.max_memory_mb * 0.8  # Evict to 80% of budget
        evicted = 0

        # AUD-7920: compute the O(n) total once and subtract per eviction
        # instead of calling estimated_memory_mb() (another O(n) scan)
        # after every eviction.
        total_bytes = sum(entry.memory_cost for entry in self._cache.values())
        target_bytes = target_mb * (1024 * 1024)

        if total_bytes <= target_bytes:
            return 0

        for key, entry in self._eviction_candidates():
            if total_bytes <= target_bytes:
                break
            # Candidate was evicted by a side effect of a previous
            # _release_entry — skip entries no longer present.
            if self._cache.get(key) is not entry:
                continue
            self._cache.pop(key)
            self._category_memory[entry.category] -= entry.memory_cost
            self._release_entry(entry)
            total_bytes -= entry.memory_cost
            evicted += 1

        return evicted

    def _evict_to_entry_count(self) -> int:
        """Evict entries to meet entry count limit. Returns number evicted."""
        target_count = int(self.max_entries * 0.8)  # Evict to 80% of limit
        evicted = 0

        if len(self._cache) <= target_count:
            return 0

        # AUD-7920: single ordered pass instead of a full rescan per eviction.
        for key, entry in self._eviction_candidates():
            if len(self._cache) <= target_count:
                break
            if self._cache.get(key) is not entry:
                continue
            self._cache.pop(key)
            self._category_memory[entry.category] -= entry.memory_cost
            self._release_entry(entry)
            evicted += 1

        return evicted

    def _is_for_current_graph(self, key: str) -> bool:
        """Check if a cache key's node is still in the graph.

        Uses a snapshot reference if available, otherwise conservatively returns True.
        Handles both properly-formatted keys ("node_type:node_id:...") and
        simple test keys ("key1").
        """
        # The evaluator sets _snapshot_nodes before calling evict
        parts = key.split(":")
        if len(parts) < 2:
            # Simple key format (test case): conservatively return True
            return True
        node_id = parts[1]
        current_nodes = getattr(self, '_current_node_ids', None)
        if current_nodes is not None:
            return node_id in current_nodes
        return True

    def set_current_nodes(self, node_ids: Set[str]):
        """Set the current graph's node IDs for stale-entry detection during eviction."""
        self._current_node_ids = node_ids

    def _release_entry(self, entry: CacheEntry):
        """Release GPU textures owned by a cache entry.

        Uses GPUResourcePool's release_texture_by_id() which handles both
        tracked textures (acquired via pool) and legacy textures (acquired
        directly from fb_manager).
        """
        if not isinstance(entry, CacheEntry) or not entry.owned_texture_ids:
            return
        pool = self.resource_pool

        # ``owned_texture_ids`` is the authoritative ownership contract.  It
        # may contain more than the beauty texture: deep-compositing entries
        # also own ``depth_texture_id``, and source-action entries carry the
        # evaluator-allocated IDT/tile-crop textures.  (Scene3D deliberately
        # stays OUT of CACHE_OWNED_TYPES and manages its own AOV textures —
        # releasing them here would double-free; its entries carry an empty
        # set.)  Iterating only ``TextureResult.texture_id`` used to leave
        # those auxiliary resources resident after eviction, even though
        # they were included in the cache's memory budget.
        #
        # Release the set directly rather than walking output metadata.  It
        # keeps the release side symmetric with
        # BaseEvaluatorMixin._collect_owned_texture_ids() and also avoids
        # double-releasing an auxiliary texture shared by two output ports.
        for tex_id in set(entry.owned_texture_ids):
            try:
                tex_id = int(tex_id)
            except (TypeError, ValueError):
                continue
            if tex_id <= 0:
                continue
            # Use pool's unified release mechanism
            pool.release_texture_by_id(tex_id)

        self._stats.memory_released_bytes += entry.memory_cost

    # ── Stats ──

    @property
    def stats(self) -> CacheStats:
        return self._stats

    def entry_count(self) -> int:
        return len(self._cache)

    def estimated_memory_mb(self) -> float:
        """Estimated GPU memory held by cache entries (MB)."""
        total_bytes = 0
        for entry in self._cache.values():
            total_bytes += entry.memory_cost
        return total_bytes / (1024 * 1024)

    def category_memory_mb(self, category: CacheCategory) -> float:
        """Estimated GPU memory for a specific category (MB)."""
        return self._category_memory[category] / (1024 * 1024)

    def category_entry_count(self, category: CacheCategory) -> int:
        """Count entries in a specific category."""
        return sum(1 for e in self._cache.values() if e.category == category)

    def dump_stats(self) -> str:
        """Formatted cache statistics with per-category breakdown."""
        s = self._stats
        total = s.hits + s.misses
        rate = (s.hits / total * 100) if total > 0 else 0

        # Per-category breakdown
        cat_stats = []
        for cat in CacheCategory:
            count = self.category_entry_count(cat)
            mem_mb = self.category_memory_mb(cat)
            if count > 0:
                cat_stats.append(f"{cat.value}={count}/{mem_mb:.1f}MB")

        cat_str = " ".join(cat_stats) if cat_stats else "empty"
        return (
            f"[CacheManager] entries={len(self._cache)} "
            f"hits={s.hits} misses={s.misses} evictions={s.evictions} "
            f"rate={rate:.0f}% mem={self.estimated_memory_mb():.1f}MB "
            f"[{cat_str}]"
        )

    def dump_full_report(self) -> str:
        """Detailed cache report for debugging."""
        lines = [self.dump_stats()]
        lines.append(f"max_entries={self.max_entries} max_memory_mb={self.max_memory_mb}")
        lines.append(f"pinned_keys={len(self._pinned_keys)} current_frame={self._current_frame}")

        # Per-category stats
        lines.append("\nPer-category breakdown:")
        for cat in CacheCategory:
            count = self.category_entry_count(cat)
            mem_mb = self.category_memory_mb(cat)
            lines.append(f"  {cat.value}: {count} entries, {mem_mb:.2f} MB")

        # Sample entries (first 5)
        lines.append("\nSample entries (first 5):")
        for key in list(self._cache.keys())[:5]:
            entry = self._cache[key]
            lines.append(f"  {key[:50]}... cat={entry.category.value} "
                         f"mem={entry.memory_cost//1024}KB frame={entry.last_used_frame}")

        return "\n".join(lines)
