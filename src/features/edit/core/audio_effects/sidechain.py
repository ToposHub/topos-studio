"""Shared routing contract for detector-only external audio inputs."""
from __future__ import annotations

from typing import Any

import numpy as np


def detector_source_id(params: dict[str, Any]) -> str:
    """Use stable track IDs; old compressor-style source aliases remain valid."""
    if params.get("sc_on") is False:
        return "internal"
    return str(params.get("sidechain_source_id", params.get("sc_source", "internal"))
               or "internal")


def detector_input(
    samples: np.ndarray, sidechain: np.ndarray | None, source_id: str,
) -> tuple[np.ndarray, str]:
    """Missing/invalid sources fall back to internal detection with a diagnostic."""
    if source_id == "internal":
        return samples, "internal"
    if sidechain is None:
        return samples, "missing"
    if (not isinstance(sidechain, np.ndarray) or sidechain.ndim != 2
            or sidechain.shape[0] < 1 or sidechain.shape[-1] != samples.shape[-1]
            or not np.isfinite(sidechain).all()):
        return samples, "invalid"
    return sidechain, "external"
