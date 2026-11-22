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
from math import ceil, floor
from typing import TYPE_CHECKING, Any, Dict, Iterable, List, Mapping, Optional, Tuple

if TYPE_CHECKING:
    from src.shared.media.presentation_time import PresentationIndex


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


def source_frame_step(comp_fps: Any, source_fps: Any) -> Fraction:
    """Source frames per composition frame at normal playback speed."""
    comp = fps_fraction(comp_fps)
    source = fps_fraction(source_fps)
    return source / comp if comp > 0 and source > 0 else Fraction(1)


def source_window_duration(source_in: int, source_out: int, *,
                           step: Fraction, phase: Fraction = Fraction(0)) -> int:
    """Number of composition frames covered by an inclusive source window."""
    if source_out < source_in or step <= 0:
        return 0
    return max(0, ceil((source_out - source_in + 1 - phase) / step))


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
    source_phase: Fraction = Fraction(0)
    rate: Fraction = Fraction(1)
    presentation_index: PresentationIndex | None = None
    source_time_origin: Fraction | None = None

    @property
    def fps_aware(self) -> bool:
        return (fps_fraction(self.comp_fps) > 0
                and (self.presentation_index is not None or fps_fraction(self.source_fps) > 0))

    @property
    def source_step(self) -> Fraction:
        """Source-frame distance between adjacent composition frames."""
        return self.rate * source_frame_step(self.comp_fps, self.source_fps)

    def source_position_at(self, comp_frame: int) -> Fraction:
        """Exact source position, including the subframe phase of the anchor."""
        if self.presentation_index is not None:
            return Fraction(self.presentation_index.frame_at(self.source_time_at(comp_frame)))
        return (self.source_in + self.source_phase
                + (int(comp_frame) - self.in_frame) * self.source_step)

    def source_time_at(self, comp_frame: int) -> Fraction:
        """Elapsed source time at a composition frame on the VFR path."""
        origin = self.source_time_origin
        if origin is None:
            if self.presentation_index is None:
                raise ValueError("A presentation index is required for source timestamps.")
            origin = self.presentation_index.seconds_at(self.source_in)
        fps = fps_fraction(self.comp_fps)
        return origin + Fraction(comp_frame - self.in_frame) * self.rate / fps

    def reanchored_at(self, sample_frame: int, in_frame: int) -> LayerTiming:
        """Preserve the exact presentation phase when moving a source anchor."""
        if self.presentation_index is not None:
            source_time = max(Fraction(0), self.source_time_at(sample_frame))
            return replace(self, in_frame=in_frame,
                           source_in=self.presentation_index.frame_at(source_time),
                           source_time_origin=source_time)
        source_origin = max(Fraction(0), self.source_position_at(sample_frame))
        source_in = floor(source_origin)
        return replace(self, in_frame=in_frame, source_in=source_in,
                       source_phase=source_origin - source_in)

    def source_frame_at(self, comp_frame: int) -> Optional[int]:
        """The source frame sampled at a visible composition frame."""
        if not self.in_frame <= int(comp_frame) <= self.out_frame:
            return None
        return floor(self.source_position_at(comp_frame))

    def with_fps(self, comp_fps: Any, source_fps: Any) -> "LayerTiming":
        return replace(self, comp_fps=fps_fraction(comp_fps),
                       source_fps=fps_fraction(source_fps))

    def window_duration(self, source_out: int) -> int:
        """Composition frames covered by the media window at this rate."""
        if self.presentation_index is not None and self.rate > 0:
            interval = (self.presentation_index.seconds_at(source_out + 1)
                        - self.source_time_at(self.in_frame))
            return max(0, ceil(interval * self.comp_fps / self.rate))
        return source_window_duration(
            self.source_in, source_out, step=self.source_step, phase=self.source_phase)

    def sampling_metadata(self) -> dict[str, str]:
        """Serialize timing without storing a file-specific index in the project."""
        data = {"source_step": str(self.source_step), "source_phase": str(self.source_phase)}
        if self.presentation_index is not None:
            data.update(source_rate=str(self.rate),
                        source_time_origin=str(self.source_time_at(self.in_frame)))
        return data


def layer_timing_from_video(
    bounds: tuple[int, int], params: Mapping[str, Any], time_range: Mapping[str, Any],
    comp_fps: Any, presentation_index: PresentationIndex | None = None,
) -> LayerTiming:
    """Resolve the saved sample clock for both layer commands and the timeline."""
    comp = fps_fraction(comp_fps)
    source = fps_fraction(params.get("fps"))
    nominal = source_frame_step(comp, source)
    step = fps_fraction(time_range.get("source_step")) or nominal
    rate = fps_fraction(time_range.get("source_rate")) or step / nominal
    origin = (fps_fraction(time_range["source_time_origin"])
              if "source_time_origin" in time_range else None)
    return LayerTiming(
        *bounds, source_in=int(params.get("source_in", 0) or 0),
        source_out=int(params.get("source_out", -1)),
        comp_fps=comp, source_fps=source, source_phase=fps_fraction(time_range.get("source_phase")),
        rate=rate, presentation_index=presentation_index if comp > 0 else None,
        source_time_origin=origin,
    )


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
    notes: Tuple[str, ...] = ()
    if not timing.fps_aware:
        notes = ("source fps unknown; trim applied with a 1:1 frame delta",)
    return LayerTimePlan(
        kind="trim_in",
        layer=timing.reanchored_at(new_in, new_in),
        fps_aware=timing.fps_aware,
        notes=notes,
    )


def plan_trim_out(timing: LayerTiming, new_out: int) -> LayerTimePlan:
    """Tail trim: duration shrinks, keeping the cut's source frame exact."""
    new_out = int(new_out)
    last_frame = max(timing.source_in, floor(timing.source_position_at(new_out)))
    new_source_out = timing.source_out
    if new_out < timing.out_frame:
        new_source_out = last_frame
        if timing.source_out >= 0:
            new_source_out = min(timing.source_out, new_source_out)
    elif new_out > timing.out_frame and timing.source_out >= 0:
        new_source_out = max(timing.source_out, last_frame)
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
    slipped = timing.reanchored_at(timing.in_frame + slip_delta, timing.in_frame)
    if timing.source_out >= 0:
        if timing.presentation_index is not None:
            new_source_out = floor(slipped.source_position_at(timing.out_frame))
        else:
            new_source_out = timing.source_out + slipped.source_in - timing.source_in
    else:
        new_source_out = -1
    return LayerTimePlan(
        kind="slip",
        layer=replace(slipped, source_out=new_source_out),
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
    if left_source_out is None:
        raise ValueError("Split cut outside the layer's time range.")
    # Keep an open tail open on the left when the original had one; an
    # explicit bound is only kept when the original window already ended.
    left = replace(timing, out_frame=cut - 1,
                   source_out=left_source_out if timing.source_out >= 0 else -1)
    right = timing.reanchored_at(cut, cut)
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
    rate = timing.rate * Fraction(old_duration, new_duration)
    return LayerTimePlan(
        kind="stretch",
        layer=replace(timing, out_frame=timing.in_frame + new_duration - 1,
                      rate=rate),
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
