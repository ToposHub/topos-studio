"""Evaluate a standalone ACEScct-to-display cube for monitoring."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import numpy as np

from src.shared.color.color_space_descriptor import ACESCG_SCENE, descriptor_for_space_preset
from src.shared.color.color_transform_provider import BuiltinColorProvider
from src.shared.color.lut_loader import LoadedLut, load_lut, resolve_lut_path, sample_lut_tetrahedral
from src.shared.color.lut_resource_registry import MAX_LUT_FILE_BYTES, MAX_LUT_GRID_SIZE


ACES_MONITOR_LUT_SPACE = descriptor_for_space_preset("acescct")
_SIGNAL_PROVIDER = BuiltinColorProvider()
_SAMPLE_BATCH_PIXELS = 65536


def _read_monitor_lut(path_or_uri: str) -> LoadedLut:
    path = Path(resolve_lut_path(path_or_uri))
    if path.suffix.lower() != ".cube":
        raise ValueError("ACES monitoring requires a 3D .cube LUT.")
    if path.stat().st_size > MAX_LUT_FILE_BYTES:
        raise ValueError("The ACES monitoring LUT exceeds the file size limit.")
    try:
        lut = load_lut(str(path))
    except (IndexError, ValueError) as exc:
        raise ValueError(f"Invalid ACES monitoring LUT: {exc}") from exc
    if not 2 <= lut.size <= MAX_LUT_GRID_SIZE or not np.isfinite(lut.table).all():
        raise ValueError(f"The monitoring LUT must have a finite 3D grid between 2 and {MAX_LUT_GRID_SIZE}.")
    if lut.shaper_1d is not None:
        raise ValueError("Use a standalone 3D ACEScct LUT without a 1D shaper.")
    domain = np.asarray((lut.domain_min, lut.domain_max), dtype=np.float64)
    if domain.shape != (2, 3) or not np.isfinite(domain).all():
        raise ValueError("The monitoring LUT must have three finite domain pairs.")
    lower, upper = domain
    with np.errstate(over="ignore", invalid="ignore"):
        span = (upper - lower).astype(np.float32)
        endpoints = domain.astype(np.float32)
    if (not np.isfinite(endpoints).all() or not np.isfinite(span).all()
            or not (endpoints[1] > endpoints[0]).all()):
        raise ValueError("The monitoring LUT domain maximum must exceed its minimum in each channel.")
    return lut


def validate_aces_monitor_lut(path_or_uri: str) -> None:
    """Validate a standalone 3D cube before importing it into the project."""
    _read_monitor_lut(path_or_uri)


@lru_cache(maxsize=2)
def _embedded_monitor_lut(uri: str) -> LoadedLut:
    # Embedded URIs are content-addressed and immutable across project loads.
    return _read_monitor_lut(uri)


def load_aces_monitor_lut(path_or_uri: str) -> LoadedLut:
    """Load a validated cube, caching immutable embedded project resources."""
    if path_or_uri.startswith("topos-res://"):
        return _embedded_monitor_lut(path_or_uri)
    return _read_monitor_lut(path_or_uri)


def apply_aces_monitor_lut(scene_rgb: np.ndarray, lut_uri: str) -> np.ndarray:
    """Transform straight float scene-linear AP1 RGB(A) through an ACEScct cube."""
    if scene_rgb.ndim < 1 or scene_rgb.shape[-1] not in (3, 4) or scene_rgb.dtype.kind != "f":
        raise ValueError("Expected floating-point scene-linear AP1 RGB or RGBA.")
    lut = load_aces_monitor_lut(lut_uri)
    result = np.empty_like(scene_rgb, order="C")
    channels = scene_rgb.shape[-1]
    output = result.reshape(-1, channels)
    source = scene_rgb.reshape(-1, channels)
    for begin in range(0, len(output), _SAMPLE_BATCH_PIXELS):
        selection = slice(begin, begin + _SAMPLE_BATCH_PIXELS)
        encoded = _SIGNAL_PROVIDER.transform_signal(source[selection], ACESCG_SCENE, ACES_MONITOR_LUT_SPACE)
        output[selection, :3] = sample_lut_tetrahedral(lut, encoded[..., :3])
        output[selection, 3:] = source[selection, 3:]
    return result
