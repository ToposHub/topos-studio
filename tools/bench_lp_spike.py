#!/usr/bin/env python3
"""V9 LP spike（批 0）：zero-motion P 帧 P0 模拟——零内核改动的立项评审实测。

目的（V9 帧间微 GOP 计划 §5 批 0，docs/codec/topos_v9_micro_gop_plan_2026-09-13.md）：
不动 codec 内核，用现有 toos CLI（YUV 4:2:2 10-bit + flat qm0 + rans2，产品
同口径）回答 M1 决策门的全部数据问题：
  1. ρ = P_size/I_size 分内容层（静止/手持/B-roll/横摇/重纹理/切镜/UI）；
  2. 纯 Skip 帧体积下界（静态内容天花板）与逐帧 exact-skip 率；
  3. 1/3/5/10 代漂移曲线（重建参考链 vs 逐帧 direct intra 参照）；
  4. P vs I 单遍编码墙钟（P 锚 qp 直编，I 同 qp 单遍——产品现行
     『首帧 sized 定标 + 帧间反馈单遍』机制下单遍口径即生产口径）；
  5. ±4/±8 整像素 MV 原型（偶数水平 dx）增益上界——P1 是否值得做、
     R 取值的依据；
  6. 量化死区探针（uniform 偏移帧）——阈值 Skip 设计输入。

P0 模拟管线（§5 批 0 原文）：
  帧 N−1 按锚 qp 编码 → 解码得 recon（编码器重建参考）；
  X = clip(curr − recon + 512)（合法 intra 输入：X−512 = 帧间残差，
  signed 域与变换输入域重合，免电平偏移——计划 §2.3 域证明）；
  同 qp 编码 X 得 P_size → 解码回 X′ → recon′ = clip(recon + (X′−512))
  进下一迭代（天然含 drift 链）。

内容层窗口为 2026-09-14 全片扫描实测选定（缩略图 1/8 tdiff + 整像素全局
位移估计），来源 tests/test_videos/*.mov，详见报告
docs/codec/topos_lp_spike_2026-09-14.md。

输出 tools/lp_spike_<date>.json。
"""
from __future__ import annotations

import ctypes
import json
import subprocess
import tempfile
import time
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parents[1]
CLI = REPO / "native/topos_codec/build/release/toos"
SRC_4K = REPO / "tests/test_videos/4K_prores422HQ.mov"    # 3840x2160@25 yuv422p10le
SRC_2K = REPO / "tests/test_videos/2K_prores422HQ.mov"    # 1920x1080@25 yuv422p10le

QPS = [56, 64, 72]        # 锚 qp 扫描点（产品轨迹 62~87 附近取三点）
MID = 512                 # 10-bit 电平中点（bd_mid，与 codec.c 一致）
N_FRAMES = 11             # 每层帧数（1 I + 10 P）
DRIFT_AT = [1, 3, 5, 10]
MV_QP = 64                # MV 原型仅在中间锚点测
MV_RS = [4, 8]            # ±R 整像素两档（§3.4 R∈{4,8} 由本批数据冻结）
MB = 16
DATE = "2026-09-14"

# 内容层窗口（2026-09-14 全片扫描实测；缩略图 1/8 tdiff + 全局位移）：
#   static   f425~470  tdiff≈0.0010~0.0018 shift(0,0)  三脚架锁定+传感器噪声
#   handheld f100~130  tdiff 0.0027~0.0085 shift ±1px    轻晃
#   broll    f150~270  tdiff 0.007~0.021  shift(0,0)    局部运动为主
#   cut      f467~475  含 f471 切镜（tdiff 0.15，P 对 pre-cut 参考的回退场景）
#   pan_2k   f226~305  水平快速横摇 ≈24px/帧（缩略图 -3px 恒定）
#   texture_2k f307+   tdiff≈0.067 零全局运动            高纹理/密集局部运动
LAYERS = [
    ("static_4k", SRC_4K, 425, N_FRAMES),
    ("handheld_4k", SRC_4K, 100, N_FRAMES),
    ("broll_4k", SRC_4K, 150, N_FRAMES),
    ("cut_4k", SRC_4K, 467, 9),          # f471 切镜落在窗口中段（k=4）
    ("pan_2k", SRC_2K, 226, N_FRAMES),
    ("texture_2k", SRC_2K, 307, N_FRAMES),
    ("synth_ui_4k", None, 0, N_FRAMES),  # 合成 dashboard（真实素材无屏幕 UI 层）
]


# ---------- 素材抽取 ----------

def _planes(frame) -> list[np.ndarray]:
    """ProRes yuv422p10le 帧 → [Y, U, V] 紧凑 uint16（U/V 半宽）。"""
    out = []
    for p in frame.planes:
        arr = np.frombuffer(ctypes.string_at(p.buffer_ptr, p.buffer_size),
                            dtype="<u2").reshape(p.height, p.line_size // 2)
        out.append(np.ascontiguousarray(arr[:frame.height, :p.width]))
    return out


def extract_layer(src: Path, start: int, n: int) -> list[list[np.ndarray]]:
    import av
    cont = av.open(str(src))
    frames = []
    for i, f in enumerate(cont.decode(video=0)):
        if i >= start:
            frames.append(_planes(f))
            if len(frames) >= n:
                break
    cont.close()
    assert len(frames) == n, f"{src} 不足 {start}+{n} 帧"
    return frames


def synth_ui_frames(n: int) -> list[list[np.ndarray]]:
    """合成 4K dashboard：静态文字块 + 每帧左移 24px 的 ticker + 逐帧跳变时钟。

    真实素材无屏幕 UI 层（2026-09-14 扫描证实），按计划 §6 素材分层以合成
    补位并明确标注 synth。视频域码值（bg=512 中灰、文字 940 白/64 黑）。"""
    W, H = 3840, 2160
    cw = W // 2
    rng = np.random.default_rng(20260914)
    text = np.full((H, W), 512, dtype=np.uint16)
    # 标题栏 + 静态文字块（细横线+竖线模拟字形边缘）+ 分隔线
    text[120:200, 100:1400] = 940
    for k in range(6):
        y0 = 400 + k * 260
        text[y0:y0 + 6, 100:1800] = 940
        text[y0:y0 + 140, 1900:1906 + (k * 137) % 900] = 940
        text[y0:y0 + 6, 1900:2800] = 300
    text[2000:2006, 100:3700] = 940
    ticker = np.full((H, W), 640, dtype=np.uint16)
    for k in range(40):
        x0 = 160 + k * 96
        ticker[1880:1980, x0:x0 + 60] = 64
        ticker[1900:1960, x0 + 12:x0 + 48] = 940
    frames = []
    for t in range(n):
        y = text.copy()
        # ticker：内容左移 24px/帧（整数像素平移 → 局部揭示边缘）
        off = 24 * t
        y[1860:2000, :] = 640
        y[1860:2000, :W - off] = ticker[1860:2000, off:]
        # 时钟：每帧翻转（数字块跳变）
        y[60:200, 3500:3800] = 940 if t % 2 == 0 else 300
        y += rng.integers(-1, 2, size=(H, W), dtype=np.int16).astype(np.uint16)
        frames.append([y, np.full((H, cw), 512, dtype=np.uint16),
                       np.full((H, cw), 512, dtype=np.uint16)])
    return frames


# ---------- toos CLI（pf=0 4:2:2 / bd10 / qm0 / rans2，产品同口径）----------

def enc(planes: list[np.ndarray], qp: int, path: Path) -> tuple[float, int]:
    raw = path.with_suffix(".raw")
    np.concatenate([p.reshape(-1) for p in planes]).astype("<u2").tofile(raw)
    t0 = time.perf_counter()
    proc = subprocess.run(
        [str(CLI), "encode", str(raw), "-o", str(path),
         "--width", str(planes[0].shape[1]), "--height", str(planes[0].shape[0]),
         "--pixel-format", "0", "--bit-depth", "10", "--qmatrix", "0",
         "--entropy", "rans2", "--qp", str(qp), "--overwrite"],
        capture_output=True, text=True)
    dt = time.perf_counter() - t0
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
    raw.unlink()
    return dt * 1000.0, path.stat().st_size


def dec(path: Path, w: int, h: int) -> tuple[float, list[np.ndarray]]:
    out = path.with_suffix(".dec.raw")
    t0 = time.perf_counter()
    proc = subprocess.run([str(CLI), "decode", str(path), "-o", str(out)],
                          capture_output=True, text=True)
    dt = time.perf_counter() - t0
    assert proc.returncode == 0, proc.stderr
    cw = w // 2
    data = np.fromfile(out, dtype="<u2")
    assert data.size == w * h + 2 * cw * h, f"decode raw size {data.size}"
    y = data[:w * h].reshape(h, w).copy()
    u = data[w * h:w * h + cw * h].reshape(h, cw).copy()
    v = data[w * h + cw * h:].reshape(h, cw).copy()
    out.unlink()
    return dt * 1000.0, [y, u, v]


def clip1023(x: np.ndarray) -> np.ndarray:
    return np.clip(x, 0, 1023)


# ---------- P0 zero-motion 链 ----------

def p0_chain(layer_frames, qp: int, td: Path, tag: str) -> dict:
    """zero-motion P0 链：返回 ρ/drift/skip/墙钟。"""
    h, w = layer_frames[0][0].shape
    n = len(layer_frames)
    s_i, s_p, enc_i_ms, enc_p_ms, dec_ms = [], [], [], [], []
    exact_skip = []
    chain: dict[int, list[np.ndarray]] = {}
    direct: dict[int, list[np.ndarray]] = {}

    def resid_img(curr, recon):
        return [clip1023(curr[p].astype(np.int32) - recon[p].astype(np.int32)
                         + MID).astype(np.uint16) for p in range(3)]

    # I0 + 重建参考（direct 参照 = 逐帧 intra 重建）
    e, si = enc(layer_frames[0], qp, td / f"{tag}_i0.toos")
    _, rec = dec(td / f"{tag}_i0.toos", w, h)
    s_i.append(si)
    enc_i_ms.append(e)
    direct[0] = [p.copy() for p in rec]
    chain[0] = [p.copy() for p in rec]
    recon = rec
    for k in range(1, n):
        curr = layer_frames[k]
        e, sik = enc(curr, qp, td / f"{tag}_i{k}.toos")
        _, dir_k = dec(td / f"{tag}_i{k}.toos", w, h)
        s_i.append(sik)
        enc_i_ms.append(e)
        direct[k] = dir_k
        # exact-skip 率：16×16 luma MB 残差全零 + 对应两块 16×8 色度全零
        #（高度非 MB 倍数的余带不计入统计）
        x = resid_img(curr, recon)
        h_mb, w_mb = (h // MB) * MB, (w // MB) * MB
        cwb = ((w // 2) // 8) * 8
        ry = np.abs(x[0].astype(np.int32) - MID)[:h_mb, :w_mb].reshape(
            h_mb // MB, MB, w_mb // MB, MB)
        rc = np.abs(x[1].astype(np.int32) - MID)[:h_mb, :cwb].reshape(
            h_mb // MB, MB, cwb // 8, 8)
        hymax = np.max(ry, axis=(1, 3))
        cumax = np.max(rc, axis=(1, 3))
        exact_skip.append(float(np.mean((hymax == 0) & (cumax == 0))))
        e, sp = enc(x, qp, td / f"{tag}_p{k}.toos")
        dp, xd = dec(td / f"{tag}_p{k}.toos", w, h)
        s_p.append(sp)
        enc_p_ms.append(e)
        dec_ms.append(dp)
        # recon 链推进：recon′ = clip(recon + (X′ − 512))
        recon = [clip1023(recon[p].astype(np.int32) + xd[p].astype(np.int32)
                          - MID).astype(np.uint16) for p in range(3)]
        chain[k] = recon
    # drift：链上 recon vs 同帧 direct intra 重建
    drift = {}
    for g in DRIFT_AT:
        if g >= n:
            continue
        dmax = max(int(np.max(np.abs(chain[g][p].astype(np.int32)
                                     - direct[g][p].astype(np.int32))))
                   for p in range(3))
        mse = float(np.mean((chain[g][0].astype(np.float64)
                             - direct[g][0].astype(np.float64)) ** 2))
        drift[g] = {"max_abs": dmax,
                    "psnr_y": round(10.0 * np.log10(1023.0 ** 2 / mse), 2)
                    if mse > 0 else 99.0}
    rho = [sp / si for sp, si in zip(s_p, s_i)]
    # 纯 Skip 帧体积下界 = 全中灰伪图（残差恒 0）同 qp 编码
    flat = [np.full((h, w), MID, dtype=np.uint16),
            np.full((h, w // 2), MID, dtype=np.uint16),
            np.full((h, w // 2), MID, dtype=np.uint16)]
    _, s_skip = enc(flat, qp, td / f"{tag}_skip.toos")
    return {
        "s_i": s_i, "s_p": s_p, "rho": [round(r, 4) for r in rho],
        "rho_median": round(float(np.median(rho)), 4),
        "ip2_gain": round(1.0 - (1.0 + float(np.mean(rho))) / 2.0, 4),
        "ip4_gain": round(1.0 - (1.0 + 3.0 * float(np.mean(rho))) / 4.0, 4),
        "skip_floor_bytes": s_skip,
        "p_over_skip_floor": [round(sp / s_skip, 2) for sp in s_p],
        "exact_skip_rate": [round(s, 4) for s in exact_skip],
        "drift": {str(g): drift[g] for g in drift},
        "enc_i_ms_median": round(float(np.median(enc_i_ms)), 1),
        "enc_p_ms_median": round(float(np.median(enc_p_ms)), 1),
        "dec_ms_median": round(float(np.median(dec_ms)), 1),
        "i_bpp": round(float(np.median(s_i)) * 8.0 / (w * h), 4),
    }


# ---------- ±R MV 原型（P1 增益上界；偶数水平 dx）----------

def _sad_maps(c: np.ndarray, r: np.ndarray, m: int, cands) -> np.ndarray:
    """整像素 SAD 图：c/r 全帧 int32，m = MB 对齐内边距像素数（≥ 最大 |MV|）。
    返回 (len(cands), gh, gw)，每 MB 平均绝对差；gh=(H−2m)/MB。"""
    gh = (c.shape[0] - 2 * m) // MB
    gw = (c.shape[1] - 2 * m) // MB
    interior = c[m:m + gh * MB, m:m + gw * MB]
    maps = np.empty((len(cands), gh, gw), dtype=np.float32)
    for i, (dx, dy) in enumerate(cands):
        sr = r[m + dy:m + dy + gh * MB, m + dx:m + dx + gw * MB]
        d = np.abs(interior - sr)
        maps[i] = d.reshape(gh, MB, gw, MB).mean(axis=(1, 3))
    return maps


def mv_probe(layer_frames, qp: int, td: Path, tag: str) -> dict:
    """±4/±8 整像素 SAD（全分辨率向量化），偶数 dx（4:2:2 偶数水平 MV，§3.4）。

    用 MV 重建残差伪图编码 → ρ_mv vs ρ_zmv；R4 = R8 候选集的 |MV|≤4 子集
    argmin。P1 是否值得做、R 取值的依据。"""
    h, w = layer_frames[0][0].shape
    m = 16  # MB 对齐内边距像素数（≥ R=8；MB 索引偏移 = 1）
    out = {"frames": []}
    # 参考链首帧重建（与 p0_chain 同起点同口径）
    enc(layer_frames[0], qp, td / f"{tag}_mvi0.toos")
    _, rec = dec(td / f"{tag}_mvi0.toos", w, h)
    cands8 = [(dx, dy) for dy in range(-8, 9)
              for dx in range(-8, 9) if dx % 2 == 0]
    cands_arr = np.array(cands8, dtype=np.int32)
    idx4 = np.array([i for i, (dx, dy) in enumerate(cands8)
                     if abs(dx) <= 4 and abs(dy) <= 4])
    gh = (h - 2 * m) // MB
    gw = (w - 2 * m) // MB
    by0, bx0 = m // MB, m // MB
    mb_h, mb_w = h // MB, w // MB

    def build_pred(mv_map: np.ndarray) -> list[np.ndarray]:
        """MV 预测图（默认 zero-MV = rec 拷贝，边界/越界 MB 天然回退零运动）；
        色度 dx/2（偶数水平 dx 保证整样本）。"""
        pred = [p.copy() for p in rec]
        for by in range(by0, by0 + gh):
            for bx in range(bx0, bx0 + gw):
                dx, dy = int(mv_map[by, bx, 0]), int(mv_map[by, bx, 1])
                ys, xs = by * MB, bx * MB
                ye0, xe0 = ys + dy, xs + dx
                if ye0 < 0 or xe0 < 0 or ye0 + MB > h or xe0 + MB > w:
                    continue
                pred[0][ys:ys + MB, xs:xs + MB] = rec[0][ye0:ye0 + MB, xe0:xe0 + MB]
                cm = MB // 2
                cys, cxs = ys, xs // 2
                ce0, cu0 = cys + dy, cxs + dx // 2
                for p in (1, 2):
                    plane = rec[p]
                    if ce0 < 0 or cu0 < 0 or ce0 + MB > h or cu0 + cm > plane.shape[1]:
                        continue
                    pred[p][ys:ys + MB, cxs:cxs + cm] = \
                        plane[ce0:ce0 + MB, cu0:cu0 + cm]
        return pred

    for k in (1, 2):
        curr = layer_frames[k]
        # zero-MV 基准
        x0 = [clip1023(curr[p].astype(np.int32) - rec[p].astype(np.int32) + MID)
              .astype(np.uint16) for p in range(3)]
        _, s_zmv = enc(x0, qp, td / f"{tag}_mvz{k}.toos")
        # SAD（Y 平面全分辨率向量化；R8 超集算一次，R4 取子集 argmin）
        maps8 = _sad_maps(curr[0].astype(np.int32), rec[0].astype(np.int32),
                          m, cands8)
        mv_maps: dict[int, np.ndarray] = {}
        for r_ in MV_RS:
            pick = np.argmin(maps8[idx4] if r_ == 4 else maps8, axis=0)
            chosen = (idx4 if r_ == 4 else np.arange(len(cands8)))[pick]
            mv = cands_arr[chosen]                     # (gh, gw, 2)
            full = np.zeros((mb_h, mb_w, 2), dtype=np.int32)
            full[by0:by0 + gh, bx0:bx0 + gw] = mv
            mv_maps[r_] = full
        rec_row: dict = {"frame": k, "s_zmv": int(s_zmv)}
        # 链推进用 R4 残差（与 p0_chain 口径可比）
        for r_ in MV_RS:
            mv_map = mv_maps[r_]
            pred = build_pred(mv_map)
            x_mv = [clip1023(curr[p].astype(np.int32) - pred[p].astype(np.int32)
                             + MID).astype(np.uint16) for p in range(3)]
            _, s_mv = enc(x_mv, qp, td / f"{tag}_mvm{r_}_{k}.toos")
            rec_row[f"s_mv_r{r_}"] = int(s_mv)
            rec_row[f"gain_r{r_}"] = round(1.0 - s_mv / s_zmv, 4)
            rec_row[f"mv_nonzero_rate_r{r_}"] = round(float(np.mean(
                (mv_map[:, :, 0] != 0) | (mv_map[:, :, 1] != 0))), 4)
            rec_row[f"mv_absmax_r{r_}"] = int(np.max(np.abs(mv_map)))
        _, xd = dec(td / f"{tag}_mvm4_{k}.toos", w, h)
        rec = [clip1023(rec[p].astype(np.int32) + xd[p].astype(np.int32) - MID)
               .astype(np.uint16) for p in range(3)]
        out["frames"].append(rec_row)
        print(f"   [mv] f{k}: zmv={s_zmv} mv4={rec_row['s_mv_r4']}"
              f"({rec_row['gain_r4']:+.3f}) mv8={rec_row['s_mv_r8']}"
              f"({rec_row['gain_r8']:+.3f}) "
              f"nonzero4={rec_row['mv_nonzero_rate_r4']:.2f} "
              f"absmax4={rec_row['mv_absmax_r4']}")
    return out


# ---------- 量化死区探针 ----------

def deadzone_probe(qp: int, td: Path) -> dict:
    """uniform 偏移伪图（luma 残差恒 d）：解出的 d′ 给出 DC 死区与符号对称性。"""
    h, w = 2160, 3840
    out = {}
    for d in (2, 1, -1, -2):
        img = [np.full((h, w), MID + d, dtype=np.uint16),
               np.full((h, w // 2), MID, dtype=np.uint16),
               np.full((h, w // 2), MID, dtype=np.uint16)]
        enc(img, qp, td / f"dz{d}.toos")
        _, dec_p = dec(td / f"dz{d}.toos", w, h)
        out[str(d)] = int(dec_p[0][100, 100]) - MID
    return out


# ---------- 主流程 ----------

def main() -> None:
    assert CLI.exists(), f"toos CLI 未构建: {CLI}"
    out = {"date": DATE, "qps": QPS, "mv_rs": MV_RS,
           "pipeline": "pf0_422_bd10_qm0_rans2",
           "p0": "X=clip(curr-recon+512); recon'=clip(recon+(X'-512))",
           "layers": {}}
    for tag, src, start, n in LAYERS:
        print(f"=== layer {tag} ===")
        frames = synth_ui_frames(n) if src is None else extract_layer(src, start, n)
        h, w = frames[0][0].shape
        print(f"  res={w}x{h} frames={len(frames)}")
        lay = {"source": src.name if src else "synthetic",
               "start": start, "res": f"{w}x{h}", "qp_cases": {}}
        for qp in QPS:
            with tempfile.TemporaryDirectory() as tds:
                td = Path(tds)
                case = p0_chain(frames, qp, td, tag)
                if qp == MV_QP and tag in ("pan_2k", "broll_4k", "handheld_4k"):
                    case["mv_probe"] = mv_probe(frames, qp, td, tag)
            lay["qp_cases"][qp] = case
            dr10 = list(case["drift"].values())[-1]
            print(f"  qp{qp}: rho_med={case['rho_median']:.3f} "
                  f"ip2={case['ip2_gain'] * 100:.1f}% ip4={case['ip4_gain'] * 100:.1f}% "
                  f"skip_floor={case['skip_floor_bytes'] / 1e6:.3f}MB "
                  f"I={case['enc_i_ms_median']:.0f}ms P={case['enc_p_ms_median']:.0f}ms "
                  f"bpp={case['i_bpp']:.3f} drift10_max={dr10['max_abs']}")
        out["layers"][tag] = lay
    with tempfile.TemporaryDirectory() as tds:
        out["deadzone"] = {str(qp): deadzone_probe(qp, Path(tds)) for qp in QPS}
    print(f"deadzone: {out['deadzone']}")
    dst = REPO / f"tools/lp_spike_{DATE}.json"
    dst.write_text(json.dumps(out, indent=1, ensure_ascii=False))
    print(f"→ {dst}")


if __name__ == "__main__":
    main()
