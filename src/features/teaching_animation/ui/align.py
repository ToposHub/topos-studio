"""Multi-select align / distribute (UX-08 phase-3a).

Pure geometry over a selection of elements — no Qt, no scene mutation — so it
is unit-testable in isolation.  The editor window turns the result into one
``CompositeCommand`` of per-element ``UpdateElementCommand``\\ s (single undo).

Convention (matching ``SceneTransform``): ``x, y`` is the element's top-left;
the effective box is ``[x, x + width*scale_x] × [y, y + height*scale_y]``.
Only position changes — width/height/scale/rotation are preserved.  Elements
already at the target position are still returned (the window may choose to
skip no-ops); the caller needs ≥2 elements for anything meaningful and ≥3 for
distribute to have interior spacing.
"""

from __future__ import annotations

from typing import Mapping

from ..core.model import SceneElement, SceneTransform


ALIGN_MODES = frozenset({
    "align_left", "align_center_h", "align_right",
    "align_top", "align_middle_v", "align_bottom",
    "distribute_h", "distribute_v",
})


def _box(el: SceneElement) -> tuple[float, float, float, float, float, float]:
    """Return (left, top, width, height, right, bottom) of the element's box."""
    w = el.transform.width * el.transform.scale_x
    h = el.transform.height * el.transform.scale_y
    left = el.transform.x
    top = el.transform.y
    return left, top, w, h, left + w, top + h


def compute_alignment(elements: list[SceneElement] | tuple[SceneElement, ...],
                      mode: str) -> dict[str, tuple[float, float]]:
    """Return ``{element_id: (new_x, new_y)}`` for the given align mode.

    Returns ``{}`` for fewer than 2 elements (nothing to align).  Raises
    ``ValueError`` for an unknown mode.
    """
    if mode not in ALIGN_MODES:
        raise ValueError(f"unknown align mode: {mode!r}")
    els = list(elements)
    if len(els) < 2:
        return {}
    box = {el.element_id: _box(el) for el in els}
    result: dict[str, tuple[float, float]] = {}

    if mode == "align_left":
        target = min(b[0] for b in box.values())
        result = {el.element_id: (target, el.transform.y) for el in els}
    elif mode == "align_right":
        target_r = max(b[4] for b in box.values())
        result = {el.element_id: (target_r - box[el.element_id][2], el.transform.y) for el in els}
    elif mode == "align_center_h":
        center = (min(b[0] for b in box.values()) + max(b[4] for b in box.values())) / 2
        result = {el.element_id: (center - box[el.element_id][2] / 2, el.transform.y) for el in els}
    elif mode == "align_top":
        target = min(b[1] for b in box.values())
        result = {el.element_id: (el.transform.x, target) for el in els}
    elif mode == "align_bottom":
        target_b = max(b[5] for b in box.values())
        result = {el.element_id: (el.transform.x, target_b - box[el.element_id][3]) for el in els}
    elif mode == "align_middle_v":
        center = (min(b[1] for b in box.values()) + max(b[5] for b in box.values())) / 2
        result = {el.element_id: (el.transform.x, center - box[el.element_id][3] / 2) for el in els}
    elif mode in ("distribute_h", "distribute_v"):
        horizontal = mode == "distribute_h"
        # Sort by center along the axis; evenly space centers between first/last.
        def _center(el: SceneElement) -> float:
            b = box[el.element_id]
            return b[0] + b[2] / 2 if horizontal else b[1] + b[3] / 2
        order = sorted(els, key=_center)
        centers = [_center(el) for el in order]
        first, last = centers[0], centers[-1]
        step = (last - first) / (len(order) - 1)
        for i, el in enumerate(order):
            b = box[el.element_id]
            new_center = first + step * i
            if horizontal:
                result[el.element_id] = (new_center - b[2] / 2, el.transform.y)
            else:
                result[el.element_id] = (el.transform.x, new_center - b[3] / 2)
    return result


def build_alignment_updates(
    elements: list[SceneElement] | tuple[SceneElement, ...],
    mode: str,
) -> dict[str, Mapping[str, SceneTransform]]:
    """Convenience: turn ``compute_alignment`` into per-element transform updates.

    Returns ``{element_id: {"transform": new_transform}}`` ready to feed
    ``UpdateElementCommand``.  No-op positions (unchanged) are omitted so the
    resulting ``CompositeCommand`` contains only real changes.
    """
    positions = compute_alignment(elements, mode)
    updates: dict[str, Mapping[str, SceneTransform]] = {}
    by_id = {el.element_id: el for el in elements}
    for eid, (nx, ny) in positions.items():
        el = by_id[eid]
        if (nx, ny) == (el.transform.x, el.transform.y):
            continue
        updates[eid] = {"transform": SceneTransform(
            x=nx, y=ny, width=el.transform.width, height=el.transform.height,
            scale_x=el.transform.scale_x, scale_y=el.transform.scale_y,
            rotation=el.transform.rotation)}
    return updates


__all__ = ["ALIGN_MODES", "compute_alignment", "build_alignment_updates"]
