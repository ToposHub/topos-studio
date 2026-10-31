"""DiskCacheStore (A6) — content-addressed on-disk cache for node results.

Stores evaluated node outputs as compressed ``.npz`` (float16 RGBA) keyed
by the evaluator's content-addressed cache key: any parameter/upstream
change produces a new key, so stale hits are impossible by construction.

Format note (A6 plan deviation): the plan asked for EXR half payloads, but
the OpenEXR binding proved unstable under this app's mixed Qt/cv2 process
(experimental intermittent illegal-instruction crashes).  A cache is an
internal acceleration structure, so correctness and stability win over
DCC interop: payloads are numpy-native, dependency-free npz.

Layout::

    <root>/node_cache/
        <key>.npz          # pixel payload (RGBA, half float, compressed)
        <key>.json         # manifest: node id, frame, dims, timestamps

Both files are written through temporary files + :func:`os.replace`, so a
crash mid-write can never expose a partial entry (the ``.writing``/``.tmp``
suffixes are never recognised on read).  Eviction is LRU over the manifest
``last_used`` timestamp with a total-capacity ceiling; explicitly pinned
entries are honoured through the manifest ``pinned`` flag.

The store can be disabled process-wide (capacity <= 0, or the
``TOPOS_DISABLE_DISK_CACHE`` environment variable used by the headless
CLI's ``--no-cache``): a disabled store never writes and never hits.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import time
from typing import Any, Dict, Optional

import numpy as np

logger = logging.getLogger(__name__)

_DISK_CACHE_DIR_KEY = "composite.node_cache_dir"
_DISK_CACHE_CAPACITY_KEY = "composite.node_cache_capacity_gb"
_DISK_CACHE_CAPACITY_DEFAULT = 20.0
_DISABLE_ENV = "TOPOS_DISABLE_DISK_CACHE"

# float16 saturates at 65504; HDR values above that would become inf and
# poison every downstream consumer of the restored texture.
_F16_MAX = 65504.0


def disk_cache_disabled_by_env() -> bool:
    """True when the process asked for the disk layer to be bypassed."""
    return os.environ.get(_DISABLE_ENV, "").strip() not in ("", "0", "false")


def default_cache_root() -> str:
    """Default root follows the AI cache convention (~/.topos_cache)."""
    return os.path.join(os.path.expanduser("~"), ".topos_cache")


def _write_payload(path: str, image: np.ndarray) -> None:
    """Write a top-down RGBA float32 array as compressed float16 npz."""
    # File-object form: np.savez* would otherwise append its own ".npz".
    with open(path, "wb") as handle:
        np.savez_compressed(
            handle,
            rgba=np.clip(image, -_F16_MAX, _F16_MAX).astype(np.float16))


def _read_payload(path: str) -> Optional[np.ndarray]:
    """Read a payload written by :func:`_write_payload` as float32 RGBA."""
    with np.load(path, allow_pickle=False) as data:
        return data["rgba"].astype(np.float32)


class DiskCacheStore:
    """Content-addressed npz cache with atomic writes and LRU eviction."""

    def __init__(self, root_dir: Optional[str] = None,
                 capacity_gb: float = _DISK_CACHE_CAPACITY_DEFAULT) -> None:
        self._root = os.path.join(root_dir or default_cache_root(),
                                  "node_cache")
        self._capacity_bytes = max(0.0, float(capacity_gb)) * (1024 ** 3)
        # capacity <= 0 or the CLI disable switch turns the layer into a
        # no-op: never write, never hit (rather than "unlimited").
        self._disabled = (self._capacity_bytes <= 0
                          or disk_cache_disabled_by_env())
        if not self._disabled:
            os.makedirs(self._root, exist_ok=True)
        self._hits = 0
        self._misses = 0
        self._writes = 0
        # AUD-8304: parallel export workers (or GUI + CLI) may store/load in
        # the same store concurrently — serialize the diagnostic counters so
        # `+=` read-modify-write cycles cannot lose updates.
        self._stats_lock = threading.Lock()

    @property
    def disabled(self) -> bool:
        return self._disabled

    # ── Paths ──────────────────────────────────────────────────────────
    @staticmethod
    def _entry_stem(key: str) -> str:
        """Filesystem-safe, stable stem for a content-addressed key.

        The evaluator's cache keys embed Python-repr tuples with colons
        and quotes — Windows rejects ':' outright (Errno 22), so every
        raw-key write failed silently on this platform.  The full key is
        preserved in the manifest; only the file names are hashed.
        """
        import hashlib
        return hashlib.sha1(key.encode("utf-8")).hexdigest()[:24]

    def _payload_path(self, key: str) -> str:
        return os.path.join(self._root, f"{self._entry_stem(key)}.npz")

    def _topos_payload_path(self, key: str) -> str:
        return os.path.join(self._root, f"{self._entry_stem(key)}.toos")

    def _meta_path(self, key: str) -> str:
        return os.path.join(self._root, f"{self._entry_stem(key)}.json")

    # ── Payload format policy (P0-7: TOPOS first, diagnosable fallback) ──
    def _payload_format(self, rgba: np.ndarray) -> str:
        """Choose the payload format for a new entry.

        "auto" (default) prefers the native TOPOS .toos payload and falls
        back to npz whenever the native layer is unavailable, the payload
        is HDR (outside the 12-bit fixed range), or the encode fails —
        each fallback logged once so the state is diagnosable, never
        silent (P1-11).

        模式来源（2026-09-19 审计收口，优先级从高到低）：
        1. 环境变量 ``TOPOS_NODE_CACHE_FORMAT``（CI/诊断逃生口，既有契约）；
        2. 偏好设置键 ``composite.node_cache_format``（auto/toos/npz，
           Composite 偏好页可设）；
        3. auto（topos .toos 优先 + 可诊断 npz 回退）。
        """
        mode = os.environ.get(
            "TOPOS_NODE_CACHE_FORMAT", "").strip().lower()
        if not mode:
            try:
                from src.shared.preferences import get_config_manager
                mode = str(get_config_manager().get(
                    "composite.node_cache_format",
                    "auto")).strip().lower()
            except Exception:  # noqa: BLE001 — 配置层缺失按 auto，不阻断缓存
                mode = "auto"
        if not mode:
            mode = "auto"
        if mode in ("npz", "off"):
            return "npz_f16"
        from .topos_cache_codec import (
            FORMAT_ID, FORMAT_ID_FLOAT, payload_is_ldr,
            probe_topos_payload_codec,
        )
        probe = probe_topos_payload_codec()
        if not probe.available:
            if mode == "topos":
                self._log_format_note_once(
                    "TOPOS payload requested but the native image codec is "
                    "unavailable; using npz (P1-11 diagnosable fallback)")
            else:
                self._log_format_note_once(
                    "TOPOS payload codec unavailable; node disk cache uses "
                    "npz payloads (P1-11 fallback)")
            return "npz_f16"
        if not payload_is_ldr(rgba):
            # G6：HDR/超界载荷走 HALF .toos（image_profile 4，f16 存储
            # 精度同 npz 但经压缩）——不再回退未压缩 npz
            return FORMAT_ID_FLOAT
        return FORMAT_ID

    def _log_format_note_once(self, message: str) -> None:
        note = getattr(self, "_format_note_logged", None)
        if note != message:
            self._format_note_logged = message
            logger.info("%s", message)

    # ── Core API ───────────────────────────────────────────────────────
    def has(self, key: str) -> bool:
        """True only when BOTH the payload and its manifest are complete."""
        if self._disabled:
            return False
        if not os.path.exists(self._meta_path(key)):
            return False
        return (os.path.exists(self._payload_path(key))
                or os.path.exists(self._topos_payload_path(key)))

    def store(self, key: str, rgba: np.ndarray,
              metadata: Optional[Dict[str, Any]] = None,
              pinned: bool = False) -> bool:
        """Atomically write ``rgba`` (H, W, 4 float32) + manifest."""
        if self._disabled:
            return False
        if rgba is None or rgba.ndim != 3 or rgba.shape[2] < 4:
            return False

        payload_format = self._payload_format(rgba)
        topos_manifest: Dict[str, Any] = {}
        if payload_format != "npz_f16":
            tmp_topos = self._topos_payload_path(key) + (
                f".writing.{os.getpid()}.{threading.get_ident()}")
            try:
                from .topos_cache_codec import (
                    FORMAT_ID_FLOAT, encode_toos_payload,
                    encode_toos_payload_float,
                )
                encode_fn = (encode_toos_payload_float
                             if payload_format == FORMAT_ID_FLOAT
                             else encode_toos_payload)
                topos_manifest = encode_fn(tmp_topos, rgba)
                os.replace(tmp_topos, self._topos_payload_path(key))
                payload_path = self._topos_payload_path(key)
            except Exception as exc:
                self._safe_unlink(tmp_topos)
                self._log_format_note_once(
                    f"TOPOS payload encode failed ({exc}); using npz")
                payload_format = "npz_f16"
                payload_path = None
        else:
            payload_path = None

        if payload_path is None:
            # Unique temp suffix: parallel export workers (or GUI + CLI) may
            # write the same content-addressed key concurrently; a fixed name
            # would let two writers interleave into one corrupted payload.
            tmp_path = (payload_path := self._payload_path(key)) + (
                f".writing.{os.getpid()}.{threading.get_ident()}")
            try:
                # Payload first: has() requires the manifest, so an interrupted
                # pair (payload without manifest) is invisible to readers.
                _write_payload(tmp_path, rgba)
                os.replace(tmp_path, payload_path)
            except OSError as exc:
                self._safe_unlink(tmp_path)
                logger.warning("DiskCacheStore store failed for %s: %s",
                               key[:12], exc)
                return False
            except Exception as exc:
                self._safe_unlink(tmp_path)
                logger.warning("DiskCacheStore payload write failed for %s: %s",
                               key[:12], exc)
                return False

        # AUD-8300: a concurrent clear()/eviction (user clears the cache
        # from preferences while export workers are writing) can delete the
        # payload between the os.replace above and this size probe, so the
        # probe must not let OSError escape into the evaluator call chain.
        # A missing payload means the entry is incomplete: treat it as a
        # failed write and clean up, mirroring the manifest-write failure
        # path below.
        try:
            payload_bytes = int(os.path.getsize(payload_path))
        except OSError as exc:
            logger.warning("DiskCacheStore size probe failed for %s: %s",
                           key[:12], exc)
            self._safe_unlink(self._payload_path(key))
            self._safe_unlink(self._topos_payload_path(key))
            return False

        manifest = {
            "key": key,
            "node_id": (metadata or {}).get("node_id", ""),
            "node_type": (metadata or {}).get("node_type", ""),
            "frame": (metadata or {}).get("frame"),
            "width": int(rgba.shape[1]),
            "height": int(rgba.shape[0]),
            "channels": int(rgba.shape[2]),
            "format": payload_format,
            "created": time.time(),
            "last_used": time.time(),
            "pinned": bool(pinned),
            "bytes": payload_bytes,
        }
        if topos_manifest:
            # P0-7: every cache artifact carries codec/profile/layout/
            # alpha/bit-depth provenance so a mismatched artifact can
            # never be silently reinterpreted.
            manifest.update(topos_manifest)
            manifest["bytes"] = topos_manifest.get(
                "bytes", manifest["bytes"])

        try:
            meta_bytes = json.dumps(manifest, ensure_ascii=False,
                                    indent=1).encode("utf-8")
            if not self._atomic_write(self._meta_path(key), meta_bytes):
                self._safe_unlink(self._payload_path(key))
                self._safe_unlink(self._topos_payload_path(key))
                return False
        except OSError as exc:
            logger.warning("DiskCacheStore manifest write failed: %s", exc)
            return False

        # AUD-8303: once the manifest points at the freshly written format,
        # a sibling-format payload left over from an earlier write under a
        # different TOPOS_NODE_CACHE_FORMAT / preference (or a TOPOS→npz
        # encode fallback) is an orphan that nothing reads or evicts until
        # the key itself is evicted — reclaim it now, after the manifest
        # commit so the previous entry is never left without its payload.
        if payload_path == self._topos_payload_path(key):
            self._safe_unlink(self._payload_path(key))
        else:
            self._safe_unlink(self._topos_payload_path(key))

        with self._stats_lock:
            self._writes += 1
        self.evict_if_needed()
        return True

    def load_metadata(self, key: str) -> Optional[Dict[str, Any]]:
        try:
            with open(self._meta_path(key), "r",
                      encoding="utf-8") as handle:
                return json.load(handle)
        except (OSError, ValueError):
            return None

    def load(self, key: str) -> Optional[np.ndarray]:
        """Return the cached RGBA float32 array, or None on any miss."""
        if not self.has(key):
            with self._stats_lock:
                self._misses += 1
            return None
        meta = self.load_metadata(key) or {}
        image: Optional[np.ndarray] = None
        from .topos_cache_codec import (
            FORMAT_ID as _TOPOS_FORMAT_ID, FORMAT_ID_FLOAT as _TOPOS_FLOAT_ID,
        )
        if meta.get("format") in (_TOPOS_FORMAT_ID, _TOPOS_FLOAT_ID):
            try:
                from .topos_cache_codec import decode_toos_payload
                image = decode_toos_payload(
                    self._topos_payload_path(key),
                    int(meta.get("width", 0)), int(meta.get("height", 0)))
            except Exception as exc:  # corrupt/native vanished → drop entry
                logger.warning("TOPOS payload read failed for %s: %s",
                               key[:12], exc)
                image = None
        else:
            try:
                image = _read_payload(self._payload_path(key))
            except Exception:  # corrupt/unreadable payload
                image = None
        if image is None:
            # Corrupt payload: drop the entry rather than loop on it.
            self._safe_unlink(self._payload_path(key))
            self._safe_unlink(self._topos_payload_path(key))
            self._safe_unlink(self._meta_path(key))
            with self._stats_lock:
                self._misses += 1
            return None
        with self._stats_lock:
            self._hits += 1
        self._touch(key)
        # Orientation is preserved through imencode/imdecode; the consumer
        # owns any GPU-upload flip (mirrors the AI cache read path).
        return np.ascontiguousarray(image.astype(np.float32, copy=False))

    def delete(self, key: str) -> None:
        self._safe_unlink(self._payload_path(key))
        self._safe_unlink(self._topos_payload_path(key))
        self._safe_unlink(self._meta_path(key))

    def clear(self) -> int:
        """Remove every entry; returns the number of entries deleted."""
        count = 0
        try:
            names = os.listdir(self._root)
        except OSError:
            return 0
        for name in names:
            if (name.endswith((".npz", ".toos", ".json", ".tmp"))
                    or ".writing" in name):
                self._safe_unlink(os.path.join(self._root, name))
                if name.endswith(".json"):
                    count += 1
        return count

    # ── Eviction ───────────────────────────────────────────────────────
    def entries(self) -> Dict[str, Dict[str, Any]]:
        """All valid manifests keyed by cache key."""
        result: Dict[str, Dict[str, Any]] = {}
        try:
            names = os.listdir(self._root)
        except OSError:
            return result
        for name in names:
            if not name.endswith(".json"):
                continue
            # Read directly: load_metadata() hashes its argument, and the
            # file name is already the hashed stem, not the raw key.
            try:
                with open(os.path.join(self._root, name), "r",
                          encoding="utf-8") as handle:
                    meta = json.load(handle)
            except (OSError, ValueError):
                continue
            if meta and isinstance(meta.get("key"), str):
                result[meta["key"]] = meta
        return result

    def total_bytes(self) -> int:
        return int(sum(meta.get("bytes", 0)
                       for meta in self.entries().values()))

    def evict_if_needed(self, capacity_bytes: Optional[float] = None
                        ) -> int:
        """LRU-evict (oldest ``last_used`` first) until under capacity."""
        cap = (self._capacity_bytes if capacity_bytes is None
               else float(capacity_bytes))
        if cap <= 0:
            return 0
        entries = self.entries()
        total = sum(meta.get("bytes", 0) for meta in entries.values())
        if total <= cap:
            return 0
        evicted = 0
        by_age = sorted(entries.values(),
                        key=lambda meta: (bool(meta.get("pinned")),
                                          meta.get("last_used", 0.0)))
        for meta in by_age:
            if total <= cap:
                break
            if meta.get("pinned"):
                continue
            key = meta["key"]
            total -= int(meta.get("bytes", 0))
            self.delete(key)
            evicted += 1
        return evicted

    def stats(self) -> Dict[str, Any]:
        entries = self.entries()
        # AUD-8304: read the counters under the same lock they are
        # incremented under.
        with self._stats_lock:
            hits, misses, writes = self._hits, self._misses, self._writes
        return {
            "entries": len(entries),
            "bytes": sum(meta.get("bytes", 0)
                         for meta in entries.values()),
            "capacity_bytes": int(self._capacity_bytes),
            "hits": hits,
            "misses": misses,
            "writes": writes,
        }

    # ── Internals ──────────────────────────────────────────────────────
    def _touch(self, key: str) -> None:
        meta = self.load_metadata(key)
        if not meta:
            return
        meta["last_used"] = time.time()
        try:
            meta_bytes = json.dumps(meta, ensure_ascii=False,
                                    indent=1).encode("utf-8")
            self._atomic_write(self._meta_path(key), meta_bytes)
        except (OSError, TypeError, ValueError):
            pass

    def _atomic_write(self, path: str, data: bytes) -> bool:
        directory = os.path.dirname(path)
        try:
            handle = tempfile.NamedTemporaryFile(
                dir=directory, prefix=".tmp_", suffix=".tmp",
                delete=False)
            try:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            finally:
                handle.close()
            os.replace(handle.name, path)
            return True
        except OSError as exc:
            logger.warning("DiskCacheStore atomic write to %s failed: %s",
                           path, exc)
            return False

    @staticmethod
    def _safe_unlink(path: str) -> None:
        try:
            os.unlink(path)
        except OSError:
            pass


# ── Process-wide accessor ──────────────────────────────────────────────
_STORE_INSTANCE: Optional[DiskCacheStore] = None
# AUD-8304: first concurrent call from parallel export workers (or GUI +
# CLI import) could otherwise build two instances; double-checked locking
# makes the lazy init race-free.
_STORE_INIT_LOCK = threading.Lock()


def get_disk_cache_store() -> DiskCacheStore:
    """Lazily create the shared store using the configured preferences."""
    global _STORE_INSTANCE
    if _STORE_INSTANCE is None:
        with _STORE_INIT_LOCK:
            if _STORE_INSTANCE is None:
                root = None
                capacity = _DISK_CACHE_CAPACITY_DEFAULT
                try:
                    from src.shared.preferences import get_config_manager  # noqa
                    config = get_config_manager()
                    root = config.get(_DISK_CACHE_DIR_KEY) or None
                    capacity = float(config.get(_DISK_CACHE_CAPACITY_KEY,
                                                _DISK_CACHE_CAPACITY_DEFAULT))
                except Exception:
                    pass
                _STORE_INSTANCE = DiskCacheStore(root, capacity)
    return _STORE_INSTANCE


def reset_disk_cache_store() -> None:
    """Drop the shared instance (used by tests)."""
    global _STORE_INSTANCE
    _STORE_INSTANCE = None
