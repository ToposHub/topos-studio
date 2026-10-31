#!/usr/bin/env python3
"""Benchmark the CPU BVH path used by Composite component picking.

Example::

    .venv/bin/python scripts/benchmark_composite_picking.py
    .venv/bin/python scripts/benchmark_composite_picking.py --grid-size 128 --rays 32 --pretty

The default grid contains roughly one million triangles and emits a JSON
record suitable for attaching to the composite optimization plan.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.features.composite.render3d.mesh_picking import (  # noqa: E402
    benchmark_triangle_bvh,
)


def _grid_mesh(grid_size: int) -> tuple[np.ndarray, np.ndarray]:
    """Return a deterministic XY grid with two triangles per cell."""
    if grid_size < 2:
        raise ValueError("grid_size must be at least 2")
    axis = np.linspace(-1.0, 1.0, grid_size, dtype=np.float32)
    xx, yy = np.meshgrid(axis, axis, indexing="xy")
    vertices = np.column_stack((xx.ravel(), yy.ravel(),
                                np.zeros(grid_size * grid_size, dtype=np.float32)))
    rows = np.arange(grid_size - 1, dtype=np.int64)[:, None] * grid_size
    cols = np.arange(grid_size - 1, dtype=np.int64)[None, :]
    top_left = (rows + cols).ravel()
    top_right = top_left + 1
    bottom_left = top_left + grid_size
    bottom_right = bottom_left + 1
    indices = np.stack((
        top_left, bottom_left, top_right,
        top_right, bottom_left, bottom_right,
    ), axis=1).reshape(-1).astype(np.int32)
    return vertices, indices


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--grid-size", type=int, default=708)
    parser.add_argument("--rays", type=int, default=128)
    parser.add_argument("--leaf-size", type=int, default=8)
    parser.add_argument("--pretty", action="store_true")
    args = parser.parse_args()
    if args.rays < 0:
        parser.error("--rays must be non-negative")

    vertices, indices = _grid_mesh(args.grid_size)
    rng = np.random.default_rng(20260828)
    origins = np.column_stack((
        rng.uniform(-0.95, 0.95, args.rays),
        rng.uniform(-0.95, 0.95, args.rays),
        np.full(args.rays, 2.0),
    ))
    directions = np.tile(np.array([0.0, 0.0, -1.0]), (args.rays, 1))
    result = benchmark_triangle_bvh(
        vertices, indices, origins, directions, leaf_size=args.leaf_size)
    output = {
        "benchmark": "composite_triangle_bvh",
        "grid_size": args.grid_size,
        "vertex_count": int(vertices.shape[0]),
        **result.to_dict(),
    }
    print(json.dumps(output, ensure_ascii=False,
                     indent=2 if args.pretty else None, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
