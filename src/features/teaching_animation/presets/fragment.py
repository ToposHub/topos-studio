"""Turn a preset scene into host-scene elements (and the reverse).

``prepare_insert`` is the drag-and-drop hot path: it remaps element ids,
re-centres the fragment on the drop point and lifts its z stack above the
host's current layers.  ``extract_fragment`` is the "Save as Preset" path —
it snapshots a selection (plus descendants) into an origin-normalised scene
dict whose animations all travel inside the elements.
"""

from __future__ import annotations

import uuid
from dataclasses import replace
from typing import Iterable, Sequence

from ..core.hierarchy import Hierarchy, read_hierarchy, write_hierarchy_into
from ..core.model import SceneElement, TeachingSceneDocument
from src.shared.keyframe import Keyframe, KeyframeTrack


def fragment_bounds(scene: TeachingSceneDocument) -> tuple[float, float, float, float] | None:
    """(min_x, min_y, max_x, max_y) over element transforms; None if empty."""
    if not scene.elements:
        return None
    min_x = min_y = float("inf")
    max_x = max_y = float("-inf")
    for element in scene.elements:
        transform = element.transform
        min_x = min(min_x, transform.x)
        min_y = min(min_y, transform.y)
        max_x = max(max_x, transform.x + transform.width)
        max_y = max(max_y, transform.y + transform.height)
    return (min_x, min_y, max_x, max_y)


def _build_id_mapping(preset_scene: TeachingSceneDocument,
                      existing: set[str]) -> dict[str, str]:
    """Fresh, collision-free ids for every preset element.

    Host ids, duplicated preset ids and (astronomically unlikely) uuid clashes
    are all resolved by regenerating until unique.
    """
    mapping: dict[str, str] = {}
    taken = set(existing)
    for element in preset_scene.elements:
        new = f"{element.element_id}_{uuid.uuid4().hex[:10]}"
        while new in taken:
            new = f"{element.element_id}_{uuid.uuid4().hex[:10]}"
        taken.add(new)
        mapping[element.element_id] = new
    return mapping


def _rebuild_track(track: KeyframeTrack, remapped_frames: list[Keyframe]) -> KeyframeTrack:
    """Copy a track with new keyframe frames, preserving bounds/extrapolation.

    ``add_keyframes_bulk`` re-sorts and collapses same-frame duplicates
    (keeping the last one), so frame remapping that collapses keyframes —
    fps rounding or page-span clamping — stays strictly ordered.
    """
    result = KeyframeTrack.from_dict(track.to_dict())
    result.keyframes = []
    result.add_keyframes_bulk(remapped_frames)
    return result


def _convert_track_fps(track: KeyframeTrack, factor: float) -> KeyframeTrack:
    return _rebuild_track(track, [
        replace(keyframe, frame=max(0, round(keyframe.frame * factor)))
        for keyframe in track.keyframes
    ])


def prepare_insert(preset_scene: TeachingSceneDocument, host_scene: TeachingSceneDocument, *,
                   target_x: float, target_y: float) -> tuple[tuple[SceneElement, ...], dict[str, str]]:
    """Remap ids, centre on the drop point, match host fps, stack above host.

    The fragment keeps its internal z ordering; its lowest layer lands one
    above the host's current maximum z so a dropped preset never slides under
    existing content.  Keyframe frames are stored as scene-frame counts, not
    wall-clock time: a 72-frame preset authored at 24 fps must become 90
    frames inside a 30 fps host or its animation plays 20% fast.  Hierarchy
    edges *inside* the fragment are remapped and returned separately for
    ``InsertSceneFragmentCommand``.
    """
    if not preset_scene.elements:
        raise ValueError("preset has no elements to insert")
    mapping = _build_id_mapping(
        preset_scene, {element.element_id for element in host_scene.elements})
    bounds = fragment_bounds(preset_scene)
    assert bounds is not None
    min_x, min_y, max_x, max_y = bounds
    dx = float(target_x) - (min_x + (max_x - min_x) / 2.0)
    dy = float(target_y) - (min_y + (max_y - min_y) / 2.0)
    z_base = max((element.z_index for element in host_scene.elements), default=-1) + 1
    z_min = min(element.z_index for element in preset_scene.elements)
    fps_factor = (host_scene.fps / preset_scene.fps) if preset_scene.fps else 1.0
    retimed = abs(fps_factor - 1.0) > 1e-9
    elements: list[SceneElement] = []
    for element in preset_scene.elements:
        transform = replace(element.transform, x=element.transform.x + dx,
                            y=element.transform.y + dy)
        updated = replace(element, element_id=mapping[element.element_id],
                          transform=transform,
                          z_index=element.z_index - z_min + z_base)
        if retimed and element.local_animation_tracks:
            updated = replace(updated, local_animation_tracks=tuple(
                _convert_track_fps(track, fps_factor)
                for track in element.local_animation_tracks))
        elements.append(updated)
    hierarchy = read_hierarchy(preset_scene.metadata)
    fragment_ids = {element.element_id for element in preset_scene.elements}
    parents = {
        mapping[child]: mapping[parent]
        for child, parent in hierarchy.parents.items()
        if child in fragment_ids and parent in fragment_ids
    }
    return tuple(elements), parents


def retime_fragment_to_page(elements: Iterable[SceneElement],
                            page_start: int, page_end: int) -> tuple[SceneElement, ...]:
    """Shift fragment keyframes (authored from frame 0) into a page span.

    Preset tracks live in a bare 0-based frame space.  When a fragment lands
    on page N its keyframes must start at that page's global start frame —
    otherwise the timeline shows the animation playing during page 1 while
    the element itself belongs to page N.  Frames are clamped to the page
    span as a safety net; callers that extend the page first (see
    ``_instantiate_preset_at``) never reach the clamp.
    """
    start = max(0, int(page_start))
    span_end = max(start + 1, int(page_end))

    def shifted(element: SceneElement) -> SceneElement:
        if not any(track.keyframes for track in element.local_animation_tracks):
            return element
        tracks = tuple(
            _rebuild_track(track, [
                replace(keyframe, frame=max(start, min(span_end - 1, keyframe.frame + start)))
                for keyframe in track.keyframes
            ])
            for track in element.local_animation_tracks)
        return replace(element, local_animation_tracks=tracks)

    return tuple(shifted(element) for element in elements)


def fragment_max_keyframe_frame(elements: Iterable[SceneElement]) -> int:
    """Latest keyframe frame carried by the fragment (-1 when static)."""
    frames = [
        keyframe.frame
        for element in elements
        for track in element.local_animation_tracks
        for keyframe in track.keyframes
    ]
    return max(frames, default=-1)


def _normalize_time_origin(elements: Iterable[SceneElement]) -> tuple[SceneElement, ...]:
    """Shift every track so the fragment's earliest keyframe sits at frame 0.

    AUD-28301: host tracks live in the scene's global frame space, so a
    selection animated mid-timeline would otherwise be saved with its
    keyframes still parked at host-global frames — while the insert path
    (``retime_fragment_to_page`` adds the target page's start frame and
    ``fragment_max_keyframe_frame`` sizes the page extension) assumes a bare
    0-based preset space.  A fragment-wide offset (not per-element) keeps
    the relative stagger between elements intact.
    """
    materialized = tuple(elements)
    offset = min(
        (keyframe.frame
         for element in materialized
         for track in element.local_animation_tracks
         for keyframe in track.keyframes),
        default=0,
    )
    if offset <= 0:
        return materialized
    return tuple(
        replace(element, local_animation_tracks=tuple(
            _rebuild_track(track, [
                replace(keyframe, frame=keyframe.frame - offset)
                for keyframe in track.keyframes
            ])
            for track in element.local_animation_tracks))
        for element in materialized)


def _descendants_of(ids: Iterable[str], parents: dict[str, str]) -> set[str]:
    keep = set(ids)
    grew = True
    while grew:
        grew = False
        for child, parent in parents.items():
            if parent in keep and child not in keep:
                keep.add(child)
                grew = True
    return keep


def extract_fragment(host_scene: TeachingSceneDocument, element_ids: Sequence[str]) -> dict:
    """Scene-JSON snapshot of the selected elements (+ their descendants).

    The snapshot is translated so its bbox starts at (0, 0) — and, since
    host tracks live in global frame space, retimed so its earliest
    keyframe sits at frame 0; scene-level animations that target kept
    elements are folded into the elements' local tracks so the saved
    preset grafts cleanly anywhere.  Returns a dict
    accepted by ``TeachingSceneDocument.from_dict`` / ``validate_scene_json``.
    """
    hierarchy = read_hierarchy(host_scene.metadata)
    parents = dict(hierarchy.parents)
    keep = _descendants_of(
        {element_id for element_id in element_ids if host_scene.element_by_id(element_id) is not None},
        parents)
    if not keep:
        raise ValueError("no selectable elements for the preset fragment")
    kept_elements = [element for element in host_scene.elements if element.element_id in keep]
    min_x = min(element.transform.x for element in kept_elements)
    min_y = min(element.transform.y for element in kept_elements)
    by_element: dict[str, list[KeyframeTrack]] = {}
    for animation in host_scene.animations:
        if animation.element_id in keep:
            by_element.setdefault(animation.element_id, []).append(animation.track)
    elements: list[SceneElement] = []
    for element in kept_elements:
        transform = replace(element.transform, x=element.transform.x - min_x,
                            y=element.transform.y - min_y)
        extras = by_element.get(element.element_id)
        tracks = tuple(element.local_animation_tracks) + tuple(extras) if extras else element.local_animation_tracks
        elements.append(replace(element, transform=transform,
                                local_animation_tracks=tracks,
                                z_index=element.z_index))
    # AUD-28301: retime to a 0-based preset frame space after folding the
    # scene-level tracks in, so both local and folded keyframes normalise.
    elements = list(_normalize_time_origin(elements))
    inner_parents = {
        child: parent for child, parent in parents.items()
        if child in keep and parent in keep
    }
    fragment = TeachingSceneDocument(
        scene_id=f"preset_{uuid.uuid4().hex}",
        schema_version=host_scene.schema_version,
        name=host_scene.name,
        width=host_scene.width, height=host_scene.height,
        fps_num=host_scene.fps_num, fps_den=host_scene.fps_den,
        duration_frames=host_scene.duration_frames,
        background=dict(host_scene.background), elements=tuple(elements),
        animations=(),
        metadata=write_hierarchy_into({}, Hierarchy(
            parents=inner_parents, locked=frozenset(), solo=frozenset(), hidden=frozenset(),
            schema_version=hierarchy.schema_version)),
    )
    return fragment.to_dict()


__all__ = [
    "extract_fragment", "fragment_bounds", "fragment_max_keyframe_frame",
    "prepare_insert", "retime_fragment_to_page",
]
