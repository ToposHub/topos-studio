#!/usr/bin/env python3
"""TRAW 批 1 验收：六档（raw12-2/4/6/8/12/16）走 pf=3 新链全量复核。

复用批 0 试点（bench_traw_pilot.py）的传感器仿真 + PWL-log12 曲线方法，
但编码走**产品链路**：toos encode --tier raw12-*（pf=3 CFA 4 相位平面 +
profile 7 + qm0 + rans2，单帧单文件），不再用 3×GBR 帧近似账。

验收门（topos_traw_format_plan §1.1.1/§8）：
  - 六档典型素材噪声裕量 ≥ 各档验收下限（12-bit 4:1 默认档 ≥12dB 透明线）
  - 记录各档实测体积相对名义比率的浮动区间（干净场景显著小于名义、
    噪重场景略大——CQ 质量锚定语义）

输出 tools/traw_batch1_<date>.json。
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import bench_traw_pilot as pilot  # 复用传感器仿真/CFA/曲线/场景（批 0 冻结方法）

REPO = Path(__file__).resolve().parents[1]
CLI = pilot.CLI
DATE = "2026-09-13"

# 档位 → (锚点 qp, 名义比率, 验收下限 dB；None = 代理档无下限)
TIERS = [
    ("raw12-2", 45, 2.0, 20.0),
    ("raw12-4", 59, 4.0, 12.0),
    ("raw12-6", 64, 6.0, 6.0),
    ("raw12-8", 66, 8.0, 4.0),
    ("raw12-12", 68, 12.0, 2.0),
    ("raw12-16", 70, 16.0, None),
]


def _encode_tier(planes: list[np.ndarray], w: int, h: int, tier: str,
                 path: Path) -> tuple[float, int]:
    """pf=3 产品链编码：4 平面 R/Gr/Gb/B 依次拼接 → toos --tier。"""
    raw = path.with_suffix(".raw")
    np.concatenate([p.reshape(-1) for p in planes]).astype("<u2").tofile(raw)
    t0 = time.perf_counter()
    proc = subprocess.run(
        [str(CLI), "encode", str(raw), "-o", str(path),
         "--width", str(w), "--height", str(h), "--tier", tier, "--overwrite"],
        capture_output=True, text=True)
    dt = time.perf_counter() - t0
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
    raw.unlink()
    return dt * 1000.0, path.stat().st_size


def _decode(path: Path, planes: int, pw: int, ph: int) -> tuple[float, list[np.ndarray]]:
    out = path.with_suffix(".dec.raw")
    t0 = time.perf_counter()
    proc = subprocess.run([str(CLI), "decode", str(path), "-o", str(out)],
                          capture_output=True, text=True)
    dt = time.perf_counter() - t0
    assert proc.returncode == 0, proc.stderr
    data = np.fromfile(out, dtype="<u2").reshape(planes, ph, pw)
    out.unlink()
    return dt * 1000.0, [data[i].copy() for i in range(planes)]


def run_scene(tag: str, rgb_lin: np.ndarray, rng) -> dict:
    results: dict = {"frames_raw_bytes": pilot.W * pilot.H * 2, "tiers": {}}
    x = pilot.sensor_sim(rgb_lin, 5.0, rng)  # base ISO（read=5e；试点证明跨 ISO 稳定）
    ph_planes = pilot.cfa_split(x)
    order = ["R", "Gr", "Gb", "B"]
    codes = [pilot.log12_fwd(ph_planes[k]).astype("<u2") for k in order]
    # 噪声 σ 取 Gr 相位处模型值（与批 0 口径一致）
    sig = np.sqrt(25.0 + np.clip(rgb_lin[0::2, 1::2, 1], 0.0, 1.0) * pilot.FW)
    sigma_gr = float(np.mean(sig))
    results["sigma_gr_e"] = sigma_gr

    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        for tier, qp, nominal, floor in TIERS:
            enc_ms, size = _encode_tier(codes, pilot.W, pilot.H, tier, td / "f.toos")
            dec_ms, dec = _decode(td / "f.toos", 4, pilot.PW, pilot.PH)
            gr = 1  # 平面序 R=0/Gr=1/Gb=2/B=3
            maxerr = int(np.max(np.abs(codes[gr].astype(np.int32)
                                       - dec[gr].astype(np.int32))))
            back = pilot.log12_inv(dec[gr].astype(np.float64))
            rmse = float(np.sqrt(np.mean((back - ph_planes["Gr"]) ** 2)))
            margin = 20.0 * np.log10(sigma_gr / rmse) if rmse > 0 else 99.0
            rec = {
                "anchor_qp": qp, "bytes": size,
                "ratio_vs_raw": pilot.W * pilot.H * 2 / size,
                "nominal_ratio": nominal,
                "size_vs_nominal_pct": round(
                    (size / (pilot.W * pilot.H * 2 / nominal) - 1.0) * 100.0, 1),
                "code_maxerr_gr": maxerr,
                "gr_rmse_e": rmse,
                "gr_margin_db": round(margin, 1),
                "enc_ms": round(enc_ms, 0), "dec_ms": round(dec_ms, 0),
                "accept_floor_db": floor,
            }
            if floor is not None:
                rec["accept"] = bool(margin >= floor)
            else:
                rec["accept"] = True
            results["tiers"][tier] = rec
            print(f"  {tier:>9} qp{qp:>2}: {size/1e6:7.2f}MB "
                  f"ratio={rec['ratio_vs_raw']:5.2f}(nominal {nominal:>4.0f}:1 "
                  f"{rec['size_vs_nominal_pct']:+.1f}%) "
                  f"maxerr={maxerr:>3d} margin={margin:5.1f}dB "
                  f"enc={enc_ms:5.0f}ms dec={dec_ms:5.0f}ms "
                  f"{'PASS' if rec['accept'] else 'FAIL'}")
    return results


def main() -> None:
    assert CLI.exists(), f"toos CLI 未构建: {CLI}"
    rng = np.random.default_rng(20260913)
    real = pilot._scene_real()
    h, w = real.shape[0], real.shape[1]
    pilot.H, pilot.W = h, w
    pilot.PH, pilot.PW = h // 2, w // 2
    out = {"date": DATE, "res": f"{w}x{h}", "chain": "pf3/profile7/qm0/rans2 单帧",
           "scenes": {}}
    floors_ok = True
    for tag, scene in (("real", real), ("synth", pilot._scene_synth())):
        print(f"=== scene {tag} ===")
        out["scenes"][tag] = run_scene(tag, scene, rng)
        for tier, rec in out["scenes"][tag]["tiers"].items():
            if not rec["accept"]:
                floors_ok = False
                print(f"!!! {tag}/{tier} 裕量 {rec['gr_margin_db']}dB < "
                      f"下限 {rec['accept_floor_db']}dB")
    out["acceptance_all_pass"] = floors_ok
    dst = REPO / f"tools/traw_batch1_{DATE}.json"
    dst.write_text(json.dumps(out, indent=1, ensure_ascii=False))
    print(f"→ {dst}")
    print("批 1 验收：", "全部通过" if floors_ok else "存在未达下限档位（见上）")
    sys.exit(0 if floors_ok else 1)


if __name__ == "__main__":
    main()
