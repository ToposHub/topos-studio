"""Immutable scene commands with undo/redo support."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any, Mapping, Protocol
import copy
import uuid

from .model import (
    SceneAnimation,
    SceneElement,
    SceneMarker,
    SceneExpression,
    ScenePage,
    SceneTransform,
    TeachingSceneDocument,
)


class SceneCommand(Protocol):
    command_id: str
    label: str

    def apply(self, scene: TeachingSceneDocument) -> TeachingSceneDocument:
        ...

    def undo(self, scene: TeachingSceneDocument) -> TeachingSceneDocument:
        ...


def _command_id() -> str:
    return str(uuid.uuid4())


@dataclass(frozen=True)
class AddElementCommand:
    element: SceneElement
    index: int | None = None
    command_id: str = field(default_factory=_command_id)
    label: str = "Add Scene Element"

    def apply(self, scene: TeachingSceneDocument) -> TeachingSceneDocument:
        return scene.with_element_added(self.element, self.index)

    def undo(self, scene: TeachingSceneDocument) -> TeachingSceneDocument:
        return scene.with_element_removed(self.element.element_id)


@dataclass(frozen=True)
class RemoveElementCommand:
    element_id: str
    removed_element: SceneElement | None = None
    removed_index: int | None = None
    removed_animations: tuple[SceneAnimation, ...] | None = None
    command_id: str = field(default_factory=_command_id)
    label: str = "Remove Scene Element"

    def apply(self, scene: TeachingSceneDocument) -> TeachingSceneDocument:
        index = next(
            (position for position, element in enumerate(scene.elements)
             if element.element_id == self.element_id),
            None,
        )
        if index is None:
            raise ValueError(f"Element not found: {self.element_id}")
        object.__setattr__(self, "removed_element", scene.elements[index])
        object.__setattr__(self, "removed_index", index)
        # ``with_element_removed`` drops the element's animation tracks with
        # it, so capture them here — undo must restore the whole pre-remove
        # state, not just the element (AUD-27400).
        object.__setattr__(self, "removed_animations", tuple(
            animation for animation in scene.animations
            if animation.element_id == self.element_id))
        return scene.with_element_removed(self.element_id)

    def undo(self, scene: TeachingSceneDocument) -> TeachingSceneDocument:
        if self.removed_element is None or self.removed_index is None:
            raise RuntimeError("remove command has not been applied")
        restored = scene.with_element_added(self.removed_element, self.removed_index)
        # Restore the animation tracks that were removed together with the
        # element; the element must exist first (with_animation_added
        # validates the element_id reference).
        for animation in self.removed_animations or ():
            restored = restored.with_animation_added(animation)
        return restored


_ELEMENT_FIELDS = frozenset({
    "type", "z_index", "visible", "transform", "style", "layout", "content",
    "asset_ref", "template_id", "local_animation_tracks", "page_id",
})


def _normalize_element_update(key: str, value: Any) -> Any:
    if key == "transform" and isinstance(value, Mapping):
        return SceneTransform.from_dict(value)
    if key == "style" and isinstance(value, Mapping):
        return copy.deepcopy(dict(value))
    if key == "layout" and isinstance(value, Mapping):
        return copy.deepcopy(dict(value))
    if key == "local_animation_tracks" and isinstance(value, (list, tuple)):
        return tuple(value)
    return copy.deepcopy(value)


def _validate_element_update_keys(updates: Mapping[str, Any]) -> None:
    """Reject update keys outside the element-field whitelist (AUD-27401/27402).

    Component paths take these keys from persisted scene metadata or caller
    input; without the same whitelist discipline as ``UpdateElementCommand`` a
    stray ``element_id`` silently rewrites element identity and an unknown key
    crashes ``dataclasses.replace`` mid-command.
    """
    unknown = set(updates) - _ELEMENT_FIELDS
    if unknown:
        raise ValueError(f"Unsupported element fields: {sorted(unknown)}")


@dataclass(frozen=True)
class UpdateElementCommand:
    element_id: str
    updates: Mapping[str, Any]
    old_element: SceneElement | None = None
    command_id: str = field(default_factory=_command_id)
    label: str = "Update Scene Element"

    def apply(self, scene: TeachingSceneDocument) -> TeachingSceneDocument:
        element = scene.element_by_id(self.element_id)
        if element is None:
            raise ValueError(f"Element not found: {self.element_id}")
        unknown = set(self.updates) - _ELEMENT_FIELDS
        if unknown:
            raise ValueError(f"Unsupported element fields: {sorted(unknown)}")
        object.__setattr__(self, "old_element", element)
        normalized = {
            key: _normalize_element_update(key, value)
            for key, value in self.updates.items()
        }
        return scene.with_element_updated(replace(element, **normalized))

    def undo(self, scene: TeachingSceneDocument) -> TeachingSceneDocument:
        if self.old_element is None:
            raise RuntimeError("update command has not been applied")
        return scene.with_element_updated(self.old_element)


@dataclass(frozen=True)
class SetSceneExpressionsCommand:
    """Replace scene-owned expressions as one validated undoable edit."""

    expressions: tuple[SceneExpression, ...]
    command_id: str = field(default_factory=_command_id)
    label: str = "Set Scene Expressions"
    _previous: tuple[SceneExpression, ...] | None = None

    def apply(self, scene: TeachingSceneDocument) -> TeachingSceneDocument:
        from ..motion.expression import ExpressionError, validate_expression

        if any(not isinstance(item, SceneExpression) for item in self.expressions):
            raise TypeError("expressions must contain SceneExpression values")
        ids = [item.expression_id for item in self.expressions]
        if len(ids) != len(set(ids)):
            raise ValueError("expression ids must be unique")
        for expression in self.expressions:
            if scene.element_by_id(expression.element_id) is None:
                raise ValueError(f"expression target not found: {expression.element_id}")
            try:
                validate_expression(expression.source)
            except ExpressionError as exc:
                raise ValueError(f"invalid expression {expression.expression_id}: {exc}") from exc
        object.__setattr__(self, "_previous", scene.expressions)
        return replace(scene, expressions=self.expressions)

    def undo(self, scene: TeachingSceneDocument) -> TeachingSceneDocument:
        if self._previous is None:
            raise RuntimeError("expression command has not been applied")
        return replace(scene, expressions=self._previous)


@dataclass(frozen=True)
class SetElementEffectsCommand:
    """Set one element's renderer-neutral effect chain as one undo step."""

    element_id: str
    effects: tuple[Any, ...]
    command_id: str = field(default_factory=_command_id)
    label: str = "Set Element Effects"
    _previous_style: Mapping[str, Any] | None = None

    def apply(self, scene: TeachingSceneDocument) -> TeachingSceneDocument:
        from ..render.effect_capabilities import write_effect_declarations

        element = scene.element_by_id(self.element_id)
        if element is None:
            raise ValueError(f"Element not found: {self.element_id}")
        if any(not hasattr(effect, "to_dict") for effect in self.effects):
            raise TypeError("effects must be EffectDeclaration values")
        object.__setattr__(self, "_previous_style", element.style)
        style = write_effect_declarations(element.style, tuple(self.effects))
        return scene.with_element_updated(replace(element, style=style))

    def undo(self, scene: TeachingSceneDocument) -> TeachingSceneDocument:
        element = scene.element_by_id(self.element_id)
        if element is None:
            raise ValueError(f"Element not found: {self.element_id}")
        if self._previous_style is None:
            raise RuntimeError("effect command has not been applied")
        return scene.with_element_updated(replace(element, style=dict(self._previous_style)))


@dataclass(frozen=True)
class SetSceneGpuEffectsCommand:
    """Set the explicit scene-level Composite effect chain."""

    effects: tuple[Any, ...]
    command_id: str = field(default_factory=_command_id)
    label: str = "Set Scene GPU Effects"
    _previous_metadata: Mapping[str, Any] | None = None

    def apply(self, scene: TeachingSceneDocument) -> TeachingSceneDocument:
        if any(not hasattr(effect, "to_dict") for effect in self.effects):
            raise TypeError("effects must be EffectDeclaration values")
        object.__setattr__(self, "_previous_metadata", scene.metadata)
        metadata = dict(scene.metadata)
        metadata["gpu_effects"] = [effect.to_dict() for effect in self.effects]
        return replace(scene, metadata=metadata)

    def undo(self, scene: TeachingSceneDocument) -> TeachingSceneDocument:
        if self._previous_metadata is None:
            raise RuntimeError("scene GPU effect command has not been applied")
        return replace(scene, metadata=self._previous_metadata)


@dataclass(frozen=True)
class SetAudioRenderPlanCommand:
    """Set a scene-owned audio plan without introducing an audio engine."""

    plan: Any
    command_id: str = field(default_factory=_command_id)
    label: str = "Set Scene Audio Plan"
    _previous_metadata: Mapping[str, Any] | None = None

    def apply(self, scene: TeachingSceneDocument) -> TeachingSceneDocument:
        from .audio_plan import AudioRenderPlan, write_audio_render_plan_into

        if not isinstance(self.plan, AudioRenderPlan):
            raise TypeError("plan must be AudioRenderPlan")
        object.__setattr__(self, "_previous_metadata", scene.metadata)
        return replace(scene, metadata=write_audio_render_plan_into(scene.metadata, self.plan))

    def undo(self, scene: TeachingSceneDocument) -> TeachingSceneDocument:
        if self._previous_metadata is None:
            raise RuntimeError("audio plan command has not been applied")
        return replace(scene, metadata=self._previous_metadata)


@dataclass(frozen=True)
class SetCompositionCommand:
    """Set precomp/mask/matte references as one reversible scene edit."""

    instances: tuple[Any, ...] = ()
    masks: tuple[Any, ...] = ()
    mattes: tuple[Any, ...] = ()
    command_id: str = field(default_factory=_command_id)
    label: str = "Set Scene Composition"
    _previous_metadata: Mapping[str, Any] | None = None

    def apply(self, scene: TeachingSceneDocument) -> TeachingSceneDocument:
        from .composition_schema import Mask, SceneInstance, TrackMatte, write_composition_into

        if not all(isinstance(item, SceneInstance) for item in self.instances):
            raise TypeError("instances must contain SceneInstance values")
        if not all(isinstance(item, Mask) for item in self.masks):
            raise TypeError("masks must contain Mask values")
        if not all(isinstance(item, TrackMatte) for item in self.mattes):
            raise TypeError("mattes must contain TrackMatte values")
        element_ids = {element.element_id for element in scene.elements}
        for mask in self.masks:
            if mask.target not in element_ids and mask.target not in {item.instance_id for item in self.instances}:
                raise ValueError(f"mask target not found: {mask.target}")
        for matte in self.mattes:
            if matte.target not in element_ids or matte.source not in element_ids:
                raise ValueError("track matte target/source must reference scene elements")
        object.__setattr__(self, "_previous_metadata", scene.metadata)
        metadata = write_composition_into(
            scene.metadata,
            instances=tuple(self.instances), masks=tuple(self.masks), mattes=tuple(self.mattes),
        )
        return replace(scene, metadata=metadata)

    def undo(self, scene: TeachingSceneDocument) -> TeachingSceneDocument:
        if self._previous_metadata is None:
            raise RuntimeError("composition command has not been applied")
        return replace(scene, metadata=self._previous_metadata)


@dataclass(frozen=True)
class SetProfessionalCapabilitiesCommand:
    """Set W6 particle and colour-pipeline metadata atomically."""

    pipeline: Any | None = None
    emitters: tuple[Any, ...] | None = None
    command_id: str = field(default_factory=_command_id)
    label: str = "Set Professional Motion Capabilities"
    _previous_metadata: Mapping[str, Any] | None = None

    def apply(self, scene: TeachingSceneDocument) -> TeachingSceneDocument:
        from .professional import ParticleEmitter, SceneColorPipeline, write_particle_emitters_into, write_scene_color_pipeline_into

        if self.pipeline is not None and not isinstance(self.pipeline, SceneColorPipeline):
            raise TypeError("pipeline must be SceneColorPipeline")
        if self.emitters is not None and not all(isinstance(item, ParticleEmitter) for item in self.emitters):
            raise TypeError("emitters must contain ParticleEmitter values")
        object.__setattr__(self, "_previous_metadata", scene.metadata)
        metadata = dict(scene.metadata)
        if self.pipeline is not None:
            metadata = write_scene_color_pipeline_into(metadata, self.pipeline)
        if self.emitters is not None:
            metadata = write_particle_emitters_into(metadata, tuple(self.emitters))
        return replace(scene, metadata=metadata)

    def undo(self, scene: TeachingSceneDocument) -> TeachingSceneDocument:
        if self._previous_metadata is None:
            raise RuntimeError("professional capabilities command has not been applied")
        return replace(scene, metadata=self._previous_metadata)


@dataclass(frozen=True)
class ReorderElementCommand:
    element_id: str
    new_index: int
    old_index: int | None = None
    command_id: str = field(default_factory=_command_id)
    label: str = "Reorder Scene Element"

    def apply(self, scene: TeachingSceneDocument) -> TeachingSceneDocument:
        old_index = next(
            (position for position, element in enumerate(scene.elements)
             if element.element_id == self.element_id),
            None,
        )
        if old_index is None:
            raise ValueError(f"Element not found: {self.element_id}")
        object.__setattr__(self, "old_index", old_index)
        return scene.with_elements_reordered(self.element_id, self.new_index)

    def undo(self, scene: TeachingSceneDocument) -> TeachingSceneDocument:
        if self.old_index is None:
            raise RuntimeError("reorder command has not been applied")
        return scene.with_elements_reordered(self.element_id, self.old_index)


@dataclass(frozen=True)
class InsertSceneFragmentCommand:
    """Graft a preset fragment (elements + inner hierarchy) in one undo step.

    Preset insertion must never leave a half-inserted fragment behind an undo,
    so element additions and the hierarchy merge share one command.  The
    fragment arrives pre-remapped from ``presets.prepare_insert`` — fresh ids,
    drop-point centring, z stacked above the host.
    """

    elements: tuple[SceneElement, ...]
    parents: Mapping[str, str] = field(default_factory=dict)
    command_id: str = field(default_factory=_command_id)
    label: str = "Insert Preset"
    _previous_metadata: Any = None

    def apply(self, scene: TeachingSceneDocument) -> TeachingSceneDocument:
        object.__setattr__(self, "_previous_metadata", None)
        metadata = scene.metadata
        if self.parents:
            object.__setattr__(self, "_previous_metadata", dict(scene.metadata))
            from .hierarchy import Hierarchy, read_hierarchy, write_hierarchy_into
            current = read_hierarchy(scene.metadata)
            merged = dict(current.parents)
            merged.update(dict(self.parents))
            metadata = write_hierarchy_into(
                scene.metadata,
                Hierarchy(parents=merged, locked=current.locked,
                          solo=current.solo, hidden=current.hidden))
        result = scene
        for element in self.elements:
            result = result.with_element_added(element)
        if self.parents:
            result = replace(result, metadata=metadata)
        return result

    def undo(self, scene: TeachingSceneDocument) -> TeachingSceneDocument:
        result = scene
        for element in reversed(self.elements):
            result = result.with_element_removed(element.element_id)
        if self._previous_metadata is not None:
            result = replace(result, metadata=self._previous_metadata)
        return result


@dataclass(frozen=True)
class SetDurationCommand:
    duration_frames: int
    old_duration_frames: int | None = None
    command_id: str = field(default_factory=_command_id)
    label: str = "Set Scene Duration"
    _previous_scene: TeachingSceneDocument | None = None

    def apply(self, scene: TeachingSceneDocument) -> TeachingSceneDocument:
        object.__setattr__(self, "old_duration_frames", scene.duration_frames)
        object.__setattr__(self, "_previous_scene", scene)
        from .pages import resize_scene_pages, retime_page_tracks
        result = resize_scene_pages(scene, self.duration_frames)
        return retime_page_tracks(scene, result)

    def undo(self, scene: TeachingSceneDocument) -> TeachingSceneDocument:
        if self._previous_scene is None:
            raise RuntimeError("duration command has not been applied")
        return self._previous_scene


def _pages_total_frames(scene: TeachingSceneDocument) -> int:
    return sum(page.duration_frames for page in scene.pages)


@dataclass(frozen=True)
class AddPageCommand:
    """Append (or insert) a page; the scene duration grows to cover it."""

    page: ScenePage
    index: int | None = None
    command_id: str = field(default_factory=_command_id)
    label: str = "Add Scene Page"
    old_duration_frames: int | None = None
    _previous_scene: TeachingSceneDocument | None = None

    def apply(self, scene: TeachingSceneDocument) -> TeachingSceneDocument:
        object.__setattr__(self, "old_duration_frames", scene.duration_frames)
        object.__setattr__(self, "_previous_scene", scene)
        from .pages import retime_page_tracks
        result = scene.with_page_added(self.page, self.index)
        result = result.with_duration(_pages_total_frames(result))
        return retime_page_tracks(scene, result)

    def undo(self, scene: TeachingSceneDocument) -> TeachingSceneDocument:
        if self._previous_scene is None:
            raise RuntimeError("page command has not been applied")
        return self._previous_scene


@dataclass(frozen=True)
class RemovePageCommand:
    """Remove a page; its elements fall back to the neighbouring page."""

    page_id: str
    command_id: str = field(default_factory=_command_id)
    label: str = "Remove Scene Page"
    _previous: Any = None
    _previous_scene: TeachingSceneDocument | None = None

    def apply(self, scene: TeachingSceneDocument) -> TeachingSceneDocument:
        object.__setattr__(self, "_previous_scene", scene)
        object.__setattr__(
            self, "_previous",
            (scene.pages, scene.elements, scene.duration_frames),
        )
        from .pages import retime_page_tracks
        result = scene.with_page_removed(self.page_id)
        result = result.with_duration(_pages_total_frames(result))
        return retime_page_tracks(scene, result)

    def undo(self, scene: TeachingSceneDocument) -> TeachingSceneDocument:
        if self._previous_scene is None:
            raise RuntimeError("page command has not been applied")
        return self._previous_scene


@dataclass(frozen=True)
class UpdatePageCommand:
    """Partial update of name/duration/background for one page.

    Changing a page duration re-anchors every later page (starts are
    cumulative), so the scene duration is recomputed from the page sum in the
    same undoable step.
    """

    page_id: str
    updates: Mapping[str, Any]
    command_id: str = field(default_factory=_command_id)
    label: str = "Update Scene Page"
    _previous: Any = None
    _previous_scene: TeachingSceneDocument | None = None

    def apply(self, scene: TeachingSceneDocument) -> TeachingSceneDocument:
        page = scene.page_by_id(self.page_id)
        if page is None:
            raise ValueError(f"Page not found: {self.page_id}")
        unknown = set(self.updates) - {"name", "duration_frames", "background"}
        if unknown:
            raise ValueError(f"Unsupported page fields: {sorted(unknown)}")
        duration = self.updates.get("duration_frames", page.duration_frames)
        new_page = replace(
            page,
            name=str(self.updates.get("name", page.name)),
            duration_frames=max(1, int(duration)),
            background=str(self.updates.get("background", page.background)),
        )
        object.__setattr__(
            self, "_previous", (page, scene.duration_frames),
        )
        object.__setattr__(self, "_previous_scene", scene)
        from .pages import retime_page_tracks
        result = scene.with_page_updated(new_page)
        result = result.with_duration(_pages_total_frames(result))
        return retime_page_tracks(scene, result)

    def undo(self, scene: TeachingSceneDocument) -> TeachingSceneDocument:
        if self._previous_scene is None:
            raise RuntimeError("page command has not been applied")
        return self._previous_scene


@dataclass(frozen=True)
class ReorderPageCommand:
    """Move one page within the page order (timing spans follow the order)."""

    page_id: str
    index: int
    old_index: int | None = None
    command_id: str = field(default_factory=_command_id)
    label: str = "Reorder Scene Page"
    _previous_scene: TeachingSceneDocument | None = None

    def apply(self, scene: TeachingSceneDocument) -> TeachingSceneDocument:
        old_index = next(
            (position for position, page in enumerate(scene.pages)
             if page.page_id == self.page_id),
            None,
        )
        if old_index is None:
            raise ValueError(f"Page not found: {self.page_id}")
        object.__setattr__(self, "old_index", old_index)
        object.__setattr__(self, "_previous_scene", scene)
        from .pages import retime_page_tracks
        result = scene.with_pages_reordered(self.page_id, self.index)
        return retime_page_tracks(scene, result)

    def undo(self, scene: TeachingSceneDocument) -> TeachingSceneDocument:
        if self._previous_scene is None:
            raise RuntimeError("page command has not been applied")
        return self._previous_scene


@dataclass(frozen=True)
class MoveElementsToPageCommand:
    """Re-page elements; frames drag their hierarchy children along.

    The whole move is one undo step and lands every affected element on the
    target page, so a frame never strands its children on the old page.
    """

    element_ids: tuple[str, ...]
    target_page_id: str
    command_id: str = field(default_factory=_command_id)
    label: str = "Move Elements to Page"
    _moves: tuple[tuple[str, str], ...] | None = None
    _previous_scene: TeachingSceneDocument | None = None

    def apply(self, scene: TeachingSceneDocument) -> TeachingSceneDocument:
        if scene.page_by_id(self.target_page_id) is None:
            raise ValueError(f"Page not found: {self.target_page_id}")
        from .hierarchy import read_hierarchy

        hierarchy = read_hierarchy(scene.metadata)
        ordered: list[str] = []
        seen: set[str] = set()

        def visit(element_id: str) -> None:
            if element_id in seen:
                return
            seen.add(element_id)
            ordered.append(element_id)
            for child_id in hierarchy.children_of(element_id):
                visit(child_id)

        for element_id in self.element_ids:
            if scene.element_by_id(element_id) is None:
                raise ValueError(f"Element not found: {element_id}")
            visit(element_id)

        moved_ids = set(ordered)
        for element_id in ordered:
            parent_id = hierarchy.parent_of(element_id)
            parent = scene.element_by_id(parent_id) if parent_id else None
            if (
                parent is not None
                and parent_id not in moved_ids
                and parent.page_id != self.target_page_id
            ):
                raise ValueError(
                    "Move the parent element with its child before changing pages"
                )

        moves: list[tuple[str, str]] = []
        object.__setattr__(self, "_previous_scene", scene)
        result = scene
        for element_id in ordered:
            element = result.element_by_id(element_id)
            if element is None or element.page_id == self.target_page_id:
                continue
            moves.append((element_id, element.page_id))
            result = result.with_element_updated(
                replace(element, page_id=self.target_page_id))
        object.__setattr__(self, "_moves", tuple(moves))
        from .pages import retime_page_tracks
        return retime_page_tracks(scene, result)

    def undo(self, scene: TeachingSceneDocument) -> TeachingSceneDocument:
        if self._previous_scene is None:
            raise RuntimeError("move command has not been applied")
        return self._previous_scene


@dataclass(frozen=True)
class SetCanvasSizeCommand:
    """R1: switch the live canvas (breakpoint / quick ratio preview).

    The first size change records the authored canvas as the responsive base
    (``metadata["responsive_base_canvas"]``) so element constraints keep
    resolving against the canvas the layout was designed on.  Fully undoable:
    undo restores both the previous size and the previous metadata.
    """

    width: int
    height: int
    command_id: str = field(default_factory=_command_id)
    label: str = "Set Canvas Size"
    _previous: tuple[int, int, Mapping[str, Any] | None] | None = None

    def apply(self, scene: TeachingSceneDocument) -> TeachingSceneDocument:
        from .constraints import BASE_CANVAS_KEY, write_base_canvas_into

        metadata = scene.metadata
        base = metadata.get(BASE_CANVAS_KEY) if isinstance(metadata, Mapping) else None
        if not isinstance(base, Mapping):
            metadata = write_base_canvas_into(metadata, scene.width, scene.height)
        object.__setattr__(self, "_previous", (scene.width, scene.height, scene.metadata))
        return replace(scene, width=int(self.width), height=int(self.height),
                       metadata=metadata)

    def undo(self, scene: TeachingSceneDocument) -> TeachingSceneDocument:
        if self._previous is None:
            raise RuntimeError("canvas size command has not been applied")
        width, height, metadata = self._previous
        return replace(scene, width=width, height=height, metadata=metadata)


@dataclass(frozen=True)
class AddAnimationCommand:
    animation: SceneAnimation
    command_id: str = field(default_factory=_command_id)
    label: str = "Add Scene Animation"

    def apply(self, scene: TeachingSceneDocument) -> TeachingSceneDocument:
        return scene.with_animation_added(self.animation)

    def undo(self, scene: TeachingSceneDocument) -> TeachingSceneDocument:
        animations = tuple(
            item for item in scene.animations
            if item.animation_id != self.animation.animation_id
        )
        if len(animations) == len(scene.animations):
            raise ValueError(f"Animation not found: {self.animation.animation_id}")
        return replace(scene, animations=animations)


@dataclass(frozen=True)
class UpdateAnimationTrackCommand:
    """Replace one animation's keyframe track as one reversible edit.

    Adding/replacing a keyframe on an existing track previously mutated the
    scene directly, so the edit was invisible to the undo stack and a later
    undo silently dropped those keyframes forever.
    """

    animation_id: str
    track: Any  # KeyframeTrack (shared keyframe model)
    command_id: str = field(default_factory=_command_id)
    label: str = "Update Animation Track"
    _previous: Any = None

    def apply(self, scene: TeachingSceneDocument) -> TeachingSceneDocument:
        object.__setattr__(self, "_previous", None)
        animations: list[SceneAnimation] = []
        found = False
        for item in scene.animations:
            if item.animation_id == self.animation_id:
                object.__setattr__(self, "_previous", item.track)
                animations.append(replace(item, track=self.track))
                found = True
            else:
                animations.append(item)
        if not found:
            raise ValueError(f"Animation not found: {self.animation_id}")
        return replace(scene, animations=tuple(animations))

    def undo(self, scene: TeachingSceneDocument) -> TeachingSceneDocument:
        if self._previous is None:
            raise RuntimeError("track command has not been applied")
        animations = tuple(
            replace(item, track=self._previous) if item.animation_id == self.animation_id else item
            for item in scene.animations
        )
        return replace(scene, animations=animations)


@dataclass(frozen=True)
class RemoveAnimationCommand:
    """Remove one element animation as a reversible editor operation.

    Deleting the last keyframe of a track removes the whole ``SceneAnimation``
    (an empty track would fail document validation); this command keeps that
    step undoable by capturing the animation when it applies.
    """

    animation_id: str
    removed: SceneAnimation | None = None
    command_id: str = field(default_factory=_command_id)
    label: str = "Remove Scene Animation"

    def apply(self, scene: TeachingSceneDocument) -> TeachingSceneDocument:
        animation = next(
            (item for item in scene.animations if item.animation_id == self.animation_id), None)
        if animation is None:
            raise ValueError(f"Animation not found: {self.animation_id}")
        object.__setattr__(self, "removed", animation)
        return replace(scene, animations=tuple(
            item for item in scene.animations if item.animation_id != self.animation_id))

    def undo(self, scene: TeachingSceneDocument) -> TeachingSceneDocument:
        if self.removed is None:
            raise RuntimeError("remove animation command has not been applied")
        return scene.with_animation_added(self.removed)


@dataclass(frozen=True)
class AddMarkerCommand:
    marker: SceneMarker
    command_id: str = field(default_factory=_command_id)
    label: str = "Add Scene Marker"

    def apply(self, scene: TeachingSceneDocument) -> TeachingSceneDocument:
        return scene.with_marker_added(self.marker)

    def undo(self, scene: TeachingSceneDocument) -> TeachingSceneDocument:
        return scene.with_marker_removed(self.marker.marker_id)


@dataclass(frozen=True)
class RemoveMarkerCommand:
    """Remove one named timeline marker as an undoable editor operation."""

    marker_id: str
    removed_marker: SceneMarker | None = None
    command_id: str = field(default_factory=_command_id)
    label: str = "Remove Scene Marker"

    def apply(self, scene: TeachingSceneDocument) -> TeachingSceneDocument:
        marker = next((item for item in scene.markers if item.marker_id == self.marker_id), None)
        if marker is None:
            raise ValueError(f"Marker not found: {self.marker_id}")
        object.__setattr__(self, "removed_marker", marker)
        return scene.with_marker_removed(self.marker_id)

    def undo(self, scene: TeachingSceneDocument) -> TeachingSceneDocument:
        if self.removed_marker is None:
            raise RuntimeError("remove marker command has not been applied")
        return scene.with_marker_added(self.removed_marker)


@dataclass(frozen=True)
class RenameMarkerCommand:
    """Rename a timeline marker without changing its timing or identity."""

    marker_id: str
    label_text: str
    previous_label: str | None = None
    command_id: str = field(default_factory=_command_id)
    label: str = "Rename Scene Marker"

    def apply(self, scene: TeachingSceneDocument) -> TeachingSceneDocument:
        marker = next((item for item in scene.markers if item.marker_id == self.marker_id), None)
        if marker is None:
            raise ValueError(f"Marker not found: {self.marker_id}")
        object.__setattr__(self, "previous_label", marker.label)
        renamed = replace(marker, label=str(self.label_text).strip() or marker.label)
        return replace(scene, markers=tuple(
            renamed if item.marker_id == self.marker_id else item for item in scene.markers
        ))

    def undo(self, scene: TeachingSceneDocument) -> TeachingSceneDocument:
        if self.previous_label is None:
            raise RuntimeError("rename marker command has not been applied")
        return replace(scene, markers=tuple(
            replace(item, label=self.previous_label) if item.marker_id == self.marker_id else item
            for item in scene.markers
        ))


@dataclass(frozen=True)
class DetachComponentInstanceCommand:
    """Turn one component instance into plain, self-contained elements (WP-6).

    The instance's overrides are baked into the referenced elements so the
    visual result does not change, and the instance record is removed from
    metadata — future edits no longer follow the component master.  Undo
    restores both the elements and the instance link.
    """

    instance_id: str
    command_id: str = field(default_factory=_command_id)
    label: str = "Detach Component Instance"
    _previous_elements: Any = None
    _previous_metadata: Any = None

    def apply(self, scene: TeachingSceneDocument) -> TeachingSceneDocument:
        from .components import read_component_instances, write_component_state_into
        instances = read_component_instances(scene.metadata)
        target = next((i for i in instances if i.instance_id == self.instance_id), None)
        if target is None:
            raise ValueError(f"component instance not found: {self.instance_id}")
        object.__setattr__(self, "_previous_elements", scene.elements)
        object.__setattr__(self, "_previous_metadata", scene.metadata)
        overrides = dict(target.overrides)
        elements = list(scene.elements)
        from ..render.evaluator import evaluate  # local import avoids a cycle at module load
        evaluated = {item.source.element_id: item for item in evaluate(scene, 0).elements}
        updated: list[SceneElement] = []
        for element in elements:
            if element.element_id in target.element_ids:
                update = overrides.get(element.element_id, {})
                if isinstance(update, Mapping):
                    # AUD-27401: the overrides come from persisted scene
                    # metadata (external data) — validate keys against the
                    # element-field whitelist before handing them to
                    # ``dataclasses.replace``.
                    _validate_element_update_keys(update)
                    from copy import deepcopy
                    patch = {key: deepcopy(value) for key, value in update.items()}
                    if "style" in patch and isinstance(patch["style"], Mapping):
                        patch["style"] = {**dict(element.style), **dict(patch["style"])}
                    element = replace(element, **patch)
                # Bake the currently resolved master/variant state too, so the
                # detached node keeps exactly what the user sees.
                resolved = evaluated.get(element.element_id)
                if resolved is not None:
                    element = replace(
                        element,
                        transform=resolved.transform,
                        style={**dict(resolved.style), "opacity": element.style.get("opacity", 1.0)},
                    )
            updated.append(element)
        remaining = tuple(i for i in instances if i.instance_id != self.instance_id)
        from .components import read_component_masters
        masters = read_component_masters(scene.metadata)
        metadata = write_component_state_into(scene.metadata, masters, remaining)
        return replace(scene, elements=tuple(updated), metadata=metadata)

    def undo(self, scene: TeachingSceneDocument) -> TeachingSceneDocument:
        if self._previous_elements is None:
            raise RuntimeError("detach command has not been applied")
        return replace(scene, elements=self._previous_elements, metadata=self._previous_metadata)


@dataclass(frozen=True)
class SetSceneBackgroundCommand:
    """Replace the document-wide fallback background as one undoable edit."""

    background: Mapping[str, Any]
    command_id: str = field(default_factory=_command_id)
    label: str = "Set Scene Background"
    _previous: Mapping[str, Any] | None = None

    def apply(self, scene: TeachingSceneDocument) -> TeachingSceneDocument:
        if not isinstance(self.background, Mapping):
            raise TypeError("background must be a mapping")
        object.__setattr__(self, "_previous", scene.background)
        return replace(scene, background=dict(self.background))

    def undo(self, scene: TeachingSceneDocument) -> TeachingSceneDocument:
        if self._previous is None:
            raise RuntimeError("scene background command has not been applied")
        return replace(scene, background=self._previous)


@dataclass(frozen=True)
class SetSceneMetadataCommand:
    """Replace scene metadata as one reversible edit (WP-4 theme switching).

    Callers build the next metadata mapping (token sets, brand theme ids,
    hierarchy, component masters...) with the relevant write_*_into helper;
    this command records the previous mapping so undo restores it exactly.
    """

    metadata: Mapping[str, Any]
    command_id: str = field(default_factory=_command_id)
    label: str = "Set Scene Metadata"
    _previous: Any = None

    def apply(self, scene: TeachingSceneDocument) -> TeachingSceneDocument:
        if not isinstance(self.metadata, Mapping):
            raise TypeError("metadata must be a mapping")
        object.__setattr__(self, "_previous", scene.metadata)
        return replace(scene, metadata=dict(self.metadata))

    def undo(self, scene: TeachingSceneDocument) -> TeachingSceneDocument:
        if self._previous is None:
            raise RuntimeError("metadata command has not been applied")
        return replace(scene, metadata=self._previous)


@dataclass(frozen=True)
class SetHierarchyCommand:
    """Toggle one element's lock / solo / hide flag in the scene Hierarchy (SG-01).

    The Hierarchy (parent links + lock/solo/hide) lives in ``scene.metadata`` so
    it round-trips without a schema bump; this command is the reversible write
    the layer tree speaks.  ``flag`` is one of ``locked`` / ``solo`` / ``hidden``
    (the Hierarchy field names); ``value`` sets membership (True adds, False
    removes), so repeating the same command is idempotent.  Undo restores the
    previous Hierarchy exactly.
    """

    element_id: str
    flag: str                       # "locked" | "solo" | "hidden"
    value: bool
    command_id: str = field(default_factory=_command_id)
    label: str = "Toggle Layer Flag"
    _previous: Any = None           # the prior Hierarchy, recorded on apply

    def __post_init__(self) -> None:
        if self.flag not in {"locked", "solo", "hidden"}:
            raise ValueError(f"SetHierarchyCommand.flag must be locked/solo/hidden, got {self.flag!r}")
        if not self.element_id:
            raise ValueError("element_id cannot be empty")

    def _resolve(self, hierarchy: Any) -> Any:
        current = frozenset(getattr(hierarchy, self.flag))
        target = (current | {self.element_id}) if self.value else (current - {self.element_id})
        return replace(hierarchy, **{self.flag: target})

    def apply(self, scene: TeachingSceneDocument) -> TeachingSceneDocument:
        from .hierarchy import read_hierarchy, write_hierarchy_into
        previous = read_hierarchy(scene.metadata)
        object.__setattr__(self, "_previous", previous)
        new_meta = write_hierarchy_into(scene.metadata, self._resolve(previous))
        return replace(scene, metadata=new_meta)

    def undo(self, scene: TeachingSceneDocument) -> TeachingSceneDocument:
        from .hierarchy import write_hierarchy_into
        if self._previous is None:
            raise RuntimeError("SetHierarchyCommand has not been applied")
        return replace(scene, metadata=write_hierarchy_into(scene.metadata, self._previous))


@dataclass(frozen=True)
class SetParentCommand:
    """Reparent one element through the validated scene hierarchy (UX-10/13)."""

    element_id: str
    parent_id: str | None
    command_id: str = field(default_factory=_command_id)
    label: str = "Reparent Layer"
    _previous: Any = None
    _previous_metadata: Mapping[str, Any] | None = None

    def apply(self, scene: TeachingSceneDocument) -> TeachingSceneDocument:
        from .hierarchy import read_hierarchy, validate_hierarchy, write_hierarchy_into

        element = scene.element_by_id(self.element_id)
        if element is None:
            raise ValueError(f"Element not found: {self.element_id}")
        parent = scene.element_by_id(self.parent_id) if self.parent_id is not None else None
        if self.parent_id is not None and parent is None:
            raise ValueError(f"Parent element not found: {self.parent_id}")
        if parent is not None and element.page_id != parent.page_id:
            raise ValueError("parent and child elements must belong to the same page")
        if self.parent_id == self.element_id:
            raise ValueError("an element cannot parent itself")
        previous = read_hierarchy(scene.metadata)
        parents = dict(previous.parents)
        if self.parent_id is None:
            parents.pop(self.element_id, None)
        else:
            parents[self.element_id] = self.parent_id
        updated = replace(previous, parents=parents)
        issues = validate_hierarchy((element.element_id for element in scene.elements), updated)
        if issues:
            raise ValueError(issues[0].message)
        object.__setattr__(self, "_previous", previous)
        object.__setattr__(self, "_previous_metadata", scene.metadata)
        return replace(scene, metadata=write_hierarchy_into(scene.metadata, updated))

    def undo(self, scene: TeachingSceneDocument) -> TeachingSceneDocument:
        if self._previous is None or self._previous_metadata is None:
            raise RuntimeError("SetParentCommand has not been applied")
        return replace(scene, metadata=self._previous_metadata)


@dataclass(frozen=True)
class RegisterComponentMasterCommand:
    """Register a component master over existing scene elements."""

    master: Any
    command_id: str = field(default_factory=_command_id)
    label: str = "Create Component Master"
    _previous_metadata: Mapping[str, Any] | None = None

    def apply(self, scene: TeachingSceneDocument) -> TeachingSceneDocument:
        from .components import read_active_variants, read_component_instances, read_component_masters, write_component_state_into
        if any(element_id not in {item.element_id for item in scene.elements}
               for element_id in self.master.element_ids):
            raise ValueError("component master references an unknown element")
        masters = read_component_masters(scene.metadata)
        if any(item.component_id == self.master.component_id for item in masters):
            raise ValueError(f"component already exists: {self.master.component_id}")
        object.__setattr__(self, "_previous_metadata", scene.metadata)
        metadata = write_component_state_into(
            scene.metadata, masters + (self.master,), read_component_instances(scene.metadata),
            read_active_variants(scene.metadata))
        return replace(scene, metadata=metadata)

    def undo(self, scene: TeachingSceneDocument) -> TeachingSceneDocument:
        if self._previous_metadata is None:
            raise RuntimeError("component master command has not been applied")
        return replace(scene, metadata=self._previous_metadata)


@dataclass(frozen=True)
class AddComponentInstanceCommand:
    """Add an instance reference; it stores element ids, never a scene copy."""

    instance: Any
    command_id: str = field(default_factory=_command_id)
    label: str = "Add Component Instance"
    _previous_metadata: Mapping[str, Any] | None = None

    def apply(self, scene: TeachingSceneDocument) -> TeachingSceneDocument:
        from .components import read_active_variants, read_component_instances, read_component_masters, write_component_state_into
        masters = read_component_masters(scene.metadata)
        master = next((item for item in masters if item.component_id == self.instance.component_id), None)
        if master is None:
            raise ValueError(f"unknown component: {self.instance.component_id}")
        if len(self.instance.element_ids) != len(master.element_ids):
            raise ValueError("component instance must map every master element")
        known = {item.element_id for item in scene.elements}
        if any(item not in known for item in self.instance.element_ids):
            raise ValueError("component instance references an unknown element")
        instances = read_component_instances(scene.metadata)
        if any(item.instance_id == self.instance.instance_id for item in instances):
            raise ValueError(f"instance already exists: {self.instance.instance_id}")
        object.__setattr__(self, "_previous_metadata", scene.metadata)
        metadata = write_component_state_into(
            scene.metadata, masters, instances + (self.instance,), read_active_variants(scene.metadata))
        return replace(scene, metadata=metadata)

    def undo(self, scene: TeachingSceneDocument) -> TeachingSceneDocument:
        if self._previous_metadata is None:
            raise RuntimeError("component instance command has not been applied")
        return replace(scene, metadata=self._previous_metadata)


@dataclass(frozen=True)
class UpdateComponentMasterCommand:
    """Update master elements and propagate non-overridden fields to instances."""

    component_id: str
    updates: Mapping[str, Mapping[str, Any]]
    command_id: str = field(default_factory=_command_id)
    label: str = "Update Component Master"
    _previous_scene: TeachingSceneDocument | None = None

    def apply(self, scene: TeachingSceneDocument) -> TeachingSceneDocument:
        from .components import read_component_instances, read_component_masters
        master = next((item for item in read_component_masters(scene.metadata)
                       if item.component_id == self.component_id), None)
        if master is None:
            raise ValueError(f"unknown component: {self.component_id}")
        object.__setattr__(self, "_previous_scene", scene)
        # AUD-27402: validate every per-element update against the same
        # whitelist used by UpdateElementCommand — the propagated instance
        # updates share these keys.
        for update in self.updates.values():
            if isinstance(update, Mapping):
                _validate_element_update_keys(update)
        by_id = {element.element_id: element for element in scene.elements}
        for source_id, update in self.updates.items():
            if source_id not in master.element_ids or source_id not in by_id:
                raise ValueError(f"unknown master element: {source_id}")
            by_id[source_id] = replace(by_id[source_id], **{
                key: _normalize_element_update(key, value) for key, value in update.items()
            })
        instances = read_component_instances(scene.metadata)
        for instance in instances:
            if instance.component_id != self.component_id:
                continue
            for source_id, update in self.updates.items():
                source_index = master.element_ids.index(source_id)
                if source_index >= len(instance.element_ids):
                    continue
                target_id = instance.element_ids[source_index]
                if target_id not in by_id:
                    continue
                overrides = instance.overrides.get(target_id, {})
                propagated = {key: value for key, value in update.items() if key not in overrides}
                if propagated:
                    by_id[target_id] = replace(by_id[target_id], **{
                        key: _normalize_element_update(key, value)
                        for key, value in propagated.items()
                    })
        return replace(scene, elements=tuple(by_id[element.element_id] for element in scene.elements))

    def undo(self, scene: TeachingSceneDocument) -> TeachingSceneDocument:
        if self._previous_scene is None:
            raise RuntimeError("component master command has not been applied")
        return self._previous_scene


@dataclass(frozen=True)
class SetComponentVariantCommand:
    component_id: str
    variant_id: str
    command_id: str = field(default_factory=_command_id)
    label: str = "Set Component Variant"
    _previous_metadata: Mapping[str, Any] | None = None

    def apply(self, scene: TeachingSceneDocument) -> TeachingSceneDocument:
        from .components import read_active_variants, read_component_instances, read_component_masters, write_component_state_into
        masters = read_component_masters(scene.metadata)
        master = next((item for item in masters if item.component_id == self.component_id), None)
        if master is None or self.variant_id not in master.variants:
            raise ValueError(f"unknown component variant: {self.component_id}/{self.variant_id}")
        active = read_active_variants(scene.metadata)
        object.__setattr__(self, "_previous_metadata", scene.metadata)
        active[self.component_id] = self.variant_id
        metadata = write_component_state_into(
            scene.metadata, masters, read_component_instances(scene.metadata), active)
        return replace(scene, metadata=metadata)

    def undo(self, scene: TeachingSceneDocument) -> TeachingSceneDocument:
        if self._previous_metadata is None:
            raise RuntimeError("component variant command has not been applied")
        return replace(scene, metadata=self._previous_metadata)


@dataclass(frozen=True)
class SetComponentOverrideCommand:
    instance_id: str
    element_id: str
    updates: Mapping[str, Any]
    command_id: str = field(default_factory=_command_id)
    label: str = "Override Component Instance"
    _previous_metadata: Mapping[str, Any] | None = None

    def apply(self, scene: TeachingSceneDocument) -> TeachingSceneDocument:
        from .components import read_active_variants, read_component_instances, read_component_masters, write_component_state_into
        instances = list(read_component_instances(scene.metadata))
        index = next((i for i, item in enumerate(instances) if item.instance_id == self.instance_id), None)
        if index is None:
            raise ValueError(f"unknown component instance: {self.instance_id}")
        instance = instances[index]
        if self.element_id not in instance.element_ids:
            raise ValueError(f"element is not part of instance: {self.element_id}")
        # AUD-27402: these keys persist into scene metadata and are replayed by
        # the detach path — reject non-whitelisted keys at the write boundary.
        _validate_element_update_keys(self.updates)
        overrides = {key: dict(value) for key, value in instance.overrides.items()}
        overrides[self.element_id] = {**overrides.get(self.element_id, {}), **dict(self.updates)}
        instances[index] = replace(instance, overrides=overrides)
        object.__setattr__(self, "_previous_metadata", scene.metadata)
        metadata = write_component_state_into(
            scene.metadata, read_component_masters(scene.metadata), tuple(instances),
            read_active_variants(scene.metadata))
        return replace(scene, metadata=metadata)

    def undo(self, scene: TeachingSceneDocument) -> TeachingSceneDocument:
        if self._previous_metadata is None:
            raise RuntimeError("component override command has not been applied")
        return replace(scene, metadata=self._previous_metadata)


@dataclass(frozen=True)
class SetResponsiveVariantsCommand:
    variants: tuple[Any, ...]
    command_id: str = field(default_factory=_command_id)
    label: str = "Set Responsive Variants"
    _previous_metadata: Mapping[str, Any] | None = None

    def apply(self, scene: TeachingSceneDocument) -> TeachingSceneDocument:
        from .components import write_responsive_variants_into
        object.__setattr__(self, "_previous_metadata", scene.metadata)
        return replace(scene, metadata=write_responsive_variants_into(scene.metadata, self.variants))

    def undo(self, scene: TeachingSceneDocument) -> TeachingSceneDocument:
        if self._previous_metadata is None:
            raise RuntimeError("responsive variant command has not been applied")
        return replace(scene, metadata=self._previous_metadata)


@dataclass(frozen=True)
class CompositeCommand:
    """Apply several commands as one undoable step.

    Sub-commands apply in order (each on the result of the previous) and undo
    in reverse.  Each sub-command captures its own pre-state on apply, so this
    is correct when sub-commands touch distinct elements (e.g. multi-element
    align/distribute).  Avoid mixing sub-commands that read/write the same
    element unless the order is carefully controlled.
    """

    commands: tuple[SceneCommand, ...]
    command_id: str = field(default_factory=_command_id)
    label: str = "Composite"

    def apply(self, scene: TeachingSceneDocument) -> TeachingSceneDocument:
        for command in self.commands:
            scene = command.apply(scene)
        return scene

    def undo(self, scene: TeachingSceneDocument) -> TeachingSceneDocument:
        for command in reversed(self.commands):
            scene = command.undo(scene)
        return scene


class SceneCommandStack:
    """Command stack that returns new scene documents and never mutates them."""

    def __init__(self, max_size: int = 100) -> None:
        if max_size <= 0:
            raise ValueError("max_size must be positive")
        self._max_size = max_size
        self._undo_stack: list[SceneCommand] = []
        self._redo_stack: list[SceneCommand] = []

    def execute(self, command: SceneCommand, scene: TeachingSceneDocument) -> TeachingSceneDocument:
        updated = command.apply(scene)
        self._undo_stack.append(command)
        self._redo_stack.clear()
        if len(self._undo_stack) > self._max_size:
            self._undo_stack = self._undo_stack[-self._max_size:]
        return updated

    def undo(self, scene: TeachingSceneDocument) -> TeachingSceneDocument | None:
        if not self._undo_stack:
            return None
        command = self._undo_stack.pop()
        restored = command.undo(scene)
        self._redo_stack.append(command)
        return restored

    def redo(self, scene: TeachingSceneDocument) -> TeachingSceneDocument | None:
        if not self._redo_stack:
            return None
        command = self._redo_stack.pop()
        updated = command.apply(scene)
        self._undo_stack.append(command)
        return updated

    def can_undo(self) -> bool:
        return bool(self._undo_stack)

    def can_redo(self) -> bool:
        return bool(self._redo_stack)

    def get_undo_label(self) -> str | None:
        return self._undo_stack[-1].label if self._undo_stack else None

    def get_redo_label(self) -> str | None:
        return self._redo_stack[-1].label if self._redo_stack else None

    def clear(self) -> None:
        self._undo_stack.clear()
        self._redo_stack.clear()
