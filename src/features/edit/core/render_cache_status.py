"""Timeline render-cache status aggregation.

The timeline indicator is intentionally driven by a small, model-only
module.  It knows which clips need composited rendering, which cache manifest
belongs to a clip, and how overlapping tracks collapse into one red/green
status strip.  Qt painting stays in the UI layer.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
from fractions import Fraction
from typing import Any

from src.features.edit.core.render_cache import (
    RenderCacheManifest,
    RenderCacheStatus,
    build_render_cache_fingerprint,
)
from src.shared.media.motion_graphic_delivery import (
    MOTION_GRAPHIC_CACHE_CODEC,
    MOTION_GRAPHIC_CACHE_CONTAINER,
    MOTION_GRAPHIC_CACHE_PATH_TAG,
    motion_graphic_preview_size,
)


STATUS_CACHED = "cached"
STATUS_UNCACHED = "uncached"


class _ManifestRoundIndex:
    """Single-pass index over ``project.render_cache_manifests``.

    AUD-17614: the indicator used to call ``project.get_render_cache_for_clip``
    for every clip, and each such call linearly scanned every manifest dict
    (rebuilding each one via ``from_dict``), giving O(clips × manifests) work
    per indicator refresh on the UI thread.  Building this index once per
    round (``build_cache_indicator_segments``) turns that into a single
    O(manifests) pass + O(1) lookups, with ``from_dict`` executed at most
    once per manifest per round.

    Semantics are identical to the project getter it replaces:

    - ``lookup(clip)`` without fingerprint → newest (last appended) manifest
      for the (timeline, clip) pair — the legacy "latest" behavior;
    - ``lookup(clip, fingerprint)`` → newest manifest whose render
      fingerprint matches exactly;
    - ``shared_motion_graphic()`` → newest VALID motion-graphic manifest for
      the (timeline, scene) pair whose file (if any) still exists.
    """

    __slots__ = ("by_clip", "mg_by_scene")

    def __init__(self, manifests: Any) -> None:
        self.by_clip: dict[tuple[str, str], list[Any]] = {}
        self.mg_by_scene: dict[tuple[str, str], list[Any]] = {}
        raw_values = manifests.values() if hasattr(manifests, "values") else ()
        for raw in tuple(raw_values):
            manifest = raw
            if isinstance(manifest, dict):
                try:
                    manifest = RenderCacheManifest.from_dict(manifest)
                except (TypeError, ValueError):
                    continue
            if not isinstance(manifest, RenderCacheManifest):
                continue
            self.by_clip.setdefault(
                (str(manifest.timeline_id), str(manifest.clip_id)), []
            ).append(manifest)
            scene_id = str(getattr(manifest, "scene_id", "") or "")
            if manifest.render_kind == "motion_graphic" and scene_id:
                self.mg_by_scene.setdefault(
                    (str(manifest.timeline_id), scene_id), []
                ).append(manifest)

    def lookup(
        self,
        timeline_id: str,
        clip_id: str,
        render_fingerprint: str | None = None,
    ) -> Any | None:
        entries = self.by_clip.get((str(timeline_id), str(clip_id)))
        if not entries:
            return None
        if render_fingerprint is None:
            return entries[-1]
        fingerprint = str(render_fingerprint)
        for manifest in reversed(entries):
            if manifest.render_fingerprint == fingerprint:
                return manifest
        return None

    def shared_motion_graphic(self, timeline_id: str, scene_id: str) -> Any | None:
        for manifest in reversed(
            self.mg_by_scene.get((str(timeline_id), str(scene_id)), ())
        ):
            if (
                manifest.status == RenderCacheStatus.VALID
                and (not manifest.path or os.path.exists(manifest.path))
            ):
                return manifest
        return None


def _fps_token(value: Any) -> str:
    """Normalize Fraction/float FPS values to a comparable rational token."""
    if value is None:
        return ""
    if hasattr(value, "num") and hasattr(value, "den"):
        try:
            return f"{int(value.num)}/{int(value.den)}"
        except (TypeError, ValueError, ZeroDivisionError):
            return ""
    raw = str(value).strip()
    if "/" in raw:
        try:
            num, den = raw.split("/", 1)
            return f"{int(num)}/{int(den)}"
        except (TypeError, ValueError, ZeroDivisionError):
            return raw
    try:
        fraction = Fraction(float(value)).limit_denominator(100000)
        return f"{fraction.numerator}/{fraction.denominator}"
    except (TypeError, ValueError, ZeroDivisionError):
        return raw


@dataclass(frozen=True)
class CacheIndicatorSegment:
    """A half-open timeline range that needs cached rendering."""

    start_frame: int
    end_frame: int
    status: str


def _asset_for_clip(project: Any, clip: Any) -> Any | None:
    assets = getattr(project, "assets", {}) or {}
    return assets.get(getattr(clip, "asset_id", ""))


def clip_requires_render_cache(clip: Any, asset: Any | None) -> bool:
    """Return whether a clip is not a direct real-time source preview.

    Plain source video/audio/image clips intentionally return ``False`` so
    they leave no indicator line.  Composite, generator, nested, effect and
    animated clips are treated as cache-required because their final pixels
    are assembled by the edit/render pipeline.
    """
    if asset is None:
        return bool(getattr(clip, "composite_id", None) or getattr(clip, "effects", ()))

    media_type = str(getattr(asset, "media_type", "") or "")
    if media_type in {
        "comp_clip", "motion_graphic", "nested_sequence", "adjustment", "solid",
    }:
        return True
    if getattr(clip, "composite_id", None) and not getattr(clip, "composite_bypassed", False):
        return True
    if getattr(clip, "effects", ()):
        return True
    if getattr(clip, "animated_params", {}):
        return True
    if float(getattr(clip, "opacity", 1.0) or 1.0) != 1.0:
        return True
    return False


def _motion_graphic_range_cached(asset: Any, clip: Any) -> bool:
    """Check the shared in-memory scene cache when available."""
    scene_id = (getattr(asset, "metadata", {}) or {}).get("scene_id")
    if not scene_id:
        return False
    try:
        from src.features.teaching_animation.scene_cache import (
            get_existing_scene_cache_coordinator,
        )
        coordinator = get_existing_scene_cache_coordinator()
        if coordinator is None:
            return False
        source_start = max(0, int(getattr(clip, "source_start", 0) or 0))
        source_end = source_start + int(getattr(clip, "timeline_duration", 0) or 0)
        # A timeline clip may be longer than the authored scene after a trim
        # or an old project migration.  The provider clamps playback to the
        # final scene frame, so the indicator must use that same finite range
        # instead of waiting forever for impossible frame numbers.
        metadata = getattr(asset, "metadata", {}) or {}
        scene_duration = int(metadata.get("duration_frames", 0) or 0)
        if scene_duration > 0:
            source_end = min(source_end, source_start + scene_duration)
        if source_end <= source_start:
            return False
        return coordinator.cache.is_range_cached(
            str(scene_id), source_start, source_end
        )
    except (ImportError, RuntimeError, TypeError, ValueError):
        return False


def _shared_motion_graphic_manifest(
    project: Any,
    timeline_id: str,
    scene_id: str,
    index: "_ManifestRoundIndex | None" = None,
) -> Any | None:
    """Find a valid scene cache that can be shared by a timeline duplicate.

    AUD-17614: when a round index is supplied (indicator rebuild), the scan
    is served from the single-pass scene index instead of re-walking and
    re-deserializing every manifest per uncached motion-graphic clip.
    """
    if index is not None:
        return index.shared_motion_graphic(timeline_id, scene_id)
    manifests = getattr(project, "render_cache_manifests", {}) or {}
    for raw in reversed(tuple(manifests.values())):
        candidate = raw
        if isinstance(candidate, dict):
            try:
                candidate = RenderCacheManifest.from_dict(candidate)
            except (TypeError, ValueError):
                continue
        if not isinstance(candidate, RenderCacheManifest):
            continue
        if (
            candidate.timeline_id == str(timeline_id)
            and candidate.render_kind == "motion_graphic"
            and candidate.scene_id == str(scene_id)
            and candidate.status == RenderCacheStatus.VALID
            and (not candidate.path or os.path.exists(candidate.path))
        ):
            return candidate
    return None


def cache_status_for_clip(
    project: Any,
    timeline_id: str,
    clip: Any,
    index: "_ManifestRoundIndex | None" = None,
) -> str | None:
    """Return ``cached``/``uncached`` or ``None`` for direct real-time media.

    ``index`` (AUD-17614) is the per-round manifest index built by
    :func:`build_cache_indicator_segments`; supplying it replaces the
    per-clip linear manifest scan with O(1) lookups.  It is optional so
    direct callers keep working unchanged (including test shells that
    expose custom ``get_render_cache_for_clip`` getters).
    """
    asset = _asset_for_clip(project, clip)
    if not clip_requires_render_cache(clip, asset):
        return None

    current_fingerprint = build_render_cache_fingerprint(project, timeline_id, clip)
    manifest = None
    if index is not None:
        # Fingerprint-qualified first, unqualified fallback — the same
        # selection order as the project getter below.
        manifest = index.lookup(
            timeline_id,
            getattr(clip, "clip_id", ""),
            render_fingerprint=current_fingerprint,
        )
        if manifest is None:
            manifest = index.lookup(timeline_id, getattr(clip, "clip_id", ""))
    else:
        getter = getattr(project, "get_render_cache_for_clip", None)
        if callable(getter):
            # Select the newest matching semantic variant first.  Falling back to
            # the unqualified lookup keeps legacy projects (whose manifests have
            # no fingerprint) working and also lets the caller report a clear
            # uncached state when the newest variant belongs to an edited graph.
            try:
                manifest = getter(
                    timeline_id,
                    getattr(clip, "clip_id", ""),
                    render_fingerprint=current_fingerprint,
                )
            except TypeError:
                # Small test shells and third-party project adapters may still
                # expose the pre-P2-18 two-argument getter.
                manifest = None
            if manifest is None:
                manifest = getter(timeline_id, getattr(clip, "clip_id", ""))
    if asset is not None and getattr(asset, "media_type", "") == "motion_graphic":
        # Prefer a completed disk cache; fall back to the existing per-frame
        # scene cache while a video cache is being built or before the user
        # explicitly promotes it to a file.
        scene_id = str(
            ((getattr(asset, "metadata", {}) or {}).get("scene_id", "")) or ""
        )
        # Timeline duplication creates a new ClipInstance but intentionally
        # keeps the same virtual scene asset.  Its cache is shared by scene
        # identity; use another valid manifest when the duplicate has no
        # clip-specific entry of its own.
        manifest_is_shared = False
        if manifest is None:
            manifest = _shared_motion_graphic_manifest(
                project, timeline_id, scene_id, index=index)
            manifest_is_shared = manifest is not None
        if manifest is not None:
            if isinstance(manifest, dict):
                manifest = RenderCacheManifest.from_dict(manifest)
            clip_start = int(getattr(clip, "timeline_start", 0) or 0)
            clip_end = int(getattr(clip, "timeline_end", clip_start) or clip_start)
            # ``start_frame`` is the position at which the cache was baked,
            # not a source identity.  Moving, splitting, or trimming a clip
            # must continue to reuse the same scene recording.  Only an
            # extension beyond the recorded duration requires a new render.
            recorded_duration = int(getattr(manifest, "duration_frames", 0) or 0)
            requested_duration = max(0, clip_end - clip_start)
            range_matches = recorded_duration <= 0 or recorded_duration >= requested_duration
            scene_matches = not getattr(manifest, "scene_id", "") or (
                str(getattr(manifest, "scene_id", "")) == str(scene_id)
            )
            if (
                isinstance(manifest, RenderCacheManifest)
                and manifest.status == RenderCacheStatus.VALID
                and (not manifest.path or os.path.exists(manifest.path))
                and scene_matches
                and range_matches
            ):
                # Motion Graphic playback has one canonical delivery
                # contract.  A valid legacy ProRes/H.264 file (or a previous
                # low-bitrate VP8 file) must be rendered again instead of
                # being reported green merely because it exists on disk.
                path_matches = (
                    not manifest.path
                    or f".{MOTION_GRAPHIC_CACHE_PATH_TAG}.{MOTION_GRAPHIC_CACHE_CONTAINER}"
                    in os.path.basename(str(manifest.path)).lower()
                )
                format_matches = (
                    str(getattr(manifest, "codec", "")).lower()
                    == MOTION_GRAPHIC_CACHE_CODEC
                    and str(getattr(manifest, "container", "")).lower()
                    == MOTION_GRAPHIC_CACHE_CONTAINER
                    and path_matches
                )
                timeline_ref = (getattr(project, "timelines", {}) or {}).get(timeline_id)
                if timeline_ref is not None:
                    expected_width, expected_height = motion_graphic_preview_size(
                        getattr(timeline_ref, "width", 1920),
                        getattr(timeline_ref, "height", 1080),
                    )
                    format_matches = format_matches and (
                        int(getattr(manifest, "width", 0) or 0) == expected_width
                        and int(getattr(manifest, "height", 0) or 0) == expected_height
                    )
                    # A cache recorded at a different timeline frame rate is
                    # not safe to reuse: duration/frame mapping and playback
                    # cadence no longer match the current sequence.
                    manifest_fps = _fps_token(getattr(manifest, "timeline_fps", ""))
                    timeline_fps = _fps_token(getattr(timeline_ref, "fps", ""))
                    if manifest_fps and timeline_fps:
                        format_matches = format_matches and manifest_fps == timeline_fps
                if format_matches:
                    if manifest.render_fingerprint and not manifest_is_shared and (
                        manifest.render_fingerprint
                        != current_fingerprint
                    ):
                        return STATUS_UNCACHED
                    return STATUS_CACHED
                return STATUS_UNCACHED
        return STATUS_CACHED if _motion_graphic_range_cached(asset, clip) else STATUS_UNCACHED

    if manifest is None:
        return STATUS_UNCACHED
    if isinstance(manifest, dict):
        manifest = RenderCacheManifest.from_dict(manifest)
    if not isinstance(manifest, RenderCacheManifest):
        return STATUS_UNCACHED
    if manifest.status != RenderCacheStatus.VALID:
        return STATUS_UNCACHED
    if manifest.path and not os.path.exists(manifest.path):
        return STATUS_UNCACHED
    if manifest.render_fingerprint and manifest.render_fingerprint != current_fingerprint:
        return STATUS_UNCACHED
    # P2-17 避免误命中：composite 类命中必须校验时间线输出契约（MG 分支
    # 早已有同等校验）。时间线分辨率/帧率变化后，旧录制的缓存不再是同一
    # 输出——继续报绿会误导用户，并在复用链上构成误命中。
    timeline_ref = (getattr(project, "timelines", {}) or {}).get(timeline_id)
    if timeline_ref is not None:
        manifest_size = (
            int(getattr(manifest, "width", 0) or 0),
            int(getattr(manifest, "height", 0) or 0),
        )
        expected_size = (
            int(getattr(timeline_ref, "width", 0) or 0),
            int(getattr(timeline_ref, "height", 0) or 0),
        )
        if (
            all(size > 0 for size in manifest_size)
            and all(size > 0 for size in expected_size)
            and manifest_size != expected_size
        ):
            return STATUS_UNCACHED
        manifest_fps = _fps_token(getattr(manifest, "timeline_fps", ""))
        timeline_fps = _fps_token(getattr(timeline_ref, "fps", ""))
        if manifest_fps and timeline_fps and manifest_fps != timeline_fps:
            return STATUS_UNCACHED
    return STATUS_CACHED


def build_cache_indicator_segments(timeline: Any, project: Any) -> tuple[CacheIndicatorSegment, ...]:
    """Aggregate cache-required clips across tracks into red/green ranges."""
    if timeline is None or project is None:
        return ()

    # AUD-17614: one single-pass manifest index serves the whole round —
    # built lazily so plain-source timelines (no cache-required clip) never
    # pay the from_dict cost of deserializing manifests at all.
    round_index: _ManifestRoundIndex | None = None

    events: dict[int, list[tuple[str, int]]] = {}
    for track in getattr(timeline, "all_tracks", ()):
        for clip in getattr(track, "clips", ()):
            start = max(0, int(getattr(clip, "timeline_start", 0)))
            end = max(start, int(getattr(clip, "timeline_end", start)))
            if end <= start:
                continue
            if round_index is None and clip_requires_render_cache(
                clip, _asset_for_clip(project, clip)
            ):
                round_index = _ManifestRoundIndex(
                    getattr(project, "render_cache_manifests", {}) or {})
            status = cache_status_for_clip(
                project, getattr(timeline, "timeline_id", ""), clip,
                index=round_index,
            )
            if status is None:
                continue
            events.setdefault(start, []).append((status, 1))
            events.setdefault(end, []).append((status, -1))

    if not events:
        return ()

    active = {STATUS_CACHED: 0, STATUS_UNCACHED: 0}
    segments: list[CacheIndicatorSegment] = []
    previous: int | None = None
    for frame in sorted(events):
        if previous is not None and frame > previous:
            status = (
                STATUS_UNCACHED if active[STATUS_UNCACHED] > 0
                else STATUS_CACHED if active[STATUS_CACHED] > 0
                else None
            )
            if status is not None:
                if segments and segments[-1].end_frame == previous and segments[-1].status == status:
                    segments[-1] = CacheIndicatorSegment(
                        segments[-1].start_frame, frame, status
                    )
                else:
                    segments.append(CacheIndicatorSegment(previous, frame, status))
        for status, delta in events[frame]:
            active[status] += delta
        previous = frame
    return tuple(segments)


__all__ = [
    "STATUS_CACHED",
    "STATUS_UNCACHED",
    "CacheIndicatorSegment",
    "clip_requires_render_cache",
    "cache_status_for_clip",
    "build_cache_indicator_segments",
]
