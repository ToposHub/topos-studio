"""SplineModel — pure-data layer for the Spline curve editor (phase 3).

Turns per-node keyframe_tracks into SplineCurve / SplineSegment / SplineKey
structures the SplineView paints, plus the cubic-bezier math (§11.4-11.7).

Design principles (per plan §11.2-11.3):
- Handles use DATA-SPACE absolute coordinates (frame, value) internally.
- Conversion to screen happens only in the view, never in the model.
- All math is in pure functions that are unit-tested independently.
- The model reads the same authoritative node.keyframe_tracks as the
  Keyframes panel — it never duplicates animation state.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from PyQt6.QtCore import QPointF

_log = logging.getLogger(__name__)


# ── Interpolation / tangent vocabulary ──────────────────────────────────

# A segment's interpolation controls how it is drawn + sampled.
INTERP_LINEAR = "linear"
INTERP_BEZIER = "bezier"
INTERP_HOLD = "hold"
INTERP_EASE_IN = "ease_in"
INTERP_EASE_OUT = "ease_out"
INTERP_EASE_IN_OUT = "ease_in_out"

# Tangent mode of a key controls handle behaviour.
TANGENT_LINEAR = "linear"
TANGENT_FLAT = "flat"
TANGENT_SMOOTH = "smooth"
TANGENT_BROKEN = "broken"
TANGENT_AUTO = "auto"
TANGENT_STEP = "step"


# ── Data structures (§11.2) ─────────────────────────────────────────────


@dataclass
class SplineKey:
    """One keyframe point on a curve, in data space (frame, value)."""
    frame: int
    value: float
    interpolation: str = INTERP_LINEAR
    tangent_mode: str = TANGENT_SMOOTH
    # Absolute data-space handle points (NOT screen pixels, NOT normalized).
    handle_left: QPointF = field(default_factory=lambda: QPointF(0.0, 0.0))
    handle_right: QPointF = field(default_factory=lambda: QPointF(0.0, 0.0))
    selected: bool = False

    def to_dict(self) -> Dict[str, Any]:
        """Serialize to the authoritative Keyframe.to_dict() shape (batch 7).

        Writes handle_left/handle_right (data-space deltas) + tangent_mode,
        matching the on-disk format Keyframe.to_dict produces. bezier_in/out
        are kept as legacy fallback for older readers.
        """
        return {
            "frame": self.frame,
            "value": self.value,
            "interpolation": self.interpolation,
            "tangent_mode": self.tangent_mode,
            "handle_left": [self.handle_left.x() - self.frame,
                            self.handle_left.y() - self.value],
            "handle_right": [self.handle_right.x() - self.frame,
                             self.handle_right.y() - self.value],
            # Legacy compatibility for older project readers.
            "bezier_in": [self.handle_left.x() - self.frame,
                          self.handle_left.y() - self.value],
            "bezier_out": [self.handle_right.x() - self.frame,
                           self.handle_right.y() - self.value],
        }


@dataclass
class SplineSegment:
    """One cubic-bezier segment between two adjacent keys (§11.4)."""
    p0: QPointF   # = K0
    p1: QPointF   # = K0.handle_right
    p2: QPointF   # = K1.handle_left
    p3: QPointF   # = K1
    interpolation: str = INTERP_BEZIER


@dataclass
class SplineCurve:
    """A full parameter curve (one animatable param on one node)."""
    curve_id: str                     # f"{node_id}:{param_name}"
    node_id: str
    param_name: str
    label: str
    color: str
    visible: bool = True
    selected: bool = False
    min_value: float = 0.0
    max_value: float = 1.0
    keys: List[SplineKey] = field(default_factory=list)
    # Batch 7: extrapolation beyond the first/last key.
    # "constant" = clamp to the end value; "linear" = extend the end slope.
    pre_extrap: str = "constant"
    post_extrap: str = "constant"

    @property
    def segments(self) -> List[SplineSegment]:
        return build_segments(self)


# ── Cubic bezier math (§11.4) ───────────────────────────────────────────


def cubic_bezier_xy(p0: QPointF, p1: QPointF, p2: QPointF, p3: QPointF,
                    t: float) -> QPointF:
    """Evaluate a cubic bezier at parameter t in [0, 1] (§11.4)."""
    u = 1.0 - t
    b0 = u * u * u
    b1 = 3.0 * u * u * t
    b2 = 3.0 * u * t * t
    b3 = t * t * t
    return QPointF(
        b0 * p0.x() + b1 * p1.x() + b2 * p2.x() + b3 * p3.x(),
        b0 * p0.y() + b1 * p1.y() + b2 * p2.y() + b3 * p3.y(),
    )


def sample_segment(seg: SplineSegment, sample_count: int) -> List[QPointF]:
    """Sample a segment into a polyline (§11.5). Used for hit-test + fit.

    hold/linear segments are sampled as straight lines; bezier/ease segments
    are sampled with the cubic formula.
    """
    if sample_count < 2:
        sample_count = 2
    pts: List[QPointF] = []
    interp = seg.interpolation
    if interp == INTERP_HOLD:
        # horizontal to p3.frame, then vertical to p3.value
        for i in range(sample_count):
            t = i / (sample_count - 1)
            x = seg.p0.x() + t * (seg.p3.x() - seg.p0.x())
            pts.append(QPointF(x, seg.p0.y()))
        pts.append(QPointF(seg.p3.x(), seg.p3.y()))
        return pts
    if interp == INTERP_LINEAR:
        for i in range(sample_count):
            t = i / (sample_count - 1)
            pts.append(QPointF(
                seg.p0.x() + t * (seg.p3.x() - seg.p0.x()),
                seg.p0.y() + t * (seg.p3.y() - seg.p0.y())))
        return pts
    if seg.interpolation in (INTERP_EASE_IN, INTERP_EASE_OUT,
                             INTERP_EASE_IN_OUT):
        # Ease modes are named easing functions in the renderer, not arbitrary
        # Bezier handles. Sample the same functions here so the Spline preview
        # cannot drift away from playback.
        dx = seg.p3.x() - seg.p0.x()
        dv = seg.p3.y() - seg.p0.y()
        for i in range(sample_count):
            t = i / (sample_count - 1)
            pts.append(QPointF(
                seg.p0.x() + t * dx,
                seg.p0.y() + _ease_progress(t, seg.interpolation) * dv))
        return pts
    # bezier
    for i in range(sample_count):
        t = i / (sample_count - 1)
        pts.append(cubic_bezier_xy(seg.p0, seg.p1, seg.p2, seg.p3, t))
    return pts


def _ease_progress(t: float, interpolation: str) -> float:
    """Return the normalized progress used by the keyframe evaluator."""
    if interpolation == INTERP_EASE_IN:
        return t * t
    if interpolation == INTERP_EASE_OUT:
        return 1.0 - (1.0 - t) * (1.0 - t)
    if interpolation == INTERP_EASE_IN_OUT:
        if t < 0.5:
            return 4.0 * t * t * t
        return 1.0 - (-2.0 * t + 2.0) ** 3 / 2.0
    return t


def build_segments(curve: SplineCurve) -> List[SplineSegment]:
    """Build adjacent-key segments for a curve (§11.5).

    Each segment's control points come from the keys' handles; for
    linear/hold/ease segments the handles are synthesized but the segment's
    ``interpolation`` flag tells the view how to draw it.
    """
    segs: List[SplineSegment] = []
    keys = sorted(curve.keys, key=lambda k: k.frame)
    for i in range(len(keys) - 1):
        k0 = keys[i]
        k1 = keys[i + 1]
        # Determine the segment interpolation from k0's interpolation.
        interp = k0.interpolation or INTERP_LINEAR
        # Synthesize ease control points if needed (§11.6).
        p1, p2 = _segment_handles(k0, k1, interp)
        segs.append(SplineSegment(
            p0=QPointF(k0.frame, k0.value),
            p1=p1, p2=p2,
            p3=QPointF(k1.frame, k1.value),
            interpolation=interp,
        ))
    return segs


def _segment_handles(k0: SplineKey, k1: SplineKey,
                     interp: str) -> Tuple[QPointF, QPointF]:
    """Compute P1/P2 control points for a segment given its interpolation."""
    dx = k1.frame - k0.frame
    if interp == INTERP_EASE_IN:
        return (QPointF(k0.frame + dx * 0.3333, k0.value),
                QPointF(k1.frame - dx * 0.3333, k1.value))
    if interp == INTERP_EASE_OUT:
        return (QPointF(k0.frame + dx * 0.3333, k0.value),
                QPointF(k1.frame - dx * 0.3333, k1.value))
    if interp == INTERP_EASE_IN_OUT:
        return (QPointF(k0.frame + dx * 0.3333, k0.value),
                QPointF(k1.frame - dx * 0.3333, k1.value))
    if interp == INTERP_BEZIER:
        # Use the keys' real handles, clamped to the segment frame range (§11.4).
        p1 = QPointF(
            _clamp(k0.handle_right.x(), k0.frame, k1.frame),
            k0.handle_right.y())
        p2 = QPointF(
            _clamp(k1.handle_left.x(), k0.frame, k1.frame),
            k1.handle_left.y())
        return (p1, p2)
    # linear / hold: handles collapse onto the keys
    return (QPointF(k0.frame, k0.value), QPointF(k1.frame, k1.value))


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


# ── Auto / smooth tangent (§11.7) ───────────────────────────────────────


def auto_slope(prev_key: Optional[SplineKey], key: SplineKey,
               next_key: Optional[SplineKey]) -> float:
    """Finite-difference slope for 'auto' tangent (§11.7)."""
    if prev_key is None or next_key is None:
        return 0.0
    dx = next_key.frame - prev_key.frame
    if dx <= 0:
        return 0.0
    # Local extremum → flat (§11.7).
    if (key.value - prev_key.value) * (next_key.value - key.value) <= 0:
        return 0.0
    return (next_key.value - prev_key.value) / dx


def apply_auto_tangent(prev_key: Optional[SplineKey], key: SplineKey,
                       next_key: Optional[SplineKey]) -> None:
    """Set key.handle_left/right from an auto slope (mutates key)."""
    slope = auto_slope(prev_key, key, next_key)
    left_dx = (key.frame - prev_key.frame) / 3.0 if prev_key else 0.0
    right_dx = (next_key.frame - key.frame) / 3.0 if next_key else 0.0
    key.handle_left = QPointF(key.frame - left_dx, key.value - slope * left_dx)
    key.handle_right = QPointF(key.frame + right_dx, key.value + slope * right_dx)


def apply_smooth_opposite(key: SplineKey, dragged: str) -> None:
    """When dragging one handle in 'smooth' mode, mirror the other (§11.8).

    ``dragged`` is 'left' or 'right'. The opposite handle keeps its length
    but flips direction so the two stay collinear.
    """
    kp = QPointF(key.frame, key.value)
    if dragged == "left":
        drag_vec = key.handle_left - kp
        opp_old = key.handle_right - kp
        old_len = math.hypot(opp_old.x(), opp_old.y())
        if old_len < 1e-9:
            old_len = math.hypot(drag_vec.x(), drag_vec.y())
        opp_dir = QPointF(-drag_vec.x(), -drag_vec.y())
        n = math.hypot(opp_dir.x(), opp_dir.y())
        if n < 1e-9:
            return
        opp_dir = QPointF(opp_dir.x() / n * old_len, opp_dir.y() / n * old_len)
        key.handle_right = kp + opp_dir
    else:
        drag_vec = key.handle_right - kp
        opp_old = key.handle_left - kp
        old_len = math.hypot(opp_old.x(), opp_old.y())
        if old_len < 1e-9:
            old_len = math.hypot(drag_vec.x(), drag_vec.y())
        opp_dir = QPointF(-drag_vec.x(), -drag_vec.y())
        n = math.hypot(opp_dir.x(), opp_dir.y())
        if n < 1e-9:
            return
        opp_dir = QPointF(opp_dir.x() / n * old_len, opp_dir.y() / n * old_len)
        key.handle_left = kp + opp_dir


# ── Hit testing helpers (§11.8) ─────────────────────────────────────────


def distance_point_to_segment(p: QPointF, a: QPointF, b: QPointF) -> float:
    """Distance from point p to segment [a,b] (§11.8)."""
    abx = b.x() - a.x()
    aby = b.y() - a.y()
    denom = abx * abx + aby * aby
    if denom <= 1e-9:
        return math.hypot(p.x() - a.x(), p.y() - a.y())
    t = ((p.x() - a.x()) * abx + (p.y() - a.y()) * aby) / denom
    t = max(0.0, min(1.0, t))
    cx = a.x() + abx * t
    cy = a.y() + aby * t
    return math.hypot(p.x() - cx, p.y() - cy)


# ── Fit bounds (§11.9) ──────────────────────────────────────────────────


def fit_bounds(curves: List[SplineCurve],
               include_handles: bool = True,
               sample_count: int = 64) -> Optional[Tuple[float, float, float, float]]:
    """Return (frame_min, frame_max, value_min, value_max) covering all
    key points, handles and sampled overshoot of visible curves (§11.9).

    Returns None when there is nothing to fit.
    """
    pts: List[QPointF] = []
    for curve in curves:
        if not curve.visible:
            continue
        keys = sorted(curve.keys, key=lambda k: k.frame)
        for k in keys:
            pts.append(QPointF(k.frame, k.value))
            if include_handles:
                pts.append(k.handle_left)
                pts.append(k.handle_right)
        for seg in build_segments(curve):
            pts.extend(sample_segment(seg, sample_count))
    if not pts:
        return None
    xs = [p.x() for p in pts]
    ys = [p.y() for p in pts]
    fmin, fmax = min(xs), max(xs)
    vmin, vmax = min(ys), max(ys)
    # padding (§11.9)
    fpad = max(5.0, (fmax - fmin) * 0.05)
    vpad = max(0.1, (vmax - vmin) * 0.1)
    return (fmin - fpad, fmax + fpad, vmin - vpad, vmax + vpad)


# ── Model: build curves from a NodeGraph ────────────────────────────────


class SplineModel:
    """Build SplineCurve list from a NodeGraph (mirrors KeyframesModel)."""

    def __init__(self) -> None:
        self._curves: List[SplineCurve] = []
        # AUD-10901: visibility memory records HIDDEN curve ids (default is
        # visible). The previous "_visible_ids" set recorded shown ids, so
        # hiding curves left the set empty and the next rebuild's
        # `if self._visible_ids` check forced everything visible again.
        self._hidden_ids: set = set()

    def rebuild(self, node_graph: Any) -> None:
        """Regenerate curves from every animated param in the graph.

        AUD-10902: the old curves are only replaced after the new set has
        been built (previously ``_curves.clear()`` ran before the guarded
        enumeration, so a bad graph silently emptied the editor), and the
        broad ``except Exception`` blocks are narrowed to the structural
        errors they actually guard, with a log line so failures are
        diagnosable instead of swallowed.
        """
        if node_graph is None:
            _log.warning("SplineModel.rebuild: no node graph provided")
            return
        try:
            nodes = list(node_graph.nodes.values())
        except (AttributeError, TypeError) as exc:
            _log.warning(
                "SplineModel.rebuild: cannot enumerate graph nodes: %s", exc)
            return
        # Stable order: canvas position then id. A malformed position falls
        # back to graph insertion order instead of losing the curve set.
        try:
            nodes.sort(key=lambda n: (n.position[1], n.position[0]))
        except (AttributeError, TypeError, IndexError, KeyError) as exc:
            _log.warning(
                "SplineModel.rebuild: node sort failed (%s); "
                "keeping graph order", exc)
        new_curves: List[SplineCurve] = []
        for node in nodes:
            color = self._node_color(node)
            for pname, track_data in getattr(node, "keyframe_tracks", {}).items():
                if not isinstance(track_data, dict):
                    continue
                kfs = track_data.get("keyframes") or []
                if not kfs:
                    continue
                curve_id = f"{node.id}:{pname}"
                curve = SplineCurve(
                    curve_id=curve_id,
                    node_id=node.id,
                    param_name=pname,
                    label=f"{node.display_title} · {pname}",
                    color=color,
                    min_value=track_data.get("min_val", 0.0),
                    max_value=track_data.get("max_val", 1.0),
                    pre_extrap=track_data.get("pre_extrap", "constant"),
                    post_extrap=track_data.get("post_extrap", "constant"),
                    # AUD-10901: everything not explicitly hidden is visible.
                    visible=(curve_id not in self._hidden_ids),
                )
                curve.keys = self._build_keys(kfs, track_data)
                new_curves.append(curve)
        # Swap in atomically: if anything above raised, the previous curves
        # stay intact instead of leaving a cleared editor.
        self._curves = new_curves

    @staticmethod
    def _node_color(node: Any) -> str:
        try:
            return node.effective_header_color()
        except (AttributeError, TypeError) as exc:
            _log.debug(
                "SplineModel._node_color: falling back to header_color for "
                "%r: %s", getattr(node, "id", "?"), exc)
            return getattr(node, "header_color", "#555555")

    @staticmethod
    def _build_keys(kfs: List[Dict[str, Any]],
                    track_data: Dict[str, Any]) -> List[SplineKey]:
        keys: List[SplineKey] = []
        for kf in kfs:
            try:
                frame = int(kf.get("frame", 0))
                value = float(kf.get("value", 0.0))
                interp = kf.get("interpolation", INTERP_LINEAR)
                tangent_mode = kf.get("tangent_mode", TANGENT_AUTO)
                # P1-1: prefer authoritative handle_left/right (absolute deltas
                # from the key). Fall back to legacy bezier_in/out only when
                # the new fields are absent. Preserve None so auto-tangent can
                # recompute below.
                hl = kf.get("handle_left")
                hr = kf.get("handle_right")
                if hl is not None:
                    handle_left = QPointF(frame + float(hl[0]), value + float(hl[1]))
                else:
                    bin_ = kf.get("bezier_in", None)
                    handle_left = (QPointF(frame + float(bin_[0]), value + float(bin_[1]))
                                   if bin_ is not None else None)
                if hr is not None:
                    handle_right = QPointF(frame + float(hr[0]), value + float(hr[1]))
                else:
                    bout = kf.get("bezier_out", None)
                    handle_right = (QPointF(frame + float(bout[0]), value + float(bout[1]))
                                    if bout is not None else None)
                k = SplineKey(
                    frame=frame, value=value, interpolation=interp,
                    tangent_mode=tangent_mode,
                    handle_left=handle_left if handle_left is not None
                                else QPointF(frame, value),
                    handle_right=handle_right if handle_right is not None
                                 else QPointF(frame, value),
                )
                # Track whether this key had saved handles so we only auto-
                # recompute the ones that didn't.
                k._had_saved_handles = (hl is not None or hr is not None
                                        or 'bezier_in' in kf or 'bezier_out' in kf)
                keys.append(k)
            except (TypeError, ValueError, IndexError):
                continue
        keys.sort(key=lambda k: k.frame)
        # P1-1: only recompute auto tangents for keys that have NO saved
        # handles AND are in 'auto' mode. User-edited handles must survive
        # rebuild (closing/reopening a project must not discard edits).
        if keys:
            for i, k in enumerate(keys):
                had_saved = getattr(k, "_had_saved_handles", False)
                if had_saved and k.tangent_mode != TANGENT_AUTO:
                    continue
                if had_saved and k.tangent_mode == TANGENT_AUTO:
                    # auto mode always recomputes (that's what auto means)
                    pass
                prev_k = keys[i - 1] if i > 0 else None
                next_k = keys[i + 1] if i < len(keys) - 1 else None
                apply_auto_tangent(prev_k, k, next_k)
        return keys

    # ── accessors ──

    @property
    def curves(self) -> List[SplineCurve]:
        return self._curves

    def visible_curves(self) -> List[SplineCurve]:
        return [c for c in self._curves if c.visible]

    def curve_by_id(self, curve_id: str) -> Optional[SplineCurve]:
        for c in self._curves:
            if c.curve_id == curve_id:
                return c
        return None

    def set_visible(self, curve_id: str, visible: bool) -> None:
        """Set one curve's visibility and remember it across rebuilds.

        AUD-10901: the memory tracks hidden ids (default visible). "Hide all"
        fills it; "Show all" empties it — both now survive rebuilds.
        """
        c = self.curve_by_id(curve_id)
        if c is not None:
            c.visible = visible
        if visible:
            self._hidden_ids.discard(curve_id)
        else:
            self._hidden_ids.add(curve_id)
