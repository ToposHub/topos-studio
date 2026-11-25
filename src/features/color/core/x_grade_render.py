"""X Grade's color field, packed alongside the Color Warper's existing texture."""
from __future__ import annotations

import math
from functools import lru_cache
from typing import TYPE_CHECKING

import numpy as np

from src.features.color.core.warper_chroma709 import (
    _CW_LUMA, _CW_PLOT_INVERSE, _CW_PLOT_POWER, _CW_PLOT_WEIGHTS, _CW_VERTICES,
    _cw_plot_to_linear, chroma_warp_position,
)
from src.features.color.core.x_grade_domain import X_GRADE_SCOPE_AXES

if TYPE_CHECKING:
    from src.features.color.core.warper_state import XGradeState


FIELD_SIZE = 97
TONAL_LEVELS = 13
# Log exposure domain: black, 18% gray, and 16x scene white map monotonically.
_TONAL_SCALE = 12.0
_TONAL_LOG_MAX = math.log2(1.0 + 16.0 * _TONAL_SCALE)
_GRAY_POINT = _CW_PLOT_WEIGHTS @ _CW_VERTICES
_RGB_TO_LMS = np.array(((0.41222147, 0.53633254, 0.05144599),
                        (0.21190350, 0.68069955, 0.10739696),
                        (0.08830246, 0.28171884, 0.62997870)), dtype=np.float32)
_LMS_OPPONENT_AXES = np.array(((1.97799850, 0.02590404),
                               (-2.42859221, 0.78277177),
                               (0.45059371, -0.80867577)), dtype=np.float32)
_EXPOSURE_REFERENCE = np.array((-0.02185, 0.02668), dtype=np.float32)
# Reference projection fitted from Rec.709 anchors on the native chromaticity
# scope. The fit is used for drag travel. Stored coordinates stay in the Topos plot.
_DRAG_VERTICES = np.array(((162.1, 936.8), (973.3, 543.1), (225.7, 15.3))) / 900.0
_DRAG_TO_BARYCENTRIC = np.linalg.inv(_DRAG_VERTICES[1:] - _DRAG_VERTICES[0])
_DRAG_WEIGHTS = np.array((1.0, math.exp(0.98027), math.exp(0.26418)))
_DRAG_POWER = 1.73808


def _exposure_position(plot: np.ndarray) -> np.ndarray:
    lms = _cw_plot_to_linear(plot) @ _RGB_TO_LMS.T
    return (lms / lms.sum(axis=-1, keepdims=True)) ** 0.43958 @ _LMS_OPPONENT_AXES


_CHROMA_LIGHTNESS_ORIGIN = _exposure_position(_GRAY_POINT)


def _chroma_lightness(position: np.ndarray) -> np.ndarray:
    """Log-luminance offset that preserves brightness as chromaticity moves."""
    u, v = np.moveaxis(3.0 * (position - _CHROMA_LIGHTNESS_ORIGIN), -1, 0)
    return u * (0.04543 + 0.02935 * u + 0.05870 * v) + v * (-0.24114 + 0.56917 * v)


def _exposure_weight(points: np.ndarray, anchor: np.ndarray, width: float, stops: float) -> np.ndarray:
    center = _exposure_position(anchor)
    width = _exposure_width(5.0 * width, stops, center)
    a, b = center - _EXPOSURE_REFERENCE
    exponent = np.clip(0.89758 + 1.78087 * a - 0.21134 * b, 0.45, 1.8)
    radius = 0.52289 * (width + 0.03921) ** exponent * np.exp(2.41558 * a - 0.13108 * b)
    dx, dy = (points - center).T
    distance_squared = (dx + 0.02370 * dy) ** 2 + (1.00155 * dy) ** 2
    return np.exp(-((distance_squared / (radius * radius)) ** 0.99752))


def _exposure_width(width: float, stops: float, center: np.ndarray) -> float:
    """Refine narrow, sub-stop exposure falloff measured in the Rec.709 TIFFs."""
    a, b = map(float, center)
    magnitude = abs(stops)
    adjustment = max(1.0 - magnitude, 0.0) * float(_smooth(np.asarray(magnitude / 0.50)))
    chroma = float(_smooth(np.asarray((b + 0.12) / 0.12)))
    narrow = 1.0 - float(_smooth(np.asarray((width - 0.28) / 0.07)))
    correction = (0.115 + min(0.105, 0.45 * max(-a, 0.0))) * adjustment * chroma * narrow
    return max(min(width, 0.02), width - correction)


def pack_x_grade(state: XGradeState, base: np.ndarray | None, space: int) -> np.ndarray:
    """Keep projection cheap. The render thread evaluates the authored records."""
    from src.features.color.core.warper_state import WarperStroke, default_stroke_params

    rows, columns, components = (0, 0, 0) if base is None or not base.any() else base.shape
    records = np.zeros((3 + rows * columns + 3 * (len(state.strokes) + len(state.pin_points)), 1, 3),
                       dtype=np.float32)
    records[0, 0] = (space, rows, columns)
    records[1, 0] = (components, len(state.strokes), len(state.pin_points))
    records[2, 0, 0] = state.protect_neutral
    position = 3 + rows * columns
    if rows:
        records[3:position, 0, :components] = base.reshape(-1, components)
    for item in (*state.strokes, *state.pin_points):
        if isinstance(item, WarperStroke):
            source, target = (item.path[0], item.path[-1]) if len(item.path) >= 2 else (
                chroma_warp_position(np.asarray([item.source_rgb, item.target_rgb])))
        else:
            source = target = item.position
        params = {**default_stroke_params(), **(item.params or {})}
        low, high, pivot = (params[key] for key in ("tonal_low", "tonal_high", "pivot"))
        zone = params.get("x_zone")
        falloff = params.get("x_falloff", 0.5)
        if zone == "dim":
            low, high = -1.0, falloff
        elif zone == "light":
            low, high = falloff, -1.0
        elif zone == "master":
            low = high = 1.0
        records[position:position + 3, 0] = [
            (source[0], source[1], target[0]),
            (target[1], params["chroma_range"], params["exposure"]),
            (low, high, pivot),
        ]
        position += 3
    return records


def unpack_x_grade_base(records: np.ndarray | list) -> tuple[np.ndarray | None, int]:
    """Retrieve the original Warper controls without evaluating the X Grade field."""
    data = np.asarray(records, dtype=np.float32)
    if data.ndim != 3 or data.shape[1:] != (1, 3) or len(data) < 3:
        raise ValueError("Invalid X Grade controls")
    space, rows, cols = map(int, data[0, 0])
    components = int(data[1, 0, 0])
    if (rows, cols, components) == (0, 0, 0):
        return None, 0
    end = 3 + rows * cols
    if rows <= 0 or cols <= 0 or components not in (2, 3) or end > len(data):
        raise ValueError("Invalid X Grade base Warper controls")
    return data[3:end, 0, :components].reshape(rows, cols, components).copy(), space


def _smooth(unit: np.ndarray) -> np.ndarray:
    unit = np.clip(unit, 0.0, 1.0)
    return unit * unit * (3.0 - 2.0 * unit)


def _radial(points: np.ndarray, center: np.ndarray, radius: float) -> np.ndarray:
    distance = np.linalg.norm(points - center, axis=-1)
    return 1.0 - _smooth(distance / radius)


def chroma_brush(start: np.ndarray, direction: np.ndarray, width: float) -> tuple[np.ndarray, np.ndarray, float]:
    """Return the center, effective displacement, and radius of a color stroke."""
    # A push near the gamut boundary should compress progressively into
    # the less saturated neighbors instead of piling colors up at the edge.
    axes = _CW_PLOT_INVERSE[:, :2]
    headroom = np.maximum(axes @ start + _CW_PLOT_INVERSE[:, 2], 0.02)
    edge_travel = float(np.max(-(axes @ direction) / headroom))
    edge_weight = float(_smooth(np.array((edge_travel - 0.55) / 0.65)))
    center = start + (_GRAY_POINT - start) * (0.5 * edge_weight)
    delta = direction * (1.0 - 0.85 * edge_weight)
    radius = max(width * (3.5 - 1.1 * edge_weight), 0.022)
    return center, delta, radius


def _scope_travel(start: np.ndarray, screen_delta: np.ndarray) -> np.ndarray:
    rgb = _cw_plot_to_linear(start)
    weighted = rgb ** (_DRAG_POWER / 2.4) * _DRAG_WEIGHTS
    barycentric = weighted / weighted.sum()
    gb = barycentric[1:] + screen_delta @ _DRAG_TO_BARYCENTRIC
    target = np.maximum(np.r_[1.0 - gb.sum(), gb], 0.0)
    target_rgb = (target / _DRAG_WEIGHTS) ** (1.0 / _DRAG_POWER)
    return (chroma_warp_position(target_rgb) - start).astype(np.float32)


def _walk_brush(mapped: np.ndarray, center: np.ndarray, travel: np.ndarray, radius: float,
                strength: np.ndarray, *, gamut_edge: bool = False) -> None:
    stride = 0.17 if gamut_edge else 0.32
    steps = max(1, math.ceil(float(np.linalg.norm(travel)) / (radius * stride)))
    delta = travel / steps
    if not gamut_edge:
        low = np.minimum(center, center + travel) - radius
        high = np.maximum(center, center + travel) + radius
        active = (strength > 0.0) & np.all((mapped >= low) & (mapped <= high), axis=-1)
        points = mapped[active]
        strength = strength[active]
        for step in range(steps):
            weight = strength * _radial(points, center + delta * step, radius)
            points += weight[..., None] * delta
        mapped[active] = points
        return

    # Spend each channel's own chromaticity headroom. Colors with room to
    # move still follow the drag when a saturated neighbor reaches the edge.
    axes = _CW_PLOT_INVERSE[:, :2].astype(np.float32)
    origin = _CW_PLOT_INVERSE[:, 2].astype(np.float32)
    path = delta @ axes.T
    growing = np.maximum(path, 0.0)
    gain = growing / max(float(growing.sum()), 1e-9)
    shrinking = np.minimum(path, 0.0)
    points = np.maximum(mapped @ axes.T + origin, 0.0)
    points /= points.sum(axis=-1, keepdims=True)
    center_bary = np.maximum(center @ axes.T + origin, 0.0)
    center_bary /= center_bary.sum()
    centers = []
    for _ in range(steps):
        centers.append(center_bary @ _CW_VERTICES)
        removed = -center_bary * np.expm1(shrinking / np.maximum(center_bary, 1e-8))
        center_bary += removed.sum() * gain - removed

    initial = points @ _CW_VERTICES
    # A point outside the swept bounds has zero weight in every step. Limit
    # the per-step work here so preview cost stays bounded with tonal planes.
    low = np.min(centers, axis=0) - radius
    high = np.max(centers, axis=0) + radius
    active = (strength > 0.0) & np.all((initial >= low) & (initial <= high), axis=-1)
    points = points[active]
    initial = initial[active]
    strength = strength[active]
    current = initial
    for position in centers:
        weight = strength * _radial(current, position, radius)
        removed = -points * np.expm1(weight[..., None] * shrinking / np.maximum(points, 1e-8))
        points += removed.sum(axis=-1, keepdims=True) * gain - removed
        current = points @ _CW_VERTICES
    mapped[active] += current - initial


def _apply_chroma_stroke(mapped: np.ndarray, start: np.ndarray, direction: np.ndarray,
                        width: float, strength: np.ndarray) -> None:
    center, travel, radius = chroma_brush(start, direction, width)
    length = float(np.linalg.norm(direction))
    screen_delta = direction @ X_GRADE_SCOPE_AXES
    screen_length = float(np.linalg.norm(screen_delta))
    end = (start + direction) @ _CW_PLOT_INVERSE[:, :2].T + _CW_PLOT_INVERSE[:, 2]
    edge = float(_smooth(np.asarray((screen_length - 55.0 / 900.0) * 30.0))
                 * _smooth(np.asarray((0.06 - end.min()) / 0.06)))
    edge_warp = mapped.copy() if edge else None

    # Retain the fine-motion response, then follow the calibrated chromaticity
    # arc as travel grows. The inward brush trails the cursor and narrows.
    progress = float(_smooth(np.asarray(length * 13.0 - 0.4)))
    retained = float(np.linalg.norm(travel)) / length
    if progress and retained > 0.68:
        amount = 0.9 * progress * float(_smooth(np.asarray((retained - 0.68) / 0.30)))
        travel += amount * (_scope_travel(start, screen_delta) - travel)
        offset = (start - _GRAY_POINT) @ X_GRADE_SCOPE_AXES
        inward = -float(offset @ screen_delta) / max(float(np.linalg.norm(offset)) * screen_length, 1e-6)
        trail = (abs(screen_delta[1]) / screen_length
                 * float(_smooth(np.asarray((inward - 0.30) / 0.60)))
                 * float(_smooth(np.asarray((length - 0.045) / 0.11))))
        center -= 0.13 * trail * direction
        radius = max(radius - 0.10 * trail * length, 0.022)
    _walk_brush(mapped, center, travel, radius, strength)
    if edge_warp is not None:
        _walk_brush(edge_warp, start, direction, max(width * 3.5, 0.022), strength, gamut_edge=True)
        mapped += edge * (edge_warp - mapped)


def _tonal(level: np.ndarray, low: float, high: float, pivot: float) -> np.ndarray:
    # Slider pivot remains a display-oriented coordinate. The stored field
    # samples a wider scene-linear domain so bright values keep grading.
    code = (np.exp2(level * _TONAL_LOG_MAX) - 1.0) / _TONAL_SCALE
    code = np.minimum(code ** (1.0 / 2.4), 1.0)
    if low < 0.0 or high < 0.0:
        falloff = high if low < 0.0 else low
        width = 0.02 + 0.98 * falloff
        weight = _smooth((code - pivot) / width + 0.5)
        return 1.0 - weight if low < 0.0 else weight
    below = _smooth(code / max(pivot, 1e-4))
    above = _smooth((1.0 - code) / max(1.0 - pivot, 1e-4))
    return (low + (1.0 - low) * below) * (high + (1.0 - high) * above)


@lru_cache(maxsize=1)
def _field_coordinates() -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    uv = np.linspace(0.0, 1.0, FIELD_SIZE, dtype=np.float32)
    x, y = np.meshgrid(uv, uv, indexing="xy")
    plot = np.stack((x, y), axis=-1).reshape(-1, 2)
    exposure = _exposure_position(plot)
    brightness = _chroma_lightness(exposure)
    plot.setflags(write=False)
    exposure.setflags(write=False)
    brightness.setflags(write=False)
    return plot, exposure, brightness


def _field(strokes: np.ndarray, pins: np.ndarray, protect_neutral: bool) -> np.ndarray:
    levels = TONAL_LEVELS if np.any(strokes[:, 2, :2] != 1.0) or np.any(pins[:, 2, :2] != 1.0) else 1
    source, exposure_points, brightness = _field_coordinates()
    tone = np.linspace(0.0, 1.0, levels, dtype=np.float32)
    mapped = np.broadcast_to(source, (levels, len(source), 2)).copy()
    exposure = np.zeros(mapped.shape[:2], dtype=np.float32)
    pin_strength = np.ones_like(exposure)
    neutral_strength: np.ndarray | float = 1.0
    if protect_neutral:
        distance = np.linalg.norm(source - _GRAY_POINT, axis=-1)
        neutral_strength = _smooth((distance - 0.018) / 0.050)
    for (x, y, _), (_, radius, _), (low, high, pivot) in pins:
        distance = np.linalg.norm(source - (x, y), axis=-1)
        weight = (1.0 - _smooth((distance - 0.014) / max(radius * 1.9, 0.011)))
        pin_strength *= 1.0 - weight * _tonal(tone, low, high, pivot)[:, None]
    for (sx, sy, tx), (ty, width, stops), (low, high, pivot) in strokes:
        start = np.asarray((sx, sy), dtype=np.float32)
        direction = np.asarray((tx, ty), dtype=np.float32) - start
        strength = _tonal(tone, low, high, pivot)[:, None] * pin_strength
        if stops:
            exposure += stops * strength * _exposure_weight(exposure_points, start, width, stops)
        if direction.any():
            _apply_chroma_stroke(mapped, start, direction, width, strength * neutral_strength)
    displacement = mapped - source
    tonal_rows, cells = np.nonzero(np.any(displacement != 0.0, axis=-1))
    exposure[tonal_rows, cells] += (
        brightness[cells] - _chroma_lightness(_exposure_position(mapped[tonal_rows, cells]))
    )
    result = np.concatenate((displacement, exposure[..., None]), axis=2)
    return result.reshape(levels * FIELD_SIZE, FIELD_SIZE, 3)


def prepare_x_grade_texture(records: np.ndarray) -> np.ndarray:
    data = np.ascontiguousarray(records, dtype=np.float32)
    if data.ndim != 3 or data.shape[1:] != (1, 3) or not np.isfinite(data).all():
        raise ValueError("Invalid X Grade controls")
    return _prepare(data.tobytes())


@lru_cache(maxsize=6)
def _prepare(data: bytes) -> np.ndarray:
    from src.features.color.core.warper_color import (
        RESOLVE_HS709_SPACES, WARPER_CHROMA709_SPACE, prepare_resolve_hs709_texture,
    )
    from src.features.color.core.warper_state import MAX_PINS, MAX_STROKES

    records = np.frombuffer(data, dtype=np.float32).reshape(-1, 3)
    if len(records) < 3:
        raise ValueError("Missing X Grade metadata")
    base_space, rows, cols = map(int, records[0])
    components, strokes, pins = map(int, records[1])
    end = 3 + rows * cols
    prepared_spaces = (*RESOLVE_HS709_SPACES, WARPER_CHROMA709_SPACE)
    base_shape = ((rows, cols, components) == (0, 0, 0)
                  or (rows > 0 and cols > 0 and base_space in (0, 1, 2, *prepared_spaces)
                      and components == (3 if base_space in prepared_spaces else 2)))
    if (not 0 < strokes <= MAX_STROKES or not 0 <= pins <= MAX_PINS or not base_shape
            or len(records) != end + 3 * (strokes + pins)):
        raise ValueError("Invalid X Grade payload dimensions")
    constraints = records[end:].reshape(-1, 3, 3)
    field = _field(constraints[:strokes], constraints[strokes:], bool(records[2, 0]))
    if rows and cols:
        base = np.ascontiguousarray(records[3:end, :components].reshape(rows, cols, components))
        if base_space in prepared_spaces:
            base = prepare_resolve_hs709_texture(base, base_space)
        h, w, components = base.shape
    else:
        base, h, w, components = None, 0, 0, 0
    start = len(field) + 1
    texture = np.zeros((start + h, max(FIELD_SIZE, w), 3), dtype=np.float32)
    texture[0, 0] = (base_space, w, h)
    texture[0, 1] = (FIELD_SIZE, len(field) // FIELD_SIZE, start)
    texture[0, 2, 0] = records[2, 0]
    texture[1:start, :FIELD_SIZE] = field
    if base is not None:
        texture[start:, :w, :components] = base
    texture.setflags(write=False)
    return texture


def _apply_legacy_grid(source: np.ndarray, mesh: np.ndarray, space: int) -> np.ndarray:
    """Vectorized legacy HSV/HSL/HSP warper for the composed CPU stage."""
    from src.features.color.core.warper_color import HSP_WEIGHTS, _hs709_basis
    from src.features.color.core.warper_response import _hue_and_chroma

    finite = np.isfinite(source).all(axis=1)
    magnitude = np.abs(np.where(finite[:, None], source, 0.0))
    hue, chroma, peak = _hue_and_chroma(magnitude)
    saturation = chroma / (peak + 1e-10)
    lightness = peak - 0.5 * chroma
    active = finite
    if space == 1:
        saturation = chroma / np.maximum(1.0 - np.abs(2.0 * lightness - 1.0), 1e-10)
        active = finite & (lightness < 1.0)
    rows, cols = mesh.shape[:2]
    x = np.clip(hue * cols - 0.5, 0.0, cols - 1)
    y = np.clip(saturation * rows - 0.5, 0.0, rows - 1)
    col, row = x.astype(np.intp), y.astype(np.intp)
    next_col, next_row = np.minimum(col + 1, cols - 1), np.minimum(row + 1, rows - 1)
    fx, fy = (x - col)[:, None], (y - row)[:, None]
    lower = mesh[row, col] * (1.0 - fx) + mesh[row, next_col] * fx
    upper = mesh[next_row, col] * (1.0 - fx) + mesh[next_row, next_col] * fx
    delta = (lower * (1.0 - fy) + upper * fy) * _smooth((saturation - 0.02) / 0.13)[:, None]
    basis = _hs709_basis(hue + delta[:, 0])
    sat = np.clip(saturation + delta[:, 1], 0.0, 1.0)
    if space == 1:
        new_chroma = (1.0 - np.abs(2.0 * lightness - 1.0)) * sat
        mapped = lightness[:, None] + new_chroma[:, None] * (basis - 0.5)
    else:
        mapped = 1.0 - sat[:, None] * (1.0 - basis)
        if space == 2:
            weights = np.asarray(HSP_WEIGHTS)
            scale = np.sqrt((magnitude ** 2 @ weights) / np.maximum(mapped ** 2 @ weights, 1e-20))
        else:
            scale = peak
        mapped *= scale[:, None]
    return np.where((active & (saturation >= 0.02))[:, None], mapped * np.sign(source), source).astype(
        source.dtype, copy=False)


def _apply_base_texture(rgb: np.ndarray, texture: np.ndarray, space: int, *, encoded_input: bool) -> np.ndarray:
    from src.features.color.core.warper_color import WARPER_PREPARED_SPACES, apply_resolve_hs709_texture

    if space in WARPER_PREPARED_SPACES:
        return apply_resolve_hs709_texture(rgb, texture, space, encoded_input=encoded_input)
    return _apply_legacy_grid(rgb.reshape(-1, 3), texture[..., :2], space).reshape(rgb.shape)


def sample_base_warper(rgb: np.ndarray, offsets: np.ndarray, space: int, *, encoded_input: bool) -> np.ndarray:
    """Evaluate the Color Warper on picker samples before applying any X Grade stroke."""
    from src.features.color.core.warper_color import (
        WARPER_PREPARED_SPACES, WARPER_X_GRADE_SPACE, prepare_resolve_hs709_texture,
    )

    if space == WARPER_X_GRADE_SPACE:
        base, space = unpack_x_grade_base(offsets)
        if base is None:
            return rgb
        offsets = base
    texture = prepare_resolve_hs709_texture(offsets, space) if space in WARPER_PREPARED_SPACES else offsets
    return _apply_base_texture(rgb, texture, space, encoded_input=encoded_input)


def apply_x_grade_texture(rgb: np.ndarray, texture: np.ndarray, *, encoded_input: bool = False) -> np.ndarray:
    original = np.asarray(rgb)
    source = original.reshape(-1, 3)
    space, width, height = map(int, texture[0, 0])
    size, levels, start = map(int, texture[0, 1])
    if width and height:
        base = np.ascontiguousarray(texture[start:start + height, :width])
        source = _apply_base_texture(source, base, space, encoded_input=encoded_input)
    finite = np.isfinite(source).all(axis=1)
    positive = np.maximum(np.where(finite[:, None], source, 0.0), 0.0)
    linear = positive ** 2.4 if encoded_input else positive
    luma = linear @ _CW_LUMA
    plot = chroma_warp_position(positive if encoded_input else positive ** (1.0 / 2.4))
    uv = np.clip(plot, 0.0, 1.0) * (size - 1)
    tone = np.minimum(np.log2(1.0 + luma * _TONAL_SCALE) / _TONAL_LOG_MAX, 1.0) * (levels - 1)
    cube = texture[1:start, :size].reshape(levels, size, size, 3)
    lo = uv.astype(np.intp)
    hi = np.minimum(lo + 1, size - 1)
    u0, v0 = lo.T
    u1, v1 = hi.T
    fx, fy = (uv - lo).T[:, :, None]
    z0 = tone.astype(np.intp)
    z1 = np.minimum(z0 + 1, levels - 1)
    fz = (tone - z0)[:, None]

    def plane(z: np.ndarray) -> np.ndarray:
        lower = cube[z, v0, u0] * (1.0 - fx) + cube[z, v0, u1] * fx
        upper = cube[z, v1, u0] * (1.0 - fx) + cube[z, v1, u1] * fx
        return lower * (1.0 - fy) + upper * fy

    delta = plane(z0) * (1.0 - fz) + plane(z1) * fz
    if texture[0, 2, 0]:
        delta[np.ptp(positive, axis=1) <= 1e-6, :2] = 0.0
    uv = plot + delta[:, :2]
    barycentric = uv @ _CW_PLOT_INVERSE[:, :2].T + _CW_PLOT_INVERSE[:, 2]
    ratio = (np.maximum(barycentric, 0.0) / _CW_PLOT_WEIGHTS) ** (2.4 / _CW_PLOT_POWER)
    changed = ratio * (luma / np.maximum(ratio @ _CW_LUMA, 1e-12))[:, None]
    mapped = np.where(delta[:, :2].any(axis=1)[:, None], changed, linear)
    mapped *= np.exp2(np.clip(delta[:, 2:3], -16.0, 16.0))
    active = finite & (luma > 0.0) & delta.any(axis=1)
    result = mapped ** (1.0 / 2.4) if encoded_input else mapped
    return np.where(active[:, None], result + np.minimum(source, 0.0), source).reshape(original.shape).astype(
        original.dtype, copy=False)
