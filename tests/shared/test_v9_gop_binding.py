"""V9 zero-motion IP-2 绑定/编码器/源 三层集成测试（micro-gop 计划批 5）。

- ToposGopContext：GOP 编解码往返、零漂锚（参考精确重复 → recon 逐位
  不变）、P 直启拒绝、no-alpha capability；
- ToposVideoEncoder gop='ip2'：Python 导出 → V9 MOV（bitstream_major=9）；
- ToposMediaSource：V9 顺播 / GOP 内 seek / 跨 GOP seek（帧 0 逐位一致）/
  close 释放。
"""
from __future__ import annotations

import ctypes
import os

import numpy as np
import pytest

from src.shared.codec import topos_binding as tb
from src.shared.codec.topos_binding import ToposCodecError
from src.shared.media.frame import DecodedFrame
from src.shared.media.topos_source import ToposMediaSource

W, H, CWC = 96, 80, 48


def _v9_cfg() -> tb._CFrameConfig:
    cfg = tb._CFrameConfig()
    cfg.struct_size = ctypes.sizeof(tb._CFrameConfig)
    cfg.abi_version = tb.TOPOS_CODEC_ABI_VERSION
    cfg.visible_width, cfg.visible_height = W, H
    cfg.profile, cfg.pixel_format, cfg.bit_depth = 3, 0, 10
    cfg.qmatrix_id, cfg.qp_base = 0, 44
    cfg.color_range = 1
    cfg.color_primaries = 1
    cfg.color_transfer = 1
    cfg.color_matrix = 1
    cfg.reserved[0] = 10
    return cfg


def _frames(n: int = 4):
    """确定性内容：条纹+噪声底帧（I 大）+ 后续帧小扰动（P 残差 = 参考
    重建噪声 + 扰动，远小于 I——闭环 P 增益的真实形态）。"""
    rng = np.random.default_rng(3)
    y0 = ((np.indices((H, W))[1] * 5 % 900 + 60).astype(np.uint16)
          + rng.integers(0, 8, (H, W)).astype(np.uint16))
    u = np.full((H, CWC), 512, np.uint16)
    v = np.full((H, CWC), 512, np.uint16)
    out = [(np.ascontiguousarray(y0), u.copy(), v.copy())]
    for k in range(1, n):
        y = out[0][0].copy()
        y[::3, ::5] = (y[::3, ::5].astype(int) + 4).clip(0, 1023).astype(np.uint16)
        out.append((y, u.copy(), v.copy()))
    return out


class TestToposGopContext:
    def test_roundtrip_and_zero_drift_anchor(self) -> None:
        codec = tb.ToposCodec()
        frames = _frames()
        d0 = [np.zeros((H, W), np.uint16), np.zeros((H, CWC), np.uint16),
              np.zeros((H, CWC), np.uint16)]
        d1 = [a.copy() for a in d0]
        d2 = [a.copy() for a in d0]
        # 解码链用独立 dctx（V9 复审 2026-09-14）：encode 与 feed 共享同一
        # 条参考链——单实例对同一 P 帧交错调用会把残差应用两次。
        with tb.ToposGopContext(codec, _v9_cfg()) as ctx, \
                tb.ToposGopContext(codec, _v9_cfg()) as dctx:
            p0, s0 = ctx.encode_frame(list(frames[0]), force_intra=True)
            p1, s1 = ctx.encode_frame(list(frames[1]))
            assert s0.packet_size > 0
            assert s1.packet_size < s0.packet_size  # 小扰动 P 远小于 I
            i0 = dctx.feed(p0, planes=d0)
            i1 = dctx.feed(p1, planes=d1)
            assert i0.frame_type == 0 and i1.frame_type == 1
            assert i0.gop_id == 1 and i1.gop_id == 1
            assert i1.sample_index == 1
            # 零漂锚：输入 = 解码输出（= 编码器当前参考，逐位）→ 残差恒 0
            # → recon 逐位不变
            p2, s2 = ctx.encode_frame([d1[0], d1[1], d1[2]])
            assert s2.packet_size < s1.packet_size
            dctx.feed(p2, planes=d2)
            assert np.array_equal(d1[0], d2[0])

    def test_p_start_rejected(self) -> None:
        codec = tb.ToposCodec()
        frames = _frames()
        with tb.ToposGopContext(codec, _v9_cfg()) as ctx, \
                tb.ToposGopContext(codec, _v9_cfg()) as ctx2:
            _p0, _ = ctx.encode_frame(list(frames[0]), force_intra=True)
            p1, _ = ctx.encode_frame(list(frames[1]))
            assert p1[15] == 1, "小扰动 P 不应触发 I 回退（±2 在死区下）"
            with pytest.raises(ToposCodecError, match="first frame must be I"):
                ctx2.feed(p1)

    def test_alpha_rejected(self) -> None:
        codec = tb.ToposCodec()
        cfg = _v9_cfg()
        cfg.alpha_mode = 1
        cfg.alpha_bit_depth = 16
        with pytest.raises(ToposCodecError, match="no-alpha"):
            tb.ToposGopContext(codec, cfg)


class TestEncoderV9Export:
    def test_ip2_rejects_aq_rdo(self, tmp_path) -> None:
        """ip2 与 AQ/RDO 显式互斥（V9 复审 2026-09-14：禁静默携带
        reserved[1]/[2] 进 V9 写路径）。"""
        from src.shared.export.topos_encoder import ToposVideoEncoder

        for extra, match in (({"aq_mode": "on"}, "aq_mode"),
                             ({"rdo_mode": "on", "entropy_mode": "v2"}, "rdo_mode")):
            class Cfg:
                width, height, fps = W, H, 25
                profile = 'standard'
                crf = 44
                entropy_mode = 'rans2'
                gop = 'ip2'
                has_alpha = False
                alpha_premultiplied = False
                pix_fmt = 'yuv422p10le'

            for k, v in extra.items():
                setattr(Cfg, k, v)
            with pytest.raises(ValueError, match=match):
                ToposVideoEncoder(str(tmp_path / "bad.mov"), Cfg())

    def test_ip2_export_and_source_roundtrip(self, tmp_path) -> None:
        """导出 → 原生容器校验 → 源顺播/seek（跨 GOP 帧逐位一致）。"""
        from src.shared.export.topos_encoder import ToposVideoEncoder

        class Cfg:
            width, height, fps = W, H, 25
            profile = 'standard'
            crf = 44
            entropy_mode = 'rans2'
            gop = 'ip2'
            has_alpha = False
            alpha_premultiplied = False
            pix_fmt = 'yuv422p10le'

        path = str(tmp_path / "v9_ip2.mov")
        enc = ToposVideoEncoder(path, Cfg())
        enc.open()
        for k, (y, u, v) in enumerate(_frames()):
            enc.encode_frame(DecodedFrame(
                frame_index=k, pts=k, time_seconds=k / 25.0,
                data=None, planes=(y, u, v), pixel_format='yuv422p10le',
                width=W, height=H))
        enc.close()
        assert os.path.getsize(path) > 0

        codec = tb.ToposCodec()
        with tb.ToposMovieFile(codec, path) as movie:
            info = movie.info()
            # ADR-C048：gop='ip2' 载体 = V7-R3（tpcC major=7 与 V7-R2 同，
            # 靠首包熵字节 8 区分）
            assert int(info.bitstream_major) == 7
            pkt0 = movie.packet(0)
            assert pkt0[45] == 8 and pkt0[15] == 0
            assert movie.is_sync(0)
            assert not movie.is_sync(1)
            assert movie.prev_sync(2) == 0

        # 源顺播（GOP context 路径；R3 载体识别 + _gop_pos 顺序定位）
        src = ToposMediaSource(path)
        src.open()
        assert src._stream_compatibility == 'v9_micro_gop'
        assert src._gop_em == 11
        seq = [src.read_next_frame() for _ in range(4)]
        assert src._gop_pos == 3
        assert len(src._gop_cache) <= 1
        # 跨 GOP seek 回 0：重解帧与首播逐位一致（重建参考正确性）
        src.seek_frame(0)
        again = src.read_next_frame()
        assert np.array_equal(np.asarray(seq[0].planes[0]),
                              np.asarray(again.planes[0]))
        # GOP 内 seek
        src.seek_frame(2)
        f2 = src.read_next_frame()
        assert np.array_equal(np.asarray(f2.planes[0]),
                              np.asarray(seq[2].planes[0]))
        src.close()
        assert src._gop_ctx is None
