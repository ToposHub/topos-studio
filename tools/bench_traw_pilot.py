#!/usr/bin/env python3
"""TRAW（Topos RAW）可行性试点：RGGB 相位平面 + 12-bit log 曲线 + 现有 codec。

目的（TRAW 计划实测立项依据，docs/codec/topos_traw_format_plan_2026-09-12.md）：
不改动 codec 内核，用现有 toos CLI（GBR 4:4:4 12-bit + rans2 + qm0）承载
Bayer 相位平面，回答四个立项问题：
  1. 压缩率：视觉无损/数学无损档能到多少（对标未压缩 RAW 与 FFV1 无损锚）；
  2. 透明拐点：codec 误差相对传感器噪声的裕量（margin dB）随 qp 的曲线——
     RAW 的画质标准不是 PSNR 绝对值，而是 codec 误差须显著低于传感器噪声；
  3. Gr/Gb 相位拆分 vs 合并 G 的码率代价（4 平面方案的税）；
  4. 速度：4K RAW 帧编解码墙钟（F1+F2 两帧口径）。

管线：真实 4K 素材帧（4K_prores422HQ.mov #10）+ 合成场景 → sRGB 线性化
→ 传感器仿真（e- 域，shot+read 噪声，base ISO read=5e / 高 ISO read=25e）
→ RGGB CFA 采样 → 4 相位平面 → PWL-log 12-bit 曲线（线性趾 + 11 档 log，
S=B0=8e，FW=16384e）→ toos encode（pf2 bd12 qm0）→ decode → 反曲线 →
e- 域 RMSE vs 噪声 σ → margin。

输出 tools/traw_pilot_<date>.json。
"""
from __future__ import annotations

import ctypes
import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parents[1]
CLI = REPO / "native/topos_codec/build/release/toos"
FFMPEG = "ffmpeg"
SRC_MOV = REPO / "tests/test_videos/4K_prores422HQ.mov"
FRAME_ID = 10

W, H = 4096, 2160          # 4K DCI 场景分辨率
PW, PH = W // 2, H // 2    # 相位平面分辨率
FW = 16000.0               # 满阱（e-）
CURVE_FW = 16384.0         # 曲线设计满阱
CURVE_S = 8.0              # log 段锚点/软膝（e-）
CODE_MAX = 4095
QPS = [28, 36, 44, 52, 58, 64, 66, 68, 70]
DATE = "2026-09-12"


# ---------- 场景 ----------

def _srgb_to_linear(v: np.ndarray) -> np.ndarray:
    v = np.clip(v, 0.0, 1.0)
    return np.where(v > 0.04045, ((v + 0.055) / 1.055) ** 2.4, v / 12.92)


def _scene_real() -> np.ndarray:
    """真实素材帧 → sRGB [0,1]（limited-range 10-bit YUV422 → 709 RGB）。"""
    import av
    cont = av.open(str(SRC_MOV))
    frame = None
    for i, f in enumerate(cont.decode(video=0)):
        if i == FRAME_ID:
            frame = f
            break
    assert frame is not None, f"源不足 {FRAME_ID} 帧: {SRC_MOV}"
    y = np.frombuffer(ctypes.string_at(frame.planes[0].buffer_ptr,
                                       frame.planes[0].buffer_size),
                      dtype="<u2").reshape(frame.planes[0].height,
                                           frame.planes[0].line_size // 2)
    y = y[:frame.height, :frame.width].astype(np.float64)
    cb_pl, cr_pl = frame.planes[1], frame.planes[2]
    cw = cb_pl.width
    cb = np.frombuffer(ctypes.string_at(cb_pl.buffer_ptr, cb_pl.buffer_size),
                       dtype="<u2").reshape(cb_pl.height, cb_pl.line_size // 2)[:frame.height, :cw]
    cr = np.frombuffer(ctypes.string_at(cr_pl.buffer_ptr, cr_pl.buffer_size),
                       dtype="<u2").reshape(cr_pl.height, cr_pl.line_size // 2)[:frame.height, :cw]
    cont.close()
    yy = (y - 64.0) / 876.0
    cb = (cb - 64.0) / 896.0 - 0.5
    cr = (cr - 64.0) / 896.0 - 0.5
    cb = np.repeat(cb, 2, axis=1)[:, :W]
    cr = np.repeat(cr, 2, axis=1)[:, :W]
    r = yy + 1.5748 * cr
    g = yy - 0.1873 * cb - 0.4681 * cr
    b = yy + 1.8556 * cb
    return np.clip(np.stack([r, g, b], axis=-1), 0.0, 1.0)


def _scene_synth() -> np.ndarray:
    """合成场景：色卡 + 渐变 + 细纹理 + 锐利边缘（sRGB [0,1]）。"""
    rows = np.arange(H)[:, None].astype(np.float64)
    cols = np.arange(W)[None, :].astype(np.float64)
    r = np.full((H, W), 0.5)
    g = np.full((H, W), 0.5)
    b = np.full((H, W), 0.5)
    # 背景水平渐变（暖→冷）
    t = cols / W
    r += 0.25 * (1 - t) - 0.1 * t
    b += 0.25 * t - 0.05 * (1 - t)
    g += 0.02 * np.sin(t * np.pi)
    # 色卡（6×4 patch，类 ColorChecker 扩展）
    cc = [(0.36, 0.21, 0.13), (0.73, 0.55, 0.36), (0.20, 0.52, 0.73),
          (0.18, 0.48, 0.27), (0.46, 0.22, 0.46), (0.72, 0.25, 0.16),
          (0.90, 0.82, 0.60), (0.88, 0.62, 0.10), (0.32, 0.12, 0.36),
          (0.12, 0.44, 0.18), (0.70, 0.24, 0.16), (0.85, 0.52, 0.70),
          (0.55, 0.60, 0.80), (0.28, 0.55, 0.62), (0.75, 0.75, 0.20),
          (0.65, 0.35, 0.05), (0.40, 0.12, 0.10), (0.75, 0.80, 0.85),
          (0.20, 0.20, 0.20), (0.35, 0.35, 0.35), (0.52, 0.52, 0.52),
          (0.70, 0.70, 0.70), (0.85, 0.85, 0.85), (0.97, 0.97, 0.97)]
    ph, pw = H // 6, W // 8
    for i, (cr_, cg_, cb_) in enumerate(cc):
        y0, x0 = divmod(i, 4)
        r[y0 * ph + 8:(y0 + 1) * ph - 8, x0 * pw + 8:(x0 + 1) * pw - 8] = cr_
        g[y0 * ph + 8:(y0 + 1) * ph - 8, x0 * pw + 8:(x0 + 1) * pw - 8] = cg_
        b[y0 * ph + 8:(y0 + 1) * ph - 8, x0 * pw + 8:(x0 + 1) * pw - 8] = cb_
    # 细纹理带（高频，模拟枝叶）+ 黑白楔形（锐利边缘）
    tex = 0.5 + 0.28 * np.sin(cols / 1.7) * np.sin(rows / 2.3) * np.sin((cols + rows) / 3.1)
    band = slice(int(H * 0.72), int(H * 0.9))
    r[band, int(W * 0.55):int(W * 0.95)] = tex[band, int(W * 0.55):int(W * 0.95)]
    g[band, int(W * 0.55):int(W * 0.95)] = tex[band, int(W * 0.55):int(W * 0.95)]
    b[band, int(W * 0.55):int(W * 0.95)] = tex[band, int(W * 0.55):int(W * 0.95)]
    wedge = ((cols + rows * 0.6) % 64 < 32).astype(np.float64)
    r[int(H * 0.02):int(H * 0.14), int(W * 0.05):int(W * 0.45)] = \
        wedge[int(H * 0.02):int(H * 0.14), int(W * 0.05):int(W * 0.45)]
    g[int(H * 0.02):int(H * 0.14), int(W * 0.05):int(W * 0.45)] = \
        wedge[int(H * 0.02):int(H * 0.14), int(W * 0.05):int(W * 0.45)]
    b[int(H * 0.02):int(H * 0.14), int(W * 0.05):int(W * 0.45)] = \
        wedge[int(H * 0.02):int(H * 0.14), int(W * 0.05):int(W * 0.45)]
    return np.clip(np.stack([r, g, b], axis=-1), 0.0, 1.0)


# ---------- 传感器仿真 + CFA + log 曲线 ----------

def sensor_sim(rgb_lin: np.ndarray, read: float, rng) -> np.ndarray:
    """线性光 [0,1] → e- 域加噪声（shot+read），H×W×3 float64。"""
    x_e = np.clip(rgb_lin, 0.0, 1.0) * FW
    sigma = np.sqrt(read * read + x_e)
    return np.clip(x_e + rng.normal(0.0, 1.0, x_e.shape) * sigma, 0.0, CURVE_FW)


def cfa_split(x: np.ndarray) -> dict[str, np.ndarray]:
    """RGGB 相位下采样（逐通道，模拟每个像点单色）：
    R=(0::2,0::2,ch0) Gr=(0::2,1::2,ch1) Gb=(1::2,0::2,ch1) B=(1::2,1::2,ch2)。"""
    return {"R": x[0::2, 0::2, 0].copy(), "Gr": x[0::2, 1::2, 1].copy(),
            "Gb": x[1::2, 0::2, 1].copy(), "B": x[1::2, 1::2, 2].copy()}


_A = CODE_MAX / np.log2(1.0 + (CURVE_FW - CURVE_S) / CURVE_S)
_SLOPE_TOE = _A / (np.log(2.0) * (CURVE_S + CURVE_S))
_C0 = _A * np.log2(2.0) - _SLOPE_TOE * CURVE_S  # 趾线性段在 x=0 的码值


def log12_fwd(x: np.ndarray) -> np.ndarray:
    """e- → 12-bit 码：线性趾（x<B0）+ log 段（11 档，S=8e）。"""
    c = np.where(x < CURVE_S,
                 _C0 + _SLOPE_TOE * x,
                 _A * np.log2(1.0 + x / CURVE_S))
    return np.clip(np.rint(c), 0, CODE_MAX)


def log12_inv(c: np.ndarray) -> np.ndarray:
    x = np.where(c < _A,
                 (c - _C0) / _SLOPE_TOE,
                 CURVE_S * (np.power(2.0, c / _A) - 1.0))
    return np.clip(x, 0.0, CURVE_FW)


# ---------- toos CLI / FFV1 ----------

def _toos_encode(planes_gbr: list[np.ndarray], qp: int, path: Path,
                 qm: int = 0) -> tuple[float, int]:
    raw = path.with_suffix(".raw")
    np.concatenate([p.reshape(-1) for p in planes_gbr]).astype("<u2").tofile(raw)
    t0 = time.perf_counter()
    proc = subprocess.run(
        [str(CLI), "encode", str(raw), "-o", str(path), "--width", str(PW),
         "--height", str(PH), "--pixel-format", "2", "--bit-depth", "12",
         "--qmatrix", str(qm), "--qp", str(qp), "--overwrite"],
        capture_output=True, text=True)
    dt = time.perf_counter() - t0
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
    raw.unlink()
    return dt * 1000.0, path.stat().st_size


def _toos_decode(path: Path) -> tuple[float, list[np.ndarray]]:
    out = path.with_suffix(".dec.raw")
    t0 = time.perf_counter()
    proc = subprocess.run([str(CLI), "decode", str(path), "-o", str(out)],
                          capture_output=True, text=True)
    dt = time.perf_counter() - t0
    assert proc.returncode == 0, proc.stderr
    data = np.fromfile(out, dtype="<u2").reshape(3, PH, PW)
    out.unlink()
    return dt * 1000.0, [data[0].copy(), data[1].copy(), data[2].copy()]


def _ffv1_size(plane: np.ndarray, path: Path) -> int:
    raw = path.with_suffix(".ff.raw")
    plane.astype("<u2").tofile(raw)
    out = path.with_suffix(".ff.mkv")
    proc = subprocess.run(
        [FFMPEG, "-y", "-loglevel", "error", "-f", "rawvideo",
         "-pix_fmt", "gray16le", "-s", f"{plane.shape[1]}x{plane.shape[0]}",
         "-r", "24", "-i", str(raw),
         "-c:v", "ffv1", "-level", "3", "-pix_fmt", "gray16le", str(out)],
        capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"ffv1 rc={proc.returncode}: {proc.stderr[-500:]}")
    raw.unlink()
    size = out.stat().st_size
    out.unlink()
    return size


def _psnr(a: np.ndarray, b: np.ndarray, peak: float) -> float:
    mse = np.mean((a.astype(np.float64) - b.astype(np.float64)) ** 2)
    return float("inf") if mse == 0 else 10.0 * np.log10(peak * peak / mse)


# ---------- 主流程 ----------

def run_scene(tag: str, rgb_lin: np.ndarray, rng) -> dict:
    results: dict = {"frames_raw_bytes": W * H * 2, "iso_cases": {}}
    for iso, read in (("base", 5.0), ("high", 25.0)):
        x = sensor_sim(rgb_lin, read, rng)
        ph = cfa_split(x)
        codes = {k: log12_fwd(v).astype("<u2") for k, v in ph.items()}
        # 曲线自身量化误差（12-bit log 对 16-bit 域的精度账）
        curve_back = log12_inv(codes["Gr"].astype(np.float64))
        curve_rel = np.abs(curve_back - ph["Gr"]) / np.maximum(ph["Gr"], 1.0)
        gm = ((ph["Gr"] + ph["Gb"]) * 0.5)
        # 噪声 σ 取 Gr 相位处模型值（噪声实现 = x_noisy − x_clean，逐点 σ=sqrt(read²+signal)）
        sig = np.sqrt(read * read + np.clip(rgb_lin[0::2, 1::2, 1], 0.0, 1.0) * FW)
        case: dict = {
            "read_e": read,
            "sigma_gr_e": float(np.mean(sig)),
            "curve_rel_err_p99": float(np.percentile(curve_rel, 99) * 100.0),
            "ffv1_lossless_bytes": int(sum(_ffv1_size(ph[k], Path(f"/tmp/traw_{tag}_{iso}_{k}"))
                                           for k in ("R", "Gr", "Gb", "B"))),
        }

        for qp in QPS:
            with tempfile.TemporaryDirectory() as td:
                td = Path(td)
                # F1=(G←Gr,B←B,R←R)；F2=(Gb,Gb,Gb) → size(Gb)=s2/3（近似，帧头开销 ×3）
                # 4 平面真实账 = s1 + s2/3
                e1, s1 = _toos_encode([codes["Gr"], codes["B"], codes["R"]], qp, td / "f1.toos")
                d1, dec1 = _toos_decode(td / "f1.toos")
                e2, s2 = _toos_encode([codes["Gb"], codes["Gb"], codes["Gb"]], qp, td / "f2.toos")
                d2, _ = _toos_decode(td / "f2.toos")
                total = s1 + s2 // 3
                enc_ms = e1 + e2 / 3.0
                dec_ms = d1 + d2 / 3.0
                rec = {"qp": qp, "bytes_4ph": total,
                       "ratio_vs_raw": W * H * 2 / total,
                       "enc_ms_4ph": enc_ms, "dec_ms_4ph": dec_ms}
                # Gr 平面（G 槽）码域精度 + e- 域透明裕量
                dec_g = dec1[0].astype(np.float64)
                rec["code_psnr_gr"] = _psnr(codes["Gr"], dec1[0], CODE_MAX)
                rec["code_maxerr_gr"] = int(np.max(np.abs(codes["Gr"].astype(np.int32)
                                                          - dec1[0].astype(np.int32))))
                back = log12_inv(dec_g)
                rmse = float(np.sqrt(np.mean((back - ph["Gr"]) ** 2)))
                rec["gr_rmse_e"] = rmse
                rec["gr_margin_db"] = 20.0 * np.log10(case["sigma_gr_e"] / rmse) if rmse > 0 else 99.0
                # 合并 G 参照（仅两个 qp 点）：3 平面、绿采样减半 —— 文件大小下界参照
                if qp in (36, 52):
                    cm = log12_fwd(gm).astype("<u2")
                    e3, s3 = _toos_encode([cm, codes["B"], codes["R"]], qp, td / "f3.toos")
                    rec["bytes_3ph_merged"] = s3
                    rec["ratio_3ph_merged"] = W * H * 2 / s3
                case.setdefault("qp_points", []).append(rec)
        case["qp_points"].sort(key=lambda r: r["qp"])
        # qm1（Standard 矩阵）探针：噪声整形量化对照（仅 real/base）
        if tag == "real" and iso == "base":
            case["qm1_probe"] = []
            for qp in (44, 52, 64):
                with tempfile.TemporaryDirectory() as td:
                    e1, s1 = _toos_encode([codes["Gr"], codes["B"], codes["R"]], qp,
                                          Path(td) / "m1.toos", qm=1)
                    d1, dec1 = _toos_decode(Path(td) / "m1.toos")
                    dec_g = dec1[0].astype(np.float64)
                    back = log12_inv(dec_g)
                    rmse = float(np.sqrt(np.mean((back - ph["Gr"]) ** 2)))
                    case["qm1_probe"].append({
                        "qp": qp, "bytes_4ph": s1 + s1 // 3,
                        "ratio_vs_raw": W * H * 2 / (s1 + s1 // 3),
                        "code_psnr_gr": _psnr(codes["Gr"], dec1[0], CODE_MAX),
                        "gr_margin_db": 20.0 * np.log10(case["sigma_gr_e"] / rmse)
                        if rmse > 0 else 99.0})
            for r_ in case["qm1_probe"]:
                print(f"   qm1 qp{r_['qp']:>2}: ratio={r_['ratio_vs_raw']:5.2f} "
                      f"PSNR={min(r_['code_psnr_gr'], 99):5.1f} "
                      f"margin={r_['gr_margin_db']:5.1f}dB")
        results["iso_cases"][iso] = case
        print(f"[{tag}/{iso}] σ_Gr={case['sigma_gr_e']:.1f}e "
              f"FFV1={case['ffv1_lossless_bytes']/1e6:.2f}MB "
              f"curve_p99_rel={case['curve_rel_err_p99']:.3f}%")
        for r_ in case["qp_points"]:
            m = f" margin={r_['gr_margin_db']:.1f}dB" if "gr_margin_db" in r_ else ""
            extra = (f" 3ph={r_['bytes_3ph_merged']/1e6:.2f}MB"
                     f"(r{r_['ratio_3ph_merged']:.1f})" if "bytes_3ph_merged" in r_ else "")
            print(f"   qp{r_['qp']:>2}: {r_['bytes_4ph']/1e6:6.2f}MB "
                  f"ratio={r_['ratio_vs_raw']:5.2f} maxerr={r_['code_maxerr_gr']:>3d} "
                  f"PSNR={min(r_['code_psnr_gr'], 99):5.1f} "
                  f"enc={r_['enc_ms_4ph']:6.0f}ms dec={r_['dec_ms_4ph']:6.0f}ms{m}{extra}")
    return results


def main() -> None:
    global W, H, PW, PH
    assert CLI.exists(), f"toos CLI 未构建: {CLI}"
    rng = np.random.default_rng(20260912)
    real = _scene_real()
    H, W = real.shape[0], real.shape[1]   # 分辨率跟随源素材（4K_prores422HQ 为 UHD 3840×2160）
    PW, PH = W // 2, H // 2
    out = {"date": DATE, "res": f"{W}x{H}", "curve": {
        "type": "pwl_log12", "toe_below_e": CURVE_S, "log_span_stops":
        float(np.log2(1.0 + (CURVE_FW - CURVE_S) / CURVE_S)), "fw_e": CURVE_FW},
        "scenes": {}}
    for tag, scene in (("real", real), ("synth", _scene_synth())):
        print(f"=== scene {tag} ===")
        out["scenes"][tag] = run_scene(tag, scene, rng)
    dst = REPO / f"tools/traw_pilot_{DATE}.json"
    dst.write_text(json.dumps(out, indent=1, ensure_ascii=False))
    print(f"→ {dst}")


if __name__ == "__main__":
    main()
