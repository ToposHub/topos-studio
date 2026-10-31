#!/usr/bin/env python3
"""T-P1-21 遗留项对拍数据：GPU 与 CPU 高斯模糊的 sigma 约定差异量化.

两路实现走生产代码（非影子实现）：

- GPU：EffectPassRunner（与预览/导出同一 GLSL 管线）；blur.glsl 约定
  ``sigma = max(u_radius * 0.5, 0.1)``，核半宽 ``ceil(u_radius)``。
- CPU：timeline_export._cpu_blur（导出/软件回退路径）；cv2 约定
  ``ksize = round(radius)*2+1, sigmaX=0`` → cv2 内部推
  ``sigma = 0.3*(round(radius)-1) + 0.8``。

约定差随半径增大（r=4: GPU 2.0 vs CPU 1.7；r=16: GPU 8.0 vs CPU 4.7）。
本脚本对每个 (radius, material) 组合输出两路输出的逐像素差统计
（mean/max LSB、PSNR、结构保留相关性），供"以哪侧约定为准"的产品
裁决参考。不修改任何实现。

用法：
    .venv/bin/python scripts/bench_blur_sigma_convention.py
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

W, H = 128, 96
RADII = (1.0, 2.0, 4.0, 8.0, 12.0, 16.0, 24.0, 32.0)


def _noise(seed: int = 7) -> np.ndarray:
    rng = np.random.default_rng(seed)
    base = np.full((H, W, 4), 128, dtype=np.uint8)
    noise = rng.integers(-60, 60, size=(H, W, 3), dtype=np.int16)
    base[:, :, :3] = np.clip(
        base[:, :, :3].astype(np.int16) + noise, 0, 255).astype(np.uint8)
    base[:, :, 3] = 255
    return base


def _steps() -> np.ndarray:
    frame = np.zeros((H, W, 4), dtype=np.uint8)
    band = H // 4
    for i, level in enumerate((40, 120, 200, 250)):
        frame[i * band:(i + 1) * band, :, :3] = level
    frame[:, :, 3] = 255
    return frame


def _checker(cell: int = 4) -> np.ndarray:
    ys, xs = np.mgrid[0:H, 0:W]
    v = (((ys // cell) + (xs // cell)) % 2 * 155 + 100).astype(np.uint8)
    frame = np.zeros((H, W, 4), dtype=np.uint8)
    frame[:, :, :3] = v[..., None]
    frame[:, :, 3] = 255
    return frame


MATERIALS = {
    "edge4": _checker(4),
    "noise": _noise(),
    "steps": _steps(),
}

BASE_PARAMS = {
    "blur_type": "gaussian", "mix": 1.0, "edge_policy": "clamp",
    "channel_r": True, "channel_g": True, "channel_b": True,
    "channel_a": False,
}


def run_gpu(rgba: np.ndarray, params: dict) -> np.ndarray:
    import moderngl

    from src.features.shared.effects import get_effect_catalog
    from src.features.shared.effects.effect_pass_runner import EffectPassRunner

    ctx = moderngl.create_standalone_context(require=330)
    try:
        spec = get_effect_catalog().get_spec("blur")
        assert spec is not None
        runner = EffectPassRunner(ctx)
        in_tex = ctx.texture((W, H), components=4, dtype="f1")
        in_tex.write(rgba.tobytes())
        out_glo = runner.run(spec, in_tex.glo, dict(params), W, H)
        assert out_glo != in_tex.glo, "GPU blur silent no-op"
        for name in ("_scratch_a", "_scratch_b", "_scratch_c"):
            tex = getattr(runner, name, None)
            if tex is not None and tex.glo == out_glo:
                fbo = getattr(runner, name + "_fbo")
                fbo.use()
                ctx.viewport = (0, 0, W, H)
                raw = fbo.read(components=4)
                return np.frombuffer(
                    raw, dtype=np.uint8).reshape(H, W, 4).copy()
        raise RuntimeError("output glo not in scratch textures")
    finally:
        ctx.release()


def run_cpu(rgba: np.ndarray, params: dict) -> np.ndarray:
    from src.features.edit.core import timeline_export as te

    bgr = rgba[:, :, [2, 1, 0, 3]]
    out = te._cpu_blur(np.ascontiguousarray(bgr), dict(params))
    return out[:, :, [2, 1, 0, 3]]


def metrics(gpu: np.ndarray, cpu: np.ndarray) -> dict:
    g = gpu[:, :, :3].astype(np.int16)
    c = cpu[:, :, :3].astype(np.int16)
    d = np.abs(g - c)
    mse = float((d.astype(np.float64) ** 2).mean())
    psnr = 10.0 * np.log10(255.0 ** 2 / mse) if mse > 0 else float("inf")
    gv = g.astype(np.float64).ravel()
    cv = c.astype(np.float64).ravel()
    corr = float(np.corrcoef(gv, cv)[0, 1]) if gv.std() > 0 else 1.0
    return {
        "mean_abs_lsb": round(float(d.mean()), 3),
        "max_abs_lsb": int(d.max()),
        "psnr_db": round(psnr, 2),
        "structure_corr": round(corr, 5),
    }


def main() -> int:
    rows = []
    for label, material in MATERIALS.items():
        for radius in RADII:
            params = dict(BASE_PARAMS, radius=radius)
            gpu = run_gpu(material, params)
            cpu = run_cpu(material, params)
            row = {"material": label, "radius": radius,
                   "gpu_sigma": max(radius * 0.5, 0.1),
                   "cpu_sigma_effective": 0.3 * (round(radius) - 1) + 0.8}
            row.update(metrics(gpu, cpu))
            rows.append(row)
            print(f"{label:7s} r={radius:5.1f} gpuσ={row['gpu_sigma']:.2f} "
                  f"cpuσ={row['cpu_sigma_effective']:.2f} "
                  f"mean={row['mean_abs_lsb']:7.3f} max={row['max_abs_lsb']:3d} "
                  f"psnr={row['psnr_db']:6.2f} corr={row['structure_corr']:.4f}",
                  flush=True)

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out_dir = ROOT / "bench_out" / "ai_upgrade" / f"T-P1-21_sigma_duipai_{stamp}"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "sigma_convention_matrix.json").write_text(
        json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")

    lines = [
        "# T-P1-21 遗留：GPU vs CPU 高斯模糊 sigma 约定对拍",
        "",
        f"- 日期：{stamp}；{W}x{H}；材质 edge4/noise/steps；半径 1..32",
        "- GPU 约定（blur.glsl）：sigma = max(radius*0.5, 0.1)，半宽 ceil(radius)",
        "- CPU 约定（cv2, sigmaX=0）：sigma = 0.3*(round(radius)-1) + 0.8，"
        "ksize=2*round(radius)+1",
        "- 两路均为生产实现（EffectPassRunner / _cpu_blur），mix=1、clamp 边缘",
        "",
        "| 材质 | 半径 | GPUσ | CPUσ | mean LSB | max LSB | PSNR dB | 结构相关 |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        lines.append(
            f"| {r['material']} | {r['radius']:g} | {r['gpu_sigma']:.2f} | "
            f"{r['cpu_sigma_effective']:.2f} | {r['mean_abs_lsb']:.3f} | "
            f"{r['max_abs_lsb']} | {r['psnr_db']:.2f} | "
            f"{r['structure_corr']:.4f} |")
    (out_dir / "result.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\nevidence -> {out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
