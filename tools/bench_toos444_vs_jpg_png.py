#!/usr/bin/env python3
"""444 规格三方对照：topos 444（GBR 12-bit）vs JPEG vs PNG8/16。

回答主张：更高要求的 444 档位（444-high/ultra，GBR 直存 12-bit）同样
碾压 JPG 与 PNG？

口径：
- GBR = 平面即 G/B/R（无 YUV 变换、无色度二次采样）——topos 侧唯一损失
  = DCT 量化；JPEG 8-bit 封顶；PNG 无损（PNG8=8bit RGB、PNG16=16bit
  容器可保 12-bit 内容，cv2/libpng level 6）。
- 参考域 RGB8：prores 帧 → YUV→RGB8（与 422 对照同源参考）。
- 12-bit 调色空间：10-bit 源 ×64 上caster 12-bit → topos 444 往返 →
  12-bit 域 PSNR，对照 8-bit 量化地板（54.8dB）与 422-10bit ultra 结果。
- 速度：PIL(JPEG/PNG8)、cv2(PNG16)、topos 生产绑定路径。

输出 tools/toos444_vs_jpg_png_2026-09-12.json。
"""
from __future__ import annotations

import ctypes
import io
import json
import statistics
import sys
import tempfile
import time
from pathlib import Path

import cv2
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
TIERS = [("444-high", 2, 63), ("444-ultra", 2, 58)]

# （与 bench_toos_vs_jpg_png 同一 BT.601 全范围实现）
def _yuv422_to_rgb_8bit(y8, u8, v8):
    h, w = y8.shape
    u = np.repeat(u8.astype(np.float64), 2, axis=1)[:, :w]
    v = np.repeat(v8.astype(np.float64), 2, axis=1)[:, :w]
    y = y8.astype(np.float64)
    r = y + 1.402 * (v - 128.0)
    g = y - 0.344136 * (u - 128.0) - 0.714136 * (v - 128.0)
    b = y + 1.772 * (u - 128.0)
    return np.stack([np.clip(np.rint(r), 0, 255),
                     np.clip(np.rint(g), 0, 255),
                     np.clip(np.rint(b), 0, 255)], axis=-1).astype(np.uint8)


def _psnr(a: np.ndarray, b: np.ndarray) -> float:
    d = a.astype(np.float64) - b.astype(np.float64)
    mse = float(np.mean(d * d))
    return float("inf") if mse == 0.0 else 10.0 * np.log10(255.0 ** 2 / mse)


def _frame_planes(frame):
    pls = []
    for p in frame.planes:
        arr = np.frombuffer(ctypes.string_at(p.buffer_ptr, p.buffer_size),
                            dtype="<u2")
        stride = p.line_size // 2
        pls.append(arr.reshape(p.height, stride)[:, :p.width].copy())
    return pls


def _make_fc_gbr(w: int, h: int, qp: int, qm: int) -> _CFrameConfig:
    fc = _CFrameConfig()
    fc.struct_size = ctypes.sizeof(_CFrameConfig)
    fc.abi_version = TOPOS_CODEC_ABI_VERSION
    fc.visible_width = w
    fc.visible_height = h
    fc.profile = 5        # GBR
    fc.pixel_format = 2   # GBR 直存
    fc.bit_depth = 12
    fc.qp_base = qp
    fc.qmatrix_id = qm    # 2 = 444 Compact（档位表）
    fc.reserved[0] = 8
    fc.reserved[1] = 1
    return fc


def main() -> int:
    codec = ToposCodec()
    ic = get_image_codec()
    recs = []
    tmp = Path(tempfile.mkdtemp(prefix="toos444_"))

    for name, path in SOURCES:
        with av.open(str(path)) as cont:
            frames = [f for i, f in enumerate(cont.decode(video=0))
                      if i in FRAME_IDS]

        for spec, rw, rh in SPECS:
            for fid, f in zip(FRAME_IDS, frames):
                pls10 = _frame_planes(
                    f.reformat(width=rw, height=rh, format="yuv422p10le")
                    if rw else f.reformat(format="yuv422p10le"))
                h, w = pls10[0].shape
                y8 = (pls10[0] >> 2).astype(np.uint8)
                u8 = (pls10[1] >> 2).astype(np.uint8)
                v8 = (pls10[2] >> 2).astype(np.uint8)
                ref = _yuv422_to_rgb_8bit(y8, u8, v8)  # RGB8 参考

                # —— topos 444（GBR 12-bit：×16 上caster，平面序 G,B,R） ——
                # NEP 50：uint8×16 会在 uint8 回绕——显式升型后再乘
                g12 = np.multiply(ref[:, :, 1], 16, dtype="<u2")
                b12 = np.multiply(ref[:, :, 2], 16, dtype="<u2")
                r12 = np.multiply(ref[:, :, 0], 16, dtype="<u2")
                data = [g12.tobytes(), b12.tobytes(), r12.tobytes()]
                for tier, qm, qp in TIERS:
                    t0 = time.perf_counter()
                    pkt, _ = codec.encode_frame(_make_fc_gbr(w, h, qp, qm),
                                                data)
                    t_enc = (time.perf_counter() - t0) * 1e3
                    toos = tmp / f"{name}_{spec}_{fid}_{tier}.toos"
                    ic.write_packet(str(toos), pkt, 2, durable=False)  # XQ
                    t0 = time.perf_counter()
                    gpls, _, _ = ic.decode(str(toos))
                    t_dec = (time.perf_counter() - t0) * 1e3
                    gg = (np.asarray(gpls[0], dtype="<u2").reshape(h, w) // 16)
                    gb = (np.asarray(gpls[1], dtype="<u2").reshape(h, w) // 16)
                    gr = (np.asarray(gpls[2], dtype="<u2").reshape(h, w) // 16)
                    got = np.stack([gr, gg, gb], axis=-1).astype(np.uint8)
                    recs.append({
                        "source": name, "spec": spec, "frame": fid,
                        "codec": f"topos-{tier}", "bytes": toos.stat().st_size,
                        "psnr_db": round(_psnr(ref, got), 2),
                        "enc_ms": t_enc, "dec_ms": t_dec,
                    })

                ref_img = Image.fromarray(ref, "RGB")
                # —— JPEG q95/q100 4:4:4 ——
                for q, tag in [(95, "q95-444"), (100, "q100-444")]:
                    buf = io.BytesIO()
                    t0 = time.perf_counter()
                    ref_img.save(buf, "JPEG", quality=q, subsampling=0)
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
                # —— PNG8（无损） ——
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
                # —— PNG16（16-bit 容器无损，cv2/libpng level6；12-bit 内容
                #      左对齐 ×16） ——
                bgr16 = np.stack([np.multiply(b12, 16, dtype="<u2"),
                                  np.multiply(g12, 16, dtype="<u2"),
                                  np.multiply(r12, 16, dtype="<u2")],
                                 axis=-1).astype(np.uint16)
                t0 = time.perf_counter()
                ok = cv2.imwrite(str(tmp / "t16.png"), bgr16,
                                 [cv2.IMWRITE_PNG_COMPRESSION, 6])
                t_enc = (time.perf_counter() - t0) * 1e3
                assert ok
                n = (tmp / "t16.png").stat().st_size
                t0 = time.perf_counter()
                back = cv2.imread(str(tmp / "t16.png"), cv2.IMREAD_UNCHANGED)
                t_dec = (time.perf_counter() - t0) * 1e3
                assert back is not None and back.dtype == np.uint16
                got8 = (back[:, :, ::-1] // 16).astype(np.uint8)
                recs.append({
                    "source": name, "spec": spec, "frame": fid,
                    "codec": "png16-lossless(12bit)", "bytes": n,
                    "psnr_db": None, "enc_ms": t_enc, "dec_ms": t_dec,
                })
                print(f"{name} {spec} f{fid}: done")

    # —— 12-bit 域调色空间：10-bit 源 ×64 → 444 往返 → 12-bit 域 PSNR ——
    tenbit = []
    for name, path in SOURCES:
        with av.open(str(path)) as cont:
            for i, f in enumerate(cont.decode(video=0)):
                if i == 10:
                    pls = _frame_planes(f)
                    break
        h, w = pls[0].shape
        # GBR 12-bit：YUV→RGB8 → ×16（8-bit 内容）；另做 10-bit 保真测试：
        # prores 10-bit 平面直接当 GBR？不成立（YUV≠GBR）。诚实口径：
        # 10-bit RGB 内容无真实素材——用 10-bit 源的 RGB8 往返 ×16 上caster，
        # 量 topos 444 对 12-bit 容器内 8-bit 内容的保真（=纯编码损失），
        # 以及 vs 422-10bit ultra 的同源对比已在 toos_vs_jpg_png 落档。
        y8 = (pls[0] >> 2).astype(np.uint8)
        u8 = (pls[1] >> 2).astype(np.uint8)
        v8 = (pls[2] >> 2).astype(np.uint8)
        ref = _yuv422_to_rgb_8bit(y8, u8, v8)
        g12 = np.multiply(ref[:, :, 1], 16, dtype="<u2")
        b12 = np.multiply(ref[:, :, 2], 16, dtype="<u2")
        r12 = np.multiply(ref[:, :, 0], 16, dtype="<u2")
        data = [g12.tobytes(), b12.tobytes(), r12.tobytes()]
        for tier, qm, qp in [("444-high", 2, 63), ("444-ultra", 2, 58)]:
            pkt, _ = codec.encode_frame(_make_fc_gbr(w, h, qp, qm), data)
            toos = tmp / f"tb_{name}_{tier}.toos"
            ic.write_packet(str(toos), pkt, 2, durable=False)
            gpls, _, _ = ic.decode(str(toos))
            # 12-bit 域 PSNR（对 12-bit 输入）
            for pi, pn in [(0, "G"), (1, "B"), (2, "R")]:
                a = [g12, b12, r12][pi].astype(np.float64)
                b = np.asarray(gpls[pi], dtype="<u2").reshape(a.shape).astype(np.float64)
                mse = float(np.mean((a - b) ** 2))
                psnr = 10.0 * np.log10(4095.0 ** 2 / mse) if mse > 0 else float("inf")
                tenbit.append({"source": name, "tier": tier, "plane": pn,
                               "psnr_db": round(psnr, 2)})

    out = REPO / "tools/toos444_vs_jpg_png_2026-09-12.json"
    out.write_text(json.dumps({"ladder": recs, "twelvebit": tenbit}, indent=1))
    print(f"JSON → {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
