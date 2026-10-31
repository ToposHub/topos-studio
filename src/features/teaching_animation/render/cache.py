"""Scene/frame render cache with explicit renderer and asset identity."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import threading
from typing import Any

from ..core.contracts import RenderContext, RenderedFrame
from ..core.model import TeachingSceneDocument
from .asset_resolver import AssetResolver

#: Upper bound on memoized file digests.  Render tasks touch a bounded asset
#: set; the cap only guards pathological long-lived processes.
_FILE_HASH_MEMO_LIMIT = 512
_file_hash_memo: dict[tuple[str, int, int], str] = {}
_file_hash_memo_lock = threading.Lock()


def _file_hash(path: Path) -> str:
    """sha256 of file content, memoized by ``(path, size, mtime_ns)``.

    ``build_cache_manifest`` runs once per rendered frame; re-reading and
    re-hashing every resolved asset on every frame dominated the cache-lookup
    cost (AUD-29810).  Keying the memo on stat identity keeps the cache-key
    contract exact: any content change bumps ``st_size`` or ``st_mtime_ns`` and
    forces a fresh digest, so a memoized hash is always the sha256 the file
    would hash to right now.
    """
    try:
        stat = path.stat()
    except OSError:
        # No stat identity available (racing delete etc.): hash straight from
        # content, preserving the historical raise-on-missing behaviour.
        return hashlib.sha256(path.read_bytes()).hexdigest()
    key = (str(path), stat.st_size, stat.st_mtime_ns)
    with _file_hash_memo_lock:
        cached = _file_hash_memo.get(key)
    if cached is not None:
        return cached
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    with _file_hash_memo_lock:
        if len(_file_hash_memo) >= _FILE_HASH_MEMO_LIMIT:
            _file_hash_memo.clear()
        _file_hash_memo[key] = digest
    return digest


@dataclass(frozen=True)
class RenderCacheManifest:
    scene_id: str
    scene_hash: str
    renderer_version: str
    backend: str
    frame: int
    width: int
    height: int
    asset_hashes: tuple[tuple[str, str], ...] = ()
    font_hashes: tuple[tuple[str, str], ...] = ()
    #: The pinned renderer build (e.g. the chromium revision for an HTML scene).
    #: Participates in the cache key so a renderer upgrade never serves a stale
    #: frame captured under a different build (plan invariant §3.1).
    renderer_build: str = ""
    #: sha256 of the packaged font set the HTML font lock renders with.  A
    #: font-file bump must invalidate cached frames exactly as a renderer bump.
    font_set_hash: str = ""
    #: Device pixel ratio of the capture surface.  Two renders that differ only
    #: by DPR rasterise different CSS layouts at the same physical dimensions,
    #: so DPR must participate in the cache key (determinism contract §3.1).
    dpr: float = 1.0
    #: Every asset id the scene/layer references, resolved or not (scene
    #: asset_refs ∪ html_layer.bound_asset_ids).  Participating in the key is
    #: what prevents cache poisoning on the non-strict (preview) path: a bound
    #: image that is undeclared/missing renders the deterministic 1×1
    #: placeholder and the frame is cached; when the asset is later declared
    #: and resolved, this set (or its content hash) changes so the stale
    #: placeholder is never served (W2 audit D-4).
    declared_asset_ids: tuple[str, ...] = ()

    @property
    def cache_key(self) -> str:
        data = {
            "scene_id": self.scene_id,
            "scene_hash": self.scene_hash,
            "renderer_version": self.renderer_version,
            "renderer_build": self.renderer_build,
            "backend": self.backend,
            "frame": self.frame,
            "width": self.width,
            "height": self.height,
            "dpr": self.dpr,
            "asset_hashes": self.asset_hashes,
            "font_hashes": self.font_hashes,
            "font_set_hash": self.font_set_hash,
            "declared_asset_ids": self.declared_asset_ids,
        }
        return hashlib.sha256(json.dumps(data, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        return {
            "scene_id": self.scene_id,
            "scene_hash": self.scene_hash,
            "renderer_version": self.renderer_version,
            "renderer_build": self.renderer_build,
            "backend": self.backend,
            "frame": self.frame,
            "width": self.width,
            "height": self.height,
            "dpr": self.dpr,
            "asset_hashes": [list(item) for item in self.asset_hashes],
            "font_hashes": [list(item) for item in self.font_hashes],
            "font_set_hash": self.font_set_hash,
            "declared_asset_ids": list(self.declared_asset_ids),
            "cache_key": self.cache_key,
        }


def build_cache_manifest(
    scene: TeachingSceneDocument,
    frame: int,
    context: RenderContext,
    *,
    renderer_build: str = "",
) -> RenderCacheManifest:
    resolver = AssetResolver(context)
    result = resolver.preflight(scene)
    scene_payload = json.dumps(scene.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    asset_hashes = tuple(sorted((asset_id, _file_hash(path)) for asset_id, path in result.resolved.items()))
    font_hashes: list[tuple[str, str]] = []
    root = Path(context.asset_root).resolve() if context.asset_root else None
    if root is not None:
        for element in scene.elements:
            for key in ("font_file", "font_path"):
                value = element.style.get(key) if isinstance(element.style, dict) else None
                if value:
                    path = (root / str(value)).resolve()
                    if path.is_file():
                        font_hashes.append((f"{element.element_id}:{key}", _file_hash(path)))
    # An HTML scene's pixels depend on the packaged font lock, so its cache key
    # must carry the font-set hash (the lock is applied unconditionally with the
    # default packaged set; a layer-built scene records it, else the default).
    font_set_hash = ""
    html_layer = getattr(scene, "html_layer", None)
    if html_layer is not None:
        font_set_hash = getattr(html_layer, "font_set_hash", "") or ""
        if not font_set_hash:
            from ..core.html_fonts import default_font_set_hash
            font_set_hash = default_font_set_hash()
    # Declared/bound asset ids (resolved or not).  The union of scene asset_refs
    # and html_layer.bound_asset_ids participates in the key so a missing bound
    # image that later resolves can never be served stale (W2 audit D-4).
    declared: set[str] = {asset.asset_id for asset in scene.asset_refs}
    if html_layer is not None:
        declared.update(getattr(html_layer, "bound_asset_ids", ()))
    return RenderCacheManifest(
        scene_id=scene.scene_id,
        scene_hash=hashlib.sha256(scene_payload.encode()).hexdigest(),
        renderer_version=context.renderer_version,
        backend=context.backend,
        frame=frame,
        width=context.width,
        height=context.height,
        dpr=getattr(context, "dpr", 1.0),
        asset_hashes=asset_hashes,
        font_hashes=tuple(sorted(font_hashes)),
        renderer_build=renderer_build,
        font_set_hash=font_set_hash,
        declared_asset_ids=tuple(sorted(declared)),
    )


class SceneRenderCache:
    """Thread-safe in-memory cache; disk persistence is an explicit export concern."""

    def __init__(self, max_entries: int = 512) -> None:
        if max_entries <= 0:
            raise ValueError("max_entries must be positive")
        self._max_entries = max_entries
        self._frames: dict[str, RenderedFrame] = {}
        self._manifests: dict[str, RenderCacheManifest] = {}
        self._lock = threading.RLock()
        self._hits = 0
        self._misses = 0
        self._invalidations = 0

    def get(self, manifest: RenderCacheManifest) -> RenderedFrame | None:
        with self._lock:
            frame = self._frames.get(manifest.cache_key)
            if frame is None:
                self._misses += 1
            else:
                self._hits += 1
            return frame

    def put(self, manifest: RenderCacheManifest, frame: RenderedFrame) -> None:
        with self._lock:
            if len(self._frames) >= self._max_entries and manifest.cache_key not in self._frames:
                oldest = next(iter(self._frames))
                self._frames.pop(oldest, None)
                self._manifests.pop(oldest, None)
            self._frames[manifest.cache_key] = frame
            self._manifests[manifest.cache_key] = manifest

    def invalidate_scene(self, scene_id: str) -> int:
        with self._lock:
            keys = [key for key, manifest in self._manifests.items() if manifest.scene_id == scene_id]
            for key in keys:
                self._frames.pop(key, None)
                self._manifests.pop(key, None)
            self._invalidations += len(keys)
            return len(keys)

    def invalidate_frame_range(self, scene_id: str, start_frame: int, end_frame: int) -> int:
        """Invalidate only frames in the half-open ``[start, end)`` range.

        The cache stores complete frames rather than tiles, so the bbox part of
        a dirty range is intentionally handled by the caller's render plan.
        Keeping frame selection here still avoids throwing away unrelated
        scrub frames when a single animated element changes.
        """
        if start_frame < 0 or end_frame <= start_frame:
            raise ValueError("frame range must be non-negative and half-open")
        with self._lock:
            keys = [
                key for key, manifest in self._manifests.items()
                if manifest.scene_id == scene_id
                and start_frame <= manifest.frame < end_frame
            ]
            for key in keys:
                self._frames.pop(key, None)
                self._manifests.pop(key, None)
            self._invalidations += len(keys)
            return len(keys)

    def clear(self) -> None:
        with self._lock:
            self._frames.clear()
            self._manifests.clear()

    def cached_frames_for_scene(self, scene_id: str) -> frozenset[int]:
        """Return the frame numbers currently available for ``scene_id``.

        The timeline cache indicator uses this read-only snapshot to show
        green only for motion-graphic frames that have actually rendered.
        Returning frame numbers instead of frame objects keeps the UI layer
        independent from renderer backends and avoids copying pixel buffers.
        """
        with self._lock:
            return frozenset(
                manifest.frame
                for manifest in self._manifests.values()
                if manifest.scene_id == scene_id
            )

    def nearest_frame_for_scene(
        self, scene_id: str, target_frame: int,
    ) -> tuple[int, RenderedFrame] | None:
        """Return the cached frame closest to ``target_frame`` for playback statics.

        Playback without a valid disk cache shows one frozen frame instead of
        blocking the preview worker on a browser capture.  The manifest store
        is scanned under one lock and the frame is fetched by its stored
        ``cache_key`` so a lookup never has to rebuild the key (whose
        ``renderer_build`` component can only be confirmed by a live renderer
        session and would therefore miss).
        """
        with self._lock:
            best_key: str | None = None
            best_frame: int | None = None
            for key, manifest in self._manifests.items():
                if manifest.scene_id != scene_id:
                    continue
                if (
                    best_frame is None
                    or abs(manifest.frame - target_frame) < abs(best_frame - target_frame)
                ):
                    best_key = key
                    best_frame = manifest.frame
            if best_key is None or best_frame is None:
                return None
            frame = self._frames.get(best_key)
            if frame is None:
                return None
            return best_frame, frame

    def is_range_cached(self, scene_id: str, start_frame: int, end_frame: int) -> bool:
        """Whether every frame in half-open ``[start_frame, end_frame)`` exists."""
        if end_frame <= start_frame:
            return False
        cached = self.cached_frames_for_scene(scene_id)
        return all(frame in cached for frame in range(start_frame, end_frame))

    def stats(self) -> dict[str, int]:
        with self._lock:
            return {
                "entries": len(self._frames),
                "hits": self._hits,
                "misses": self._misses,
                "invalidations": self._invalidations,
            }
