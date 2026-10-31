"""Qt-free view-model for the Motion Graphic keyframes timeline.

The keyframes timeline reuses the layer tree's ordering (top-most element
first) and projects each element into one element row plus one row per
animatable property when expanded.  Tracks resolve with the same precedence
as the evaluator (``render/evaluator.py::_tracks_for``): an element's
``local_animation_tracks`` (preset-built) are shown first and a scene-level
``SceneAnimation`` overrides them, so an edit made here always wins at
evaluation time.

This module is pure Python — no Qt — so the row model and the track-edit
helpers unit-test directly while :mod:`mg_keyframes_panel` stays a renderer.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Iterable, Mapping

from src.shared.keyframe import InterpolationType, Keyframe, KeyframeTrack

from ..core.model import SceneElement, TeachingSceneDocument
from ..render.evaluator import ANIMATABLE_STYLE_PROPERTIES
from .layer_tree_model import _element_label, page_display_name

#: Transform properties every native element exposes, in AE-familiar order.
TRANSFORM_PROPERTIES: tuple[str, ...] = (
    "x", "y", "scale_x", "scale_y", "rotation", "opacity", "width", "height",
)

#: Clamping ranges for newly created tracks.  Transform ranges stay wide so a
#: dragged keyframe never hits a synthetic ceiling (mirrors auto-key).
_PROPERTY_RANGES: dict[str, tuple[float, float]] = {
    "x": (-1_000_000.0, 1_000_000.0),
    "y": (-1_000_000.0, 1_000_000.0),
    "width": (0.0, 1_000_000.0),
    "height": (0.0, 1_000_000.0),
    "scale_x": (-1_000_000.0, 1_000_000.0),
    "scale_y": (-1_000_000.0, 1_000_000.0),
    "rotation": (-36_000.0, 36_000.0),
    "opacity": (0.0, 1.0),
}
_PROPERTY_RANGES.update(dict(ANIMATABLE_STYLE_PROPERTIES))

#: Style keyframes that make sense per element type.  Everything else falls
#: back to blur-only; transform properties apply to all types.
_STYLE_PROPERTIES_BY_TYPE: dict[str, tuple[str, ...]] = {
    "text": ("font_size", "blur"),
    "formula": ("font_size", "blur"),
    "shape": ("fill_opacity", "stroke_width", "blur"),
}
_DEFAULT_STYLE_PROPERTIES: tuple[str, ...] = ("blur",)

# Keep in sync with evaluator._STYLE_DEFAULTS (private there on purpose).
_STATIC_STYLE_DEFAULTS: dict[str, float] = {
    "font_size": 32.0, "blur": 0.0, "fill_opacity": 1.0, "stroke_width": 0.0,
}


def property_range(property_name: str) -> tuple[float, float]:
    """Return the (lo, hi) clamp a new track for this property should carry."""

    return _PROPERTY_RANGES.get(property_name, (-1_000_000.0, 1_000_000.0))


def animatable_properties_for(element: SceneElement) -> tuple[str, ...]:
    """Ordered property rows for one element (transform set + type styles)."""

    styles = _STYLE_PROPERTIES_BY_TYPE.get(element.type, _DEFAULT_STYLE_PROPERTIES)
    return TRANSFORM_PROPERTIES + styles


def static_value_for(element: SceneElement, property_name: str) -> float:
    """The un-animated value a new keyframe seeds from (the static truth)."""

    transform = element.transform
    if property_name == "opacity":
        try:
            return max(0.0, min(1.0, float(element.style.get("opacity", 1.0))))
        except (TypeError, ValueError):
            return 1.0
    if property_name in ("x", "y", "width", "height", "scale_x", "scale_y", "rotation"):
        return float(getattr(transform, property_name, 0.0))
    if property_name in _STATIC_STYLE_DEFAULTS:
        raw = element.style.get(property_name, _STATIC_STYLE_DEFAULTS[property_name])
        try:
            return float(raw)
        except (TypeError, ValueError):
            return _STATIC_STYLE_DEFAULTS[property_name]
    return 0.0


def effective_tracks(
    scene: TeachingSceneDocument, element_id: str,
) -> dict[str, tuple[KeyframeTrack | None, str]]:
    """Merge both track stores for one element with evaluator precedence.

    Returns ``{property: (track, source)}`` where source is ``"local"`` for
    preset-built ``local_animation_tracks`` or ``"scene"`` for a
    ``SceneAnimation`` that overrides it.
    """

    element = scene.element_by_id(element_id)
    result: dict[str, tuple[KeyframeTrack | None, str]] = {}
    if element is None:
        return result
    for track in element.local_animation_tracks:
        result[track.param_name] = (track, "local")
    for animation in scene.animations:
        if animation.element_id == element_id:
            result[animation.property] = (animation.track, "scene")
    return result


@dataclass
class MgKeyframePoint:
    """One keyframe diamond as the timeline paints it (no value payload)."""

    frame: int
    interpolation: str = "linear"
    selected: bool = False
    source: str = "scene"


@dataclass
class MgKeyframesRow:
    """One row of the keyframes timeline (page, background, element or property)."""

    row_id: str                     # "page:{id}" | "element:{id}" | "prop:{id}:{prop}"
    kind: str                       # "page" | "background" | "element" | "property"
    element_id: str = ""
    page_id: str = ""
    property_name: str = ""
    label: str = ""
    element_type: str = ""
    depth: int = 0
    expanded: bool = False
    selected: bool = False
    points: tuple[MgKeyframePoint, ...] = ()
    has_track: bool = False
    track_source: str = ""          # "scene" | "local" | ""
    editable: bool = True           # False for read-only rows (page headers)
    page_spans: tuple[tuple[int, int], ...] = ()  # page rows: the page extent
    background_value: str = ""       # background rows: empty means inherit


def build_keyframe_rows(
    scene: TeachingSceneDocument, *,
    selected_ids: frozenset[str] | Iterable[str] = frozenset(),
    expanded_ids: frozenset[str] | Iterable[str] = frozenset(),
    expanded_page_ids: frozenset[str] | Iterable[str] | None = None,
    property_filter: str = "all",
) -> list[MgKeyframesRow]:
    """Flatten a scene into visible timeline rows (top-most element first).

    Elements are grouped under one page row per artboard (timeline order,
    z-descending inside each page); the page row carries its frame extent for
    the span bar.  ``expanded_page_ids`` controls whether child rows are
    visible, while property rows only exist for expanded elements.
    """

    selected = frozenset(selected_ids)
    expanded = frozenset(expanded_ids)
    expanded_pages = (
        frozenset(scene.page_ids)
        if expanded_page_ids is None else frozenset(expanded_page_ids)
    )
    if property_filter not in {"all", "animated"}:
        raise ValueError(f"Unknown keyframe property filter: {property_filter}")
    ordered = sorted(
        enumerate(scene.elements),
        key=lambda pair: (pair[1].z_index, pair[0]),
        reverse=True,
    )
    starts = scene.page_start_frames()
    extra_depth = 1
    rows: list[MgKeyframesRow] = []

    def _element_rows(element: SceneElement) -> list[MgKeyframesRow]:
        element_id = element.element_id
        is_expanded = element_id in expanded
        element_rows = [MgKeyframesRow(
            row_id=f"element:{element_id}",
            kind="element",
            element_id=element_id,
            page_id=element.page_id,
            label=_element_label(element),
            element_type=element.type,
            depth=extra_depth,
            expanded=is_expanded,
            selected=element_id in selected,
        )]
        if not is_expanded:
            return element_rows
        tracks = effective_tracks(scene, element_id)
        properties = list(animatable_properties_for(element))
        properties.extend(sorted(name for name in tracks if name not in properties))
        if property_filter == "animated":
            properties = [
                name for name in properties
                if (tracks.get(name, (None, ""))[0] is not None
                    and tracks[name][0].keyframes)
            ]
        for property_name in properties:
            track, source = tracks.get(property_name, (None, ""))
            points = tuple(
                MgKeyframePoint(
                    frame=int(kf.frame),
                    interpolation=str(getattr(kf.interpolation, "value", kf.interpolation)),
                    source=source,
                )
                for kf in (track.keyframes if track is not None else ())
            )
            element_rows.append(MgKeyframesRow(
                row_id=f"prop:{element_id}:{property_name}",
                kind="property",
                element_id=element_id,
                property_name=property_name,
                label=property_name,
                element_type=element.type,
                depth=1 + extra_depth,
                points=points,
                has_track=track is not None,
                track_source=source,
            ))
        return element_rows

    for index, page in enumerate(scene.pages):
        start = starts[index]
        end = start + page.duration_frames - 1
        rows.append(MgKeyframesRow(
            row_id=f"page:{page.page_id}",
            kind="page",
            element_id=page.page_id,
            page_id=page.page_id,
            label=page_display_name(page, index),
            expanded=page.page_id in expanded_pages,
            editable=False,
            page_spans=((start, max(start, end)),),
        ))
        if page.page_id in expanded_pages:
            for _idx, element in ordered:
                if element.page_id == page.page_id:
                    rows.extend(_element_rows(element))
            rows.append(MgKeyframesRow(
                row_id=f"background:{page.page_id}",
                kind="background",
                page_id=page.page_id,
                label="Background",
                depth=extra_depth,
                editable=False,
                page_spans=((start, max(start, end)),),
                background_value=page.background,
            ))
    rows.append(MgKeyframesRow(
        row_id="background:global",
        kind="background",
        label="Global Background",
        editable=False,
        page_spans=((0, max(0, scene.duration_frames - 1)),),
        background_value=str(
            scene.background.get("css", scene.background.get("color", ""))
            if isinstance(scene.background, Mapping) else ""
        ),
    ))
    return rows


# ── track-edit helpers (pure; the panel calls these, then emits results) ─────


def copy_track(track: KeyframeTrack) -> KeyframeTrack:
    """Deep copy through the serialized form so extrap/handles survive."""

    return KeyframeTrack.from_dict(track.to_dict())


def new_track_for(property_name: str) -> KeyframeTrack:
    lo, hi = property_range(property_name)
    return KeyframeTrack(property_name, keyframes=[], min_val=lo, max_val=hi)


def track_with_keyframe_added(
    track: KeyframeTrack | None, frame: int, value: float, property_name: str,
) -> KeyframeTrack:
    """Add (or replace-in-place) one keyframe; creating the track if needed."""

    result = copy_track(track) if track is not None else new_track_for(property_name)
    result.add_keyframe(Keyframe(int(frame), float(value)))
    return result


def keyframe_with_frame(keyframe: Keyframe, frame: int) -> Keyframe:
    """Clone a keyframe at another frame without dropping curve metadata."""

    # ``Keyframe`` has accumulated legacy Bezier fields and authoritative
    # tangent handles over time.  Reconstructing it from only value and
    # interpolation silently resets those fields, which is especially visible
    # after Alt-drag or paste.
    return replace(keyframe, frame=int(frame))


def track_with_keyframe_copied(
    track: KeyframeTrack | None,
    keyframe: Keyframe,
    frame: int,
    property_name: str,
) -> KeyframeTrack:
    """Copy one complete keyframe to a new frame in a track."""

    result = copy_track(track) if track is not None else new_track_for(property_name)
    result.add_keyframe(keyframe_with_frame(keyframe, frame))
    return result


def track_with_keyframe_value(
    track: KeyframeTrack | None, frame: int, value: float,
) -> KeyframeTrack | None:
    """Change one keyframe's value while retaining its interpolation handles."""

    if track is None:
        return None
    result = copy_track(track)
    keyframe = result.get_keyframe_at(int(frame))
    if keyframe is None:
        return result
    keyframe.value = float(value)
    return result


def track_with_keyframes_removed(
    track: KeyframeTrack | None, frames: Iterable[int],
) -> KeyframeTrack | None:
    """Remove keyframes; an emptied track becomes None (remove the animation)."""

    if track is None:
        return None
    result = copy_track(track)
    for frame in frames:
        result.remove_keyframe(int(frame))
    return result if result.keyframes else None


def track_with_keyframes_moved(
    track: KeyframeTrack, moves: Iterable[tuple[int, int]],
) -> KeyframeTrack:
    """Move keyframes within one track: ``(old_frame, new_frame)`` pairs.

    Callers validate that the destination frames stay unique before calling;
    values and interpolation of every moved keyframe are preserved.
    """

    move_map = {int(old): int(new) for old, new in moves}
    result = copy_track(track)
    originals = {int(kf.frame): kf for kf in track.keyframes}
    for old_frame in move_map:
        result.remove_keyframe(old_frame)
    for old_frame, new_frame in move_map.items():
        original = originals[old_frame]
        result.add_keyframe(keyframe_with_frame(original, new_frame))
    return result


def track_with_interpolation(
    track: KeyframeTrack, frames: Iterable[int], interpolation: InterpolationType,
) -> KeyframeTrack:
    """Set the interpolation mode of the keyframes starting at ``frames``."""

    targets = {int(frame) for frame in frames}
    result = copy_track(track)
    for kf in result.keyframes:
        if int(kf.frame) in targets:
            kf.interpolation = interpolation
    return result


# ── frame ↔ pixel mapping (shared by the tree and the track canvas) ─────────


@dataclass
class FrameMapper:
    """Linear frame↔pixel mapping with optional fixed frame-zero origin."""

    pixels_per_frame: float = 8.0
    scroll_offset: float = 0.0
    origin_locked: bool = False

    def frame_to_x(self, frame: float) -> float:
        return self.scroll_offset + frame * self.pixels_per_frame

    def x_to_frame(self, x: float) -> float:
        return (x - self.scroll_offset) / max(self.pixels_per_frame, 1e-9)

    def zoom_at(self, anchor_x: float, factor: float,
                min_ppf: float = 0.05, max_ppf: float = 256.0) -> None:
        anchor_frame = self.x_to_frame(anchor_x)
        ppf = min(max_ppf, max(min_ppf, self.pixels_per_frame * factor))
        self.pixels_per_frame = ppf
        if self.origin_locked:
            # The Motion Graphic timeline mirrors Edit's fixed left ruler:
            # frame zero stays pinned to the tree/track split while zooming.
            self.scroll_offset = 0.0
        else:
            self.scroll_offset = anchor_x - anchor_frame * ppf


__all__ = [
    "FrameMapper",
    "MgKeyframePoint",
    "MgKeyframesRow",
    "TRANSFORM_PROPERTIES",
    "animatable_properties_for",
    "build_keyframe_rows",
    "copy_track",
    "effective_tracks",
    "keyframe_with_frame",
    "new_track_for",
    "property_range",
    "static_value_for",
    "track_with_interpolation",
    "track_with_keyframe_added",
    "track_with_keyframe_copied",
    "track_with_keyframe_value",
    "track_with_keyframes_moved",
    "track_with_keyframes_removed",
]
