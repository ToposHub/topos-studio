"""Point cloud data layer (B2) — PLY/xyz loading, tracker import, and a
deterministic CPU rasterizer.

The plan's GL_POINTS pass inside Scene3D is follow-up wiring; the MVP
rasterizes points on the CPU with a simple pinhole/ortho projection so
coverage, colour modes and depth shading are fully unit-testable and the
node renders without a 3D scene.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import numpy as np

logger = logging.getLogger(__name__)

TRACK_DATA_VERSION = 1


def srgb_to_linear(values: "np.ndarray") -> "np.ndarray":
    """Piecewise sRGB → scene-linear transfer (IEC 61966-2-1)."""
    v = np.asarray(values, dtype=np.float32)
    return np.where(
        v <= 0.04045, v / 12.92,
        ((v + 0.055) / 1.055) ** 2.4).astype(np.float32)


@dataclass
class PointCloud:
    """positions (N, 3) float32 + optional vertex colours (N, 3) 0..1."""

    positions: "np.ndarray"
    colors: Optional["np.ndarray"] = None

    @property
    def count(self) -> int:
        return int(self.positions.shape[0])


def load_point_cloud(path: str) -> PointCloud:
    """Load ``.ply`` (trimesh, incl. vertex colours) or ``.xyz`` text
    (``x y z [r g b]`` per line, `#` comments)."""
    suffix = Path(path).suffix.lower()
    if suffix == ".xyz":
        return _load_xyz(path)
    return _load_ply(path)


def _filter_finite(positions: "np.ndarray",
                   colors: Optional["np.ndarray"], path: str
                   ) -> tuple["np.ndarray", Optional["np.ndarray"]]:
    """AUD-9106：过滤外部点云中的 NaN/Inf 坐标（含对齐的 colors）。

    ``"nan"``/``"inf"`` 可被 ``float()`` 正常解析，非有限坐标进入 rasterize
    会触发未定义整型转换（invalid value cast，落点像素平台相关）或
    ``span=inf → scale=0`` 的整幅塌缩。返回过滤后的 (positions, colors)。
    """
    if positions.size == 0:
        return positions, colors
    finite = np.isfinite(positions).all(axis=1)
    if bool(finite.all()):
        return positions, colors
    dropped = int((~finite).sum())
    logger.warning("点云含 %d 个非有限坐标点，已丢弃: %s", dropped, path)
    positions = positions[finite]
    if colors is not None and colors.shape[0] == finite.shape[0]:
        colors = colors[finite]
    return positions, colors


def _load_ply(path: str) -> PointCloud:
    try:
        import trimesh
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("trimesh required for PLY point clouds") from exc
    cloud = trimesh.load(path, process=False)
    vertices = getattr(cloud, "vertices", None)
    if vertices is None or len(vertices) == 0:
        raise ValueError(f"no vertices in point cloud: {path}")
    positions = np.asarray(vertices, dtype=np.float32)[:, :3]
    colors = None
    # trimesh exposes vertex colours on .visual (meshes) or .colors
    # (PointClouds) — accept both shapes.
    data = None
    visual = getattr(cloud, "visual", None)
    if visual is not None and getattr(visual, "kind", "") == "vertex":
        vertex_colors = np.asarray(visual.vertex_colors)
        face_colors = np.asarray(getattr(visual, "face_colors", []))
        data = (vertex_colors if len(vertex_colors) == len(positions)
                else face_colors)
    cloud_colors = getattr(cloud, "colors", None)
    if cloud_colors is not None:
        arr = np.asarray(cloud_colors)
        if len(arr) == len(positions):
            data = arr
    if data is not None and len(data) == len(positions):
        # PLY stores sRGB bytes; the pipeline texture is declared
        # scene-linear, so decode before the rasterizer sees the values.
        colors = srgb_to_linear(data[:, :3].astype(np.float32) / 255.0)
    # AUD-9106：trimesh 顶点可为 NaN（损坏/畸形 PLY）。
    positions, colors = _filter_finite(positions, colors, path)
    if positions.shape[0] == 0:
        raise ValueError(f"no finite vertices in point cloud: {path}")
    return PointCloud(positions=positions, colors=colors)


def _load_xyz(path: str) -> PointCloud:
    rows: list = []
    colors: list = []
    with open(path, "r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.replace(",", " ").split()
            if len(parts) < 3:
                continue
            try:
                rows.append([float(parts[0]), float(parts[1]),
                             float(parts[2])])
                if len(parts) >= 6:
                    colors.append([float(parts[3]), float(parts[4]),
                                   float(parts[5])])
            except ValueError:
                continue
    if not rows:
        raise ValueError(f"empty point cloud file: {path}")
    positions = np.asarray(rows, dtype=np.float32)
    colors_arr = None
    if len(colors) == len(rows):
        colors_arr = np.asarray(colors, dtype=np.float32)
        # xyz colour convention is ambiguous — most exporters write
        # 0..255.  Heuristic: any value above 1 means byte range.
        if colors_arr.size and float(colors_arr.max()) > 1.0:
            colors_arr = colors_arr / 255.0
        colors_arr = srgb_to_linear(np.clip(colors_arr, 0.0, 1.0))
    # AUD-9106："nan"/"inf" 可被 float() 解析——过滤非有限坐标再交付。
    positions, colors_arr = _filter_finite(positions, colors_arr, path)
    if positions.shape[0] == 0:
        raise ValueError(f"no finite points in point cloud file: {path}")
    return PointCloud(positions=positions, colors=colors_arr)


def points_from_track_data(track_data: Dict[str, Any]
                           ) -> Tuple["np.ndarray", int, int]:
    """Tracker import: valid per-frame translations become planar points
    (tx, ty, 0) in source pixels.  Returns (points, width, height)."""
    transforms = (track_data or {}).get("transforms") or {}
    width = int((track_data or {}).get("width", 0) or 0)
    height = int((track_data or {}).get("height", 0) or 0)
    rows: list = []
    for frame in sorted(transforms):
        try:
            int(frame)
        except (TypeError, ValueError):
            continue  # tolerate junk keys instead of aborting the import
        transform = transforms[frame] or {}
        if str(transform.get("status", "valid")) != "valid":
            continue
        rows.append([float(transform.get("tx", 0.0)),
                     float(transform.get("ty", 0.0)), 0.0])
    return (np.asarray(rows, dtype=np.float32).reshape(-1, 3),
            width, height)


def rasterize(points: "np.ndarray", colors: Optional["np.ndarray"],
              width: int, height: int, point_size: int = 2,
              color_mode: str = "flat", flat_color=(0.85, 0.9, 1.0),
              background=(0.0, 0.0, 0.0, 0.0),
              fit: bool = True) -> "np.ndarray":
    """Deterministic CPU point rasterization.

    Orthographic fit: the cloud's bbox is centered and scaled into the
    frame (preserving aspect) when ``fit`` is on — coverage assertions in
    tests rely on this.  ``depth_ramp`` shades by normalized depth.
    """
    out = np.empty((int(height), int(width), 4), dtype=np.float32)
    out[..., :3] = background[:3]
    out[..., 3] = background[3] if len(background) > 3 else 0.0
    if points is None or len(points) == 0 or width <= 0 or height <= 0:
        return out

    pts = np.asarray(points, dtype=np.float64)
    # AUD-9106 双保险：rasterize 前过滤非有限坐标，防上游（tracker 等）
    # 未过滤的 NaN/Inf 触发未定义整型转换 / span=inf 塌缩。
    if pts.size:
        finite = np.isfinite(pts).all(axis=1)
        if not bool(finite.all()):
            logger.warning("rasterize: 丢弃 %d 个非有限坐标点",
                           int((~finite).sum()))
            pts = pts[finite]
            if (color_mode == "vertex_color" and colors is not None
                    and len(colors) == len(points)):
                colors = np.asarray(colors, dtype=np.float32)[finite]
            if pts.shape[0] == 0:
                return out
    lo = pts.min(axis=0)
    hi = pts.max(axis=0)
    span = np.maximum(hi - lo, 1e-9)
    center = (lo + hi) * 0.5
    if fit:
        scale = min((width - 2) / max(span[0], 1e-9),
                    (height - 2) / max(span[1], 1e-9))
    else:
        scale = 1.0
    xy = (pts[:, :2] - center[:2]) * scale
    px = np.clip((xy[:, 0] + width / 2.0).round().astype(np.int64),
                 0, width - 1)
    py = np.clip(((height / 2.0) - (xy[:, 1])).round().astype(np.int64),
                 0, height - 1)
    depth = np.clip((pts[:, 2] - lo[2]) / max(span[2], 1e-9), 0.0, 1.0)

    # AUD-9106：过滤后 pts/colors 行数一致，颜色分支按过滤后的行数取值。
    if color_mode == "vertex_color" and colors is not None \
            and len(colors) == pts.shape[0]:
        rgb = np.clip(np.asarray(colors, dtype=np.float32), 0.0, 1.0)
    elif color_mode == "depth_ramp":
        rgb = np.stack([depth, np.full_like(depth, 0.4),
                        1.0 - depth], axis=-1).astype(np.float32)
    else:
        rgb = np.tile(np.asarray(flat_color, dtype=np.float32),
                      (int(pts.shape[0]), 1))

    half = max(0, int(point_size) // 2)
    for dy in range(-half, half + 1):
        for dx in range(-half, half + 1):
            ys = np.clip(py + dy, 0, height - 1)
            xs = np.clip(px + dx, 0, width - 1)
            out[ys, xs, :3] = rgb
            out[ys, xs, 3] = 1.0
    return out
