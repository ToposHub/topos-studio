# -*- coding: utf-8 -*-
"""CW-B10 验收:真实素材上的 warper 后端确定性/有限性/恒等冒烟。

对 B00 manifest 冻结的三份 fixture 的首/中锚点帧执行:
1. identity 网格三空间(HSV legacy / HSL / HSP)应用 = 原帧(浮点容差内);
2. 随机网格(固定种子)应用:全部有限(无 NaN/Inf)、画面确实变化;
3. 同输入重复求解逐位一致(确定性);
4. 记录 CPU 逐像素应用的耗时(仅参考;生产路径为 GPU)。

输出 docs/validation/color_warper_b10/report.json,供验收报告引用。

用法:
    .venv/bin/python scripts/validate_warper_b10.py
"""

from __future__ import annotations

import hashlib
import json
import platform
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import av
import numpy as np

REPO = Path(__file__).resolve().parent.parent
MANIFEST = REPO / "docs/validation/color_warper_b00/fixtures_manifest.json"
OUT_DIR = REPO / "docs/validation/color_warper_b10"
FIXTURES = REPO / "tests/test_videos"
SPACES = ("hsv", "hsl", "hsp")
IDENTITY_TOL = 2e-6  # rgb→空间→rgb CPU 往返(ε 防护)与 fp32 网格的合量级


def _anchor_frame(filename: str, index: int) -> tuple[np.ndarray, str]:
    with av.open(str(FIXTURES / filename)) as container:
        video = container.streams.video[0]
        video.thread_type = "AUTO"
        for position, frame in enumerate(container.decode(video)):
            if position == index:
                rgb = frame.reformat(format="rgb24")
                array = np.frombuffer(rgb.planes[0], dtype=np.uint8).reshape(
                    rgb.height, rgb.width, 3).astype(np.float32) / 255.0
                digest = hashlib.sha256(np.ascontiguousarray(array).tobytes()).hexdigest()
                return array, digest
    raise RuntimeError(f"frame {index} not found in {filename}")


def _grid(seed: int, size: int = 8, identity: bool = False) -> np.ndarray:
    if identity:
        return np.zeros((size, size, 2), np.float32)
    rng = np.random.default_rng(seed)
    grid = rng.normal(0, 0.15, (size, size, 2)).astype(np.float32)
    grid[..., 0] = np.clip(grid[..., 0], -0.3, 0.3)
    grid[..., 1] = np.clip(grid[..., 1], -0.4, 0.4)
    return np.ascontiguousarray(grid)


#: 确定性等步长采样(纯 Python 逐像素太慢;等步长覆盖整帧分布,
#: 全色域有限性/边界由单元测试夹具覆盖,B10 记录采样规模)。
VALIDATION_STRIDE = 8


def _apply(image: np.ndarray, grid: np.ndarray, space: str) -> np.ndarray:
    from src.features.color.core.warper_color import (
        apply_warper_in_space,
        apply_warper_legacy_hsv,
    )
    sampled = image[::VALIDATION_STRIDE, ::VALIDATION_STRIDE]
    out = np.empty_like(sampled)
    for row in range(sampled.shape[0]):
        for col in range(sampled.shape[1]):
            pixel = sampled[row, col]
            if space == "hsv":
                out[row, col] = apply_warper_legacy_hsv(pixel, grid)
            else:
                out[row, col] = apply_warper_in_space(pixel, grid, space)
    return out


def main() -> int:
    sys.path.insert(0, str(REPO))
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    report: dict = {
        "stage": "CW-B10",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "environment": {
            "pyav": av.__version__,
            "python": sys.version.split()[0],
            "platform": f"{platform.system()} {platform.release()} {platform.machine()}",
        },
        "fixtures": [],
        "all_passed": True,
    }
    for fixture in manifest["fixtures"]:
        name = fixture["file"]
        for anchor in ("first_frame", "mid_frame"):
            frame, digest = _anchor_frame(name, fixture[anchor]["frame_index"])
            entry = {
                "file": name, "anchor": anchor,
                "frame_index": fixture[anchor]["frame_index"],
                "input_sha256": digest,
                "spaces": [],
            }
            reference = frame[::VALIDATION_STRIDE, ::VALIDATION_STRIDE]
            for space in SPACES:
                identity = _apply(frame, _grid(0, identity=True), space)
                identity_max_diff = float(np.abs(identity - reference).max())
                grid = _grid(20261112)
                started = time.perf_counter()
                warped = _apply(frame, grid, space)
                elapsed_ms = (time.perf_counter() - started) * 1000.0
                warped_again = _apply(frame, grid, space)
                space_result = {
                    "space": space,
                    "sampled_pixels": int(reference.shape[0] * reference.shape[1]),
                    "identity_max_abs_diff": identity_max_diff,
                    "identity_ok": identity_max_diff <= IDENTITY_TOL,
                    "warped_all_finite": bool(np.isfinite(warped).all()),
                    "warped_changed": float(np.abs(warped - reference).max()) > 1e-4,
                    "deterministic": bool(np.array_equal(warped, warped_again)),
                    "cpu_apply_ms": round(elapsed_ms, 1),
                }
                entry["spaces"].append(space_result)
                if not all([space_result["identity_ok"],
                            space_result["warped_all_finite"],
                            space_result["warped_changed"],
                            space_result["deterministic"]]):
                    report["all_passed"] = False
            report["fixtures"].append(entry)
            print(f"{name} {anchor}: " + ", ".join(
                f"{s['space']}={'OK' if s['identity_ok'] and s['warped_all_finite'] and s['deterministic'] else 'FAIL'}"
                f"({s['cpu_apply_ms']}ms)" for s in entry["spaces"]))

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    target = OUT_DIR / "report.json"
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"report -> {target} | all_passed={report['all_passed']}")
    return 0 if report["all_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
