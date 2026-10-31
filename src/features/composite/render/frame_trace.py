"""P0-4: per-frame Composite performance trace.

Every comp_clip frame gets a stable ``trace_id`` that chains the
Composite-side stages (graph eval, strategy decision, tiles, readback,
cache, producer) so the same frame can be correlated end-to-end — the Edit
timeline / encoder supplies its own trace_id through the renderer APIs and
receives the same id back on the result.

Schema rules (plan §六 P0-4):

- CPU wall time and GPU time are separate fields. This backend has no GPU
  timestamp queries wired, so ``gpu_time_ms`` stays ``None`` and
  ``gpu_time_unavailable`` is ``True`` — a missing measurement is marked,
  never reported as 0.
- ``cpu_upload_bytes``/``cpu_download_bytes`` are ``None`` when unknown.
  Only a provably zero-transfer path (the GPU handle producer) reports 0,
  which is what makes "全 GPU" claims auditable from the trace alone.
- The dict is JSON-serializable and emitted through a single sink hook
  (``set_trace_sink``) so the master plan's unified event schema can
  consume it without this module depending on Edit code.

Emission is gated by ``TOPOS_COMP_FRAME_TRACE`` (off by default; the
default sink prints one ``COMP_TRACE`` line). The trace object is attached
to ``ExportResult.frame_trace`` regardless, for programmatic consumers.
"""

from __future__ import annotations

import itertools
import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger(__name__)

_ENABLED = os.environ.get("TOPOS_COMP_FRAME_TRACE", "off").strip().lower() \
    not in {"0", "false", "off", "no", ""}

_SINK: Optional[Callable[[Dict[str, Any]], None]] = None
_SINK_LOCK = threading.Lock()
_SEQ = itertools.count(1)


def set_trace_sink(sink: Optional[Callable[[Dict[str, Any]], None]]) -> None:
    """Install the consumer of trace dicts (adapter to the unified schema).

    The sink must not raise and must not retain mutable references — it
    receives an already-copied dict.
    """
    global _SINK
    with _SINK_LOCK:
        _SINK = sink


def _default_sink(trace: Dict[str, Any]) -> None:
    try:
        print("COMP_TRACE " + json.dumps(trace, ensure_ascii=False,
                                         default=str), flush=True)
    except Exception:
        pass


def new_trace_id() -> str:
    """Stable per-frame correlation id (monotonic, process-unique)."""
    return f"comp-{time.monotonic_ns()}-{next(_SEQ)}"


@dataclass
class CompositeFrameTrace:
    """P0-4 trace record for one comp_clip frame (Composite side)."""

    trace_id: str = field(default_factory=new_trace_id)
    frame: int = 0
    generation: int = 0
    graph_id: str = ""

    # ── request shape ──
    quality: str = ""                 # 'preview' | 'export' | ...
    producer: str = ""                # 'readback' | 'gpu_handle' | 'async_pbo'
    readback_format: str = ""         # 'srgb_u8' | 'linear_f32' | ''
    target_w: int = 0
    target_h: int = 0

    # ── backend / device ──
    backend: str = "opengl"
    device_identity: str = ""
    gl_version: str = ""

    # ── work breakdown ──
    node_count: int = 0
    strategy: str = ""                # full_frame / tiled_gpu_stitch / ...
    strategy_reason: str = ""
    tile_count: int = 0
    eval_ms: Optional[float] = None
    readback_ms: Optional[float] = None
    total_ms: Optional[float] = None
    # No GPU timestamp queries on this backend — explicitly marked, not 0.
    gpu_time_ms: Optional[float] = None
    gpu_time_unavailable: bool = True

    # ── transfer accounting (None = unknown, never fake 0) ──
    cpu_upload_bytes: Optional[int] = None
    cpu_download_bytes: Optional[int] = None

    # ── cache / degradation ──
    cache_hit: Optional[bool] = None
    pool_peak_bytes: Optional[int] = None
    fallback_reason: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "trace_id": self.trace_id,
            "frame": self.frame,
            "generation": self.generation,
            "graph_id": self.graph_id,
            "quality": self.quality,
            "producer": self.producer,
            "readback_format": self.readback_format,
            "target": [self.target_w, self.target_h],
            "backend": self.backend,
            "device_identity": self.device_identity,
            "gl_version": self.gl_version,
            "node_count": self.node_count,
            "strategy": self.strategy,
            "strategy_reason": self.strategy_reason,
            "tile_count": self.tile_count,
            "eval_ms": _round(self.eval_ms),
            "readback_ms": _round(self.readback_ms),
            "total_ms": _round(self.total_ms),
            "gpu_time_ms": None,
            "gpu_time_unavailable": self.gpu_time_unavailable,
            "cpu_upload_bytes": self.cpu_upload_bytes,
            "cpu_download_bytes": self.cpu_download_bytes,
            "cache_hit": self.cache_hit,
            "pool_peak_bytes": self.pool_peak_bytes,
            "fallback_reason": self.fallback_reason,
        }


def _round(v: Optional[float]) -> Optional[float]:
    return round(v, 3) if isinstance(v, float) else v


def emit_trace(trace: CompositeFrameTrace) -> Dict[str, Any]:
    """Snapshot the trace and hand it to the sink (never raises).

    Returns the dict so callers can attach it to their result objects.
    """
    data = trace.to_dict()
    with _SINK_LOCK:
        sink = _SINK
    if sink is None and _ENABLED:
        sink = _default_sink
    if sink is not None:
        try:
            sink(data)
        except Exception as e:  # pragma: no cover - sink must not break render
            logger.debug("frame trace sink failed: %s", e)
    return data
