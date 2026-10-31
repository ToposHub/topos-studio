#!/usr/bin/env python3
"""toos_tools.py —— .toos ↔ PNG 互转适配器（Topos Image 阶段 4 交付物）。

定位：CLI/适配器层。核心库（libtopos_codec）保持第三方图像库零依赖；
PNG 读写经由 Pillow/numpy 在适配层完成，属于显式转换（每次调用都会
报告位深缩放与 Alpha 关联，绝不静默转换）。

依赖：toos CLI（native/topos_codec/build/<cfg>/toos）+ numpy + opencv-python
（cv2 负责 16-bit PNG 读写；Pillow 不支持 16-bit RGB PNG）。

用法：
  # .toos → PNG（16-bit PNG；10/12-bit 左移对齐到 16-bit 容器，显式无损缩放）
  python scripts/toos_tools.py decode image.toos -o image.png

  # PNG → .toos（16-bit PNG → 10-bit GBR Image HQ；右移舍入，报告精度损失）
  python scripts/toos_tools.py encode image.png -o image.toos \\
      [--bit-depth 10|12] [--image-profile 1|2] [--qp 28] [--overwrite]

通道序（ADR-C017 C-130）：GBR 平面序 = G,B,R；PNG 通道序 = R,G,B(A)。
本适配器负责两侧互换，不改变文件语义。
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import cv2
import numpy as np

REPO = Path(__file__).resolve().parents[1]
DEFAULT_TPIC = REPO / "native/topos_codec/build/debug/toos"

CHANNELS_BY_PF = {0: ["Y", "U", "V"], 1: ["Y", "U", "V"], 2: ["G", "B", "R"]}


def _find_toos(explicit: str | None) -> Path:
    if explicit:
        p = Path(explicit)
        if not p.exists():
            raise SystemExit(f"toos CLI 不存在: {p}")
        return p
    if DEFAULT_TPIC.exists():
        return DEFAULT_TPIC
    for cfg in ("release", "debug"):
        p = REPO / f"native/topos_codec/build/{cfg}/toos"
        if p.exists():
            return p
    raise SystemExit("未找到 toos CLI；先构建 native/topos_codec 或用 --toos 指定路径")


def _run(cmd: list[str]) -> None:
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        sys.stderr.write(proc.stdout)
        sys.stderr.write(proc.stderr)
        raise SystemExit(f"命令失败: {' '.join(cmd)}")


def cmd_decode(args: argparse.Namespace) -> int:
    toos = _find_toos(args.toos)
    with tempfile.TemporaryDirectory() as td:
        raw = Path(td) / "planes.raw"
        _run([str(toos), "decode", str(args.input), "-o", str(raw)])
        buf = np.fromfile(raw, dtype="<u2")
        side = json.loads(Path(str(raw) + ".json").read_text())
    width, height = int(side["width"]), int(side["height"])
    bit_depth = int(side["bit_depth"])
    pixel_format = int(side["pixel_format"])
    # E10：HALF 样本域（sample_kind=1，spec §15）显式拒绝——此前 HALF 文件
    # 被当整数 u16 直接出 PNG（half 位型当码值 → 静默错误像素）。适配器
    # 只覆盖整数 GBR 族；浮点文件请用绑定层 decode_float()。
    sample_kind = int(side.get("sample_kind", 0) or 0)
    if sample_kind == 1:
        raise SystemExit(
            "HALF（浮点）.toos 不被本适配器支持（half 位型按整数码值出 PNG "
            "= 错误像素）。请用 Python 绑定 decode_float() 解码浮点平面。")
    # YUV → PNG 适配器不做色彩矩阵转换（显式拒绝，绝不静默猜 sRGB）。
    # 必须在平面切片前拒绝：YUV422 色度平面半宽，全分辨率切片假设不成立。
    if pixel_format != 2:
        raise SystemExit(
            "YUV (Preview) 转 PNG 需要显式色彩矩阵选择；为避免静默色彩错误，"
            "本适配器只支持 GBR (Image HQ/XQ) 文件。请先在上层完成 YUV→RGB。")
    has_alpha = str(side["channels"]).endswith(",A")
    names = [c for c in str(side["channels"]).split(",") if c and c != "A"]
    if has_alpha:
        names.append("A")
    per = width * height
    planes = [buf[i * per:(i + 1) * per].reshape(height, width) for i in range(len(names))]

    # 通道名（GBR 平面序 G,B,R）→ RGB(A)
    r, g, b = planes[2], planes[0], planes[1]

    # 刻度契约（topos_codec.h 输入/输出约定）：彩色平面 = 声明位深满刻度
    # （0..2^bd-1），alpha 平面恒为 16-bit 满刻度容器（0..65535）。
    # → 16-bit PNG：彩色左移对齐（无损）；alpha 恒等直通。
    shift = 16 - bit_depth
    color = [p << shift for p in (r, g, b)]
    alpha = planes[3] if has_alpha else None
    rgba = color + ([alpha] if alpha is not None else [])
    out = np.stack(rgba, axis=-1)
    # cv2 写 16-bit PNG（BGR 通道序，RGB→BGR 显式交换）
    if out.shape[-1] == 4:
        bgr = out[..., [2, 1, 0, 3]]
    else:
        bgr = out[..., [2, 1, 0]]
    if not cv2.imwrite(str(args.output), bgr):
        raise SystemExit(f"PNG 写入失败: {args.output}")
    alpha_note = ", alpha 直通 (A16 容器=16-bit PNG)" if has_alpha else ""
    print(f"{args.input}: {width}x{height} {bit_depth}-bit {names} → "
          f"{args.output} (16-bit PNG, 彩色 <<{shift} 无损对齐{alpha_note})")
    return 0


def cmd_encode(args: argparse.Namespace) -> int:
    toos = _find_toos(args.toos)
    img = cv2.imread(str(args.input), cv2.IMREAD_UNCHANGED)
    if img is None:
        raise SystemExit(f"PNG 读取失败: {args.input}")
    if img.ndim == 2 or img.shape[-1] < 3:
        raise SystemExit("仅支持 RGB/RGBA PNG")
    if img.dtype == np.uint8:
        src_bits = 8
        img16 = img.astype(np.uint16) << 8  # 8-bit 值域 0..255 → 精确 16-bit 值（无损）
    elif img.dtype == np.uint16:
        src_bits = 16
        img16 = img
    else:
        raise SystemExit(f"不支持的 PNG 位深: {img.dtype}")
    if img16.shape[-1] == 4:
        r, g, b = img16[..., 2], img16[..., 1], img16[..., 0]
        alpha = img16[..., 3]
        alpha_mode = 1  # straight（PNG 无 premultiplied 语义）
    else:
        r, g, b = img16[..., 2], img16[..., 1], img16[..., 0]
        alpha = None
        alpha_mode = 0
    # 刻度契约（topos_codec.h 输入约定，与 decode 输出对称）：
    #   彩色平面 = 声明位深满刻度（0..2^bd-1）；alpha 平面恒为 0..65535（A16 容器）。
    # 彩色：16-bit 精确值 → 目标位深（四舍五入 + 饱和到 2^bd-1）；
    # alpha：恒等直通（PNG 16-bit alpha 即 A16 容器语义）。
    bd = args.bit_depth
    shift = 16 - bd
    half = 1 << (shift - 1)
    max_v = (1 << bd) - 1

    def to_color(p: np.ndarray) -> np.ndarray:
        q = (p.astype(np.uint32) + half) >> shift
        return np.minimum(q, max_v).astype(np.uint16)

    gbr = [to_color(g), to_color(b), to_color(r)]  # 平面序 G,B,R
    planes = gbr + ([alpha] if alpha is not None else [])
    height, width = img.shape[:2]
    raw = np.concatenate([p.reshape(-1) for p in planes]).astype("<u2")

    profile = args.image_profile
    if profile is None:
        profile = 2 if bd == 12 else 1
    with tempfile.TemporaryDirectory() as td:
        raw_path = Path(td) / "planes.raw"
        raw.tofile(raw_path)
        out = Path(args.output).resolve()
        if out.exists() and not args.overwrite:
            raise SystemExit(f"{out} 已存在（用 --overwrite）")
        cmd = [str(toos), "encode", str(raw_path), "-o", str(out),
               "--width", str(width), "--height", str(height),
               "--pixel-format", "2", "--bit-depth", str(bd),
               "--alpha-mode", str(alpha_mode)]
        if alpha_mode != 0:
            cmd += ["--alpha-bit-depth", "16"]  # 仅带 Alpha 时有效（codec 校验规则）
        cmd += ["--image-profile", str(profile), "--qp", str(args.qp), "--overwrite"]
        _run(cmd)
    src_loss = max(0, src_bits - bd)
    alpha_note = "Alpha=straight A16 直通" if alpha is not None else "无 Alpha"
    print(f"{args.input}: {width}x{height} {src_bits}-bit → {bd}-bit GBR "
          f"(彩色精度损失: 低 {src_loss} bit 舍入, {alpha_note}) → {out}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=".toos ↔ PNG 适配器（显式转换）")
    ap.add_argument("--toos", help="toos CLI 路径（默认自动探测）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("decode", help=".toos → PNG")
    d.add_argument("input")
    d.add_argument("-o", "--output", required=True)
    d.set_defaults(func=cmd_decode)

    e = sub.add_parser("encode", help="PNG → .toos")
    e.add_argument("input")
    e.add_argument("-o", "--output", required=True)
    e.add_argument("--bit-depth", type=int, default=10, choices=(10, 12))
    e.add_argument("--image-profile", type=int, default=None, choices=(1, 2))
    e.add_argument("--qp", type=int, default=28)
    e.add_argument("--overwrite", action="store_true")
    e.set_defaults(func=cmd_encode)

    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
