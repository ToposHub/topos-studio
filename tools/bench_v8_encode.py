#!/usr/bin/env python3
"""V8 批 2：V8 vs V7-R2 同帧同 qp 码率对照（真实素材）。

口径：stats.color_payload_bytes——V7 = Σ(前缀+流)（=批 0 的 A），
V8 = Σ(目录+瓦片表+流+CRC)（=批 0 的 B+簿记）。批 0 预测（tile=32、
12B 目录）：qp≤30 +1.4~2.7%；qp44 +2.7~5.2%。±1pp 内为验收通过。
输出 tools/v8_encode_size_2026-09-12.json。
"""
from __future__ import annotations

import ctypes
import json
import sys
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
    ("dnxhr444", REPO / "tests/test_videos/4K_DNxHR 444 10bit.mov", 10),
]
QPS = [20, 30, 44]


def _frame_planes(frame):
    pls = []
    for p in frame.planes:
        stride = p.line_size // 2
        arr = np.frombuffer((ctypes.c_char * p.buffer_size).from_address(
            p.buffer_ptr), dtype="<u2")
        pls.append(arr.reshape(p.height, stride)[:, :p.width].copy())
    return pls


def _make_fc(w, h, qp, v8):
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
    fc.reserved[0] = 9 if v8 else 8
    return fc


def main() -> int:
    codec = ToposCodec()
    recs = []
    for name, path, fi in SOURCES:
        with av.open(str(path)) as cont:
            planes4k = None
            for i, frame in enumerate(cont.decode(video=0)):
                if i == fi:
                    planes4k = _frame_planes(frame.reformat(format="yuv422p10le"))
                    break
        assert planes4k is not None
        for res, planes in (("4K", planes4k),
                            ("8K", [np.ascontiguousarray(
                                np.repeat(np.repeat(p, 2, 0), 2, 1))
                                for p in planes4k])):
            h, w = planes[0].shape
            data = [np.ascontiguousarray(p).tobytes() for p in planes]
            for qp in QPS:
                _p7, st7 = codec.encode_frame(_make_fc(w, h, qp, False), data)
                _p8, st8 = codec.encode_frame(_make_fc(w, h, qp, True), data)
                delta = 100.0 * (st8.color_payload_bytes
                                 - st7.color_payload_bytes) / st7.color_payload_bytes
                recs.append({
                    "source": name, "frame": fi, "res": res, "w": w, "h": h,
                    "qp": qp,
                    "v7_packet": st7.packet_size, "v8_packet": st8.packet_size,
                    "v7_payload": st7.color_payload_bytes,
                    "v8_payload": st8.color_payload_bytes,
                    "payload_delta_pct": delta,
                })
                print(f"{name:11} f{fi} {res} qp{qp}: V7={st7.color_payload_bytes/1e6:7.3f}MB "
                      f"V8={st8.color_payload_bytes/1e6:7.3f}MB Δ={delta:+.2f}%")
    out = REPO / "tools/v8_encode_size_2026-09-12.json"
    out.write_text(json.dumps(recs, indent=1))
    print(f"\nJSON → {out}")
    lo = min(r["payload_delta_pct"] for r in recs)
    hi = max(r["payload_delta_pct"] for r in recs)
    print(f"Δ 范围：{lo:+.2f}% ~ {hi:+.2f}%（批 0 预测 +1.4~5.2%，±1pp 验收带）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
