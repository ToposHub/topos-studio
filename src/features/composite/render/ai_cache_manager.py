"""
AI Cache Manager - Disk-based caching for AI node results.

Stores AI computation results as EXR/PNG files with content-addressable keys.
Provides LRU eviction when cache exceeds configured size limits.
"""

import os
import json
import hashlib
import logging
import threading
import time
import numpy as np
import cv2
from typing import Dict, Optional, Set

logger = logging.getLogger(__name__)

# AUD-7901: process-wide lock serializing every manifest read-modify-write.
# The manifest file is shared by ALL AICacheManager instances in the process
# (module.py builds a throwaway instance per save callback, composite_page
# and composite_renderer hold long-lived ones), so a per-instance lock
# cannot prevent two instances from clobbering each other's entries with
# blind whole-file overwrites.
_MANIFEST_FILE_LOCK = threading.Lock()


class AICacheManager:
    """Manages disk-based cache for AI node computation results.

    Cache directory layout:
        {cache_dir}/
            manifest.json      — Maps cache keys to metadata
            {cache_key}.exr    — Cached result images

    Content-addressable keys are computed from:
        SHA256(node_type + sorted_params_json + upstream_key)[:32]
    """

    def __init__(self, cache_dir: str = None, max_size_gb: float = 10.0) -> None:
        if cache_dir is None:
            cache_dir = os.path.join(os.path.expanduser("~"), ".topos_cache", "ai_nodes")

        self._cache_dir = cache_dir
        self._max_size_bytes = int(max_size_gb * 1024 * 1024 * 1024)
        # Instance lock: protects the in-memory manifest dict and the
        # pending-mutation journals (fast, no I/O under it — AUD-7900).
        self._lock = threading.Lock()
        self._manifest_path = os.path.join(cache_dir, "manifest.json")
        self._manifest: Dict[str, dict] = {}
        # AUD-7901: mutations not yet persisted. Persisting replays them
        # onto a freshly re-read disk manifest (read-modify-write), so a
        # sibling instance's concurrent entries are never lost.
        self._pending_adds: Dict[str, dict] = {}
        self._pending_removals: Set[str] = set()

        os.makedirs(cache_dir, exist_ok=True)
        self._load_manifest()

    def _load_manifest(self):
        """Load manifest from disk."""
        if os.path.exists(self._manifest_path):
            try:
                with open(self._manifest_path, 'r') as f:
                    self._manifest = json.load(f)
            except (json.JSONDecodeError, IOError) as exc:
                # AUD-7903: a corrupt manifest must never be silently
                # discarded — every cached .exr referenced only by it would
                # become an undiagnosable orphan. Log loudly here; the
                # on-disk file itself is preserved (backed up) by
                # _read_disk_manifest_locked before the next RMW replaces it.
                logger.warning(
                    "AI cache manifest is corrupt (%s); starting empty and "
                    "backing up the corrupt file on next write", exc)
                self._manifest = {}

    def _read_disk_manifest_locked(self) -> Dict[str, dict]:
        """Read the on-disk manifest (call under _MANIFEST_FILE_LOCK)."""
        if not os.path.exists(self._manifest_path):
            return {}
        try:
            with open(self._manifest_path, 'r') as f:
                loaded = json.load(f)
            return loaded if isinstance(loaded, dict) else {}
        except (OSError, ValueError) as exc:
            # AUD-7903: preserve the corrupt file before the caller's
            # read-modify-write replaces it, so the pre-corruption state
            # stays diagnosable and the wipe is never silent (orphaned
            # .exr files can still be reconciled against the backup).
            backup_path = self._manifest_path + ".corrupt"
            try:
                os.replace(self._manifest_path, backup_path)
                logger.warning(
                    "AI cache manifest unreadable (%s); backed up to %s",
                    exc, backup_path)
            except OSError as move_exc:
                logger.warning(
                    "AI cache manifest unreadable (%s); backup to %s "
                    "failed: %s", exc, backup_path, move_exc)
            return {}

    def _sync_manifest_to_disk(self) -> None:
        """Persist pending manifest mutations (AUD-7900/AUD-7901).

        Must NOT be called while holding ``self._lock``: the whole point of
        the pending journal is that the file I/O happens outside the
        instance lock, so renderer-thread cache queries (has_cache /
        get_cache_path) are never blocked by a manifest write.

        Cross-instance safety (AUD-7901): the write is a read-modify-write
        cycle serialized by the process-wide file lock — the disk state is
        re-read, this instance's pending adds/removals are applied on top,
        and the result replaces the file atomically (temp file + os.replace
        prevents concurrent readers from seeing a torn manifest). A failed
        write keeps the journal so the next mutation retries it.
        """
        with _MANIFEST_FILE_LOCK:
            with self._lock:
                adds = dict(self._pending_adds)
                removals = set(self._pending_removals)
            if not adds and not removals:
                return
            try:
                disk = self._read_disk_manifest_locked()
                for key in removals:
                    disk.pop(key, None)
                disk.update(adds)
                tmp_path = "{}.tmp.{}.{}".format(
                    self._manifest_path, os.getpid(), threading.get_ident())
                try:
                    with open(tmp_path, 'w') as f:
                        json.dump(disk, f, indent=2)
                    os.replace(tmp_path, self._manifest_path)
                except OSError:
                    try:
                        os.remove(tmp_path)
                    except OSError:
                        pass
                    raise
            except OSError as exc:
                logger.warning("AI cache manifest write failed: %s", exc)
                return
            # Deltas are on disk — drop them from the journal. Anything
            # mutated after the snapshot above stays queued for the next
            # sync (a removal of a just-added key is retried harmlessly).
            with self._lock:
                for key, entry in adds.items():
                    if self._pending_adds.get(key) is entry:
                        self._pending_adds.pop(key, None)
                self._pending_removals -= removals

    def _register_add(self, cache_key: str, entry: dict) -> None:
        """Record an add/update in the journal (call under self._lock)."""
        self._pending_removals.discard(cache_key)
        self._pending_adds[cache_key] = entry

    def _register_removal(self, cache_key: str) -> None:
        """Record a removal in the journal (call under self._lock)."""
        self._pending_adds.pop(cache_key, None)
        self._pending_removals.add(cache_key)

    def compute_key(self, node_type: str, params: dict, upstream_key: str = "",
                    frame_number: Optional[int] = None,
                    provider: Optional[str] = None) -> str:
        """Compute a content-addressable cache key.

        F6-03: Enhanced frame-aware cache key that includes:
        - prompt, negative_prompt, seed for AI generation uniqueness
        - frame_number for video/animation support
        - provider for model selection differentiation

        Args:
            node_type: Node type string (e.g. "ai_generate")
            params: Node parameters dict (must include prompt/seed for uniqueness)
            upstream_key: Hash key from upstream evaluation cache
            frame_number: Optional frame number for animation (F6-03)
            provider: Optional AI model provider name (F6-03)

        Returns:
            32-character hex string cache key
        """
        # F6-03: Extract critical parameters for AI cache uniqueness
        prompt = params.get("prompt", "")
        negative_prompt = params.get("negative_prompt", "")
        seed = params.get("seed", -1)
        model_path = params.get("model_path", "")
        guidance_scale = params.get("guidance_scale", 7.5)
        strength = params.get("strength", 0.75)
        width = params.get("width", 1024)
        height = params.get("height", 1024)
        mode = params.get("mode", "txt2img")

        # Build deterministic cache key with all critical parameters
        critical_params = {
            "prompt": prompt,
            "negative_prompt": negative_prompt,
            "seed": seed,
            "guidance_scale": guidance_scale,
            "strength": strength,
            "width": width,
            "height": height,
            "mode": mode,
            "model_path": model_path,
        }

        # B6: inpaint-family parameters change the produced content just as
        # much as the generation params above — a tile-size or blend change
        # must not be served from a cache entry made under the old ones.
        # Only appended when present, so generation nodes keep their keys.
        critical_params.update({
            k: params[k]
            for k in ("model", "use_tiling", "tile_size", "overlap", "blend")
            if k in params
        })

        params_str = json.dumps(critical_params, sort_keys=True, default=str)

        # F6-03: Include frame_number and provider in cache key
        frame_suffix = f":fn{frame_number}" if frame_number is not None else ""
        provider_suffix = f":pv{provider}" if provider is not None else ""

        content = f"{node_type}:{params_str}:{upstream_key}{frame_suffix}{provider_suffix}"
        return hashlib.sha256(content.encode()).hexdigest()[:32]

    def has_cache(self, cache_key: str) -> bool:
        """Check if a cached result exists."""
        with self._lock:
            entry = self._manifest.get(cache_key)
            if entry is None:
                return False
            path = entry.get("path", "")
            return os.path.exists(path)

    def get_cache_path(self, cache_key: str) -> Optional[str]:
        """Get the file path for a cached result."""
        with self._lock:
            entry = self._manifest.get(cache_key)
            if entry is None:
                return None
            path = entry.get("path", "")
            if os.path.exists(path):
                return path
            return None

    def get_cache_info(self, cache_key: str) -> Optional[dict]:
        """Get metadata for a cached result."""
        with self._lock:
            entry = self._manifest.get(cache_key)
            if entry is None:
                return None
            return dict(entry)

    def put(self, cache_key: str, image: np.ndarray, metadata: dict = None) -> str:
        """Save an image to the cache.

        Args:
            cache_key: Content-addressable key
            image: BGR float32 numpy array [0, 1]
            metadata: Optional metadata dict

        Returns:
            File path of the cached result
        """
        if metadata is None:
            metadata = {}

        h, w = image.shape[:2]
        filename = f"{cache_key}.exr"
        path = os.path.join(self._cache_dir, filename)

        # AUD-7900: the EXR encode/write of a 4K float32 frame costs hundreds
        # of milliseconds — do it OUTSIDE the instance lock so renderer-thread
        # cache queries are never blocked behind a write. A temp file +
        # atomic replace also prevents a concurrent reader from loading a
        # partially written EXR. The temp name must keep the .exr extension:
        # cv2.imwrite picks its encoder from the file extension.
        tmp_path = os.path.join(
            self._cache_dir,
            f"{cache_key}.tmp.{os.getpid()}.{threading.get_ident()}.exr")
        try:
            # OpenCV expects BGR for EXR.
            # T-P1-71（libdeflate SIGILL）：不带参数时 cv2 默认 ZIP 压缩，
            # libOpenEXR 经 libdeflate 派发在本机间歇性 SIGILL（.ips 栈
            # libdeflate_zlib_compress）。RLE(1) 无损且不走 deflate。
            cv2.imwrite(tmp_path, image.astype(np.float32),
                        [cv2.IMWRITE_EXR_COMPRESSION, 1])
            if os.path.exists(tmp_path):
                os.replace(tmp_path, path)
        except OSError as exc:
            logger.warning("AI cache EXR write failed for %s: %s",
                           cache_key, exc)
            try:
                os.remove(tmp_path)
            except OSError:
                pass

        file_size = os.path.getsize(path) if os.path.exists(path) else 0

        entry = {
            "path": path,
            "width": w,
            "height": h,
            "timestamp": time.time(),
            "size": file_size,
            **metadata,
        }
        # Lock-held work is pure bookkeeping (no I/O): manifest dict update
        # + journal entry (AUD-7900).
        with self._lock:
            self._manifest[cache_key] = entry
            self._register_add(cache_key, entry)
        self._sync_manifest_to_disk()

        # AUD-7900: eviction deletes files — collect the plan under the
        # lock, delete outside it (the manifest removals go through the
        # journal + file lock, so the cross-instance manifest stays
        # consistent).
        self._evict_if_needed()

        return path

    def invalidate(self, cache_key: str):
        """Remove a specific cache entry."""
        with self._lock:
            entry = self._manifest.pop(cache_key, None)
            self._register_removal(cache_key)
            path = (entry or {}).get("path", "") or os.path.join(
                self._cache_dir, f"{cache_key}.exr")
        # File removal outside the lock (AUD-7900).
        if path and os.path.exists(path):
            try:
                os.remove(path)
            except OSError:
                pass
        self._sync_manifest_to_disk()

    def invalidate_node(self, node_id: str):
        """Remove all cache entries for a specific node."""
        with self._lock:
            keys_to_remove = [
                k for k, v in self._manifest.items()
                if v.get("node_id") == node_id
            ]
            paths = []
            for k in keys_to_remove:
                entry = self._manifest.pop(k, None)
                self._register_removal(k)
                if entry:
                    paths.append(entry.get("path", ""))
        for path in paths:
            if path and os.path.exists(path):
                try:
                    os.remove(path)
                except OSError:
                    pass
        if keys_to_remove:
            self._sync_manifest_to_disk()

    def get_total_size(self) -> int:
        """Get total cache size in bytes."""
        with self._lock:
            return sum(v.get("size", 0) for v in self._manifest.values())

    def clear_all(self):
        """Clear all cache entries.

        "All" includes entries written by sibling instances, so this is a
        dedicated read-and-wipe under the file lock (the pending journal
        alone cannot name keys this instance never saw — AUD-7901).
        """
        with self._lock:
            paths = [entry.get("path", "")
                     for entry in self._manifest.values()]
            self._manifest.clear()
            self._pending_adds.clear()
            self._pending_removals.clear()
        with _MANIFEST_FILE_LOCK:
            disk = self._read_disk_manifest_locked()
            paths.extend(e.get("path", "") for e in disk.values())
            tmp_path = "{}.tmp.{}.{}".format(
                self._manifest_path, os.getpid(), threading.get_ident())
            try:
                with open(tmp_path, 'w') as f:
                    json.dump({}, f, indent=2)
                os.replace(tmp_path, self._manifest_path)
            except OSError as exc:
                logger.warning("AI cache manifest write failed: %s", exc)
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass
        for path in dict.fromkeys(paths):
            if path and os.path.exists(path):
                try:
                    os.remove(path)
                except OSError:
                    pass

    def _evict_if_needed(self):
        """Evict oldest entries if cache exceeds size limit.

        AUD-7900: the eviction plan (which keys) is computed under the
        instance lock, but the file deletions and the manifest persist
        happen outside it.
        """
        with self._lock:
            total = sum(v.get("size", 0) for v in self._manifest.values())
            if total <= self._max_size_bytes:
                return
            # Sort by timestamp (oldest first)
            sorted_entries = sorted(
                self._manifest.items(),
                key=lambda x: x[1].get("timestamp", 0)
            )
            evict = []
            for key, entry in sorted_entries:
                if total <= self._max_size_bytes * 0.8:  # Evict to 80% capacity
                    break
                path = entry.get("path", "")
                size = entry.get("size", 0)
                self._manifest.pop(key, None)
                self._register_removal(key)
                evict.append(path)
                total -= size
        if not evict:
            return
        for path in evict:
            if path and os.path.exists(path):
                try:
                    os.remove(path)
                except OSError:
                    pass
        self._sync_manifest_to_disk()
