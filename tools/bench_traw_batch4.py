#!/usr/bin/env python3
"""批 4 标定：TRAW 16-bit linear 六档锚点实测（tools/traw_batch4_*.json）。

与批 0 试点（bench_traw_pilot.py）同源机械：真实/合成场景 → 传感器仿真
（shot+read 噪声，read=5e，FW=16000e）→ RGGB 相位拆分 → 16-bit linear
码域（e- × 65535/CURVE_FW，无曲线）→ toos CLI pf=3 CFA 编码扫 qp。

产出：
1. ratio-vs-qp 曲线（四传感器真实 DNG）→ 六档锚点（有效 qe 域融合，
   最小化 |log ratio − log 名义| 后按可分 qe 吸附——qp 封顶使 52..63
   混叠到 qe63、64..75 不可达，直接平均 qp_base 会落死区，见 qe_of）；
2. 无损边界：maxerr=0 的最大 qp（编码端预失真后的像素精确域）；
3. 各锚点 maxerr（16-bit linear 域验收 ≤ 噪声量级）。
"""
from __future__ import annotations

import ctypes
import json
import subprocess
import tempfile
from datetime import date
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parent.parent
CLI = REPO / "native/topos_codec/build/release/toos"
OUT_JSON = REPO / f"tools/traw_batch4_{date.today().isoformat()}.json"

W, H = 3840, 2160
TARGET_W, TARGET_H = W, H  # DNG 中心裁剪目标（锚点与合成场景同口径）
PW, PH = W // 2, H // 2
FW = 16000.0
CURVE_FW = 16384.0
CODE16_MAX = 65535
GAIN16 = CODE16_MAX / CURVE_FW  # e- → 16-bit 码
# qp 封顶几何（quant.h tc_qp_eff_ceiling）：qe = qp_base + 12（bd16 偏移），
# qp_base ≤63 → qe 顶格 63（51..63 整段混叠到 qe63）；qp_base ≥64 → qe 放开
# 到 95。可分 qe = [0..63] ∪ [76..95]，64..75 不可达（死区）。sweep 全偶数
# 覆盖 20..84：死区段各点字节相同，即混叠的自文档证据。
QPS = list(range(20, 86, 2))
TIERS = {"2": 2.0, "4": 4.0, "6": 6.0, "8": 8.0, "12": 12.0, "16": 16.0}
BD16_QP_OFFSET = 12


def qe_of(qp_base: int) -> int:
    """有效 qe（与 codec.c 编码端同式：+偏移后按 qp_base 域封顶）。"""
    qe = qp_base + BD16_QP_OFFSET
    return min(qe, 95 if qp_base >= 64 else 63)


def snap_reachable_qe(mean_qe: float) -> int:
    """融合 qe 吸附到 sweep 可达的 qe（候选=sweep 各 qp 的实际 qe 集合，
    混叠段 52..62 → qe63）；跨死区平手时向下取（CQ 画质为硬约束）。"""
    cands = sorted({qe_of(q) for q in QPS})
    return min(cands, key=lambda qe: (abs(mean_qe - qe), qe))


# ---------- 场景（与试点同构；真实源缺失时自动降级为纯合成） ----------

def _scene_synth() -> np.ndarray:
    rows = np.arange(H)[:, None].astype(np.float64)
    cols = np.arange(W)[None, :].astype(np.float64)
    r = np.full((H, W), 0.5)
    g = np.full((H, W), 0.5)
    b = np.full((H, W), 0.5)
    t = cols / W
    r += 0.25 * (1 - t) - 0.1 * t
    b += 0.25 * t - 0.05 * (1 - t)
    g += 0.02 * np.sin(t * np.pi)
    cc = [(0.36, 0.21, 0.13), (0.73, 0.55, 0.36), (0.20, 0.52, 0.73),
          (0.18, 0.48, 0.27), (0.46, 0.22, 0.46), (0.72, 0.25, 0.16),
          (0.90, 0.82, 0.60), (0.88, 0.62, 0.10), (0.32, 0.12, 0.36),
          (0.12, 0.44, 0.18), (0.70, 0.24, 0.16), (0.85, 0.52, 0.70)]
    ph_, pw_ = H // 6, W // 8
    for i, (cr_, cg_, cb_) in enumerate(cc):
        y0, x0 = divmod(i, 4)
        r[y0 * ph_ + 8:(y0 + 1) * ph_ - 8, x0 * pw_ + 8:(x0 + 1) * pw_ - 8] = cr_
        g[y0 * ph_ + 8:(y0 + 1) * ph_ - 8, x0 * pw_ + 8:(x0 + 1) * pw_ - 8] = cg_
        b[y0 * ph_ + 8:(y0 + 1) * ph_ - 8, x0 * pw_ + 8:(x0 + 1) * pw_ - 8] = cb_
    tex = 0.5 + 0.28 * np.sin(cols / 1.7) * np.sin(rows / 2.3) * np.sin((cols + rows) / 3.1)
    band = slice(int(H * 0.72), int(H * 0.9))
    r[band, int(W * 0.55):int(W * 0.95)] = tex[band, int(W * 0.55):int(W * 0.95)]
    g[band, int(W * 0.55):int(W * 0.95)] = tex[band, int(W * 0.55):int(W * 0.95)]
    b[band, int(W * 0.55):int(W * 0.95)] = tex[band, int(W * 0.55):int(W * 0.95)]
    wedge = ((cols + rows * 0.6) % 64 < 32).astype(np.float64)
    wr = (slice(int(H * 0.02), int(H * 0.14)), slice(int(W * 0.05), int(W * 0.45)))
    r[wr] = wedge[wr]
    g[wr] = wedge[wr]
    b[wr] = wedge[wr]
    return np.clip(np.stack([r, g, b], axis=-1), 0.0, 1.0)


def _scene_real() -> np.ndarray | None:
    src = REPO / "samples/4K_prores422HQ/4K_prores422HQ_#10.mov"
    if not src.exists():
        alt = list((REPO / "samples").glob("**/*#10*.mov")) if (REPO / "samples").exists() else []
        if not alt:
            return None
        src = alt[0]
    import av
    cont = av.open(str(src))
    frame = None
    for i, f in enumerate(cont.decode(video=0)):
        if i == 10:
            frame = f
            break
    cont.close()
    if frame is None:
        return None
    y = np.frombuffer(ctypes.string_at(frame.planes[0].buffer_ptr,
                                       frame.planes[0].buffer_size),
                      dtype="<u2").reshape(frame.planes[0].height,
                                           frame.planes[0].line_size // 2)
    y = y[:frame.height, :frame.width].astype(np.float64)
    cb_pl, cr_pl = frame.planes[1], frame.planes[2]
    cb = np.frombuffer(ctypes.string_at(cb_pl.buffer_ptr, cb_pl.buffer_size),
                       dtype="<u2").reshape(cb_pl.height, cb_pl.line_size // 2)[:frame.height, :cb_pl.width]
    cr = np.frombuffer(ctypes.string_at(cr_pl.buffer_ptr, cr_pl.buffer_size),
                       dtype="<u2").reshape(cr_pl.height, cr_pl.line_size // 2)[:frame.height, :cr_pl.width]
    yy = (y - 64.0) / 876.0
    cb = (cb - 64.0) / 896.0 - 0.5
    cr = (cr - 64.0) / 896.0 - 0.5
    cb = np.repeat(cb, 2, axis=1)[:, :W]
    cr = np.repeat(cr, 2, axis=1)[:, :W]
    r = yy + 1.5748 * cr
    g = yy - 0.1873 * cb - 0.4681 * cr
    b = yy + 1.8556 * cb
    return np.clip(np.stack([r, g, b], axis=-1), 0.0, 1.0)


def sensor_sim(rgb_lin: np.ndarray, read: float, rng) -> np.ndarray:
    x_e = np.clip(rgb_lin, 0.0, 1.0) * FW
    sigma = np.sqrt(read * read + x_e)
    return np.clip(x_e + rng.normal(0.0, 1.0, x_e.shape) * sigma, 0.0, CURVE_FW)


def cfa_split(x: np.ndarray) -> list[np.ndarray]:
    return [x[0::2, 0::2, 0].copy(), x[0::2, 1::2, 1].copy(),
            x[1::2, 0::2, 1].copy(), x[1::2, 1::2, 2].copy()]


def load_dng_cfa16(path: Path) -> tuple[list[np.ndarray], dict]:
    """真实 DNG → 16-bit 线性码域 CFA 四相位平面（R,Gr,Gb,B）。

    raw_image_visible（CFA 单通道）中心裁剪到 TARGET_W×TARGET_H，按
    raw_pattern 拆相位平面并线性归一到 16-bit 容器（×65535/white_level）。
    跳过传感器仿真——这就是真实传感器数据（批 4 锚点复测用途）。
    """
    import rawpy
    with rawpy.imread(str(path)) as raw:
        cfa = raw.raw_image_visible
        pattern = np.array(raw.raw_pattern)
        white = float(raw.white_level or 65535.0)
    ph, pw = pattern.shape
    tw, th = TARGET_W, TARGET_H
    if cfa.shape[0] < th or cfa.shape[1] < tw:
        raise ValueError(f"{path}: sensor {cfa.shape[1]}x{cfa.shape[0]} < {tw}x{th}")
    y0 = ((cfa.shape[0] - th) // 2 // ph) * ph
    x0 = ((cfa.shape[1] - tw) // 2 // pw) * pw
    cfa = cfa[y0:y0 + th, x0:x0 + tw].astype(np.float64)
    codes = np.clip(cfa * (65535.0 / white), 0.0, 65535.0)
    # 相位平面按颜色角色排序（CLI 契约 R/Gr/Gb/B）：Gr = 与 R 同行的 G。
    roles = {}
    for pi in range(ph):
        for pj in range(pw):
            v = int(pattern[pi][pj])
            roles[(pi, pj)] = {0: 'R', 2: 'B'}.get(v, None)
    r_pos = next(k for k, v in roles.items() if v == 'R')
    b_pos = next(k for k, v in roles.items() if v == 'B')
    for (pi, pj), v in list(roles.items()):
        if v is None:
            roles[(pi, pj)] = 'Gr' if pi == r_pos[0] else 'Gb'
    planes = [None] * 4
    for (pi, pj), role in roles.items():
        planes[{'R': 0, 'Gr': 1, 'Gb': 2, 'B': 3}[role]] = \
            codes[pi::ph, pj::pw].astype(np.float64)
    meta = {"sensor": f"{cfa.shape[1]}x{cfa.shape[0]}",
            "white_level": white, "pattern": pattern.tolist()}
    return planes, meta


def _encode_cfa16(planes: list[np.ndarray], qp: int, path: Path) -> int:
    raw = path.with_suffix(".raw")
    np.concatenate([p.reshape(-1) for p in planes]).astype("<u2").tofile(raw)
    proc = subprocess.run(
        [str(CLI), "encode", str(raw), "-o", str(path), "--width", str(W),
         "--height", str(H), "--pixel-format", "3",
         "--bit-depth", "16", "--qmatrix", "0", "--qp", str(qp), "--overwrite"],
        capture_output=True, text=True)
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
    raw.unlink()
    size = path.stat().st_size
    return size


def _decode_cfa16(path: Path) -> list[np.ndarray]:
    out = path.with_suffix(".dec.raw")
    proc = subprocess.run([str(CLI), "decode", str(path), "-o", str(out)],
                          capture_output=True, text=True)
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
    dec = np.fromfile(out, dtype="<u2").reshape(4, PH, PW)
    out.unlink()
    return [dec[i] for i in range(4)]


def run_scene_planes(tag: str, planes16: list[np.ndarray], meta: dict | None = None) -> dict:
    """对给定 16-bit CFA 平面扫 qp（合成与真实 DNG 共用）。"""
    container = W * H * 2  # 16-bit 容器名义体积（单样本 2B × W·H 总样本）
    points = []
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        for qp in QPS:
            f = td / f"q{qp}.toos"
            size = _encode_cfa16(planes16, qp, f)
            dec = _decode_cfa16(f)
            maxerr = int(max(np.max(np.abs(a.astype(np.int32) - b.astype(np.int32)))
                             for a, b in zip(planes16, dec)))
            points.append({"qp": qp, "bytes": size,
                           "ratio_vs_raw": container / size,
                           "code_maxerr": maxerr})
            print(f"  [{tag}] qp={qp:2d} ratio={container/size:6.2f}:1 maxerr={maxerr}")
    anchors = {}
    for name, nominal in TIERS.items():
        best = min(points, key=lambda r: abs(np.log2(r["ratio_vs_raw"]) - np.log2(nominal)))
        anchors[name] = {"qp": best["qp"], "ratio": round(best["ratio_vs_raw"], 3),
                         "code_maxerr": best["code_maxerr"]}
    lossless_qp = max((r["qp"] for r in points if r["code_maxerr"] == 0), default=None)
    return {"scene": tag, "container_bytes": container,
            "source_meta": meta or {}, "qp_points": points,
            "tier_anchors": anchors, "lossless_max_qp": lossless_qp}


def run_scene(tag: str, rgb_lin: np.ndarray, rng) -> dict:
    ph_e = sensor_sim(rgb_lin, 5.0, rng)
    planes16 = [np.clip(np.rint(p * GAIN16), 0, CODE16_MAX).astype(np.uint16)
                for p in cfa_split(ph_e)]
    container = W * H * 2  # 16-bit 容器名义体积（单样本 2B × W·H 总样本）
    points = []
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        for qp in QPS:
            f = td / f"q{qp}.toos"
            size = _encode_cfa16(planes16, qp, f)
            dec = _decode_cfa16(f)
            maxerr = int(max(np.max(np.abs(a.astype(np.int32) - b.astype(np.int32)))
                             for a, b in zip(planes16, dec)))
            points.append({"qp": qp, "bytes": size,
                           "ratio_vs_raw": container / size,
                           "code_maxerr": maxerr})
            print(f"  [{tag}] qp={qp:2d} ratio={container/size:6.2f}:1 maxerr={maxerr}")
    anchors = {}
    for name, nominal in TIERS.items():
        best = min(points, key=lambda r: abs(np.log2(r["ratio_vs_raw"]) - np.log2(nominal)))
        anchors[name] = {"qp": best["qp"], "ratio": round(best["ratio_vs_raw"], 3),
                         "code_maxerr": best["code_maxerr"]}
    lossless_qp = max((r["qp"] for r in points if r["code_maxerr"] == 0), default=None)
    return {"scene": tag, "container_bytes": container, "qp_points": points,
            "tier_anchors": anchors, "lossless_max_qp": lossless_qp}


def main() -> None:
    assert CLI.exists(), f"先构建 CLI: {CLI}"
    report = {"date": date.today().isoformat(), "geometry": f"{W}x{H} (CFA {PW}x{PH} x4)",
              "domain": "16-bit linear (e- x 65535/16384, read=5e)", "scenes": []}
    # 批 4 锚点复测：真实 DNG（samples/dng/*.dng，真传感器码域直编）优先；
    # 缺失时降级合成场景。每个 DNG 独立成 scene（不同传感器/位深分开记录）。
    import glob as _glob
    dngs = sorted(_glob.glob(str(REPO / "samples/dng/*.dng")))
    for d in dngs:
        tag = Path(d).stem
        try:
            planes, meta = load_dng_cfa16(Path(d))
        except Exception as e:
            print(f"  [{tag}] 加载失败（{e}），跳过")
            continue
        report["scenes"].append(run_scene_planes(f"dng:{tag}", planes, meta))
    if dngs:
        report["domain"] = "16-bit linear（真实传感器码域 ×65535/white_level）"
    if not dngs:
        synth = _scene_synth()
        report["scenes"].append(run_scene("synth", synth, np.random.default_rng(11)))
        real = _scene_real()
        if real is not None:
            report["scenes"].append(run_scene("real", real, np.random.default_rng(12)))
        else:
            print("  [real] 真实源缺失，跳过")
    # 锚点融合：先映射到有效 qe（封顶后）再平均——直接平均 qp_base 会把
    # 场景分歧（qe63 vs qe76）平均进 52..63 死区（如 qp57：名义 qe69，
    # 实际被 63 封顶，从未实测）。融合后吸附到 sweep 可达 qe；锚点存
    # qp_base = target_qe − 12（+12 恰好触顶的自洽可验值，51..63 字节
    # 同径），标签取混叠段代表实测点。
    scenes = report["scenes"]
    fused = {}
    for name in TIERS:
        cands = [s["tier_anchors"][name] for s in scenes]
        mean_qe = sum(qe_of(c["qp"]) for c in cands) / len(cands)
        target_qe = snap_reachable_qe(mean_qe)
        rep_q = min(QPS, key=lambda q: (abs(qe_of(q) - target_qe), q))
        ratios, maxerrs = [], []
        for s in scenes:
            pt = next(p for p in s["qp_points"] if p["qp"] == rep_q)
            ratios.append(round(pt["ratio_vs_raw"], 3))
            maxerrs.append(pt["code_maxerr"])
        fused[name] = {"qp": target_qe - BD16_QP_OFFSET,
                       "target_qe": target_qe, "rep_sweep_qp": rep_q,
                       "ratios": ratios, "code_maxerr": maxerrs}
    report["fused_tier_anchors"] = fused
    report["lossless_max_qp_per_scene"] = {s["scene"]: s["lossless_max_qp"] for s in scenes}
    OUT_JSON.write_text(json.dumps(report, indent=2, ensure_ascii=False))
    print(f"\nwrote {OUT_JSON}")
    for name, a in fused.items():
        print(f"  raw16-{name}: qp{a['qp']}  ratios={a['ratios']}  maxerr={a['code_maxerr']}")


if __name__ == "__main__":
    main()
