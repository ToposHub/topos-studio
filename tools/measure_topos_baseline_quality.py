#!/usr/bin/env python3
"""TOPOS vs 母版客观质量度量（画质对齐计划 §4.2 验收指标）。

对基准语料（tools/generate_topos_baseline_corpus.py 产物）逐档计算：

- Y/U/V PSNR、MAE（原生码值域，与同分辨率同像素格式母版参考锁步解码）；
- SSIM（Y 平面 8-bit 化，Wang et al. 11×11 σ1.5 高斯窗，scipy 实现）；
- 8×8 块边界残差指数 block_index（DCT 块效应探测：边界梯度均值 /
  非边界梯度均值，1.0 = 无块效应；Proxy 档以当前版本为基线持续降低）；
- 抽样帧步长可调（默认 5，含首尾帧）。

**MAE 单位警告（BENCH-0012，2026-09-29）**：``mae_*`` 以档位原生码值域
为单位——10-bit 档（proxy…4444）与 12-bit 档（4444xq）差 ~4×，**跨档直比
无效**。历史质量表中"2K 4444xq MAE≈14.3 异常"即此口径混排：归一化后
xq（norm≈0.0033）优于 4444（≈0.0039），档位单调性成立。跨档比较一律用
``mae_*_norm``（误差/峰值）或 ``mae_*_10bit``（10-bit 等效码）——本脚本
已补这两个字段；实测偏差 ~0.07 code 证明参考链无 gamma/范围缺陷。

参考链：母版 4K_prores4444 →（PyAV 确定性 reformat/scale）→ 各分辨率
各像素格式。TOPOS 侧走 ToposMovieFile + ToposDecoder 原生解码。

输出 JSON（--out）与表格；供 P2 重标定前后对比与 P7 CI 门禁复用。

用法::

    .venv/bin/python tools/measure_topos_baseline_quality.py --tag baseline
    .venv/bin/python tools/measure_topos_baseline_quality.py --tag recal \\
        --stride 10 --only 2K --tiers proxy,lt
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import av
import numpy as np
from scipy.ndimage import gaussian_filter

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from src.shared.codec.topos_binding import (  # noqa: E402
    ToposCodec,
    ToposDecoder,
    ToposMovieFile,
)

MEDIA = REPO / "tests" / "test_videos"
MASTER = MEDIA / "4K_prores4444.mov"
OUT_DIR = REPO / "bench_out" / "topos_quality"

FRAMES = 487
SIZES = {"2K": (1920, 1080), "4K": (3840, 2160)}
TIERS = {
    "proxy": "yuv422p10le",
    "lt": "yuv422p10le",
    "standard": "yuv422p10le",
    "hq": "yuv422p10le",
    "4444": "yuv444p10le",
    "4444xq": "yuv444p12le",
}


def sample_indices(stride: int) -> list:
    idx = sorted(set(range(0, FRAMES, stride)) | {FRAMES - 1})
    return idx


def plane_bits(fmt: str) -> int:
    return 10 if "10le" in fmt else 12


# ————————————————————————— SSIM（Wang et al.） —————————————————————————

def ssim_y(a: np.ndarray, b: np.ndarray) -> float:
    """8-bit Y 平面 SSIM（11×11 σ1.5 高斯统计；输入已归一 [0,255] float）。"""
    a = a.astype(np.float32, copy=False)
    b = b.astype(np.float32, copy=False)
    c1, c2 = (0.01 * 255.0) ** 2, (0.03 * 255.0) ** 2
    mu_a = gaussian_filter(a, 1.5)
    mu_b = gaussian_filter(b, 1.5)
    saa = gaussian_filter(a * a, 1.5) - mu_a * mu_a
    sbb = gaussian_filter(b * b, 1.5) - mu_b * mu_b
    sab = gaussian_filter(a * b, 1.5) - mu_a * mu_b
    num = (2 * mu_a * mu_b + c1) * (2 * sab + c2)
    den = (mu_a ** 2 + mu_b ** 2 + c1) * (saa + sbb + c2)
    return float(np.mean(num / den))


# ————————————————————————— 帧迭代器 —————————————————————————

def _plane_array(plane) -> np.ndarray:
    return np.ascontiguousarray(np.ndarray(
        (plane.height, plane.width), dtype="<u2",
        buffer=memoryview(plane), strides=(plane.line_size, 2)))


def iter_reference(size: str, fmt: str):
    """母版 → (index, planes)。2K 先缩放；格式统一 reformat（确定性）。"""
    w, h = SIZES[size]
    with av.open(str(MASTER)) as c:
        s = c.streams.video[0]
        for i, f in enumerate(c.decode(s)):
            if (f.width, f.height) != (w, h) or f.format.name != fmt:
                f = f.reformat(width=w, height=h, format=fmt)
            yield i, tuple(_plane_array(p) for p in f.planes[:3])


def iter_topos(codec: ToposCodec, path: Path, indices: list[int]):
    """只解码预先冻结的抽样帧，避免把 487 帧都作无用解码。"""
    movie = ToposMovieFile(codec, str(path))
    decoder = ToposDecoder(codec)
    try:
        n = int(movie.info().sample_count)
        views = None
        if n != FRAMES:
            raise ValueError(f"{path.name}: 帧数 {n} != 预期 {FRAMES}")
        for i in indices:
            buf, size = movie.packet_into(i)
            fi = decoder.prepare(buf, size)
            if views is None:
                planes = []
                for k in range(int(fi.plane_count)):
                    pw, ph = codec.plane_geometry(fi, k)
                    planes.append(np.empty((ph, pw), dtype="<u2"))
                views = decoder.make_views(planes)
            decoder.decode_views(buf, views, int(fi.plane_count), size)
            yield i, tuple(np.array(p) for p in planes)
    finally:
        decoder.close()
        movie.close()


# ————————————————————————— 度量累计器 —————————————————————————

class Metric:
    """跨抽样帧累计：PSNR/MAE（全像素聚合）+ SSIM/block_index（帧均值）。"""

    def __init__(self, fmt: str):
        self.bits = plane_bits(fmt)
        self.sse = [0.0, 0.0, 0.0]
        self.sae = [0.0, 0.0, 0.0]
        self.cnt = [0, 0, 0]
        self.ssim_sum = 0.0
        self.block_sum = 0.0
        self.frames = 0

    def add(self, ref: tuple, tp: tuple) -> None:
        for k in range(3):
            r = ref[k].astype(np.float64)
            t = tp[k].astype(np.float64)
            d = t - r
            self.sse[k] += float(np.dot(d.ravel(), d.ravel()))
            self.sae[k] += float(np.abs(d).sum())
            self.cnt[k] += d.size
        peak = float((1 << self.bits) - 1)
        a8 = ref[0].astype(np.float32) * (255.0 / peak)
        b8 = tp[0].astype(np.float32) * (255.0 / peak)
        self.ssim_sum += ssim_y(a8, b8)
        self.block_sum += block_index_y(tp[0])
        self.frames += 1

    def result(self) -> dict:
        out = {"frames": self.frames, "bit_depth": self.bits}
        for k, name in enumerate(("y", "u", "v")):
            mse = self.sse[k] / max(1, self.cnt[k])
            peak = float((1 << self.bits) - 1)
            out[f"psnr_{name}"] = (10.0 * math.log10(peak * peak / mse)
                                   if mse > 0 else float("inf"))
            mae = self.sae[k] / max(1, self.cnt[k])
            out[f"mae_{name}"] = mae
            # BENCH-0012：mae_* 的单位是「档位原生码值」（10-bit 档 0..1023、
            # 12-bit 档 0..4095）——跨档直比无效（2K 4444xq 的 MAE≈14.3 事件
            # 即 12-bit 域数值被误与 10-bit 档并排比较所致）。补归一化字段：
            # mae_*_norm = 误差/峰值（跨 bit 深度可比）；
            # mae_*_10bit = 折算到 10-bit 等效码（与既有 10-bit 档直比）。
            out[f"mae_{name}_norm"] = mae / peak
            out[f"mae_{name}_10bit"] = mae * 1023.0 / peak
        out["ssim_y"] = self.ssim_sum / max(1, self.frames)
        out["block_index_y"] = self.block_sum / max(1, self.frames)
        return out


def block_index_y(y: np.ndarray) -> float:
    """8×8 DCT 块效应指数：块边界列/行梯度均值 ÷ 非边界梯度均值。

    1.0 = 边界与内部梯度无差异（无块效应）；>1 显著块边界。量化系数
    归零产生的高频丢失在边界表现为连续 8 像素网格的阶跃。
    """
    f = y.astype(np.float64)
    dx = np.abs(np.diff(f, axis=1))          # (h, w-1)：col x-1→x 差
    dy = np.abs(np.diff(f, axis=0))          # (h-1, w)
    # 8 像素网格边界：x ≡ 0 (mod 8) → dx 列索引 x-1 ≡ 7 (mod 8)
    b_cols = dx[:, 7::8]
    b_rows = dy[7::8, :]
    inner_cols = (dx.sum() - b_cols.sum()) / (dx.size - b_cols.size)
    inner_rows = (dy.sum() - b_rows.sum()) / (dy.size - b_rows.size)
    ratio_v = b_cols.mean() / max(1e-9, inner_cols)
    ratio_h = b_rows.mean() / max(1e-9, inner_rows)
    return float(0.5 * (ratio_v + ratio_h))


# ————————————————————————— 主流程 —————————————————————————

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--tag", required=True, help="结果标签（baseline/recal/...）")
    parser.add_argument("--stride", type=int, default=5,
                        help="抽样帧步长（默认 5；含首尾帧）")
    parser.add_argument("--only", action="append", default=[],
                        choices=sorted(SIZES))
    parser.add_argument("--tiers", default="", help="逗号分隔档位子集")
    args = parser.parse_args()

    sizes = list(args.only) or list(SIZES)
    wanted = set(args.tiers.split(",")) if args.tiers else None
    manifest = json.loads((MEDIA / "topos_prores_baseline_manifest.json").read_text())
    naming = manifest["topos_naming"]
    sampled = sample_indices(max(1, args.stride))
    sample_set = set(sampled)
    print(f"度量 {len(sampled)} 抽样帧（步长 {args.stride}）", flush=True)

    codec = ToposCodec()
    results = {}
    t0 = time.perf_counter()
    for size in sizes:
        groups: dict = {}
        for tier, fmt in TIERS.items():
            if wanted and tier not in wanted:
                continue
            path = MEDIA / f"{size}_topos_{naming[tier]}.mov"
            if not path.is_file():
                raise FileNotFoundError(f"质量基准素材缺失: {path}")
            groups.setdefault(fmt, []).append((tier, path))
        for fmt, tiers in groups.items():
            metrics = {tier: Metric(fmt) for tier, _ in tiers}
            streams = {tier: iter_topos(codec, path, sampled)
                       for tier, path in tiers}
            for i, ref in iter_reference(size, fmt):
                if i not in sample_set:
                    continue
                for tier, _ in tiers:
                    _, tp = next(streams[tier])
                    metrics[tier].add(ref, tp)
            for tier, _ in tiers:
                r = metrics[tier].result()
                results[f"{size}/{tier}"] = r
                print(
                    f"  {size} {tier:8} PSNR Y/U/V = "
                    f"{r['psnr_y']:6.2f}/{r['psnr_u']:6.2f}/{r['psnr_v']:6.2f} dB  "
                    f"SSIM={r['ssim_y']:.4f}  block={r['block_index_y']:.3f}  "
                    f"MAE_Y={r['mae_y']:.2f}", flush=True)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = OUT_DIR / f"quality_{args.tag}.json"
    payload = {
        "tag": args.tag,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "stride": args.stride,
        "frames_sampled": len(sampled),
        "master": str(MASTER.relative_to(REPO)),
        "elapsed_s": round(time.perf_counter() - t0, 1),
        "results": results,
        # N05：产物绑定——报告数值只对「这个 commit + 这块库 + 这些输入」
        # 有效；verify_topos_quality_gates 据此拒绝为变化后的 codec 放行。
        "provenance": _provenance(codec),
    }
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    print(f"\n结果：{out_path}（{payload['elapsed_s']}s）", flush=True)
    return 0


def _provenance(codec: ToposCodec) -> dict:
    """度量产物绑定：commit / 库 SHA / 输入完整指纹 / 生成命令。"""
    import hashlib as _hl
    import subprocess as _sp

    prov: dict = {"command": " ".join(sys.argv)}
    prov["measurement_script_sha256"] = _hl.sha256(
        Path(__file__).read_bytes()).hexdigest()
    try:
        prov["git_commit"] = _sp.run(
            ["git", "rev-parse", "HEAD"], cwd=REPO, capture_output=True,
            text=True, check=True).stdout.strip()
        dirty = _sp.run(
            ["git", "status", "--porcelain"], cwd=REPO, capture_output=True,
            text=True, check=True).stdout.strip()
        prov["git_dirty_files"] = len(dirty.splitlines())
    except Exception:  # noqa: BLE001 — git 不可用时如实记录
        prov["git_commit"] = "unknown"
    # 与上方真正解码的同一实例绑定；显式 TOPOS_CODEC_LIB 不得被候选路径覆盖。
    lib = Path(codec._lib._name).resolve(strict=True)
    digest = _hl.sha256()
    with lib.open("rb") as source:
        for chunk in iter(lambda: source.read(1 << 20), b""):
            digest.update(chunk)
    version = codec.version()
    prov["dylib_sha256"] = digest.hexdigest()
    prov["dylib_path"] = str(lib)
    prov["dylib_abi_version"] = codec.abi_version
    prov["dylib_git_commit"] = version.git_commit
    prov["dylib_build_target"] = version.build_target
    try:
        if MASTER.is_file():
            prov["master_sha256"] = _hl.sha256(MASTER.read_bytes()).hexdigest()
    except Exception:  # noqa: BLE001
        pass
    return prov


if __name__ == "__main__":
    raise SystemExit(main())
