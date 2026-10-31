"""Render Cache data models for Comp Clips."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping
import hashlib
import json
import os
import uuid

from src.shared.media.motion_graphic_delivery import MOTION_GRAPHIC_DEFAULT_QUALITY


@dataclass(frozen=True)
class RenderCacheSettings:
    """Project-level render cache settings."""
    # Topos 422 Proxy is intra-frame and is decoded through the same planar
    # path as the timeline's professional media. It is the safe default for
    # CPU-baked effects/transitions when a frame cannot stay on the GPU.
    codec: str = "topos_proxy"
    pixel_format: str = "yuv422p10le"
    resolution_mode: str = "timeline"
    custom_width: int | None = None
    custom_height: int | None = None
    quality: str = "preview"
    motion_graphic_quality: str = MOTION_GRAPHIC_DEFAULT_QUALITY
    cache_root: str = "Cache"
    auto_mode: str = "off"  # off, smart, user
    max_size_gb: float = 50.0
    # Container is kept separate from codec so a project can use the same
    # codec family with a different delivery/container policy.
    container: str = "mov"

    def to_dict(self) -> dict[str, Any]:
        return {
            "codec": self.codec,
            "pixel_format": self.pixel_format,
            "resolution_mode": self.resolution_mode,
            "custom_width": self.custom_width,
            "custom_height": self.custom_height,
            "quality": self.quality,
            "motion_graphic_quality": self.motion_graphic_quality,
            "cache_root": self.cache_root,
            "auto_mode": self.auto_mode,
            "max_size_gb": self.max_size_gb,
            "container": self.container,
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> RenderCacheSettings:
        return RenderCacheSettings(
            codec=data.get("codec", "topos_proxy"),
            pixel_format=data.get("pixel_format", "yuv422p10le"),
            resolution_mode=data.get("resolution_mode", "timeline"),
            custom_width=data.get("custom_width"),
            custom_height=data.get("custom_height"),
            quality=data.get("quality", "preview"),
            motion_graphic_quality=data.get(
                "motion_graphic_quality", MOTION_GRAPHIC_DEFAULT_QUALITY),
            cache_root=data.get("cache_root", "Cache"),
            auto_mode=data.get("auto_mode", "off"),
            max_size_gb=data.get("max_size_gb", 50.0),
            container=data.get("container", "mov"),
        )

    @property
    def file_extension(self) -> str:
        """Return the normalized extension used for new cache files."""
        container = str(self.container or "mp4").lower().lstrip(".")
        return container if container in {"mov", "mp4", "m4v", "webm"} else "mp4"


class RenderCacheStatus:
    """Status of a clip's render cache."""
    VALID = "valid"         # Cache is up-to-date
    STALE = "stale"         # Cache needs re-render
    RENDERING = "rendering" # Currently rendering
    FAILED = "failed"       # Render failed
    MISSING = "missing"     # No cache exists


def _fingerprint_value(value: Any) -> Any:
    """Convert project/model values into deterministic JSON data."""
    if isinstance(value, Mapping):
        return {
            str(key): _fingerprint_value(value[key])
            for key in sorted(value, key=str)
        }
    if isinstance(value, (tuple, list)):
        return [_fingerprint_value(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return sorted((_fingerprint_value(item) for item in value), key=repr)
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        try:
            return _fingerprint_value(to_dict())
        except Exception:  # noqa: BLE001 — fingerprints must never block editing
            return repr(value)
    enum_value = getattr(value, "value", None)
    if isinstance(enum_value, (str, int, float, bool)):
        return enum_value
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return repr(value)


def _asset_version(asset: Any | None) -> dict[str, Any]:
    """Return source identity without hashing the full media file."""
    if asset is None:
        return {}
    path = str(getattr(asset, "path", "") or "")
    record: dict[str, Any] = {
        "asset_id": str(getattr(asset, "asset_id", "") or ""),
        "media_type": str(getattr(asset, "media_type", "") or ""),
        "path": path,
        "metadata": _fingerprint_value(getattr(asset, "metadata", {}) or {}),
    }
    if path and not path.startswith("topos://"):
        try:
            stat = os.stat(path)
            record["file_size"] = int(stat.st_size)
            record["file_mtime_ns"] = int(stat.st_mtime_ns)
        except OSError:
            record["file_size"] = -1
            record["file_mtime_ns"] = -1
    return record


def build_render_cache_fingerprint(
    project: Any,
    timeline_id: str,
    clip: Any,
) -> str:
    """Build the semantic identity of a timeline render-cache result.

    The project revision is intentionally not included: it is a global
    transaction counter and would invalidate every clip after an unrelated
    edit.  Instead this fingerprint follows the render dependency graph for
    this clip, which lets unchanged cache entries survive undo/redo and makes
    missed imperative invalidation safe at lookup time.
    """
    timelines = getattr(project, "timelines", {}) or {}
    timeline = timelines.get(str(timeline_id)) if hasattr(timelines, "get") else None
    assets = getattr(project, "assets", {}) or {}
    asset = assets.get(str(getattr(clip, "asset_id", ""))) if hasattr(assets, "get") else None

    graph_id = str(getattr(clip, "composite_id", "") or "")
    compositions = getattr(project, "compositions", {}) or {}
    graph = compositions.get(graph_id) if graph_id and hasattr(compositions, "get") else None
    graph = graph if graph is not None else {}

    # A graph can reference one or more Color nodes independently of the
    # timeline clip's convenience color_stack_id.  Include every referenced
    # stack so changing a node-level grade cannot leave a green cache.
    stack_ids: set[str] = set()
    clip_stack_id = str(getattr(clip, "color_stack_id", "") or "")
    if clip_stack_id:
        stack_ids.add(clip_stack_id)
    if isinstance(graph, Mapping):
        for node in graph.get("nodes", ()) or ():
            if not isinstance(node, Mapping):
                continue
            node_type = str(node.get("type") or node.get("node_type") or "")
            if node_type != "topos_color":
                continue
            params = node.get("parameters") or node.get("data") or {}
            if isinstance(params, Mapping) and params.get("color_stack_id"):
                stack_ids.add(str(params["color_stack_id"]))
    # F4.1: 调色链段栈（组前/片段微调/组后）——漏收即"绿缓存"（组 look
    # 变更后缓存不失效）。主栈 id 上方已收集；此处补链段。
    try:
        from src.features.edit.core.color_chain import chain_stack_ids
        stack_ids.update(
            chain_stack_ids(project, str(timeline_id), clip))
    except Exception:  # noqa: BLE001 — 链解析失败不阻断指纹
        pass
    color_stacks = getattr(project, "color_stacks", {}) or {}
    stack_payload = {
        stack_id: _fingerprint_value(color_stacks.get(stack_id))
        for stack_id in sorted(stack_ids)
        if hasattr(color_stacks, "get") and color_stacks.get(stack_id) is not None
    }

    clip_payload = _fingerprint_value(clip)
    timeline_payload = {
        "timeline_id": str(timeline_id),
        "fps": _fingerprint_value(getattr(timeline, "fps", None)),
        "width": int(getattr(timeline, "width", 0) or 0),
        "height": int(getattr(timeline, "height", 0) or 0),
        "format_settings": _fingerprint_value(
            getattr(timeline, "format_settings", {}) or {}
        ),
    }
    settings = getattr(project, "get_render_cache_settings", None)
    settings_value = settings() if callable(settings) else getattr(
        project, "render_cache_settings", None
    )
    # M04.3：素材解释覆盖（fps/旋转/SAR/场序）——改变解释即改变渲染
    # 语义；漏收 = 改覆盖后缓存仍绿（陈旧像素）。空覆盖 = 稳定空串，
    # 无覆盖工程指纹不变。
    try:
        interpretation_override = (
            getattr(project, "interpretation_overrides", None) or {}
        ).get(str(getattr(clip, "asset_id", "")))
        interpretation_value = (
            interpretation_override.to_dict()
            if hasattr(interpretation_override, "to_dict")
            else (interpretation_override if interpretation_override
                  is not None else ""))
    except Exception:  # noqa: BLE001 — 覆盖读取失败不阻断指纹
        interpretation_value = ""
    payload = {
        "schema": 1,
        "timeline": timeline_payload,
        "clip": clip_payload,
        "graph_id": graph_id,
        "graph": _fingerprint_value(graph),
        "color_stacks": stack_payload,
        "asset": _asset_version(asset),
        "cache_settings": _fingerprint_value(settings_value),
        "interpretation_override": _fingerprint_value(interpretation_value),
    }
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class RenderCacheManifest:
    """Manifest for a single clip's render cache."""
    cache_id: str = ""
    timeline_id: str = ""
    clip_id: str = ""
    composite_id: str = ""
    color_stack_revision: int = 0
    composite_graph_revision: int = 0
    source_asset_revision: str = ""
    # FX-H02: effect-chain signature (build_effects_signature). When the
    # clip's effect chain changes (add/remove/reorder/param/keyframe/toggle),
    # the signature changes and a previously-VALID cache must be treated as
    # STALE so the user never sees the old (pre-effect) frame. Empty by
    # default for backwards compatibility with pre-FX-H02 save files.
    effect_signature: str = ""
    # P2-18: dependency-graph fingerprint. Empty means a legacy manifest that
    # predates semantic lookup validation and remains backward compatible.
    render_fingerprint: str = ""
    timeline_fps: str = "24000/1001"
    start_frame: int = 0
    duration_frames: int = 0
    path: str = ""
    codec: str = "topos_proxy"
    width: int = 1920
    height: int = 1080
    status: str = RenderCacheStatus.MISSING
    container: str = "mov"
    render_kind: str = "composite"  # composite | motion_graphic
    scene_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "cache_id": self.cache_id,
            "timeline_id": self.timeline_id,
            "clip_id": self.clip_id,
            "composite_id": self.composite_id,
            "color_stack_revision": self.color_stack_revision,
            "composite_graph_revision": self.composite_graph_revision,
            "source_asset_revision": self.source_asset_revision,
            "effect_signature": self.effect_signature,
            "render_fingerprint": self.render_fingerprint,
            "timeline_fps": self.timeline_fps,
            "start_frame": self.start_frame,
            "duration_frames": self.duration_frames,
            "path": self.path,
            "codec": self.codec,
            "width": self.width,
            "height": self.height,
            "status": self.status,
            "container": self.container,
            "render_kind": self.render_kind,
            "scene_id": self.scene_id,
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> RenderCacheManifest:
        path = str(data.get("path", "") or "")
        # Legacy manifests predate explicit codec/container fields.  Infer
        # their original ProRes/MOV convention from the stored path instead
        # of silently reinterpreting an old cache as H.264/MP4.
        legacy_mov = path.lower().endswith((".mov", ".qt"))
        return RenderCacheManifest(
            cache_id=data.get("cache_id", ""),
            timeline_id=data.get("timeline_id", ""),
            clip_id=data.get("clip_id", ""),
            composite_id=data.get("composite_id", ""),
            color_stack_revision=data.get("color_stack_revision", 0),
            composite_graph_revision=data.get("composite_graph_revision", 0),
            source_asset_revision=data.get("source_asset_revision", ""),
            effect_signature=data.get("effect_signature", ""),
            render_fingerprint=data.get("render_fingerprint", ""),
            timeline_fps=data.get("timeline_fps", "24000/1001"),
            start_frame=data.get("start_frame", 0),
            duration_frames=data.get("duration_frames", 0),
            path=path,
            codec=data.get("codec") or ("prores_422_proxy" if legacy_mov else "h264_proxy"),
            width=data.get("width", 1920),
            height=data.get("height", 1080),
            status=data.get("status", RenderCacheStatus.MISSING),
            container=data.get("container") or ("mov" if legacy_mov else "mp4"),
            render_kind=data.get("render_kind", "composite"),
            scene_id=data.get("scene_id", ""),
        )

    @staticmethod
    def create_new(timeline_id: str, clip_id: str, composite_id: str,
                   start_frame: int, duration_frames: int,
                   width: int, height: int, fps: str,
                   codec: str = "topos_proxy", container: str = "mov",
                   render_kind: str = "composite", scene_id: str = "") -> RenderCacheManifest:
        cache_id = str(uuid.uuid4())
        extension = str(container or "mp4").lower().lstrip(".")
        if extension not in {"mp4", "mov", "m4v", "webm"}:
            extension = "mp4"
        return RenderCacheManifest(
            cache_id=cache_id,
            timeline_id=timeline_id,
            clip_id=clip_id,
            composite_id=composite_id,
            timeline_fps=fps,
            start_frame=start_frame,
            duration_frames=duration_frames,
            path=f"Cache/clip_{clip_id[:8]}_{cache_id[:8]}.{extension}",
            codec=codec,
            width=width,
            height=height,
            status=RenderCacheStatus.MISSING,
            container=extension,
            render_kind=render_kind,
            scene_id=scene_id,
        )

    def is_valid_for(
        self,
        color_stack_revision: int,
        composite_graph_revision: int,
        effect_signature: str | None = None,
        render_fingerprint: str | None = None,
    ) -> bool:
        """Is this cache still valid for the given revisions/signature?

        FX-H02: when ``effect_signature`` is supplied, the cache is valid
        only if its stored signature matches — so any effect mutation
        (add/remove/reorder/param/keyframe/toggle) flips a previously-VALID
        cache to invalid. When None (legacy caller), the effect signature
        is not checked (preserves the old contract).
        """
        if self.status != RenderCacheStatus.VALID:
            return False
        if self.color_stack_revision < color_stack_revision:
            return False
        if self.composite_graph_revision < composite_graph_revision:
            return False
        if effect_signature is not None and self.effect_signature != effect_signature:
            return False
        if (
            render_fingerprint is not None
            and self.render_fingerprint
            and self.render_fingerprint != render_fingerprint
        ):
            return False
        return True
