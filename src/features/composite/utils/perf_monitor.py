"""
PerfMonitor - Thread-safe performance data collector for the composite pipeline.

Render thread writes per-node timing, cache stats, and GPU resource usage.
UI thread reads snapshots for display in perf panel and node badges.

Design:
- Lock held only during dict assignments (microseconds), never during GPU work
- begin_frame() clears per-node data each frame
- get_snapshot() returns an immutable copy and resets dropped count
"""

import threading
import time
from collections import deque
from dataclasses import dataclass, field, replace
from typing import Dict, Iterable, Optional


@dataclass
class PerfNodeStats:
    """Per-node performance data for one evaluation frame."""
    node_id: str
    node_type: str
    eval_time_ms: float      # CPU wall time for this node (0.0 for cache hits)
    was_cached: bool
    was_dirty: bool
    error: Optional[str] = None

    def to_dict(self) -> dict:
        """Return a JSON-safe representation for trace export."""
        return {
            "node_id": self.node_id,
            "node_type": self.node_type,
            "eval_time_ms": float(self.eval_time_ms),
            "was_cached": bool(self.was_cached),
            "was_dirty": bool(self.was_dirty),
            "error": self.error,
        }


@dataclass
class PerfFrameStats:
    """Aggregate performance data for one evaluation frame."""
    request_id: int
    total_eval_ms: float
    request_latency_ms: float   # RenderRequest.timestamp → end of evaluation
    cache_hit_rate: float       # 0.0 - 1.0
    active_textures: int
    vram_mb: float
    dropped_requests: int       # requests dropped since last snapshot

    def to_dict(self) -> dict:
        """Return a JSON-safe representation for trace export."""
        return {
            "request_id": int(self.request_id),
            "total_eval_ms": float(self.total_eval_ms),
            "request_latency_ms": float(self.request_latency_ms),
            "cache_hit_rate": float(self.cache_hit_rate),
            "active_textures": int(self.active_textures),
            "vram_mb": float(self.vram_mb),
            "dropped_requests": int(self.dropped_requests),
        }


@dataclass
class PerfSnapshot:
    """Immutable snapshot returned to UI thread."""
    frame: Optional[PerfFrameStats] = None
    nodes: Dict[str, PerfNodeStats] = field(default_factory=dict)
    cache_hits: int = 0
    cache_misses: int = 0
    cache_evictions: int = 0
    timestamp: float = 0.0
    # F12：3D 渲染统计（Scene3D 每帧 record_3d_stats 填充；None=非 3D 帧）
    render3d: Optional["Render3DStats"] = None

    def to_dict(self) -> dict:
        """Return one complete, JSON-safe frame trace entry."""
        return {
            "timestamp": float(self.timestamp),
            "frame": self.frame.to_dict() if self.frame is not None else None,
            "nodes": {
                node_id: stats.to_dict()
                for node_id, stats in sorted(self.nodes.items())
            },
            "cache_hits": int(self.cache_hits),
            "cache_misses": int(self.cache_misses),
            "cache_evictions": int(self.cache_evictions),
            "render3d": self.render3d.to_dict()
            if self.render3d is not None else None,
        }


@dataclass
class Render3DStats:
    """3D 渲染统计（F12 性能面板增强）。

    Scene3D 渲染线程每帧 record_3d_stats 填充；UI 线程 PerfPanel 读取显示。
    None=非 3D 帧（2D 合成不填）。

    Attributes:
        draw_calls: draw call 数（每个 flat_mesh 一次）。
        triangles: 三角面总数。
        vertices: 顶点总数。
        mesh_count: 渲染的 mesh 数。
        material_binds: 材质绑定次数（贴图上传/sampler 绑定）。
        texture_binds: 纹理绑定次数。
        bone_binds: 骨骼矩阵上传次数（skinned mesh）。
        skin_count: 蒙皮 mesh 数。
        render_passes: 渲染 pass 数（beauty/depth/id；AOV 多 pass 时 >1）。
        msaa_samples: MSAA 采样数（1=关闭；4/8=开启）。
        lod_level: 当前 LOD 等级（0=全精度）。
        render_ms: 3D 渲染耗时（毫秒；GL draw 总时间）。
    """
    draw_calls: int = 0
    triangles: int = 0
    vertices: int = 0
    mesh_count: int = 0
    material_binds: int = 0
    texture_binds: int = 0
    bone_binds: int = 0
    skin_count: int = 0
    render_passes: int = 1
    msaa_samples: int = 1
    lod_level: int = 0
    render_ms: float = 0.0

    def to_dict(self) -> dict:
        """Return a JSON-safe representation for trace export."""
        return {
            "draw_calls": int(self.draw_calls),
            "triangles": int(self.triangles),
            "vertices": int(self.vertices),
            "mesh_count": int(self.mesh_count),
            "material_binds": int(self.material_binds),
            "texture_binds": int(self.texture_binds),
            "bone_binds": int(self.bone_binds),
            "skin_count": int(self.skin_count),
            "render_passes": int(self.render_passes),
            "msaa_samples": int(self.msaa_samples),
            "lod_level": int(self.lod_level),
            "render_ms": float(self.render_ms),
        }

    def summary_lines(self) -> list:
        """返回 UI 显示行（PerfPanel 渲染用）。"""
        return [
            f"Draw Calls: {self.draw_calls}",
            f"Triangles: {self.triangles:,}",
            f"Vertices: {self.vertices:,}",
            f"Meshes: {self.mesh_count}",
            f"Materials: {self.material_binds}",
            f"Textures: {self.texture_binds}",
            f"Bones: {self.bone_binds} ({self.skin_count} skins)",
            f"Passes: {self.render_passes}",
            f"MSAA: {self.msaa_samples}x",
            f"LOD: {self.lod_level}",
            f"3D Render: {self.render_ms:.1f}ms",
        ]


class PerfMonitor:
    """Thread-safe performance data collector.

    Render thread calls: begin_frame(), record_node_eval(), record_frame(), record_dropped()
    UI thread calls: get_snapshot() / get_history()
    """

    def __init__(self, history_size: int = 120):
        self._lock = threading.Lock()
        self._nodes: Dict[str, PerfNodeStats] = {}
        self._frame: Optional[PerfFrameStats] = None
        self._cache_hits: int = 0
        self._cache_misses: int = 0
        self._cache_evictions: int = 0
        self._dropped_count: int = 0
        self._request_timestamp: float = 0.0
        # F12：3D 渲染统计（Scene3D record_3d_stats 填充；None=非 3D 帧）
        self._render3d: Optional[Render3DStats] = None
        # Bounded completed-frame trace for diagnostics/export. Render-thread
        # writes stay O(1); callers receive detached snapshots below.
        self._history = deque(maxlen=max(1, int(history_size)))

    # ── Render Thread API ──

    def begin_frame(self, request_timestamp: float = 0.0):
        """Clear per-node data for a new frame. Called at eval loop start."""
        with self._lock:
            self._nodes.clear()
            self._request_timestamp = request_timestamp
            self._render3d = None  # 清除上一帧 3D 统计

    def record_node_eval(self, node_id: str, node_type: str,
                         eval_time_ms: float, was_cached: bool,
                         was_dirty: bool, error: Optional[str] = None):
        """Record evaluation result for a single node."""
        with self._lock:
            self._nodes[node_id] = PerfNodeStats(
                node_id=node_id,
                node_type=node_type,
                eval_time_ms=eval_time_ms,
                was_cached=was_cached,
                was_dirty=was_dirty,
                error=error,
            )

    def record_frame(self, request_id: int, total_eval_ms: float,
                     cache_hits: int, cache_misses: int, cache_evictions: int,
                     active_textures: int, vram_mb: float):
        """Record aggregate frame stats after evaluation completes."""
        total = cache_hits + cache_misses
        hit_rate = cache_hits / total if total > 0 else 0.0
        latency_ms = (time.monotonic() - self._request_timestamp) * 1000.0 if self._request_timestamp > 0 else 0.0

        with self._lock:
            self._frame = PerfFrameStats(
                request_id=request_id,
                total_eval_ms=total_eval_ms,
                request_latency_ms=latency_ms,
                cache_hit_rate=hit_rate,
                active_textures=active_textures,
                vram_mb=vram_mb,
                dropped_requests=self._dropped_count,
            )
            self._cache_hits = cache_hits
            self._cache_misses = cache_misses
            self._cache_evictions = cache_evictions
            # Reset dropped count after recording
            self._dropped_count = 0
            self._history.append(self._copy_snapshot_locked())

    def record_dropped(self):
        """Increment dropped request counter (stale/cancelled requests)."""
        with self._lock:
            self._dropped_count += 1

    def record_3d_stats(self, stats: "Render3DStats"):
        """Record 3D rendering stats for current frame (F12 perf panel).

        Called by Scene3D render thread after GL draw completes.
        Cleared at next begin_frame().
        """
        with self._lock:
            self._render3d = stats

    # ── UI Thread API ──

    def _copy_snapshot_locked(
        self, *, include_pending_dropped: bool = False
    ) -> PerfSnapshot:
        """Copy current state while ``_lock`` is held."""
        frame_copy = replace(self._frame) if self._frame is not None else None
        if include_pending_dropped and frame_copy is not None and self._dropped_count > 0:
            frame_copy = replace(
                frame_copy,
                dropped_requests=(
                    frame_copy.dropped_requests + self._dropped_count
                ),
            )
        return PerfSnapshot(
            frame=frame_copy,
            nodes={
                node_id: replace(stats)
                for node_id, stats in self._nodes.items()
            },
            cache_hits=self._cache_hits,
            cache_misses=self._cache_misses,
            cache_evictions=self._cache_evictions,
            timestamp=time.monotonic(),
            render3d=(replace(self._render3d)
                       if self._render3d is not None else None),
        )

    def get_snapshot(self) -> PerfSnapshot:
        """Return an immutable copy of current perf data for UI consumption."""
        with self._lock:
            snapshot = self._copy_snapshot_locked(
                include_pending_dropped=True)
            # Reset dropped count after reading
            self._dropped_count = 0
        return snapshot

    def get_history(self, limit: Optional[int] = None) -> list[PerfSnapshot]:
        """Return completed frame snapshots in chronological order.

        ``limit`` is applied from the newest end while preserving order in the
        returned list. The returned snapshots are detached from the monitor.
        """
        with self._lock:
            entries = list(self._history)
        if limit is not None:
            count = max(0, int(limit))
            entries = entries[-count:] if count else []
        return [
            PerfSnapshot(
                frame=(replace(entry.frame)
                       if entry.frame is not None else None),
                nodes={
                    node_id: replace(stats)
                    for node_id, stats in entry.nodes.items()
                },
                cache_hits=entry.cache_hits,
                cache_misses=entry.cache_misses,
                cache_evictions=entry.cache_evictions,
                timestamp=entry.timestamp,
                render3d=(replace(entry.render3d)
                           if entry.render3d is not None else None),
            )
            for entry in entries
        ]

    def clear_history(self) -> None:
        """Discard the trace without affecting current-frame counters."""
        with self._lock:
            self._history.clear()


def build_perf_report(snapshots: Iterable[PerfSnapshot]) -> dict:
    """Build a stable JSON report from completed performance snapshots.

    The report includes the raw frame trace plus summaries for hot nodes,
    cache behaviour, dropped requests, memory peaks, and 3D render cost. It
    intentionally contains only JSON-safe Python values.
    """
    entries = list(snapshots)
    frames = [snapshot.frame for snapshot in entries if snapshot.frame is not None]

    def _average(values: list[float]) -> float:
        return sum(values) / len(values) if values else 0.0

    node_totals: dict[str, dict] = {}
    for snapshot in entries:
        for node_id, stats in snapshot.nodes.items():
            item = node_totals.setdefault(
                str(node_id),
                {
                    "node_id": str(node_id),
                    "node_type": stats.node_type,
                    "frames": 0,
                    "total_eval_ms": 0.0,
                    "max_eval_ms": 0.0,
                    "cached_frames": 0,
                    "dirty_frames": 0,
                    "errors": [],
                },
            )
            item["node_type"] = stats.node_type
            item["frames"] += 1
            item["total_eval_ms"] += float(stats.eval_time_ms)
            item["max_eval_ms"] = max(
                item["max_eval_ms"], float(stats.eval_time_ms)
            )
            item["cached_frames"] += int(bool(stats.was_cached))
            item["dirty_frames"] += int(bool(stats.was_dirty))
            if stats.error and stats.error not in item["errors"]:
                item["errors"].append(str(stats.error))

    for item in node_totals.values():
        item["average_eval_ms"] = (
            item["total_eval_ms"] / item["frames"]
            if item["frames"] else 0.0
        )

    hot_nodes = sorted(
        node_totals.values(),
        key=lambda item: (-item["total_eval_ms"], item["node_id"]),
    )
    render3d = [snapshot.render3d for snapshot in entries
                if snapshot.render3d is not None]
    frame_summary = {
        "count": len(frames),
        "request_id_first": int(frames[0].request_id) if frames else None,
        "request_id_last": int(frames[-1].request_id) if frames else None,
        "average_eval_ms": _average([float(f.total_eval_ms) for f in frames]),
        "max_eval_ms": max((float(f.total_eval_ms) for f in frames), default=0.0),
        "average_latency_ms": _average(
            [float(f.request_latency_ms) for f in frames]
        ),
        "max_latency_ms": max(
            (float(f.request_latency_ms) for f in frames), default=0.0
        ),
        "average_cache_hit_rate": _average(
            [float(f.cache_hit_rate) for f in frames]
        ),
        "cache_hits": sum(int(snapshot.cache_hits) for snapshot in entries),
        "cache_misses": sum(int(snapshot.cache_misses) for snapshot in entries),
        "cache_evictions": sum(
            int(snapshot.cache_evictions) for snapshot in entries
        ),
        "dropped_requests": sum(int(f.dropped_requests) for f in frames),
        "peak_active_textures": max(
            (int(f.active_textures) for f in frames), default=0
        ),
        "peak_vram_mb": max(
            (float(f.vram_mb) for f in frames), default=0.0
        ),
    }
    render3d_summary = {
        "count": len(render3d),
        "average_render_ms": _average([float(s.render_ms) for s in render3d]),
        "max_render_ms": max(
            (float(s.render_ms) for s in render3d), default=0.0
        ),
        "peak_draw_calls": max(
            (int(s.draw_calls) for s in render3d), default=0
        ),
        "peak_triangles": max(
            (int(s.triangles) for s in render3d), default=0
        ),
        "peak_meshes": max(
            (int(s.mesh_count) for s in render3d), default=0
        ),
    }
    return {
        "schema_version": 1,
        "sample_count": len(entries),
        "frame_summary": frame_summary,
        "render3d_summary": render3d_summary,
        "hot_nodes": hot_nodes,
        "frames": [snapshot.to_dict() for snapshot in entries],
    }
