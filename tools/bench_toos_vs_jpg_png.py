#!/usr/bin/env python3
"""图片编码三方对照：topos(.toos) vs JPEG vs PNG。

回答四个主张（2026-09-12）：
  1) topos 画质优于 JPG、介于 JPG 与 PNG 之间；
  2) 422-ultra 画质大致持平 PNG（PNG=数学无损，此处口径 = 视觉无损/PSNR 量级）；
  3) 默认 10-bit，后期调色空间充足；
  4) 编码/解码速度碾压 PNG。

口径：
- 参考域：RGB8（prores 4K 帧 → YUV→RGB 8-bit）。PNG 无损（PSNR=∞ 仅记尺寸/速度）；
  JPEG q85/q90（默认 4:2:0）、q95+4:4:4；topos 四档位（RGB→YUV422 10-bit 全
  流程往返，含色度二次采样代价——用户从 RGB 源存 .toos 的真实所得）。
- PSNR 在 RGB8 域计算（MSE/255²）。
- 10-bit 主张：prores 10-bit 源 → topos 往返 → 10-bit 域 PSNR（Y/UV 分列），
  对照 8-bit 量化的理论上限（10→8→10 往返 PSNR ≈ 48-49dB 封顶）。
- 速度：PIL（libjpeg/zlib compress_level=6 默认口径）vs topos 生产绑定路径。

输出 tools/toos_vs_jpg_png_2026-09-12.json。
"""
from __future__ import annotations

import ctypes
import io
import json
import statistics
import sys
import time
from pathlib import Path

import numpy as np
from PIL import Image

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

import av  # noqa: E402

from src.shared.codec.topos_binding import (  # noqa: E402
    TOPOS_CODEC_ABI_VERSION, ToposCodec, _CFrameConfig,
)
from src.shared.codec.topos_image_binding import get_image_codec  # noqa: E402

SOURCES = [
    ("prores422HQ", REPO / "tests/test_videos/4K_prores422HQ.mov"),
    ("dnxhrhqx", REPO / "tests/test_videos/4K_DNxHR HQX 10bit.mov"),
]
FRAME_IDS = [10, 11]
SPECS = [("2K", 1920, 1080), ("4K", None, None)]
TIERS = [("422-low", 3, 63), ("422-medium", 1, 61),
         ("422-high", 1, 58), ("422-ultra", 0, 58)]

# BT.601 全范围（JPEG 同域），numpy 实现
def _rgb_to_yuv422_8bit(rgb: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """RGB8 → (Y8 全分辨率, U8/V8 水平 2×1 面积平均)，BT.601 全范围。"""
    r = rgb[:, :, 0].astype(np.float64)
    g = rgb[:, :, 1].astype(np.float64)
    b = rgb[:, :, 2].astype(np.float64)
    y = 0.299 * r + 0.587 * g + 0.114 * b
    r2 = rgb.shape[1] // 2
    u = (-0.169 * r[:, 0::2][:, :r2] - 0.331 * g[:, 0::2][:, :r2]
         + 0.5 * b[:, 0::2][:, :r2] + 128.0
         + (-0.169 * r[:, 1::2][:, :r2] - 0.331 * g[:, 1::2][:, :r2]
            + 0.5 * b[:, 1::2][:, :r2] + 128.0)) / 2.0
    v = (0.5 * r[:, 0::2][:, :r2] - 0.419 * g[:, 0::2][:, :r2]
         - 0.081 * b[:, 0::2][:, :r2] + 128.0
         + (0.5 * r[:, 1::2][:, :r2] - 0.419 * g[:, 1::2][:, :r2]
            - 0.081 * b[:, 1::2][:, :r2] + 128.0)) / 2.0
    y8 = np.clip(np.rint(y), 0, 255).astype(np.uint8)
    u8 = np.clip(np.rint(u), 0, 255).astype(np.uint8)
    v8 = np.clip(np.rint(v), 0, 255).astype(np.uint8)
    return y8, u8, v8


def _yuv422_to_rgb_8bit(y8: np.ndarray, u8: np.ndarray, v8: np.ndarray) -> np.ndarray:
    h, w = y8.shape
    u = cv2_resize_cols(u8.astype(np.float64), w)
    v = cv2_resize_cols(v8.astype(np.float64), w)
    y = y8.astype(np.float64)
    r = y + 1.402 * (v - 128.0)
    g = y - 0.344136 * (u - 128.0) - 0.714136 * (v - 128.0)
    b = y + 1.772 * (u - 128.0)
    out = np.stack([np.clip(np.rint(r), 0, 255),
                    np.clip(np.rint(g), 0, 255),
                    np.clip(np.rint(b), 0, 255)], axis=-1).astype(np.uint8)
    return out


def cv2_resize_cols(plane: np.ndarray, w: int) -> np.ndarray:
    """色度水平 2× 最近邻上采（与编码端 2×1 抽取对称；基准口径用）。"""
    up = np.repeat(plane, 2, axis=1)
    return up[:, :w]


def _psnr(a: np.ndarray, b: np.ndarray) -> float:
    d = a.astype(np.float64) - b.astype(np.float64)
    mse = float(np.mean(d * d))
    if mse == 0.0:
        return float("inf")
    return 10.0 * np.log10(255.0 * 255.0 / mse)


def _frame_planes(frame) -> list[np.ndarray]:
    pls = []
    for p in frame.planes:
        arr = np.frombuffer(ctypes.string_at(p.buffer_ptr, p.buffer_size),
                            dtype="<u2")
        stride = p.line_size // 2
        pls.append(arr.reshape(p.height, stride)[:, :p.width].copy())
    return pls


def _make_fc(w: int, h: int, qp: int, qm: int) -> _CFrameConfig:
    fc = _CFrameConfig()
    fc.struct_size = ctypes.sizeof(_CFrameConfig)
    fc.abi_version = TOPOS_CODEC_ABI_VERSION
    fc.visible_width = w
    fc.visible_height = h
    fc.profile = 3
    fc.pixel_format = 0
    fc.bit_depth = 10
    fc.qp_base = qp
    fc.qmatrix_id = qm
    fc.reserved[0] = 8
    fc.reserved[1] = 1
    return fc


def main() -> int:
    codec = ToposCodec()
    ic = get_image_codec()
    recs = []
    tmp = Path("/tmp/toos_cmp")
    tmp.mkdir(exist_ok=True)

    for name, path in SOURCES:
        with av.open(str(path)) as cont:
            frames = []
            for i, frame in enumerate(cont.decode(video=0)):
                if i in FRAME_IDS:
                    frames.append(frame)
            assert len(frames) == len(FRAME_IDS)

        for spec, rw, rh in SPECS:
            for fid, f in zip(FRAME_IDS, frames):
                pls10 = _frame_planes(
                    f.reformat(width=rw, height=rh, format="yuv422p10le")
                    if rw else f.reformat(format="yuv422p10le"))
                h, w = pls10[0].shape
                # RGB8 参考（10-bit YUV → 8-bit → RGB）
                y8 = (pls10[0] >> 2).astype(np.uint8)
                u8 = (pls10[1] >> 2).astype(np.uint8)
                v8 = (pls10[2] >> 2).astype(np.uint8)
                ref = _yuv422_to_rgb_8bit(y8, u8, v8)
                ref_img = Image.fromarray(ref, "RGB")

                # —— topos 四档位（RGB → YUV422 10-bit → .toos → 往返） ——
                y10, u10, v10 = _rgb_to_yuv422_8bit(ref)
                data = [np.multiply(y10, 4, dtype="<u2").tobytes(),
                        np.multiply(u10, 4, dtype="<u2").tobytes(),
                        np.multiply(v10, 4, dtype="<u2").tobytes()]
                for tier, qm, qp in TIERS:
                    t0 = time.perf_counter()
                    pkt, _ = codec.encode_frame(_make_fc(w, h, qp, qm), data)
                    t_enc = (time.perf_counter() - t0) * 1e3
                    toos = tmp / f"{name}_{spec}_{fid}_{tier}.toos"
                    ic.write_packet(str(toos), pkt, 0, durable=False)
                    t0 = time.perf_counter()
                    gpls, _, _ = ic.decode(str(toos))
                    t_dec = (time.perf_counter() - t0) * 1e3
                    gy = (np.asarray(gpls[0], dtype="<u2").reshape(h, w) // 4).astype(np.uint8)
                    gu = (np.asarray(gpls[1], dtype="<u2").reshape(h, w // 2) // 4).astype(np.uint8)
                    gv = (np.asarray(gpls[2], dtype="<u2").reshape(h, w // 2) // 4).astype(np.uint8)
                    got = _yuv422_to_rgb_8bit(gy, gu, gv)
                    recs.append({
                        "source": name, "spec": spec, "frame": fid,
                        "codec": f"topos-{tier}", "bytes": toos.stat().st_size,
                        "psnr_db": round(_psnr(ref, got), 2),
                        "enc_ms": t_enc, "dec_ms": t_dec,
                    })

                # —— JPEG 三档 ——
                for q, ss, tag in [(85, -1, "q85-420"), (90, -1, "q90-420"),
                                   (95, 0, "q95-444")]:
                    buf = io.BytesIO()
                    t0 = time.perf_counter()
                    ref_img.save(buf, "JPEG", quality=q, subsampling=ss)
                    t_enc = (time.perf_counter() - t0) * 1e3
                    n = buf.tell()
                    t0 = time.perf_counter()
                    got = np.asarray(Image.open(io.BytesIO(buf.getvalue())))
                    t_dec = (time.perf_counter() - t0) * 1e3
                    recs.append({
                        "source": name, "spec": spec, "frame": fid,
                        "codec": f"jpeg-{tag}", "bytes": n,
                        "psnr_db": round(_psnr(ref, got), 2),
                        "enc_ms": t_enc, "dec_ms": t_dec,
                    })

                # —— PNG8（无损：PSNR=∞；只记尺寸/速度） ——
                buf = io.BytesIO()
                t0 = time.perf_counter()
                ref_img.save(buf, "PNG", compress_level=6)
                t_enc = (time.perf_counter() - t0) * 1e3
                n = buf.tell()
                t0 = time.perf_counter()
                Image.open(io.BytesIO(buf.getvalue())).load()
                t_dec = (time.perf_counter() - t0) * 1e3
                recs.append({
                    "source": name, "spec": spec, "frame": fid,
                    "codec": "png8-lossless", "bytes": n,
                    "psnr_db": None, "enc_ms": t_enc, "dec_ms": t_dec,
                })
                print(f"{name} {spec} f{fid}: done")

    # —— 10-bit 调色空间主张：prores 10-bit → topos 往返 → 10-bit 域 PSNR ——
    tenbit = []
    for name, path in SOURCES:
        with av.open(str(path)) as cont:
            for i, f in enumerate(cont.decode(video=0)):
                if i == 10:
                    pls = _frame_planes(f)
                    break
        h, w = pls[0].shape
        data = [p.tobytes() for p in pls]
        for tier, qm, qp in [("422-high", 1, 58), ("422-ultra", 0, 58)]:
            pkt, _ = codec.encode_frame(_make_fc(w, h, qp, qm), data)
            toos = tmp / f"tb_{name}_{tier}.toos"
            ic.write_packet(str(toos), pkt, 0, durable=False)
            gpls, _, _ = ic.decode(str(toos))
            for pi, pn in enumerate(["Y", "U", "V"]):
                a = pls[pi].astype(np.float64)
                b = np.asarray(gpls[pi], dtype="<u2").reshape(a.shape).astype(np.float64)
                mse = float(np.mean((a - b) ** 2))
                psnr = 10.0 * np.log10(1023.0 ** 2 / mse) if mse > 0 else float("inf")
                tenbit.append({"source": name, "tier": tier, "plane": pn,
                               "psnr_db": round(psnr, 2)})
    # 8-bit 量化理论上限
    for name, path in SOURCES:
        with av.open(str(path)) as cont:
            for i, f in enumerate(cont.decode(video=0)):
                if i == 10:
                    pls = _frame_planes(f)
                    break
        a = pls[0].astype(np.float64)
        b = ((pls[0] >> 2) << 2).astype(np.float64)
        mse = float(np.mean((a - b) ** 2))
        tenbit.append({"source": name, "tier": "8bit-quant-ceiling",
                       "plane": "Y", "psnr_db": round(10.0 * np.log10(1023.0 ** 2 / mse), 2)})

    out = REPO / "tools/toos_vs_jpg_png_2026-09-12.json"
    out.write_text(json.dumps({"ladder": recs, "tenbit": tenbit}, indent=1))
    print(f"JSON → {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
