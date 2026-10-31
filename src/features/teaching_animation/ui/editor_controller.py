"""Qt-independent editor state, commands and local scene diff tracking."""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import replace
from typing import Callable, Mapping
import uuid

from ..core.commands import (
    AddMarkerCommand, CompositeCommand, RemoveMarkerCommand, RenameMarkerCommand,
    SceneCommand, SceneCommandStack, UpdateElementCommand,
)
from ..core.contracts import AnimationFrame, RenderContext, RenderedFrame
from ..core.model import SceneAnimation, SceneElement, SceneTransform, TeachingSceneDocument
from src.shared.keyframe import Keyframe, KeyframeTrack


@dataclass(frozen=True)
class SceneDiff:
    changed_element_ids: frozenset[str] = frozenset()
    changed_fields: Mapping[str, frozenset[str]] = None  # type: ignore[assignment]
    duration_changed: bool = False
    animation_changed: bool = False
    pages_changed: bool = False

    def __post_init__(self) -> None:
        if self.changed_fields is None:
            object.__setattr__(self, "changed_fields", {})

    @property
    def is_empty(self) -> bool:
        return not (
            self.changed_element_ids
            or self.duration_changed
            or self.animation_changed
            or self.pages_changed
        )


def diff_scenes(before: TeachingSceneDocument, after: TeachingSceneDocument) -> SceneDiff:
    old = {element.element_id: element for element in before.elements}
    new = {element.element_id: element for element in after.elements}
    changed: set[str] = set(old) ^ set(new)
    fields: dict[str, frozenset[str]] = {}
    for element_id in set(old) & set(new):
        changed_fields = frozenset(
            field for field in (
                "type", "content", "style", "layout", "transform", "visible",
                "z_index", "asset_ref", "template_id", "local_animation_tracks",
                "page_id",
            )
            if getattr(old[element_id], field) != getattr(new[element_id], field)
        )
        if changed_fields:
            changed.add(element_id)
            fields[element_id] = changed_fields
    return SceneDiff(
        changed_element_ids=frozenset(changed),
        changed_fields=fields,
        duration_changed=before.duration_frames != after.duration_frames,
        animation_changed=(
            before.animations != after.animations
            or any(
                old_element.local_animation_tracks != new_element.local_animation_tracks
                for element_id, old_element in old.items()
                if (new_element := new.get(element_id)) is not None
            )
        ),
        pages_changed=before.pages != after.pages,
    )


class SceneEditorController:
    """Own one scene's editable state and never mutate the document in place."""

    def __init__(self, scene: TeachingSceneDocument) -> None:
        self._scene = scene
        self._stack = SceneCommandStack()
        self._dirty = False
        self._current_frame = 0
        self._preview_originals: dict[str, SceneElement] = {}
        self._listeners: list[Callable[[TeachingSceneDocument, SceneDiff], None]] = []
        # Auto-key (UX-14): when True, finalizing a direct-manipulation edit
        # also writes keyframes at the current frame for the changed transform
        # properties, so dragging an element at frame N animates it.  Off by
        # default ⇒ existing static-transform behaviour is unchanged.
        self._auto_key = False

    @property
    def scene(self) -> TeachingSceneDocument:
        return self._scene

    @property
    def current_frame(self) -> int:
        return self._current_frame

    @property
    def current_page_id(self) -> str:
        """Page owning the current frame; new elements land here by default."""
        return self._scene.page_ids[self._scene.page_index_for_frame(self._current_frame)]

    @property
    def is_dirty(self) -> bool:
        return self._dirty

    @property
    def auto_key(self) -> bool:
        return self._auto_key

    def set_auto_key(self, enabled: bool) -> None:
        self._auto_key = bool(enabled)

    def subscribe(self, listener: Callable[[TeachingSceneDocument, SceneDiff], None]) -> None:
        self._listeners.append(listener)

    def set_frame(self, frame: int) -> int:
        self._current_frame = max(0, min(int(frame), self._scene.duration_frames - 1))
        return self._current_frame

    def execute(self, command: SceneCommand) -> SceneDiff:
        previous = self._scene
        self._scene = self._stack.execute(command, self._scene)
        self._clamp_current_frame()
        diff = diff_scenes(previous, self._scene)
        self._dirty = True
        self._notify(diff)
        return diff

    def _clamp_current_frame(self) -> None:
        self._current_frame = max(
            0,
            min(self._current_frame, max(0, self._scene.duration_frames - 1)),
        )

    def update_element(self, element_id: str, updates: Mapping[str, object]) -> SceneDiff:
        return self.execute(UpdateElementCommand(element_id, dict(updates)))

    def preview_element(self, element_id: str, updates: Mapping[str, object]) -> SceneDiff:
        """Update the live preview without adding an undo entry per mouse move."""
        previous = self._scene
        element = self._scene.element_by_id(element_id)
        if element is None:
            raise ValueError(f"Element not found: {element_id}")
        from dataclasses import replace as dc_replace
        normalized = dict(updates)
        if "transform" in normalized and isinstance(normalized["transform"], Mapping):
            normalized["transform"] = SceneTransform.from_dict(normalized["transform"])
        self._scene = self._scene.with_element_updated(dc_replace(element, **normalized))
        diff = diff_scenes(previous, self._scene)
        self._notify(diff)
        return diff

    def begin_preview(self, element_id: str) -> None:
        element = self._scene.element_by_id(element_id)
        if element is not None:
            self._preview_originals.setdefault(element_id, element)

    def commit_preview(self, element_id: str) -> SceneDiff | None:
        original = self._preview_originals.pop(element_id, None)
        current = self._scene.element_by_id(element_id)
        if original is None or current is None or original == current:
            return None
        self._scene = self._scene.with_element_updated(original)
        updates = {
            "type": current.type, "z_index": current.z_index, "visible": current.visible,
            "transform": current.transform, "style": current.style, "content": current.content,
            "asset_ref": current.asset_ref, "template_id": current.template_id,
            "local_animation_tracks": current.local_animation_tracks,
        }
        # Auto-key (UX-14): a finalized drag at the current frame writes a
        # keyframe for each transform property that actually moved, so direct
        # manipulation becomes animation.  Static (auto-key off) is unchanged.
        # AUD-30611: the element update and every auto-keyframe land in ONE
        # command (one undo record, one scene notification), matching the
        # "one gesture = one undo" convention apply_track_edits established.
        commands: list[SceneCommand] = [UpdateElementCommand(element_id, updates)]
        if self._auto_key and original.transform != current.transform:
            commands.extend(
                self._auto_key_transform_commands(element_id, original.transform, current.transform))
        if len(commands) == 1:
            return self.execute(commands[0])
        return self.execute(CompositeCommand(tuple(commands), label="Drag + Auto-Key"))

    def _auto_key_transform_commands(
        self,
        element_id: str,
        before: SceneTransform,
        after: SceneTransform,
    ) -> list[SceneCommand]:
        """Build a keyframe command for each transform field that changed.

        The element-update command that precedes these never touches
        ``scene.animations``, so the animation commands built against the
        pre-drag document apply identically to the post-update document.
        """
        frame = self._current_frame
        # Wide range so position/scale/rotation keyframes are never clamped
        # (get_value clamps to [min_val, max_val]; the default 0..1 range would
        # break coordinate keyframes).
        wide = {"min_val": -1_000_000.0, "max_val": 1_000_000.0}
        commands: list[SceneCommand] = []
        for name in ("x", "y", "scale_x", "scale_y", "rotation"):
            if getattr(before, name) != getattr(after, name):
                commands.extend(self._keyframe_commands(
                    element_id, name, frame, float(getattr(after, name)), **wide))
        return commands

    def undo(self) -> SceneDiff | None:
        previous = self._scene
        restored = self._stack.undo(self._scene)
        if restored is None:
            return None
        self._scene = restored
        self._clamp_current_frame()
        diff = diff_scenes(previous, self._scene)
        self._dirty = True
        self._notify(diff)
        return diff

    def redo(self) -> SceneDiff | None:
        previous = self._scene
        updated = self._stack.redo(self._scene)
        if updated is None:
            return None
        self._scene = updated
        self._clamp_current_frame()
        diff = diff_scenes(previous, self._scene)
        self._dirty = True
        self._notify(diff)
        return diff

    def add_keyframe(self, element_id: str, property_name: str, frame: int, value: float,
                     *, min_val: float | None = None, max_val: float | None = None) -> SceneDiff:
        """Add/replace a numeric keyframe while retaining shared keyframe semantics.

        ``min_val`` / ``max_val`` bound the track's clamping range: they seed
        a new track, and on an existing track they only ever WIDEN it (a
        narrow legacy track — e.g. an AI ``set_animation`` default ``[0, 1]``
        — used to silently clamp authored coordinates at evaluation time,
        AUD-30610).  They default to the ``[0, max(1, |value|)]`` range, which
        suits normalised properties like opacity but is too narrow for
        coordinate keyframes (x/y) — callers auto-keying transforms pass a
        wide range.
        """
        commands = self._keyframe_commands(
            element_id, property_name, frame, value, min_val=min_val, max_val=max_val)
        return self.execute(commands[0])

    def _keyframe_commands(
        self,
        element_id: str,
        property_name: str,
        frame: int,
        value: float,
        *,
        min_val: float | None = None,
        max_val: float | None = None,
    ) -> list[SceneCommand]:
        """Build the single command adding/replacing one numeric keyframe."""
        if self._scene.element_by_id(element_id) is None:
            raise ValueError(f"Element not found: {element_id}")
        existing = next((item for item in self._scene.animations
                         if item.element_id == element_id and item.property == property_name), None)
        if existing is None:
            lo = 0.0 if min_val is None else float(min_val)
            hi = max(1.0, abs(value)) if max_val is None else float(max_val)
            track = KeyframeTrack(property_name, min_val=lo, max_val=hi)
            track.add_keyframe(Keyframe(frame, value))
            animation = SceneAnimation(f"animation_{uuid.uuid4().hex}", element_id, property_name, track)
            from ..core.commands import AddAnimationCommand
            return [AddAnimationCommand(animation)]
        track = KeyframeTrack.from_dict(existing.track.to_dict())
        # AUD-30610: the stored value is never clamped silently at authoring
        # time — the track's range widens to hold it (and the caller's bounds)
        # so evaluation returns exactly what was keyed.  Widening never
        # narrows the range and never changes existing in-range keyframes.
        widened_min = min(track.min_val, float(value))
        widened_max = max(track.max_val, float(value))
        if min_val is not None:
            widened_min = min(widened_min, float(min_val))
        if max_val is not None:
            widened_max = max(widened_max, float(max_val))
        track.min_val = widened_min
        track.max_val = widened_max
        track.add_keyframe(Keyframe(frame, value))
        from ..core.commands import UpdateAnimationTrackCommand
        return [UpdateAnimationTrackCommand(existing.animation_id, track)]

    def apply_track_edits(self, edits) -> SceneDiff | None:
        """Apply a batch of whole-track edits as one undoable step.

        ``edits`` is a sequence of ``(element_id, property, track_or_None)``
        tuples — the payload the keyframes timeline emits on drag release.
        ``None`` (or an empty track) removes the element's animation for that
        property — the scene-level ``SceneAnimation`` when one exists, else the
        preset-built ``local_animation_tracks`` entry (a scene-level None
        cannot shadow an embedded track, so the deletion must land there).  A
        track creates or replaces the scene animation.  One batch becomes one
        undo entry, so a multi-keyframe drag is a single reversible edit.
        """
        scene = self._scene
        commands: list[SceneCommand] = []
        for element_id, property_name, track in edits:
            element_id = str(element_id)
            property_name = str(property_name)
            element = scene.element_by_id(element_id)
            if element is None:
                raise ValueError(f"Element not found: {element_id}")
            existing = next(
                (item for item in scene.animations
                 if item.element_id == element_id and item.property == property_name),
                None,
            )
            if track is None or not track.keyframes:
                if existing is not None:
                    from ..core.commands import RemoveAnimationCommand
                    commands.append(RemoveAnimationCommand(existing.animation_id))
                elif any(t.param_name == property_name for t in element.local_animation_tracks):
                    remaining = tuple(
                        t for t in element.local_animation_tracks
                        if t.param_name != property_name
                    )
                    commands.append(UpdateElementCommand(
                        element_id, {"local_animation_tracks": remaining},
                        label="Remove Animation Track"))
                continue
            if track.param_name != property_name:
                track = replace(track, param_name=property_name)
            if existing is None:
                from ..core.commands import AddAnimationCommand
                animation = SceneAnimation(
                    f"animation_{uuid.uuid4().hex}", element_id, property_name, track)
                commands.append(AddAnimationCommand(animation))
            else:
                from ..core.commands import UpdateAnimationTrackCommand
                commands.append(UpdateAnimationTrackCommand(existing.animation_id, track))
        if not commands:
            return None
        if len(commands) == 1:
            return self.execute(commands[0])
        return self.execute(CompositeCommand(tuple(commands), label="Edit Keyframes"))

    def add_marker(self, frame: int, label: str | None = None) -> SceneDiff:
        from ..core.model import SceneMarker
        marker = SceneMarker(
            marker_id=f"marker_{uuid.uuid4().hex}",
            frame=int(frame),
            label=label or f"Marker {int(frame)}",
        )
        return self.execute(AddMarkerCommand(marker))

    def remove_marker(self, marker_id: str) -> SceneDiff:
        return self.execute(RemoveMarkerCommand(str(marker_id)))

    def clear_markers(self) -> SceneDiff | None:
        """Remove every timeline marker as one undoable operation."""
        markers = self._scene.markers
        if not markers:
            return None
        commands = tuple(RemoveMarkerCommand(marker.marker_id) for marker in markers)
        return self.execute(CompositeCommand(commands, label="Remove All Markers"))

    def rename_marker(self, marker_id: str, label: str) -> SceneDiff:
        return self.execute(RenameMarkerCommand(str(marker_id), str(label)))

    def render(self, context: RenderContext | None = None, *,
               page_id: str | None = None) -> RenderedFrame:
        """Render the current frame for the editor preview.

        ``page_id`` limits the render (and hit-testing scene) to one artboard —
        the editor canvas shows a single page at a time; export paths keep
        using the full document without this argument.
        """
        context = context or RenderContext(fps=self._scene.fps, width=self._scene.width, height=self._scene.height)
        from ..render.scene_renderer_router import select_scene_renderer
        scene = self._scene if page_id is None else self._scene.limited_to_page(page_id)
        renderer = select_scene_renderer(scene)
        renderer.prepare(scene, context)
        try:
            return renderer.render_frame(AnimationFrame(self._current_frame, context.fps))
        finally:
            renderer.cleanup()

    def save(self, saver: Callable[[TeachingSceneDocument], None]) -> None:
        saver(self._scene)
        self._dirty = False

    def mark_clean(self) -> None:
        self._dirty = False

    def _notify(self, diff: SceneDiff) -> None:
        for listener in tuple(self._listeners):
            listener(self._scene, diff)
