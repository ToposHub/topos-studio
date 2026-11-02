"""Versioned clip stabilization payload and constant-time frame lookup."""
from __future__ import annotations

import copy
from collections.abc import Mapping
from typing import Any

import numpy as np

DEFAULT_PARAMS: dict[str, Any] = {
    'mode': 'perspective', 'camera_lock': False, 'zoom': True,
    'cropping_ratio': .5, 'smooth': .25, 'strength': 1.0, 'bypass': False,
}


def validate_stabilization_state(state: object) -> dict[str, Any]:
    """Freeze a JSON-safe state at commit/load boundaries; unknown keys survive."""
    if state is None or state == {}:
        return {}
    if not isinstance(state, Mapping):
        raise ValueError('Stabilization state must be a mapping')
    if state.get('schema_version', 1) != 1:
        raise ValueError('Unsupported stabilization schema version')
    result = copy.deepcopy(dict(state))
    params = {**DEFAULT_PARAMS, **dict(result.get('params') or {})}
    if params['mode'] not in ('perspective', 'similarity', 'translation'):
        raise ValueError('Unknown stabilization motion model')
    for key in ('smooth', 'strength', 'cropping_ratio'):
        number = float(params[key])
        if not np.isfinite(number) or not 0.0 <= number <= 1.0:
            raise ValueError(f'Invalid stabilization {key}')
        params[key] = number
    for key in ('camera_lock', 'zoom', 'bypass'):
        if not isinstance(params[key], bool):
            raise ValueError(f'Stabilization {key} must be a boolean')
    result['params'] = params
    result['schema_version'] = 1
    for key in ('source_width', 'source_height'):
        value = int(result.get(key, 0))
        if value <= 0:
            raise ValueError('Stabilization source dimensions must be positive')
        result[key] = value
    for key in ('corrections', 'transforms'):
        values = result.get(key, {})
        if not isinstance(values, Mapping):
            raise ValueError(f'Stabilization {key} must be frame keyed')
        matrices: dict[str, list[float]] = {}
        for frame, entry in values.items():
            matrix = np.asarray(entry, dtype=np.float64)
            if matrix.size != 9 or not np.isfinite(matrix).all():
                raise ValueError('Stabilization matrix must contain nine finite numbers')
            matrix = matrix.reshape(3, 3)
            if abs(np.linalg.det(matrix)) < 1e-12:
                raise ValueError('Stabilization matrix is singular')
            matrices[str(int(frame))] = matrix.ravel().tolist()
        result[key] = matrices
    return result


def stabilization_matrix(state: object, source_frame: int) -> np.ndarray | None:
    """Read only the current correction; missing/failed frames remain unchanged."""
    if not isinstance(state, Mapping) or not state:
        return None
    if state.get('schema_version', 1) != 1:
        raise ValueError('Unsupported stabilization schema version')
    params = state.get('params') or {}
    if params.get('bypass', False) or (not params.get('camera_lock', False)
                                     and float(params.get('strength', 1.0)) == 0.0):
        return None
    entry = (state.get('corrections') or {}).get(str(int(source_frame)))
    if entry is None:
        return None
    matrix = np.asarray(entry, dtype=np.float64).reshape(3, 3)
    if not np.isfinite(matrix).all() or abs(np.linalg.det(matrix)) < 1e-12:
        raise ValueError('Invalid stabilization correction')
    if np.allclose(matrix, np.eye(3), atol=1e-10, rtol=0.0):
        return None
    return matrix
