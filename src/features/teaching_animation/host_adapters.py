"""Edit/Composite host adapters for the Phase 4 manual integration loop."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Callable, Mapping
import threading
import time
import uuid

from .core.contracts import AnimationFrame, RenderContext
from .core.model import MotionGraphicNode, TeachingSceneDocument
from .core.audio_plan import AudioRenderPlan, audio_plan_for_scene
from .render.frame_adapter import FrameAdapter, RgbaFrame
from .scene_store import TeachingSceneStore, get_shared_scene_store


MOTION_GRAPHIC_ASSET_PREFIX = "teaching_animation:"
LEGACY_MOTION_GRAPHIC_ASSET_PREFIX = "motion_graphic:"


def motion_graphic_scene_id(asset_id: Any) -> str | None:
    """Return the shared scene id for either Motion Graphic asset prefix."""
    value = str(asset_id or "")
    for prefix in (MOTION_GRAPHIC_ASSET_PREFIX, LEGACY_MOTION_GRAPHIC_ASSET_PREFIX):
        if value.startswith(prefix):
            scene_id = value[len(prefix):]
            return scene_id or None
    return None


def is_motion_graphic_asset_id(asset_id: Any) -> bool:
    return motion_graphic_scene_id(asset_id) is not None


@dataclass(frozen=True)
class SceneReferenceManifest:
    """Stable reference shared by a timeline Clip and Composite Node."""

    scene_id: str
    scene_schema_version: int
    renderer_version: str = "teaching-animation-svg/0.1"
    asset_ids: tuple[str, ...] = ()
    cache_id: str | None = None
    payload_revision: int = 0
    audio_asset_ids: tuple[str, ...] = ()
    audio_plan_hash: str = ""

    @classmethod
    def from_scene(cls, scene: TeachingSceneDocument, *, cache_id: str | None = None, payload_revision: int = 0) -> "SceneReferenceManifest":
        import hashlib
        import json
        audio_plan = audio_plan_for_scene(scene)
        audio_ids = tuple(clip.asset_ref for clip in audio_plan.clips)
        plan_hash = hashlib.sha256(json.dumps(audio_plan.to_dict(), sort_keys=True).encode()).hexdigest()
        return cls(
            scene.scene_id, scene.schema_version,
            asset_ids=tuple(asset.asset_id for asset in scene.asset_refs),
            cache_id=cache_id, payload_revision=payload_revision,
            audio_asset_ids=audio_ids, audio_plan_hash=plan_hash,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "scene_id": self.scene_id,
            "scene_schema_version": self.scene_schema_version,
            "renderer_version": self.renderer_version,
            "asset_ids": list(self.asset_ids),
            "cache_id": self.cache_id,
            "payload_revision": self.payload_revision,
            "audio_asset_ids": list(self.audio_asset_ids),
            "audio_plan_hash": self.audio_plan_hash,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "SceneReferenceManifest":
        return cls(
            scene_id=str(data.get("scene_id", "")),
            scene_schema_version=int(data.get("scene_schema_version", 1)),
            renderer_version=str(data.get("renderer_version", "teaching-animation-svg/0.1")),
            asset_ids=tuple(str(item) for item in data.get("asset_ids", ())),
            cache_id=data.get("cache_id"),
            payload_revision=int(data.get("payload_revision", 0)),
            audio_asset_ids=tuple(str(item) for item in data.get("audio_asset_ids", ())),
            audio_plan_hash=str(data.get("audio_plan_hash", "")),
        )


def get_scene_audio_render_plan(scene: TeachingSceneDocument) -> AudioRenderPlan:
    """Host-facing accessor for the immutable scene-owned audio plan."""
    return audio_plan_for_scene(scene)


def map_scene_audio_to_timeline(
    scene: TeachingSceneDocument,
    *,
    timeline_start: int,
    scene_in_frame: int = 0,
    scene_out_frame: int | None = None,
) -> AudioRenderPlan:
    """Map scene audio into Edit timeline coordinates for the host mixer."""
    return audio_plan_for_scene(scene).to_timeline(
        timeline_start=timeline_start,
        scene_in_frame=scene_in_frame,
        scene_out_frame=scene_out_frame,
    )


@dataclass(frozen=True)
class GraphicsClipInsertResult:
    timeline: Any
    clip: Any
    manifest: SceneReferenceManifest
    track_id: str
    created_track: bool = False


def _manifest_block(sequence: Any) -> dict[str, Any]:
    extra = getattr(sequence, "caption_graphics_extra", {}) or {}
    return dict(extra.get("teaching_animation_references", {}))


def _with_manifest(sequence: Any, clip_id: str, manifest: SceneReferenceManifest) -> Any:
    extra = dict(getattr(sequence, "caption_graphics_extra", {}) or {})
    refs = _manifest_block(sequence)
    refs[clip_id] = manifest.to_dict()
    extra["teaching_animation_references"] = refs
    return sequence._rebuild(caption_graphics_extra=extra)


def _without_manifest(sequence: Any, clip_ids: tuple[str, ...] | list[str]) -> Any:
    """Remove scene references for clips that no longer exist."""
    extra = dict(getattr(sequence, "caption_graphics_extra", {}) or {})
    refs = _manifest_block(sequence)
    for clip_id in clip_ids:
        refs.pop(clip_id, None)
    if refs:
        extra["teaching_animation_references"] = refs
    else:
        extra.pop("teaching_animation_references", None)
    return sequence._rebuild(caption_graphics_extra=extra)


def get_scene_reference_manifest(sequence: Any, clip_id: str) -> SceneReferenceManifest | None:
    raw = _manifest_block(sequence).get(clip_id)
    return SceneReferenceManifest.from_dict(raw) if isinstance(raw, Mapping) else None


class MotionGraphicClipCommand:
    """Undoable immutable timeline insertion command."""

    def __init__(self, scene: TeachingSceneDocument, timeline_start: int, timeline_duration: int | None = None,
                 track_id: str | None = None, mode: str = "append",
                 scene_store: TeachingSceneStore | None = None,
                 asset_id: str | None = None) -> None:
        self.command_id = str(uuid.uuid4())
        self.label = "Insert Teaching Animation Clip"
        self.scene = scene
        self.timeline_start = timeline_start
        self.timeline_duration = timeline_duration
        self.track_id = track_id
        self.mode = mode
        self.scene_store = scene_store or get_shared_scene_store()
        # Keep the old reference as the canonical project asset id.  The
        # optional argument lets external callers migrate to another stable
        # virtual id without changing the command's undo semantics.
        self.asset_id = asset_id or f"{MOTION_GRAPHIC_ASSET_PREFIX}{scene.scene_id}"
        self._clip_id: str | None = None
        self._created_track_id: str | None = None
        self._removed_clips: list[tuple[str, Any, dict[str, Any] | None]] = []

    def apply(self, sequence: Any) -> Any:
        from src.features.edit.core.timeline_v2 import ClipInstance, EditTrack

        self.scene_store.register(self.scene)
        self._removed_clips.clear()
        track = sequence.get_track_by_id(self.track_id) if self.track_id else None
        if track is None:
            track_id = self.track_id or f"teaching_graphics_{uuid.uuid4().hex[:10]}"
            track_index = max((item.index for item in sequence.video_tracks), default=-1) + 1
            track = EditTrack(track_id=track_id, name="Teaching Graphics", track_type="video", index=track_index, track_role="graphics")
            sequence = sequence.with_track_added(track)
            self._created_track_id = track_id
        duration = self.timeline_duration or self.scene.duration_frames
        start = max(0, self.timeline_start)
        if self.mode == "append" and track.clips:
            start = max(start, max(clip.timeline_end for clip in track.clips))
        clip_id = self._clip_id or f"teaching_clip_{uuid.uuid4().hex}"
        clip = ClipInstance(
            clip_id=clip_id,
            asset_id=self.asset_id,
            track_id=track.track_id,
            timeline_start=start,
            timeline_duration=duration,
            source_start=0,
            source_duration=self.scene.duration_frames,
            composite_id=f"teaching_scene:{self.scene.scene_id}",
            render_cache_id=f"scene:{self.scene.scene_id}:r{self.scene_store.revision(self.scene.scene_id)}",
        )
        if self.mode == "overwrite":
            for existing in tuple(track.clips):
                if existing.timeline_start < clip.timeline_end and existing.timeline_end > clip.timeline_start:
                    self._removed_clips.append((track.track_id, existing, _manifest_block(sequence).get(existing.clip_id)))
                    sequence = sequence.with_clip_removed(existing.clip_id)
                    sequence = _without_manifest(sequence, (existing.clip_id,))
        sequence = sequence.with_clip_added(track.track_id, clip)
        # Keep the public manifest backward-compatible.  The Clip carries the
        # revisioned cache identity; the manifest remains a stable scene
        # reference for older project readers.
        sequence = _with_manifest(sequence, clip.clip_id, SceneReferenceManifest.from_scene(self.scene))
        self._clip_id = clip.clip_id
        return sequence

    def undo(self, sequence: Any) -> Any:
        if self._clip_id:
            sequence = sequence.with_clip_removed(self._clip_id)
            sequence = _without_manifest(sequence, (self._clip_id,))
        for track_id, clip, manifest in self._removed_clips:
            if sequence.get_track_by_id(track_id) is not None and sequence.get_clip_by_id(clip.clip_id) is None:
                sequence = sequence.with_clip_added(track_id, clip)
                if manifest is not None:
                    sequence = _with_manifest(sequence, clip.clip_id, SceneReferenceManifest.from_dict(manifest))
        if self._created_track_id and sequence.get_track_by_id(self._created_track_id) is not None:
            track = sequence.get_track_by_id(self._created_track_id)
            if track is not None and not track.clips:
                sequence = sequence.with_track_removed(self._created_track_id)
        return sequence

    def to_dict(self) -> dict[str, Any]:
        return {
            "command_id": self.command_id,
            "label": self.label,
            "scene_id": self.scene.scene_id,
            "asset_id": self.asset_id,
            "timeline_start": self.timeline_start,
            "timeline_duration": self.timeline_duration,
            "track_id": self.track_id,
            "mode": self.mode,
        }


class TimelineSceneFrameProvider:
    """Map a host timeline frame to the referenced scene's local frame."""

    def __init__(self, scenes: Mapping[str, TeachingSceneDocument] | None = None) -> None:
        if scenes is None:
            store = get_shared_scene_store()
            self._scenes = {scene.scene_id: scene for scene in store.scenes()}
        else:
            self._scenes = dict(scenes)
        self._scene_store: TeachingSceneStore | None = None
        if scenes is None:
            self._scene_store = get_shared_scene_store()
            self._unsubscribe = self._scene_store.subscribe(self.register_scene)
        else:
            self._unsubscribe = None

    def register_scene(self, scene: TeachingSceneDocument) -> None:
        self._scenes[scene.scene_id] = scene

    def remove_scene(self, scene_id: str) -> None:
        self._scenes.pop(scene_id, None)

    def close(self) -> None:
        if self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None

    def local_frame(self, clip: Any, timeline_frame: int) -> int:
        if not (clip.timeline_start <= timeline_frame < clip.timeline_end):
            raise ValueError("timeline frame is outside graphics clip")
        relative = max(0, int(timeline_frame - clip.timeline_start))
        source_duration = max(1, int(getattr(clip, "source_duration", 1)))
        timeline_duration = max(
            1,
            int(getattr(clip, "timeline_end", clip.timeline_start + source_duration))
            - int(getattr(clip, "timeline_start", 0)),
        )
        # A retimed Motion Graphic still owns the complete authored scene.
        # Normalise the local timeline span to the source span so both the
        # interactive preview and the cache worker use identical frame
        # mapping.  The manifest worker passes a lightweight namespace here,
        # hence this logic intentionally does not require ClipInstance.
        if is_motion_graphic_asset_id(getattr(clip, "asset_id", "")) and timeline_duration != source_duration:
            timeline_span = max(1, timeline_duration - 1)
            source_span = max(0, source_duration - 1)
            mapped = (relative * source_span + timeline_span // 2) // timeline_span
            source_start = int(getattr(clip, "source_start", 0))
            return source_start + min(source_span, mapped)
        return min(source_duration - 1, relative + int(getattr(clip, "source_start", 0)))

    def render_frame(self, clip: Any, timeline_frame: int, context: RenderContext | None = None) -> RgbaFrame:
        scene_id = motion_graphic_scene_id(getattr(clip, "asset_id", ""))
        if scene_id is None:
            raise KeyError(f"Unsupported Motion Graphic asset id: {clip.asset_id}")
        scene = self._scenes.get(scene_id)
        if scene is None:
            raise KeyError(f"Teaching scene is not registered: {scene_id}")
        local = self.local_frame(clip, timeline_frame)
        if context is None:
            from .render.scene_renderer_router import backend_for_scene
            backend = backend_for_scene(scene)
            context = RenderContext(
                fps=scene.fps, width=scene.width, height=scene.height,
                backend=backend,
                # Normal host playback consumes a scene that has already
                # passed the creation/edit validation gate. Preserve authored
                # uniform intro/outro frames here; explicit validation callers
                # pass a context with this flag disabled.
                allow_uniform_frames=backend == "html",
            )
        from .render.scene_renderer_router import select_scene_renderer
        from .render.cache import build_cache_manifest
        from .scene_cache import get_shared_scene_cache_coordinator
        coordinator = get_shared_scene_cache_coordinator()
        renderer = select_scene_renderer(scene)
        # Build the key before preparing a renderer session.  A cache hit can
        # therefore bypass SVG setup or a browser capture session entirely;
        # the second playback really is a cache delivery path, not a render
        # followed by a discarded result.
        renderer_build = getattr(renderer, "renderer_build", "") or ""
        manifest = build_cache_manifest(
            scene, local, context, renderer_build=renderer_build,
        )
        rendered = coordinator.cache.get(manifest)
        if rendered is not None:
            renderer.cleanup()
            return FrameAdapter.from_rendered_frame(rendered)

        try:
            # prepare() stays inside the try: it can allocate a browser
            # session and still fail partway (render_worker keeps the same
            # prepare/cleanup finally contract), so any exit path releases
            # the partial session instead of leaking it.
            renderer.prepare(scene, context)
            # The first playback request renders the requested frame and puts
            # it into the same deterministic cache used by background render
            # jobs.  Later playback/scrubbing requests resolve the exact same
            # scene hash + context + frame key without invoking the renderer.
            if rendered is None:
                rendered = renderer.render_frame(AnimationFrame(local, context.fps))
                confirmed_build = getattr(renderer, "renderer_build", "") or ""
                if confirmed_build != manifest.renderer_build:
                    manifest = build_cache_manifest(
                        scene, local, context, renderer_build=confirmed_build,
                    )
                coordinator.cache.put(manifest, rendered)
            return FrameAdapter.from_rendered_frame(rendered)
        finally:
            renderer.cleanup()

    def thumbnail(self, clip: Any, context: RenderContext | None = None) -> RgbaFrame:
        return self.render_frame(clip, clip.timeline_start, context)


def render_teaching_scene_frame(
    scene_id: str,
    frame: int,
    *,
    context: RenderContext | None = None,
    scenes: Mapping[str, TeachingSceneDocument] | None = None,
) -> RgbaFrame:
    """Render a shared scene reference for Edit and Composite consumers."""
    provider = TimelineSceneFrameProvider(scenes)
    scene = provider._scenes.get(scene_id)
    if scene is None:
        provider.close()
        raise KeyError(f"Teaching scene is not registered: {scene_id}")
    clip = type("TeachingSceneClip", (), {
        "asset_id": f"teaching_animation:{scene_id}",
        "timeline_start": 0,
        "timeline_end": scene.duration_frames,
        "source_start": 0,
        "source_duration": scene.duration_frames,
    })()
    try:
        render_context = (
            replace(context, fps=scene.fps)
            if context is not None
            else RenderContext(
                fps=scene.fps, width=scene.width, height=scene.height,
                transparent=True,
                backend="html" if scene.html_layer is not None else "svg",
                allow_uniform_frames=scene.html_layer is not None,
            )
        )
        local_frame = max(0, min(scene.duration_frames - 1, int(frame)))
        return provider.render_frame(clip, local_frame, render_context)
    finally:
        provider.close()


_SCHEDULED_SCENE_FRAME_LOCK = threading.Lock()
_SCHEDULED_SCENE_FRAMES: dict[str, Any] = {}
#: Per-scene ``(attempts, last_attempt_monotonic)``.  Attempts only accumulate
#: when a submitted job finished without landing a usable frame (a browser
#: service failure); a successful static-frame lookup resets them.
_SCENE_FRAME_ATTEMPTS: dict[str, tuple[int, float]] = {}
_SCENE_FRAME_RETRY_CAP = 3
_SCENE_FRAME_RETRY_WINDOW_SECONDS = 60.0


def reset_teaching_scene_frame_schedule(scene_id: str) -> None:
    """Forget scheduling state for ``scene_id`` after a successful lookup."""
    with _SCHEDULED_SCENE_FRAME_LOCK:
        _SCHEDULED_SCENE_FRAMES.pop(scene_id, None)
        _SCENE_FRAME_ATTEMPTS.pop(scene_id, None)


def schedule_teaching_scene_frame(
    scene_id: str,
    frame: int,
    *,
    context: RenderContext | None = None,
    scenes: Mapping[str, TeachingSceneDocument] | None = None,
) -> bool:
    """Render one scene frame on the background teaching-render thread.

    Playback's static-frame fallback calls this when neither the disk render
    cache nor the memory frame cache can supply a poster frame.  The render
    runs on the shared coordinator worker so the preview render thread is
    never blocked by a browser capture.  Returns ``True`` when a new job was
    submitted; ``False`` when one is already in flight or retries are capped.
    """
    scene = (
        scenes.get(scene_id)
        if scenes is not None
        else get_shared_scene_store().get(scene_id)
    )
    if scene is None:
        return False

    from .scene_cache import get_shared_scene_cache_coordinator

    with _SCHEDULED_SCENE_FRAME_LOCK:
        pending = _SCHEDULED_SCENE_FRAMES.get(scene_id)
        if pending is not None and not pending.done:
            return False
        now = time.monotonic()
        attempts, last_attempt = _SCENE_FRAME_ATTEMPTS.get(scene_id, (0, 0.0))
        if now - last_attempt >= _SCENE_FRAME_RETRY_WINDOW_SECONDS:
            attempts = 0
        if attempts >= _SCENE_FRAME_RETRY_CAP:
            return False
        if context is None:
            from .render.scene_renderer_router import backend_for_scene
            render_context = RenderContext(
                fps=scene.fps, width=scene.width, height=scene.height,
                transparent=True,
                backend=backend_for_scene(scene),
                allow_uniform_frames=scene.html_layer is not None,
            )
        else:
            render_context = context
        local_frame = max(0, min(int(scene.duration_frames) - 1, int(frame)))
        handle = get_shared_scene_cache_coordinator().worker.submit(
            scene, (local_frame,), render_context,
        )
        _SCHEDULED_SCENE_FRAMES[scene_id] = handle
        _SCENE_FRAME_ATTEMPTS[scene_id] = (attempts + 1, now)
        return True


@dataclass(frozen=True)
class MotionGraphicNodeDescriptor:
    node_type: str = "motion_graphic"
    display_name: str = "Motion Graphic"
    category: str = "Teaching"
    inputs: tuple[str, ...] = ()
    outputs: tuple[str, ...] = ("rgba",)

    def to_dict(self) -> dict[str, Any]:
        return {"node_type": self.node_type, "display_name": self.display_name, "category": self.category, "inputs": list(self.inputs), "outputs": list(self.outputs)}


class MotionGraphicNodeProvider:
    provider_id = "teaching_animation"
    descriptor = MotionGraphicNodeDescriptor()

    def get_context(self) -> Mapping[str, Any]:
        return {"provider_id": self.provider_id, "node_types": (self.descriptor.node_type,), "status": "available"}

    def create_node(self, scene: TeachingSceneDocument, node_id: str | None = None) -> MotionGraphicNode:
        return MotionGraphicNode(node_id=node_id or f"motion_graphic_{uuid.uuid4().hex}", scene_id=scene.scene_id, template_id="custom")

    def render_node(self, scene: TeachingSceneDocument, frame: int, context: RenderContext | None = None) -> RgbaFrame:
        from types import SimpleNamespace
        clip = SimpleNamespace(asset_id=f"teaching_animation:{scene.scene_id}", timeline_start=0, timeline_end=scene.duration_frames, source_start=0, source_duration=scene.duration_frames)
        return TimelineSceneFrameProvider({scene.scene_id: scene}).render_frame(clip, frame, context)


class SceneChangeBus:
    """Transport-neutral scene change signal for Clip, Node and editor clients."""

    def __init__(self) -> None:
        self._listeners: list[Callable[[TeachingSceneDocument], None]] = []

    def subscribe(self, listener: Callable[[TeachingSceneDocument], None]) -> None:
        self._listeners.append(listener)

    def publish(self, scene: TeachingSceneDocument) -> None:
        for listener in tuple(self._listeners):
            listener(scene)


def get_scene_store() -> TeachingSceneStore:
    """Return the shared scene repository used by both host entry points."""
    return get_shared_scene_store()


def create_motion_graphic_node_provider() -> MotionGraphicNodeProvider:
    return MotionGraphicNodeProvider()


def insert_motion_graphic_clip(sequence: Any, scene: TeachingSceneDocument, *, timeline_start: int = 0,
                               timeline_duration: int | None = None, track_id: str | None = None,
                               mode: str = "append", command_stack: Any = None) -> GraphicsClipInsertResult:
    """Insert a graphics clip, optionally through the host Edit undo stack."""
    command = MotionGraphicClipCommand(scene, timeline_start, timeline_duration, track_id, mode)
    if command_stack is None:
        updated = command.apply(sequence)
    else:
        updated = command_stack.execute(command, sequence)
    if command._clip_id is None:
        raise RuntimeError("graphics clip command did not create a clip")
    clip = updated.get_clip_by_id(command._clip_id)
    if clip is None:
        raise RuntimeError("inserted graphics clip cannot be found")
    manifest = get_scene_reference_manifest(updated, clip.clip_id)
    if manifest is None:
        raise RuntimeError("inserted graphics clip has no scene reference manifest")
    return GraphicsClipInsertResult(updated, clip, manifest, clip.track_id, command._created_track_id is not None)


def open_scene_editor(scene: TeachingSceneDocument, *, registry: Any = None, parent: Any = None,
                      saver: Callable[[TeachingSceneDocument], None] | None = None,
                      insert_callback: Callable[[TeachingSceneDocument], None] | None = None,
                      render_cache_callback: Callable[[TeachingSceneDocument], str] | None = None) -> Any:
    """Open/focus the one editor window for a scene, lazily importing Qt UI."""
    if registry is None:
        from .ui.editor_window_registry import EditorWindowRegistry
        registry = EditorWindowRegistry()
    return registry.open(
        scene, parent=parent, saver=saver, insert_callback=insert_callback,
        render_cache_callback=render_cache_callback,
    )


def open_clip_editor(clip: Any, scenes: Mapping[str, TeachingSceneDocument], *, registry: Any = None,
                     parent: Any = None, saver: Callable[[TeachingSceneDocument], None] | None = None) -> Any:
    """Resolve a Clip's shared scene_id reference and focus its editor."""
    scene_id = motion_graphic_scene_id(getattr(clip, "asset_id", ""))
    if scene_id is None:
        raise KeyError(f"Unsupported Motion Graphic asset id: {clip.asset_id}")
    scene = scenes.get(scene_id)
    if scene is None:
        raise KeyError(f"Teaching scene is not registered: {scene_id}")
    return open_scene_editor(scene, registry=registry, parent=parent, saver=saver)


__all__ = [
    "GraphicsClipInsertResult", "MotionGraphicClipCommand", "MotionGraphicNodeDescriptor",
    "MotionGraphicNodeProvider", "SceneChangeBus", "SceneReferenceManifest",
    "TimelineSceneFrameProvider", "create_motion_graphic_node_provider",
    "get_scene_reference_manifest", "get_scene_store", "insert_motion_graphic_clip", "open_clip_editor", "open_scene_editor",
    "get_scene_audio_render_plan", "map_scene_audio_to_timeline", "render_teaching_scene_frame",
    "MOTION_GRAPHIC_ASSET_PREFIX", "LEGACY_MOTION_GRAPHIC_ASSET_PREFIX",
    "motion_graphic_scene_id", "is_motion_graphic_asset_id",
]
