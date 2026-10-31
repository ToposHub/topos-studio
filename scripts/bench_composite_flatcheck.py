"""FLAT 输出专项复核：对基准中输出纯色的节点做根因定位。

对每个可疑节点分别在 [默认参数] / [基准参数] 下渲染一帧，dump：
  - 输出 dtype/shape/mean/std/直方图分位
  - export (linear_f32) 路径的同样统计（区分预览读回链 vs 求值本身）
  - 与 baseline 直连对比

用法：
    .venv/Scripts/python.exe scripts/bench_composite_flatcheck.py edge_detect transform ...
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("TOPOS_HEADLESS_GL", "1")

BENCH_DIR = Path(os.environ.get("TEMP", "")) / "topos_comp_bench"
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from bench_composite_nodes_gpu_coverage import (  # noqa: E402
    PARAM_OVERRIDES, build_graph, render_frames,
)


def _stats(tag: str, arr: np.ndarray) -> None:
    a = arr.astype("float64")
    qs = [float(np.quantile(a, q)) for q in (0.01, 0.25, 0.5, 0.75, 0.99)]
    print(f"    {tag}: shape={arr.shape} dtype={arr.dtype} "
          f"mean={a.mean():.4f} std={a.std():.4f} "
          f"q[1,25,50,75,99]={['%.3f' % q for q in qs]}")


def check(node_type: str, res: str = "hd") -> None:
    print(f"\n=== {node_type}@{res} ===", flush=True)
    if res == "4k":
        sources = sorted(BENCH_DIR.glob("real4k_f*.png"))
    else:
        sources = sorted(BENCH_DIR.glob("realhd_f*.png"))
    from bench_composite_nodes_gpu_coverage import _png_size
    out_size = _png_size(sources[0])

    for label, params in (("default", {}), ("override", PARAM_OVERRIDES.get(node_type, {}))):
        graph = build_graph([(node_type, params)], sources, out_size)
        for quality in ("preview", "export"):
            timings, info, err = render_frames(graph, quality, frames=[3])
            if err:
                print(f"  [{label}/{quality}] ERROR {err.splitlines()[0]}")
                continue
            if not timings:
                print(f"  [{label}/{quality}] no timings")
                continue
            from src.features.composite.render.composite_frame_renderer import (
                CompositeFrameRenderer,
            )
            # 重渲染一帧拿实际数组做统计
            r = CompositeFrameRenderer()
            try:
                arr = r.render_comp_clip_frame(
                    graph_data=graph, timeline_frame=3, source_frame=3, quality=quality)
            finally:
                r.cleanup()
            if arr is None:
                print(f"  [{label}/{quality}] returned None")
                continue
            _stats(f"{label}/{quality}", np.asarray(arr))
        print(f"  [override params] {PARAM_OVERRIDES.get(node_type, {})}")


if __name__ == "__main__":
    nodes = sys.argv[1:] or ["edge_detect", "transform", "matte",
                             "chroma_keyer", "difference_keyer", "relight"]
    for n in nodes:
        check(n)
