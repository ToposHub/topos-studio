"""3D 视口性能 benchmark 工具（Milestone A / P0-8）。

按 master_plan_2026-07-07.md §14.4 的建议，提供合成 mesh 的 perf stats 采集器：
- 用 Viewport3DPerfStats 模拟小/中/大三档模型规模，验证 stats 字段被正确填充。
- 不依赖真实 GL（合成 mesh + 模拟 draw call 计数），可在 CI 跑轻量版本。
- 真实 GL benchmark 留给手动验收（§14.3 手动验收模型表）。

用法（CLI 跑一次，把结果写入 docs/benchmarks/3d_viewport/）::

    .venv/bin/python tests/benchmark/benchmark_3d_viewport.py --write

输出文件（§14.4）::

    docs/benchmarks/3d_viewport/<YYYY-MM-DD>_synthetic_profile.json
    docs/benchmarks/3d_viewport/<YYYY-MM-DD>_summary.md
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import asdict
from datetime import date

# 让脚本可独立运行（不在 package 内也能 import src）。
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from src.features.composite.render3d.perf_stats import (
    Viewport3DPerfStats, format_overlay, format_quad_overlay,
)


# ──────────────────────────────────────────────────────────────────────────
# 合成模型档位（对应计划 §0.2 量化指标 + §14.3 手动验收模型）
# ──────────────────────────────────────────────────────────────────────────

SYNTHETIC_PROFILES = {
    # 小：~5 万三角，单视图基准
    "small": {
        "mesh_count": 8,
        "triangles_per_mesh": 6250,   # 8 * 6250 = 50k
        "material_count": 4,
        "texture_count": 12,
        "label": "小模型 ~50k 三角",
    },
    # 中：~50 万三角（用户当前 FBX 量级，主基准）
    "medium": {
        "mesh_count": 40,
        "triangles_per_mesh": 12500,  # 40 * 12500 = 500k
        "material_count": 12,
        "texture_count": 30,
        "label": "中模型 ~500k 三角（用户 FBX 量级）",
    },
    # 大：~200 万三角
    "large": {
        "mesh_count": 80,
        "triangles_per_mesh": 25000,  # 80 * 25000 = 2M
        "material_count": 24,
        "texture_count": 60,
        "label": "大模型 ~2M 三角",
    },
}


def simulate_render_frame(profile: dict, layout: str = "single",
                          navigation: bool = False,
                          sim_gpu_ms: float = 0.0) -> Viewport3DPerfStats:
    """模拟一帧渲染并填充 Viewport3DPerfStats。

    这不调真实 GL——它按 profile 规模填 stats 字段，并把 sim_gpu_ms 作为
    透视渲染耗时（供 benchmark 比较不同 profile 的 CPU 统计开销）。
    """
    s = Viewport3DPerfStats()
    s.frame_id = 0
    s.quality_profile = "navigation" if navigation else "preview"
    s.viewport_layout = layout
    s.active_view = "perspective"
    t0 = time.perf_counter()
    # 模拟 per-mesh draw：每 mesh 一个 draw call + 三角形计数
    s.mesh_count = profile["mesh_count"]
    s.triangle_count = profile["mesh_count"] * profile["triangles_per_mesh"]
    s.material_count = profile["material_count"]
    s.texture_count = profile["texture_count"]
    s.draw_calls = profile["mesh_count"]
    s.triangles_submitted = s.triangle_count
    s.meshes_visible = profile["mesh_count"]  # 模拟全可见（无剔除）
    s.meshes_culled = 0
    s.texture_binds = profile["texture_count"]
    s.texture_uploads = 0
    s.material_switches = profile["material_count"]
    s.shader_switches = 1
    s.uniform_sets = profile["mesh_count"] * 8
    s.fbo_switches = 1
    s.model_cache_hit = True
    s.texture_cache_hit = True
    s.vbo_cache_hit = True
    s.navigation_active = navigation
    s.lod_level = 2 if navigation else 0
    s.internal_resolution_scale = 0.65 if navigation else 1.0
    s.resolution = (1920, 1080)
    # 透视耗时：用 sim_gpu_ms（外部估算的 GPU draw 耗时）+ CPU 统计开销
    s.perspective_ms = float(sim_gpu_ms) + (time.perf_counter() - t0) * 1000.0
    if layout == "quad":
        # 四视图：非活动视图各分担一部分
        s.top_ms = s.perspective_ms * 0.7
        s.right_ms = s.perspective_ms * 0.7
        s.front_ms = s.perspective_ms * 0.7
        s.fbo_switches = 4
    s.total_cpu_ms = (time.perf_counter() - t0) * 1000.0 + (
        s.perspective_ms + s.top_ms + s.right_ms + s.front_ms)
    return s


def run_benchmark() -> dict:
    """跑全部 profile × layout 组合，返回汇总 dict。"""
    results = {
        "date": str(date.today()),
        "profiles": {},
    }
    # 模拟 GPU 耗时（按规模粗估，仅供 benchmark 结构验证，非真实测量）
    sim_gpu = {"small": 4.0, "medium": 16.0, "large": 45.0}
    for name, profile in SYNTHETIC_PROFILES.items():
        results["profiles"][name] = {
            "label": profile["label"],
            "single_static": simulate_render_frame(
                profile, "single", navigation=False,
                sim_gpu_ms=sim_gpu[name]).to_dict(),
            "single_nav": simulate_render_frame(
                profile, "single", navigation=True,
                sim_gpu_ms=sim_gpu[name] * 0.5).to_dict(),
            "quad_static": simulate_render_frame(
                profile, "quad", navigation=False,
                sim_gpu_ms=sim_gpu[name]).to_dict(),
        }
    return results


def write_reports(results: dict, base_dir: str = "docs/benchmarks/3d_viewport") -> None:
    """把 benchmark 结果写成 json + summary.md（计划 §14.4）。"""
    os.makedirs(base_dir, exist_ok=True)
    today = str(date.today())
    json_path = os.path.join(base_dir, f"{today}_synthetic_profile.json")
    md_path = os.path.join(base_dir, f"{today}_summary.md")
    with open(json_path, "w", encoding="utf-8") as fh:
        json.dump(results, fh, indent=2, ensure_ascii=False)
    lines = [
        f"# 3D 视口合成 benchmark 摘要 {results['date']}",
        "",
        "> 合成 mesh 模拟（非真实 GL）；用于验证 perf stats 采集结构。",
        "> 真实 FPS/帧时间请用 perf overlay 在实际 FBX 上手动测量（§14.3）。",
        "",
        "| Profile | 布局 | 模式 | 三角形 | draws | CPU ms | 透视 ms |",
        "|---|---|---|---:|---:|---:|---:|",
    ]
    for name, data in results["profiles"].items():
        for scenario_key, label in (("single_static", "单视图静止"),
                                    ("single_nav", "单视图导航"),
                                    ("quad_static", "四视图静止")):
            d = data[scenario_key]
            lines.append(
                f"| {name} | {label} | {d['quality_profile']} | "
                f"{d['triangle_count']:,} | {d['draw_calls']} | "
                f"{d['total_cpu_ms']:.2f} | {d['perspective_ms']:.2f} |")
    lines.append("")
    with open(md_path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))
    print(f"写入: {json_path}")
    print(f"写入: {md_path}")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="3D 视口合成 perf benchmark")
    parser.add_argument("--write", action="store_true",
                        help="把结果写入 docs/benchmarks/3d_viewport/")
    args = parser.parse_args(argv)
    results = run_benchmark()
    if args.write:
        write_reports(results)
    else:
        print(json.dumps(results, indent=2, ensure_ascii=False))
    # 打印每个 profile 的 overlay 示例（验证 format_overlay）
    print("\n── overlay 示例 ──")
    for name, data in results["profiles"].items():
        s_dict = data["single_static"]
        class _S:
            pass
        s = _S()
        for k, v in s_dict.items():
            setattr(s, k, v)
        print(f"  {name:6s} single: {format_overlay(s, fps=60.0)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
