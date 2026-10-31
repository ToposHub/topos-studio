#!/usr/bin/env python3
"""V8 批 0：共享表统计损失实测（topos_v8_format_plan §3 批 0）。

三段真实素材 × {4K, 8K 缩放} × {qp20, 30, 44}：
  A = V7-R2 现状（逐带精确表，生产 payload 逐位）；
  B = 瓦片聚合表模拟（模型来源改聚合计数，表每瓦片一份 + 每带瓦片
      模型流；不动位流格式，只测尺寸差）。
C 侧模拟器 ``tc_dev_rans2_tile_sim`` 自带 A 锚差分（重放 ≠ 生产即报
MALFORMED），本脚本只采集与汇总。段簿记（seg_blocks=16）为解析项，
与 B 相加得 V8 总账口径。实验代码，不进生产路径。

用法：.venv/bin/python tools/bench_v8_table_loss.py [--out json路径]
"""
from __future__ import annotations

import ctypes
import json
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
    ("prores422HQ", REPO / "tests/test_videos/4K_prores422HQ.mov"),
    ("dnxhrhqx", REPO / "tests/test_videos/4K_DNxHR HQX 10bit.mov"),
    ("dnxhr444", REPO / "tests/test_videos/4K_DNxHR 444 10bit.mov"),
]
FRAME_INDICES = [10, 40]
QPS = [20, 30, 44]
TILE_ROWS = [0, 64, 32, 16]  # 0 = 整平面一片
SEG_BLOCKS = 16              # V8 规格默认段粒度


class _CTileSimOut(ctypes.Structure):
    _fields_ = [
        ("a_total", ctypes.c_uint64),
        ("a_prefix", ctypes.c_uint64),
        ("a_stream", ctypes.c_uint64),
        ("b_total", ctypes.c_uint64),
        ("b_table", ctypes.c_uint64),
        ("b_stream", ctypes.c_uint64),
        ("n_bands", ctypes.c_uint32),
        ("n_tiles", ctypes.c_uint32),
        ("tile_rows", ctypes.c_uint32),
        ("seg_blocks", ctypes.c_uint32),
        ("seg_count", ctypes.c_uint64),
        ("seg_dir_bytes", ctypes.c_uint64),
        ("seg_state_extra", ctypes.c_uint64),
        ("flags_lvl", ctypes.c_uint32 * 3),
        ("flags_dc", ctypes.c_uint32 * 2),
    ]


def _bind_sim(lib) -> None:
    lib.tc_dev_rans2_tile_sim.argtypes = [
        ctypes.c_uint32, ctypes.c_uint32, ctypes.POINTER(_CTileSimOut),
    ]
    lib.tc_dev_rans2_tile_sim.restype = ctypes.c_int32


def _frame_planes(frame) -> list[np.ndarray]:
    """av frame（reformat 到 yuv422p10le 后）→ 三平面 u16 数组。

    from_address 包装的是 av 帧内部缓冲（无所有权）；行紧贴时切片是
    连续视图、ascontiguousarray 不拷贝 → 悬空引用。必须显式 copy()。"""
    pls = []
    for p in frame.planes:
        stride = p.line_size // 2
        arr = np.frombuffer((ctypes.c_char * p.buffer_size).from_address(
            p.buffer_ptr), dtype="<u2")
        arr = arr.reshape(p.height, stride)[:, :p.width].copy()
        pls.append(arr)
    return pls


def _decode_frames(path: Path, indices: list[int]) -> dict[int, list]:
    want = set(indices)
    out: dict[int, list] = {}
    with av.open(str(path)) as cont:
        for i, frame in enumerate(cont.decode(video=0)):
            if i > max(want):
                break
            if i in want:
                out[i] = _frame_planes(frame.reformat(format="yuv422p10le"))
    missing = want - set(out)
    if missing:
        raise RuntimeError(f"{path.name}: 缺帧 {sorted(missing)}（流长不足）")
    return out


def _upscale2x(planes: list[np.ndarray]) -> list[np.ndarray]:
    return [np.ascontiguousarray(np.repeat(np.repeat(p, 2, 0), 2, 1))
            for p in planes]


def _make_fc(w: int, h: int, qp: int) -> _CFrameConfig:
    fc = _CFrameConfig()
    fc.struct_size = ctypes.sizeof(_CFrameConfig)
    fc.abi_version = TOPOS_CODEC_ABI_VERSION
    fc.visible_width = w
    fc.visible_height = h
    fc.profile = 3            # 422
    fc.pixel_format = 0       # 422 10bit
    fc.bit_depth = 10
    fc.qp_base = qp
    fc.qmatrix_id = 1
    fc.slice_rows = 16        # V7 默认切片（A3 口径：4K 51 链）
    fc.sar_num = fc.sar_den = 1
    fc.reserved[0] = 8        # V7-R2（major=7, entropy_mode=7）
    return fc


def _run_config(codec: ToposCodec, planes: list, w: int, h: int, qp: int,
                tiles: list[int]) -> tuple[int, dict, dict]:
    lib = codec._lib
    fc = _make_fc(w, h, qp)
    data = [np.ascontiguousarray(p).tobytes() for p in planes]
    lib.tc_dev_set_tok_capture(1)
    try:
        pkt, st = codec.encode_frame(fc, data)
        outs = {}
        for tr in tiles:
            o = _CTileSimOut()
            rc = lib.tc_dev_rans2_tile_sim(tr, SEG_BLOCKS, ctypes.byref(o))
            if rc != 0:
                raise RuntimeError(f"tile sim rc={rc} (tile_rows={tr})——"
                                   "A 锚差分失配或无捕获会话")
            outs[tr] = {
                "a_total": int(o.a_total), "a_prefix": int(o.a_prefix),
                "a_stream": int(o.a_stream),
                "b_total": int(o.b_total), "b_table": int(o.b_table),
                "b_stream": int(o.b_stream),
                "n_bands": int(o.n_bands), "n_tiles": int(o.n_tiles),
                "seg_count": int(o.seg_count),
                "seg_dir_bytes": int(o.seg_dir_bytes),
                "seg_state_extra": int(o.seg_state_extra),
                "flags_lvl": list(o.flags_lvl), "flags_dc": list(o.flags_dc),
            }
        st_info = {
            "packet_size": int(st.packet_size),
            "color_payload_bytes": int(st.color_payload_bytes),
            "slice_count": int(st.slice_count),
        }
    finally:
        lib.tc_dev_set_tok_capture(0)
    return len(pkt), outs, st_info


def main() -> int:
    out_path = REPO / "tools/v8_table_loss_2026-09-12.json"
    if len(sys.argv) > 2 and sys.argv[1] == "--out":
        out_path = Path(sys.argv[2])
    codec = ToposCodec()
    _bind_sim(codec._lib)

    records = []
    t0 = time.perf_counter()
    for name, path in SOURCES:
        if not path.exists():
            print(f"跳过（缺素材）：{path}")
            continue
        frames = _decode_frames(path, FRAME_INDICES)
        for fi, planes4k in sorted(frames.items()):
            for res, planes in (("4K", planes4k), ("8K", _upscale2x(planes4k))):
                h, w = planes[0].shape
                for qp in QPS:
                    pkt_bytes, outs, st = _run_config(codec, planes, w, h,
                                                      qp, TILE_ROWS)
                    for tr, o in outs.items():
                        v8 = (o["b_total"] + o["seg_dir_bytes"]
                              + o["seg_state_extra"])
                        rec = {
                            "source": name, "frame": fi, "res": res,
                            "w": w, "h": h, "qp": qp, "tile_rows": tr,
                            "pkt_bytes": pkt_bytes, **o, **st,
                            "loss_stream_pct": 100.0 * (o["b_total"]
                                                        - o["a_total"])
                            / o["a_total"],
                            "loss_v8_pct": 100.0 * (v8 - o["a_total"])
                            / o["a_total"],
                        }
                        records.append(rec)
                    base = records[-len(TILE_ROWS)]
                    print(f"{name} f{fi} {res} qp{qp}: pkt={pkt_bytes/1e6:.2f}MB "
                          f"a={base['a_total']/1e6:.3f}MB "
                          f"b(全平面)={outs[0]['b_total']/1e6:.3f}MB "
                          f"v8(全平面)={base['loss_v8_pct']:+.2f}% "
                          f"[{time.perf_counter()-t0:.0f}s]")
    out_path.write_text(json.dumps(records, indent=1))
    print(f"\nJSON → {out_path}  ({len(records)} 行)")

    # —— 汇总表（按 res×qp×tile_rows 平均） ——
    print("\n== 统计损失汇总（% vs A，负值=更小；V8 口径含段簿记）==")
    print("| 素材帧 | 分辨率 | qp | tile_rows | 纯表源 % | V8 总账 % |")
    print("|---|---|---|---|---|---|")
    key = lambda r: (r["source"], r["frame"], r["res"], r["qp"], r["tile_rows"])
    for r in sorted(records, key=key):
        print(f"| {r['source']} f{r['frame']} | {r['res']} | {r['qp']} "
              f"| {r['tile_rows'] or '平面'} | {r['loss_stream_pct']:+.2f} "
              f"| {r['loss_v8_pct']:+.2f} |")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
