"""Volume grid data layer (B1, downgraded path).

Spike result (plan constraint 13): ``pyopenvdb``/``openvdb`` are NOT
importable in this environment and no wheel is packaged, so the full
OpenVDB pipeline failed its packaging gate.  Per the plan's fallback
clause this layer therefore:

* loads the project's own dense volume format (``.npz`` with a ``density``
  (D, H, W) float32 array + optional ``spacing``) — fully functional;
* detects ``.vdb`` files and raises :class:`VolumeDependencyError` with an
  actionable message (convert them offline with ``tools/convert_vdb.py``
  once the dependency lands) instead of crashing or silently skipping.

Raymarching itself is deterministic CPU integration in the node; the GPU
scene3d pass waits for the dependency decision.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np


class VolumeDependencyError(RuntimeError):
    """Raised when a feature needs pyopenvdb which is not packaged."""


@dataclass
class VolumeGrid:
    """Dense scalar density volume, (D, H, W) float32, values ≥ 0.

    ``spacing`` is the world-space size of one voxel on each axis (npz
    round-trips it; ``.vdb`` converters must fill it or physical
    absorption is wrong).  NaN/inf are scrubbed to 0 at construction so a
    bad export cannot poison the raymarch.
    """

    density: "np.ndarray"
    spacing: float = 1.0

    def __post_init__(self) -> None:
        self.density = np.ascontiguousarray(
            self.density, dtype=np.float32)
        if self.density.ndim != 3:
            raise ValueError("volume density must be 3D (D, H, W)")
        self.density = np.nan_to_num(self.density, nan=0.0, posinf=0.0,
                                     neginf=0.0)
        np.clip(self.density, 0.0, None, out=self.density)

    @property
    def shape(self):
        return self.density.shape


def load_volume(path: str) -> VolumeGrid:
    """Load a ``.npz`` dense volume; ``.vdb`` hits the dependency gate."""
    suffix = Path(path).suffix.lower()
    if suffix == ".vdb":
        raise VolumeDependencyError(
            f"OpenVDB support is not packaged (pyopenvdb unavailable); "
            f"convert '{Path(path).name}' to the .npz dense volume format "
            f"offline (tools/convert_vdb.py)")
    if suffix != ".npz":
        raise ValueError(f"unsupported volume format: {suffix}")
    with np.load(path, allow_pickle=False) as data:
        if "density" not in data:
            raise ValueError("volume .npz lacks a 'density' array")
        spacing = 1.0
        if "spacing" in data:
            try:
                spacing = float(np.atleast_1d(data["spacing"]).ravel()[0])
            except (TypeError, ValueError, IndexError):
                spacing = 1.0
        return VolumeGrid(
            np.asarray(data["density"], dtype=np.float32),
            spacing=spacing if spacing > 0.0 else 1.0)


def save_volume(path: str, density: "np.ndarray",
                spacing: float = 1.0) -> None:
    """Write the dense volume format (fixture/converter helper)."""
    np.savez_compressed(path,
                        density=np.ascontiguousarray(
                            density, dtype=np.float32),
                        spacing=np.float32(spacing))


def integrate_transmittance(density_line: "np.ndarray",
                            step_world: float) -> float:
    """Beer–Lambert transmittance along one sampled ray line.

    T = exp(-Σ σ_i · Δworld) — the analytic surface used by the node's
    CPU raymarch and its tests.  ``step_world`` is the world-space length
    each density sample represents.
    """
    total = float(np.sum(np.asarray(density_line, dtype=np.float32)))
    return float(np.exp(-total * float(step_world)))
