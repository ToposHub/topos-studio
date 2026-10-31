#!/usr/bin/env python3
"""ProRes 422HQ vs 4444 对照：422 原生素材是否也存在「444 容器税」。

镜像 topos 444 实验的两场景口径（tools/bench_toos444_vs_jpg_png.py）：
- 场景 A（422 原生实拍素材）：prores422HQ/DNxHR 4K 帧 → prores_ks
  profile=hq(yuv422p10le) / profile=4444(yuv444p10le，色度上采 422→444 入)
  → 解码回 422 域 → 10-bit 域逐平面 PSNR vs 源。
- 场景 B（RGB 原生锐利色度内容）：合成彩条/文字状边缘（1080p）→ RGB8
  参考；422HQ 路径经 4:2:2 二次采样、4444 路径全分辨率 → RGB8 域 PSNR。

ProRes 档位为 Apple 预设码率目标（HQ≈220Mbps@4K、4444≈330Mbps），
预设对预设，与 topos 档位对档位同构。编码经 PyAV/libav prores_ks。
输出 tools/prores_4444_vs_422hq_2026-09-12.json。
"""
from __future__ import annotations

import ctypes
import io
import json
import sys
from pathlib import Path

import numpy as np
import av

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

SOURCES = [
    ("prores422HQ", REPO / "tests/test_videos/4K_prores422HQ.mov"),
    ("dnxhrhqx", REPO / "tests/test_videos/4K_DNxHR HQX 10bit.mov"),
]
FRAME_IDS = [10, 11]


def _frame_planes(frame):
    pls = []
    for p in frame.planes:
        arr = np.frombuffer(ctypes.string_at(p.buffer_ptr, p.buffer_size),
                            dtype="<u2")
        stride = p.line_size // 2
        pls.append(arr.reshape(p.height, stride)[:, :p.width].copy())
    return pls


def _make_frame(planes, pix_fmt, w, h) -> av.VideoFrame:
    frame = av.VideoFrame(w, h, pix_fmt)
    frame.planes[0].update(np.ascontiguousarray(planes[0]))
    for ci in (1, 2):
        pl = planes[ci]
        if pl.shape[1] != w and pix_fmt == "yuv444p10le":
            # 422 半宽色度入 444 容器：水平上采（场景 A）
            pl = np.repeat(pl, 2, axis=1)[:, :w]
        frame.planes[ci].update(np.ascontiguousarray(pl))
    return frame


def encode_prores(planes, pix_fmt, profile, w, h) -> bytes:
    buf = io.BytesIO()
    cont = av.open(buf, "w", format="matroska")
    st = cont.add_stream("prores_ks", rate=24)
    st.width, st.height, st.pix_fmt = w, h, pix_fmt
    st.options = {"profile": profile}
    for pk in st.encode(_make_frame(planes, pix_fmt, w, h)):
        cont.mux(pk)
    for pk in st.encode(None):
        cont.mux(pk)
    cont.close()
    return buf.getvalue()


def decode_planes(data: bytes, fmt: str):
    cont = av.open(io.BytesIO(data))
    for frame in cont.decode(video=0):
        return _frame_planes(frame.reformat(format=fmt))
    raise RuntimeError("no frame")


def psnr10(a, b) -> float:
    d = a.astype(np.float64) - b.astype(np.float64)
    mse = float(np.mean(d * d))
    return float("inf") if mse == 0 else 10.0 * np.log10(1023.0 ** 2 / mse)


def psnr8(a, b) -> float:
    d = a.astype(np.float64) - b.astype(np.float64)
    mse = float(np.mean(d * d))
    return float("inf") if mse == 0 else 10.0 * np.log10(255.0 ** 2 / mse)


def main() -> int:
    recs = []
    tmp = Path("/tmp/prores_cmp")
    tmp.mkdir(exist_ok=True)

    # —— 场景 A：422 原生实拍素材 ——
    for name, path in SOURCES:
        with av.open(str(path)) as cont:
            frames = [f for i, f in enumerate(cont.decode(video=0))
                      if i in FRAME_IDS]
        for fid, f in zip(FRAME_IDS, frames):
            src = _frame_planes(f)
            h, w = src[0].shape
            for profile, pf, tag in [("hq", "yuv422p10le", "422HQ"),
                                     ("4444", "yuv444p10le", "4444")]:
                data = encode_prores(src, pf, profile, w, h)
                (tmp / f"{name}_{fid}_{tag}.mks").write_bytes(data)
                dec = decode_prores_plain = decode_planes(data, "yuv422p10le")
                ps = [psnr10(s, d) for s, d in zip(src, dec)]
                recs.append({
                    "scene": "A_422native", "source": name, "frame": fid,
                    "codec": f"prores-{tag}", "bytes": len(data),
                    "mbps_24": len(data) * 8 * 24 / 1e6,
                    "psnr_y": round(ps[0], 2), "psnr_u": round(ps[1], 2),
                    "psnr_v": round(ps[2], 2),
                })
                print(f"A {name:10} f{fid} {tag:5}: {len(data)/1e6:6.2f}MB "
                      f"({len(data)*8*24/1e6:5.0f}Mbps@24) "
                      f"Y {ps[0]:.2f} U {ps[1]:.2f} V {ps[2]:.2f} dB")

    # —— 场景 B：RGB 原生锐利色度合成内容（1080p，与 topos 实验同图案） ——
    w, h = 1920, 1080
    yy, xx = np.mgrid[0:h, 0:w]
    r = ((xx // 160) % 3 == 0).astype(np.uint8) * 200 + 40
    g = ((xx // 160) % 3 == 1).astype(np.uint8) * 200 + 40
    b = ((xx // 160) % 3 == 2).astype(np.uint8) * 200 + 40
    stripes = (xx % 8) < 4
    r = np.where(stripes, r ^ 180, r).astype(np.uint8)
    g = np.where(stripes, g ^ 180, g).astype(np.uint8)
    b = np.where(stripes, b ^ 180, b).astype(np.uint8)
    ref8 = np.stack([r, g, b], -1)

    rf = ref8[:, :, 0].astype(np.float64)
    gf = ref8[:, :, 1].astype(np.float64)
    bf = ref8[:, :, 2].astype(np.float64)
    y = 0.299 * rf + 0.587 * gf + 0.114 * bf
    r2 = w // 2
    u = (-0.169 * rf[:, 0::2][:, :r2] - 0.331 * gf[:, 0::2][:, :r2]
         + 0.5 * bf[:, 0::2][:, :r2] + 128
         - 0.169 * rf[:, 1::2][:, :r2] - 0.331 * gf[:, 1::2][:, :r2]
         + 0.5 * bf[:, 1::2][:, :r2] + 128) / 2
    v = (0.5 * rf[:, 0::2][:, :r2] - 0.419 * gf[:, 0::2][:, :r2]
         - 0.081 * bf[:, 0::2][:, :r2] + 128
         + 0.5 * rf[:, 1::2][:, :r2] - 0.419 * gf[:, 1::2][:, :r2]
         - 0.081 * bf[:, 1::2][:, :r2] + 128) / 2
    src_planes_422 = [
        np.multiply(np.clip(np.rint(y), 0, 255).astype(np.uint8), 4,
                    dtype="<u2"),
        np.multiply(np.clip(np.rint(u), 0, 255).astype(np.uint8), 4,
                    dtype="<u2"),
        np.multiply(np.clip(np.rint(v), 0, 255).astype(np.uint8), 4,
                    dtype="<u2"),
    ]
    # 4444 路径的真实工作流输入：RGB → YUV444 直入（无二次采样）
    uf = np.rint((-0.169 * rf - 0.331 * gf + 0.5 * bf + 128))
    vf = np.rint((0.5 * rf - 0.419 * gf - 0.081 * bf + 128))
    src_planes_444 = [
        src_planes_422[0],
        np.multiply(np.clip(uf, 0, 255).astype(np.uint8), 4, dtype="<u2"),
        np.multiply(np.clip(vf, 0, 255).astype(np.uint8), 4, dtype="<u2"),
    ]

    def to_rgb8(planes, from_444: bool):
        y = planes[0] >> 2
        u = planes[1] >> 2
        v = planes[2] >> 2
        if not from_444:
            u = np.repeat(u, 2, axis=1)[:, :w]
            v = np.repeat(v, 2, axis=1)[:, :w]
        yf = y.astype(np.float64)
        uf = u.astype(np.float64)
        vf = v.astype(np.float64)
        rr = yf + 1.402 * (vf - 128)
        gg = yf - 0.344136 * (uf - 128) - 0.714136 * (vf - 128)
        bb = yf + 1.772 * (uf - 128)
        return np.stack([np.clip(np.rint(rr), 0, 255),
                         np.clip(np.rint(gg), 0, 255),
                         np.clip(np.rint(bb), 0, 255)], -1).astype(np.uint8)

    for profile, pf, tag, is444 in [("hq", "yuv422p10le", "422HQ", False),
                                    ("4444", "yuv444p10le", "4444", True)]:
        inp = src_planes_444 if is444 else src_planes_422
        data = encode_prores(inp, pf, profile, w, h)
        dec = decode_planes(data, pf)
        got = to_rgb8(dec, is444)
        recs.append({
            "scene": "B_rgbnative", "source": "synthetic", "frame": 0,
            "codec": f"prores-{tag}", "bytes": len(data),
            "mbps_24": len(data) * 8 * 24 / 1e6,
            "psnr_rgb8": round(psnr8(ref8, got), 2),
        })
        print(f"B synthetic {tag:5}: {len(data)/1e6:6.2f}MB "
              f"RGB8 PSNR {recs[-1]['psnr_rgb8']:.2f}dB")

    out = REPO / "tools/prores_4444_vs_422hq_2026-09-12.json"
    out.write_text(json.dumps(recs, indent=1))
    print(f"JSON → {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
