"""V8 批 4：GPU 段化解码器差分验收（模仿 test_metal_kernels_rans 结构）。

链路：V8 包 → tc_dev_v8_gpu_export（smeta 14×u32/段 + 瓦片表 709×u16，
host 解码器同源解析）→ rans2_decode_segment（每线程一段）→ CSR 显存
原位接 inverse_dct_wsd_v8（per-block 段映射 + per-segment Q）→ 像素；
与 CPU 参考解码（tc_frame_decode → V8 路径）逐值对比。token 全程不离开
显存。

- 端到端位精确 ×4 几何（444 对齐 / 444 非对齐 12bit / 422 色度非对齐 /
  422 段跨行）；
- conceal：瓦片 CRC 字节破坏 → GPU 状态 11 + 段清零 → 与 CPU conceal
  参考逐值一致。
"""
from __future__ import annotations

import ctypes
import sys
from pathlib import Path

import numpy as np
import pytest

REPO = Path(__file__).resolve().parents[4]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from src.shared.codec.topos_binding import (  # noqa: E402
    TOPOS_CODEC_ABI_VERSION, ToposCodec, ToposDecoder, _CFrameConfig,
)
from src.shared.gpu.metal.context import MetalComputeContext  # noqa: E402
from src.shared.gpu.metal.kernels_dec import (  # noqa: E402
    METAL_RANS2_DECODE_SEGMENT_MSL,
)
from src.shared.gpu.metal.kernels_idct import (  # noqa: E402
    METAL_INVERSE_DCT_WSD_V8_MSL,
)
from tests.shared.gpu.metal.test_metal_kernels_idct import (  # noqa: E402
    _bind_qcap, _capture_csr, _frozen_tables, _plane_dims, requires_metal,
)

_SMETA = 14     # 每 segment u32 数
_TABS = 709     # 归一化表 u16 数/tile


def _bind_export(lib) -> None:
    lib.tc_dev_v8_gpu_export.argtypes = [
        ctypes.c_char_p, ctypes.c_size_t,
        ctypes.POINTER(ctypes.c_uint32), ctypes.c_uint32,
        ctypes.POINTER(ctypes.c_uint16), ctypes.c_uint32,
        ctypes.POINTER(ctypes.c_uint32), ctypes.POINTER(ctypes.c_uint32),
        ctypes.POINTER(ctypes.c_size_t),
    ]
    lib.tc_dev_v8_gpu_export.restype = ctypes.c_int32


def _make_fc(w, h, pf, bd, qp=30, v8=True):
    fc = _CFrameConfig()
    fc.struct_size = ctypes.sizeof(_CFrameConfig)
    fc.abi_version = TOPOS_CODEC_ABI_VERSION
    fc.visible_width = w
    fc.visible_height = h
    fc.profile = 5 if pf == 1 else 3
    fc.pixel_format = pf
    fc.bit_depth = bd
    fc.qmatrix_id = 1
    fc.qp_base = qp
    fc.slice_rows = 16
    fc.sar_num = fc.sar_den = 1
    fc.reserved[0] = 9 if v8 else 8
    return fc


def _v8_export(codec: ToposCodec, packet: bytes):
    """tc_dev_v8_gpu_export → (smeta[n,14], tabs[nt,709], ns, nt)。"""
    _bind_export(codec._lib)
    n_seg = ctypes.c_uint32(0)
    n_tile = ctypes.c_uint32(0)
    used = ctypes.c_size_t(0)
    rc = codec._lib.tc_dev_v8_gpu_export(
        packet, len(packet), None, _SMETA, None, _TABS,
        ctypes.byref(n_seg), ctypes.byref(n_tile), ctypes.byref(used))
    assert rc == 0, f"gpu export probe rc={rc}"
    ns, nt = int(n_seg.value), int(n_tile.value)
    smeta = np.zeros((ns, _SMETA), dtype=np.uint32)
    tabs = np.zeros((nt, _TABS), dtype=np.uint16)
    rc = codec._lib.tc_dev_v8_gpu_export(
        packet, len(packet),
        smeta.ctypes.data_as(ctypes.POINTER(ctypes.c_uint32)), _SMETA,
        tabs.ctypes.data_as(ctypes.POINTER(ctypes.c_uint16)), _TABS,
        ctypes.byref(n_seg), ctypes.byref(n_tile), ctypes.byref(used))
    assert rc == 0
    return smeta, tabs, ns, nt


def _qtab_for_qp(codec: ToposCodec, qp: int, bd: int = 10):
    """同 qmatrix/qp/bd 的 Q[64]（经 V7 孪生帧 qcap 捕获——单一真相源）。

    Q 依赖 (qmatrix, qp_eff, bd_offset)——孪生帧必须同 bd（12-bit 用例）。"""
    lib = codec._lib
    _bind_qcap(lib)
    rng = np.random.default_rng(0xB499)
    dims = [(64, 64), (64, 32), (64, 32)]
    planes = [rng.integers(0, 1 << bd, d).astype("<u2") for d in dims]
    packet, _ = codec.encode_frame(_make_fc(64, 64, 0, bd, qp=qp, v8=False),
                                   [p.tobytes() for p in planes])
    _, meta, qtab, _, _, _ = _capture_csr(codec, packet, dims, 1)
    q_luma = qtab[0].copy()
    q_chroma = qtab[1].copy() if len(qtab) > 1 else q_luma.copy()
    return q_luma, q_chroma


def _gpu_v8_decode(ctx, smeta, streams, tabs, ns):
    """流上载 → GPU 段化解码 → CSR（显存 buffers + numpy 读回）。"""
    off_total = int((smeta[:, 2] + 1).sum())
    pairs_total = int((smeta[:, 2] * 63).sum())
    dc_total = int((smeta[:, 10] + smeta[:, 2]).max()) if ns else 1
    bufs = {
        0: ctx.create_buffer_from_numpy(streams),
        1: ctx.create_buffer_from_numpy(smeta),
        2: ctx.create_buffer_from_numpy(tabs),
        3: ctx.create_buffer_from_numpy(np.zeros(max(1, dc_total), np.int32)),
        4: ctx.create_buffer_from_numpy(np.zeros(max(1, off_total), np.uint32)),
        5: ctx.create_buffer_from_numpy(np.zeros(max(1, pairs_total), "<u8")),
        6: ctx.create_buffer_from_numpy(np.zeros(max(1, ns), np.uint32)),
        7: ctx.create_buffer_from_numpy(np.array([ns], np.uint32)),
    }
    k = ctx.compile_kernel("rans2_decode_segment", METAL_RANS2_DECODE_SEGMENT_MSL)
    g = 64
    threads = (((ns + g - 1) // g) * g, 1, 1)
    ctx.dispatch_sync(k, buffers=bufs, threads=threads, group=(g, 1, 1))
    status = np.frombuffer(ctx.read_buffer(bufs[6]), np.uint32)
    return bufs, status


def _gpu_chain(ctx, codec, packet, w, h, pf, bd, dims, qp):
    """全 GPU 链：export → rANS 段解码 → CSR 原位 wsd_v8 → 三平面像素。"""
    smeta, tabs, ns, nt = _v8_export(codec, packet)
    streams = np.frombuffer(packet, np.uint8).copy()
    bufs, status = _gpu_v8_decode(ctx, smeta, streams, tabs, ns)
    _, mtable, wtable = _frozen_tables(codec)
    q_luma, q_chroma = _qtab_for_qp(codec, qp, bd)
    mid, maxv = 1 << (bd - 1), (1 << bd) - 1
    wsd = ctx.compile_kernel("inverse_dct_wsd_v8", METAL_INVERSE_DCT_WSD_V8_MSL)
    out_planes = []
    for p, (ph, pw) in enumerate(dims):
        sel = np.where(smeta[:, 0] == p)[0]
        nseg = len(sel)
        cols = int(smeta[sel[0], 9])
        rows = (ph + 7) // 8
        blkseg = np.zeros(rows * cols, np.uint32)
        sblk0 = np.zeros(nseg, np.uint32)
        soff = np.zeros(nseg, np.uint32)
        spbase = np.zeros(nseg, np.uint32)
        qtab = np.zeros((nseg, 64), np.uint32)
        for si, gi in enumerate(sel):
            seg0 = int(smeta[gi, 1])
            nb = int(smeta[gi, 2])
            sblk0[si] = seg0
            soff[si] = int(smeta[gi, 11])
            spbase[si] = int(smeta[gi, 12])
            blkseg[seg0:seg0 + nb] = si
        qtab[:] = q_luma if p == 0 else q_chroma
        params = ctx.create_buffer_from_numpy(
            np.array([cols, rows, pw, ph, mid, maxv, pw,
                      int(smeta[sel[0], 10])], np.int32))
        out = np.zeros(ph * pw, "<u2")
        chain = {0: bufs[3], 1: bufs[5], 2: bufs[4],
                 3: ctx.create_buffer_from_numpy(out),
                 4: params,
                 5: ctx.create_buffer_from_numpy(mtable),
                 6: ctx.create_buffer_from_numpy(wtable),
                 7: ctx.create_buffer_from_numpy(qtab),
                 8: ctx.create_buffer_from_numpy(blkseg),
                 9: ctx.create_buffer_from_numpy(sblk0),
                 10: ctx.create_buffer_from_numpy(spbase),
                 11: ctx.create_buffer_from_numpy(soff)}
        ctx.dispatch_sync(wsd, buffers=chain,
                          threads=(cols * 8, rows * 8, 1), group=(8, 8, 1))
        got = np.frombuffer(ctx.read_buffer(chain[3]), "<u2").reshape(ph, pw)
        out_planes.append(got.copy())
    return out_planes, status


@requires_metal
class TestGpuV8Decode:

    CASES = [
        (96, 64, 1, 10, 30),    # 444 对齐
        (313, 141, 1, 12, 30),  # 444 非 8 对齐 + 12-bit
        (150, 90, 0, 10, 30),   # 422 色度非对齐
        (128, 96, 0, 10, 44),   # 422 8 块宽（段跨行）+ 高 qp
    ]

    @pytest.mark.parametrize("w,h,pf,bd,qp", CASES)
    def test_gpu_vs_cpu_pixels(self, w, h, pf, bd, qp):
        """GPU rANS→CSR→wsd_v8 像素 vs CPU 参考解码逐值一致。"""
        codec = ToposCodec()
        rng = np.random.default_rng(0xB400 + w + h + pf)
        dims = _plane_dims(pf, w, h)
        planes = [rng.integers(0, 1 << bd, d).astype("<u2") for d in dims]
        packet, _ = codec.encode_frame(_make_fc(w, h, pf, bd, qp),
                                       [p.tobytes() for p in planes])
        ref = self._cpu_reference(codec, packet, w, h, pf, bd)
        ctx = MetalComputeContext()
        got, status = _gpu_chain(ctx, codec, packet, w, h, pf, bd, dims, qp)
        assert int(status.max(initial=0)) == 0, f"status={status}"
        for p in range(3):
            diff = int((got[p] != ref[p]).sum())
            assert diff == 0, f"plane {p}: {diff}/{ref[p].size} 值不符"

    def test_conceal_tile_crc(self):
        """瓦片 CRC 字节破坏 → GPU 状态 11 + 段清零 + 像素 == CPU conceal。"""
        w, h, pf, bd, qp = 150, 90, 0, 10, 30
        codec = ToposCodec()
        rng = np.random.default_rng(0xB402)
        dims = _plane_dims(pf, w, h)
        planes = [rng.integers(0, 1 << bd, d).astype("<u2") for d in dims]
        packet, _ = codec.encode_frame(_make_fc(w, h, pf, bd, qp),
                                       [p.tobytes() for p in planes])
        smeta, tabs, ns, nt = _v8_export(codec, packet)
        # 定位某瓦片的 dir 区偏移 → 破坏该瓦片 CRC：翻 CRC 字本身
        # （CRC 区在每平面载荷尾；简化：翻包尾字节 = 末瓦片 CRC 低位）
        bad = bytearray(packet)
        bad[-1] ^= 0xFF
        bad = bytes(bad)
        # CPU 参考：conceal 生效
        ref = self._cpu_reference(codec, bad, w, h, pf, bd)
        # GPU：受损瓦片 crc_ok=0 → 其段 status 11
        smeta2, tabs2, ns2, nt2 = _v8_export(codec, bad)
        assert int(smeta2[:, 8].min()) == 0, "受损瓦片 crc_ok 必须为 0"
        ctx = MetalComputeContext()
        got, status = _gpu_chain(ctx, codec, bad, w, h, pf, bd, dims, qp)
        bad_segs = np.where(smeta2[:, 8] == 0)[0]
        assert int(status[bad_segs].max()) == 11
        ok_segs = np.where(smeta2[:, 8] != 0)[0]
        assert int(status[ok_segs].max(initial=0)) == 0
        for p in range(3):
            diff = int((got[p] != ref[p]).sum())
            assert diff == 0, f"plane {p}: {diff}/{ref[p].size} 值不符"

    # ---- helpers ----

    def _cpu_reference(self, codec, packet, w, h, pf, bd):
        arrays = [np.zeros(d, "<u2") for d in _plane_dims(pf, w, h)]
        with ToposDecoder(codec) as dec:
            views = dec.make_views(arrays)
            dec.decode_views(packet, views, len(arrays))
        return arrays
