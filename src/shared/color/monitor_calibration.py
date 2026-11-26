"""Machine-local display-code LUTs applied after a monitor's output transform."""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from os import stat_result
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

import numpy as np

from src.shared.color.lut_resource_registry import MAX_LUT_FILE_BYTES


DESKTOP_CALIBRATION_LUT = "color.monitor.desktop_calibration_lut"
EXTERNAL_CALIBRATION_LUT = "color.monitor.external_calibration_lut"
CALIBRATION_LUT_SUFFIXES = (".cube", ".3dl")
CalibrationRevision = tuple[int, int, int, int]


@dataclass(frozen=True)
class MonitorCalibration:
    """An immutable OCIO processor for normalized display-encoded RGB."""

    processor: Any

    def apply_rgb(self, rgb: np.ndarray) -> np.ndarray:
        if rgb.ndim < 1 or rgb.shape[-1] != 3 or rgb.dtype.kind != "f":
            raise ValueError("Monitor calibration expects floating-point RGB.")
        result = np.array(rgb, dtype=np.float32, order="C", copy=True)
        if result.size:
            self.processor.applyRGB(result.reshape(-1, 3))
        return result


def _revision(stat: stat_result) -> CalibrationRevision:
    return stat.st_mtime_ns, stat.st_size, stat.st_ctime_ns, stat.st_ino


def monitor_calibration_revision(path: str) -> CalibrationRevision:
    """Identify a local LUT revision, including replacements at the same path."""
    return _revision(Path(path).expanduser().stat())


@lru_cache(maxsize=8)
def _load_calibration(path: Path, revision: CalibrationRevision) -> MonitorCalibration:
    size = revision[1]
    if not 0 < size <= MAX_LUT_FILE_BYTES:
        raise ValueError("The monitor calibration LUT is empty or exceeds the size limit.")
    try:
        import PyOpenColorIO as ocio
    except ImportError as exc:
        raise ValueError("OpenColorIO is required for monitor calibration.") from exc

    with path.open("rb") as source:
        payload = source.read(MAX_LUT_FILE_BYTES + 1)
    if len(payload) != size or _revision(path.stat()) != revision:
        raise ValueError("The monitor calibration LUT changed while it was being read.")

    # OCIO caches parsed transforms by filename. Compile from an isolated
    # snapshot so a recalibration can replace a file at the same user path.
    try:
        with TemporaryDirectory(prefix="topos-monitor-calibration-") as directory:
            snapshot = Path(directory) / f"profile{path.suffix.lower()}"
            snapshot.write_bytes(payload)
            transform = ocio.FileTransform(src=str(snapshot), interpolation=ocio.INTERP_BEST)
            processor = ocio.Config.CreateRaw().getProcessor(transform).getDefaultCPUProcessor()
        calibration = MonitorCalibration(processor)
        axis = np.linspace(0.0, 1.0, 9, dtype=np.float32)
        grid = np.stack(np.meshgrid(axis, axis, axis, indexing="ij"), axis=-1)
        if not np.isfinite(calibration.apply_rgb(grid)).all():
            raise ValueError("The monitor calibration LUT contains invalid output values.")
    except ocio.Exception as exc:
        raise ValueError(f"Invalid monitor calibration LUT: {exc}") from exc
    return calibration


def load_monitor_calibration(path: str) -> MonitorCalibration:
    """Load a `.cube` or `.3dl` defined in the display branch's signal space."""
    source = Path(path).expanduser().resolve()
    if source.suffix.lower() not in CALIBRATION_LUT_SUFFIXES:
        raise ValueError("Select a .cube or .3dl monitor calibration LUT.")
    return _load_calibration(source, _revision(source.stat()))
