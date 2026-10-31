"""
Serialization Formats for Composite Graph Data.

Supports multiple serialization formats:
- JSON: Human-readable, backward compatible, default format
- MessagePack: Binary format, 50-70% size reduction for large projects
- gzip compression: Optional compression layer for both formats

Format detection:
- JSON files start with '{' (ASCII 0x7B)
- MessagePack files start with 0x82-0x8F (fixmap) or 0xDE-0xDF (map16/32)
- gzip files start with magic bytes 0x1F 0x8B

Features:
- format_version field distinguishes serialization format (json/msgpack)
- Auto-detection on load for seamless format migration
- Incremental save: stores only deltas since last snapshot
"""

import json
import gzip
import copy
import logging
import threading
from typing import Any, Dict, Optional, Tuple
from enum import Enum

logger = logging.getLogger(__name__)

# Try to import msgpack, fall back gracefully
try:
    import msgpack
    MSGPACK_AVAILABLE = True
except ImportError:
    MSGPACK_AVAILABLE = False
    logger.warning("msgpack not available, MessagePack format disabled")


class SerializationFormat(Enum):
    """Supported serialization formats."""
    JSON = "json"
    MSGPACK = "msgpack"


class CompressionFormat(Enum):
    """Supported compression formats."""
    NONE = "none"
    GZIP = "gzip"


# Magic bytes for format detection
MSGPACK_MAP_PREFIX_BYTES = (
    0x80, 0x81, 0x82, 0x83, 0x84, 0x85, 0x86, 0x87,
    0x88, 0x89, 0x8A, 0x8B, 0x8C, 0x8D, 0x8E, 0x8F,
    0xDE, 0xDF,  # map16, map32
)
GZIP_MAGIC = b'\x1f\x8b'
JSON_MAGIC = b'{'


class FormatVersion:
    """Format version constants for serialization.
    
    format_version: Serialization format version (json/msgpack)
    schema_version: Data schema version (managed by graph_migration.py)
    """
    FORMAT_JSON = 1
    FORMAT_MSGPACK = 2
    FORMAT_MSGPACK_GZIP = 3
    FORMAT_JSON_GZIP = 4
    
    CURRENT_FORMAT_VERSION = FORMAT_JSON  # Default is JSON for backward compat


def detect_format(data: bytes) -> Tuple[SerializationFormat, CompressionFormat]:
    """Detect serialization format from raw bytes.
    
    Args:
        data: Raw file content bytes
        
    Returns:
        Tuple of (SerializationFormat, CompressionFormat)
    """
    if not data:
        return SerializationFormat.JSON, CompressionFormat.NONE
    
    # Check for gzip compression first
    if data[:2] == GZIP_MAGIC:
        # Decompress to check inner format
        try:
            inner_data = gzip.decompress(data)
            if inner_data[:1] == JSON_MAGIC:
                return SerializationFormat.JSON, CompressionFormat.GZIP
            elif inner_data[0] in MSGPACK_MAP_PREFIX_BYTES:
                return SerializationFormat.MSGPACK, CompressionFormat.GZIP
            else:
                # Unknown inner format, assume JSON
                return SerializationFormat.JSON, CompressionFormat.GZIP
        except Exception:
            # Failed to decompress, assume gzip JSON
            return SerializationFormat.JSON, CompressionFormat.GZIP
    
    # Check for MessagePack (map structure)
    if data[0] in MSGPACK_MAP_PREFIX_BYTES:
        return SerializationFormat.MSGPACK, CompressionFormat.NONE
    
    # Check for JSON
    if data[:1] == JSON_MAGIC:
        return SerializationFormat.JSON, CompressionFormat.NONE
    
    # Default to JSON
    return SerializationFormat.JSON, CompressionFormat.NONE


def serialize(
    data: Dict[str, Any],
    format: SerializationFormat = SerializationFormat.JSON,
    compression: CompressionFormat = CompressionFormat.NONE,
    use_bin_type: bool = True
) -> bytes:
    """Serialize data to specified format.
    
    Args:
        data: Dictionary to serialize
        format: Serialization format (JSON or MessagePack)
        compression: Compression format (NONE or GZIP)
        use_bin_type: For MessagePack, use bin type for bytes
        
    Returns:
        Serialized bytes
    """
    # AUD-11601: never mutate the caller's dict. Copying before injecting
    # the ``_format_version`` marker keeps undo snapshots and cached dicts
    # pristine (the delta envelope, not the graph data, owns the marker).
    data = dict(data)

    # Add format_version marker for MessagePack
    if format == SerializationFormat.MSGPACK:
        data["_format_version"] = FormatVersion.FORMAT_MSGPACK
    else:
        data["_format_version"] = FormatVersion.FORMAT_JSON
    
    if format == SerializationFormat.MSGPACK:
        if not MSGPACK_AVAILABLE:
            logger.warning("MessagePack requested but not available, falling back to JSON")
            format = SerializationFormat.JSON
        else:
            # MessagePack serialization
            raw = msgpack.packb(data, use_bin_type=use_bin_type)
    
    # JSON serialization (default or fallback)
    if format == SerializationFormat.JSON:
        raw = json.dumps(data, ensure_ascii=False, indent=2).encode('utf-8')
    
    # Apply compression
    if compression == CompressionFormat.GZIP:
        raw = gzip.compress(raw, compresslevel=6)
    
    return raw


def deserialize(
    data: bytes,
    format: Optional[SerializationFormat] = None,
    compression: Optional[CompressionFormat] = None
) -> Dict[str, Any]:
    """Deserialize data from bytes.
    
    Auto-detects format if not specified.
    
    Args:
        data: Raw bytes to deserialize
        format: Optional explicit format (auto-detected if None)
        compression: Optional explicit compression (auto-detected if None)
        
    Returns:
        Deserialized dictionary
    """
    if not data:
        return {}
    
    # Auto-detect format if not specified
    if format is None or compression is None:
        detected_format, detected_compression = detect_format(data)
        if format is None:
            format = detected_format
        if compression is None:
            compression = detected_compression
    
    # Decompress if needed
    if compression == CompressionFormat.GZIP:
        try:
            data = gzip.decompress(data)
        except Exception as e:
            logger.error(f"Failed to decompress gzip data: {e}")
            raise ValueError("Invalid gzip data")
    
    # Deserialize based on format
    if format == SerializationFormat.MSGPACK:
        if not MSGPACK_AVAILABLE:
            raise ValueError("MessagePack data but msgpack library not available")
        result = msgpack.unpackb(data, raw=False)
    else:
        # JSON
        result = json.loads(data.decode('utf-8'))

    # AUD-11602: the contract (and return annotation) is a top-level dict.
    # A list/str/number payload must fail with a clear format error instead
    # of surfacing as an AttributeError deep in downstream consumers.
    if not isinstance(result, dict):
        raise ValueError(
            f"Deserialized payload must be a JSON/MessagePack object "
            f"(dict), got {type(result).__name__}")

    return result


# ── Incremental Save Support ──

# Top-level keys with dedicated (id-diff / list-replace) delta logic in
# IncrementalSaver._compute_graph_delta.  Every other top-level key is
# compared generically with full-replacement semantics (AUD-11600) so new
# or unknown fields (fps, duration_frames, layers, layer_stacks,
# composite_schema_version, ...) survive an incremental round-trip instead
# of being silently dropped.
_STRUCTURAL_TOP_LEVEL_KEYS = frozenset({"nodes", "connections"})

# Delta keys produced and applied by dedicated logic; apply_delta treats
# every other key in the delta as a full top-level replacement.
_DEDICATED_DELTA_KEYS = frozenset({
    "nodes_added", "nodes_removed", "nodes_modified", "connections_changed",
})


def _validate_node_list(nodes: Any, source: str) -> Dict[str, Dict[str, Any]]:
    """Validate a graph payload's ``nodes`` field and index it by id.

    AUD-11604: ``nodes`` may come from an external persisted file. A dict
    instead of a list, a non-dict entry, or a missing/duplicate ``id`` must
    raise a ValueError with field context instead of a bare
    KeyError/TypeError (or a silent overwrite of duplicate ids).

    Returns:
        Mapping of node id -> node dict. An absent/None field yields {}.
    """
    if nodes is None:
        return {}
    if not isinstance(nodes, list):
        raise ValueError(
            f"Malformed graph data ({source}): 'nodes' must be a list, "
            f"got {type(nodes).__name__}")
    indexed: Dict[str, Dict[str, Any]] = {}
    for index, node in enumerate(nodes):
        if not isinstance(node, dict):
            raise ValueError(
                f"Malformed graph data ({source}): node at index {index} "
                f"must be a dict, got {type(node).__name__}")
        node_id = node.get("id")
        if not isinstance(node_id, str) or not node_id:
            raise ValueError(
                f"Malformed graph data ({source}): node at index {index} "
                f"has a missing or invalid 'id' ({node_id!r})")
        if node_id in indexed:
            raise ValueError(
                f"Malformed graph data ({source}): duplicate node id "
                f"{node_id!r} at index {index}")
        indexed[node_id] = node
    return indexed


class IncrementalSaver:
    """Incremental save: stores only changes since last snapshot.
    
    Features:
    - Maintains last saved snapshot in memory
    - Computes delta: added/removed/modified nodes, connections
    - Delta format: {base_version: N, delta: {...}}
    - Load: base snapshot + apply delta
    
    Usage:
        saver = IncrementalSaver()
        saver.set_base_snapshot(graph.to_dict())
        delta = saver.compute_delta(new_data)
        # Save delta instead of full snapshot
    """
    
    def __init__(self) -> None:
        # AUD-11611: 实例可能被跨线程共用（后台线程 save_incremental /
        # UI 线程 set_base_snapshot），``_base_snapshot`` 与
        # ``_base_generation`` 的读写必须受锁保护。RLock 允许
        # save_incremental 持锁期间重入 compute_delta/set_base_snapshot。
        self._lock = threading.RLock()
        self._base_snapshot: Optional[Dict[str, Any]] = None
        self._base_generation: int = 0

    def set_base_snapshot(self, snapshot: Dict[str, Any], generation: int = 0) -> None:
        """Set the reference snapshot for delta computation."""
        with self._lock:
            self._base_snapshot = copy.deepcopy(snapshot)
            self._base_generation = generation

    def clear_base_snapshot(self) -> None:
        """Clear the base snapshot (force full save)."""
        with self._lock:
            self._base_snapshot = None
            self._base_generation = 0

    @property
    def base_generation(self) -> int:
        """Generation of the current base snapshot (public read accessor).

        AUD-11603: ``save_incremental`` advances the base after each
        successful save; this accessor lets callers track the generation
        without touching private state.
        """
        with self._lock:
            return self._base_generation

    @property
    def has_base_snapshot(self) -> bool:
        """Whether a base snapshot is set (incremental saves possible)."""
        with self._lock:
            return self._base_snapshot is not None

    def compute_delta(self, new_data: Dict[str, Any]) -> Dict[str, Any]:
        """Compute delta between base snapshot and new data.

        Returns a delta dict with:
        - base_version: generation number of base snapshot
        - delta_type: "incremental" or "full"
        - delta: actual changes (if incremental)
        - full_data: complete data (if full)
        """
        with self._lock:
            if self._base_snapshot is None:
                # No base, return full data
                return {
                    "delta_type": "full",
                    "base_version": 0,
                    # AUD-11601: hold a deep copy, not a reference — the caller's
                    # dict must not leak into (or be mutated through) the envelope.
                    "full_data": copy.deepcopy(new_data),
                }

            delta = self._compute_graph_delta(self._base_snapshot, new_data)

            # AUD-11600: "no changes" must consider *every* top-level field, not
            # just the node/connection keys — otherwise an edit that only touched
            # fps/duration/layers/... would be reported as no_changes and the
            # modification silently dropped on save.
            if not delta:
                # No changes, return minimal delta
                return {
                    "delta_type": "incremental",
                    "base_version": self._base_generation,
                    "delta": {"no_changes": True},
                }

            return {
                "delta_type": "incremental",
                "base_version": self._base_generation,
                "delta": delta,
            }
    
    def _compute_graph_delta(
        self,
        base: Dict[str, Any],
        new: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Compute detailed delta between two graph snapshots."""
        delta = {}

        # Nodes delta (AUD-11604: validate external payloads before indexing)
        base_nodes = _validate_node_list(base.get("nodes"), "base snapshot")
        new_nodes = _validate_node_list(new.get("nodes"), "new data")
        
        base_ids = set(base_nodes.keys())
        new_ids = set(new_nodes.keys())
        
        # Added nodes
        added_ids = new_ids - base_ids
        if added_ids:
            delta["nodes_added"] = [new_nodes[id] for id in added_ids]
        
        # Removed nodes
        removed_ids = base_ids - new_ids
        if removed_ids:
            delta["nodes_removed"] = list(removed_ids)
        
        # Modified nodes (parameters, position, disabled)
        common_ids = base_ids & new_ids
        modified_nodes = []
        for nid in common_ids:
            base_node = base_nodes[nid]
            new_node = new_nodes[nid]
            
            node_delta = {}
            
            # Check parameters
            if base_node.get("parameters") != new_node.get("parameters"):
                node_delta["parameters"] = new_node.get("parameters", {})
            
            # Check position
            if base_node.get("position") != new_node.get("position"):
                node_delta["position"] = new_node.get("position")
            
            # Check disabled
            if base_node.get("disabled") != new_node.get("disabled"):
                node_delta["disabled"] = new_node.get("disabled", False)
            
            # Check expressions
            if base_node.get("expressions") != new_node.get("expressions"):
                node_delta["expressions"] = new_node.get("expressions", {})

            # B4: user_label — a canvas rename must survive incremental save
            if base_node.get("user_label") != new_node.get("user_label"):
                node_delta["user_label"] = new_node.get("user_label")

            # keyframe_tracks (animation edits) — same rationale
            if (base_node.get("keyframe_tracks")
                    != new_node.get("keyframe_tracks")):
                node_delta["keyframe_tracks"] = new_node.get(
                    "keyframe_tracks", {})

            if node_delta:
                node_delta["id"] = nid
                modified_nodes.append(node_delta)
        
        if modified_nodes:
            delta["nodes_modified"] = modified_nodes
        
        # Connections delta
        base_conns = base.get("connections", [])
        new_conns = new.get("connections", [])
        
        if base_conns != new_conns:
            delta["connections_changed"] = new_conns
        
        # AUD-11600: every other top-level key (backdrops, sticky_notes,
        # groups, viewport_state, viewer_state and any future/unknown field
        # such as fps, duration_frames, layers, layer_stacks,
        # composite_schema_version, ...) is compared generically with
        # full-replacement semantics so nothing is silently dropped from an
        # incremental round-trip.
        for key in sorted(set(base.keys()) | set(new.keys())):
            if key in _STRUCTURAL_TOP_LEVEL_KEYS:
                continue
            if base.get(key) != new.get(key):
                delta[key] = new.get(key)

        return delta
    
    def apply_delta(
        self,
        base: Dict[str, Any],
        delta_data: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Apply delta to base snapshot to reconstruct full data.
        
        Args:
            base: Base snapshot dict
            delta_data: Delta dict from compute_delta
            
        Returns:
            Reconstructed full data
        """
        if delta_data.get("delta_type") == "full":
            return delta_data.get("full_data", base)
        
        if delta_data.get("delta", {}).get("no_changes"):
            return copy.deepcopy(base)
        
        delta = delta_data.get("delta", {})
        result = copy.deepcopy(base)
        
        # Apply nodes added
        for node in delta.get("nodes_added", []):
            result.setdefault("nodes", []).append(node)
        
        # Apply nodes removed
        removed_ids = set(delta.get("nodes_removed", []))
        if removed_ids:
            result["nodes"] = [
                n for n in result.get("nodes", [])
                if n.get("id") not in removed_ids
            ]
        
        # Apply nodes modified
        for mod in delta.get("nodes_modified", []):
            nid = mod.get("id")
            for node in result.get("nodes", []):
                if node.get("id") == nid:
                    if "parameters" in mod:
                        node["parameters"] = mod["parameters"]
                    if "position" in mod:
                        node["position"] = mod["position"]
                    if "disabled" in mod:
                        node["disabled"] = mod["disabled"]
                    if "expressions" in mod:
                        node["expressions"] = mod["expressions"]
                    if "user_label" in mod:
                        node["user_label"] = mod["user_label"]
                    if "keyframe_tracks" in mod:
                        node["keyframe_tracks"] = mod["keyframe_tracks"]
                    break
        
        # Apply connections
        if "connections_changed" in delta:
            result["connections"] = delta["connections_changed"]
        
        # AUD-11600: apply any remaining top-level field (backdrops,
        # sticky_notes, groups, UI state and future/unknown fields) with
        # full-replacement semantics.
        for key in delta:
            if key in _DEDICATED_DELTA_KEYS:
                continue
            result[key] = delta[key]

        return result


def save_incremental(
    data: Dict[str, Any],
    saver: IncrementalSaver,
    format: SerializationFormat = SerializationFormat.JSON,
    compression: CompressionFormat = CompressionFormat.NONE
) -> bytes:
    """Save data using incremental delta if possible.
    
    Args:
        data: Full graph data
        saver: IncrementalSaver instance with base snapshot
        format: Serialization format
        compression: Compression format
        
    Returns:
        Serialized delta or full data
    """
    # AUD-11603: the module owns the base after a successful save. Advancing
    # it here (instead of relying on the caller to remember
    # ``set_base_snapshot``) keeps the next delta consistent with what was
    # actually persisted and prevents the stale-delta corruption path.
    # AUD-11611: compute→serialize→advance is one atomic step under the
    # saver's lock; a concurrent set_base_snapshot/clear must not interleave
    # mid-save. RLock makes the nested calls re-entrant.
    with saver._lock:
        delta_data = saver.compute_delta(data)
        raw = serialize(delta_data, format, compression)
        saver.set_base_snapshot(data, generation=saver.base_generation + 1)
    return raw


def load_incremental(
    data: bytes,
    base_snapshot: Optional[Dict[str, Any]] = None,
    saver: Optional[IncrementalSaver] = None
) -> Dict[str, Any]:
    """Load data, handling incremental delta format.

    Args:
        data: Serialized bytes (may be delta or full)
        base_snapshot: Optional base snapshot for delta reconstruction
        saver: Optional IncrementalSaver (used if base_snapshot is None)

    Returns:
        Reconstructed full graph data

    Raises:
        ValueError: If the payload is not an object, an incremental delta is
            missing its base, or the delta's ``base_version`` does not match
            the saver's base generation (stale delta — AUD-11603).
    """
    loaded = deserialize(data)

    # Check if it's a delta
    if "delta_type" not in loaded:
        # Full data
        return loaded

    if loaded["delta_type"] == "full":
        return loaded.get("full_data", {})

    # Incremental delta - need base snapshot
    used_saver_base = False
    if base_snapshot is None:
        # AUD-11611: read base state under the saver's lock so a concurrent
        # set_base_snapshot/clear cannot hand back a torn snapshot/generation
        # pair.
        if saver is None:
            raise ValueError("Incremental delta requires base snapshot or saver")
        with saver._lock:
            snapshot = saver._base_snapshot
        if snapshot is None:
            raise ValueError("Incremental delta requires base snapshot or saver")
        base_snapshot = snapshot
        used_saver_base = True

    # AUD-11603: a delta computed against a different base generation is
    # stale — its node/connection references may not exist in the current
    # base. Applying it silently corrupts the reconstructed graph, so refuse
    # instead of warning. When the caller supplies an explicit base snapshot
    # its generation is unknown and cannot be verified.
    if used_saver_base and saver is not None:
        delta_version = loaded.get("base_version", 0)
        with saver._lock:
            current_generation = saver._base_generation
        if delta_version != current_generation:
            raise ValueError(
                f"Delta base version mismatch: delta was computed against "
                f"generation {delta_version} but the saver base is "
                f"generation {current_generation}; refusing to apply "
                f"possibly stale data (re-save a full snapshot instead)")

    return saver.apply_delta(base_snapshot, loaded) if saver \
        else IncrementalSaver().apply_delta(base_snapshot, loaded)


# ── File Extension Helpers ──

def get_extension(
    format: SerializationFormat = SerializationFormat.JSON,
    compression: CompressionFormat = CompressionFormat.NONE
) -> str:
    """Get file extension for serialization format.
    
    Returns:
        File extension string (e.g., ".tgproj", ".tgproj.gz", ".tgproj.mp")
    """
    base = ".tgproj"
    
    if format == SerializationFormat.MSGPACK:
        base = ".tgproj.mp"  # MessagePack extension
    
    if compression == CompressionFormat.GZIP:
        return base + ".gz"
    
    return base


def parse_extension(filepath: str) -> Tuple[SerializationFormat, CompressionFormat]:
    """Parse file extension to determine format.
    
    Args:
        filepath: File path with extension
        
    Returns:
        Tuple of (SerializationFormat, CompressionFormat)
    """
    lower = filepath.lower()
    
    compression = CompressionFormat.NONE
    if lower.endswith(".gz"):
        compression = CompressionFormat.GZIP
        lower = lower[:-3]
    
    format = SerializationFormat.JSON
    if lower.endswith(".mp") or lower.endswith(".msgpack"):
        format = SerializationFormat.MSGPACK
    
    return format, compression