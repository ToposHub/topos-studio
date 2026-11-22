"""Offline reference oracles for the 2026-11-22 mixer plan.

Examples only: never import these allocating Python implementations into the
realtime callback. Tests run without an audio device or a user project.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import hashlib
import json
import math
from typing import Mapping, Sequence

import numpy as np


@dataclass(frozen=True)
class Edge:
    edge_id: str
    source: str
    target: str


@dataclass(frozen=True)
class PdcPlan:
    order: tuple[str, ...]
    arrival: dict[str, int]
    edge_delay: dict[str, int]


def plan_pdc(latency: Mapping[str, int], edges: Sequence[Edge]) -> PdcPlan:
    """Node latency is intrinsic DSP delay; taps must be explicit graph nodes."""
    if any(type(value) is not int or value < 0 for value in latency.values()):
        raise ValueError('latencies must be non-negative integer samples')
    if len({edge.edge_id for edge in edges}) != len(edges):
        raise ValueError('duplicate edge identity')
    incoming: dict[str, list[Edge]] = {node: [] for node in latency}
    outgoing: dict[str, list[Edge]] = {node: [] for node in latency}
    for edge in edges:
        if edge.source not in latency or edge.target not in latency:
            raise ValueError('unknown graph node')
        incoming[edge.target].append(edge)
        outgoing[edge.source].append(edge)
    indegree = {node: len(value) for node, value in incoming.items()}
    queue = deque(sorted(node for node, count in indegree.items() if count == 0))
    arrival: dict[str, int] = {}
    delay: dict[str, int] = {}
    order: list[str] = []
    while queue:
        node = queue.popleft()
        arrival[node] = max((arrival[e.source] + latency[e.source]
                             for e in incoming[node]), default=0)
        for edge in incoming[node]:
            delay[edge.edge_id] = arrival[node] - arrival[edge.source] - latency[edge.source]
        order.append(node)
        for edge in outgoing[node]:
            indegree[edge.target] -= 1
            if indegree[edge.target] == 0:
                queue.append(edge.target)
    if len(order) != len(latency):
        raise ValueError('routing cycle: no valid PDC plan')
    return PdcPlan(tuple(order), arrival, delay)


class ReferenceDelay:
    """Allocating planar delay oracle; production uses preallocated ring buffers."""

    def __init__(self, channels: int, samples: int) -> None:
        if channels < 1 or samples < 0:
            raise ValueError('invalid delay shape')
        self.memory = np.zeros((channels, samples), dtype=np.float64)

    def process(self, block: np.ndarray) -> np.ndarray:
        if block.ndim != 2 or block.shape[0] != self.memory.shape[0]:
            raise ValueError('layout mismatch')
        joined = np.concatenate((self.memory, block), axis=1)
        count = block.shape[1]
        output = joined[:, :count].copy()
        self.memory = joined[:, count:].copy()
        return output


def gain_with_trim_vca(base_gain: float, trim_factor: float,
                       vca_db: Sequence[float], muted: bool = False) -> float:
    """Volume trim is a LINEAR factor in the existing model, not dB."""
    values = (base_gain, trim_factor, *vca_db)
    if not all(math.isfinite(value) for value in values) or min(base_gain, trim_factor) < 0:
        raise ValueError('invalid gain')
    return 0.0 if muted else base_gain * trim_factor * 10.0 ** (sum(vca_db) / 20.0)


def sound_hash(explicit_sound_payload: Mapping[str, object]) -> str:
    """The caller must supply the COMPLETE sound schema; paths/UI state excluded."""
    encoded = json.dumps(explicit_sound_payload, sort_keys=True, separators=(',', ':'),
                         ensure_ascii=False, allow_nan=False).encode('utf-8')
    return hashlib.sha256(encoded).hexdigest()


def validate_measurement(record: Mapping[str, object], *, target_lufs: float = -23.0,
                         tolerance_lu: float = 0.5, ceiling_dbtp: float = -1.0) -> tuple[str, ...]:
    """Synthetic programme profile example, not a universal delivery standard."""
    errors: list[str] = []
    if record.get('status') != 'complete':
        errors.append('measurement incomplete')
    required = ('integrated_lufs', 'true_peak_dbtp', 'peak_dbfs')
    for key in required:
        value = record.get(key)
        if type(value) not in (int, float) or not math.isfinite(value):
            errors.append(f'{key}: missing/non-finite')
    if errors:
        return tuple(errors)
    if abs(float(record['integrated_lufs']) - target_lufs) > tolerance_lu:
        errors.append('loudness out of tolerance')
    if float(record['true_peak_dbtp']) > ceiling_dbtp:
        errors.append('true peak exceeded')
    if float(record['peak_dbfs']) > 0.0:
        errors.append('sample clipping')
    return tuple(errors)


def gated_loudness_from_k_energy(block_energy: np.ndarray) -> float:
    """Gate oracle ONLY: input is channel-weighted K-filtered 400 ms energy,
    measured every 100 ms. Does not implement K filters, dialogue detection,
    short-term, true peak, or LRA. Never feed raw PCM/RMS to this function.
    """
    energy = np.asarray(block_energy, dtype=np.float64)
    if energy.ndim != 1 or np.any(~np.isfinite(energy)) or np.any(energy < 0.0):
        raise ValueError('invalid block energy')
    absolute_energy = 10.0 ** ((-70.0 + 0.691) / 10.0)
    absolute = energy[energy > absolute_energy]
    if absolute.size == 0:
        return -math.inf
    relative_energy = absolute.mean() * 10.0 ** (-10.0 / 10.0)
    final = absolute[absolute > relative_energy]
    return -0.691 + 10.0 * math.log10(float(final.mean()))


def estimate_delay(a: np.ndarray, b: np.ndarray, max_lag: int) -> tuple[int, float]:
    """GCC-PHAT: positive lag means b is delayed relative to a.
    Peak ratio is an ambiguity indicator, not a calibrated probability.
    """
    a, b = np.asarray(a, dtype=np.float64), np.asarray(b, dtype=np.float64)
    if a.ndim != 1 or b.ndim != 1 or min(a.size, b.size) < 8:
        raise ValueError('mono windows of at least 8 samples required')
    if np.any(~np.isfinite(a)) or np.any(~np.isfinite(b)):
        raise ValueError('non-finite input')
    a, b = a - a.mean(), b - b.mean()
    if min(float(np.linalg.norm(a)), float(np.linalg.norm(b))) < 1e-10:
        raise ValueError('no signal')
    size = 1 << (a.size + b.size - 2).bit_length()
    if max_lag < 0 or max_lag >= min(a.size, b.size):
        raise ValueError('invalid search window')
    cross = np.fft.rfft(b, size) * np.conj(np.fft.rfft(a, size))
    correlation = np.fft.irfft(cross / np.maximum(np.abs(cross), 1e-12), size)
    lags = np.arange(-max_lag, max_lag + 1)
    scores = np.abs(correlation[lags % size])
    winner = int(np.argmax(scores))
    rivals = scores[np.abs(lags - lags[winner]) > 2]
    confidence = float(scores[winner] / max(float(rivals.max(initial=0.0)), 1e-12))
    return int(lags[winner]), confidence


def crossfade(a: np.ndarray, b: np.ndarray, *, correlated: bool) -> np.ndarray:
    """Overlap windows with equal gain for correlated sources, equal power
    for uncorrelated room tone. This does not synthesize missing source handles.
    """
    if a.shape != b.shape or a.ndim != 2 or a.shape[1] < 2:
        raise ValueError('equal planar overlap windows required')
    t = np.linspace(0.0, 1.0, a.shape[1])[None, :]
    if correlated:
        return a * (1.0 - t) + b * t
    return a * np.cos(t * np.pi / 2.0) + b * np.sin(t * np.pi / 2.0)


def sample_at_frame(frame: int, fps_num: int, fps_den: int, rate: int) -> int:
    """Floor absolute rational boundaries. Adjacent windows share one boundary."""
    if fps_num <= 0 or fps_den <= 0 or rate <= 0 or frame < 0:
        raise ValueError('invalid time coordinate')
    return frame * rate * fps_den // fps_num


def null_error(reference: np.ndarray, candidate: np.ndarray) -> tuple[float, float]:
    if reference.shape != candidate.shape or reference.size == 0:
        raise ValueError('align layout/start/count before null testing')
    delta = candidate.astype(np.float64) - reference.astype(np.float64)
    if np.any(~np.isfinite(delta)):
        raise ValueError('non-finite render')
    return float(np.max(np.abs(delta))), float(np.sqrt(np.mean(delta ** 2)))
