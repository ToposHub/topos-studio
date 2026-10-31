"""Bridge between the HTML page deck and first-class scene pages.

``scene.html_layer`` stays the rendering backend (browser page deck, export).
``scene.pages`` is the model-level page list every editor surface reads
(Layers, Timeline, page navigation).  Whenever the html layer changes — new
source, edited page timing, patched sections — :func:`sync_pages_with_html_layer`
regenerates the pages from the detected sections so both views agree.

Native-only scenes never pass through here; their pages are managed with the
page commands (add/remove/update/reorder/move).
"""

from __future__ import annotations

from dataclasses import replace

from .html_page_deck import detect_page_count, page_start_frame, page_timing_for
from .model import ScenePage, TeachingSceneDocument
from src.shared.keyframe import KeyframeTrack


def page_frame_ranges(scene: TeachingSceneDocument) -> dict[str, tuple[int, int]]:
    """Return half-open global frame ranges for every scene page.

    Page durations are authored in page order, while animation tracks remain
    stored in the scene's global frame space.  Keeping this conversion in one
    pure helper prevents the editor, renderer and page commands from deriving
    slightly different boundaries.
    """

    starts = scene.page_start_frames()
    return {
        page.page_id: (
            starts[index],
            max(starts[index] + 1, starts[index] + page.duration_frames),
        )
        for index, page in enumerate(scene.pages)
    }


def _retime_track(
    track: KeyframeTrack,
    old_range: tuple[int, int],
    new_range: tuple[int, int],
) -> KeyframeTrack:
    """Move a page-local track to its new global range without losing metadata."""

    if not track.keyframes or old_range == new_range:
        return track
    result = KeyframeTrack.from_dict(track.to_dict())
    old_start, _old_end = old_range
    new_start, new_end = new_range
    remapped = []
    for keyframe in result.keyframes:
        local_frame = int(keyframe.frame) - old_start
        frame = max(new_start, min(new_end - 1, new_start + local_frame))
        remapped.append(replace(keyframe, frame=frame))
    result.keyframes = []
    result.add_keyframes_bulk(remapped)
    return result


def retime_page_tracks(
    before: TeachingSceneDocument,
    after: TeachingSceneDocument,
) -> TeachingSceneDocument:
    """Keep element animation at the same local time when pages move.

    Tracks are still serialized in global scene-frame units for compatibility.
    Page commands call this helper whenever page order, duration or ownership
    changes.  Keyframes are translated by their old/new page starts and are
    clamped to the destination page instead of leaking into another page.
    """

    old_ranges = page_frame_ranges(before)
    new_ranges = page_frame_ranges(after)
    old_elements = {element.element_id: element for element in before.elements}
    changed_elements = []
    for element in after.elements:
        old_element = old_elements.get(element.element_id)
        old_range = old_ranges.get(old_element.page_id) if old_element else None
        new_range = new_ranges.get(element.page_id)
        tracks = element.local_animation_tracks
        if old_range is not None and new_range is not None:
            tracks = tuple(_retime_track(track, old_range, new_range) for track in tracks)
        changed_elements.append(
            element if tracks == element.local_animation_tracks
            else replace(element, local_animation_tracks=tracks)
        )

    changed_animations = []
    for animation in after.animations:
        old_element = old_elements.get(animation.element_id)
        new_element = next(
            (element for element in after.elements if element.element_id == animation.element_id),
            None,
        )
        old_range = old_ranges.get(old_element.page_id) if old_element else None
        new_range = new_ranges.get(new_element.page_id) if new_element else None
        track = animation.track
        if old_range is not None and new_range is not None:
            track = _retime_track(track, old_range, new_range)
        changed_animations.append(
            animation if track == animation.track else replace(animation, track=track)
        )

    return replace(
        after,
        elements=tuple(changed_elements),
        animations=tuple(changed_animations),
    )


def resize_scene_pages(
    scene: TeachingSceneDocument,
    duration_frames: int,
) -> TeachingSceneDocument:
    """Resize the page sequence while preserving earlier page durations.

    The final page absorbs ordinary duration changes.  If the requested total
    is shorter than the prefix, pages are reduced from the end but never below
    one frame; the page model therefore remains valid.
    """

    target = max(len(scene.pages), int(duration_frames))
    if not scene.pages:
        return scene.with_duration(target)
    durations = [page.duration_frames for page in scene.pages]
    delta = target - sum(durations)
    if delta >= 0:
        durations[-1] += delta
    else:
        remaining = -delta
        for index in range(len(durations) - 1, -1, -1):
            reducible = durations[index] - 1
            reduction = min(reducible, remaining)
            durations[index] -= reduction
            remaining -= reduction
            if remaining == 0:
                break
    pages = tuple(
        replace(page, duration_frames=durations[index])
        for index, page in enumerate(scene.pages)
    )
    return replace(scene, pages=pages, duration_frames=sum(durations))


def _unique_page_id(used: set[str], base: str) -> str:
    candidate = base
    serial = 1
    while candidate in used:
        serial += 1
        candidate = f"{base}_{serial}"
    return candidate


def sync_pages_with_html_layer(
    scene: TeachingSceneDocument,
    *,
    duration_frames: int | None = None,
) -> TeachingSceneDocument:
    """Regenerate ``scene.pages`` from the html layer's detected sections.

    Page ids and authored names survive while the section count is stable, so
    element assignments and renames are not lost across timing edits.  When
    the section count shrinks, elements on dropped pages move to the last
    surviving page.  The scene duration is untouched — the last page absorbs
    any rounding difference, mirroring the HTML span behaviour.

    ``duration_frames`` overrides the total used for the last page's span
    (e.g. a timing command that runs before the scene-duration command inside
    a composite).
    """
    layer = scene.html_layer
    if layer is None:
        return scene
    total = scene.duration_frames if duration_frames is None else max(1, int(duration_frames))
    count = max(1, detect_page_count(layer.sanitized_html))
    timing = page_timing_for(
        layer.metadata,
        page_count=count,
        fallback_default_seconds=(total / scene.fps) / count,
    )
    starts = [page_start_frame(index, scene.fps, timing) for index in range(count)]
    previous = scene.pages
    used: set[str] = set()
    pages: list[ScenePage] = []
    for index in range(count):
        start = starts[index]
        end = starts[index + 1] if index + 1 < count else total
        if index < len(previous):
            old = previous[index]
            page_id, name, background = old.page_id, old.name, old.background
        else:
            page_id = _unique_page_id(used, f"page_{index + 1}")
            name, background = "", ""
        used.add(page_id)
        pages.append(ScenePage(
            page_id=page_id,
            name=name,
            duration_frames=max(1, end - start),
            background=background,
        ))
    elements = scene.elements
    if count < len(previous):
        last_id = pages[-1].page_id
        elements = tuple(
            element if element.page_id in used
            else replace(element, page_id=last_id)
            for element in elements
        )
    return retime_page_tracks(
        scene,
        replace(scene, pages=tuple(pages), elements=elements),
    )


__all__ = [
    "page_frame_ranges", "retime_page_tracks", "resize_scene_pages",
    "sync_pages_with_html_layer",
]
