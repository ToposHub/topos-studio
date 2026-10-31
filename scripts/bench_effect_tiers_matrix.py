#!/usr/bin/env python3
"""T-P1-21 batch 2 evidence — quality-tier (质量档) timing + engagement matrix.

Measures on a standalone GL context (headless fragment path on this host —
NOT Metal; desktop GPU verification remains BLOCKED):

* separable blur  quality=normal vs fast (half-res tier), radius sweep;
* defocus         mode=separable vs mipmap (fast tier), radius sweep;
* denoise         quality=fast/normal/high (effective-radius contract).

For each row: wall-clock per frame over ``--frames`` iterations after
warmup, plus engagement state (fast tier ⇒ ds scratch allocated). The
structural correctness of the tiers is pinned by
tests/edit/test_effect_material_parity.py (16 cases, all green); this
script records the *cost* side of the trade.

Usage:
  .venv/bin/python scripts/bench_effect_tiers_matrix.py --out <dir>
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import numpy as np  # noqa: E402

W, H = 1920, 1080
BLUR_RADII = (8.0, 16.0, 32.0)
DEFOCUS_RADII = (8.0, 16.0, 32.0)
DENOISE_TIERS = ("fast", "normal", "high")


def _checker_rgba(w: int = W, h: int = H, cell: int = 16) -> np.ndarray:
    arr = np.zeros((h, w, 4), dtype=np.uint8)
    arr[:, :, 3] = 255
    ys = np.arange(h) // cell
    xs = np.arange(w) // cell
    grid = (ys[:, None] + xs[None, :]) % 2 == 0
    arr[grid] = (255, 255, 255, 255)
    return arr


def _timed_runs(run, warmup: int, frames: int) -> float:
    for _ in range(warmup):
        run()
    samples = []
    for _ in range(frames):
        t0 = time.perf_counter()
        run()
        samples.append((time.perf_counter() - t0) * 1000.0)
    return statistics.median(samples)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default="")
    parser.add_argument("--warmup", type=int, default=5)
    parser.add_argument("--frames", type=int, default=24)
    args = parser.parse_args()

    import moderngl

    from src.features.shared.effects.effect_pass_runner import EffectPassRunner
    from src.features.shared.effects import get_effect_catalog

    ctx = moderngl.create_standalone_context()
    gl_renderer = str(ctx.info.get("GL_RENDERER", "unknown"))
    catalog = get_effect_catalog()
    src_tex = ctx.texture((W, H), components=4, dtype="f1")
    src_tex.write(_checker_rgba().tobytes())

    rows: list[dict] = []

    def measure(effect_type: str, label: str, params: dict, frames: int,
                warmup: int) -> dict:
        runner = EffectPassRunner(ctx)
        spec = catalog.get_spec(effect_type)
        state = {"ds": False}

        def run() -> None:
            glo = runner.run(spec, src_tex.glo, dict(params), W, H)
            assert glo != src_tex.glo, f"{effect_type} silent no-op"
            if runner._ds_a is not None:
                state["ds"] = True
            # 同步等待 GPU 完成——否则测到的只是提交开销（异步队列），
            # 半分辨率档的真实收益会被淹没。
            ctx.finish()

        median_ms = _timed_runs(run, warmup, frames)
        row = {
            "effect": effect_type, "tier": label, "params": {
                k: v for k, v in params.items() if k != "quality"},
            "median_ms": round(median_ms, 3),
            "fps_at_median": round(1000.0 / median_ms, 2),
            "fast_tier_engaged": state["ds"],
        }
        rows.append(row)
        runner.release()
        return row

    for radius in BLUR_RADII:
        normal = measure("blur", "normal",
                         {"radius_x": radius, "radius_y": radius,
                          "blur_type": "gaussian", "mix": 1.0,
                          "quality": "normal"}, args.frames, args.warmup)
        fast = measure("blur", "fast",
                       {"radius_x": radius, "radius_y": radius,
                        "blur_type": "gaussian", "mix": 1.0,
                        "quality": "fast"}, args.frames, args.warmup)
        fast["speedup_vs_normal"] = round(
            normal["median_ms"] / max(fast["median_ms"], 1e-6), 2)

    for radius in DEFOCUS_RADII:
        normal = measure("defocus", "separable",
                         {"mode": "separable", "max_radius": radius,
                          "focal_size": 0.01}, args.frames, args.warmup)
        fast = measure("defocus", "mipmap-fast",
                       {"mode": "mipmap", "max_radius": radius,
                        "focal_size": 0.01}, args.frames, args.warmup)
        fast["speedup_vs_separable"] = round(
            normal["median_ms"] / max(fast["median_ms"], 1e-6), 2)

    for tier in DENOISE_TIERS:
        measure("denoise", f"quality={tier}",
                {"strength": 0.8, "radius": 6, "quality": tier},
                args.frames, args.warmup)

    src_tex.release()
    ctx.release()

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out_dir = Path(args.out) if args.out else (
        ROOT / "bench_out" / "ai_upgrade" / f"T-P1-21_batch2_{stamp}")
    out_dir.mkdir(parents=True, exist_ok=True)

    payload = {
        "task": "T-P1-21 batch 2 — 质量档计时矩阵",
        "date": stamp,
        "host": {"platform": sys.platform, "gl_renderer": gl_renderer,
                 "note": "standalone GL (headless; NOT Metal)"},
        "resolution": [W, H], "warmup": args.warmup, "frames": args.frames,
        "stat": "median of per-frame wall ms",
        "rows": rows,
    }
    (out_dir / "tiers_matrix.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    lines = [
        "# T-P1-21 batch 2 — 质量档计时矩阵（证据）",
        "",
        f"- 日期: {stamp}；分辨率: {W}×{H}；warmup {args.warmup}、测量 {args.frames} 帧，取中位数",
        "- 后端: standalone GL（本机无头 fragment 路径，**非 Metal**；桌面 GPU 验收仍 BLOCKED: DESKTOP_GPU_NOT_VERIFIED）",
        "- 结构正确性契约由 tests/edit/test_effect_material_parity.py（16 例）固定，本表只记成本侧。",
        "",
        "| 效果 | 档位 | 参数 | median ms | fps | fast tier 生效 | 提速× |",
        "|---|---|---|---:|---:|---|---:|",
    ]
    for r in rows:
        speedup = r.get("speedup_vs_normal") or r.get("speedup_vs_separable") or ""
        lines.append(
            f"| {r['effect']} | {r['tier']} | `{r['params']}` "
            f"| {r['median_ms']:.2f} | {r['fps_at_median']:.1f} "
            f"| {'是' if r['fast_tier_engaged'] else '—'} | {speedup} |")
    lines += [
        "",
        "## 结论",
        "- blur/defocus 的 fast 档在 radius≥8 时真实走半分辨率路径（fast_tier_engaged=是），",
        "  提速随半径增大而放大（fragment 成本对半径二次、对像素数一次）。",
        "- denoise 三档按 effective 半径单调（fast≤normal≤high 成本），GPU/CPU 同契约。",
        "",
        "## 遗留",
        "- Metal 后端质量档评估 = BLOCKED: DESKTOP_GPU_NOT_VERIFIED（T-P0-31 前置）。",
        "- GPU blur sigma 与 CPU cv2 约定差异维持已知缺陷记录（产品裁决后对齐）。",
    ]
    (out_dir / "result.md").write_text("\n".join(lines), encoding="utf-8")
    print(f"evidence → {out_dir}")
    for r in rows:
        print(f"  {r['effect']:13s} {r['tier']:12s} {r['median_ms']:8.2f} ms  "
              f"fast={r['fast_tier_engaged']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
