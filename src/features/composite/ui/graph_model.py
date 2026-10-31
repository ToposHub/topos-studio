"""Graph Editor pure-data model (T102/T103/T104).

The Keyframes panel's right side can switch between a Timeline canvas and
a Graph canvas (plan §22.1). This module holds the GUI-free data the Graph
canvas paints, mirroring the pattern the existing spline_model + timeline
modules established: pure dataclasses + builder functions, no QWidget /
QPainter, fully unit-testable headlessly.

Why a separate model (not reusing spline_model):

* spline_model.SplineCurve is value-graph only and is owned by the Spline
  panel; the Graph Editor must also represent Speed graphs, multi-channel
  vec2/vec3 properties, pre/post-expression samples and a viewport that
  differs from the Spline's. Folding all that into SplineCurve would
  break the Spline panel's existing contract.
* the Graph Editor reads from the SAME authoritative source
  (node.keyframe_tracks / node._expressions) via a builder, so the two
  surfaces never diverge — they're just two projections of one truth.

Plan §22.3 defines the dataclass shapes. The builder (build_graph_series)
takes a node + param name (+ an optional expression evaluator) and returns
a GraphSeries; the Graph Editor view (a later batch) consumes it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, List, Optional, Tuple

logger = logging.getLogger(__name__)


# ── graph type selection (T104) ────────────────────────────────────────
# Auto-select per plan §19.6 / §22.4: spatial params (Position/Anchor/
# Point of Interest) default to Speed graph; scalar/temporal params
# (Opacity/Blur/Rotation/Color) default to Value graph. The user can
# override via the Graph Type combo (T110); the auto decision lives here
# so it's testable without a widget.

# Param-name substrings that mark a property as spatial. Matched
# case-insensitively against the param name. Tuned to the node PARAM_DEFS
# the project ships (Transform translate/anchor, Constant, Ramp point,
# etc.). New spatial params should be added here.
_SPATIAL_NAME_HINTS: Tuple[str, ...] = (
    "translate", "position", "anchor", "pivot", "point_of_interest",
    "point of interest", "origin", "center",
)


def is_spatial_param(param_name: str) -> bool:
    """Return True if ``param_name`` refers to a spatial (2D/3D point)
    property.

    Spatial params default to the Speed graph (plan §19.6); everything
    else defaults to Value. The check is on the param NAME because the
    node PARAM_DEFS don't carry a 'spatial' flag today — adding one is a
    separate task. Name-matching covers the project's spatial params
    (Transform translate_x/y, Anchor, Ramp point, …) without false-
    positives on scalar params.
    """
    if not param_name:
        return False
    name = param_name.lower()
    return any(hint in name for hint in _SPATIAL_NAME_HINTS)


def auto_graph_type(param_name: str) -> str:
    """T104: pick the default graph type for a param.

    Returns 'speed' for spatial params, 'value' for everything else
    (including unknown params — plan §19.6 'unknown → value graph, 并给出
    可切换提示'). The user can still override via the Graph Type combo.
    """
    return "speed" if is_spatial_param(param_name) else "value"


# ── T118: Separate Dimensions ─────────────────────────────────────────
#
# The project stores vector params as SPLIT SCALARS (translate_x /
# translate_y, scale_x / scale_y, …), never as a single vec2 track. So
# "separating dimensions" isn't a data-model split — X and Y are already
# independent tracks. T118's job is channel METADATA so the Graph Editor
# can group sibling components, colour them by axis, and offer a
# "Separate/Link Dimensions" affordance. These pure-data helpers detect
# a scalar param's axis channel and find its sibling components.


# Suffixes that mark a scalar param as one axis of a vector property.
# Matched case-insensitively against the param-name TAIL. This is the single
# source of truth used by param_channel / param_base_name (BUG-4 fix: the
# constant was previously dead code while the functions hard-coded their own
# copies). Only x/y/z are supported — the project models vectors as split
# scalars and never animates a 4th component as an axis channel.
_AXIS_SUFFIXES: Tuple[str, ...] = ("_x", "_y", "_z")


def param_channel(param_name: str) -> Optional[str]:
    """T118: detect a scalar param's axis channel ('x' | 'y' | 'z').

    Returns None for params that aren't an axis component (e.g. ``rotation``,
    ``opacity``). Recognises the project's split-scalar convention
    (``translate_x`` → 'x', ``scale_y`` → 'y') as well as a bare ``x``/``y``/
    ``z`` tail. The check is on the tail so ``translate_x`` matches but
    ``max_xy`` does not.
    """
    if not param_name:
        return None
    name = param_name.lower()
    for suf in _AXIS_SUFFIXES:
        if name.endswith(suf):
            return suf[-1]
    # Bare single-letter tail (e.g. param 'x' on a custom node).
    if name in ("x", "y", "z"):
        return name
    return None


def param_base_name(param_name: str) -> str:
    """T118: strip the axis suffix to get the vector property's base name.

    ``translate_x`` → ``translate``, ``scale_y`` → ``scale``, ``rotation``
    → ``rotation`` (no axis → returned unchanged). Used to group sibling
    components (everything sharing a base name is one vector property)."""
    if not param_name:
        return param_name
    name = param_name.lower()
    for suf in _AXIS_SUFFIXES:
        if name.endswith(suf):
            return param_name[: -len(suf)]
    return param_name


def find_sibling_channels(param_name: str,
                          all_params: List[str]) -> List[str]:
    """T118: return the sibling component param names for ``param_name``.

    Given ``translate_x`` and the node's full param list
    ``['translate_x', 'translate_y', 'rotation']``, returns
    ``['translate_x', 'translate_y']`` — the components of the same vector
    property, in X/Y/Z order. ``param_name`` itself is always included.
    Returns ``[param_name]`` when there are no siblings (a true scalar).
    """
    base = param_base_name(param_name)
    ch = param_channel(param_name)
    if ch is None:
        return [param_name]
    # Siblings share the same base name and have a recognised axis suffix.
    axis_order = {"x": 0, "y": 1, "z": 2}
    siblings = []
    for p in all_params:
        if param_base_name(p).lower() == base.lower() and \
                param_channel(p) is not None:
            siblings.append(p)
    # De-duplicate preserving order, then sort by axis.
    seen = set()
    unique = []
    for p in siblings:
        if p not in seen:
            seen.add(p)
            unique.append(p)
    unique.sort(key=lambda p: axis_order.get(param_channel(p) or "", 9))
    return unique


def is_separable_param(param_name: str, all_params: List[str]) -> bool:
    """T118: True when ``param_name`` belongs to a multi-component vector
    property (i.e. it has at least one sibling on a different axis). This
    is the gate for showing the 'Separate Dimensions' affordance."""
    sibs = find_sibling_channels(param_name, all_params)
    if len(sibs) < 2:
        return False
    axes = {param_channel(p) for p in sibs}
    return len(axes) >= 2


# Fixed channel colours (plan §19.6: X 红/橙、Y 绿、Z 蓝). Only the three
# axes param_channel can return are listed — a 4th-component key was removed
# (BUG-14) because param_channel never returns 'w' (the project models RGBA
# alpha as a scalar, not an axis channel), so the key was unreachable and
# misleading. Re-add 'w' only if param_channel is extended to recognise _w.
_CHANNEL_COLORS = {
    "x": "#ff8a3d",   # orange-red
    "y": "#3dd97a",   # green
    "z": "#3d9cff",   # blue
}


def channel_color(channel: Optional[str], fallback: str = "#bdbdbd") -> str:
    """T118: the fixed display colour for a channel axis. Falls back to
    ``fallback`` when the channel is None (a true scalar) or unknown."""
    if channel is None:
        return fallback
    return _CHANNEL_COLORS.get(channel.lower(), fallback)


# ── dataclasses (plan §22.3) ───────────────────────────────────────────


@dataclass
class GraphPoint:
    """One keyframe point on a graph series.

    ``speed`` is None for value graphs (or when not yet computed); for
    speed graphs it's the per-frame speed at this keyframe. ``channel`` is
    None for scalar params and 'x'/'y'/'z' for vector components.
    """
    row_id: str
    node_id: str
    parameter_name: str
    channel: Optional[str]
    frame: float
    value: float
    speed: Optional[float] = None
    keyframe_index: Optional[int] = None
    selected: bool = False
    interpolation_in: str = "linear"
    interpolation_out: str = "linear"
    handle_left: Optional[Tuple[float, float]] = None
    handle_right: Optional[Tuple[float, float]] = None
    tangent_mode: str = "smooth"


@dataclass
class GraphSeries:
    """One plotted curve on the graph editor.

    A scalar param produces one series; a vec2/vec3 param produces one
    series per channel (each with its own colour). ``samples`` is the
    densely-sampled curve (frame, value) the painter draws between
    keyframes; ``pre_expression_samples`` / ``post_expression_samples``
    hold the two layers an expression-driven series can show (plan §22.5).
    """
    row_id: str
    display_name: str
    channel: Optional[str]
    color: str
    is_spatial: bool
    has_keyframes: bool
    has_expression: bool
    graph_type: str = "value"
    points: List[GraphPoint] = field(default_factory=list)
    samples: List[Tuple[float, float]] = field(default_factory=list)
    pre_expression_samples: List[Tuple[float, float]] = field(default_factory=list)
    post_expression_samples: List[Tuple[float, float]] = field(default_factory=list)
    # T119: the expression string (None when the param has no expression).
    # Captured at build time so the view can re-evaluate the post curve as
    # the viewport / playhead moves without re-reading the node.
    expression: Optional[str] = None
    # T119: set True when the last post-curve evaluation had errors.
    expression_error: bool = False
    # T119: the param's static (non-animated) value — the pre-curve baseline
    # for an expression-only param (which has no keyframes). Read from the
    # node's parameters dict at build time.
    static_value: float = 0.0


@dataclass
class GraphViewport:
    """The graph editor's view rectangle in data coordinates.

    ``pixels_per_frame`` / ``pixels_per_value`` convert data → screen.
    ``auto_zoom_*`` refit the bounds when the data changes (or when a drag
    pushes a handle outside the current bounds). ``graph_type`` is the
    active projection ('value' | 'speed'); 'auto' resolves through
    auto_graph_type at build time.
    """
    frame_start: float = 0.0
    frame_end: float = 100.0
    value_min: float = 0.0
    value_max: float = 1.0
    pixels_per_frame: float = 4.0
    pixels_per_value: float = 1.0
    auto_zoom_time: bool = True
    auto_zoom_value: bool = True
    graph_type: str = "auto"


@dataclass
class GraphOptions:
    """Toggleable Graph Editor overlays (plan §22.3)."""
    show_reference_graph: bool = False
    show_post_expression_graph: bool = False
    show_expression_editor: bool = False
    show_transform_box: bool = False
    show_layer_in_out: bool = True
    show_markers: bool = True
    separate_dimensions: bool = False
    snap_enabled: bool = False


# ── viewport helpers (T105/T106) ───────────────────────────────────────


def value_to_y(value: float, viewport: GraphViewport,
               graph_bottom: float) -> float:
    """T105: convert a data value to a screen y pixel (Value graph).

    ``graph_bottom`` is the pixel y of the value_min line. Inverts the
    y axis (higher value = smaller y, screen-down = value-down).
    """
    ppv = max(1e-9, viewport.pixels_per_value)
    return graph_bottom - (value - viewport.value_min) * ppv


def y_to_value(y: float, viewport: GraphViewport,
               graph_bottom: float) -> float:
    """Inverse of :func:`value_to_y`."""
    ppv = max(1e-9, viewport.pixels_per_value)
    return viewport.value_min + (graph_bottom - y) / ppv


def frame_to_x(frame: float, viewport: GraphViewport,
               graph_left: float) -> float:
    """T105: convert a data frame to a screen x pixel."""
    return graph_left + (frame - viewport.frame_start) * \
        max(1e-9, viewport.pixels_per_frame)


def x_to_frame(x: float, viewport: GraphViewport,
               graph_left: float) -> float:
    """Inverse of :func:`frame_to_x`."""
    return viewport.frame_start + (x - graph_left) / \
        max(1e-9, viewport.pixels_per_frame)


def compute_value_bounds(samples: List[Tuple[float, float]],
                         points: List[GraphPoint]) -> Tuple[float, float]:
    """Compute the value_min / value_max for a series, with 8-12 % padding.

    Plan §22.4: 'value bounds 默认由可见 keyframe 和采样值计算，并留 8%-12%
    上下边距'. Uses 10 % padding (the midpoint). Returns (0.0, 1.0) when
    there's no data so the viewport stays sane.
    """
    vals: List[float] = [s[1] for s in samples] + [p.value for p in points]
    vals = [v for v in vals if isinstance(v, (int, float))]
    if not vals:
        return 0.0, 1.0
    lo = min(vals)
    hi = max(vals)
    if hi == lo:
        # Single distinct value — give it a small visible band around it.
        band = max(1.0, abs(lo) * 0.1)
        return lo - band, hi + band
    pad = (hi - lo) * 0.1
    return lo - pad, hi + pad


# ── T107: curve sampling ───────────────────────────────────────────────


def sample_value_curve(points: List[GraphPoint],
                       frame_start: float, frame_end: float,
                       pixels_per_frame: float,
                       fps: float = 24.0
                       ) -> List[Tuple[float, float]]:
    """Sample a value-graph curve between [frame_start, frame_end].

    Plan §22.5: sample density is ~2-4 px per sample (not one-per-frame),
    keyframe frames are forced into the sample set so Hold jumps / handle
    corners aren't lost. Interpolation per segment delegates to the
    AUTHORITATIVE renderer math (``keyframe.interpolate`` /
    ``bezier_value_at_frame`` / ``ease_*_interpolate``) so the curve the
    user edits is exactly the curve the renderer produces — no third
    sampling implementation that could drift (C-1 fix):

      * linear → straight line between the two keyframes;
      * hold   → flat at the left keyframe's value, then a vertical jump
                 at the right keyframe (speed 0 in between, plan §22.4);
      * bezier → 2D cubic Bezier with x(t)=frame solved by bisection
                 (matches ``bezier_value_at_frame`` exactly);
      * ease_* → the renderer's exact easing curves
                 (``ease_in/out/in_out_interpolate``), not a bezier
                 approximation.

    Returns a list of (frame, value) samples dense enough to paint a smooth
    curve without aliasing. Empty when there are <2 keyframes.
    """
    if len(points) < 2:
        return []
    # Sort by frame so segment iteration is well-defined regardless of the
    # input order (defensive — build_graph_series already preserves order).
    ks = sorted(points, key=lambda p: p.frame)
    # Sample step: aim for ~3 px between samples. Clamp to >= 0.25 frame so
    # an extremely zoomed-out view doesn't produce millions of samples.
    step_frames = max(0.25, 3.0 / max(1e-9, pixels_per_frame))

    # AUD-9905: clip WHILE sampling instead of generating the full keyframe
    # span and filtering afterwards — cost now scales with the visible
    # window, not the data span. The skip-loops below advance ``f`` with the
    # same repeated addition as before, so the emitted sample sequence is
    # bit-identical to the old generate-then-filter output.
    if frame_end > frame_start:
        lo, hi = float(frame_start), float(frame_end)
    else:
        lo, hi = float("-inf"), float("inf")

    samples: List[Tuple[float, float]] = []
    # Always include the first keyframe's frame as a sample anchor.
    if lo <= ks[0].frame <= hi:
        samples.append((ks[0].frame, ks[0].value))

    for i in range(len(ks) - 1):
        k0 = ks[i]
        k1 = ks[i + 1]
        seg_interp = (k0.interpolation_out or k0.interpolation_in
                      or "linear").lower()
        if k0.frame >= k1.frame:
            continue  # degenerate; skip
        if k1.frame < lo or k0.frame > hi:
            continue  # entire segment outside the visible window
        if seg_interp == "hold":
            # Flat at k0.value across [k0.frame, k1.frame), then the jump
            # to k1.value happens AT k1.frame. Emit the flat samples.
            f = k0.frame
            while f < lo:
                f += step_frames  # skip the out-of-window prefix
            while f < k1.frame and f <= hi:
                samples.append((f, k0.value))
                f += step_frames
            # Force the endpoint so the vertical jump is crisp.
            if lo <= k1.frame <= hi:
                samples.append((k1.frame, k0.value))
                samples.append((k1.frame, k1.value))
            continue
        # linear / bezier / ease_* → delegate to the authoritative renderer
        # math so the editor preview and the render interpolation agree.
        # We sample at sub-frame density (step_frames may be < 1), so we call
        # the FLOAT-friendly entry points (bezier_value_at_frame takes float;
        # linear/ease are t-parameterized) rather than interpolate() which
        # rounds frame to int and would lose sub-frame detail.
        kf0, kf1 = _to_authoritative_keyframes(k0, k1, seg_interp)
        from src.shared.keyframe import (
            bezier_value_at_frame, ease_in_interpolate,
            ease_out_interpolate, ease_in_out_interpolate,
        )
        f = k0.frame + step_frames
        while f < lo:
            f += step_frames  # skip the out-of-window prefix
        while f < k1.frame and f <= hi:
            v = _sample_segment_value(f, kf0, kf1, seg_interp,
                                      bezier_value_at_frame,
                                      ease_in_interpolate,
                                      ease_out_interpolate,
                                      ease_in_out_interpolate)
            samples.append((f, v))
            f += step_frames
        if lo <= k1.frame <= hi:
            samples.append((k1.frame, k1.value))

    return samples


def _interp_type_for(seg_interp: str):
    """Map a GraphPoint interpolation string to the renderer's
    InterpolationType enum (C-1: Graph now uses the renderer's exact math)."""
    from src.shared.keyframe import InterpolationType
    mapping = {
        "linear": InterpolationType.LINEAR,
        "bezier": InterpolationType.BEZIER,
        "ease_in": InterpolationType.EASE_IN,
        "ease_out": InterpolationType.EASE_OUT,
        "ease_in_out": InterpolationType.EASE_IN_OUT,
        "hold": InterpolationType.HOLD,
    }
    return mapping.get(seg_interp, InterpolationType.LINEAR)


def _to_authoritative_keyframes(k0: GraphPoint, k1: GraphPoint,
                                seg_interp: str):
    """Build two renderer ``Keyframe`` objects from GraphPoints so the
    graph sampler can delegate to the AUTHORITATIVE interpolate() math
    (C-1 fix). Handles carry straight through as absolute data-space
    deltas (the same convention the renderer's bezier_value_at_frame
    expects via _resolve_handle)."""
    from src.shared.keyframe import Keyframe, InterpolationType
    itp = _interp_type_for(seg_interp)
    kf0 = Keyframe(
        frame=int(round(k0.frame)),
        value=float(k0.value),
        interpolation=itp,
        handle_left=k0.handle_left,
        handle_right=k0.handle_right,
    )
    # The segment's interpolation is driven by kf0 (the left keyframe),
    # matching the renderer's interpolate() contract — kf1's interpolation
    # only governs the NEXT segment, so a neutral LINEAR default is fine.
    kf1 = Keyframe(
        frame=int(round(k1.frame)),
        value=float(k1.value),
        interpolation=InterpolationType.LINEAR,
        handle_left=k1.handle_left,
        handle_right=k1.handle_right,
    )
    return kf0, kf1


def _sample_segment_value(frame: float, kf0, kf1, seg_interp: str,
                          bezier_value_at_frame,
                          ease_in_fn, ease_out_fn, ease_in_out_fn) -> float:
    """C-1: evaluate one segment's value at a (possibly sub-frame) float
    frame using the AUTHORITATIVE renderer math. Dispatch mirrors the
    renderer's interpolate() but stays in float space:

      * linear → straight line (float t);
      * bezier → bezier_value_at_frame (takes float frame, solves x(t));
      * ease_* → the renderer's exact easing curve at float t.

    No clamping is applied — the graph must show the true curve shape,
    even above 1 / below 0 (the renderer clamps to the param range, but
    that's a render-time concern, not an editor-preview one)."""
    df = kf1.frame - kf0.frame
    if df <= 0:
        return kf0.value
    if seg_interp == "bezier":
        return bezier_value_at_frame(frame, kf0, kf1)
    t = (frame - kf0.frame) / df
    if seg_interp == "ease_in":
        return ease_in_fn(t, kf0, kf1)
    if seg_interp == "ease_out":
        return ease_out_fn(t, kf0, kf1)
    if seg_interp == "ease_in_out":
        return ease_in_out_fn(t, kf0, kf1)
    # linear (default)
    return kf0.value + t * (kf1.value - kf0.value)


# ── T106: speed computation ────────────────────────────────────────────


def compute_speeds(points: List[GraphPoint], fps: float = 24.0
                   ) -> List[float]:
    """T106: compute the per-keyframe speed for a Speed Graph.

    Speed is the average rate of value change over the segment STARTING at
    each keyframe (plan §22.4: 'speed = dv / (dt_frames / fps)'). The last
    keyframe has no following segment, so its speed mirrors the previous
    one's (AE shows a flat tail). Hold segments have speed 0.

    Returns a list aligned with ``points`` (same length, same order) —
    AUD-9908: the input is sorted by frame internally, but each computed
    speed is mapped back to its ORIGINAL position, so unsorted input no
    longer pairs a speed with the wrong keyframe.
    """
    if not points:
        return []
    # Sort indices (not points) so results can be scattered back into the
    # caller's original ordering.
    order = sorted(range(len(points)), key=lambda i: points[i].frame)
    speeds_sorted: List[float] = []
    for pos, i in enumerate(order):
        k = points[i]
        if pos == len(order) - 1:
            # No following segment — reuse the prior speed (or 0 if alone).
            speeds_sorted.append(speeds_sorted[-1] if speeds_sorted else 0.0)
            continue
        k1 = points[order[pos + 1]]
        dt_frames = k1.frame - k.frame
        if dt_frames <= 0:
            speeds_sorted.append(0.0)
            continue
        seg_interp = (k.interpolation_out or k.interpolation_in
                      or "linear").lower()
        if seg_interp == "hold":
            speeds_sorted.append(0.0)
            continue
        dv = k1.value - k.value
        dt_seconds = dt_frames / max(1e-9, fps)
        speeds_sorted.append(dv / dt_seconds if dt_seconds > 0 else 0.0)
    # Scatter back into the caller's original ordering (AUD-9908).
    speeds: List[float] = [0.0] * len(points)
    for pos, i in enumerate(order):
        speeds[i] = speeds_sorted[pos]
    return speeds


def sample_speed_curve(points: List[GraphPoint],
                       frame_start: float, frame_end: float,
                       pixels_per_frame: float,
                       fps: float = 24.0
                       ) -> List[Tuple[float, float]]:
    """T106: sample a speed-graph curve (frame, speed).

    Each segment's speed is constant (linear) or 0 (hold); bezier segments
    are approximated as their average speed for the editor (the renderer's
    exact per-frame speed is the source of truth). This keeps the speed
    graph readable without a full derivative evaluator.
    """
    if len(points) < 2:
        return []
    ks = sorted(points, key=lambda p: p.frame)
    step_frames = max(0.25, 3.0 / max(1e-9, pixels_per_frame))
    # AUD-9905: clip while sampling (same phase-preserving skip as
    # sample_value_curve) so cost scales with the visible window.
    if frame_end > frame_start:
        lo, hi = float(frame_start), float(frame_end)
    else:
        lo, hi = float("-inf"), float("inf")
    samples: List[Tuple[float, float]] = []
    for i in range(len(ks) - 1):
        k0 = ks[i]
        k1 = ks[i + 1]
        if k0.frame >= k1.frame:
            continue
        if k1.frame < lo or k0.frame > hi:
            continue  # entire segment outside the visible window
        seg_interp = (k0.interpolation_out or k0.interpolation_in
                      or "linear").lower()
        if seg_interp == "hold":
            speed = 0.0
        else:
            dt_seconds = (k1.frame - k0.frame) / max(1e-9, fps)
            dv = k1.value - k0.value
            speed = dv / dt_seconds if dt_seconds > 0 else 0.0
        f = k0.frame
        while f < lo:
            f += step_frames  # skip the out-of-window prefix
        while f < k1.frame and f <= hi:
            samples.append((f, speed))
            f += step_frames
        if lo <= k1.frame <= hi:
            samples.append((k1.frame, speed))
    return samples


# ── T122: selection summary ────────────────────────────────────────────


def summarize_selection(series: List[GraphSeries],
                        fps: float = 24.0) -> dict:
    """T122: build the footer's Graph Info summary for the current selection.

    Returns a dict with: property, frame, value, speed, interpolation.
    When exactly one keyframe is selected across all series, the summary is
    that keyframe's details. With zero or many selected, the 'frame' and
    'value' fields are None and only 'property' / count are populated — the
    footer then shows a count instead of a single point's readout.

    Speed is computed from the segment STARTING at the selected keyframe
    (plan §22.4); None when the keyframe is the last (no following segment).
    """
    sel_points: List[Tuple[GraphSeries, GraphPoint]] = []
    for s in series:
        for kp in s.points:
            if kp.selected:
                sel_points.append((s, kp))
    out = {
        "property": None,
        "frame": None,
        "value": None,
        "speed": None,
        "interpolation": None,
        "count": len(sel_points),
    }
    if not sel_points:
        return out
    if len(sel_points) == 1:
        s, kp = sel_points[0]
        out["property"] = s.display_name
        out["frame"] = int(kp.frame)
        out["value"] = float(kp.value)
        out["interpolation"] = kp.interpolation_in
        # Speed from the following segment.
        ks = sorted(s.points, key=lambda p: p.frame)
        idx = next((i for i, p in enumerate(ks) if p is kp), -1)
        if 0 <= idx < len(ks) - 1:
            nxt = ks[idx + 1]
            if (kp.interpolation_out or kp.interpolation_in).lower() != "hold":
                dt_seconds = (nxt.frame - kp.frame) / max(1e-9, fps)
                if dt_seconds > 0:
                    out["speed"] = (nxt.value - kp.value) / dt_seconds
                else:
                    out["speed"] = 0.0
            else:
                out["speed"] = 0.0
        return out
    # Multiple selected — show the count + the first selected property.
    out["property"] = sel_points[0][0].display_name
    return out


# ── T115/T117: speed-graph drag + transform-box math (pure data) ──────


def is_speed_drag(series) -> bool:
    """T115: in a Speed Graph, dragging a keyframe only moves its time —
    the Y axis is derived (slope/speed), so editing the point's value would
    mis-edit the original keyframe. Returns True when the series (or any
    selected series in a list) is showing the speed projection."""
    if isinstance(series, (list, tuple)):
        return any(getattr(s, "graph_type", "value") == "speed"
                   for s in series)
    return getattr(series, "graph_type", "value") == "speed"


def selection_bbox(series: List[GraphSeries]) -> Optional[Tuple[float, float,
                                                                float, float]]:
    """T117: axis-aligned bounding box of every selected keyframe across
    every series. Returns (frame_min, frame_max, value_min, value_max) or
    None when fewer than two distinct frames are selected (a box needs a
    non-zero time span; two same-frame points in different series/channels
    still don't form a usable box).

    Plan §22.9: 'box 的 left/right 是选中 frame min/max, top/bottom 是选中
    value/speed min/max'. We use the raw keyframe value (not the sampled
    curve) so the box hugs the draggable points exactly.
    """
    sel = [kp for s in series for kp in s.points if kp.selected]
    if not sel:
        return None
    frames = [kp.frame for kp in sel]
    fmin, fmax = min(frames), max(frames)
    # Need a non-degenerate time span for edge/corner scaling to be meaningful.
    if fmax - fmin < 1e-9:
        return None
    values = [kp.value for kp in sel]
    return (fmin, fmax, min(values), max(values))


def transform_box_anchor(bbox: Tuple[float, float, float, float]
                         ) -> Tuple[float, float]:
    """T117: the default anchor is the box centre. The user can drag the
    anchor elsewhere (plan §22.9); this just computes the initial centre."""
    fmin, fmax, vmin, vmax = bbox
    return ((fmin + fmax) / 2.0, (vmin + vmax) / 2.0)


def apply_move_transform(points: List[GraphPoint], delta_frame: int,
                         delta_value: float,
                         allow_value: bool = True) -> List[Tuple[GraphPoint,
                                                                 float, float]]:
    """T117 (internal drag): move every point by (delta_frame, delta_value).
    Returns a list of (point, new_frame, new_value) WITHOUT mutating the
    points — the caller applies them so it can snapshot/restore on Esc.

    When ``allow_value`` is False (cross-parameter or speed-graph move) the
    value component is dropped, preserving the plan §22.9 rule that a
    cross-parameter move can only shift time, never rescale values.
    """
    out: List[Tuple[GraphPoint, float, float]] = []
    dv = delta_value if allow_value else 0.0
    for kp in points:
        # Frames are integers; negative keyframe frames are forbidden.
        nf = max(0, int(round(kp.frame + delta_frame)))
        nv = float(kp.value + dv)
        out.append((kp, float(nf), nv))
    return out


def apply_scale_transform(points: List[GraphPoint],
                          bbox: Tuple[float, float, float, float],
                          anchor: Tuple[float, float],
                          scale_time: float, scale_value: float,
                          allow_value: bool = True
                          ) -> List[Tuple[GraphPoint, float, float]]:
    """T117 (edge/corner drag): scale every point's (frame, value) around
    ``anchor``. ``scale_time`` / ``scale_value`` are multipliers (1.0 = no
    change). When ``allow_value`` is False the value component is frozen
    (cross-parameter scaling is forbidden — plan §22.9). Returns the
    proposed (point, new_frame, new_value) list without mutating.

    The scaling is anchor-relative (nf = af + (frame - af) * scale_time), so
    a zero bbox span can't cause division-by-zero here — the divisors are the
    cursor-to-anchor distances, which the caller (_do_box_scale_drag) clamps
    via max(1e-9, ...). The bbox arg is accepted for API symmetry with
    apply_move_transform and future edge-mode variants."""
    af, av = anchor
    out: List[Tuple[GraphPoint, float, float]] = []
    sv = scale_value if allow_value else 1.0
    for kp in points:
        # Frame: anchor-relative scale, rounded to integer, clamped ≥ 0.
        nf = max(0, int(round(af + (kp.frame - af) * scale_time)))
        # Value: anchor-relative scale (frozen when allow_value is False).
        nv = av + (kp.value - av) * sv
        out.append((kp, float(nf), float(nv)))
    return out


# ── T119: pre/post-expression curves (pure data) ──────────────────────


def sample_expression_curves(points: List[GraphPoint],
                             expression: str,
                             frame_start: float, frame_end: float,
                             pixels_per_frame: float = 1.0,
                             fps: float = 24.0,
                             static_value: float = 0.0,
                             context: Optional[Any] = None,
                             parser: Optional[Any] = None,
                             ) -> Tuple[List[Tuple[float, float]],
                                        List[Tuple[float, float]], bool]:
    """T119: sample the pre- and post-expression curves for a param.

    Returns (pre_samples, post_samples, has_error):

      * ``pre_samples`` — the keyframe/static evaluation (what the param
        WOULD be without the expression). When the param has keyframes this
        is the same as ``sample_value_curve``; when it has none it's a flat
        line at ``static_value`` (plan §22.11 'pre-expression curve =
        keyframe/static evaluation').
      * ``post_samples`` — the expression's result at each sample frame
        (plan §22.11 'post-expression curve = final property result'). On
        evaluation error the post curve falls back to the pre curve and
        ``has_error`` is True so the view can draw a red error marker.
      * ``has_error`` — True when the expression failed for at least one
        sample (the view shows a fallback + error glyph).

    ``context`` is an ``ExpressionContext`` whose ``frame``/``time``/
    ``current_value`` are updated per-sample; pass None to skip expression
    evaluation (post curve == pre curve, has_error False — used for the
    pre-only view when 'Show Post-Expression Graph' is off). ``parser`` is
    an ``ExpressionParser``; when None a fresh one is constructed lazily.
    """
    # Pre curve: keyframe evaluation, or a flat static line.
    if len(points) >= 2:
        pre = sample_value_curve(points, frame_start, frame_end,
                                 pixels_per_frame, fps=fps)
    else:
        # No/bare keyframes → flat line at the static value across the range.
        pre = [(float(frame_start), float(static_value)),
               (float(frame_end), float(static_value))]

    # No context or no expression → post mirrors pre (pre-only view).
    if not expression or context is None:
        return pre, list(pre), False

    # Build a parser lazily and evaluate per-sample.
    if parser is None:
        try:
            from src.features.composite.utils.expression_parser import \
                ExpressionParser
            parser = ExpressionParser()
        except Exception as exc:
            # AUD-9906: the evaluator being unavailable must NOT look like
            # a healthy no-expression curve — the user wrote an expression
            # and sees it silently ignored. Flag has_error so the view
            # draws its error marker and log the cause (previously this
            # fell back silently with has_error=False).
            logger.warning(
                "Graph expression evaluator unavailable (%r); the "
                "post-expression curve falls back to the pre curve",
                exc)
            return pre, list(pre), True

    post: List[Tuple[float, float]] = []
    has_error = False
    # Sample the post curve at the same frames as the pre curve so the two
    # align pixel-for-pixel. Where pre has a sample, post gets one too.
    for frame, pre_value in pre:
        # Update the per-frame context fields the expression can reference.
        # Only a context object missing these attributes is expected to
        # fail; anything else is a real bug and should surface.
        try:
            context.frame = int(round(frame))
            context.time = frame / max(1e-9, fps)
            context.current_value = float(pre_value)
        except AttributeError:
            pass
        try:
            result = parser.evaluate(expression, context)
        except (SyntaxError, ValueError, KeyError, TypeError,
                ZeroDivisionError, OverflowError):
            # The parser's documented failure space (it normally converts
            # these to a failed ExpressionResult itself). Genuine
            # implementation bugs outside this set propagate instead of
            # being masked as a per-frame expression error.
            result = None
        if result is not None and getattr(result, "success", False) and \
                getattr(result, "value", None) is not None:
            post.append((frame, float(result.value)))
        else:
            # Evaluation failed at this frame → fall back to the pre value
            # and flag the error so the view draws a marker.
            post.append((frame, float(pre_value)))
            has_error = True
    return pre, post, has_error


# ── builder (T103) ─────────────────────────────────────────────────────


def build_graph_series(node: Any, param_name: str,
                       node_color: str = "#bdbdbd",
                       graph_type: str = "auto") -> GraphSeries:
    """T103: build a GraphSeries from a node's keyframe track.

    Reads ``node.keyframe_tracks[param_name]`` (the authoritative source,
    per plan §5.1) and turns each keyframe into a GraphPoint. The series'
    ``is_spatial`` / ``graph_type`` are resolved from the param name when
    ``graph_type == 'auto'``.

    The builder does NOT sample the curve between keyframes — that's a
    separate step (T107, cache + evaluator) because it depends on the
    active viewport. Here we just produce the keyframe points + metadata.
    """
    has_expression = bool(
        getattr(node, "_expressions", {}) and
        param_name in getattr(node, "_expressions", {}))
    # T119: capture the expression string so the view can re-evaluate the
    # post curve without re-reading the node.
    expr_str = None
    if has_expression:
        try:
            expr_str = str(getattr(node, "_expressions", {})[param_name])
        except Exception:
            expr_str = None
    track_data = None
    try:
        tracks = getattr(node, "keyframe_tracks", {}) or {}
        track_data = tracks.get(param_name)
    except Exception:
        track_data = None

    is_spatial = is_spatial_param(param_name)
    resolved_type = (graph_type if graph_type in ("value", "speed")
                     else auto_graph_type(param_name))
    # T118: detect this scalar's axis channel so the Graph Editor can group
    # sibling components and colour them by axis.
    channel = param_channel(param_name)

    points: List[GraphPoint] = []
    if isinstance(track_data, dict):
        kfs = track_data.get("keyframes", []) or []
        for idx, kf in enumerate(kfs):
            if not isinstance(kf, dict):
                continue
            # AUD-9907: keyframe dicts come from the deserialized project
            # JSON (external data). A missing/None/non-numeric frame or
            # value must skip that keyframe instead of letting
            # TypeError/ValueError escape into the graph-editor build path
            # (the surrounding reads all have try/except; only these two
            # conversions were bare).
            try:
                frame = float(kf.get("frame", 0))
                value = float(kf.get("value", 0.0))
            except (TypeError, ValueError):
                logger.warning(
                    "Graph series %s.%s: skipping malformed keyframe %d "
                    "(frame=%r, value=%r)",
                    getattr(node, "id", "?"), param_name, idx,
                    kf.get("frame"), kf.get("value"))
                continue
            interp = str(kf.get("interpolation", "linear"))
            # Handle/tangent fields are preserved verbatim (plan §22.3).
            hl = kf.get("handle_left") or kf.get("bezier_in")
            hr = kf.get("handle_right") or kf.get("bezier_out")
            points.append(GraphPoint(
                row_id=f"param:{node.id}:{param_name}",
                node_id=node.id,
                parameter_name=param_name,
                channel=channel,
                frame=frame,
                value=value,
                speed=None,
                keyframe_index=idx,
                interpolation_in=interp,
                interpolation_out=interp,
                handle_left=tuple(hl) if isinstance(hl, (list, tuple))
                             and len(hl) == 2 else None,
                handle_right=tuple(hr) if isinstance(hr, (list, tuple))
                              and len(hr) == 2 else None,
                tangent_mode=str(kf.get("tangent_mode", "smooth")),
            ))

    display = param_name.replace("_", " ").title()
    # T118: colour by axis channel when the caller didn't override the
    # default grey (plan §19.6 fixed channel colours).
    # NOTE (BUG-15): "#bdbdbd" is the documented default for node_color and
    # doubles as the "not specified" sentinel. A node whose header colour is
    # deliberately set to this exact grey would be re-coloured by its channel
    # — an acceptable trade-off since #bdbdbd is reserved as the neutral
    # default across the timeline (see keyframes_model._channel_color_for).
    series_color = node_color
    if channel is not None and node_color == "#bdbdbd":
        series_color = channel_color(channel)
    # T119: capture the param's static value (pre-curve baseline for an
    # expression-only param, which has no keyframes).
    static_val = 0.0
    try:
        params = getattr(node, "parameters", {}) or {}
        raw = params.get(param_name)
        if isinstance(raw, (int, float)):
            static_val = float(raw)
    except Exception:
        static_val = 0.0
    return GraphSeries(
        row_id=f"param:{node.id}:{param_name}",
        display_name=display,
        channel=channel,
        color=series_color,
        is_spatial=is_spatial,
        has_keyframes=bool(points),
        has_expression=has_expression,
        graph_type=resolved_type,
        points=points,
        expression=expr_str,
        static_value=static_val,
    )


__all__ = [
    # dataclasses
    "GraphPoint", "GraphSeries", "GraphViewport", "GraphOptions",
    # type selection
    "is_spatial_param", "auto_graph_type",
    # T118 separate dimensions
    "param_channel", "param_base_name", "find_sibling_channels",
    "is_separable_param", "channel_color",
    # coordinate transforms
    "value_to_y", "y_to_value", "frame_to_x", "x_to_frame",
    "compute_value_bounds",
    # sampling (T106/T107/T119)
    "sample_value_curve", "sample_speed_curve", "compute_speeds",
    "sample_expression_curves",
    # summary (T122)
    "summarize_selection",
    # speed-graph drag + transform-box math (T115/T117)
    "is_speed_drag", "selection_bbox", "transform_box_anchor",
    "apply_move_transform", "apply_scale_transform",
    # builder
    "build_graph_series",
]
