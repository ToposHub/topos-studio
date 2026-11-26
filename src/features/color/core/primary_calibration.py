"""Pointwise primary grading from measured Rec.709 Gamma 2.4 responses.

The profile stores signed RGB response grids with B, G, R axes. Individual
controls contribute additive, piecewise-linear changes in encoded RGB. A
weighted sum of those grids needs only one trilinear sample per pixel because
the sampler is linear in the table values. The sum is sampled before clipping
or converting back to linear RGB.
"""

from __future__ import annotations

import math
from bisect import bisect_right
from collections import OrderedDict
from collections.abc import Mapping
from dataclasses import dataclass
from functools import lru_cache
from numbers import Real
from pathlib import Path
from threading import Lock
from typing import Any, Literal

import numpy as np

from src.shared.color.lut_loader import LoadedLut, sample_lut_trilinear


PRIMARY_CALIBRATION_PATH = (
    Path(__file__).with_name("data") / "primary_calibration_rec709_g24_v1.npz"
)
PRIMARY_CALIBRATION_GAMMA = 2.4
PRIMARY_CALIBRATION_SCHEMA_VERSION = 1
PRIMARY_CALIBRATION_VERSION_KEY = "primary_calibration_version"
PRIMARY_CALIBRATION_CURRENT_VERSION = 2
PRIMARY_CALIBRATION_TEMP_SCALE = 100.0

Control = tuple[str, str]
Extrapolation = Literal["hold", "linear", "error"]
_GridKey = tuple[tuple[Control, float], ...]
_CHANNELS = frozenset({"r", "g", "b", "y", "value"})
_GRID_CACHE_SIZE = 8
_SAMPLE_BATCH_SIZE = 262_144


@dataclass(frozen=True)
class _Response:
    knots: tuple[float, ...]
    tables: tuple[np.ndarray | None, ...]


def _readonly_grid(grid: np.ndarray) -> np.ndarray:
    # Back with immutable bytes so callers cannot re-enable writes to the cache.
    return np.frombuffer(grid.tobytes(), dtype=np.float32).reshape(grid.shape)


def _rgb_array(rgb: np.ndarray) -> np.ndarray:
    rgb = np.asarray(rgb, dtype=np.float32)
    if rgb.ndim == 0 or rgb.shape[-1] != 3 or not np.isfinite(rgb).all():
        raise ValueError("Expected finite RGB values with shape (..., 3)")
    return rgb


def _apply_encoded_grid(rgb: np.ndarray, grid: np.ndarray) -> np.ndarray:
    lut = LoadedLut(table=grid, size=grid.shape[0], format="cube")
    points = rgb.reshape(-1, 3)
    result = np.empty_like(points)
    # Keep the sampler's temporary arrays bounded for full-resolution CPU work.
    for start in range(0, len(points), _SAMPLE_BATCH_SIZE):
        end = start + _SAMPLE_BATCH_SIZE
        block = points[start:end]
        result[start:end] = np.clip(block + sample_lut_trilinear(lut, block), 0.0, 1.0)
    return result.reshape(rgb.shape)


class PrimaryCalibration:
    """An immutable set of measured controls with a bounded response-grid cache.

    Control keys are ``(name, channel)`` with ``value`` as the scalar channel.
    Values are numeric deltas from neutral in the source control's units. The
    evaluator rejects unsupported nonzero controls. The supported key set and
    measured intervals are available for callers that need to gate their UI.
    """

    def __init__(self, responses: dict[Control, _Response], size: int) -> None:
        self._responses = responses
        self._identity = _readonly_grid(np.zeros((size, size, size, 3), dtype=np.float32))
        self._cache: OrderedDict[_GridKey, np.ndarray] = OrderedDict()
        self._cache_lock = Lock()

    @classmethod
    def from_npz(cls, path: Path | str = PRIMARY_CALIBRATION_PATH) -> PrimaryCalibration:
        """Load versioned float16 measurements. A zero knot is added per control."""
        with np.load(path, allow_pickle=False) as archive:
            version = archive["schema_version"]
            controls = archive["control_names"]
            channels = archive["channel_names"]
            knots = archive["knots"]
            deltas = archive["deltas"]
        if (version.ndim != 0 or version.dtype.kind not in "iu"
                or int(version) != PRIMARY_CALIBRATION_SCHEMA_VERSION):
            raise ValueError("Unsupported primary calibration schema version")
        if (knots.ndim != 1 or knots.size == 0 or knots.dtype.kind != "f"
                or knots.dtype.itemsize != 8 or not np.isfinite(knots).all()
                or np.any(knots == 0.0)):
            raise ValueError("Primary calibration knots must be finite, nonzero float64 deltas")
        if (controls.shape != knots.shape or channels.shape != knots.shape
                or controls.dtype.kind != "U" or channels.dtype.kind != "U"):
            raise ValueError("Primary calibration names and channels must match the knots")
        if (deltas.ndim != 5 or deltas.shape[0] != knots.size or deltas.shape[-1] != 3
                or deltas.shape[1] < 2 or deltas.shape[1:4] != (deltas.shape[1],) * 3
                or deltas.dtype.kind != "f" or deltas.dtype.itemsize != 2
                or not np.isfinite(deltas).all() or np.any(np.abs(deltas) > 1.0)):
            raise ValueError("Expected finite signed float16 deltas with shape (K, N, N, N, 3)")

        records: dict[Control, dict[float, np.ndarray | None]] = {}
        for control, channel, knot, table in zip(controls, channels, knots, deltas):
            key = (str(control), str(channel))
            if not control or channel not in _CHANNELS:
                raise ValueError(f"Invalid primary calibration control {key!r}")
            measured = records.setdefault(key, {0.0: None})
            if float(knot) in measured:
                raise ValueError(f"Duplicate primary calibration knot for {key!r} at {knot}")
            measured[float(knot)] = table

        responses = {}
        for key, measured in records.items():
            ordered_knots = tuple(sorted(measured))
            responses[key] = _Response(ordered_knots, tuple(measured[knot] for knot in ordered_knots))
        return cls(responses, deltas.shape[1])

    @property
    def supported_controls(self) -> frozenset[Control]:
        return frozenset(self._responses)

    def measured_interval(self, control: Control) -> tuple[float, float]:
        """Return the measured range for a control, including implicit neutral."""
        knots = self._responses[control].knots
        return knots[0], knots[-1]

    def build_grid(self, controls: Mapping[Control, float], *,
                   extrapolation: Extrapolation = "hold") -> np.ndarray:
        """Return a read-only float32 grid of signed encoded RGB differences.

        ``hold`` uses the nearest endpoint outside a sampled interval.
        ``linear`` continues the adjacent segment. ``error`` limits the
        evaluator to the measured interval for strict comparisons.
        """
        if extrapolation not in ("hold", "linear", "error"):
            raise ValueError(f"Unknown primary calibration extrapolation {extrapolation!r}")
        active: list[tuple[Control, float]] = []
        for control, delta in controls.items():
            if isinstance(delta, bool) or not isinstance(delta, Real) or not math.isfinite(delta):
                raise ValueError(f"Expected a finite numeric delta for {control!r}")
            if delta == 0.0:
                continue
            response = self._responses.get(control)
            if response is None:
                raise ValueError(f"Unsupported primary calibration control {control!r}")
            amount = float(delta)
            lower, upper = response.knots[0], response.knots[-1]
            if not lower <= amount <= upper:
                if extrapolation == "error":
                    raise ValueError(f"Delta {amount} for {control!r} lies outside the measured interval")
                if extrapolation == "hold":
                    amount = min(max(amount, lower), upper)
            if amount:
                active.append((control, amount))
        if not active:
            return self._identity

        key = tuple(sorted(active))
        with self._cache_lock:
            cached = self._cache.get(key)
            if cached is not None:
                self._cache.move_to_end(key)
                return cached
            grid = np.zeros_like(self._identity)
            for control, delta in key:
                response = self._responses[control]
                knots = response.knots
                high = min(max(bisect_right(knots, delta), 1), len(knots) - 1)
                low = high - 1
                t = (delta - knots[low]) / (knots[high] - knots[low])
                for table, weight in ((response.tables[low], 1.0 - t), (response.tables[high], t)):
                    if table is not None and weight:
                        # Very large extrapolated values cannot safely occupy a GPU grid.
                        if not math.isfinite(weight) or abs(weight) > np.finfo(np.float32).max:
                            raise ValueError(f"Extrapolated response for {control!r} exceeds float32 range")
                        grid += np.multiply(table, np.float32(weight), dtype=np.float32)
            if not np.isfinite(grid).all():
                raise ValueError("Combined primary calibration response exceeds float32 range")
            immutable = _readonly_grid(grid)
            self._cache[key] = immutable
            if len(self._cache) > _GRID_CACHE_SIZE:
                self._cache.popitem(last=False)
            return immutable

    def apply_encoded(self, rgb: np.ndarray, controls: Mapping[Control, float], *,
                      extrapolation: Extrapolation = "hold") -> np.ndarray:
        """Evaluate RGB data already in the measured Gamma 2.4 code domain."""
        rgb = _rgb_array(rgb)
        if np.any(rgb < 0.0) or np.any(rgb > 1.0):
            raise ValueError("Encoded primary calibration expects RGB within [0, 1]")
        grid = self.build_grid(controls, extrapolation=extrapolation)
        if grid is self._identity:
            return rgb.copy()
        return _apply_encoded_grid(rgb, grid)

    def apply_linear(self, rgb: np.ndarray, controls: Mapping[Control, float], *,
                     extrapolation: Extrapolation = "hold") -> np.ndarray:
        """Evaluate linear Rec.709 while retaining the input's signed/HDR excess.

        Out-of-range channels extend the calibrated SDR boundary with unit slope
        in linear RGB. This extension is continuous and gives an exact bypass
        for neutral controls, including negative and super-white values.
        """
        rgb = _rgb_array(rgb)
        grid = self.build_grid(controls, extrapolation=extrapolation)
        if grid is self._identity:
            return rgb.copy()
        return apply_calibration_grid(rgb, grid)


def apply_calibration_grid(
    rgb: np.ndarray, grid: np.ndarray, *, encoded_input: bool = False,
) -> np.ndarray:
    """Apply the measured lattice in Gamma 2.4, retaining out-of-range excess."""
    sdr = np.clip(rgb, 0.0, 1.0)
    encoded = sdr if encoded_input else np.power(sdr, np.float32(1.0 / PRIMARY_CALIBRATION_GAMMA))
    graded = _apply_encoded_grid(encoded, grid)
    if not encoded_input:
        graded = np.power(graded, np.float32(PRIMARY_CALIBRATION_GAMMA))
    return graded + (rgb - sdr)


def _grading_params(layer: Any) -> Mapping[str, Any]:
    value = (layer.get("grading_params") if isinstance(layer, Mapping)
             else getattr(layer, "grading_params", None))
    return value if isinstance(value, Mapping) else {}


def primary_calibration_version(layer: Any) -> int:
    """An absent or zero version is an existing Topos grade."""
    version = _grading_params(layer).get(PRIMARY_CALIBRATION_VERSION_KEY, 0)
    if (isinstance(version, bool) or not isinstance(version, Real)
            or version not in (0, 1, PRIMARY_CALIBRATION_CURRENT_VERSION)):
        raise ValueError(f"Unsupported primary calibration version: {version!r}")
    return int(version)


def grade_uses_calibration(layer: Any) -> bool:
    return primary_calibration_version(layer) != 0


def ungraded_primary_params(params: Mapping[str, Any]) -> bool:
    """Recognize neutral, unversioned grades without rewriting older values."""
    from src.features.color.core.base_grade import BASE_GRADE_DEFAULTS
    from src.features.color.core.chromogen import CHROMOGEN_KEY
    from src.features.color.core.layer import DEFAULT_EXTENDED_GRADING_PARAMS

    defaults = DEFAULT_EXTENDED_GRADING_PARAMS
    if isinstance(params.get(PRIMARY_CALIBRATION_VERSION_KEY), bool):
        return False
    for key, value in params.items():
        if key in BASE_GRADE_DEFAULTS or key == CHROMOGEN_KEY:
            continue
        if isinstance(value, (tuple, list)):
            if tuple(value) != defaults.get(key, (0, 0, 0)):
                return False
        else:
            default = defaults.get(key, 0)
            if not isinstance(value, Real) or not isinstance(default, Real):
                return False
            if not math.isclose(value, default, rel_tol=0.0, abs_tol=1e-9):
                return False
    return True


@lru_cache(maxsize=1)
def _built_in_profile() -> PrimaryCalibration:
    return PrimaryCalibration.from_npz()


_SCALAR_SCALES = {
    "temp": PRIMARY_CALIBRATION_TEMP_SCALE, "tint": 1.0,
    "contrast": 0.01, "vibrance": 1.0, "shadow": 1.0,
    "highlights": 1.0, "saturation": 0.5, "hue_shift": 100.0 / 360.0,
}
_LOG_WHEELS = ("log_shadow", "log_midtone", "log_highlight")


def calibrated_grade_inputs(layer: Any) -> tuple[np.ndarray | None, Mapping[str, Any]] | None:
    """Split a versioned grade into one measured grid and remaining Topos controls."""
    version = primary_calibration_version(layer)
    if not version:
        return None
    from src.features.color.core.composition_uniforms import params_to_offset_rgb, params_to_rgb

    params = _grading_params(layer)
    residual = dict(params)
    refined = version == PRIMARY_CALIBRATION_CURRENT_VERSION
    controls: dict[Control, float] = {}
    # Match the default Resolve pivot. Custom pivots and range boundaries retain
    # their existing Topos algorithms because the profile only samples defaults.
    default_pivot = params.get("pivot", 0.18) == 0.18
    default_log_ranges = (params.get("log_low_range", 0.333) == 0.333
                          and params.get("log_high_range", 0.550) == 0.550)
    for control, scale in _SCALAR_SCALES.items():
        if control == "contrast" and not (refined or default_pivot):
            continue
        amount = float(params.get(control, 0.0))
        controls[control, "value"] = amount * scale
        residual[control] = 0.0
    for control in ("lift", "gamma", "gain"):
        factors = params_to_rgb(*params.get(control, (0.0, 0.0, 0.0)))
        # The stored HSB model has three degrees of freedom. The channel
        # median recovers neutral Y when one displayed channel is adjusted.
        master = sorted(factors)[1]
        controls[control, "y"] = master - 1.0
        for channel, factor in zip("rgb", factors):
            controls[control, channel] = factor - master
        residual[control] = (0.0, 0.0, 0.0)
    for control in ("offset", "log_offset", *_LOG_WHEELS):
        if control in _LOG_WHEELS and not (refined or default_log_ranges):
            continue
        factors = params_to_offset_rgb(*params.get(control, (0.0, 0.0, 0.0)))
        is_offset = control in ("offset", "log_offset")
        if control == "log_offset" and params.get("offset") == params.get("log_offset"):
            factors = (0.0, 0.0, 0.0)  # The calibrated panel shares this state.
        key = "offset" if is_offset else control
        for channel, factor in zip("rgb", factors):
            controls[key, channel] = controls.get((key, channel), 0.0) + factor * (100.0 if is_offset else 1.0)
        residual[control] = (0.0, 0.0, 0.0)
    if refined and any(not math.isfinite(amount) for amount in controls.values()):
        raise ValueError("Primary calibration controls must be finite")
    active = {key: amount for key, amount in controls.items() if abs(amount) > 1e-7}
    if refined:
        from src.features.color.core.primary_calibration_v2 import refined_grid

        pivot, low, high, lum_mix = (
            float(params[key]) if params.get(key) is not None else default
            for key, default in (
                ("pivot", 0.18), ("log_low_range", 0.333), ("log_high_range", 0.550), ("lum_mix", 100.0),
            )
        )
        if not all(math.isfinite(value) for value in (pivot, low, high, lum_mix)):
            raise ValueError("Primary calibration boundaries and Lum Mix must be finite")
        detail = float(params.get("mid_detail") or 0.0)
        if not math.isfinite(detail) or abs(detail) > float(np.finfo(np.float32).max):
            raise ValueError("Primary calibration Midtone Detail strength must be finite and fit float32")
        residual["lum_mix"] = 100.0
        grid = refined_grid(
            _built_in_profile(), tuple(sorted(active.items())), pivot + 0.255, low, high, lum_mix,
        ) if active else None
    else:
        grid = _built_in_profile().build_grid(active, extrapolation="linear") if active else None
    return grid, residual
