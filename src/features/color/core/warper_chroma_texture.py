"""Prepare and sample a tiled RGB response for the Rec.709 Chroma Warper."""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np

if TYPE_CHECKING:
    from src.features.color.core.warper_state import WarperStateV2


CHROMA_LUT_SIZE = 49
_HUE_SPACES = {"hsv": 0, "hsl": 1, "hsp": 2, "hsy": 7}
_SPACE_NAMES = {index: name for name, index in _HUE_SPACES.items()}


def pack_hue_state(state: WarperStateV2, *, legacy_encoded: bool = False) -> np.ndarray | None:
    from src.features.color.core.warper_color import WARPER_SPACE_INDEX
    from src.features.color.core.warper_state import HS_SPACES709_ALGORITHM, resolve_hs_render_space

    grid = state.grids["hs"]
    if state.is_identity():
        return None
    pins = []
    for pin in grid.pins:
        parts = pin.point_id.split(":")
        if (pin._extra_fields.get("affects_response") is True and len(parts) == 3
                and parts[0] == "hs" and parts[1].isdigit() and parts[2].isdigit()):
            row, col = int(parts[1]), int(parts[2])
            if row < grid.rows and col < grid.cols and not grid.offsets[row, col].any():
                pins.append((row, col, 0))
    space = resolve_hs_render_space(state.algorithm_version)
    if space is None:
        space = (_HUE_SPACES[state.color_space] if state.algorithm_version == HS_SPACES709_ALGORITHM
                 else WARPER_SPACE_INDEX.get(state.color_space, 0))
    body = np.zeros((grid.rows * grid.cols + 2, 1, 3), dtype=np.float32)
    has_constraints = any(state.white_point) or bool(pins)
    body[0, 0] = (2, space, len(pins) + 1 if has_constraints else 0)
    body[1, 0] = (grid.rows, grid.cols, -grid.components if legacy_encoded and grid.components == 2 else grid.components)
    body[2:, 0, :grid.components] = grid.offsets.reshape(-1, grid.components)
    if has_constraints:
        tail = np.asarray([state.white_point, *pins], np.float32)[:, None, :]
        body = np.concatenate((body, tail))
    return body


def pack_chroma_state(state: WarperStateV2, hs: WarperStateV2 | None = None) -> np.ndarray | None:
    """Project authored controls, without evaluating image colors in the UI thread."""
    if state.mode == "chroma_luma":
        if state.is_identity():
            return None
        from src.features.color.core.warper_state import CL_GRID_NAMES

        hue_records = (None if hs is None else
                       pack_hue_state(hs, legacy_encoded=bool(state._extra_fields.get("legacy_hs_encoded"))))
        records = [np.array([[0, state.axis_angle, -2 if hue_records is not None else -1]], dtype=np.float32)]
        for name in CL_GRID_NAMES:
            grid = state.grids[name]
            data = np.zeros((grid.rows, grid.cols, 3), dtype=np.float32)
            data[..., :2] = grid.offsets
            for pin in grid.pins:
                parts = pin.point_id.rsplit(":", 2)
                if len(parts) == 3 and parts[0] == name and parts[1].isdigit() and parts[2].isdigit():
                    row, col = map(int, parts[1:])
                    if 0 <= row < grid.rows and 0 <= col < grid.cols:
                        data[row, col, 2] = 1.0
            meta = np.array([[grid.rows, grid.cols, _HUE_SPACES[state.color_space]]], dtype=np.float32)
            records.extend((meta, data.reshape(-1, 3)))
        if hue_records is not None:
            records.append(hue_records[:, 0])
        return np.concatenate(records)[:, None, :]

    if not state.strokes:
        return None
    from src.features.color.core.warper_chroma709 import chroma_warp_position
    from src.features.color.core.warper_state import default_stroke_params

    def record(source: tuple[float, float], target: tuple[float, float],
               values: dict[str, float] | None, mode: int) -> tuple[tuple[float, ...], ...]:
        params = {**default_stroke_params(), **(values or {})}
        return ((source[0], source[1], target[0]),
                (target[1], params["chroma_range"], params["exposure"]),
                (params["tonal_low"], params["tonal_high"], params["pivot"]),
                (mode, 0, 0))

    entries = [(1, len(state.strokes), len(state.pin_points))]
    for stroke in state.strokes:
        if len(stroke.path) >= 2:
            source, target = stroke.path[0], stroke.path[-1]
        else:
            source, target = map(tuple, chroma_warp_position(np.array([stroke.source_rgb, stroke.target_rgb])))
        entries.extend(record(source, target, stroke.params, int(stroke.mode == "point_to_point")))
    for pin in state.pin_points:
        entries.extend(record(pin.position, pin.position, pin.params, 0))
    return np.asarray(entries, dtype=np.float32)[:, None, :]


@lru_cache(maxsize=1)
def _coordinates() -> np.ndarray:
    line = np.linspace(0, 1, CHROMA_LUT_SIZE, dtype=np.float32)
    b, g, r = np.meshgrid(line, line, line, indexing="ij")
    return np.stack((r, g, b), axis=-1).reshape(-1, 3)


@lru_cache(maxsize=1)
def _stroke_inputs() -> tuple[np.ndarray, np.ndarray]:
    from src.features.color.core.warper_chroma709 import chroma_warp_inputs

    return chroma_warp_inputs(_coordinates())


def prepare_chroma_texture(records: np.ndarray) -> np.ndarray:
    data = np.ascontiguousarray(records, dtype=np.float32)
    if data.ndim != 3 or data.shape[1:] != (1, 3) or not np.isfinite(data).all():
        raise ValueError("Invalid Chroma Warper controls")
    return _prepare(data.tobytes())


@lru_cache(maxsize=4)
def _prepare(data: bytes) -> np.ndarray:
    from src.features.color.core.warper_chroma709 import apply_chroma_luma_709, apply_chroma_warp_709
    from src.features.color.core.warper_state import MAX_GRID_DIM, MAX_PINS, MAX_STROKES

    header, *body = np.frombuffer(data, dtype=np.float32).reshape(-1, 3)
    rgb = _coordinates()
    if header[0] == 0:
        grids = []
        position = 0
        for _ in range(2):
            if position >= len(body):
                raise ValueError("Missing Chroma–Luma grid")
            rows, cols = map(int, body[position][:2])
            end = position + 1 + rows * cols
            if not (2 <= rows <= MAX_GRID_DIM and 2 <= cols <= MAX_GRID_DIM and end <= len(body)):
                raise ValueError("Invalid Chroma–Luma grid dimensions")
            grids.append(np.asarray(body[position + 1:end]).reshape(rows, cols, 3))
            position = end
        hue_space = int(header[2])
        if hue_space == -2:
            rgb = _hue_cube(np.asarray(body[position:], dtype=np.float32).tobytes())
        elif hue_space >= 0 and position < len(body):
            rows, cols, stored_components = map(int, body[position])
            components = abs(stored_components)
            if not (2 <= rows <= MAX_GRID_DIM and 2 <= cols <= MAX_GRID_DIM
                    and components in (2, 3) and position + 1 + rows * cols == len(body)):
                raise ValueError("Invalid Hue–Saturation grid in Chroma Warper")
            hue_grid = np.asarray(body[position + 1:]).reshape(rows, cols, 3)[..., :components]
            rgb = _apply_hue_grid(rgb, hue_grid, hue_space, legacy_encoded=stored_components < 0)
        elif position != len(body) or hue_space >= 0:
            raise ValueError("Unexpected Chroma–Luma controls")
        color_space = _SPACE_NAMES.get(int(body[0][2]))
        if color_space is None:
            raise ValueError("Invalid Chroma–Luma color space")
        mapped = apply_chroma_luma_709(rgb, (grids[0], grids[1]), float(header[1]), color_space=color_space)
    elif header[0] == 1:
        strokes, pins = map(int, header[1:])
        if not (0 < strokes <= MAX_STROKES and 0 <= pins <= MAX_PINS and len(body) == (strokes + pins) * 4):
            raise ValueError("Invalid Chroma Warp strokes or pins")
        constraints = np.asarray(body).reshape(-1, 4, 3)
        mapped = apply_chroma_warp_709(rgb, constraints[:strokes], constraints[strokes:],
                                       inputs=_stroke_inputs())
    elif header[0] == 2:
        mapped = _hue_cube(data)
    else:
        raise ValueError("Invalid Chroma Warper mode")
    texture = (mapped - _coordinates()).astype(np.float32).reshape(-1, CHROMA_LUT_SIZE, 3)
    texture.setflags(write=False)
    return texture


@lru_cache(maxsize=4)
def _hue_cube(data: bytes) -> np.ndarray:
    """Reuse the resolved HS stage when only the following CL grid is moving."""
    from src.features.color.core.warper_color import RESOLVE_HS709_SPACES
    from src.features.color.core.warper_response import _hue_and_chroma
    from src.features.color.core.warper_state import MAX_GRID_DIM

    records = np.frombuffer(data, dtype=np.float32).reshape(-1, 3)
    rgb = _coordinates()
    if len(records) < 2 or records[0][0] != 2:
        raise ValueError("Missing Hue–Saturation controls")
    _, space, controls = map(int, records[0])
    rows, cols, stored_components = map(int, records[1])
    components = abs(stored_components)
    end = 2 + rows * cols
    if not (2 <= rows <= MAX_GRID_DIM and 2 <= cols <= MAX_GRID_DIM and components in (2, 3)
            and 0 <= controls <= 1 + rows * cols and len(records) == end + controls
            and space in (*_HUE_SPACES.values(), *RESOLVE_HS709_SPACES)):
        raise ValueError("Invalid Hue–Saturation controls")
    grid = np.asarray(records[2:end]).reshape(rows, cols, 3)[..., :components]
    mapped = _apply_hue_grid(rgb, grid, space, legacy_encoded=stored_components < 0) if grid.any() else rgb
    if not controls:
        return mapped
    white = records[end]
    adjusted = mapped + (_white_point_shift(rgb, white) if white.any() else 0.0)
    if controls > 1:
        hue, chroma, peak = _hue_and_chroma(rgb)
        saturation = chroma / np.maximum(peak, 1e-10)
        weight = np.ones(len(rgb), dtype=rgb.dtype)
        for row, col, _ in records[end + 1:]:
            angular = abs((hue - col / cols + 0.5) % 1.0 - 0.5) * cols
            # The middle pinned ring's measured boundary is near 0.41 in
            # encoded HSV saturation. Preserve the pinned neighborhood without
            # replacing the versioned response of the surrounding controls.
            radius = 0.82 * (row + 1.0) / rows
            radial = abs(saturation - radius) * rows
            weight = np.minimum(weight, 1.0 - np.maximum(1.0 - angular, 0.0)
                                * np.maximum(1.0 - radial, 0.0))
        adjusted = rgb + (adjusted - rgb) * weight[:, None]
    return np.maximum(adjusted, 0.0)


@lru_cache(maxsize=1)
def _white_point_profiles() -> tuple[np.ndarray, np.ndarray]:
    from src.shared.color.lut_loader import LoadedLut, sample_lut_trilinear

    with np.load(Path(__file__).with_name("data") / "hs_white_point_rec709_g24.npz", allow_pickle=False) as data:
        response = data["response"].astype(np.float32)
    if response.shape != (2, 17, 17, 17, 3) or not np.isfinite(response).all():
        raise ValueError("Invalid white-point calibration")
    horizontal, vertical = (sample_lut_trilinear(LoadedLut(table=axis, size=17, format="cube"), _coordinates())
                            for axis in response)
    return horizontal, vertical


def _white_point_shift(rgb: np.ndarray, white: np.ndarray) -> np.ndarray:
    x, y, lightness = white
    shift = np.zeros_like(rgb)
    if x or y:
        horizontal, vertical = _white_point_profiles()
        shift = horizontal * x + vertical * y
    if lightness:
        peak = rgb.max(axis=1)
        desaturation = rgb.min(axis=1) / np.maximum(peak, 1e-10)
        gain = np.exp(0.760 * lightness * desaturation ** 1.66)
        shift += (rgb + shift) * (gain - 1.0)[:, None]
    return shift


def _apply_hue_grid(rgb: np.ndarray, grid: np.ndarray, space: int, *, legacy_encoded: bool = False) -> np.ndarray:
    from src.features.color.core.warper_color import (
        RESOLVE_HS709_SPACES, apply_resolve_hs709_texture, prepare_resolve_hs709_texture,
        _hs709_basis, smoothstep,
    )
    from src.features.color.core.warper_response import _hue_and_chroma

    if space in RESOLVE_HS709_SPACES:
        return apply_resolve_hs709_texture(rgb, prepare_resolve_hs709_texture(grid, space),
                                           space, encoded_input=True)
    linear = rgb if legacy_encoded else rgb ** 2.4
    hue, chroma, peak = _hue_and_chroma(linear)
    saturation = chroma / np.maximum(peak, 1e-10)
    low = peak - chroma
    lightness = (peak + low) * 0.5
    hsl = space == 1
    if hsl:
        saturation = chroma / np.maximum(1.0 - np.abs(2.0 * lightness - 1.0), 1e-10)
    rows, cols = grid.shape[:2]
    x, y = hue * cols - 0.5, np.clip(saturation, 0, 1) * rows - 0.5
    ix, iy = np.floor(x).astype(np.intp), np.floor(y).astype(np.intp)
    left, right = ix.clip(0, cols - 1), (ix + 1).clip(0, cols - 1)
    bottom, top = iy.clip(0, rows - 1), (iy + 1).clip(0, rows - 1)
    dx, dy = (x - ix)[:, None], (y - iy)[:, None]
    lower = grid[bottom, left] * (1 - dx) + grid[bottom, right] * dx
    higher = grid[top, left] * (1 - dx) + grid[top, right] * dx
    delta = (lower * (1 - dy) + higher * dy) * smoothstep(0.02, 0.15, saturation)[:, None]
    basis = _hs709_basis(hue + delta[:, 0])
    sat = np.clip(saturation + delta[:, 1], 0, 1)
    if hsl:
        new_chroma = (1.0 - abs(2.0 * lightness - 1.0)) * sat
        changed = lightness[:, None] + new_chroma[:, None] * (basis - 0.5)
    else:
        changed = (1.0 - sat[:, None] * (1.0 - basis)) * peak[:, None]
        if space == 2:
            weights = np.array([0.299, 0.587, 0.114])
            changed *= np.sqrt((linear ** 2 @ weights) / np.maximum(changed ** 2 @ weights, 1e-10))[:, None]
        elif space == 7:
            weights = np.array([0.2126, 0.7152, 0.0722])
            changed *= ((linear @ weights) / np.maximum(changed @ weights, 1e-10))[:, None]
    if grid.shape[-1] == 3:
        changed *= np.exp2(2.4 * delta[:, 2:3])
    adjusted = (changed * (linear > 0)).clip(0.0)
    if not legacy_encoded:
        adjusted = adjusted ** (1.0 / 2.4)
    return np.where((saturation < 0.02)[:, None], rgb, adjusted)


def apply_chroma_texture(rgb: np.ndarray, texture: np.ndarray, *, encoded_input: bool = False) -> np.ndarray:
    original = np.asarray(rgb)
    color = original.reshape(-1, 3)
    finite = np.isfinite(color).all(axis=-1, keepdims=True)
    sdr = np.clip(np.where(finite, color, 0.0), 0.0, 1.0)
    encoded = sdr if encoded_input else sdr ** (1.0 / 2.4)
    size = CHROMA_LUT_SIZE
    cube = texture.reshape(size, size, size, 3)
    grid = encoded * (size - 1)
    lower = grid.astype(np.intp)
    upper = np.minimum(lower + 1, size - 1)
    r0, g0, b0 = lower.T
    r1, g1, b1 = upper.T
    dr, dg, db = (grid - lower).T[:, :, None]

    def plane(b: np.ndarray) -> np.ndarray:
        bottom = cube[b, g0, r0] * (1 - dr) + cube[b, g0, r1] * dr
        top = cube[b, g1, r0] * (1 - dr) + cube[b, g1, r1] * dr
        return bottom * (1 - dg) + top * dg

    delta = plane(b0) * (1 - db) + plane(b1) * db
    mapped = np.maximum(encoded + delta, 0.0)
    adjusted = mapped if encoded_input else mapped ** 2.4
    result = np.where(finite & delta.any(axis=-1, keepdims=True), adjusted + (color - sdr), color)
    return result.reshape(original.shape).astype(original.dtype, copy=False)
