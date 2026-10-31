"""L01 确定性夹具生成器。

全部夹具用**纯算术**生成（整数格点散列 + 解析式），不使用随机流——
跨 numpy 版本/跨机器比特级确定。每个夹具的 canonical float32 C-order
.npy 字节 SHA-256 记入 fixture_manifest.json；测试重新生成比对 hash
防生成器漂移（specs/spec_schema.md §5）。

许可：全部夹具为程序生成（CC0），无第三方素材。

用法：
    .venv/bin/python -m content_staging.library_v1.fixtures.generate_fixtures --write
    .venv/bin/python -m content_staging.library_v1.fixtures.generate_fixtures --check
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
from pathlib import Path
from typing import Callable, Dict, List

import numpy as np

_GENERATED_DIR = Path(__file__).resolve().parent / "generated"
_MANIFEST_PATH = Path(__file__).resolve().parent / "fixture_manifest.json"

_U64 = np.uint64


def _lattice_hash(ix: np.ndarray, iy: np.ndarray, seed: int = 0) -> np.ndarray:
    """整数格点 → [0,1) 均匀值。murmur3 风格乘法散列，uint64 全确定。"""
    x = ix.astype(np.uint32).astype(_U64)
    y = iy.astype(np.uint32).astype(_U64)
    h = (x * _U64(0x9E3779B185EBCA87)) ^ (y * _U64(0xC2B2AE3D27D4EB4F)) ^ (_U64(seed) * _U64(0x165667B19E3779F9))
    h ^= h >> _U64(31)
    h = h * _U64(0x85EBCA77C2B2AE63)
    h ^= h >> _U64(29)
    h = h * _U64(0xC2B2AE3D27D4EB4F)
    h ^= h >> _U64(32)
    return h.astype(np.float64) / 18446744073709551616.0


def _value_noise(height: int, width: int, cell: int, seed: int) -> np.ndarray:
    """双线性插值 value noise（格点散列），输出 [0,1) (H,W) float64。"""
    gh, gw = height // cell + 2, width // cell + 2
    gy, gx = np.meshgrid(np.arange(gh), np.arange(gw), indexing="ij")
    grid = _lattice_hash(gx, gy, seed)
    out = np.zeros((height, width), dtype=np.float64)
    for j in range(height):
        y0 = j // cell
        fy = (j % cell) / cell
        for i in range(width):
            x0 = i // cell
            fx = (i % cell) / cell
            v00 = grid[y0, x0]
            v10 = grid[y0, x0 + 1]
            v01 = grid[y0 + 1, x0]
            v11 = grid[y0 + 1, x0 + 1]
            out[j, i] = (v00 * (1 - fx) * (1 - fy) + v10 * fx * (1 - fy)
                         + v01 * (1 - fx) * fy + v11 * fx * fy)
    return out


# ──────────────────────────────────────────────────────────────────────
# 夹具构造（全部 float32、scene-linear、straight alpha）
# ──────────────────────────────────────────────────────────────────────

def gray_ramp_linear() -> np.ndarray:
    """64×32 线性灰梯度（含 0 与 1 端点），亮度映射类验收基材。"""
    x = np.linspace(0.0, 1.0, 64, dtype=np.float64)
    img = np.tile(x, (32, 1))
    return np.repeat(img[:, :, None], 3, axis=2).astype(np.float32)


def hdr_probes() -> np.ndarray:
    """9×1 数值探针：§9.1 覆盖 -0.25/0/0.18/1/4.5/12 与高饱和色。"""
    rows = [
        (-0.25, -0.25, -0.25),
        (0.0, 0.0, 0.0),
        (0.18, 0.18, 0.18),
        (1.0, 1.0, 1.0),
        (4.5, 4.5, 4.5),
        (12.0, 12.0, 12.0),
        (1.0, 0.0, 0.0),        # 纯红
        (0.0, 1.0, 1.0),        # 饱和青
        (12.0, 0.1, 4.5),       # HDR 混合色
    ]
    return np.asarray(rows, dtype=np.float32)


def color_patches() -> np.ndarray:
    """64×64 色块：肤色簇×5 + 原色×3 + 次色×3 + 中性×5（8×8 块）。"""
    patches = [
        (0.28, 0.20, 0.16), (0.42, 0.30, 0.24), (0.55, 0.41, 0.33),
        (0.65, 0.50, 0.40), (0.78, 0.62, 0.50),                # 肤色簇
        (1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0),     # 原色
        (1.0, 1.0, 0.0), (0.0, 1.0, 0.0), (1.0, 0.0, 1.0),     # 次色
        (0.0, 0.0, 0.0), (0.18, 0.18, 0.18), (0.5, 0.5, 0.5),
        (1.0, 1.0, 1.0), (0.9, 0.4, 0.1),                      # 中性+暖橙
    ]
    img = np.zeros((64, 64, 3), dtype=np.float64)
    for idx, color in enumerate(patches):
        by, bx = divmod(idx, 8)
        img[by * 8:(by + 1) * 8, bx * 8:(bx + 1) * 8, :] = color
    return img.astype(np.float32)


def hdr_point_light() -> np.ndarray:
    """96×48 暗场 + 3 个高斯点光（峰值 4.5 中性 / 12 中性 / 4.5 红）。"""
    h, w = 48, 96
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float64)
    img = np.full((h, w, 3), 0.01, dtype=np.float64)

    def spot(cx: float, cy: float, peak: float, sigma: float, color: tuple) -> None:
        g = peak * np.exp(-(((xx - cx) ** 2 + (yy - cy) ** 2) / (2.0 * sigma * sigma)))
        img[:, :, 0] += g * color[0]
        img[:, :, 1] += g * color[1]
        img[:, :, 2] += g * color[2]

    spot(20.0, 24.0, 4.5, 3.0, (1.0, 1.0, 1.0))
    spot(68.0, 30.0, 12.0, 2.0, (1.0, 1.0, 1.0))
    spot(44.0, 10.0, 4.5, 2.5, (1.0, 0.3, 0.2))
    return img.astype(np.float32)


def alpha_fringe_hair() -> np.ndarray:
    """64×64 RGBA straight：左半不透明肤色 + 6px 渐隐彩边 + 3 根 1px 发丝。"""
    h, w = 64, 64
    img = np.zeros((h, w, 4), dtype=np.float64)
    color = np.array([0.60, 0.50, 0.40])
    img[:, :32, :3] = color
    img[:, :32, 3] = 1.0
    for k in range(6):                      # 32..37 列：alpha 1→0，RGB 保持（straight）
        a = 1.0 - (k + 0.5) / 6.0
        img[:, 32 + k, :3] = color
        img[:, 32 + k, 3] = a
    for strand, base_x in enumerate((44, 50, 56)):  # 发丝：1px 曲线，部分 alpha
        for y in range(4, 60):
            x = base_x + int(round(3.0 * np.sin(y / 9.0 + strand)))
            a = 0.5 + 0.3 * _lattice_hash(np.array([y]), np.array([strand]), 3)[0]
            img[y, x, :3] = (0.16, 0.11, 0.08)
            img[y, x, 3] = a
    return img.astype(np.float32)


def uv_grid() -> np.ndarray:
    """64×64 UV 网格：R=x/63、G=y/63、B=网格线，四角 4×4 白色角标。"""
    h, w = 64, 64
    img = np.zeros((h, w, 3), dtype=np.float64)
    img[:, :, 0] = np.tile(np.arange(w) / 63.0, (h, 1))
    img[:, :, 1] = np.tile((np.arange(h) / 63.0)[:, None], (1, w))
    img[::8, :, 2] = 1.0
    img[:, ::8, 2] = 1.0
    for cy, cx in ((0, 0), (0, w - 4), (h - 4, 0), (h - 4, w - 4)):
        img[cy:cy + 4, cx:cx + 4, :] = 1.0
    return img.astype(np.float32)


def short_seq_8() -> np.ndarray:
    """8 帧确定性移动边缘序列 (8,32,16,3)：左 0.18 灰 / 右 0.72 灰。"""
    frames = np.zeros((8, 32, 16, 3), dtype=np.float64)
    for i in range(8):
        edge = 2 + int(round(12.0 * i / 7.0))
        frames[i, :, :edge, :] = 0.18
        frames[i, :, edge:, :] = 0.72
    return frames.astype(np.float32)


def paper_tex_64() -> np.ndarray:
    """64×64 程序纸纹（cell=8 value noise），排线/水彩类底纹，CC0。"""
    return _value_noise(64, 64, 8, seed=11).astype(np.float32)


def rank_field_64() -> np.ndarray:
    """64×64 均匀 rank field（逐像素格点散列 seed=7），X06/X18 揭示顺序。"""
    yy, xx = np.meshgrid(np.arange(64), np.arange(64), indexing="ij")
    return _lattice_hash(xx, yy, 7).astype(np.float32)


# ──────────────────────────────────────────────────────────────────────
# manifest 与校验
# ──────────────────────────────────────────────────────────────────────

_BUILDERS: Dict[str, Callable[[], np.ndarray]] = {
    "gray_ramp_linear": gray_ramp_linear,
    "hdr_probes": hdr_probes,
    "color_patches": color_patches,
    "hdr_point_light": hdr_point_light,
    "alpha_fringe_hair": alpha_fringe_hair,
    "uv_grid": uv_grid,
    "short_seq_8": short_seq_8,
    "paper_tex_64": paper_tex_64,
    "rank_field_64": rank_field_64,
}

_PURPOSE: Dict[str, str] = {
    "gray_ramp_linear": "亮度映射类（F16/F17/F04…）",
    "hdr_probes": "HDR 数值覆盖（§9.1 共同验收门）",
    "color_patches": "肤色保护/分区/饱和类（F03/F04…）",
    "hdr_point_light": "halation/starburst/glow 类（F06/F10/C20）",
    "alpha_fringe_hair": "alpha/边缘类（C17/C03、straight/premult 验收）",
    "uv_grid": "UV 重采样类（X09/X12–X17、C12/C13）",
    "short_seq_8": "确定性短序列（转场 A/B 双源对）",
    "paper_tex_64": "纸纹/排线底纹（F14/F15，程序生成 CC0）",
    "rank_field_64": "rank-field 揭示顺序（X06/X18）",
}


def canonical_bytes(arr: np.ndarray) -> bytes:
    """canonical 序列化：float32 C-order .npy 字节（allow_pickle=False）。"""
    arr = np.ascontiguousarray(arr, dtype=np.float32)
    buf = io.BytesIO()
    np.lib.format.write_array(buf, arr, allow_pickle=False)
    return buf.getvalue()


def sha256_of(arr: np.ndarray) -> str:
    return hashlib.sha256(canonical_bytes(arr)).hexdigest()


def build_manifest() -> List[Dict[str, object]]:
    entries = []
    for name, builder in _BUILDERS.items():
        arr = builder()
        entries.append({
            "name": name,
            "shape": list(arr.shape),
            "dtype": "float32",
            "sha256": sha256_of(arr),
            "purpose": _PURPOSE[name],
            "license": "CC0（程序生成，无第三方素材）",
        })
    return entries


def write() -> Path:
    _GENERATED_DIR.mkdir(parents=True, exist_ok=True)
    entries = build_manifest()
    for entry in entries:
        arr = _BUILDERS[entry["name"]]()
        np.save(_GENERATED_DIR / f"{entry['name']}.npy", arr, allow_pickle=False)
    _MANIFEST_PATH.write_text(
        json.dumps({"fixtures": entries}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8")
    return _GENERATED_DIR


def check() -> List[str]:
    """重新生成并比对 manifest hash 与磁盘文件；返回不一致清单（空=通过）。"""
    problems: List[str] = []
    manifest = json.loads(_MANIFEST_PATH.read_text(encoding="utf-8"))["fixtures"]
    on_disk = {p.stem: p for p in _GENERATED_DIR.glob("*.npy")} if _GENERATED_DIR.exists() else {}
    if len(manifest) != len(_BUILDERS):
        problems.append("manifest 条目数与生成器数量不一致")
    for entry in manifest:
        name = entry["name"]
        if name not in _BUILDERS:
            problems.append(f"{name}: 不在生成器集合中")
            continue
        arr = _BUILDERS[name]()
        digest = sha256_of(arr)
        if digest != entry["sha256"]:
            problems.append(f"{name}: 生成器 hash 与 manifest 不一致（生成器漂移）")
        if name not in on_disk:
            problems.append(f"{name}: 磁盘缺 {name}.npy")
        else:
            disk_bytes = on_disk[name].read_bytes()
            if hashlib.sha256(disk_bytes).hexdigest() != entry["sha256"]:
                problems.append(f"{name}: 磁盘文件 hash 与 manifest 不一致")
    for stem in on_disk:
        if stem not in _BUILDERS:
            problems.append(f"{stem}: 磁盘存在未登记的 .npy")
    return problems


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--write", action="store_true", help="生成夹具与 manifest")
    group.add_argument("--check", action="store_true", help="校验再生一致性")
    args = parser.parse_args()
    if args.write:
        out = write()
        print(f"written: {out}")
        return 0
    problems = check()
    for p in problems:
        print(f"MISMATCH: {p}")
    print("OK" if not problems else f"{len(problems)} problem(s)")
    return 0 if not problems else 1


if __name__ == "__main__":
    raise SystemExit(main())
