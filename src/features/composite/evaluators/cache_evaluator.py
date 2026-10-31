"""
CacheEvaluatorMixin - Cache management methods.

Provides cache-related methods:
- Source texture ID tracking
- Cache invalidation for specific nodes
- Full cache clearing
- A6 node disk-cache layer (read-back fill + write-through for nodes that
  opt in through the ``cache_to_disk`` parameter)

These methods manage the content-addressable cache that stores
GPU textures for reuse across evaluations.
"""

import logging
import queue
import threading
from typing import Any, Dict, Optional, Set, TYPE_CHECKING

import numpy as np

from ..nodes.base_node import TextureResult

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


# ── AUD-5923: bounded background disk-cache writer ────────────────────
# The GL readback must stay on the render thread (GL state is thread-bound),
# but the npz/TOPOS compression + file IO no longer have to: a cache miss
# used to spend its whole frame budget compressing ~132MB of 4K float32.
# A single writer thread with a tiny queue keeps memory bounded (~2 frames)
# and never blocks the render thread; when the writer falls behind, the
# store is skipped (disk cache is an opt-in best-effort layer).
_DISK_STORE_QUEUE_MAXSIZE = 2

_disk_store_queue: "queue.Queue[Optional[tuple[str, np.ndarray, Dict[str, Any]]]]" = (
    queue.Queue(maxsize=_DISK_STORE_QUEUE_MAXSIZE)
)
_disk_store_thread: Optional[threading.Thread] = None
_disk_store_thread_lock = threading.Lock()


def _disk_store_worker_loop() -> None:
    while True:
        item = _disk_store_queue.get()
        if item is None:
            return
        disk_key, rgba, metadata = item
        try:
            from ..render.disk_cache_store import get_disk_cache_store
            get_disk_cache_store().store(disk_key, rgba, metadata)
        except Exception as exc:
            logger.warning(
                "Disk cache write failed for %s: %s",
                metadata.get("node_id"), exc,
            )


def _ensure_disk_store_worker() -> None:
    global _disk_store_thread
    with _disk_store_thread_lock:
        if _disk_store_thread is not None and _disk_store_thread.is_alive():
            return
        _disk_store_thread = threading.Thread(
            target=_disk_store_worker_loop,
            name="disk-cache-store-writer",
            daemon=True,
        )
        _disk_store_thread.start()


class CacheEvaluatorMixin:
    """Mixin providing cache management methods."""

    # ── A6: node disk-cache layer ─────────────────────────────────────
    @staticmethod
    def _disk_cache_key(cache_key: str) -> str:
        """Namespace node keys inside the shared store ("n" prefix)."""
        return "n" + cache_key

    def _load_disk_cache_outputs(
            self, ns, cache_key: str,
            working_inputs: Dict[str, TextureResult]) -> Optional[dict]:
        """Restore a node result from the disk layer, or None on miss.

        AUD-5920: the disk-hit branch in ``_evaluate_snapshot`` bypasses the
        ``_propagate_auxiliary_metadata`` call every fresh evaluation runs,
        so depth/AOV/render_diagnostics silently vanished on hit frames
        (opt-in ``cache_to_disk`` nodes are precisely the depth/AOV family —
        zblur/fog/depth_matte/cryptomatte_select/relight …). The restored
        result now gets the same auxiliary propagation, referencing the
        working input's depth/AOV glo directly.
        """
        try:
            from ..render.disk_cache_store import get_disk_cache_store
            rgba = get_disk_cache_store().load(
                self._disk_cache_key(cache_key))
        except Exception as exc:  # disk problems must never break eval
            logger.warning("Disk cache read failed: %s", exc)
            return None
        if rgba is None:
            return None
        reference = next(
            (tex for tex in working_inputs.values()
             if isinstance(tex, TextureResult) and tex.is_valid), None)
        tex_result = self._upload_rgba_array_result(rgba, reference)
        if tex_result is None:
            return None
        if reference is not None:
            tex_result = self._propagate_auxiliary_metadata(
                working_inputs, {"__disk__": tex_result}).get("__disk__")
            if not isinstance(tex_result, TextureResult):
                return None
        port = ns.output_ports[0][0] if ns.output_ports else "result"
        return {port: tex_result}

    def _store_disk_cache_outputs(self, ns, cache_key: str,
                                  outputs: Dict[str, Any]) -> None:
        """Write the node's primary output to the disk layer (opt-in).

        AUD-5923: the GL readback stays synchronous on the render thread
        (GL state is thread-bound), but the npz/TOPOS payload compression
        and file IO are handed to a bounded background writer thread so a
        cache miss no longer spikes the frame time. When the writer is
        still busy with earlier frames the store is skipped — the disk
        layer is a best-effort optimization, never a correctness contract.
        """
        try:
            tex = next(
                (tex for tex in outputs.values()
                 if isinstance(tex, TextureResult) and tex.is_valid),
                None)
            if tex is None:
                return
            rgba = self._readback_result_array(tex)
            if rgba is None:
                return
            metadata = {"node_id": ns.node_id, "node_type": ns.node_type}
            _ensure_disk_store_worker()
            try:
                _disk_store_queue.put_nowait(
                    (self._disk_cache_key(cache_key), rgba, metadata))
            except queue.Full:
                logger.debug(
                    "Disk cache writer busy; skipping store for %s",
                    ns.node_id,
                )
        except Exception as exc:
            logger.warning("Disk cache write failed for %s: %s",
                           ns.node_id, exc)

    def _readback_result_array(
            self, tex_result: TextureResult) -> Optional["np.ndarray"]:
        """Read a GPU texture back as a top-down RGBA float32 array."""
        tex = self._get_texture_by_id(tex_result.texture_id)
        if tex is None:
            return None
        fbo = self.fb_manager.get_fbo(tex)
        fbo.use()
        self.ctx.viewport = (0, 0, int(tex_result.width),
                             int(tex_result.height))
        raw = fbo.read(components=4, dtype="f4")
        arr = np.frombuffer(raw, dtype=np.float32).reshape(
            tex_result.height, tex_result.width, 4)
        # GL rows start at the bottom; CPU arrays are top-down.
        return np.ascontiguousarray(np.flipud(arr))

    def _upload_rgba_array_result(
            self, rgba: "np.ndarray",
            reference: Optional[TextureResult]) -> Optional[TextureResult]:
        """Upload a top-down RGBA float32 array as a pipeline texture."""
        if rgba is None or rgba.ndim != 3 or rgba.shape[2] < 4:
            return None
        h, w = rgba.shape[:2]
        if h <= 0 or w <= 0:
            return None
        data = np.ascontiguousarray(np.flipud(rgba))
        tex = None
        try:
            tex = self._resource_pool.acquire_texture(w, h)
            # The shared pool hands out half-float (f2) textures; the
            # readback payload is float32, so narrow it before upload or
            # the byte-size check in tex.write() rejects the whole hit.
            tex.write(data.astype(np.float16).tobytes())
        except Exception as exc:
            # AUD-5930 同款泄漏模式（batch-059 证据）：acquire 成功后
            # write 抛错时纹理滞留 active，归还后再返回 None。
            if tex is not None:
                self._release_effect_textures_on_error(tex)
            logger.warning("Disk cache upload failed: %s", exc)
            return None
        return TextureResult(
            texture_id=tex.glo,
            width=w,
            height=h,
            format=reference.format if reference is not None else None,
            alpha_premultiplied=(reference.alpha_premultiplied
                                 if reference is not None else None),
            color_space=(reference.color_space
                         if reference is not None else None),
        )

    # ── Source texture tracking ────────────────────────────────────────
    def _get_source_texture_ids(self) -> Set[int]:
        """Get texture IDs owned by GPU-stateful source nodes.

        Source nodes (color_input, constant, image_input) manage their own
        texture lifetimes. These should NOT be released during cancellation.
        """
        source_ids = set()
        for nid, node in self._render_nodes.items():
            if hasattr(node, 'get_texture_id'):
                tex_id = node.get_texture_id()
                if tex_id > 0:
                    source_ids.add(tex_id)
        return source_ids

    def _invalidate_cache_for_node(self, node_id: str):
        """Remove all cache entries for a specific node, releasing GPU textures."""
        self._cache_manager.invalidate_node(node_id)

    def _invalidate_cache_with_downstream(self, snapshot, node_id: str) -> None:
        """Invalidate cache entries for ``node_id`` and every node downstream of it.

        Used by self-managed source nodes (e.g. Constant) that release + recreate
        their GL texture on content change. Such a node's released glo may still
        be referenced by cached TextureResults of its consumers — most notably an
        Output node in passthrough ("input" format_mode), which copies the source
        glo verbatim into its own display result. Clearing only the source's entry
        would leave those downstream entries holding a dead texture name; binding
        it (cache hit on a later frame) segfaults the GL driver. This walks the
        connection graph from ``node_id`` and drops every reachable entry.

        ``snapshot`` is the GraphSnapshot used for the current evaluation; it
        provides ``get_downstream_nodes`` for the traversal.
        """
        if snapshot is None:
            self._invalidate_cache_for_node(node_id)
            return
        # BFS over the downstream subgraph. get_downstream_nodes returns direct
        # consumers; expand transitively so multi-hop chains (Constant→Merge→
        # Output) are fully cleared. Guard against cycles with a visited set.
        visited: Set[str] = set()
        frontier = [node_id]
        while frontier:
            nid = frontier.pop()
            if nid in visited:
                continue
            visited.add(nid)
            self._invalidate_cache_for_node(nid)
            try:
                frontier.extend(snapshot.get_downstream_nodes(nid))
            except Exception:
                # Snapshot API missing/changed — best-effort; the source entry
                # itself is already cleared above.
                pass

    def _clear_eval_cache(self):
        """Clear the entire eval cache, releasing all GPU temp textures.

        This is the ONLY correct way to fully clear the cache.
        Never use bare self._cache_manager._cache.clear() — it would leak GPU textures.
        """
        self._cache_manager.clear()