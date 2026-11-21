"""Rational layer-time contract (B03).

The UI and persisted projects use inclusive composition frames
(``[in_frame, out_frame]``); pure functions here work on half-open
intervals internally.  Frame rates are exact fractions — ``fps_fraction``
recovers NTSC-style rates (``30000/1001``) from persisted floats — so
cross-frame-rate edits (comp 25 / source 24) never drift through float
arithmetic.  The same contract feeds slide/trim/slip/split/stretch plans
and the shared-read-node diagnostics.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from fractions import Fraction
from typing import Any, Dict, Iterable, List, Optional, Tuple


def fps_fraction(value: Any) -> Fraction:
    """Parse an exact frame rate from int/float/str/tuple persistence.

    Floats are recovered with a bounded denominator (1001 covers NTSC);
    ``"30000/1001"`` strings and ``(30000, 1001)`` tuples parse exactly.
    Unusable values return ``Fraction(0)`` (callers treat 0 as unknown).
    """
    try:
        if isinstance(value, Fraction):
            return value
        if isinstance(value, (tuple, list)) and len(value) == 2:
            return Fraction(int(value[0]), int(value[1]))
        if isinstance(value, str):
            text = value.strip()
            if "/" in text:
                num, _, den = text.partition("/")
                return Fraction(int(num), int(den))
            value = float(text)
        if isinstance(value, bool):
            return Fraction(0)
        if isinstance(value, int):
            return Fraction(value)
        if isinstance(value, float):
            if not value or value != value or value in (float("inf"), float("-inf")):
                return Fraction(0)
            return Fraction(value).limit_denominator(1001)
    except (TypeError, ValueError, ZeroDivisionError, OverflowError):
        return Fraction(0)
    return Fraction(0)


def half_open_range(in_frame: int, out_frame_inclusive: int) -> Tuple[int, int]:
    """UI inclusive tail → internal half-open ``[in, end_exclusive)``."""
    return int(in_frame), int(out_frame_inclusive) + 1


def inclusive_range(in_frame: int, end_exclusive: int) -> Tuple[int, int]:
    """Internal half-open interval → UI inclusive ``[in, out]``."""
    return int(in_frame), int(end_exclusive) - 1


def source_delta_for_comp_delta(comp_delta: int, comp_fps: Any, source_fps: Any,
                                rate: Any = 1) -> int:
    """Exact source-frame delta for a composition-frame delta (floor).

    Floor semantics match :func:`source_frame_cfr`: the sampled frame is
    the last frame whose window opened by the playhead position.
    """
    comp = fps_fraction(comp_fps)
    source = fps_fraction(source_fps)
    if comp <= 0 or source <= 0:
        return int(comp_delta)
    ratio = source * fps_fraction(rate) / comp
    scaled = Fraction(int(comp_delta)) * ratio
    return scaled.numerator // scaled.denominator


def source_frame_cfr(comp_frame: int, *, in_frame: int, end_exclusive: int,
                     source_origin: Any, comp_fps: Any, source_fps: Any,
                     rate: Any = Fraction(1)) -> Optional[int]:
    """Map a composition frame to a CFR source frame (constant rate).

    Returns None outside the half-open ``[in_frame, end_exclusive)`` layer
    range.  ``source_origin`` may be a fraction for non-integer anchors.
    Reverse and VFR mappings live in their own adapters (:class:`TimeRemap`
    and the timestamp index) and share this frame contract.
    """
    comp_frame = int(comp_frame)
    if not int(in_frame) <= comp_frame < int(end_exclusive):
        return None
    comp = fps_fraction(comp_fps)
    source = fps_fraction(source_fps)
    if comp <= 0 or source <= 0:
        return None
    delta = (Fraction(comp_frame - int(in_frame))
             * source * fps_fraction(rate) / comp)
    position = fps_fraction(source_origin) + delta
    return position.numerator // position.denominator


# ── Layer timing state and edit plans ───────────────────────────────────────


@dataclass(frozen=True)
class LayerTiming:
    """One layer's time state: comp bounds (inclusive) + source window.

    ``source_out < 0`` means "to the end of the material".  FPS values are
    exact fractions (``0`` = unknown, edits then degrade to a 1:1 frame
    delta and are flagged non-fps-aware in the plan).
    """

    in_frame: int
    out_frame: int
    source_in: int = 0
    source_out: int = -1
    comp_fps: Any = Fraction(24)
    source_fps: Any = Fraction(24)

    @property
    def fps_aware(self) -> bool:
        return fps_fraction(self.comp_fps) > 0 and fps_fraction(self.source_fps) > 0

    def source_frame_at(self, comp_frame: int) -> Optional[int]:
        """The source frame sampled at a composition frame."""
        if not self.fps_aware:
            position = self.source_in + (int(comp_frame) - self.in_frame)
            return position if self.in_frame <= int(comp_frame) <= self.out_frame else None
        in_f, end = half_open_range(self.in_frame, self.out_frame)
        return source_frame_cfr(
            int(comp_frame), in_frame=in_f, end_exclusive=end,
            source_origin=Fraction(self.source_in),
            comp_fps=self.comp_fps, source_fps=self.source_fps)

    def with_fps(self, comp_fps: Any, source_fps: Any) -> "LayerTiming":
        return replace(self, comp_fps=fps_fraction(comp_fps),
                       source_fps=fps_fraction(source_fps))


@dataclass(frozen=True)
class LayerTimePlan:
    """Result of one pure layer-time edit (B03 §3).

    ``layer`` is the edited instance; ``peer`` carries the second (right)
    instance produced by a split.  ``keyframe_shift`` is the composition
    frame translation the host applies to the layer's own animation; a
    split moves the right instance's animation by ``peer_keyframe_shift``.
    """

    kind: str  # slide | trim_in | trim_out | slip | split | stretch
    layer: LayerTiming
    peer: Optional[LayerTiming] = None
    keyframe_shift: int = 0
    peer_keyframe_shift: int = 0
    shared_asset_id: str = ""
    fps_aware: bool = True
    notes: Tuple[str, ...] = ()


def plan_slide(timing: LayerTiming, delta: int) -> LayerTimePlan:
    """Whole-layer move: comp bounds and own animation shift together.

    The source window is untouched — the material moves with the layer.
    """
    delta = int(delta)
    return LayerTimePlan(
        kind="slide",
        layer=replace(timing, in_frame=timing.in_frame + delta,
                      out_frame=timing.out_frame + delta),
        keyframe_shift=delta,
        fps_aware=timing.fps_aware,
    )


def plan_trim_in(timing: LayerTiming, new_in: int) -> LayerTimePlan:
    """Head trim: visible bound moves, keeping the same source frames.

    Advancing the head by N composition frames advances the sampled source
    window by the exact cross-rate equivalent (comp 25 / source 24: 25 comp
    frames → 24 source frames), never by the raw comp delta.
    """
    new_in = int(new_in)
    delta = new_in - timing.in_frame
    source_delta = (source_delta_for_comp_delta(delta, timing.comp_fps,
                                                timing.source_fps)
                    if timing.fps_aware else delta)
    notes: Tuple[str, ...] = ()
    if not timing.fps_aware:
        notes = ("source fps unknown; trim applied with a 1:1 frame delta",)
    return LayerTimePlan(
        kind="trim_in",
        layer=replace(timing, in_frame=new_in,
                      source_in=max(0, timing.source_in + source_delta)),
        fps_aware=timing.fps_aware,
        notes=notes,
    )


def plan_trim_out(timing: LayerTiming, new_out: int) -> LayerTimePlan:
    """Tail trim: duration shrinks, keeping the cut's source frame exact."""
    new_out = int(new_out)
    old_duration = timing.out_frame - timing.in_frame + 1
    new_duration = new_out - timing.in_frame + 1
    if timing.fps_aware:
        s_old = source_delta_for_comp_delta(old_duration, timing.comp_fps,
                                            timing.source_fps)
        s_new = source_delta_for_comp_delta(new_duration, timing.comp_fps,
                                            timing.source_fps)
        if timing.source_out >= 0:
            new_source_out = max(timing.source_in,
                                 timing.source_out + (s_new - s_old))
        else:
            # Open end: only a shrinking tail needs an explicit bound.
            new_source_out = (-1 if new_duration >= old_duration
                              else timing.source_in + s_new - 1)
    else:
        s_new = new_duration
        new_source_out = (timing.source_out if timing.source_out >= 0
                          else (-1 if new_duration >= old_duration
                                else timing.source_in + s_new - 1))
    notes: Tuple[str, ...] = ()
    if not timing.fps_aware:
        notes = ("source fps unknown; trim applied with a 1:1 frame delta",)
    return LayerTimePlan(
        kind="trim_out",
        layer=replace(timing, out_frame=new_out, source_out=new_source_out),
        fps_aware=timing.fps_aware,
        notes=notes,
    )


def plan_slip(timing: LayerTiming, slip_delta: int) -> LayerTimePlan:
    """Slip: composition bounds stay; the sampled window moves in place."""
    slip_delta = int(slip_delta)
    source_delta = (source_delta_for_comp_delta(slip_delta, timing.comp_fps,
                                                timing.source_fps)
                    if timing.fps_aware else slip_delta)
    new_source_in = max(0, timing.source_in + source_delta)
    if timing.source_out >= 0:
        new_source_out = max(new_source_in, timing.source_out + source_delta)
    else:
        new_source_out = -1
    return LayerTimePlan(
        kind="slip",
        layer=replace(timing, source_in=new_source_in,
                      source_out=new_source_out),
        fps_aware=timing.fps_aware,
    )


def plan_split(timing: LayerTiming, cut_frame: int,
               asset_id: str = "") -> LayerTimePlan:
    """Split: the cut frame belongs to the right instance (B03 §3).

    Two layer instances share the same source asset; each holds its own
    time window.  The right instance's subsequent animation moves with it
    (``peer_keyframe_shift``); the left keeps the original anchor.
    """
    cut = int(cut_frame)
    if not timing.in_frame < cut <= timing.out_frame:
        raise ValueError(
            f"Split cut {cut} must lie inside ({timing.in_frame}, "
            f"{timing.out_frame}].")
    left_source_out = timing.source_frame_at(cut - 1)
    right_source_in = timing.source_frame_at(cut)
    if left_source_out is None or right_source_in is None:
        raise ValueError("Split cut outside the layer's time range.")
    # Keep an open tail open on the left when the original had one; an
    # explicit bound is only kept when the original window already ended.
    left = replace(timing, out_frame=cut - 1,
                   source_out=left_source_out if timing.source_out >= 0 else -1)
    right = replace(timing, in_frame=cut, source_in=right_source_in)
    return LayerTimePlan(
        kind="split",
        layer=left,
        peer=right,
        keyframe_shift=0,
        peer_keyframe_shift=cut - timing.in_frame,
        shared_asset_id=str(asset_id or ""),
        fps_aware=timing.fps_aware,
    )


def plan_stretch(timing: LayerTiming, new_duration: int) -> LayerTimePlan:
    """Stretch: same source window, new playback rate over the bounds."""
    old_duration = timing.out_frame - timing.in_frame + 1
    if old_duration <= 0:
        raise ValueError("Stretch needs a positive layer duration.")
    new_duration = int(new_duration)
    if new_duration <= 0:
        raise ValueError("Stretch needs a positive target duration.")
    rate = Fraction(new_duration, old_duration)
    return LayerTimePlan(
        kind="stretch",
        layer=replace(timing, out_frame=timing.in_frame + new_duration - 1),
        keyframe_shift=0,
        fps_aware=timing.fps_aware,
        notes=(f"rate={rate.numerator}/{rate.denominator}",),
    )


# ── Shared read-node diagnostics (B03 §1 / §4) ─────────────────────────────


def shared_read_node_conflicts(layer_stacks: Iterable[Any],
                               get_layer: Any,
                               get_node: Any = None
                               ) -> List[Dict[str, Any]]:
    """Report read nodes claimed by several layers with divergent ranges.

    A single read node cannot serve two layers at different times: the
    frame overrides and layer gates intersect and both layers lose frames.
    Each conflict names the node, the claiming layers and their ranges so
    the host can offer an independent read node per layer (same asset id,
    shared decode cache).
    """
    claims: Dict[str, List[Any]] = {}
    for stack in layer_stacks or ():
        for layer_id in getattr(stack, "layer_ids", []) or []:
            layer = get_layer(layer_id) if callable(get_layer) else None
            if layer is None:
                continue
            source_id = getattr(layer, "source_node_id", None)
            if not source_id:
                continue
            if get_node is not None and get_node(source_id) is None:
                continue
            claims.setdefault(str(source_id), []).append(layer)

    conflicts: List[Dict[str, Any]] = []
    for source_id, layers in sorted(claims.items()):
        if len(layers) < 2:
            continue
        ranges = {(int(getattr(l, "in_frame", 0) or 0),
                   int(getattr(l, "out_frame", -1) or -1)) for l in layers}
        if len(ranges) <= 1:
            continue
        conflicts.append({
            "code": "layer.shared_source_range",
            "node_id": source_id,
            "layer_ids": [str(l.layer_id) for l in layers],
            "ranges": sorted(ranges),
        })
    return conflicts
