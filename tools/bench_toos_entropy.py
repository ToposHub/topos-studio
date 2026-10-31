#!/usr/bin/env python3
"""图片链路（.toos/TPIM）熵档基准：码率 + 编码/解码性能，2K/4K 真实素材。

背景：视频链路默认已转正 V7-R2 rans2（ADR-C038），图片链路 `toos encode`
CLI 默认 vlc（V2 canonical VLC），且 TPIM v1 规格把内层 packet 限制在
V1..V6（image_container.c IDSC 校验）。本基准回答：图片侧若吃到 rANS
（V7-R2）能有多大提升（位级 packet 对照），以及各熵档的编解码耗时。

口径：
- 内层 packet 级（tc_frame_encode，reserved[0] = 0/1/8），AQ/RDO 关闭；
- 码率 = packet_size（含 53B 帧头；.toos 信封为固定开销，单独实测）；
- 编码/解码 = ToposCodec.encode_frame / tc_frame_decode 墙钟，median(3)，
  1 次预热；解码含平面直写（与生产 ToposMediaSource 同构）；
- 素材：tests/test_videos 4K prores422HQ / DNxHR HQX 10bit（422 10bit，
  image_profile=0 Preview 家族），2K = 1920×1080（swscale 缩放）。

输出 tools/toos_entropy_<date>.json。
"""
from __future__ import annotations

import ctypes
import json
import statistics
import sys
import time
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

import av  # noqa: E402

from src.shared.codec.topos_binding import (  # noqa: E402
    TOPOS_CODEC_ABI_VERSION, ToposCodec, _CFrameConfig,
)

SOURCES = [
    ("prores422HQ", REPO / "tests/test_videos/4K_prores422HQ.mov", 10),
    ("dnxhrhqx", REPO / "tests/test_videos/4K_DNxHR HQX 10bit.mov", 10),
]
RES = [("4K", None, None), ("2K", 1920, 1080)]
QPS = [24, 44]
CHAINS = [
    ("v1_rice", 0),       # V1 Rice（历史位流）
    ("v2_vlc", 1),        # V2 canonical VLC —— 图片链路现行默认（toos encode）
    ("v7_rans2", 8),      # V7-R2 —— 视频现行默认（TPIM v1 信封不允许）；V7-R 已退役（ADR-C046）
]
N_TIMED = 3


def _frame_planes(frame) -> list[np.ndarray]:
    pls = []
    for p in frame.planes:
        arr = np.frombuffer(ctypes.string_at(p.buffer_ptr, p.buffer_size),
                            dtype="<u2")
        stride = p.line_size // 2
        pls.append(arr.reshape(p.height, stride)[:, :p.width].copy())
    return pls


def _make_fc(w: int, h: int, qp: int, coding: int) -> _CFrameConfig:
    fc = _CFrameConfig()
    fc.struct_size = ctypes.sizeof(_CFrameConfig)
    fc.abi_version = TOPOS_CODEC_ABI_VERSION
    fc.visible_width = w
    fc.visible_height = h
    fc.profile = 3
    fc.pixel_format = 0
    fc.bit_depth = 10
    fc.qp_base = qp
    fc.qmatrix_id = 1
    fc.reserved[0] = coding
    return fc


def _decode_timed(codec: ToposCodec, pkt: bytes, w: int, h: int,
                  planes_ptr, strides, out_info) -> float:
    """单次解码墙钟（秒）。缓冲由调用方预建复用。"""
    buf = (ctypes.c_ubyte * len(pkt)).from_buffer_copy(pkt)
    t0 = time.perf_counter()
    rc = codec._lib.tc_frame_decode(ctypes.cast(buf, ctypes.c_void_p),
                                    ctypes.c_size_t(len(pkt)), planes_ptr,
                                    strides, ctypes.byref(out_info))
    dt = time.perf_counter() - t0
    assert rc == 0, f"decode rc={rc}"
    return dt


def main() -> int:
    codec = ToposCodec()
    recs = []
    for name, path, fi in SOURCES:
        with av.open(str(path)) as cont:
            frame4k = None
            for i, frame in enumerate(cont.decode(video=0)):
                if i == fi:
                    frame4k = frame
                    break
        assert frame4k is not None
        for res, rw, rh in RES:
            f = frame4k.reformat(width=rw, height=rh, format="yuv422p10le") \
                if rw else frame4k.reformat(format="yuv422p10le")
            planes = _frame_planes(f)
            h, w = planes[0].shape
            data = [np.ascontiguousarray(p).tobytes() for p in planes]
            # 解码缓冲预建
            dims = [(h, w), (h, w // 2), (h, w // 2)]
            holders = [np.zeros(d, "<u2") for d in dims]
            planes_ptr = (ctypes.POINTER(ctypes.c_ushort) * 4)()
            strides = (ctypes.c_size_t * 4)()
            for i, arr in enumerate(holders):
                planes_ptr[i] = ctypes.cast(arr.ctypes.data,
                                            ctypes.POINTER(ctypes.c_ushort))
                strides[i] = arr.shape[1]
            import src.shared.codec.topos_binding as tb
            out_info = tb._CFrameOutput()
            out_info.struct_size = ctypes.sizeof(tb._CFrameOutput)
            out_info.abi_version = TOPOS_CODEC_ABI_VERSION

            for qp in QPS:
                size_ref = None
                for chain, coding in CHAINS:
                    pkt = None
                    enc_ms = []
                    for _ in range(N_TIMED + 1):
                        t0 = time.perf_counter()
                        pkt, st = codec.encode_frame(_make_fc(w, h, qp, coding),
                                                     data)
                        enc_ms.append((time.perf_counter() - t0) * 1e3)
                    enc_ms = enc_ms[1:]  # 去预热
                    dec_ms = []
                    for _ in range(N_TIMED + 1):
                        dec_ms.append(_decode_timed(codec, pkt, w, h,
                                                    planes_ptr, strides,
                                                    out_info) * 1e3)
                    dec_ms = dec_ms[1:]
                    if size_ref is None:
                        size_ref = st.packet_size
                    rec = {
                        "source": name, "res": res, "w": w, "h": h, "qp": qp,
                        "chain": chain, "coding_selection": coding,
                        "packet_bytes": st.packet_size,
                        "payload_bytes": st.color_payload_bytes,
                        "bits_per_pixel": st.packet_size * 8.0 / (w * h),
                        "encode_ms": statistics.median(enc_ms),
                        "decode_ms": statistics.median(dec_ms),
                        "encode_mbps_in": w * h / (statistics.median(enc_ms) / 1e3) / 1e6,
                        "decode_mbps_in": w * h / (statistics.median(dec_ms) / 1e3) / 1e6,
                        "size_vs_image_default_pct":
                            100.0 * (st.packet_size - size_ref) / size_ref,
                    }
                    recs.append(rec)
                    print(f"{name:11} {res} qp{qp} {chain:9}: "
                          f"{st.packet_size/1e6:7.3f}MB "
                          f"({rec['bits_per_pixel']:5.2f}bpp) "
                          f"enc {rec['encode_ms']:7.1f}ms "
                          f"dec {rec['decode_ms']:6.1f}ms")

    # —— .toos 信封：固定开销实测 + TPIM v1 对 rans2 的规格拒收 ——
    env = {}
    try:
        from src.shared.codec.topos_image_binding import get_image_codec
        ic = get_image_codec()
        # 取 prores422HQ 4K qp24 vlc 包
        with av.open(str(SOURCES[0][1])) as cont:
            for i, frame in enumerate(cont.decode(video=0)):
                if i == SOURCES[0][2]:
                    planes = _frame_planes(
                        frame.reformat(format="yuv422p10le"))
                    break
        data = [np.ascontiguousarray(p).tobytes() for p in planes]
        h, w = planes[0].shape
        pkt_vlc, _ = codec.encode_frame(_make_fc(w, h, 24, 1), data)
        out_toos = Path("/tmp/toos_bench_vlc.toos")
        ic.write_packet(str(out_toos), pkt_vlc, 0)
        env["vlc_packet_bytes"] = len(pkt_vlc)
        env["toos_file_bytes"] = out_toos.stat().st_size
        env["envelope_overhead_bytes"] = out_toos.stat().st_size - len(pkt_vlc)
        pkt_r2, _ = codec.encode_frame(_make_fc(w, h, 24, 8), data)
        try:
            ic.write_packet("/tmp/toos_bench_r2.toos", pkt_r2, 0)
            env["rans2_envelope"] = "ACCEPTED（意外——与 V1..V6 规格不符）"
        except Exception as e:  # noqa: BLE001
            env["rans2_envelope"] = f"REJECTED: {e}"
    except Exception as e:  # noqa: BLE001
        env["error"] = str(e)
    recs.append({"envelope_probe": env})
    print("\n信封探测:", json.dumps(env, ensure_ascii=False))

    out = REPO / "tools/toos_entropy_2026-09-12.json"
    out.write_text(json.dumps(recs, indent=1))
    print(f"\nJSON → {out}")

    # 汇总：图片默认(v2_vlc) vs rans2 的尺寸/速度差
    by = {(r.get("source"), r.get("res"), r.get("qp"), r.get("chain")): r
          for r in recs if "chain" in r}
    print("\n==== 汇总：v2_vlc（图片默认） vs v7_rans2（视频默认） ====")
    for (s, res, qp) in {(r["source"], r["res"], r["qp"]) for r in recs
                         if "chain" in r}:
        v = by[(s, res, qp, "v2_vlc")]
        r2 = by[(s, res, qp, "v7_rans2")]
        dsz = 100.0 * (r2["packet_bytes"] - v["packet_bytes"]) / v["packet_bytes"]
        de = 100.0 * (r2["encode_ms"] - v["encode_ms"]) / v["encode_ms"]
        dd = 100.0 * (r2["decode_ms"] - v["decode_ms"]) / v["decode_ms"]
        print(f"{s:11} {res} qp{qp}: 尺寸 {dsz:+6.2f}%  "
              f"编码 {de:+7.1f}%  解码 {dd:+7.1f}%")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
