#!/usr/bin/env python3
"""B1 离线 .vdb → .npz 稠密体积转换器（依赖门降级配套）。

本机 pyopenvdb 不可用（B1 spike 结论，见 render3d/volume_grid.py）。
本脚本在装有 pyopenvdb 的环境（如 Houdini/Blender 的 Python）运行：

    python tools/convert_vdb.py smoke.vdb smoke.npz [--grid density] [--res 256]

输出 .npz：{"density": (D, H, W) float32, "spacing": float32}
（volume3d 节点与 tests/composite/test_volume_render.py 的 fixture 契约）。
"""
from __future__ import annotations

import argparse
import sys


def _coord_xyz(coord):
    """Accept a pyopenvdb Coord (attributes or indexable) or a tuple."""
    try:
        return int(coord.x), int(coord.y), int(coord.z)
    except AttributeError:
        return int(coord[0]), int(coord[1]), int(coord[2])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", help=".vdb 输入文件")
    parser.add_argument("output", help=".npz 输出文件")
    parser.add_argument("--grid", default="",
                        help="grid 名称（默认自动选 density/fog_volume/第一个）")
    parser.add_argument("--res", type=int, default=256,
                        help="稠密化最大分辨率")
    args = parser.parse_args()

    try:
        import pyopenvdb as vdb
    except ImportError:
        print("pyopenvdb 不可用——请在装有 OpenVDB 的环境运行本脚本，"
              "或先解决打包门（约束 13）。", file=sys.stderr)
        return 2

    import numpy as np

    # API 兼容：readAllGrids 最广可用；旧版本回退到按名 read。
    try:
        grids = list(vdb.readAllGrids(args.input))
    except AttributeError:
        grids = [vdb.read(args.input, grid_name=args.grid or "density")]
    if not grids:
        print("文件中没有任何 grid", file=sys.stderr)
        return 3

    wanted = (args.grid or "").strip().lower()
    grid = None
    if wanted:
        grid = next((g for g in grids
                     if str(getattr(g, "name", "")).lower() == wanted), None)
    else:
        for candidate in grids:
            name = str(getattr(candidate, "name", "")).lower()
            if "density" in name or "fog" in name:
                grid = candidate
                break
    if grid is None:
        grid = grids[0]

    bbox = grid.evalActiveVoxelBoundingBox()
    if bbox is None:
        print("grid 无 active voxel", file=sys.stderr)
        return 3
    (x0, y0, z0) = _coord_xyz(bbox.min())
    (x1, y1, z1) = _coord_xyz(bbox.max())
    dims = (x1 - x0 + 1, y1 - y0 + 1, z1 - z0 + 1)
    scale = max(1, max(dims) // max(1, args.res))

    # 体素世界间距（各向同性时取 x 分量；异常时回退 1.0）。
    spacing = float(scale)
    try:
        voxel = grid.voxelSize()
        sx = float(getattr(voxel, "x", None)
                   if hasattr(voxel, "x") else voxel[0])
        spacing = sx * scale
    except Exception:
        pass

    # 稀疏 active voxel → bbox 稠密化（与 volume_grid.py 契约一致）。
    accessor = grid.getAccessor()
    dense = np.zeros(
        ((z1 - z0) // scale + 1, (y1 - y0) // scale + 1,
         (x1 - x0) // scale + 1), dtype=np.float32)
    for z in range(z0, z1 + 1, scale):
        for y in range(y0, y1 + 1, scale):
            for x in range(x0, x1 + 1, scale):
                dense[(z - z0) // scale, (y - y0) // scale,
                      (x - x0) // scale] = accessor.getValue((x, y, z))
    np.savez_compressed(args.output, density=dense,
                        spacing=np.float32(spacing))
    print(f"written {args.output}: {dense.shape} spacing={spacing}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
