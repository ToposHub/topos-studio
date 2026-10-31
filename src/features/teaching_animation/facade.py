"""Stable host-facing facade for Teaching Animation.

The facade is intentionally useful even when the optional implementation is
not installed: callers receive explicit unavailable/fallback results instead
of importing private module paths or raising an incidental ImportError.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Protocol

from .core.contracts import (
    AnimationFrame,
    RenderContext,
    RenderError,
    RenderErrorCode,
    SceneJson,
)
from .core.model import (
    TeachingAnimationProjectPayload,
    TeachingSceneDocument,
)
from .core.validator import SceneValidationError, validate_project_payload, validate_scene
from .scene_store import get_shared_scene_store


@dataclass(frozen=True)
class FacadeResult:
    """Transport-neutral result for optional module operations."""

    ok: bool
    value: Any = None
    error: RenderError | None = None

    @classmethod
    def unavailable(cls, operation: str) -> "FacadeResult":
        return cls(
            ok=False,
            error=RenderError(
                code=RenderErrorCode.MISSING_MODULE,
                message=f"Teaching Animation module is unavailable: {operation}",
                recoverable=True,
                fallback_available=True,
            ),
        )


@dataclass(frozen=True)
class MissingModuleFallback:
    """Explicit state for a project that contains unknown teaching payload."""

    module_id: str
    scene_id: str | None
    payload_preserved: bool
    cache_preserved: bool
    message: str = "Teaching Animation module is not installed"


@dataclass(frozen=True)
class OfflineSceneFrameFallback:
    """Explicit timeline state when a scene module is unavailable at render time."""

    scene_id: str
    cache_id: str | None
    cached_frame_available: bool
    message: str = "Teaching Animation scene is offline; showing preserved cache when available"


class TeachingAnimationHost(Protocol):
    """Host operations exposed to AI, Edit and Composite through the facade."""

    def create_scene(self, scene: SceneJson) -> FacadeResult:
        ...

    def preview_frame(self, scene: SceneJson, frame: int, context: RenderContext) -> FacadeResult:
        ...

    def insert_into_timeline(self, scene_id: str, options: Mapping[str, Any] | None = None) -> FacadeResult:
        ...

    def export_scene(self, scene_id: str, options: Mapping[str, Any] | None = None) -> FacadeResult:
        ...


class TeachingAnimationContextProvider(Protocol):
    """P0 AI provider contract; registration is implemented in a later phase."""

    provider_id: str

    def get_context(self) -> Mapping[str, Any]:
        ...


class TeachingAnimationCommandProvider(Protocol):
    """P0 AI command contract; commands must remain reviewable/applyable."""

    provider_id: str

    def dry_run(self, command: str, arguments: Mapping[str, Any]) -> FacadeResult:
        ...


def create_scene(scene: SceneJson) -> FacadeResult:
    """Create a structured scene when the input has the Phase 1 schema.

    The small Phase 0 transport shape remains accepted for compatibility; a
    complete scene document is returned as ``TeachingSceneDocument``.
    """
    if isinstance(scene, TeachingSceneDocument):
        get_shared_scene_store().register(scene)
        return FacadeResult(ok=True, value=scene)
    if not isinstance(scene, dict):
        return FacadeResult(
            ok=False,
            error=RenderError(
                code=RenderErrorCode.INVALID_SCENE,
                message="scene must be a JSON object",
                recoverable=True,
            ),
        )
    if "scene_id" in scene or "elements" in scene:
        try:
            document = TeachingSceneDocument.from_dict(scene)
        except (TypeError, ValueError) as exc:
            return FacadeResult(
                ok=False,
                error=RenderError(
                    code=RenderErrorCode.INVALID_SCENE,
                    message=str(exc),
                    recoverable=True,
                ),
            )
        result = validate_scene(document)
        if not result.is_valid:
            return FacadeResult(
                ok=False,
                error=RenderError(
                    code=RenderErrorCode.INVALID_SCENE,
                    message="; ".join(issue.message for issue in result.errors),
                    recoverable=True,
                ),
            )
        get_shared_scene_store().register(document)
        return FacadeResult(ok=True, value=document)
    return FacadeResult(ok=True, value=dict(scene))


def create_default_scene(name: str = "Teaching Scene") -> TeachingSceneDocument:
    """Create a new independent scene with a unique scene ID."""
    return TeachingSceneDocument.create_default(name=name)


def serialize_scene(scene: TeachingSceneDocument) -> SceneJson:
    """Serialize a scene document at the feature boundary."""
    return scene.to_dict()


def deserialize_scene(data: Mapping[str, Any]) -> TeachingSceneDocument:
    """Deserialize and migrate one scene document."""
    return TeachingSceneDocument.from_dict(data)


def serialize_project_payload(payload: TeachingAnimationProjectPayload) -> dict[str, Any]:
    """Serialize the independent teaching payload for ToposProjectIO."""
    return payload.to_dict()


def deserialize_project_payload(data: Mapping[str, Any]) -> TeachingAnimationProjectPayload:
    """Deserialize and migrate the independent teaching payload."""
    payload = TeachingAnimationProjectPayload.from_dict(data)
    result = validate_project_payload(payload.to_dict())
    if not result.is_valid:
        raise SceneValidationError(result)
    return payload


def preview_frame(scene: SceneJson, frame: int, context: RenderContext) -> FacadeResult:
    """Render a deterministic frame (SVG or HTML) through the public facade."""
    try:
        from .render.scene_renderer_router import select_scene_renderer

        renderer = select_scene_renderer(scene)
        try:
            # prepare() must live inside the try: it can allocate a browser
            # session and still fail partway (asset preflight, token read);
            # cleanup in the same finally releases those partial resources.
            renderer.prepare(scene, context)
            return FacadeResult(ok=True, value=renderer.render_frame(AnimationFrame(frame, context.fps)))
        finally:
            renderer.cleanup()
    except (TypeError, ValueError) as exc:
        return FacadeResult(
            ok=False,
            error=RenderError(
                code=RenderErrorCode.INVALID_SCENE,
                message=str(exc),
                frame=frame,
                recoverable=True,
            ),
        )
    except RuntimeError as exc:
        # HtmlRenderError (and any renderer RuntimeError) is surfaced as a
        # structured backend failure rather than propagating out of the facade.
        from .render.html_scene_renderer_v2 import HtmlRenderError
        if isinstance(exc, HtmlRenderError):
            return FacadeResult(ok=False, error=exc.render_error)
        raise


def insert_into_timeline(scene_id: str, options: Mapping[str, Any] | None = None) -> FacadeResult:
    """Insert a real graphics Clip through the Edit host boundary.

    ``timeline`` is intentionally a runtime-only option.  It is not part of
    the serialized contract and is supplied by the Timeline controller.
    """
    if not scene_id:
        return FacadeResult(
            ok=False,
            error=RenderError(
                code=RenderErrorCode.INVALID_SCENE,
                message="scene_id cannot be empty",
                recoverable=True,
            ),
        )
    values = dict(options or {})
    timeline = values.pop("timeline", None)
    scene = get_shared_scene_store().get(scene_id)
    if scene is None:
        return FacadeResult(ok=False, error=RenderError(code=RenderErrorCode.INVALID_SCENE,
                                                        message=f"scene not found: {scene_id}", recoverable=True))
    if timeline is None:
        return FacadeResult(ok=False, error=RenderError(code=RenderErrorCode.RENDER_FAILED,
                                                        message="timeline is required for insertion", recoverable=True))
    try:
        from .host_adapters import insert_motion_graphic_clip
        result = insert_motion_graphic_clip(
            timeline,
            scene,
            timeline_start=int(values.pop("timeline_start", 0)),
            timeline_duration=values.pop("timeline_duration", None),
            track_id=values.pop("track_id", None),
            mode=str(values.pop("mode", "append")),
        )
        return FacadeResult(ok=True, value=result)
    except (TypeError, ValueError, KeyError, AttributeError) as exc:
        return FacadeResult(ok=False, error=RenderError(code=RenderErrorCode.RENDER_FAILED,
                                                        message=str(exc), recoverable=True))


def export_scene(scene_id: str, options: Mapping[str, Any] | None = None) -> FacadeResult:
    """Export a registered scene through the real worker/export boundary."""
    if not scene_id:
        return FacadeResult(
            ok=False,
            error=RenderError(
                code=RenderErrorCode.INVALID_SCENE,
                message="scene_id cannot be empty",
                recoverable=True,
            ),
        )
    scene = get_shared_scene_store().get(scene_id)
    values = dict(options or {})
    output = values.pop("output", None)
    if scene is None:
        return FacadeResult(ok=False, error=RenderError(code=RenderErrorCode.INVALID_SCENE,
                                                        message=f"scene not found: {scene_id}", recoverable=True))
    if not output:
        return FacadeResult(ok=False, error=RenderError(code=RenderErrorCode.RENDER_FAILED,
                                                        message="output is required for export", recoverable=True))
    return export_scene_document(scene, str(output), values)


def export_scene_document(scene: TeachingSceneDocument, output: str, options: Mapping[str, Any] | None = None) -> FacadeResult:
    """Export one scene through the Phase 6 worker/export boundary."""
    try:
        from .export_service import ExportOptions, TeachingAnimationExportService
        service = TeachingAnimationExportService()
        try:
            result = service.export(scene, output, options=ExportOptions(**dict(options or {})))
        finally:
            service.close()
        return FacadeResult(ok=True, value=result)
    except RuntimeError as exc:
        # HtmlRenderError (HTML capture/backend failure) must surface as a
        # structured RenderError instead of escaping the facade as a raw
        # RuntimeError — same mapping preview_frame applies (AUD-27910).
        from .render.html_scene_renderer_v2 import HtmlRenderError
        if isinstance(exc, HtmlRenderError):
            return FacadeResult(ok=False, error=exc.render_error)
        raise
    except (TypeError, ValueError, OSError) as exc:
        return FacadeResult(ok=False, error=RenderError(code=RenderErrorCode.RENDER_FAILED, message=str(exc), recoverable=True))


def missing_module_fallback(
    scene_id: str | None,
    *,
    payload_preserved: bool = True,
    cache_preserved: bool = True,
) -> MissingModuleFallback:
    """Create the explicit offline state used by project/timeline callers."""
    return MissingModuleFallback(
        module_id="teaching_animation",
        scene_id=scene_id,
        payload_preserved=payload_preserved,
        cache_preserved=cache_preserved,
    )


def offline_scene_frame_fallback(scene_id: str, cache_id: str | None = None, *, cached_frame_available: bool = False) -> OfflineSceneFrameFallback:
    return OfflineSceneFrameFallback(scene_id, cache_id, cached_frame_available)
