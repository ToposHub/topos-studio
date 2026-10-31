"""阶段 8：Topos 导出接入测试（编码器 / 8 处登记点 / 代理 / 持久化）。

覆盖计划 §12 阶段 8 门禁：
- ToposVideoEncoder duck-type 契约 + packed/planar 双输入 + 色彩映射；
- 取消/失败不留伪成功文件（temp + 原子替换 + abort + 0 帧拒绝）；
- Alpha profile 不被非 Alpha 输出路径静默压平（alpha 直通/丢弃均显式）；
- R-16：8 处登记点逐项断言（枚举/设置映射/容器表/job 映射/目录/
  QuickExport/codec_map/i18n）——遗漏任一处 = UI 选不到或静默回退；
- 工程重开后 codec 参数完整保留（job dict round-trip，schema v3 兼容）；
- TPIC 磁盘代理（自研 decode→encode 链 + factory 自动路由回放）；
- 渲染缓存 topos 档（RenderCacheEncoder 分派）。

C 库未构建时素材用例 skip（构建见 native/topos_codec/README.md）。
"""
from __future__ import annotations

import inspect
import json
import os

import numpy as np
import os
import pytest

pytestmark = pytest.mark.media_core

from src.shared.codec import topos_binding
from src.shared.export.topos_encoder import ToposVideoEncoder

W, H, N = 64, 40, 4


def _lib_available() -> bool:
    import os as _os

    explicit = _os.environ.get("TOPOS_CODEC_LIB")
    if explicit is not None:
        return True
    return any(p.is_file() for p in topos_binding.candidate_library_paths())


@pytest.fixture(scope="module")
def require_lib():
    if not _lib_available():
        pytest.skip("libtopos_codec 未构建；先运行 bash native/topos_codec/run_tests.sh")


def _enc_config(**overrides):
    from src.shared.export.video_encoder import FFmpegEncoderConfig
    from src.shared.media.metadata import Rational

    kwargs = dict(
        codec='topos', width=W, height=H, fps=Rational(24, 1),
        pix_fmt='yuv422p10le', profile='standard', crf=8,
    )
    kwargs.update(overrides)
    return FFmpegEncoderConfig(**kwargs)


def test_retired_entropy_modes_rejected_at_construction(tmp_path) -> None:
    """V 代际收纳（2026-09-13）：退役 entropy_mode 构造期显式 ValueError。

    intra(=V3)/intra-range(=V6)/acpair(=V4)/acpair_table(=V5)/rans(=V7-R)
    写端退役——禁止静默回落（退役 ≠ 回退）；原 intra 导出 roundtrip 宿主
    随代际退役（其场景覆盖由 native 保留代宿主承接）。合法域收缩为
    v1|v2|rans2；域外值同样构造期拒绝。
    """
    retired = ('intra', 'intra-range', 'acpair', 'acpair_table', 'rans')
    for mode in retired:
        with pytest.raises(ValueError, match='retired|退役'):
            ToposVideoEncoder(str(tmp_path / 'invalid.mov'),
                              _enc_config(entropy_mode=mode))
    for bad in ('v3', 'RANS', 'huffman'):
        with pytest.raises(ValueError):
            ToposVideoEncoder(str(tmp_path / 'invalid.mov'),
                              _enc_config(entropy_mode=bad))
    # ''/None = 未设置 → 回落默认 'rans2'（与 aq_mode/rdo_mode 等可选配置
    # 字段同口径，属"缺省"而非"域外退役值"）；显式退役值已在上面拒绝，
    # 大小写变体（'RANS'）经归一化同样命中退役守卫。
    enc = ToposVideoEncoder(str(tmp_path / 'empty-default.mov'),
                            _enc_config(entropy_mode=''))
    assert enc._rans2 and not enc._entropy_v2


def test_rans2_default_stream_is_v7r2(require_lib, tmp_path) -> None:
    """缺省（'rans2'）产物钉死为 V7-R2：帧头 major=7 / entropy=7。"""
    from src.shared.media.frame import DecodedFrame

    path = tmp_path / 'rans2.mov'
    enc = ToposVideoEncoder(str(path), _enc_config(crf=58))
    enc.open()
    pixels = np.zeros((H, W, 3), dtype=np.uint8)
    pixels[:, :, 1] = np.arange(W, dtype=np.uint8)[None, :] * 3
    enc.encode_frame(DecodedFrame(frame_index=0, pts=0, time_seconds=0.0,
                                 data=pixels, pixel_format='bgr24', width=W, height=H))
    enc.close()
    codec = topos_binding.ToposCodec()
    with topos_binding.ToposMovieFile(codec, str(path)) as movie:
        packet = movie.packet(0)
        assert packet[6] == 7  # major=7（V7-R2）
        assert packet[45] == 7  # entropy_mode=7（V7-R2）
        frame = codec.decode(packet)
        assert frame.info.visible_width == W
        assert frame.info.visible_height == H
        assert frame.info.concealed_slices == 0


def _v1_stream_head(out: str, pix_fmt: str, **cfg_overrides) -> bytes:
    """显式 V1（entropy_mode='v1'）编码 1 帧并返回文件头 256 字节。

    T1.5 转正后默认流是 V2（major=2，minor 恒 0——ADR-C027 D-6，
    pf/bd 全枚举开放）；v1.x 扩展代 minor 1/2/3 的 legacy 断言改在
    显式 V1 流上做（位流与历史版本逐字节一致）。
    """
    from src.shared.media.frame import DecodedFrame, PlaneInfo

    w, h = 64, 40
    cw = w // 2 if '422' in pix_fmt else w
    bd = 12 if 'p12' in pix_fmt else 10
    chans = ('G', 'B', 'R') if pix_fmt.startswith('gbr') else ('Y', 'U', 'V')
    planes = tuple(np.zeros((h, cw if i else w), '<u2') for i in range(3))
    infos = tuple(
        PlaneInfo.create(i, cw if i else w, h, chans[i], np.uint16, bd)
        for i in range(3))
    enc = ToposVideoEncoder(out, _enc_config(
        pix_fmt=pix_fmt, entropy_mode='v1', **cfg_overrides))
    enc.open()
    enc.encode_frame(DecodedFrame(
        frame_index=0, pts=0, time_seconds=0.0, planes=planes,
        pixel_format=pix_fmt, width=w, height=h, plane_infos=infos))
    enc.close()
    with open(out, 'rb') as fh:
        return fh.read(256)


def _bgr48_frame(i: int):
    from src.shared.media.frame import DecodedFrame

    bgr = np.zeros((H, W, 3), np.uint16)
    bgr[..., 2] = 30000 + i * 5000   # R 梯度
    bgr[..., 1] = 10000 + i * 2000   # G 梯度
    return DecodedFrame(frame_index=i, pts=i, time_seconds=i / 24,
                        data=bgr, pixel_format='bgr48le', width=W, height=H)


def _open_decoded(path: str):
    from src.shared.media.factory import MediaSourceFactory

    src = MediaSourceFactory.create(path)
    src.open()
    return src


# =============================================================================
# 六档 capability schema（无 lib）。2026-09-21 复查扩：netproxy/raw 一并
# 纳入一致性面（codec_map/面板 catalogue/Quick Export/i18n——raw 此前
# 漏登 deliver_controller codec_map 即因不在本表）。
_TOPOS_CODEC_IDS = ("topos_proxy", "topos_lt", "topos", "topos_hq",
                    "topos_4444", "topos_4444xq", "topos_lp",
                    "topos_netproxy", "topos_raw")
_NO_ALPHA_CODECS = ("topos_lp", "topos_raw")


class TestToposProfiles:
    def test_seven_tiers_all_available(self) -> None:
        # M4-R7 落地：'raw' 编码链（CFA 平面直入 + GOP intra 强制）已激活
        # ——九档全部可用（帧内六档 + netproxy + lp + raw）
        from src.shared.codec.topos_profiles import TOPOS_PROFILE_TIERS
        assert TOPOS_PROFILE_TIERS["raw"].available is True
        assert TOPOS_PROFILE_TIERS["raw"].native_profile == 7
        """v1.8（ADR-C056）：帧内六档 + lp 帧间档；M4-R7 激活 raw。"""
        from src.shared.codec.topos_profiles import (
            AVAILABLE_TOPOS_TIERS,
            TOPOS_PROFILE_TIERS,
            TOPOS_TIER_IDS,
            available_topos_tiers,
            topos_capability_summary,
        )

        assert set(TOPOS_PROFILE_TIERS) == {
            "netproxy", "proxy", "lt", "standard", "hq", "4444", "4444xq",
            "lp", "raw",
        }
        # R4.4：帧内六档全部可用（审计 R4 完成门槛）；v1.8 追加 lp；
        # v1.9 P2 追加 netproxy（网络代理，非 ProRes 对标）；
        # M4-R7 激活 raw（比率锚经 crf 驱动，RAW→RAW 直出）
        assert AVAILABLE_TOPOS_TIERS == (
            "netproxy", "proxy", "lt", "standard", "hq", "4444", "4444xq",
            "lp", "raw")
        labels = [t.label for t in available_topos_tiers() if t.available]
        assert labels == ["Topos 422 Net Proxy", "Topos 422 Proxy",
                          "Topos 422 LT", "Topos 422", "Topos 422 HQ",
                          "Topos 4444", "Topos 4444 XQ", "Topos 422 LP",
                          "Topos RAW"]

        for tier_id, prof in (("netproxy", 3), ("proxy", 3), ("lt", 3),
                              ("standard", 3), ("hq", 3), ("4444", 5),
                              ("4444xq", 6), ("lp", 3)):
            tier = TOPOS_PROFILE_TIERS[tier_id]
            assert tier.available is True
            assert tier.native_profile == prof

        # v1.9 P2：netproxy 保留旧 Proxy 低码率；新 Proxy 重标定为
        # ProRes 容量上限下的默认目标（同母版实测 bpp 取紧端 ×0.98×0.95）
        assert TOPOS_PROFILE_TIERS["netproxy"].target_bpp == 0.29108
        assert TOPOS_PROFILE_TIERS["proxy"].target_bpp == 0.64483
        assert TOPOS_PROFILE_TIERS["lt"].target_bpp == 1.35234
        assert TOPOS_PROFILE_TIERS["standard"].target_bpp == 2.11179
        assert TOPOS_PROFILE_TIERS["hq"].target_bpp == 3.15075

        # v1.8 lp 档能力收缩（显式声明，不猜测）
        lp = TOPOS_PROFILE_TIERS["lp"]
        assert TOPOS_TIER_IDS["lp"] == 7
        assert lp.alpha_supported is False
        assert lp.anchor_qp == 72
        assert lp.target_bpp is None          # 无码控（固定锚 qp）
        assert lp.frame_target_bytes(1920, 1080) is None
        assert lp.is_rate_controlled is False
        assert lp.bit_depths == (10,)

        summary = topos_capability_summary()
        # v1.1 音频轨（container_spec_v1.1；仅时间线导出携带——渲染缓存/
        # 代理纯视频的 ADR-C008 语义不变）
        assert summary["standard"]["audio"]["supported"] is True
        assert set(summary["standard"]["audio"]["codecs"]) == {
            "pcm_s16", "pcm_s24", "pcm_s32", "pcm_f32", "aac"}
        assert set(summary["standard"]["audio"]["layouts"]) == {
            "1.0", "2.0", "5.1", "7.1"}  # v1.6 mono stems
        assert set(summary["standard"]["audio"]["sample_rates"]) == {
            44100, 48000, 88200, 96000, 176400, 192000}
        assert summary["standard"]["alpha_supported"] is True
        assert summary["hq"]["target_bpp"] > summary["lt"]["target_bpp"]
        assert summary["lp"]["alpha_supported"] is False
        assert summary["lp"]["anchor_qp"] == 72

    def test_frame_target_bytes_scales_with_resolution(self) -> None:
        from src.shared.codec.topos_profiles import get_topos_tier

        std = get_topos_tier("standard")
        t1080 = std.frame_target_bytes(1920, 1080)
        t_half = std.frame_target_bytes(960, 540)
        assert t1080 > 0 and t_half > 0
        assert abs(t1080 / t_half - 4.0) < 0.01   # 面积线性
        # v1.9 P2 ProRes 容量重标定：standard 1.503→2.112 bpp（同母版
        # ProRes 实测 bpp 取紧端 ×0.98×0.95）⇒ 1080p25 ≈ 13.1 MB/s
        rate_mb_s = t1080 * 25 / (1024 * 1024)
        assert 12.5 <= rate_mb_s <= 13.5

    def test_unknown_tier_raises(self) -> None:
        from src.shared.codec.topos_profiles import get_topos_tier

        with pytest.raises(KeyError, match="Unknown Topos profile tier"):
            get_topos_tier("ultra")


# =============================================================================
# ToposVideoEncoder（lib）
# =============================================================================


class TestToposVideoEncoder:
    def test_packed_bgr48_roundtrip_and_metadata(self, require_lib, tmp_path) -> None:
        out = str(tmp_path / "packed.mov")
        enc = ToposVideoEncoder(out, _enc_config())
        assert enc.is_open is False and enc.frame_count == 0
        enc.open()
        for i in range(N):
            enc.encode_frame(_bgr48_frame(i))
            assert enc.get_progress() == float(i + 1)
        # 原子性：close 前目标文件不存在，临时文件存在
        assert not os.path.exists(out)
        assert os.path.exists(out + ".topos-tmp")
        enc.close()
        assert enc.is_open is False

        src = _open_decoded(out)
        try:
            md = src.probe()
            assert (md.width, md.height) == (W, H)
            assert md.total_frames == N
            assert md.codec_name == "topos"
            assert md.pixel_format == "yuv422p10le"
            assert md.color_range == "limited"
            assert str(md.fps) == "24/1"
            f0, f2 = src.read_frame(0), src.read_frame(2)
            # R 梯度递增 → Y 递增（packed 转换方向正确）
            assert f2.planes[0][0, 0] > f0.planes[0][0, 0]
        finally:
            src.close()

    def test_aq_mode_default_off_optin_changes_bytes(
            self, require_lib, tmp_path) -> None:
        """V2.x AQ：默认 off（位流与显式 off 逐字节一致）；aq_mode='on'
        时色度带偏移改变字节——固定 qp（crf）路径同样生效（2026-09-03
        修复：此前偏移仅 sized/m7 路径计算，plain 编码静默关闭 AQ）。
        slice_rows=2 保证 ≥2 带，偏移实际触发。"""
        from src.shared.media.frame import DecodedFrame, PlaneInfo

        w, h = 64, 40
        cw = w // 2
        planes = []
        for idx in range(3):
            width = cw if idx else w
            p = np.full((h, width), 512, '<u2')
            if idx:  # 色度上半忙（中等幅度全频谱）/下半平 → 带间活动度差
                for y in range(h // 2):
                    for x in range(width):
                        p[y, x] = 512 + (((x * 7 + y * 13) % 64) - 32) * 8
            planes.append(p)
        infos = tuple(
            PlaneInfo.create(i, cw if i else w, h, c, np.uint16, 10)
            for i, c in enumerate(('Y', 'U', 'V')))

        def _encode(out: str, **ov) -> bytes:
            enc = ToposVideoEncoder(out, _enc_config(crf=30, **ov),
                                    slice_rows=2)
            enc.open()
            enc.encode_frame(DecodedFrame(
                frame_index=0, pts=0, time_seconds=0.0, planes=tuple(planes),
                pixel_format='yuv422p10le', width=w, height=h,
                plane_infos=infos))
            enc.close()
            with open(out, 'rb') as fh:
                return fh.read()

        with pytest.raises(ValueError, match="aq_mode"):
            ToposVideoEncoder(str(tmp_path / "bad.mov"),
                              _enc_config(aq_mode="auto"))

        b_default = _encode(str(tmp_path / "def.mov"))
        b_off = _encode(str(tmp_path / "off.mov"), aq_mode='off')
        b_on = _encode(str(tmp_path / "on.mov"), aq_mode='on')
        assert b_default == b_off            # 默认关 = 历史逐字节一致
        assert len(b_on) != len(b_off)       # plain 固定 qp 路径 AQ 生效

    def test_rdo_mode_default_off_optin_changes_bytes(
            self, require_lib, tmp_path) -> None:
        """V2.x RDO：默认 off（位流与显式 off 逐字节一致）；rdo_mode='on'
        改变字节（固定 qp 与 sized 路径一致生效——reserved[2]）；V1 熵
        下拒绝；非法值构造期拒绝。"""
        from src.shared.media.frame import DecodedFrame, PlaneInfo

        w, h = 64, 40
        cw = w // 2
        # 全频谱伪随机纹理（固定种子，确定性）：大量中小 level——精修有
        # 候选可动。P5.1 flat 矩阵下原算术条纹图案恰无候选可动（RDO
        # no-op），改用种子噪声保证区分度（真实素材上 flat 下 RDO 仍
        # +0.13% 字节差，功能本身不受矩阵切换影响）
        rng = np.random.default_rng(20260910)
        planes = []
        for idx in range(3):
            width = cw if idx else w
            p = (rng.integers(0, 256, size=(h, width)) + 384).astype('<u2')
            planes.append(p)
        infos = tuple(
            PlaneInfo.create(i, cw if i else w, h, c, np.uint16, 10)
            for i, c in enumerate(('Y', 'U', 'V')))

        def _encode(out: str, **ov) -> bytes:
            # P5.1 flat 矩阵下 RDO 精修的咬合点随内容逐 qp 波动（46/50/52
            # 有字节差，44/48 恰净零）——取 50（实测 ±2 字节差）。
            # ADR-C035：RDO 码率模型基于 V2 VLC——本测试域显式 v2
            ov.setdefault('entropy_mode', 'v2')
            enc = ToposVideoEncoder(out, _enc_config(crf=50, **ov),
                                    slice_rows=2)
            enc.open()
            enc.encode_frame(DecodedFrame(
                frame_index=0, pts=0, time_seconds=0.0, planes=tuple(planes),
                pixel_format='yuv422p10le', width=w, height=h,
                plane_infos=infos))
            enc.close()
            with open(out, 'rb') as fh:
                return fh.read()

        with pytest.raises(ValueError, match="rdo_mode"):
            ToposVideoEncoder(str(tmp_path / "bad.mov"),
                              _enc_config(rdo_mode="auto"))
        with pytest.raises(ValueError, match="entropy_mode='v2'"):
            ToposVideoEncoder(str(tmp_path / "v1rdo.mov"),
                              _enc_config(entropy_mode='v1', rdo_mode='on'))

        b_default = _encode(str(tmp_path / "def.mov"))
        b_off = _encode(str(tmp_path / "off.mov"), rdo_mode='off')
        b_on = _encode(str(tmp_path / "on.mov"), rdo_mode='on')
        assert b_default == b_off            # 默认关 = 历史逐字节一致
        assert len(b_on) != len(b_off)       # 固定 qp 路径 RDO 生效

    def test_packed_bgr8_roundtrip(self, require_lib, tmp_path) -> None:
        from src.shared.media.frame import DecodedFrame

        out = str(tmp_path / "packed8.mov")
        enc = ToposVideoEncoder(out, _enc_config())
        enc.open()
        bgr = np.zeros((H, W, 3), np.uint8)
        bgr[..., 2] = 180   # R 主导
        enc.encode_frame(DecodedFrame(frame_index=0, pts=0, time_seconds=0.0,
                                      data=bgr, pixel_format='bgr24',
                                      width=W, height=H))
        enc.close()
        src = _open_decoded(out)
        try:
            f = src.read_frame(0)
            # 8-bit R=180 → RGB 0.706 → BT.709 luma = 0.2126*0.706 ≈ 0.15
            # → limited Y ≈ 0.15*876+64 ≈ 195（纯红的亮度只由 Kr 加权）
            y_val = int(f.planes[0][0, 0])
            assert 150 < y_val < 250, f"Y={y_val}"
        finally:
            src.close()

    def test_planar_passthrough_quality(self, require_lib, tmp_path) -> None:
        from src.shared.media.frame import DecodedFrame, PlaneInfo

        cw = W // 2
        y = np.tile(np.linspace(100, 900, W, dtype='<u2'), (H, 1))
        u = np.full((H, cw), 400, '<u2')
        v = np.full((H, cw), 600, '<u2')
        out = str(tmp_path / "planar.mov")
        enc = ToposVideoEncoder(out, _enc_config(profile='hq', crf=4))
        enc.open()
        infos = (PlaneInfo.create(0, W, H, 'Y', np.uint16, 10),
                 PlaneInfo.create(1, cw, H, 'U', np.uint16, 10),
                 PlaneInfo.create(2, cw, H, 'V', np.uint16, 10))
        enc.encode_frame(DecodedFrame(
            frame_index=0, pts=0, time_seconds=0.0,
            planes=(y, u, v), pixel_format='yuv422p10le',
            width=W, height=H, plane_infos=infos))
        enc.close()

        src = _open_decoded(out)
        try:
            f = src.read_frame(0)
            # 低 QP 近无损：梯度重建误差有界
            err = float(np.max(np.abs(f.planes[0].astype(np.int32) - y.astype(np.int32))))
            assert err < 24, f"Y passthrough err={err}"
            assert f.planes[1].shape == (H, cw)
        finally:
            src.close()

    def test_alpha_passthrough_not_flattened(self, require_lib, tmp_path) -> None:
        from src.shared.media.frame import DecodedFrame, PlaneInfo

        cw = W // 2
        y = np.full((H, W), 500, '<u2')
        u = np.full((H, cw), 400, '<u2')
        v = np.full((H, cw), 600, '<u2')
        a = (np.arange(W)[None, :] * 4095 // (W - 1)).astype('<u2')
        a = np.repeat(a, H, axis=0)

        out = str(tmp_path / "alpha.mov")
        enc = ToposVideoEncoder(out, _enc_config(
            pix_fmt='topos_yuva422p10a12', profile='hq', crf=6, has_alpha=True))
        enc.open()
        infos = (PlaneInfo.create(0, W, H, 'Y', np.uint16, 10),
                 PlaneInfo.create(1, cw, H, 'U', np.uint16, 10),
                 PlaneInfo.create(2, cw, H, 'V', np.uint16, 10),
                 PlaneInfo.create(3, W, H, 'A', np.uint16, 12))
        enc.encode_frame(DecodedFrame(
            frame_index=0, pts=0, time_seconds=0.0,
            planes=(y, u, v, a), pixel_format='topos_yuva422p10a12',
            width=W, height=H, plane_infos=infos))
        enc.close()

        src = _open_decoded(out)
        try:
            md = src.probe()
            assert md.has_alpha is True
            assert md.pixel_format == "topos_yuva422p10a12"
            f = src.read_frame(0)
            assert len(f.planes) == 4
            # mode2 a12 受限近似：误差有界（远小于满刻度）
            err = float(np.max(np.abs(
                f.planes[3].astype(np.int32) - a.astype(np.int32))))
            assert err <= 64, f"alpha passthrough err={err}"
            # 半透明区不压平：中列 alpha ≈ 中值
            mid = float(f.planes[3][H // 2, W // 2])
            assert 1400 < mid < 2700
        finally:
            src.close()

    def test_alpha_plane_dropped_explicitly_without_flag(self, require_lib, tmp_path, caplog) -> None:
        from src.shared.media.frame import DecodedFrame, PlaneInfo

        cw = W // 2
        planes = (np.full((H, W), 500, '<u2'),
                  np.full((H, cw), 400, '<u2'),
                  np.full((H, cw), 600, '<u2'),
                  np.full((H, W), 3000, '<u2'))
        out = str(tmp_path / "noalpha.mov")
        enc = ToposVideoEncoder(out, _enc_config())   # has_alpha=False
        enc.open()
        infos = tuple(PlaneInfo.create(i, *s, c, np.uint16, 10)
                      for i, (s, c) in enumerate(
                          [((W, H), 'Y'), ((cw, H), 'U'), ((cw, H), 'V'), ((W, H), 'A')]))
        enc.encode_frame(DecodedFrame(
            frame_index=0, pts=0, time_seconds=0.0, planes=planes,
            pixel_format='topos_yuva422p10a12', width=W, height=H,
            plane_infos=infos))
        enc.close()

        src = _open_decoded(out)
        try:
            assert src.probe().has_alpha is False   # 显式丢弃（日志警告），非静默压平
        finally:
            src.close()

    def test_abort_and_zero_frame_leave_no_output(self, require_lib, tmp_path) -> None:
        out = str(tmp_path / "cancel.mov")
        enc = ToposVideoEncoder(out, _enc_config())
        enc.open()
        enc.encode_frame(_bgr48_frame(0))
        enc.abort()
        assert not os.path.exists(out)
        assert not os.path.exists(out + ".topos-tmp")

        # 0 帧定稿：拒绝产出空文件
        out2 = str(tmp_path / "empty.mov")
        enc2 = ToposVideoEncoder(out2, _enc_config())
        enc2.open()
        with pytest.raises(RuntimeError, match="没有任何帧"):
            enc2.close()
        assert not os.path.exists(out2)
        assert not os.path.exists(out2 + ".topos-tmp")

    def test_audio_explicitly_ignored(self, require_lib, tmp_path, caplog) -> None:
        out = str(tmp_path / "noaudio.mov")
        enc = ToposVideoEncoder(out, _enc_config())
        enc.open()
        enc.add_audio_stream(codec="aac", sample_rate=48000, channels=2)
        enc.write_audio_samples(np.zeros((2, 1024), np.float32))
        enc.encode_frame(_bgr48_frame(0))
        enc.close()
        assert os.path.exists(out)

    def test_unsupported_color_fails_loudly(self, require_lib, tmp_path) -> None:
        from src.shared.export.video_encoder import ColorMetadataConfig

        cfg = _enc_config(color_metadata=ColorMetadataConfig(
            primaries='bt709', transfer='bt709', matrix='bt709', range='limited'))
        # 构造未知 primaries 的配置（duck-type 覆盖）
        object.__setattr__(cfg.color_metadata, 'primaries', 'weird')
        enc = ToposVideoEncoder(str(tmp_path / "bad.mov"), cfg)
        with pytest.raises(RuntimeError, match="primaries"):
            enc.open()

    def test_tier_format_constraint_and_qp_range(self, require_lib, tmp_path) -> None:
        """R4.4：档位全可用，但格式交叉在配置期拦截（Pro444 拒 4:2:2）。"""
        with pytest.raises(ValueError, match="4:4:4"):
            ToposVideoEncoder(str(tmp_path / "x.mov"),
                              _enc_config(profile='4444'))
        with pytest.raises(ValueError, match="0-95"):   # v1.5 qp 域（ADR-C031）
            ToposVideoEncoder(str(tmp_path / "y.mov"), _enc_config(crf=96))

    def test_rate_controlled_default_tier(self, require_lib, tmp_path) -> None:
        # 不给 crf → 档位 bpp 帧级 sized 搜索（默认路径）
        from src.shared.export.video_encoder import FFmpegEncoderConfig
        from src.shared.media.metadata import Rational

        cfg = FFmpegEncoderConfig(codec='topos', width=W, height=H,
                                  fps=Rational(24, 1), pix_fmt='yuv422p10le',
                                  profile='proxy')
        assert cfg.crf is None
        out = str(tmp_path / "sized.mov")
        enc = ToposVideoEncoder(out, cfg)
        enc.open()
        for i in range(2):
            enc.encode_frame(_bgr48_frame(i))
        enc.close()
        src = _open_decoded(out)
        try:
            assert src.probe().total_frames == 2
        finally:
            src.close()

    def test_vfr_fps_roundtrip_30000_1001(self, require_lib, tmp_path) -> None:
        from src.shared.media.metadata import Rational

        enc = ToposVideoEncoder(
            str(tmp_path / "vfr.mov"),
            _enc_config(fps=Rational(30000, 1001)))
        enc.open()
        enc.encode_frame(_bgr48_frame(0))
        enc.close()
        src = _open_decoded(str(tmp_path / "vfr.mov"))
        try:
            assert str(src.probe().fps) == "30000/1001"
        finally:
            src.close()


# =============================================================================
# R-16：8 处登记点逐项断言（无 lib）
# =============================================================================


class TestRegistrationPoints:
    """遗漏任一处 = UI 选不到 topos 或静默回退其它 codec。"""

    def test_export_codec_enum(self) -> None:
        from src.features.edit.core.timeline_export import ExportCodec

        assert ExportCodec.TOPOS_PROXY.value == "topos_proxy"
        assert ExportCodec.TOPOS_LT.value == "topos_lt"
        assert ExportCodec.TOPOS.value == "topos"
        assert ExportCodec.TOPOS_HQ.value == "topos_hq"
        # R4.6：4444/4444XQ 独立 bitstream profile 进导出枚举（对齐 ProRes 命名）
        assert ExportCodec.TOPOS_4444.value == "topos_4444"
        assert ExportCodec.TOPOS_4444XQ.value == "topos_4444xq"

    def test_codec_settings_map(self) -> None:
        from src.features.edit.core.timeline_export import (
            ExportCodec,
            TimelineExporter,
        )

        for member, tier in ((ExportCodec.TOPOS_PROXY, "proxy"),
                             (ExportCodec.TOPOS_LT, "lt"),
                             (ExportCodec.TOPOS, "standard"),
                             (ExportCodec.TOPOS_HQ, "hq")):
            cfg = type("C", (), {"codec": member})()
            codec, pix_fmt, profile = TimelineExporter._get_codec_settings(object(), cfg)
            assert codec == "topos"
            assert pix_fmt == "yuv422p10le"
            assert profile == tier
        # R4.6：444 档映射 4:4:4 pix_fmt（tier 格式交叉的 UI 侧默认值）
        for member, tier, fmt in ((ExportCodec.TOPOS_4444, "4444",
                                   "yuv444p10le"),
                                  (ExportCodec.TOPOS_4444XQ, "4444xq",
                                   "yuv444p12le")):
            cfg = type("C", (), {"codec": member})()
            codec, pix_fmt, profile = TimelineExporter._get_codec_settings(object(), cfg)
            assert codec == "topos"
            assert pix_fmt == fmt
            assert profile == tier

    def test_create_encoder_dispatch(self, tmp_path) -> None:
        from src.features.edit.core.timeline_export import TimelineExporter
        from src.shared.export.video_encoder import FFmpegVideoEncoder

        top = TimelineExporter._create_encoder(
            object(), str(tmp_path / "t.mov"), _enc_config())
        assert isinstance(top, ToposVideoEncoder)

        from src.shared.export.video_encoder import FFmpegEncoderConfig
        from src.shared.media.metadata import Rational

        ff = TimelineExporter._create_encoder(
            object(), str(tmp_path / "p.mov"),
            FFmpegEncoderConfig(codec='prores', width=64, height=40,
                                fps=Rational(24, 1)))
        assert isinstance(ff, FFmpegVideoEncoder)

    def test_container_table(self) -> None:
        from src.features.deliver.core.video_export_formats import (
            CODEC_CONTAINER_TABLE,
            allowed_codecs_for_container,
            allowed_containers_for_codec,
        )

        for cid in _TOPOS_CODEC_IDS:
            assert cid in CODEC_CONTAINER_TABLE
            assert allowed_containers_for_codec(cid) == frozenset({"mov"})
        assert "topos" in allowed_codecs_for_container("mov")
        assert "topos" not in allowed_codecs_for_container("mp4")
        assert "topos" not in allowed_codecs_for_container("mxf")

    def test_job_codec_mapping(self) -> None:
        from src.features.deliver.core.video_export_job import _map_codec
        from src.features.edit.core.timeline_export import ExportCodec

        assert _map_codec("topos") is ExportCodec.TOPOS
        assert _map_codec("topos_hq") is ExportCodec.TOPOS_HQ
        assert _map_codec("topos_proxy") is ExportCodec.TOPOS_PROXY
        assert _map_codec("topos_lt") is ExportCodec.TOPOS_LT
        # 复验 P1-08：六档全映射 + 未知 codec 显式失败
        assert _map_codec("topos_4444") is ExportCodec.TOPOS_4444
        assert _map_codec("topos_4444xq") is ExportCodec.TOPOS_4444XQ
        # v1.8（ADR-C056）：lp 帧间档
        assert _map_codec("topos_lp") is ExportCodec.TOPOS_LP
        with pytest.raises((ValueError, KeyError)):
            _map_codec("definitely_not_a_codec")

    def test_settings_panel_catalogue(self) -> None:
        from src.features.deliver.ui.video_export_settings_panel import _CODEC_CATALOGUE

        entries = {e["codec"]: e for e in _CODEC_CATALOGUE}
        for cid in _TOPOS_CODEC_IDS:
            e = entries[cid]
            assert e["container"] == "mov"
            # v1.8（ADR-C056）：lp 帧间档显式 no-alpha（UI 隐藏 Alpha 控件）；
            # raw（M4-R8）与 alpha 互斥（TRAW 契约），其余档全档可选携带
            assert e["alpha"] is (cid not in _NO_ALPHA_CODECS)
            assert e["crf"] is False        # 档位自带码控/固定锚，UI 不暴露 CRF

    def test_quick_export_container_codecs(self) -> None:
        from src.shared.export.quick_export_dialog import QuickExportDialog

        # AUD-33301（锁定旧行为协议改锚）：_CONTAINER_CODECS 硬编码副本
        # 已删除，快速导出容器编码表改为从 deliver 的
        # CODEC_CONTAINER_TABLE 派生（单一真相源）。本断言的意图不变：
        # topos 档在 Quick Export 的 mov 容器可达，且绝不进入 mp4/mkv/mxf。
        table = {cont: QuickExportDialog._container_codec_ids(cont)
                 for cont in ("mp4", "mov", "mkv", "mxf")}
        for cid in _TOPOS_CODEC_IDS:
            assert cid in table["mov"], f"Quick Export 缺 {cid}（复验 P1-08）"
        for cont in ("mp4", "mkv", "mxf"):
            assert "topos" not in table.get(cont, [])

    def test_deliver_controller_codec_map(self) -> None:
        from src.app.controllers import deliver_controller

        src_text = inspect.getsource(
            deliver_controller.DeliverControllerMixin.perform_video_export)
        for cid in _TOPOS_CODEC_IDS:
            assert f'"{cid}"' in src_text, f"perform_video_export 缺 {cid}"

    def test_i18n_keys(self) -> None:
        root = os.path.dirname(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))))
        for loc in ("zh_CN", "en_US"):
            path = os.path.join(root, "src", "shared", "i18n", "locales", f"{loc}.json")
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
            exp = data["export"]
            for key in ("codec_topos", "codec_topos_lt",
                        "codec_topos_hq", "codec_topos_proxy",
                        "codec_topos_4444", "codec_topos_4444xq",
                        "codec_topos_lp", "codec_topos_netproxy",
                        "codec_topos_raw"):
                assert key in exp and exp[key], f"{loc} 缺 {key}"

    def test_job_persistence_roundtrip(self) -> None:
        from src.features.deliver.core.video_export_job import VideoConfig, VideoExportJob

        job = VideoExportJob()
        job.video_config = VideoConfig(
            container="mov", codec="topos_hq", alpha=True,
            extra={"topos_note": "mezzanine"})
        cfg = job.to_timeline_export_config()
        assert cfg.codec.value == "topos_hq"
        assert cfg.has_alpha is True          # alpha 转发（此前只持久化不消费）

        data = job.to_dict()
        restored = VideoExportJob.from_dict(data)
        assert restored.video_config.codec == "topos_hq"
        assert restored.video_config.alpha is True
        assert restored.video_config.extra == {"topos_note": "mezzanine"}

    def test_render_cache_codec_parse_and_dispatch(self, require_lib, tmp_path) -> None:
        from src.features.edit.core.render_cache_encoder import (
            RenderCacheEncoder,
            RenderCacheEncoderConfig,
        )

        codec, profile, pix_fmt = RenderCacheEncoder._parse_codec("topos")
        assert (codec, profile, pix_fmt) == ("topos", "standard", "yuv422p10le")

        cfg = RenderCacheEncoderConfig(
            output_path=str(tmp_path / "cache.mov"),
            temp_path=str(tmp_path / "cache.tmp"),
            width=W, height=H, codec="topos")
        enc = RenderCacheEncoder(cfg)
        enc.open()
        assert enc._encoder is not None and isinstance(enc._encoder, ToposVideoEncoder)
        enc.write_frame_bgr(np.full((H, W, 3), 128, np.uint8))
        assert enc.close() is True
        assert os.path.exists(cfg.output_path)

        from src.shared.media.topos_source import is_topos_movie
        assert is_topos_movie(cfg.output_path)


# =============================================================================
# TPIC 磁盘代理（lib）
# =============================================================================


class TestToposProxyChain:
    def _make_source(self, path: str, w: int = 96, h: int = 64, n: int = 5) -> None:
        from src.shared.export.video_encoder import FFmpegEncoderConfig
        from src.shared.media.frame import DecodedFrame
        from src.shared.media.metadata import Rational

        enc = ToposVideoEncoder(path, FFmpegEncoderConfig(
            codec='topos', width=w, height=h, fps=Rational(24, 1),
            pix_fmt='yuv422p10le', profile='standard', crf=6))
        enc.open()
        for i in range(n):
            bgr = np.full((h, w, 3), 20000 + i * 3000, np.uint16)
            enc.encode_frame(DecodedFrame(
                frame_index=i, pts=i, time_seconds=i / 24, data=bgr,
                pixel_format='bgr48le', width=w, height=h))
        enc.close()

    def test_topos_source_proxy_generated_and_playable(self, require_lib, tmp_path) -> None:
        from src.shared.media.proxy import DiskProxyConfig
        from src.shared.media.proxy_generator import ProxyGenerator
        from src.shared.media.topos_source import is_topos_movie

        src_path = str(tmp_path / "source.mov")
        self._make_source(src_path)

        gen = ProxyGenerator(DiskProxyConfig())
        result = gen.generate(src_path)
        assert result.success, result.error_message
        assert result.proxy_path and os.path.exists(result.proxy_path)
        # 代理同为 TPIC → 播放侧 factory 内容嗅探自动路由（零改动）
        assert is_topos_movie(result.proxy_path)

        s = _open_decoded(result.proxy_path)
        try:
            md = s.probe()
            assert (md.width, md.height) == (96, 64)
            assert md.total_frames == 5
            assert md.codec_name == "topos"
            f = s.read_frame(2)
            assert len(f.planes) == 3 and f.planes[0].dtype == np.uint16
        finally:
            s.close()

    def test_proxy_downscales_to_target_height(self, require_lib, tmp_path) -> None:
        from src.shared.media.proxy import DiskProxyConfig, ProxyResolution
        from src.shared.media.proxy_generator import ProxyGenerator

        src_path = str(tmp_path / "big.mov")
        self._make_source(src_path, w=320, h=240, n=3)

        cfg = DiskProxyConfig()
        cfg.target_height = 120   # 320x240 → 160x120
        gen = ProxyGenerator(cfg)
        result = gen.generate(src_path)
        assert result.success, result.error_message
        s = _open_decoded(result.proxy_path)
        try:
            assert (s.probe().width, s.probe().height) == (160, 120)
        finally:
            s.close()

    def test_non_topos_garbage_source_fails_cleanly(self, require_lib, tmp_path) -> None:
        from src.shared.media.proxy import DiskProxyConfig
        from src.shared.media.proxy_generator import ProxyGenerator

        bogus = str(tmp_path / "bogus.mov")
        with open(bogus, "wb") as fh:
            fh.write(b"not a movie at all" * 64)
        result = ProxyGenerator(DiskProxyConfig()).generate(bogus)
        assert result.success is False
        assert result.error_message


# =============================================================================
# ExportFrameProvider + ToposVideoEncoder 直通导出（alpha 全链，lib）
# =============================================================================


class TestPassthroughExport:
    def test_frame_provider_to_encoder_alpha_roundtrip(self, require_lib, tmp_path) -> None:
        """decode → ExportFrameProvider → ToposVideoEncoder → decode：Alpha 不丢。"""
        from src.shared.export.frame_provider import ExportFrameProvider
        from src.shared.export.video_encoder import FFmpegEncoderConfig
        from src.shared.media.frame import DecodedFrame, PlaneInfo
        from src.shared.media.metadata import Rational

        # 源：含 alpha 的 TPIC
        src_path = str(tmp_path / "src_alpha.mov")
        cw = 32
        y = np.tile(np.linspace(200, 800, 64, dtype='<u2'), (40, 1))
        u = np.full((40, cw), 400, '<u2')
        v = np.full((40, cw), 600, '<u2')
        a = (np.arange(64)[None, :] * 4095 // 63).astype('<u2')
        a = np.repeat(a, 40, axis=0)

        enc = ToposVideoEncoder(src_path, FFmpegEncoderConfig(
            codec='topos', width=64, height=40, fps=Rational(24, 1),
            pix_fmt='topos_yuva422p10a12', profile='hq', crf=6, has_alpha=True))
        enc.open()
        infos = (PlaneInfo.create(0, 64, 40, 'Y', np.uint16, 10),
                 PlaneInfo.create(1, 32, 40, 'U', np.uint16, 10),
                 PlaneInfo.create(2, 32, 40, 'V', np.uint16, 10),
                 PlaneInfo.create(3, 64, 40, 'A', np.uint16, 12))
        for i in range(3):
            enc.encode_frame(DecodedFrame(
                frame_index=i, pts=i, time_seconds=i / 24,
                planes=(y, u, v, a), pixel_format='topos_yuva422p10a12',
                width=64, height=40, plane_infos=infos))
        enc.close()

        # ExportFrameProvider 逐帧迭代 planar 帧 → ToposVideoEncoder
        from src.shared.media.factory import MediaSourceFactory

        source = MediaSourceFactory.create(src_path)
        source.open()
        try:
            provider = ExportFrameProvider(source)
            out_path = str(tmp_path / "out_alpha.mov")
            enc2 = ToposVideoEncoder(out_path, FFmpegEncoderConfig(
                codec='topos', width=64, height=40, fps=Rational(24, 1),
                pix_fmt='topos_yuva422p10a12', profile='hq', crf=6, has_alpha=True))
            enc2.open()
            count = 0
            for frame in provider:
                assert len(frame.planes) == 4    # planar YUVA 直通（不压平）
                enc2.encode_frame(frame)
                count += 1
            enc2.close()
        finally:
            source.close()
        assert count == 3

        s = _open_decoded(out_path)
        try:
            md = s.probe()
            assert md.has_alpha is True and md.total_frames == 3
            f = s.read_frame(1)
            # 双重 mode2 a12 编解码后的 alpha 仍有界误差
            err = float(np.max(np.abs(
                f.planes[3].astype(np.int32) - a.astype(np.int32))))
            assert err <= 128, f"alpha double-generation err={err}"
        finally:
            s.close()


# =============================================================================
# R1 数据正确性修复（2026-08-30 审计 §3：P0-2/P0-3/P1-1/P1-4 + 生命周期幂等）
# 用例先于修复编写：修复前必须失败（DID NOT RAISE / 数值断言不成立）。
# =============================================================================


class TestR1DataCorrectness:
    def test_bgra_channel_order_and_alpha(self, require_lib, tmp_path) -> None:
        """P0-2：BGRA 不得走通用 [::-1]（会变 ARGB——颜色与 Alpha 同时错）。"""
        from src.shared.media.frame import DecodedFrame

        # 纯红 + 全不透明：翻转后 (B=0,G=0,R=255,A=255) 被读成
        # RGB=(255,255,0)、A=0 —— Y 与 Alpha 双重错误，分离度极大
        out = str(tmp_path / "bgra_red.mov")
        enc = ToposVideoEncoder(out, _enc_config(
            pix_fmt='topos_yuva422p10a12', profile='hq', crf=4, has_alpha=True))
        enc.open()
        bgra = np.zeros((H, W, 4), np.uint8)
        bgra[..., 2] = 255   # R
        bgra[..., 3] = 255   # A
        enc.encode_frame(DecodedFrame(frame_index=0, pts=0, time_seconds=0.0,
                                      data=bgra, pixel_format='bgra',
                                      width=W, height=H))
        enc.close()

        s = _open_decoded(out)
        try:
            f = s.read_frame(0)
            y_val = int(f.planes[0][H // 2, W // 2])
            a_val = int(f.planes[3][H // 2, W // 2])
            # BT.709 limited：纯红 Y = 0.2126*876+64 ≈ 250（翻转后 ≈ 877）
            assert 230 <= y_val <= 270, f"Y={y_val}"
            # 255 → a12 满刻度 ≈ 4095（翻转后 alpha 取的是 B 通道 = 0）
            assert a_val >= 4095 - 64, f"A={a_val}"
        finally:
            s.close()

        # 半透明：BGR=(10,20,30)、A 列梯度 0..255（x=10 处 A=40，审计复现值）
        out2 = str(tmp_path / "bgra_grad.mov")
        enc2 = ToposVideoEncoder(out2, _enc_config(
            pix_fmt='topos_yuva422p10a12', profile='hq', crf=4, has_alpha=True))
        enc2.open()
        bgra2 = np.zeros((H, W, 4), np.uint8)
        bgra2[..., 0] = 10
        bgra2[..., 1] = 20
        bgra2[..., 2] = 30
        bgra2[..., 3] = np.tile(
            (np.arange(W) * 255 // (W - 1)).astype(np.uint8), (H, 1))
        enc2.encode_frame(DecodedFrame(frame_index=0, pts=0, time_seconds=0.0,
                                       data=bgra2, pixel_format='bgra',
                                       width=W, height=H))
        enc2.close()

        s2 = _open_decoded(out2)
        try:
            f2 = s2.read_frame(0)
            assert int(bgra2[0, 10, 3]) == 40
            a40 = int(f2.planes[3][H // 2, 10])
            y_val = int(f2.planes[0][H // 2, 10])
            # 40/255*4095 ≈ 642（翻转后 alpha = B = 10 → ≈ 161）
            assert 642 - 64 <= a40 <= 642 + 64, f"A={a40}"
            # rgb(30,20,10) → Y ≈ 137（翻转后 RGB=(40,30,20) → Y ≈ 172）
            assert 137 - 15 <= y_val <= 137 + 15, f"Y={y_val}"
        finally:
            s2.close()

    def test_rgba_channel_order_and_alpha(self, require_lib, tmp_path) -> None:
        """RGBA 契约钉死：显式映射后 RGB 与 A 各就各位（防回归）。"""
        from src.shared.media.frame import DecodedFrame

        out = str(tmp_path / "rgba.mov")
        enc = ToposVideoEncoder(out, _enc_config(
            pix_fmt='topos_yuva422p10a12', profile='hq', crf=4, has_alpha=True))
        enc.open()
        rgba = np.zeros((H, W, 4), np.uint8)
        rgba[..., 0] = 255   # R
        rgba[..., 3] = 255   # A
        enc.encode_frame(DecodedFrame(frame_index=0, pts=0, time_seconds=0.0,
                                      data=rgba, pixel_format='rgba',
                                      width=W, height=H))
        enc.close()

        s = _open_decoded(out)
        try:
            f = s.read_frame(0)
            assert 230 <= int(f.planes[0][H // 2, W // 2]) <= 270
            assert int(f.planes[3][H // 2, W // 2]) >= 4095 - 64
        finally:
            s.close()

    def test_unknown_color_range_rejected(self, require_lib, tmp_path) -> None:
        """P1-4：range 仅接受 limited/full；未知值显式失败（含 'unknown'）。"""
        from src.shared.export.video_encoder import ColorMetadataConfig

        for bad in ('mystery', 'unknown', 'pc', 'limited!'):
            cfg = _enc_config(color_metadata=ColorMetadataConfig(
                primaries='bt709', transfer='bt709', matrix='bt709', range=bad))
            enc = ToposVideoEncoder(str(tmp_path / "bad_range.mov"), cfg)
            with pytest.raises(RuntimeError, match="color range"):
                enc.open()

        # 显式 full 可开且 roundtrip 保真
        out = str(tmp_path / "full.mov")
        enc = ToposVideoEncoder(out, _enc_config(color_metadata=ColorMetadataConfig(
            primaries='bt709', transfer='bt709', matrix='bt709', range='full')))
        enc.open()
        enc.encode_frame(_bgr48_frame(0))
        enc.close()
        s = _open_decoded(out)
        try:
            assert s.probe().color_range == 'full'
        finally:
            s.close()

    def test_planar_depth_mismatch_rejected(self, require_lib, tmp_path) -> None:
        """P1-1（R4.1 语义更新）：12-bit 平面配 10-bit pix_fmt 不得静默接受。

        yuv422p12le 本身已是 v1.2 合法格式（见 TestR4FormatExpansion）；
        本用例守住的是位深交叉——10-bit 导出配置收到 12-bit 平面必须显式拒绝。
        """
        from src.shared.media.frame import DecodedFrame, PlaneInfo

        cw = W // 2
        y = np.full((H, W), 4000, '<u2')
        u = np.full((H, cw), 2048, '<u2')
        v = np.full((H, cw), 1024, '<u2')
        infos = (PlaneInfo.create(0, W, H, 'Y', np.uint16, 12),
                 PlaneInfo.create(1, cw, H, 'U', np.uint16, 12),
                 PlaneInfo.create(2, cw, H, 'V', np.uint16, 12))
        enc = ToposVideoEncoder(str(tmp_path / "p12.mov"), _enc_config())
        enc.open()
        with pytest.raises(RuntimeError, match="位深"):
            enc.encode_frame(DecodedFrame(
                frame_index=0, pts=0, time_seconds=0.0,
                planes=(y, u, v), pixel_format='yuv422p10le',
                width=W, height=H, plane_infos=infos))
        enc.abort()

    def test_planar_domain_check_sampling(self, require_lib, tmp_path, monkeypatch) -> None:
        """P1-12：码值域全平面扫描按「首帧 + 每 256 帧抽检」调度——
        抽样帧的越界不被逐帧扫描（首帧全检已锚定）；强制全检开关恢复逐帧。"""
        from src.shared.media.frame import DecodedFrame, PlaneInfo

        cw = W // 2
        infos = (PlaneInfo.create(0, W, H, 'Y', np.uint16, 10),
                 PlaneInfo.create(1, cw, H, 'U', np.uint16, 10),
                 PlaneInfo.create(2, cw, H, 'V', np.uint16, 10))
        monkeypatch.delenv('TOPOS_ENCODER_FULL_CHECK', raising=False)
        enc = ToposVideoEncoder(str(tmp_path / "sample.mov"), _enc_config())
        enc.open()
        try:
            def _frame(idx, yval):
                return DecodedFrame(
                    frame_index=idx, pts=idx, time_seconds=idx / 24.0,
                    planes=(np.full((H, W), yval, '<u2'),
                            np.full((H, cw), 400, '<u2'),
                            np.full((H, cw), 600, '<u2')),
                    pixel_format='yuv422p10le', width=W, height=H,
                    plane_infos=infos)

            enc.encode_frame(_frame(0, 500))     # 首帧全检（合法）
            # 抽样间隔内（帧 1..255）：越界值不被逐帧扫描捕获（抽样语义）
            enc.encode_frame(_frame(1, 2000))
            # 抽样帧（第 256 次 encode）：全检恢复 → 拒绝
            with pytest.raises(RuntimeError, match="1023"):
                for idx in range(2, 257):
                    enc.encode_frame(_frame(idx, 2000 if idx == 256 else 500))
        finally:
            enc.abort()

        # 强制全检：抽样帧的越界同样立即拒绝
        monkeypatch.setenv('TOPOS_ENCODER_FULL_CHECK', '1')
        enc2 = ToposVideoEncoder(str(tmp_path / "full.mov"), _enc_config())
        enc2.open()
        try:
            with pytest.raises(RuntimeError, match="1023"):
                enc2.encode_frame(DecodedFrame(
                    frame_index=0, pts=0, time_seconds=0.0,
                    planes=(np.full((H, W), 500, '<u2'),
                            np.full((H, cw), 400, '<u2'),
                            np.full((H, cw), 600, '<u2')),
                    pixel_format='yuv422p10le', width=W, height=H,
                    plane_infos=infos))
                enc2.encode_frame(DecodedFrame(
                    frame_index=1, pts=1, time_seconds=1 / 24.0,
                    planes=(np.full((H, W), 2000, '<u2'),
                            np.full((H, cw), 400, '<u2'),
                            np.full((H, cw), 600, '<u2')),
                    pixel_format='yuv422p10le', width=W, height=H,
                    plane_infos=infos))
        finally:
            enc2.abort()

    def test_planar_out_of_range_sample_rejected(self, require_lib, tmp_path) -> None:
        """P1-1：10-bit 平面出现 >1023 码值（如 12-bit 数据误标）必须拒绝。"""
        from src.shared.media.frame import DecodedFrame, PlaneInfo

        cw = W // 2
        infos = (PlaneInfo.create(0, W, H, 'Y', np.uint16, 10),
                 PlaneInfo.create(1, cw, H, 'U', np.uint16, 10),
                 PlaneInfo.create(2, cw, H, 'V', np.uint16, 10))
        enc = ToposVideoEncoder(str(tmp_path / "over.mov"), _enc_config())
        enc.open()
        y = np.full((H, W), 2000, '<u2')   # > 1023
        with pytest.raises(RuntimeError, match="1023"):
            enc.encode_frame(DecodedFrame(
                frame_index=0, pts=0, time_seconds=0.0,
                planes=(y, np.full((H, cw), 400, '<u2'),
                        np.full((H, cw), 600, '<u2')),
                pixel_format='yuv422p10le', width=W, height=H,
                plane_infos=infos))
        enc.abort()

        # alpha 同理：a12 平面出现 >4095 码值必须拒绝
        enc2 = ToposVideoEncoder(str(tmp_path / "over_a.mov"), _enc_config(
            pix_fmt='topos_yuva422p10a12', has_alpha=True, crf=6))
        enc2.open()
        a_infos = infos + (PlaneInfo.create(3, W, H, 'A', np.uint16, 12),)
        a = np.full((H, W), 5000, '<u2')   # > 4095
        with pytest.raises(RuntimeError, match="超出"):
            enc2.encode_frame(DecodedFrame(
                frame_index=0, pts=0, time_seconds=0.0,
                planes=(np.full((H, W), 500, '<u2'),
                        np.full((H, cw), 400, '<u2'),
                        np.full((H, cw), 600, '<u2'), a),
                pixel_format='topos_yuva422p10a12', width=W, height=H,
                plane_infos=a_infos))
        enc2.abort()

    def test_premultiplied_flag_roundtrip(self, require_lib, tmp_path) -> None:
        """P0-3：alpha_premultiplied 必须写进 tpcC 并解码回 extra。"""
        from src.shared.media.frame import DecodedFrame

        out = str(tmp_path / "premult.mov")
        enc = ToposVideoEncoder(out, _enc_config(
            pix_fmt='topos_yuva422p10a12', profile='hq', crf=6,
            has_alpha=True, alpha_premultiplied=True))
        enc.open()
        bgra = np.zeros((H, W, 4), np.uint8)
        bgra[..., 2] = 200
        bgra[..., 3] = 255
        enc.encode_frame(DecodedFrame(frame_index=0, pts=0, time_seconds=0.0,
                                      data=bgra, pixel_format='bgra',
                                      width=W, height=H))
        enc.close()

        s = _open_decoded(out)
        try:
            assert s.read_frame(0).extra.get('topos_alpha_premultiplied') is True
        finally:
            s.close()

        # 未设置（默认）= straight
        out2 = str(tmp_path / "straight.mov")
        enc2 = ToposVideoEncoder(out2, _enc_config(
            pix_fmt='topos_yuva422p10a12', profile='hq', crf=6, has_alpha=True))
        enc2.open()
        enc2.encode_frame(DecodedFrame(frame_index=0, pts=0, time_seconds=0.0,
                                       data=bgra, pixel_format='bgra',
                                       width=W, height=H))
        enc2.close()
        s2 = _open_decoded(out2)
        try:
            assert s2.read_frame(0).extra.get('topos_alpha_premultiplied') is False
        finally:
            s2.close()

    def test_premultiplied_consistency_rules(self, require_lib, tmp_path) -> None:
        """P0-3 一致性：整段固定语义；帧与配置冲突 / packed 冲突均显式报错。"""
        from src.shared.media.frame import DecodedFrame, PlaneInfo

        cw = W // 2
        infos = (PlaneInfo.create(0, W, H, 'Y', np.uint16, 10),
                 PlaneInfo.create(1, cw, H, 'U', np.uint16, 10),
                 PlaneInfo.create(2, cw, H, 'V', np.uint16, 10),
                 PlaneInfo.create(3, W, H, 'A', np.uint16, 12))
        planes = (np.full((H, W), 500, '<u2'), np.full((H, cw), 400, '<u2'),
                  np.full((H, cw), 600, '<u2'),
                  np.full((H, W), 3000, '<u2'))

        # (a) 配置 straight + planar 源帧自带 premultiplied=True → 拒绝
        enc = ToposVideoEncoder(str(tmp_path / "conflict.mov"), _enc_config(
            pix_fmt='topos_yuva422p10a12', has_alpha=True, crf=6))
        enc.open()
        with pytest.raises(RuntimeError, match="alpha_premultiplied"):
            enc.encode_frame(DecodedFrame(
                frame_index=0, pts=0, time_seconds=0.0, planes=planes,
                pixel_format='topos_yuva422p10a12', width=W, height=H,
                plane_infos=infos,
                extra={'topos_alpha_premultiplied': True}))
        enc.abort()

        # (b) 配置 premultiplied + packed 合成帧：packed 不携带逐帧标志、
        #     不与配置冲突（配置声明即整段语义）——正常编码且标志 roundtrip
        bgra = np.zeros((H, W, 4), np.uint8)
        bgra[..., 3] = 255
        out_b = str(tmp_path / "packed_premult.mov")
        enc2 = ToposVideoEncoder(out_b, _enc_config(
            pix_fmt='topos_yuva422p10a12', has_alpha=True, crf=6,
            alpha_premultiplied=True))
        enc2.open()
        enc2.encode_frame(DecodedFrame(
            frame_index=0, pts=0, time_seconds=0.0, data=bgra,
            pixel_format='bgra', width=W, height=H))
        enc2.close()
        sb = _open_decoded(out_b)
        try:
            assert sb.read_frame(0).extra.get(
                'topos_alpha_premultiplied') is True
        finally:
            sb.close()

        # (c) 配置 premultiplied + planar 帧一致 → 通过
        enc3 = ToposVideoEncoder(str(tmp_path / "agree.mov"), _enc_config(
            pix_fmt='topos_yuva422p10a12', has_alpha=True, crf=6,
            alpha_premultiplied=True))
        enc3.open()
        enc3.encode_frame(DecodedFrame(
            frame_index=0, pts=0, time_seconds=0.0, planes=planes,
            pixel_format='topos_yuva422p10a12', width=W, height=H,
            plane_infos=infos,
            extra={'topos_alpha_premultiplied': True}))
        enc3.close()
        assert os.path.exists(str(tmp_path / "agree.mov"))

    def test_planar_alpha_bit_depth_respected(self, require_lib, tmp_path) -> None:
        """alpha 位深须从输入格式解析（a16→mode1 无损），不得硬编码 12。"""
        from src.shared.media.frame import DecodedFrame, PlaneInfo

        cw = W // 2
        a16 = np.tile(
            (np.arange(W, dtype=np.uint32) * 65535 // (W - 1)).astype('<u2'),
            (H, 1))
        infos16 = (PlaneInfo.create(0, W, H, 'Y', np.uint16, 10),
                   PlaneInfo.create(1, cw, H, 'U', np.uint16, 10),
                   PlaneInfo.create(2, cw, H, 'V', np.uint16, 10),
                   PlaneInfo.create(3, W, H, 'A', np.uint16, 16))
        out = str(tmp_path / "a16.mov")
        enc = ToposVideoEncoder(out, _enc_config(
            pix_fmt='topos_yuva422p10a16', profile='hq', crf=6, has_alpha=True))
        enc.open()
        enc.encode_frame(DecodedFrame(
            frame_index=0, pts=0, time_seconds=0.0,
            planes=(np.full((H, W), 500, '<u2'), np.full((H, cw), 400, '<u2'),
                    np.full((H, cw), 600, '<u2'), a16),
            pixel_format='topos_yuva422p10a16', width=W, height=H,
            plane_infos=infos16))
        enc.close()

        s = _open_decoded(out)
        try:
            md = s.probe()
            assert md.pixel_format == 'topos_yuva422p10a16'
            f = s.read_frame(0)
            err = float(np.max(np.abs(
                f.planes[3].astype(np.int64) - a16.astype(np.int64))))
            assert err <= 16, f"a16 alpha err={err}"
            assert int(f.planes[3].max()) > 60000   # 满刻度未被当 12-bit 压扁
        finally:
            s.close()

        # a8：格式名与码值域 roundtrip
        a8 = np.tile(
            (np.arange(W, dtype=np.uint32) * 255 // (W - 1)).astype('<u2'),
            (H, 1))
        infos8 = infos16[:3] + (PlaneInfo.create(3, W, H, 'A', np.uint16, 8),)
        out8 = str(tmp_path / "a8.mov")
        enc8 = ToposVideoEncoder(out8, _enc_config(
            pix_fmt='topos_yuva422p10a8', profile='hq', crf=6, has_alpha=True))
        enc8.open()
        enc8.encode_frame(DecodedFrame(
            frame_index=0, pts=0, time_seconds=0.0,
            planes=(np.full((H, W), 500, '<u2'), np.full((H, cw), 400, '<u2'),
                    np.full((H, cw), 600, '<u2'), a8),
            pixel_format='topos_yuva422p10a8', width=W, height=H,
            plane_infos=infos8))
        enc8.close()
        s8 = _open_decoded(out8)
        try:
            assert s8.probe().pixel_format == 'topos_yuva422p10a8'
            f8 = s8.read_frame(0)
            err8 = float(np.max(np.abs(
                f8.planes[3].astype(np.int64) - a8.astype(np.int64))))
            assert err8 <= 8, f"a8 alpha err={err8}"
        finally:
            s8.close()

    def test_packed_unknown_format_rejected(self, require_lib, tmp_path) -> None:
        """packed 路径白名单：非 rgb*/bgr* 布局不得被当 RGB 静默编码。"""
        from src.shared.media.frame import DecodedFrame

        enc = ToposVideoEncoder(str(tmp_path / "gray.mov"), _enc_config())
        enc.open()
        with pytest.raises(RuntimeError, match="不受支持"):
            enc.encode_frame(DecodedFrame(
                frame_index=0, pts=0, time_seconds=0.0,
                data=np.zeros((H, W, 3), np.uint8), pixel_format='xyzzy',
                width=W, height=H))
        enc.abort()

        enc2 = ToposVideoEncoder(str(tmp_path / "argb.mov"), _enc_config(
            has_alpha=True, pix_fmt='topos_yuva422p10a12'))
        enc2.open()
        with pytest.raises(RuntimeError, match="不受支持"):
            enc2.encode_frame(DecodedFrame(
                frame_index=0, pts=0, time_seconds=0.0,
                data=np.zeros((H, W, 4), np.uint8), pixel_format='argb',
                width=W, height=H))
        enc2.abort()

    def test_packed_four_channel_without_alpha_warns(
            self, require_lib, tmp_path, caplog) -> None:
        """未选 Alpha 输出但输入为 4 通道 packed：显式警告（与 planar 对齐）。"""
        import logging as _logging

        from src.shared.media.frame import DecodedFrame

        out = str(tmp_path / "drop4.mov")
        enc = ToposVideoEncoder(out, _enc_config())   # has_alpha=False
        enc.open()
        bgra = np.zeros((H, W, 4), np.uint8)
        bgra[..., 2] = 200
        with caplog.at_level(_logging.WARNING, logger='src.shared.export.topos_encoder'):
            enc.encode_frame(DecodedFrame(
                frame_index=0, pts=0, time_seconds=0.0, data=bgra,
                pixel_format='bgra', width=W, height=H))
        enc.close()
        assert '丢弃' in caplog.text

        s = _open_decoded(out)
        try:
            assert s.probe().has_alpha is False
            assert len(s.read_frame(0).planes) == 3
        finally:
            s.close()

    def test_close_abort_idempotent_and_temp_cleaned(
            self, require_lib, tmp_path) -> None:
        """R1 任务 5：close/abort 任意顺序重复调用均安全、无临时残留。"""
        out = str(tmp_path / "life.mov")
        enc = ToposVideoEncoder(out, _enc_config())
        enc.open()
        enc.encode_frame(_bgr48_frame(0))
        enc.close()
        assert os.path.exists(out)
        assert not os.path.exists(out + ".topos-tmp")
        enc.close()                    # 幂等
        enc.abort()                    # close 后 abort：不得动已产出文件
        assert os.path.exists(out)
        assert not os.path.exists(out + ".topos-tmp")
        assert enc._mux is None        # 成功定稿后不残留已关闭 mux 引用

        out2 = str(tmp_path / "life2.mov")
        enc2 = ToposVideoEncoder(out2, _enc_config())
        enc2.open()
        enc2.encode_frame(_bgr48_frame(0))
        enc2.abort()
        enc2.abort()                   # 重复 abort 静默
        enc2.close()                   # abort 后 close 静默
        assert not os.path.exists(out2)
        assert not os.path.exists(out2 + ".topos-tmp")

        # 编码中途失败（非法 planar 格式）后 abort：状态可恢复、无残留
        out3 = str(tmp_path / "life3.mov")
        enc3 = ToposVideoEncoder(out3, _enc_config())
        enc3.open()
        from src.shared.media.frame import DecodedFrame
        with pytest.raises(RuntimeError):
            enc3.encode_frame(DecodedFrame(
                frame_index=0, pts=0, time_seconds=0.0,
                planes=(np.zeros((H, W), '<u2'),), pixel_format='yuv420p',
                width=W, height=H))
        enc3.abort()
        assert not os.path.exists(out3)
        assert not os.path.exists(out3 + ".topos-tmp")

    def test_proxy_preserves_alpha_depth_and_premultiplied(
            self, require_lib, tmp_path) -> None:
        """代理链：a16 位深与 premultiplied 标志须原样传递（不得压到 12/straight）。"""
        from src.shared.media.frame import DecodedFrame, PlaneInfo
        from src.shared.media.proxy import DiskProxyConfig
        from src.shared.media.proxy_generator import ProxyGenerator

        cw = W // 2
        a16 = np.tile(
            (np.arange(W, dtype=np.uint32) * 65535 // (W - 1)).astype('<u2'),
            (H, 1))
        infos16 = (PlaneInfo.create(0, W, H, 'Y', np.uint16, 10),
                   PlaneInfo.create(1, cw, H, 'U', np.uint16, 10),
                   PlaneInfo.create(2, cw, H, 'V', np.uint16, 10),
                   PlaneInfo.create(3, W, H, 'A', np.uint16, 16))
        src_path = str(tmp_path / "src_a16.mov")
        enc = ToposVideoEncoder(src_path, _enc_config(
            pix_fmt='topos_yuva422p10a16', profile='hq', crf=6,
            has_alpha=True, alpha_premultiplied=True))
        enc.open()
        enc.encode_frame(DecodedFrame(
            frame_index=0, pts=0, time_seconds=0.0,
            planes=(np.full((H, W), 500, '<u2'), np.full((H, cw), 400, '<u2'),
                    np.full((H, cw), 600, '<u2'), a16),
            pixel_format='topos_yuva422p10a16', width=W, height=H,
            plane_infos=infos16,
            extra={'topos_alpha_premultiplied': True}))
        enc.close()

        result = ProxyGenerator(DiskProxyConfig()).generate(src_path)
        assert result.success, result.error_message
        s = _open_decoded(result.proxy_path)
        try:
            md = s.probe()
            assert md.has_alpha is True
            assert md.pixel_format == 'topos_yuva422p10a16'
            f = s.read_frame(0)
            assert f.extra.get('topos_alpha_premultiplied') is True
            assert int(f.planes[3].max()) > 60000
        finally:
            s.close()


# =============================================================================
# R3 Alpha 预算、统计与元数据闭环（2026-08-30 审计 §4 R3 / P1-3）
# 用例先于实现编写：修复前必须失败（无 tpcB 预算元数据可读、无 12→10→8
# 自适应、无超硬上限策略、native stats 被丢弃）。
#
# 内容参数经实测校准（64×40，sized 档位码控，比例 = alpha_payload/color_payload）：
#   smooth 梯度 alpha  → a12 ≈0.008（全部档位不触发自适应，回归安全）
#   12-bit 域 ±8 级抖动 → a12≈2.75 / a10≈2.56 / a8≈0.008（自适应 12→8 黄金用例）
#   全随机 alpha       → a8 仍 ≈5.6 > 硬上限（超限策略三态用例）
# 实测最大误差：a10 ≤ 32、a8 ≤ 192，均在 spec §8.6 界（63/255）内。
# =============================================================================


class TestR3AlphaBudget:
    @staticmethod
    def _float_bgra(alpha: str = 'smooth', base: float = 0.5, seed: int = 7) -> np.ndarray:
        """float32 BGRA（信号域 [0,1]）；颜色恒为平滑梯度，alpha 形态可控。"""
        rng = np.random.default_rng(seed)
        yy, xx = np.mgrid[0:H, 0:W]
        bgra = np.zeros((H, W, 4), np.float32)
        bgra[..., 0] = xx / max(1, W - 1)
        bgra[..., 1] = yy / max(1, H - 1)
        bgra[..., 2] = (xx + yy) / max(1, W + H - 2)
        if alpha == 'smooth':
            bgra[..., 3] = xx / max(1, W - 1) * 0.5
        elif alpha == 'noise16':
            noise = rng.integers(0, 16, size=(H, W))
            bgra[..., 3] = np.clip(base + (noise - 8.0) / 4095.0, 0.0, 1.0)
        elif alpha == 'random':
            bgra[..., 3] = rng.random((H, W))
        elif alpha == 'opaque':
            bgra[..., 3] = 1.0
        elif alpha == 'transparent':
            bgra[..., 3] = 0.0
        elif alpha == 'hard_edge':
            bgra[..., 3] = (xx >= W // 2).astype(np.float32)
        else:
            raise ValueError(alpha)
        return bgra

    def _export(self, tmp_path, name: str, alpha: str, tier: str = 'standard',
                policy: str = None, pix_fmt: str = 'yuv422p10le',
                frames: int = 3) -> str:
        """自适应路径导出（裸 pix_fmt + has_alpha；crf=None → sized 档位码控）。"""
        from src.shared.media.frame import DecodedFrame

        kwargs = dict(pix_fmt=pix_fmt, profile=tier, has_alpha=True, crf=None)
        if policy is not None:
            kwargs['alpha_budget_policy'] = policy
        out = str(tmp_path / name)
        enc = ToposVideoEncoder(out, _enc_config(**kwargs))
        enc.open()
        for i in range(frames):
            enc.encode_frame(DecodedFrame(
                frame_index=i, pts=i, time_seconds=i / 24,
                data=self._float_bgra(alpha), pixel_format='bgra',
                width=W, height=H))
        enc.close()
        return out

    def _budget_of(self, path: str, frame: int = 0) -> dict:
        s = _open_decoded(path)
        try:
            return s.read_frame(frame).extra.get('topos_alpha_budget')
        finally:
            s.close()

    @pytest.mark.parametrize('tier,pix_fmt', [
        ('proxy', 'yuv422p10le'), ('lt', 'yuv422p10le'),
        ('standard', 'yuv422p10le'), ('hq', 'yuv422p10le'),
        # 复验 P1-11：预算矩阵扩至六档（Pro444 须 444；Extreme 须 444+12）
        ('4444', 'yuv444p10le'), ('4444xq', 'yuv444p12le'),
    ])
    def test_budget_metadata_roundtrip_all_tiers(self, require_lib, tmp_path, tier, pix_fmt) -> None:
        """任务 1/5：stats 被消费；目标/实际比例、模式、最大误差经 tpcB 记录可读。"""
        from src.shared.codec.topos_profiles import get_topos_tier

        out = self._export(tmp_path, f"budget_{tier}.mov", 'smooth',
                           tier=tier, pix_fmt=pix_fmt)
        bud = self._budget_of(out)
        t = get_topos_tier(tier)
        assert isinstance(bud, dict), "解码侧必须能读到 alpha 预算元数据（tpcB）"
        assert bud['alpha_mode'] == 2
        assert bud['target_ratio_bp'] == round(t.alpha_budget_ratio * 10000)
        assert bud['frame_count'] == 3
        if t.native_profile != 3:
            # Pro444/Extreme（P5 未重定标）：smooth 保持 a12、不降档不超限
            assert bud['alpha_bit_depth'] == 12
            assert bud['max_abs_error'] <= 15     # a12 spec §8.6 误差界
            assert bud['actual_ratio_bp'] <= round(t.alpha_hard_cap * 10000)
            assert bud['flags'] == {'overrun': False, 'authorized': False,
                            'adapted': False, 'ratio_saturated': False}
        elif bud['flags'].get('adapted'):
            # 低码率 profile-3 档（如 netproxy 级 bpp）：smooth 颜色流
            # 压得足够小 → alpha/颜色比例超预算 → 自适应 a12→a8
            assert bud['alpha_bit_depth'] == 8
            assert bud['max_abs_error'] <= 128     # a8 预量化半步（16-bit 域）
        else:
            # v1.9 P2 重标定后的高码率 profile-3 档：颜色流预算充足 →
            # 比例在目标内、保持 a12 不降档不超限
            assert bud['alpha_bit_depth'] == 12
            assert bud['max_abs_error'] <= 15
            assert bud['actual_ratio_bp'] <= round(t.alpha_hard_cap * 10000)
            assert bud['flags']['overrun'] is False

    def test_alpha_depth_adapts_to_fit_budget(self, require_lib, tmp_path) -> None:
        """任务 2：noise16 在 a12/a10 比例 ≈2.7 > 目标 0.20，a8 ≈0.008 → 自适应 12→8。"""
        alpha_in = self._float_bgra('noise16')
        out = self._export(tmp_path, "adapts.mov", 'noise16', tier='proxy')
        s = _open_decoded(out)
        try:
            f = s.read_frame(0)
            bud = f.extra['topos_alpha_budget']
            assert bud['alpha_bit_depth'] == 8
            assert bud['flags']['adapted'] is True
            assert bud['flags']['overrun'] is False
            assert bud['actual_ratio_bp'] <= 2000
            # 解码平面语义 = 8-bit 满刻度（topos_source 按 tpcC 位深右移）
            assert int(f.plane_infos[3].bit_depth) == 8
            assert int(f.planes[3].max()) <= 255
            ref = np.rint(alpha_in[..., 3] * 255.0).astype(np.int32)
            err = float(np.max(np.abs(f.planes[3].astype(np.int32) - ref)))
            assert err <= 2, f"自适应后 alpha err={err}（a8 域）"
        finally:
            s.close()

    def test_hard_cap_error_policy_raises(self, require_lib, tmp_path) -> None:
        """任务 3：全随机 alpha 降到底仍 > cap，policy=error → 结构化诊断错误。"""
        with pytest.raises(RuntimeError) as ei:
            self._export(tmp_path, "over_err.mov", 'random', tier='proxy',
                         policy='error')
        msg = str(ei.value)
        assert '硬上限' in msg
        assert 'alpha/color=' in msg          # 诊断带实际比例
        assert 'cap=' in msg
        assert not os.path.exists(str(tmp_path / "over_err.mov"))

    def test_hard_cap_record_policy_delivers(self, require_lib, tmp_path, caplog) -> None:
        """任务 3：默认 policy=record → 照常交付 + tpcB 超限标志 + 显式警告。"""
        import logging

        with caplog.at_level(logging.WARNING, logger='src.shared.export.topos_encoder'):
            out = self._export(tmp_path, "over_rec.mov", 'random', tier='proxy')
        assert os.path.exists(out)
        bud = self._budget_of(out)
        assert bud['flags']['overrun'] is True
        assert bud['flags']['authorized'] is False
        assert bud['actual_ratio_bp'] > 2500
        assert bud['alpha_bit_depth'] == 8       # 已降到底仍超限
        assert any('硬上限' in r.message for r in caplog.records)

    def test_hard_cap_continue_policy_authorized(self, require_lib, tmp_path) -> None:
        """任务 3：policy=continue（用户授权）→ 交付并记录授权标志。"""
        out = self._export(tmp_path, "over_auth.mov", 'random', tier='standard',
                           policy='continue')
        bud = self._budget_of(out)
        assert bud['flags']['overrun'] is True
        assert bud['flags']['authorized'] is True
        assert bud['flags']['adapted'] is True

    def test_lossless_alpha_never_degrades(self, require_lib, tmp_path, caplog) -> None:
        """任务 4：mode1（a16）超限绝不自动降质——默认记录+提示；error 策略报错。"""
        import logging

        alpha_in = self._float_bgra('random')
        with caplog.at_level(logging.WARNING, logger='src.shared.export.topos_encoder'):
            out = self._export(tmp_path, "lossless.mov", 'random', tier='hq',
                               pix_fmt='topos_yuva422p10a16')
        s = _open_decoded(out)
        try:
            f = s.read_frame(0)
            bud = f.extra['topos_alpha_budget']
            assert bud['alpha_mode'] == 1
            assert bud['alpha_bit_depth'] == 16
            # 语义标志：无损超限必记录、绝不自动降质。ratio_saturated 是
            # u16 元数据饱和诊断位（qp95 域下随机内容的无损 alpha 可超
            # 主码流 6.55×）——不参与无损语义，不做精确钉扎
            assert bud['flags']['overrun'] is True
            assert bud['flags']['authorized'] is False
            assert bud['flags']['adapted'] is False
            # mode1 无损：解码 alpha 与输入 bit-exact
            ref = np.rint(alpha_in[..., 3] * 65535.0).astype(np.int64)
            err = int(np.max(np.abs(f.planes[3].astype(np.int64) - ref)))
            assert err <= 1, f"无损 alpha err={err}"
        finally:
            s.close()
        assert any('无损' in r.message and '降质' in r.message
                   for r in caplog.records), "超限必须显式提示无损不降质"

        with pytest.raises(RuntimeError, match='无损'):
            self._export(tmp_path, "lossless_err.mov", 'random', tier='hq',
                         pix_fmt='topos_yuva422p10a16', policy='error')

    def test_explicit_alpha_depth_locked(self, require_lib, tmp_path) -> None:
        """显式 pix_fmt 位深声明 = 格式契约：超预算也不静默降档，记录代替。"""
        out = self._export(tmp_path, "locked.mov", 'random', tier='standard',
                           pix_fmt='topos_yuva422p10a12')
        bud = self._budget_of(out)
        assert bud['alpha_bit_depth'] == 12
        assert bud['flags']['adapted'] is False
        assert bud['flags']['overrun'] is True

    def test_constant_and_hard_edge_alpha_in_budget(self, require_lib, tmp_path) -> None:
        """任务 6 矩阵：全透明/全不透明/硬边——零误差、不超预算、不降档。"""
        for kind in ('opaque', 'transparent', 'hard_edge'):
            out = self._export(tmp_path, f"{kind}.mov", kind, tier='standard')
            s = _open_decoded(out)
            try:
                f = s.read_frame(0)
                bud = f.extra['topos_alpha_budget']
                assert bud['flags'] == {'overrun': False, 'authorized': False,
                                        'adapted': False,
                                        'ratio_saturated': False}, kind
                assert bud['actual_ratio_bp'] <= 2500, kind
                assert bud['max_abs_error'] == 0, kind   # 二值/常量在 12-bit 网格上精确
                if kind == 'opaque':
                    assert int(f.planes[3].min()) >= 4095 - 64
                elif kind == 'transparent':
                    assert int(f.planes[3].max()) <= 64
                else:  # hard_edge：左半透明右半不透明，边缘无灰阶泄漏
                    left = f.planes[3][:, :W // 2 - 2]
                    right = f.planes[3][:, W // 2 + 2:]
                    assert int(left.max()) <= 64
                    assert int(right.min()) >= 4095 - 64
            finally:
                s.close()

    def test_budget_deterministic_bytes(self, require_lib, tmp_path) -> None:
        """预算决策（首帧探测 + tpcB）必须是输入的纯函数：同输入同字节。"""
        import hashlib

        a = self._export(tmp_path, "det_a.mov", 'noise16', tier='standard')
        b = self._export(tmp_path, "det_b.mov", 'noise16', tier='standard')
        with open(a, 'rb') as fa, open(b, 'rb') as fb:
            assert hashlib.sha256(fa.read()).digest() == \
                hashlib.sha256(fb.read()).digest()

    def test_proxy_chain_records_budget(self, require_lib, tmp_path) -> None:
        """代理重编码链同样消费 stats 并写 tpcB（record-only，不抛错）。"""
        from src.shared.export.video_encoder import FFmpegEncoderConfig
        from src.shared.media.frame import DecodedFrame, PlaneInfo
        from src.shared.media.metadata import Rational
        from src.shared.media.proxy import DiskProxyConfig
        from src.shared.media.proxy_generator import ProxyGenerator

        cw = W // 2
        a = (np.arange(W)[None, :] * 4095 // (W - 1)).astype('<u2')
        a = np.repeat(a, H, axis=0)
        infos = (PlaneInfo.create(0, W, H, 'Y', np.uint16, 10),
                 PlaneInfo.create(1, cw, H, 'U', np.uint16, 10),
                 PlaneInfo.create(2, cw, H, 'V', np.uint16, 10),
                 PlaneInfo.create(3, W, H, 'A', np.uint16, 12))
        src_path = str(tmp_path / "src_budget.mov")
        enc = ToposVideoEncoder(src_path, FFmpegEncoderConfig(
            codec='topos', width=W, height=H, fps=Rational(24, 1),
            pix_fmt='topos_yuva422p10a12', profile='standard', crf=6, has_alpha=True))
        enc.open()
        for i in range(3):
            enc.encode_frame(DecodedFrame(
                frame_index=i, pts=i, time_seconds=i / 24,
                planes=(np.full((H, W), 500 + i, '<u2'),
                        np.full((H, cw), 400, '<u2'),
                        np.full((H, cw), 600, '<u2'), a),
                pixel_format='topos_yuva422p10a12', width=W, height=H,
                plane_infos=infos))
        enc.close()

        result = ProxyGenerator(DiskProxyConfig()).generate(src_path)
        assert result.success, result.error_message
        s = _open_decoded(result.proxy_path)
        try:
            f = s.read_frame(0)
            bud = f.extra.get('topos_alpha_budget')
            assert isinstance(bud, dict), "代理产物也必须携带 tpcB 预算记录"
            assert bud['alpha_mode'] == 2
            # 代理链用 proxy 档位定标 → 目标比例 0.20
            assert bud['target_ratio_bp'] == 2000
            assert bud['frame_count'] == 3
        finally:
            s.close()


# =============================================================================
# R4.1 格式扩展：12-bit YUV 4:2:2（v1.2 枚举，帧头 minor=1）
# 失败先行依据：实现前 tc_query_support(3,0,12) 恒拒（旧 abi_compat 测试
# 恰好钉死了该拒绝——即反向证据）；encoder 白名单/转换/解码命名均 10-bit 硬编码。
# =============================================================================


def test_rans2_context_entropy_roundtrip(require_lib, tmp_path) -> None:
    """ADR-C036：'rans2'（V7-R2，entropy=7）显式选用——入流帧头 (7,7)、
    往返可解码零隐蔽；默认仍 'rans'（(7,6)，ADR-C035）。"""
    from src.shared.media.frame import DecodedFrame, PlaneInfo

    rng = np.random.default_rng(0xC036)
    w, h = 96, 64
    cw = w // 2
    planes = tuple(
        np.clip(512 + 120 * rng.standard_normal((h, cw if i else w)), 0, 1023)
        .astype('<u2') for i in range(3))
    infos = tuple(
        PlaneInfo.create(i, cw if i else w, h, c, np.uint16, 10)
        for i, c in enumerate(('Y', 'U', 'V')))
    out = str(tmp_path / "rans2.mov")
    enc = ToposVideoEncoder(out, _enc_config(
        entropy_mode='rans2', crf=30, width=w, height=h))
    enc.open()
    enc.encode_frame(DecodedFrame(
        frame_index=0, pts=0, time_seconds=0.0, planes=planes,
        pixel_format='yuv422p10le', width=w, height=h, plane_infos=infos))
    enc.close()
    with open(out, 'rb') as fh:
        head = fh.read(128)
    mdat = head.find(b'TPIC', 20)
    assert mdat >= 0
    assert head[mdat + 6] == 7 and head[mdat + 7] == 0
    assert head[mdat + 45] == 7, "rans2 入流 entropy_mode=7（V7-R2，ADR-C036）"

    s = _open_decoded(out)
    try:
        f = s.read_frame(0)
        assert f.planes[0].shape == (h, w)
        # 零隐蔽流重建应贴近源（crf30 纹理噪声，界放宽到量级检查）
        err = float(np.max(np.abs(
            f.planes[0].astype(np.int32) - planes[0].astype(np.int32))))
        assert err <= 64, f"rans2 往返 err={err}"
    finally:
        s.close()


class TestR4FormatExpansion:
    def test_query_support_12bit(self, require_lib) -> None:
        """能力协商：YUV 4:2:2 10/12-bit 支持；11/13 拒绝。"""
        assert topos_binding.ToposCodec().query_support(3, 0, 12, 0) == 0
        assert topos_binding.ToposCodec().query_support(3, 0, 12, 2) == 0
        assert topos_binding.ToposCodec().query_support(
            3, 0, 11, 0) == topos_binding.TC_ERR_UNSUPPORTED_PIXEL_FORMAT
        assert topos_binding.ToposCodec().query_support(
            3, 0, 13, 0) == topos_binding.TC_ERR_UNSUPPORTED_PIXEL_FORMAT

    def test_planar_12bit_roundtrip(self, require_lib, tmp_path) -> None:
        """planar yuv422p12le 直通：往返 + 帧头 minor=1 + 解码位深/命名。"""
        from src.shared.media.frame import DecodedFrame, PlaneInfo
        from src.shared.media.metadata import Rational

        w, h = 64, 40
        cw = w // 2
        y = np.tile(np.linspace(300, 3800, w, dtype='<u2'), (h, 1))
        u = np.full((h, cw), 1600, '<u2')
        v = np.full((h, cw), 2400, '<u2')
        out = str(tmp_path / "bd12.mov")
        enc = ToposVideoEncoder(out, _enc_config(
            pix_fmt='yuv422p12le', profile='hq', crf=6))
        enc.open()
        infos = (PlaneInfo.create(0, w, h, 'Y', np.uint16, 12),
                 PlaneInfo.create(1, cw, h, 'U', np.uint16, 12),
                 PlaneInfo.create(2, cw, h, 'V', np.uint16, 12))
        for i in range(3):
            enc.encode_frame(DecodedFrame(
                frame_index=i, pts=i, time_seconds=i / 24,
                planes=(y, u, v), pixel_format='yuv422p12le',
                width=w, height=h, plane_infos=infos))
        enc.close()

        # T1.5 转正：默认流 V2（major=2，minor 恒 0）；v1.2 的 minor=1
        # 断言移至显式 V1 流（见 _v1_stream_head）
        with open(out, 'rb') as fh:
            head = fh.read(64)
        mdat = head.find(b'TPIC', 20)
        assert mdat >= 0
        assert head[mdat + 6] == 7, "默认流 version_major 必须为 7（V7-R rANS，ADR-C035）"
        assert head[mdat + 7] == 0, "V2 流 version_minor 恒 0（ADR-C027 D-6）"
        assert head[mdat + 12] == 12
        head1 = _v1_stream_head(str(tmp_path / "bd12_v1.mov"),
                                'yuv422p12le', profile='hq', crf=6)
        mdat1 = head1.find(b'TPIC', 20)
        assert head1[mdat1 + 6] == 1 and head1[mdat1 + 7] == 1, \
            "V1 legacy：12-bit minor=1（v1.2 扩展枚举）"

        s = _open_decoded(out)
        try:
            md = s.probe()
            assert md.pixel_format == 'yuv422p12le'
            f = s.read_frame(0)
            assert int(f.plane_infos[0].bit_depth) == 12
            # qp6 高保真：往返误差有界（12-bit 域）
            err = float(np.max(np.abs(
                f.planes[0].astype(np.int32) - y.astype(np.int32))))
            assert err <= 24, f"12-bit 往返 err={err}"
        finally:
            s.close()

    def test_packed_bgra_to_12bit_roundtrip(self, require_lib, tmp_path) -> None:
        """packed BGRA → yuv422p12le：转换矩阵按 12-bit 定标（Y≈3760 亮度判别）。"""
        from src.shared.media.frame import DecodedFrame

        out = str(tmp_path / "bd12_packed.mov")
        enc = ToposVideoEncoder(out, _enc_config(
            pix_fmt='yuv422p12le', profile='hq', crf=4))
        enc.open()
        bgra = np.zeros((H, W, 4), np.uint8)
        bgra[..., 2] = 255   # 纯红（BGRA ch2=R）
        bgra[..., 3] = 255
        enc.encode_frame(DecodedFrame(frame_index=0, pts=0, time_seconds=0.0,
                                      data=bgra, pixel_format='bgra',
                                      width=W, height=H))
        enc.close()

        s = _open_decoded(out)
        try:
            f = s.read_frame(0)
            y_val = int(f.planes[0][H // 2, W // 2])
            # BT.709 limited 12-bit：纯红 Y = 0.2126*219*16 + 16*16 ≈ 998
            assert 940 <= y_val <= 1060, f"Y={y_val}（12-bit 定标错误？）"
            assert int(f.planes[0].max()) <= 4095
        finally:
            s.close()

    def test_planar_12bit_rejects_10bit_planes(self, require_lib, tmp_path) -> None:
        """位深交叉校验：10-bit 数据不得以 12-bit 头静默编码。"""
        from src.shared.media.frame import DecodedFrame, PlaneInfo

        w, h = 64, 40
        cw = w // 2
        y = np.full((h, w), 900, '<u2')     # 10-bit 域值 + 10-bit PlaneInfo
        u = np.full((h, cw), 400, '<u2')
        v = np.full((h, cw), 600, '<u2')
        enc = ToposVideoEncoder(str(tmp_path / "bad.mov"), _enc_config(
            pix_fmt='yuv422p12le', profile='hq', crf=6))
        enc.open()
        infos = (PlaneInfo.create(0, w, h, 'Y', np.uint16, 10),
                 PlaneInfo.create(1, cw, h, 'U', np.uint16, 10),
                 PlaneInfo.create(2, cw, h, 'V', np.uint16, 10))
        with pytest.raises(RuntimeError, match='位深'):
            enc.encode_frame(DecodedFrame(
                frame_index=0, pts=0, time_seconds=0.0,
                planes=(y, u, v), pixel_format='yuv422p12le',
                width=w, height=h, plane_infos=infos))

    def test_hq_tier_declares_12bit(self, require_lib) -> None:
        from src.shared.codec.topos_profiles import (
            TOPOS_CODEC_LABEL, get_topos_tier)
        t = get_topos_tier('hq')
        # 批 4：hq/4444 档位扩 16-bit（视频/图片 16-bit 档）
        assert t.bit_depths == (10, 12, 16)
        # 编解码器标签同步声明 10/12/16 全域（2026-09-13 复查同步）
        assert '10/12/16-bit' in TOPOS_CODEC_LABEL

    def test_alpha_with_12bit_color_roundtrip(self, require_lib, tmp_path) -> None:
        """12-bit 颜色 + mode2 a12 alpha 组合（topos_yuva422p12a12）。"""
        from src.shared.media.frame import DecodedFrame, PlaneInfo

        w, h = 64, 40
        cw = w // 2
        y = np.tile(np.linspace(300, 3800, w, dtype='<u2'), (h, 1))
        u = np.full((h, cw), 1600, '<u2')
        v = np.full((h, cw), 2400, '<u2')
        a = (np.arange(w)[None, :] * 4095 // (w - 1)).astype('<u2')
        a = np.repeat(a, h, axis=0)
        out = str(tmp_path / "bd12_alpha.mov")
        enc = ToposVideoEncoder(out, _enc_config(
            pix_fmt='topos_yuva422p12a12', profile='hq', crf=6, has_alpha=True))
        enc.open()
        infos = (PlaneInfo.create(0, w, h, 'Y', np.uint16, 12),
                 PlaneInfo.create(1, cw, h, 'U', np.uint16, 12),
                 PlaneInfo.create(2, cw, h, 'V', np.uint16, 12),
                 PlaneInfo.create(3, w, h, 'A', np.uint16, 12))
        for i in range(2):
            enc.encode_frame(DecodedFrame(
                frame_index=i, pts=i, time_seconds=i / 24,
                planes=(y, u, v, a), pixel_format='topos_yuva422p12a12',
                width=w, height=h, plane_infos=infos))
        enc.close()

        s = _open_decoded(out)
        try:
            md = s.probe()
            assert md.pixel_format == 'topos_yuva422p12a12'
            assert md.has_alpha is True
            f = s.read_frame(0)
            assert int(f.plane_infos[3].bit_depth) == 12
            assert int(f.planes[3].max()) >= 4095 - 64   # alpha 梯度保满刻度
            yerr = float(np.max(np.abs(
                f.planes[0].astype(np.int32) - y.astype(np.int32))))
            assert yerr <= 24
        finally:
            s.close()


class TestR42Yuv444:
    """R4.2：YUV 4:4:4 10/12-bit（v1.3 枚举扩展，pf=1 / 帧头 minor=2）。"""

    def test_query_support_pf444(self, require_lib) -> None:
        """能力协商：pf=1 10/12-bit 支持；pf=3 未知拒绝
        （pf=2/GBR 于 R4.3 起合法，见 TestR43Gbr）。"""
        c = topos_binding.ToposCodec()
        assert c.query_support(3, 1, 10, 0) == 0
        assert c.query_support(3, 1, 12, 0) == 0
        assert c.query_support(
            3, 3, 10, 0) == topos_binding.TC_ERR_UNSUPPORTED_PIXEL_FORMAT

    def test_planar_444_roundtrip(self, require_lib, tmp_path) -> None:
        """planar yuv444p10le 直通：往返 + 帧头 minor=2/pf=1 + 解码全宽几何。"""
        from src.shared.media.frame import DecodedFrame, PlaneInfo

        w, h = 64, 40
        y = np.tile(np.linspace(300, 950, w, dtype='<u2'), (h, 1))
        u = np.tile(np.linspace(400, 700, w, dtype='<u2'), (h, 1))
        v = np.tile(np.linspace(500, 800, w, dtype='<u2'), (h, 1))
        out = str(tmp_path / "pf444.mov")
        enc = ToposVideoEncoder(out, _enc_config(
            pix_fmt='yuv444p10le', profile='hq', crf=6))
        enc.open()
        infos = (PlaneInfo.create(0, w, h, 'Y', np.uint16, 10),
                 PlaneInfo.create(1, w, h, 'U', np.uint16, 10),
                 PlaneInfo.create(2, w, h, 'V', np.uint16, 10))
        for i in range(3):
            enc.encode_frame(DecodedFrame(
                frame_index=i, pts=i, time_seconds=i / 24,
                planes=(y, u, v), pixel_format='yuv444p10le',
                width=w, height=h, plane_infos=infos))
        enc.close()

        # T1.5 转正：默认流 V2（major=2，minor 恒 0）；v1.3 的 minor=2
        # 断言移至显式 V1 流
        with open(out, 'rb') as fh:
            head = fh.read(64)
        mdat = head.find(b'TPIC', 20)
        assert mdat >= 0
        assert head[mdat + 6] == 7, "默认流 version_major 必须为 7（V7-R rANS，ADR-C035）"
        assert head[mdat + 7] == 0, "V2 流 version_minor 恒 0（ADR-C027 D-6）"
        assert head[mdat + 11] == 1, "4:4:4 流帧头 pixel_format 必须为 1"
        head1 = _v1_stream_head(str(tmp_path / "yuv444_v1.mov"),
                                'yuv444p10le', profile='hq', crf=6)
        mdat1 = head1.find(b'TPIC', 20)
        assert head1[mdat1 + 6] == 1 and head1[mdat1 + 7] == 2, \
            "V1 legacy：4:4:4 minor=2（v1.3 扩展枚举）"

        s = _open_decoded(out)
        try:
            md = s.probe()
            assert md.pixel_format == 'yuv444p10le'
            f = s.read_frame(0)
            # 4:4:4 几何：U/V = 全宽
            assert f.planes[1].shape == (h, w), \
                f"U 平面形状 {f.planes[1].shape} != {(h, w)}"
            assert f.planes[2].shape == (h, w)
            err = float(np.max(np.abs(
                f.planes[0].astype(np.int32) - y.astype(np.int32))))
            assert err <= 8, f"4:4:4 往返 err={err}"
            uerr = float(np.max(np.abs(
                f.planes[1].astype(np.int32) - u.astype(np.int32))))
            assert uerr <= 8, f"U 往返 err={uerr}（半宽几何错误会大面积失配）"
        finally:
            s.close()

    def test_packed_bgra_to_444(self, require_lib, tmp_path) -> None:
        """packed BGRA → yuv444p10le：转换不做色度下采样（U 全宽可辨）。"""
        from src.shared.media.frame import DecodedFrame

        out = str(tmp_path / "pf444_packed.mov")
        enc = ToposVideoEncoder(out, _enc_config(
            pix_fmt='yuv444p10le', profile='hq', crf=4))
        enc.open()
        bgra = np.zeros((H, W, 4), np.uint8)
        # 左半蓝右半红（4:4:4 下 U/V 每列独立可辨）
        bgra[:, :W // 2, 0] = 255   # B（左半，轴序 H,W,C）
        bgra[:, W // 2:, 2] = 255   # R（右半）
        bgra[..., 3] = 255
        enc.encode_frame(DecodedFrame(frame_index=0, pts=0, time_seconds=0.0,
                                      data=bgra, pixel_format='bgra',
                                      width=W, height=H))
        enc.close()

        s = _open_decoded(out)
        try:
            f = s.read_frame(0)
            assert f.planes[1].shape == (H, W), "4:4:4 解码 U 必须全宽"
            u_left = int(f.planes[1][H // 2, W // 4])
            u_right = int(f.planes[1][H // 2, 3 * W // 4])
            # BT.709 limited：蓝 U 高、红 U 低，符号相反
            assert u_left - u_right >= 100, \
                f"U(left)={u_left} U(right)={u_right}（色度几何/转换错误？）"
        finally:
            s.close()

    def test_alpha_with_444_color_roundtrip(self, require_lib, tmp_path) -> None:
        """4:4:4 颜色 + mode2 a8 alpha 组合（topos_yuva444p10a8）。"""
        from src.shared.media.frame import DecodedFrame, PlaneInfo

        w, h = 64, 40
        y = np.full((h, w), 400, '<u2')
        u = np.full((h, w), 500, '<u2')
        v = np.full((h, w), 600, '<u2')
        a = (np.arange(w)[None, :] * 255 // (w - 1)).astype('<u2')
        a = np.repeat(a, h, axis=0)
        out = str(tmp_path / "pf444_alpha.mov")
        enc = ToposVideoEncoder(out, _enc_config(
            pix_fmt='topos_yuva444p10a8', profile='hq', crf=6, has_alpha=True))
        enc.open()
        infos = (PlaneInfo.create(0, w, h, 'Y', np.uint16, 10),
                 PlaneInfo.create(1, w, h, 'U', np.uint16, 10),
                 PlaneInfo.create(2, w, h, 'V', np.uint16, 10),
                 PlaneInfo.create(3, w, h, 'A', np.uint16, 8))
        for i in range(2):
            enc.encode_frame(DecodedFrame(
                frame_index=i, pts=i, time_seconds=i / 24,
                planes=(y, u, v, a), pixel_format='topos_yuva444p10a8',
                width=w, height=h, plane_infos=infos))
        enc.close()

        s = _open_decoded(out)
        try:
            md = s.probe()
            assert md.pixel_format == 'topos_yuva444p10a8'
            assert md.has_alpha is True
            f = s.read_frame(0)
            assert f.planes[1].shape == (h, w)
            assert int(f.plane_infos[3].bit_depth) == 8
            assert int(f.planes[3].max()) >= 255 - 8
        finally:
            s.close()

    def test_444_12bit_roundtrip(self, require_lib, tmp_path) -> None:
        """4:4:4 + 12-bit 组合（pf=1 × bd=12 → minor=2）。"""
        from src.shared.media.frame import DecodedFrame, PlaneInfo

        w, h = 64, 40
        y = np.tile(np.linspace(300, 3800, w, dtype='<u2'), (h, 1))
        u = np.full((h, w), 1600, '<u2')
        v = np.full((h, w), 2400, '<u2')
        out = str(tmp_path / "pf444_bd12.mov")
        enc = ToposVideoEncoder(out, _enc_config(
            pix_fmt='yuv444p12le', profile='hq', crf=6))
        enc.open()
        infos = (PlaneInfo.create(0, w, h, 'Y', np.uint16, 12),
                 PlaneInfo.create(1, w, h, 'U', np.uint16, 12),
                 PlaneInfo.create(2, w, h, 'V', np.uint16, 12))
        enc.encode_frame(DecodedFrame(
            frame_index=0, pts=0, time_seconds=0.0,
            planes=(y, u, v), pixel_format='yuv444p12le',
            width=w, height=h, plane_infos=infos))
        enc.close()

        with open(out, 'rb') as fh:
            head = fh.read(64)
        mdat = head.find(b'TPIC', 20)
        assert mdat >= 0
        # T1.5 转正：默认流 V2（minor 恒 0）；v1.3 minor=2 移至显式 V1 流
        assert head[mdat + 6] == 7, "默认流 version_major 必须为 7（V7-R rANS，ADR-C035）"
        assert head[mdat + 7] == 0, "V2 流 version_minor 恒 0"
        assert head[mdat + 11] == 1
        assert head[mdat + 12] == 12
        head1 = _v1_stream_head(str(tmp_path / "yuv444p12_v1.mov"),
                                'yuv444p12le', profile='hq', crf=6)
        mdat1 = head1.find(b'TPIC', 20)
        assert head1[mdat1 + 6] == 1 and head1[mdat1 + 7] == 2, \
            "V1 legacy：4:4:4 minor=2（v1.3）"

        s = _open_decoded(out)
        try:
            md = s.probe()
            assert md.pixel_format == 'yuv444p12le'
            f = s.read_frame(0)
            assert f.planes[1].shape == (h, w)
            assert int(f.plane_infos[0].bit_depth) == 12
        finally:
            s.close()


class TestR43Gbr:
    """R4.3：GBR 4:4:4 10/12-bit（v1.4 枚举扩展，pf=2 / minor=3 / matrix=0）。

    平面契约 = FFmpeg gbrp 约定（G, B, R 顺序，全幅满刻度 0..2^bd−1）。
    """

    def test_query_support_gbr(self, require_lib) -> None:
        c = topos_binding.ToposCodec()
        assert c.query_support(3, 2, 10, 0) == 0
        assert c.query_support(3, 2, 12, 0) == 0
        assert c.query_support(
            3, 3, 10, 0) == topos_binding.TC_ERR_UNSUPPORTED_PIXEL_FORMAT

    def test_planar_gbr_roundtrip(self, require_lib, tmp_path) -> None:
        """planar gbrp10le 直通：往返 + 帧头 minor=3/pf=2/matrix=0 + G,B,R 序。"""
        from src.shared.media.frame import DecodedFrame, PlaneInfo

        w, h = 64, 40
        g = np.tile(np.linspace(100, 900, w, dtype='<u2'), (h, 1))
        b = np.full((h, w), 300, '<u2')
        r = np.full((h, w), 700, '<u2')
        out = str(tmp_path / "gbr.mov")
        enc = ToposVideoEncoder(out, _enc_config(
            pix_fmt='gbrp10le', profile='hq', crf=6))
        enc.open()
        infos = (PlaneInfo.create(0, w, h, 'G', np.uint16, 10),
                 PlaneInfo.create(1, w, h, 'B', np.uint16, 10),
                 PlaneInfo.create(2, w, h, 'R', np.uint16, 10))
        for i in range(2):
            enc.encode_frame(DecodedFrame(
                frame_index=i, pts=i, time_seconds=i / 24,
                planes=(g, b, r), pixel_format='gbrp10le',
                width=w, height=h, plane_infos=infos))
        enc.close()

        with open(out, 'rb') as fh:
            head = fh.read(256)
        mdat = head.find(b'TPIC', 20)
        assert mdat >= 0
        # T1.5 转正：默认流 V2（major=2，minor 恒 0）；v1.4 的 minor=3
        # 断言移至显式 V1 流
        assert head[mdat + 6] == 7, "默认流 version_major 必须为 7（V7-R rANS，ADR-C035）"
        assert head[mdat + 7] == 0, "V2 流 version_minor 恒 0（ADR-C027 D-6）"
        assert head[mdat + 11] == 2, "GBR 流帧头 pixel_format 必须为 2"
        assert head[mdat + 35] == 0, "GBR 流帧头 color_matrix 必须为 0（identity）"
        head1 = _v1_stream_head(str(tmp_path / "gbr_v1.mov"),
                                'gbrp10le', profile='hq', crf=6)
        mdat1 = head1.find(b'TPIC', 20)
        assert head1[mdat1 + 6] == 1 and head1[mdat1 + 7] == 3, \
            "V1 legacy：GBR minor=3（v1.4 扩展枚举）"

        s = _open_decoded(out)
        try:
            md = s.probe()
            assert md.pixel_format == 'gbrp10le'
            f = s.read_frame(0)
            # G,B,R 三平面全宽 + 顺序保持（各平面内容互异可判别）
            for p in f.planes[:3]:
                assert p.shape == (h, w)
            gerr = float(np.max(np.abs(
                f.planes[0].astype(np.int32) - g.astype(np.int32))))
            berr = float(np.max(np.abs(
                f.planes[1].astype(np.int32) - b.astype(np.int32))))
            rerr = float(np.max(np.abs(
                f.planes[2].astype(np.int32) - r.astype(np.int32))))
            assert gerr <= 8 and berr <= 8 and rerr <= 8, \
                f"GBR 往返 err g={gerr} b={berr} r={rerr}"
        finally:
            s.close()

    def test_packed_rgb_to_gbr(self, require_lib, tmp_path) -> None:
        """packed BGRA → gbrp10le：通道直通（纯绿 → G 高、B/R 低）。"""
        from src.shared.media.frame import DecodedFrame

        out = str(tmp_path / "gbr_packed.mov")
        enc = ToposVideoEncoder(out, _enc_config(
            pix_fmt='gbrp10le', profile='hq', crf=4))
        enc.open()
        bgra = np.zeros((H, W, 4), np.uint8)
        bgra[..., 1] = 255   # 纯绿（BGRA ch1=G）
        bgra[..., 3] = 255
        enc.encode_frame(DecodedFrame(frame_index=0, pts=0, time_seconds=0.0,
                                      data=bgra, pixel_format='bgra',
                                      width=W, height=H))
        enc.close()

        s = _open_decoded(out)
        try:
            f = s.read_frame(0)
            gv = int(f.planes[0][H // 2, W // 2])
            bv = int(f.planes[1][H // 2, W // 2])
            rv = int(f.planes[2][H // 2, W // 2])
            # 满刻度直通：纯绿 G≈1023、B/R≈0
            assert gv >= 980, f"G={gv}（通道序/定标错误？）"
            assert bv <= 40 and rv <= 40, f"B={bv} R={rv}"
        finally:
            s.close()

    def test_alpha_with_gbr_roundtrip(self, require_lib, tmp_path) -> None:
        """GBR + mode2 a8 alpha 组合（topos_gbrap10a8）。"""
        from src.shared.media.frame import DecodedFrame, PlaneInfo

        w, h = 64, 40
        g = np.full((h, w), 400, '<u2')
        b = np.full((h, w), 500, '<u2')
        r = np.full((h, w), 600, '<u2')
        a = (np.arange(w)[None, :] * 255 // (w - 1)).astype('<u2')
        a = np.repeat(a, h, axis=0)
        out = str(tmp_path / "gbr_alpha.mov")
        enc = ToposVideoEncoder(out, _enc_config(
            pix_fmt='topos_gbrap10a8', profile='hq', crf=6, has_alpha=True))
        enc.open()
        infos = (PlaneInfo.create(0, w, h, 'G', np.uint16, 10),
                 PlaneInfo.create(1, w, h, 'B', np.uint16, 10),
                 PlaneInfo.create(2, w, h, 'R', np.uint16, 10),
                 PlaneInfo.create(3, w, h, 'A', np.uint16, 8))
        for i in range(2):
            enc.encode_frame(DecodedFrame(
                frame_index=i, pts=i, time_seconds=i / 24,
                planes=(g, b, r, a), pixel_format='topos_gbrap10a8',
                width=w, height=h, plane_infos=infos))
        enc.close()

        s = _open_decoded(out)
        try:
            md = s.probe()
            assert md.pixel_format == 'topos_gbrap10a8'
            assert md.has_alpha is True
            f = s.read_frame(0)
            assert f.planes[1].shape == (h, w)
            assert int(f.plane_infos[3].bit_depth) == 8
            assert int(f.planes[3].max()) >= 255 - 8
        finally:
            s.close()

    def test_gbr_12bit_roundtrip(self, require_lib, tmp_path) -> None:
        """GBR + 12-bit 组合（pf=2 × bd=12 → minor=3）。"""
        from src.shared.media.frame import DecodedFrame, PlaneInfo

        w, h = 64, 40
        g = np.tile(np.linspace(300, 3800, w, dtype='<u2'), (h, 1))
        b = np.full((h, w), 1600, '<u2')
        r = np.full((h, w), 2400, '<u2')
        out = str(tmp_path / "gbr_bd12.mov")
        enc = ToposVideoEncoder(out, _enc_config(
            pix_fmt='gbrp12le', profile='hq', crf=6))
        enc.open()
        infos = (PlaneInfo.create(0, w, h, 'G', np.uint16, 12),
                 PlaneInfo.create(1, w, h, 'B', np.uint16, 12),
                 PlaneInfo.create(2, w, h, 'R', np.uint16, 12))
        enc.encode_frame(DecodedFrame(
            frame_index=0, pts=0, time_seconds=0.0,
            planes=(g, b, r), pixel_format='gbrp12le',
            width=w, height=h, plane_infos=infos))
        enc.close()

        with open(out, 'rb') as fh:
            head = fh.read(64)
        mdat = head.find(b'TPIC', 20)
        assert mdat >= 0
        # T1.5 转正：默认流 V2（minor 恒 0）；v1.4 minor=3 移至显式 V1 流
        assert head[mdat + 6] == 7, "默认流 version_major 必须为 7（V7-R rANS，ADR-C035）"
        assert head[mdat + 7] == 0, "V2 流 version_minor 恒 0"
        assert head[mdat + 11] == 2
        assert head[mdat + 12] == 12
        head1 = _v1_stream_head(str(tmp_path / "gbrp12_v1.mov"),
                                'gbrp12le', profile='hq', crf=6)
        mdat1 = head1.find(b'TPIC', 20)
        assert head1[mdat1 + 6] == 1 and head1[mdat1 + 7] == 3, \
            "V1 legacy：GBR minor=3（v1.4）"

        s = _open_decoded(out)
        try:
            md = s.probe()
            assert md.pixel_format == 'gbrp12le'
            f = s.read_frame(0)
            assert f.planes[2].shape == (h, w)
            assert int(f.plane_infos[0].bit_depth) == 12
        finally:
            s.close()


class TestR44Tiers:
    """R4.4：Pro444/Extreme 档位激活（profile 5/6，独立矩阵/码控域）。"""

    def test_tiers_available(self, require_lib) -> None:
        from src.shared.codec.topos_profiles import (
            AVAILABLE_TOPOS_TIERS, available_topos_tiers, get_topos_tier)
        assert '4444' in AVAILABLE_TOPOS_TIERS
        assert '4444xq' in AVAILABLE_TOPOS_TIERS
        tiers = {t.tier_id: t for t in available_topos_tiers()}
        assert tiers['4444'].available is True
        assert tiers['4444xq'].available is True
        assert get_topos_tier('4444').native_profile == 5
        assert get_topos_tier('4444xq').native_profile == 6
        assert get_topos_tier('hq').native_profile == 3

    def test_query_support_profiles(self, require_lib) -> None:
        c = topos_binding.ToposCodec()
        assert c.query_support(5, 1, 10, 0) == 0
        assert c.query_support(5, 2, 12, 0) == 0
        assert c.query_support(6, 1, 12, 0) == 0
        assert c.query_support(6, 2, 12, 0) == 0
        assert c.query_support(
            5, 0, 10, 0) == topos_binding.TC_ERR_UNSUPPORTED_PIXEL_FORMAT
        assert c.query_support(
            6, 1, 10, 0) == topos_binding.TC_ERR_UNSUPPORTED_PIXEL_FORMAT
        assert c.query_support(
            4, 0, 10, 0) == topos_binding.TC_ERR_UNSUPPORTED_PROFILE

    def test_4444_export_writes_profile5(self, require_lib, tmp_path) -> None:
        from src.shared.media.frame import DecodedFrame, PlaneInfo

        w, h = 64, 40
        y = np.tile(np.linspace(300, 3800, w, dtype='<u2'), (h, 1))
        u = np.full((h, w), 1600, '<u2')
        v = np.full((h, w), 2400, '<u2')
        out = str(tmp_path / "xq4444.mov")
        enc = ToposVideoEncoder(out, _enc_config(
            pix_fmt='yuv444p12le', profile='4444', crf=6))
        enc.open()
        infos = (PlaneInfo.create(0, w, h, 'Y', np.uint16, 12),
                 PlaneInfo.create(1, w, h, 'U', np.uint16, 12),
                 PlaneInfo.create(2, w, h, 'V', np.uint16, 12))
        enc.encode_frame(DecodedFrame(
            frame_index=0, pts=0, time_seconds=0.0,
            planes=(y, u, v), pixel_format='yuv444p12le',
            width=w, height=h, plane_infos=infos))
        enc.close()

        with open(out, 'rb') as fh:
            head = fh.read(256)
        mdat = head.find(b'TPIC', 20)
        assert mdat >= 0
        assert head[mdat + 10] == 5, "Pro444 流帧头 profile 必须为 5"
        # T1.5 转正：默认流 V2（major=2，minor 恒 0）；v1.3 minor=2 断言
        # 移至显式 V1 流（profile5 在 V1 下 pf=1 → minor=2）
        assert head[mdat + 6] == 7, "默认流 version_major 必须为 7（V7-R rANS，ADR-C035）"
        assert head[mdat + 7] == 0, "V2 流 version_minor 恒 0"
        assert head[mdat + 12] == 12

        s = _open_decoded(out)
        try:
            md = s.probe()
            assert md.codec_profile == 'Topos 4444'
            assert md.pixel_format == 'yuv444p12le'
        finally:
            s.close()

    def test_extreme_export_writes_profile6(self, require_lib, tmp_path) -> None:
        from src.shared.media.frame import DecodedFrame, PlaneInfo

        w, h = 64, 40
        g = np.tile(np.linspace(300, 3800, w, dtype='<u2'), (h, 1))
        b = np.full((h, w), 1600, '<u2')
        r = np.full((h, w), 2400, '<u2')
        out = str(tmp_path / "extreme.mov")
        enc = ToposVideoEncoder(out, _enc_config(
            pix_fmt='gbrp12le', profile='4444xq', crf=6))
        enc.open()
        infos = (PlaneInfo.create(0, w, h, 'G', np.uint16, 12),
                 PlaneInfo.create(1, w, h, 'B', np.uint16, 12),
                 PlaneInfo.create(2, w, h, 'R', np.uint16, 12))
        enc.encode_frame(DecodedFrame(
            frame_index=0, pts=0, time_seconds=0.0,
            planes=(g, b, r), pixel_format='gbrp12le',
            width=w, height=h, plane_infos=infos))
        enc.close()

        with open(out, 'rb') as fh:
            head = fh.read(256)
        mdat = head.find(b'TPIC', 20)
        assert mdat >= 0
        assert head[mdat + 10] == 6, "Extreme 流帧头 profile 必须为 6"
        assert head[mdat + 11] == 2   # GBR
        # T1.5 转正：默认流 V2（major=2，minor 恒 0）；v1.4 minor=3 断言
        # 移至显式 V1 流（profile6 GBR 在 V1 下 minor=3）
        assert head[mdat + 6] == 7, "默认流 version_major 必须为 7（V7-R rANS，ADR-C035）"
        assert head[mdat + 7] == 0, "V2 流 version_minor 恒 0"

        s = _open_decoded(out)
        try:
            md = s.probe()
            assert md.codec_profile == 'Topos 4444 XQ'
        finally:
            s.close()

    def test_tier_format_constraints(self, require_lib, tmp_path) -> None:
        """档位格式约束：Pro444 拒 4:2:2；Extreme 拒 10-bit / 4:2:2。"""
        with pytest.raises(ValueError, match='4:4:4'):
            ToposVideoEncoder(str(tmp_path / "bad1.mov"), _enc_config(
                pix_fmt='yuv422p10le', profile='4444', crf=6))
        with pytest.raises(ValueError, match='12'):
            ToposVideoEncoder(str(tmp_path / "bad2.mov"), _enc_config(
                pix_fmt='yuv444p10le', profile='4444xq', crf=6))
        with pytest.raises(ValueError, match='4:4:4'):
            ToposVideoEncoder(str(tmp_path / "bad3.mov"), _enc_config(
                pix_fmt='yuv422p12le', profile='4444xq', crf=6))

    def test_extreme_roundtrip_quality(self, require_lib, tmp_path) -> None:
        """Extreme 高码率档：qp6 下 GBR 12-bit 往返误差界。"""
        from src.shared.media.frame import DecodedFrame, PlaneInfo

        w, h = 64, 40
        g = np.tile(np.linspace(300, 3800, w, dtype='<u2'), (h, 1))
        b = np.full((h, w), 1600, '<u2')
        r = np.full((h, w), 2400, '<u2')
        out = str(tmp_path / "extreme_q.mov")
        enc = ToposVideoEncoder(out, _enc_config(
            pix_fmt='gbrp12le', profile='4444xq', crf=6))
        enc.open()
        infos = (PlaneInfo.create(0, w, h, 'G', np.uint16, 12),
                 PlaneInfo.create(1, w, h, 'B', np.uint16, 12),
                 PlaneInfo.create(2, w, h, 'R', np.uint16, 12))
        for i in range(2):
            enc.encode_frame(DecodedFrame(
                frame_index=i, pts=i, time_seconds=i / 24,
                planes=(g, b, r), pixel_format='gbrp12le',
                width=w, height=h, plane_infos=infos))
        enc.close()
        s = _open_decoded(out)
        try:
            f = s.read_frame(0)
            err = float(np.max(np.abs(
                f.planes[0].astype(np.int32) - g.astype(np.int32))))
            assert err <= 24, f"Extreme 往返 err={err}"
        finally:
            s.close()


# ---------------------------------------------------------------------------
# R5：容器能力声明——音频轨为 V2.0 非目标，preflight/UI 据此在编码前告知
# ---------------------------------------------------------------------------


class TestR5ContainerCaps:
    def test_movie_caps_declare_audio_v1_1(self) -> None:
        """v1.1 音频轨（ADR-C051）：audio_tracks 解冻为 True；
        v1.4（ADR-C052）：edit lists 解冻为音频 trak 最小子集
        （仅 mp4a priming；视频 trak 仍禁——container_spec §10）。"""
        from src.shared.codec.topos_binding import TOPOS_MOVIE_CAPS

        assert TOPOS_MOVIE_CAPS["video"] is True
        assert TOPOS_MOVIE_CAPS["audio_tracks"] is True
        assert TOPOS_MOVIE_CAPS["field_order"] is False
        assert TOPOS_MOVIE_CAPS["edit_lists"] == "audio_only"

    def test_preflight_warns_before_encode_when_audio_present(self, tmp_path) -> None:
        """v1.1 音频轨（plan M-A6）：include_audio + 受支持编解码（aac）
        → 音频写入容器、原 topos.video_only 警告解除。审计 R5 任务 4 的
        告警前置语义由「未勾选音频」与「不支持编解码」两个新分支延续
        （见 tests/deliver/test_video_export_preflight.py）。"""
        from src.features.deliver.core.video_export_job import (
            AudioConfig,
            VideoConfig,
            VideoExportJob,
        )
        from src.features.deliver.core.video_export_preflight import (
            PreflightContext,
            run_video_export_preflight,
        )

        track = type("T", (), {"track_id": "a1", "name": "A1",
                               "clips": [("c", 0, 5)], "strip_eq": None})()
        seq = type("S", (), {
            "frame_count": 10, "fps": 25.0,
            "audio_tracks": [track], "subtitle_tracks": [],
            "all_tracks": [],
        })()
        job = VideoExportJob(
            timeline_id="t1",
            output_path=str(tmp_path / "x.mov"),
            video_config=VideoConfig(codec="topos", container="mov"),
            audio_config=AudioConfig(include_audio=True, codec="aac"),
        )
        result = run_video_export_preflight(job, PreflightContext(project=object(), sequence=seq))
        assert result.ok
        assert not any(i.code == "topos.video_only" for i in result.warnings)


# ---------------------------------------------------------------------------
# R6：性能门槛整改——线程池调度 API 与编码器设置/恢复
# ---------------------------------------------------------------------------


class TestR6SliceThreads:
    def test_binding_roundtrip_and_clamp(self) -> None:
        """set/slice_threads 往返 + 下界钳位（1..16；进程级）。

        上界 M10-6 起 = TC_SLICE_MAX_THREADS=16（16~24 逻辑核机器实测
        t16 收益）；默认值 = min(逻辑核数, 16)，同样落在 1..16。
        """
        from src.shared.codec.topos_binding import ToposCodec

        codec = ToposCodec()
        prev = codec.slice_threads()
        try:
            assert 1 <= prev <= 16
            got = codec.set_slice_threads(1)
            assert got == 1
            assert codec.slice_threads() == 1
            # 上界钳位（TC_SLICE_MAX_THREADS=16）
            assert codec.set_slice_threads(99) == 16
            # 下界钳位
            assert codec.set_slice_threads(0) == 1
        finally:
            codec.set_slice_threads(prev)

    def test_encoder_applies_and_restores(self, tmp_path) -> None:
        """ToposVideoEncoder(slice_threads=N)：首次编码前登记、close 注销
        （复验 P1-17：登记表语义——注销后回到基线/剩余登记者）。"""
        import numpy as np

        from src.shared.codec.slice_threads_policy import SLICE_THREADS_POLICY
        from src.shared.codec.topos_binding import ToposCodec
        from src.shared.media.frame import DecodedFrame, PlaneInfo

        codec = ToposCodec()
        prev = codec.slice_threads()
        SLICE_THREADS_POLICY.reset_for_test()  # 顺序无关：清基线后重捕获
        try:
            codec.set_slice_threads(2)
            w, h = 64, 48
            cw = w // 2
            out = str(tmp_path / "r6_threads.mov")
            enc = ToposVideoEncoder(out, _enc_config(width=w, height=h),
                                    slice_threads=1)
            assert not enc._threads_registered  # 惰性：编码前不登记
            enc.open()
            assert not enc._threads_registered  # open 仍不登记
            infos = (PlaneInfo.create(0, w, h, 'Y', np.uint16, 10),
                     PlaneInfo.create(1, cw, h, 'U', np.uint16, 10),
                     PlaneInfo.create(2, cw, h, 'V', np.uint16, 10))
            g = np.full((h, w), 512, dtype=np.uint16)
            c = np.full((h, cw), 512, dtype=np.uint16)
            enc.encode_frame(DecodedFrame(
                frame_index=0, pts=0, time_seconds=0.0,
                planes=(g, c.copy(), c.copy()), pixel_format='yuv422p10le',
                width=w, height=h, plane_infos=infos))
            assert enc._threads_registered
            assert SLICE_THREADS_POLICY.effective() == 1
            assert codec.slice_threads() == 1
            enc.close()
            assert codec.slice_threads() == 2  # 注销后回到基线
            assert os.path.exists(out)
        finally:
            codec.set_slice_threads(prev)

    def test_two_encoders_out_of_order_close_preserves_thread_policy(
            self, require_lib, tmp_path) -> None:
        """复验 P1-17：交错编码器乱序 close 不得破坏在线编码器的线程策略。

        旧缺陷（嵌套保存/恢复）：A(1) open → B(8) open → A close 恢复 2
        （B 声明的 8 被覆盖）→ B 以错误线程数运行。
        登记表语义：注销 A 后生效值仍 = B 的 8，直至 B 注销才回基线。
        """
        import numpy as np

        from src.shared.codec.slice_threads_policy import SLICE_THREADS_POLICY
        from src.shared.codec.topos_binding import ToposCodec as _TC
        from src.shared.media.frame import DecodedFrame

        w, h = 64, 48
        bgr = np.zeros((h, w, 3), np.uint8)
        bgr[..., 1] = 128

        # C10：进程级 native 线程数是测试全局状态——测试前快照，测试后
        # 恢复。历史直接 reset_for_test + set(2)，进程状态泄漏到后续测试
        # （与 RAW 直出组合回归崩溃/像素偏差相关，见计划 C10/C11）。
        prev_threads = _TC().slice_threads()
        try:
            self._run_out_of_order_close(tmp_path, w, h)
        finally:
            SLICE_THREADS_POLICY.reset_for_test()
            _TC().set_slice_threads(prev_threads)

    def _run_out_of_order_close(self, tmp_path, w: int, h: int) -> None:
        import numpy as np

        from src.shared.codec.slice_threads_policy import SLICE_THREADS_POLICY
        from src.shared.media.frame import DecodedFrame

        bgr = np.zeros((h, w, 3), np.uint8)
        bgr[..., 1] = 128

        SLICE_THREADS_POLICY.reset_for_test()
        from src.shared.codec.topos_binding import ToposCodec as _TC
        _TC().set_slice_threads(2)

        def _mk(name, n):
            enc = ToposVideoEncoder(
                str(tmp_path / name), _enc_config(width=w, height=h,
                                                  pix_fmt="bgr24"),
                slice_threads=n)
            enc.open()
            enc.encode_frame(DecodedFrame(
                frame_index=0, pts=0, time_seconds=0.0,
                data=bgr.copy(), pixel_format="bgr24", width=w, height=h))
            return enc

        a = _mk("a.mov", 1)
        b = _mk("b.mov", 8)
        assert SLICE_THREADS_POLICY.effective() == 8  # 后登记者生效
        a.close()                                      # 乱序：先关 A
        assert SLICE_THREADS_POLICY.effective() == 8, \
            "A 注销不得覆盖在线 B 的线程策略（P1-17）"
        b.close()
        assert SLICE_THREADS_POLICY.active_count() == 0  # 全部注销回基线
        assert SLICE_THREADS_POLICY.effective() == 2

    def test_thread_policy_role_qos_and_diagnostics(self, caplog) -> None:
        """P1-05（过渡期）：异角色登记切换记 QoS warning；describe() 可诊断。"""
        import logging

        from src.shared.codec.slice_threads_policy import SLICE_THREADS_POLICY

        SLICE_THREADS_POLICY.reset_for_test()
        try:
            SLICE_THREADS_POLICY.register("pb-1", 16, role="playback")
            with caplog.at_level(logging.WARNING,
                                 logger="src.shared.codec.slice_threads_policy"):
                SLICE_THREADS_POLICY.register("exp-1", 8, role="export")
            assert any("playback" in r.message and "export" in r.message
                       for r in caplog.records)
            d = SLICE_THREADS_POLICY.describe()
            assert d["effective"] == 8  # 后登记者生效（语义不变）
            roles = {r["key"]: r["role"] for r in d["registrations"]}
            assert roles == {"pb-1": "playback", "exp-1": "export"}
            # 同角色追加登记不触发 QoS（无跨角色抢占）
            SLICE_THREADS_POLICY.register("exp-2", 4, role="export")
            assert SLICE_THREADS_POLICY.describe()["effective"] == 4
        finally:
            SLICE_THREADS_POLICY.unregister("pb-1")
            SLICE_THREADS_POLICY.unregister("exp-1")
            SLICE_THREADS_POLICY.unregister("exp-2")

    def test_encoder_none_leaves_untouched(self, tmp_path) -> None:
        """slice_threads=None（默认）：全程不干预进程级设置。"""
        from src.shared.codec.topos_binding import ToposCodec
        from src.shared.export.topos_encoder import ToposVideoEncoder

        codec = ToposCodec()
        prev = codec.slice_threads()
        try:
            cfg = type("C", (), {
                "codec": "topos", "profile": "standard",
                "width": 64, "height": 48, "fps": type(
                    "F", (), {"num": 24, "den": 1})(),
                "pix_fmt": "yuv422p10le", "has_alpha": False, "crf": None,
                "alpha_premultiplied": False,
            })()
            enc = ToposVideoEncoder(str(tmp_path / "none.mov"), cfg)
            enc.open()
            enc.abort()
            assert codec.slice_threads() == prev
        finally:
            codec.set_slice_threads(prev)


# =============================================================================
# 对标达芬奇路线 阶段 1：帧间反馈单遍码控（长期均值贴档位目标）
# =============================================================================


class TestRateFeedbackController:
    """控制器模型级单测：bytes ≈ C·2^(−qp/8) 模型下的收敛/变复杂度/确定性。"""

    def _model_bytes(self, c_factor: float, qp: int, target: int, qp_star: int = 30) -> int:
        """bytes = C·2^(−qp/8)，C 定标为 qp=qp_star 时恰为 target×factor。"""
        return max(1, int(target * c_factor * 2.0 ** ((qp_star - qp) / 8.0)))

    def test_seed_and_steady_state(self) -> None:
        from src.shared.export.topos_rate_control import ToposRateFeedback

        target = 100_000
        rc = ToposRateFeedback(target)
        assert not rc.active
        rc.seed(30, target)
        assert rc.active and rc.qp == 30
        for _ in range(20):
            rc.note(self._model_bytes(1.0, rc.qp, target))
        assert abs(rc.qp - 30) <= 2  # 恒定复杂度下贴住定标点

    def test_complexity_shift_converges(self) -> None:
        from src.shared.export.topos_rate_control import ToposRateFeedback

        target = 100_000
        rc = ToposRateFeedback(target)
        rc.seed(30, target)
        for _ in range(10):  # 稳态
            rc.note(self._model_bytes(1.0, rc.qp, target))
        for _ in range(10):   # 复杂度 ×4 → 目标 qp* = 30+16
            rc.note(self._model_bytes(4.0, rc.qp, target))
        assert abs(rc.qp - 46) <= 3  # 每帧钳 ±4，10 帧内收敛
        frames, total, dev = rc.stats()
        assert frames == 21 and abs(dev) < 1.5

    def test_qp_clamped_to_bounds(self) -> None:
        from src.shared.export.topos_rate_control import ToposRateFeedback

        rc = ToposRateFeedback(1000, qp_min=10, qp_max=20)
        rc.seed(10, 1000)
        for _ in range(30):
            rc.note(1_000_000)  # 持续严重超目标 → 顶到 qp_max
        assert rc.qp == 20
        rc2 = ToposRateFeedback(1_000_000, qp_min=10, qp_max=20)
        rc2.seed(20, 1)
        for _ in range(30):
            rc2.note(1)  # 持续欠目标 → 压到 qp_min
        assert rc2.qp == 10

    def test_deterministic_sequence(self) -> None:
        from src.shared.export.topos_rate_control import ToposRateFeedback

        target = 50_000
        seq_a, seq_b = [], []
        for out in (seq_a, seq_b):
            rc = ToposRateFeedback(target)
            rc.seed(28, target)
            out.append(rc.qp)
            for i in range(50):
                factor = 1.0 + (i % 7) * 0.6
                rc.note(self._model_bytes(factor, rc.qp, target))
                out.append(rc.qp)
        assert seq_a == seq_b  # 同字节序列 → 同 qp 序列（§11.1 序列级）


class TestRateFeedbackSinglePass:
    """产品路径：rate 档（无 crf）单遍反馈——均值码率贴目标 + 序列确定性。

    素材用真实 2K 测试源（tests/test_videos/2K_prores422HQ.mov，未入库——
    缺失时跳过）：合成噪声/上采样纹理的熵与真实素材差一个量级，档位
    bpp 定标（1080p 实拍数据率中值）只对真实内容可达。
    """

    SRC_2K = "tests/test_videos/2K_prores422HQ.mov"

    def _real_frames(self, n: int):
        import av
        from src.shared.media.frame import DecodedFrame, PlaneInfo

        path = os.path.join(os.path.dirname(__file__), "..", "..", self.SRC_2K)
        if not os.path.isfile(path):
            pytest.skip(f"测试素材缺失：{self.SRC_2K}")
        w, h, cw = 1920, 1080, 960
        infos = (PlaneInfo.create(0, w, h, 'Y', np.uint16, 10),
                 PlaneInfo.create(1, cw, h, 'U', np.uint16, 10),
                 PlaneInfo.create(2, cw, h, 'V', np.uint16, 10))
        frames = []
        with av.open(path) as cont:
            ctx = cont.streams.video[0].codec_context
            ctx.thread_count = 0
            for i, f in enumerate(cont.decode(video=0)):
                if i >= n:
                    break
                planes = []
                for p, pw in zip(f.planes, (w, cw, cw)):
                    arr = np.frombuffer(bytes(p), dtype='<u2').reshape(
                        p.height, p.line_size // 2)
                    planes.append(np.ascontiguousarray(arr[:, :pw]))
                frames.append(DecodedFrame(
                    frame_index=i, pts=i, time_seconds=i / 25,
                    planes=tuple(planes), pixel_format='yuv422p10le',
                    width=w, height=h, plane_infos=infos))
        return frames

    def _mdat_payload(self, path: str) -> int:
        import struct
        with open(path, 'rb') as fh:
            data = fh.read()
        pos = 0
        while pos + 8 <= len(data):
            size = struct.unpack('>I', data[pos:pos + 4])[0]
            btype = data[pos + 4:pos + 8]
            hdr = 8
            if size == 1:  # 64 位扩展长度：largesize 跟在类型后
                if pos + 16 > len(data):
                    break
                size = struct.unpack('>Q', data[pos + 8:pos + 16])[0]
                hdr = 16
            if size < hdr:
                break
            if btype == b'mdat':
                return size - hdr
            pos += size
        raise AssertionError("未找到 mdat 盒")

    def test_average_bitrate_tracks_tier_target(self, require_lib, tmp_path) -> None:
        from src.shared.codec.topos_profiles import get_topos_tier

        n = 24
        frames = self._real_frames(n)
        out = str(tmp_path / "rc_avg.mov")
        enc = ToposVideoEncoder(out, _enc_config(
            crf=None, width=1920, height=1080))
        enc.open()
        for f in frames:
            enc.encode_frame(f)
        enc.close()
        assert enc._rc is not None and enc._rc.frames == n
        target = get_topos_tier('standard').frame_target_bytes(1920, 1080)
        ratio = self._mdat_payload(out) / (n * target)
        assert 0.85 <= ratio <= 1.15, f"平均码率偏差过大：ratio={ratio:.3f}"

    def test_sequence_deterministic_bytes(self, require_lib, tmp_path) -> None:
        import hashlib

        frames = self._real_frames(12)
        digests = []
        for name in ("rc_det_a.mov", "rc_det_b.mov"):
            out = str(tmp_path / name)
            enc = ToposVideoEncoder(out, _enc_config(
                crf=None, width=1920, height=1080))
            enc.open()
            for f in frames:
                enc.encode_frame(f)
            enc.close()
            with open(out, 'rb') as fh:
                digests.append(hashlib.sha256(fh.read()).digest())
        assert digests[0] == digests[1]


# =============================================================================
# O1（复验 2026-08-31）：格式协商与资源边界——P0-01 / P1-02 / P1-03
# =============================================================================


class TestO1FormatNegotiation:
    def test_constructor_rejects_unknown_pix_fmt(self, tmp_path) -> None:
        """P0-01（最小复现 11）：完全非法 pix_fmt 不得默认成 422/10-bit。"""
        from src.shared.export.topos_encoder import ToposVideoEncoder

        with pytest.raises(ValueError, match="不在编码域"):
            ToposVideoEncoder(
                str(tmp_path / "bad.mov"), _enc_config(pix_fmt="totally_invalid"))

    def test_constructor_rejects_pseudo_numbered_fmt(self, tmp_path) -> None:
        """形如 'yuv422p99le' 的伪标格式同样显式拒绝。"""
        from src.shared.export.topos_encoder import ToposVideoEncoder

        with pytest.raises(ValueError, match="不在编码域"):
            ToposVideoEncoder(
                str(tmp_path / "bad2.mov"), _enc_config(pix_fmt="yuv422p99le"))

    def test_planar_input_format_must_match_encoder_config(
            self, require_lib, tmp_path) -> None:
        """P0-01（最小复现 2）：config=yuv444p10le 而 frame=gbrp10le 时，
        G/B/R 不得被静默标成 Y/U/V——必须显式报错。"""
        import numpy as np

        from src.shared.media.frame import DecodedFrame, PlaneInfo

        w, h = 64, 48
        g = np.full((h, w), 512, dtype=np.uint16)
        out = str(tmp_path / "mismatch.mov")
        enc = ToposVideoEncoder(out, _enc_config(
            width=w, height=h, pix_fmt="yuv444p10le"))
        enc.open()
        try:
            infos = tuple(
                PlaneInfo.create(i, w, h, c, np.uint16, 10)
                for i, c in enumerate(("G", "B", "R")))
            frame = DecodedFrame(
                frame_index=0, pts=0, time_seconds=0.0,
                planes=(g.copy(), g.copy(), g.copy()),
                pixel_format="gbrp10le", width=w, height=h,
                plane_infos=infos)
            with pytest.raises(RuntimeError, match="错配"):
                enc.encode_frame(frame)
        finally:
            enc.abort()
        assert not os.path.exists(out)

    def test_planar_alpha_dtype_must_be_uint16(
            self, require_lib, tmp_path) -> None:
        """P1-02：float Alpha 平面不得被 astype('<u4') 静默截断。"""
        import numpy as np

        from src.shared.media.frame import DecodedFrame, PlaneInfo

        w, h = 64, 48
        cw = w // 2
        out = str(tmp_path / "flt_a.mov")
        enc = ToposVideoEncoder(out, _enc_config(
            width=w, height=h, pix_fmt="topos_yuva422p10a12",
            has_alpha=True, crf=None))
        enc.open()
        try:
            infos = (PlaneInfo.create(0, w, h, 'Y', np.uint16, 10),
                     PlaneInfo.create(1, cw, h, 'U', np.uint16, 10),
                     PlaneInfo.create(2, cw, h, 'V', np.uint16, 10),
                     PlaneInfo.create(3, w, h, 'A', np.uint16, 12))
            planes = (np.full((h, w), 512, np.uint16),
                      np.full((h, cw), 512, np.uint16),
                      np.full((h, cw), 512, np.uint16),
                      np.full((h, w), 0.5, np.float32))
            with pytest.raises(RuntimeError, match="Alpha 平面要求 uint16"):
                enc.encode_frame(DecodedFrame(
                    frame_index=0, pts=0, time_seconds=0.0,
                    planes=planes, pixel_format="topos_yuva422p10a12",
                    width=w, height=h, plane_infos=infos))
        finally:
            enc.abort()

    def test_mux_close_failure_does_not_publish(
            self, require_lib, tmp_path) -> None:
        """P1-03：mux close 失败 → 不发布输出、删临时文件、显式抛错。"""
        import numpy as np

        from src.shared.media.frame import DecodedFrame

        w, h = 64, 48
        out = str(tmp_path / "closefail.mov")
        enc = ToposVideoEncoder(out, _enc_config(width=w, height=h))
        enc.open()
        bgr = np.zeros((h, w, 3), np.uint8)
        bgr[..., 2] = 200
        enc.encode_frame(DecodedFrame(
            frame_index=0, pts=0, time_seconds=0.0,
            data=bgr, pixel_format="bgr24", width=w, height=h))
        assert enc._mux is not None

        real_close = enc._mux.close

        def _boom():
            real_close()
            raise RuntimeError("injected close failure")

        enc._mux.close = _boom
        with pytest.raises(RuntimeError, match="mux close 失败"):
            enc.close()
        assert not os.path.exists(out), "close 失败不得发布输出文件"
        assert not os.path.exists(out + ".topos-tmp"), "临时文件应被清理"


# =============================================================================
# O2（复验 2026-08-31）：profile 协商与应用一致——P1-06/07/08/09/11
# =============================================================================


class TestO2ProfileNegotiation:
    def test_binding_invalid_capability_rejected(self, require_lib) -> None:
        """P1-07（最小复现 11）：(999,999,999) 不再被对拍成 (3,0,10)。"""
        from src.shared.codec.topos_binding import ToposCodec, ToposCodecError

        codec = ToposCodec()
        with pytest.raises(ToposCodecError, match="profile"):
            codec.movie_config(64, 48, profile=999)
        with pytest.raises(ToposCodecError, match="pixel_format"):
            codec.movie_config(64, 48, pixel_format=999)
        with pytest.raises(ToposCodecError, match="bit_depth"):
            codec.movie_config(64, 48, bit_depth=999)
        with pytest.raises(ToposCodecError, match="alpha_mode"):
            codec.movie_config(64, 48, alpha_mode=7)
        with pytest.raises(ToposCodecError, match="alpha_bit_depth"):
            codec.movie_config(64, 48, alpha_mode=2, alpha_bit_depth=11)
        # 合法值不受影响
        cfg = codec.movie_config(64, 48, profile=6, pixel_format=2,
                                      bit_depth=12, alpha_mode=2,
                                      alpha_bit_depth=8)
        assert (cfg.profile, cfg.pixel_format, cfg.bit_depth) == (6, 2, 12)

    def test_capability_summary_distinguishes_presets_and_profiles(self) -> None:
        """P2-09：profile5/6 进同一张表；逐档 is_preset 区分预设/独立 profile。"""
        from src.shared.codec.topos_profiles import topos_capability_summary

        s = topos_capability_summary()
        assert "profile5" in s["_codec"]["bitstream_profiles"]
        assert "profile6" in s["_codec"]["bitstream_profiles"]
        assert s["_codec"]["tiers_are_presets"] is False
        for tid in ("proxy", "lt", "standard", "hq"):
            assert s[tid]["is_preset"] is True, tid
        for tid in ("4444", "4444xq"):
            assert s[tid]["is_preset"] is False, tid
            assert s[tid]["native_profile"] in (5, 6)

    def test_gbr_plane_components_and_output_mode(
            self, require_lib, tmp_path) -> None:
        """P1-09：GBR 文件解码平面组件 = G/B/R/A，output_mode=planar_rgb。"""
        import numpy as np

        from src.shared.media.frame import DecodedFrame

        w, h = 64, 48
        out = str(tmp_path / "gbr.mov")
        enc = ToposVideoEncoder(out, _enc_config(
            width=w, height=h, pix_fmt="gbrp10le", profile="hq"))
        enc.open()
        bgr = np.zeros((h, w, 3), np.uint8)
        bgr[..., 0] = 30   # B
        bgr[..., 1] = 90   # G
        bgr[..., 2] = 200  # R
        enc.encode_frame(DecodedFrame(
            frame_index=0, pts=0, time_seconds=0.0,
            data=bgr, pixel_format="bgr24", width=w, height=h))
        enc.close()

        from src.shared.media.factory import MediaSourceFactory

        src = MediaSourceFactory.create(out)
        src.open()
        try:
            assert src.output_mode == "planar_rgb"
            f = src.read_frame(0)
            comps = [pi.component for pi in f.plane_infos]
            assert comps == ["G", "B", "R"], comps
            # 平面序 (G,B,R)：数值直接可验
            assert abs(int(f.planes[0][0, 0]) - int(round(90 / 255 * 1023))) <= 4
            assert abs(int(f.planes[1][0, 0]) - int(round(30 / 255 * 1023))) <= 4
            assert abs(int(f.planes[2][0, 0]) - int(round(200 / 255 * 1023))) <= 4
        finally:
            src.close()

    def test_six_tier_app_roundtrip_headers(self, require_lib, tmp_path) -> None:
        """P1-10：六档真实 TimelineExporter 导出 → 解码头/格式逐档正确。"""
        from src.features.edit.core.timeline_export import ExportCodec

        # v1.7 tpcD：探测侧恢复导出档位的精确展示名（422 四预设不再共用
        # profile 3 的粗粒度名）
        want_names = {
            ExportCodec.TOPOS_PROXY: "Topos 422 Proxy",
            ExportCodec.TOPOS_LT: "Topos 422 LT",
            ExportCodec.TOPOS: "Topos 422",
            ExportCodec.TOPOS_HQ: "Topos 422 HQ",
            ExportCodec.TOPOS_4444: "Topos 4444",
            ExportCodec.TOPOS_4444XQ: "Topos 4444 XQ",
        }
        cases = (
            (ExportCodec.TOPOS_PROXY, "topos_yuva422p10a12", 3, 0, 10),
            (ExportCodec.TOPOS_LT, "topos_yuva422p10a12", 3, 0, 10),
            (ExportCodec.TOPOS, "topos_yuva422p10a12", 3, 0, 10),
            (ExportCodec.TOPOS_HQ, "topos_yuva422p10a12", 3, 0, 10),
            (ExportCodec.TOPOS_4444, "topos_yuva444p10a12", 5, 1, 10),
            (ExportCodec.TOPOS_4444XQ, "topos_yuva444p12a12", 6, 1, 12),
        )
        import numpy as np

        from src.shared.media.frame import DecodedFrame
        from src.shared.media.metadata import Rational

        from src.features.edit.core.timeline_export import (
            ExportRangeType,
            ExportSource,
            TimelineExportConfig,
            TimelineExporter,
        )
        from src.features.edit.core.timeline_v2 import (
            ClipInstance,
            EditTrack,
            TimelineSequence,
        )
        from src.features.edit.core.project import EditProject
        from src.shared.media.factory import MediaSourceFactory

        w, h = 64, 48
        for codec, want_pf, want_profile, want_pfe, want_bd in cases:
            bgra = np.zeros((h, w, 4), np.uint8)
            bgra[..., 0] = 40
            bgra[..., 1] = 80
            bgra[..., 2] = 160
            bgra[..., 3] = 255
            frames = [DecodedFrame(
                frame_index=i, pts=i, time_seconds=i / 24.0,
                data=bgra.copy(), pixel_format="bgra",
                width=w, height=h) for i in range(2)]

            project = EditProject.create("O2")
            spath = str(tmp_path / f"src_{codec.value}.mov")
            with open(spath, "wb") as fh:
                fh.write(b"placeholder")
            project = project.import_asset(
                path=spath, media_type="video",
                metadata={"width": w, "height": h, "fps": 24.0,
                          "total_frames": 2, "duration_seconds": 2 / 24.0})
            asset_id = list(project.assets.keys())[-1]
            clip = ClipInstance(
                clip_id="c0", asset_id=asset_id, track_id="t0",
                timeline_start=0, timeline_duration=2,
                source_start=0, source_duration=2)
            seq = TimelineSequence(
                name="O2", width=w, height=h, fps=Rational(24, 1),
                video_tracks=(EditTrack(
                    track_id="t0", name="V1", track_type="video",
                    index=0, clips=(clip,)),))

            class _P:
                def open_clip(self, path, **_kw):
                    return str(path)

                def request_frame(self, _cid, frame, quality="master"):
                    return frames[int(frame) % 2]

                def close_clip(self, *_a):
                    pass

            out = str(tmp_path / f"{codec.value}.mov")
            exporter = TimelineExporter(
                project=project, sequence=seq, frame_provider=_P())
            cfg = TimelineExportConfig(
                source=ExportSource.TIMELINE, timeline_id=seq.timeline_id,
                range_type=ExportRangeType.ENTIRE, codec=codec,
                output_path=out, has_alpha=True, include_audio=False,
                quality=6, quality_mode="crf", resolution=(w, h))
            assert exporter.export_timeline(cfg).success, codec.value

            src = MediaSourceFactory.create(out)
            src.open()
            try:
                md = src.probe()
                assert md.codec_profile == want_names[codec], \
                    (codec.value, md.codec_profile)
                assert str(md.pixel_format) == want_pf, \
                    (codec.value, md.pixel_format)
                f = src.read_frame(0)
                # 444 档色度全宽
                want_cw = w if want_pfe != 0 else (w + 1) // 2
                assert f.planes[1].shape == (h, want_cw), \
                    (codec.value, f.planes[1].shape)
                assert len(f.planes) == 4, codec.value
            finally:
                src.close()

    def test_budget_flag_ratio_saturated_roundtrip(
            self, require_lib, tmp_path) -> None:
        """P1-11：tpcB ratio_saturated 标志位写入/读回（0x0008，v1.3）。"""
        import ctypes

        import numpy as np

        from src.shared.codec import topos_binding
        from src.shared.codec.topos_binding import (
            TOPOS_CODEC_ABI_VERSION, ToposCodec, ToposMuxFile, ToposMovieFile,
        )

        codec = ToposCodec()
        w, h = 64, 48
        movie_cfg = codec.movie_config(
            w, h, qp=8, alpha_mode=2, alpha_bit_depth=8)
        fc = topos_binding._CFrameConfig()
        fc.struct_size = ctypes.sizeof(topos_binding._CFrameConfig)
        fc.abi_version = TOPOS_CODEC_ABI_VERSION
        fc.visible_width, fc.visible_height = w, h
        fc.qp_base = 8
        fc.qmatrix_id = 1
        fc.alpha_mode = 2
        fc.alpha_bit_depth = 8
        fc.color_range = fc.color_primaries = fc.color_transfer = 1
        fc.color_matrix = 1
        fc.sar_num = fc.sar_den = 1
        y = np.full((h, w), 400, "<u2")
        c = np.full((h, w // 2), 400, "<u2")
        a = np.full((h, w), 200, "<u2")
        pkt, _st = codec.encode_frame(
            fc, [y.tobytes(), c.tobytes(), c.tobytes(), a.tobytes()])

        mux = ToposMuxFile(codec, str(tmp_path / "sat.mov"), movie_cfg)
        mux.add_packet(pkt, pts=0, dur=1)
        mux.set_alpha_budget({
            "target_ratio_bp": 2500,
            "actual_ratio_bp": 65535,
            "max_abs_error": 3,
            "flags": {"overrun": False, "authorized": False,
                      "adapted": True, "ratio_saturated": True},
            "frame_count": 1,
        })
        mux.finish()
        mux.close()

        mv = ToposMovieFile(codec, str(tmp_path / "sat.mov"))
        try:
            info = mv.alpha_budget()
        finally:
            mv.close()
        assert info is not None
        assert info["flags"]["ratio_saturated"] is True, info
        assert info["actual_ratio_bp"] == 65535


class TestO2MultigenMatrix:
    """P1-10：YUV444/GBR 12-bit × profile 5/6 多代矩阵（native CLI 只覆盖
    422/10-bit Standard；此处经 binding 补全 DoD 缺口）。"""

    @staticmethod
    def _psnr(a, b) -> float:
        d = a.astype(np.float64) - b.astype(np.float64)
        mse = float(np.mean(d * d))
        if mse <= 1e-12:
            return 99.0
        return float(10.0 * np.log10((1023.0 * 1023.0) / mse))

    @pytest.mark.parametrize('pf,bd,profile', [
        (1, 12, 5),   # YUV444 12-bit Pro444
        (2, 12, 5),   # GBR 12-bit Pro444
        (1, 12, 6),   # YUV444 12-bit Extreme
        (2, 12, 6),   # GBR 12-bit Extreme
        (1, 10, 5),   # YUV444 10-bit Pro444
    ])
    def test_multigen_444_gbr_profile56(self, require_lib, pf, bd, profile) -> None:
        import ctypes

        from src.shared.codec import topos_binding
        from src.shared.codec.topos_binding import (
            TOPOS_CODEC_ABI_VERSION, ToposCodec,
        )

        codec = ToposCodec()
        w, h = 96, 64
        rng = np.random.default_rng(0xBEEF + pf * 7 + bd)

        def make_frame(seed_shift: int = 0):
            y = (rng.random((h, w)) * 1023).astype("<u2")
            c1 = (rng.random((h, w)) * 1023).astype("<u2")
            c2 = (rng.random((h, w)) * 1023).astype("<u2")
            return [y.tobytes(), c1.tobytes(), c2.tobytes()]

        def cfg_for():
            fc = topos_binding._CFrameConfig()
            fc.struct_size = ctypes.sizeof(topos_binding._CFrameConfig)
            fc.abi_version = TOPOS_CODEC_ABI_VERSION
            fc.visible_width, fc.visible_height = w, h
            fc.qp_base = 24
            fc.qmatrix_id = 1
            fc.profile = profile
            fc.pixel_format = pf
            fc.bit_depth = bd
            fc.color_range = fc.color_primaries = fc.color_transfer = 1
            fc.color_matrix = 0 if pf == 2 else 1
            fc.sar_num = fc.sar_den = 1
            return fc

        gens = [make_frame()]
        for g in range(1, 5):
            pkt, _st = codec.encode_frame(cfg_for(), gens[g - 1])
            frame = codec.decode(pkt)
            gens.append([bytes(pl) for pl in frame.planes])
            del pkt

        for g in (1, 3, 4):
            a = np.frombuffer(gens[0][0], "<u2").reshape(h, w)
            b = np.frombuffer(gens[g][0], "<u2").reshape(h, w)
            psnr = self._psnr(a, b)
            assert psnr > 26.0, (pf, bd, profile, g, psnr)
        # 退化平缓：gen4 与 gen1 差距有限（逐代重编码无崩溃/发散）
        p1 = self._psnr(np.frombuffer(gens[0][0], "<u2").reshape(h, w),
                        np.frombuffer(gens[1][0], "<u2").reshape(h, w))
        p4 = self._psnr(np.frombuffer(gens[0][0], "<u2").reshape(h, w),
                        np.frombuffer(gens[4][0], "<u2").reshape(h, w))
        assert p1 - p4 < 6.0, (pf, bd, profile, p1, p4)


# =============================================================================
# ToposVideoEncoder GOP 分段并行（ADR-C050）
# =============================================================================

W16, H16, N16 = 64, 40, 16


class TestToposGopSegmentParallel:
    """gop='ip2' 分段并行：字节确定性（worker 数不改变产物）+ 段首 I 序列。

    每段独立 GOP context、段首强制 I；mux/pts/预算记账在主线程有序回填——
    workers=1 与 workers=4 必须产出逐字节相同的 MOV（含容器元数据）。
    """

    @staticmethod
    def _ip2_frame(i: int):
        from src.shared.media.frame import DecodedFrame

        bgr = np.zeros((H, W, 3), np.uint16)
        # 帧间微小变化（±~100 级）→ qp44 下 P 必小于 I（不触发回退）
        bgr[..., 2] = 8000 + ((i * 37) % 97)
        bgr[..., 1] = 4000 + ((i * 53) % 89)
        bgr[..., 0] = 2000 + ((i * 71) % 83)
        return DecodedFrame(frame_index=i, pts=i, time_seconds=i / 24,
                            data=bgr, pixel_format='bgr48le', width=W, height=H)

    def _encode(self, path: str, workers: int, gop_size=4) -> None:
        enc = ToposVideoEncoder(path, _enc_config(
            crf=44, gop='ip2', gop_size=gop_size, gop_workers=workers))
        enc.open()
        for i in range(N16):
            enc.encode_frame(self._ip2_frame(i))
        enc.close()

    def test_workers_byte_invariance(self, require_lib, tmp_path) -> None:
        out1 = str(tmp_path / "w1.mov")
        out4 = str(tmp_path / "w4.mov")
        self._encode(out1, workers=1)
        self._encode(out4, workers=4)
        with open(out1, 'rb') as f1, open(out4, 'rb') as f4:
            assert f1.read() == f4.read()

    def test_segment_starts_force_intra(self, require_lib, tmp_path) -> None:
        out = str(tmp_path / "seg.mov")
        self._encode(out, workers=3)
        from src.shared.codec.topos_binding import ToposCodec, ToposMovieFile
        codec = ToposCodec()
        movie = ToposMovieFile(codec, out)
        try:
            summary = movie.info()
            assert summary.sample_count == N16
            types = []
            for k in range(summary.sample_count):
                pkt = movie.packet(k)
                types.append(pkt[15])
            # 段长 4：帧 0/4/8/12 为段首 I，其余 P
            assert types == [0, 1, 1, 1] * 4
        finally:
            movie.close()

    def test_gop_size_zero_keeps_single_gop(self, require_lib, tmp_path) -> None:
        out = str(tmp_path / "single.mov")
        enc = ToposVideoEncoder(out, _enc_config(
            crf=44, gop='ip2', gop_size=0, gop_workers=2))
        assert enc._gop_seg is False
        enc.open()
        for i in range(6):
            enc.encode_frame(self._ip2_frame(i))
        enc.close()
        from src.shared.codec.topos_binding import ToposCodec, ToposMovieFile
        movie = ToposMovieFile(ToposCodec(), out)
        try:
            assert movie.info().sample_count == 6
            types = [movie.packet(k)[15] for k in range(6)]
            # 单 GOP：仅首帧 I（现行为，逐字节不变）
            assert types[0] == 0 and all(t == 1 for t in types[1:])
        finally:
            movie.close()

    def test_default_policy_2k_on_4k_off(self, require_lib) -> None:
        from src.shared.export.video_encoder import FFmpegEncoderConfig
        from src.shared.media.metadata import Rational

        # ≤2K：默认分段（fps×2 帧）
        enc = ToposVideoEncoder("/tmp/_unused.mov", FFmpegEncoderConfig(
            codec='topos', width=1920, height=1080, fps=Rational(24, 1),
            pix_fmt='yuv422p10le', gop='ip2'))
        assert enc._gop_len == 48 and enc._gop_seg is True
        # 4K：默认单 GOP（band 机器已近饱和，分段默认关）
        enc4k = ToposVideoEncoder("/tmp/_unused.mov", FFmpegEncoderConfig(
            codec='topos', width=3840, height=2160, fps=Rational(24, 1),
            pix_fmt='yuv422p10le', gop='ip2'))
        assert enc4k._gop_len == 0 and enc4k._gop_seg is False
        # 4K 显式 gop_size：尊重显式设置
        enc4kx = ToposVideoEncoder("/tmp/_unused.mov", FFmpegEncoderConfig(
            codec='topos', width=3840, height=2160, fps=Rational(24, 1),
            pix_fmt='yuv422p10le', gop='ip2', gop_size=16))
        assert enc4kx._gop_len == 16 and enc4kx._gop_seg is True

    def test_invalid_params_rejected(self, require_lib, tmp_path) -> None:
        with pytest.raises(ValueError, match='gop_size'):
            ToposVideoEncoder(str(tmp_path / "bad.mov"), _enc_config(
                crf=8, gop='ip2', gop_size=-1))
        with pytest.raises(ValueError, match='gop_workers'):
            ToposVideoEncoder(str(tmp_path / "bad.mov"), _enc_config(
                crf=8, gop='ip2', gop_size=4, gop_workers=0))


class TestToposLpTier:
    """v1.8（ADR-C056）：Topos 422 LP 帧间档产品化。

    覆盖：tier ⟹ gop='ip2' 载体力 + 固定锚 qp（无 qp 覆盖时 72）；
    TimelineExporter 产品路径导出 → tpcD tier=7 → probe 显示
    "Topos 422 LP"；读侧 GOP context 解码；alpha / 图片序列容器显式
    收缩；帧提取 P 帧拒绝（不可独立解码的包不落盘）。
    """

    @staticmethod
    def _lp_frame(i: int):
        from src.shared.media.frame import DecodedFrame

        bgr = np.zeros((H, W, 3), np.uint16)
        bgr[..., 2] = 8000 + ((i * 37) % 97)
        bgr[..., 1] = 4000 + ((i * 53) % 89)
        bgr[..., 0] = 2000 + ((i * 71) % 83)
        return DecodedFrame(frame_index=i, pts=i, time_seconds=i / 24,
                            data=bgr, pixel_format='bgr48le', width=W, height=H)

    def test_tier_forces_ip2_and_anchor_qp(self, require_lib) -> None:
        """lp 档即帧间档：tier 决定载体；无 qp 覆盖时用固定锚 72。"""
        from src.shared.codec.topos_profiles import topos_tier_numeric_id

        enc = ToposVideoEncoder("/tmp/_unused_lp.mov", _enc_config(
            profile='lp', crf=None))
        assert enc._gop == 'ip2'
        assert enc._tier.tier_id == 'lp'
        assert enc._qp_override is None
        assert enc._tier.anchor_qp == 72
        assert topos_tier_numeric_id('lp') == 7
        enc.open()
        try:
            assert enc._cfg_fields['qp'] == 72
        finally:
            enc.abort()
        # 显式 crf 仍拥有最高优先级（质量旋钮 = 锚 qp 覆盖）
        enc2 = ToposVideoEncoder("/tmp/_unused_lp.mov", _enc_config(
            profile='lp', crf=60))
        enc2.open()
        try:
            assert enc2._cfg_fields['qp'] == 60
        finally:
            enc2.abort()

    def test_lp_alpha_rejected(self, require_lib) -> None:
        with pytest.raises(ValueError, match='alpha'):
            ToposVideoEncoder("/tmp/_unused_lp.mov", _enc_config(
                profile='lp', has_alpha=True,
                pix_fmt='topos_yuva422p10a12'))

    def test_lp_image_sequence_container_rejected(self, require_lib) -> None:
        from src.shared.export.topos_encoder import ToposImageSequenceEncoder

        with pytest.raises(ValueError, match='图片序列'):
            ToposImageSequenceEncoder(
                "/tmp/_unused_lp_seq", _enc_config(profile='lp', crf=None))

    def test_lp_app_roundtrip_probe_and_decode(self, require_lib, tmp_path) -> None:
        """产品路径：TimelineExporter(TOPOS_LP) → 容器 tpcD tier=7 →
        probe 显示 'Topos 422 LP' → 读侧 GOP context 逐帧解码。"""
        from src.features.edit.core.timeline_export import (
            ExportCodec,
            ExportRangeType,
            ExportSource,
            TimelineExportConfig,
            TimelineExporter,
        )
        from src.features.edit.core.project import EditProject
        from src.features.edit.core.timeline_v2 import (
            ClipInstance,
            EditTrack,
            TimelineSequence,
        )
        from src.shared.media.factory import MediaSourceFactory
        from src.shared.media.metadata import Rational

        w, h, n = 64, 48, 6
        bgra = np.zeros((h, w, 4), np.uint8)
        bgra[..., 0] = 40
        bgra[..., 1] = 80
        bgra[..., 2] = 160
        bgra[..., 3] = 255
        from src.shared.media.frame import DecodedFrame

        frames = [DecodedFrame(
            frame_index=i, pts=i, time_seconds=i / 24.0,
            data=bgra.copy(), pixel_format="bgra",
            width=w, height=h) for i in range(n)]

        project = EditProject.create("LP")
        spath = str(tmp_path / "src.mov")
        with open(spath, "wb") as fh:
            fh.write(b"placeholder")
        project = project.import_asset(
            path=spath, media_type="video",
            metadata={"width": w, "height": h, "fps": 24.0,
                      "total_frames": n, "duration_seconds": n / 24.0})
        asset_id = list(project.assets.keys())[-1]
        clip = ClipInstance(
            clip_id="c0", asset_id=asset_id, track_id="t0",
            timeline_start=0, timeline_duration=n,
            source_start=0, source_duration=n)
        seq = TimelineSequence(
            name="LP", width=w, height=h, fps=Rational(24, 1),
            video_tracks=(EditTrack(
                track_id="t0", name="V1", track_type="video",
                index=0, clips=(clip,)),))

        class _P:
            def open_clip(self, path, **_kw):
                return str(path)

            def request_frame(self, _cid, frame, quality="master"):
                return frames[int(frame) % n]

            def close_clip(self, *_a):
                pass

        out = str(tmp_path / "lp.mov")
        exporter = TimelineExporter(
            project=project, sequence=seq, frame_provider=_P())
        cfg = TimelineExportConfig(
            source=ExportSource.TIMELINE, timeline_id=seq.timeline_id,
            range_type=ExportRangeType.ENTIRE, codec=ExportCodec.TOPOS_LP,
            output_path=out, has_alpha=False, include_audio=False,
            resolution=(w, h))
        assert exporter.export_timeline(cfg).success

        src = MediaSourceFactory.create(out)
        src.open()
        try:
            md = src.probe()
            # tpcD tier=7 → 探测层恢复精确档位名（非粗粒度 'Topos 422'）
            assert md.codec_profile == "Topos 422 LP", md.codec_profile
            assert int(md.codec_profile and 1)
            assert str(md.pixel_format) == "yuv422p10le"
            # 读侧 GOP context：I/P 顺序解码全帧
            for i in range(n):
                df = src.read_frame(i)
                assert df.width == w and df.height == h
        finally:
            src.close()

        # 容器级 tier id 读回（tpcD reserved[1] = 7）
        from src.shared.codec.topos_binding import (
            TC_TIER_LP,
            ToposCodec,
            ToposMovieFile,
        )
        movie = ToposMovieFile(ToposCodec(), out)
        try:
            assert int(movie.info().tier_id) == TC_TIER_LP
        finally:
            movie.close()

    def test_lp_frame_extract_i_ok_p_rejected(self, require_lib, tmp_path) -> None:
        """LP 源帧提取：I 帧封装成功（TMET 转发 tier=7）；P 帧显式拒绝
        且不落盘（静默产出不可解码 .toos = 数据丢失）。"""
        from src.shared.codec.topos_binding import (
            ToposCodec,
            ToposMovieFile,
        )
        from src.shared.codec.topos_image_binding import get_image_codec
        from src.shared.media.topos_frame_extract import ToposFrameExtractor

        enc = ToposVideoEncoder(str(tmp_path / "lp.mov"), _enc_config(
            profile='lp', crf=44, gop_size=4, gop_workers=2))
        enc.open()
        for i in range(6):
            enc.encode_frame(self._lp_frame(i))
        enc.close()

        movie = ToposMovieFile(ToposCodec(), str(tmp_path / "lp.mov"))
        try:
            types = [movie.packet(k)[15] for k in range(6)]
            # 段长 4：帧 0/4 为段首 I，其余 P
            assert types == [0, 1, 1, 1, 0, 1]
        finally:
            movie.close()

        with ToposFrameExtractor(str(tmp_path / "lp.mov")) as ex:
            # I 帧（段首）→ 封装成功；TMET 携带源档位
            res = ex.extract(0, str(tmp_path / "i0.toos"))
            assert res.image_profile == 0
            meta = get_image_codec().read_export_meta(str(tmp_path / "i0.toos"))
            assert meta is not None and meta["tier_id"] == 7
            assert meta["label"] == "Topos 422 LP"
            # P 帧 → 显式拒绝，无输出文件
            with pytest.raises(Exception, match="帧间"):
                ex.extract(1, str(tmp_path / "p1.toos"))
            assert not (tmp_path / "p1.toos").exists()


# ---------------------------------------------------------------------------
# M4-R7：视频 RAW 档（TRAW CFA）编码器域规则 + 编码冒烟
# ---------------------------------------------------------------------------


class TestRawVideoTierDomain:
    """raw 档构造期 fail-closed：载体/Alpha/qp/格式交叉逐项验证。"""

    def _raw_cfg(self, **ov):
        return _enc_config(pix_fmt='topos_cfa12le', profile='raw', crf=59,
                           **ov)

    def test_raw_gop_forced_intra(self, tmp_path) -> None:
        """raw 无帧间形态——显式 gop='ip2' 与 raw 组合同样落 intra（与 lp
        对偶的"档位决定载体"语义，非静默降级）。"""
        enc = ToposVideoEncoder(str(tmp_path / 'raw.mov'),
                                self._raw_cfg(gop='ip2'))
        assert enc._gop == 'intra'

    def test_raw_requires_ratio_anchor(self, tmp_path) -> None:
        """raw qp 由比率锚表驱动——无 crf（qp 覆盖）构造期拒绝。"""
        with pytest.raises(ValueError, match='比率'):
            ToposVideoEncoder(
                str(tmp_path / 'raw.mov'),
                _enc_config(pix_fmt='topos_cfa12le', profile='raw', crf=None))

    def test_raw_rejects_alpha(self, tmp_path) -> None:
        with pytest.raises(ValueError, match='alpha'):
            ToposVideoEncoder(str(tmp_path / 'raw.mov'),
                              self._raw_cfg(has_alpha=True))

    def test_raw_rejects_aq(self, tmp_path) -> None:
        with pytest.raises(ValueError, match='aq_mode'):
            ToposVideoEncoder(str(tmp_path / 'raw.mov'),
                              self._raw_cfg(aq_mode='on'))

    def test_raw_rejects_rdo(self, tmp_path) -> None:
        with pytest.raises(ValueError, match='rdo_mode'):
            ToposVideoEncoder(str(tmp_path / 'raw.mov'),
                              self._raw_cfg(rdo_mode='on'))

    def test_raw_rejects_v2_entropy(self, tmp_path) -> None:
        with pytest.raises(ValueError, match='entropy_mode'):
            ToposVideoEncoder(str(tmp_path / 'raw.mov'),
                              self._raw_cfg(entropy_mode='v2'))

    def test_raw_rejects_non_cfa_pix_fmt(self, tmp_path) -> None:
        """profile 7 交叉规则：pf=3 之外一律拒绝（拒换头）。"""
        with pytest.raises(ValueError, match='CFA'):
            ToposVideoEncoder(
                str(tmp_path / 'raw.mov'),
                _enc_config(pix_fmt='yuv422p10le', profile='raw', crf=59))

    def test_raw_bd16_requires_rans2(self, tmp_path) -> None:
        """bd≥13 须宽域熵（native 交叉规则前置）。"""
        with pytest.raises(ValueError, match='宽域熵'):
            ToposVideoEncoder(
                str(tmp_path / 'raw.mov'),
                _enc_config(pix_fmt='topos_cfa16le', profile='raw', crf=72,
                            entropy_mode='v1'))

    def test_raw_bd16_accepts_rans2(self, tmp_path) -> None:
        enc = ToposVideoEncoder(str(tmp_path / 'raw16.mov'),
                                _enc_config(pix_fmt='topos_cfa16le',
                                            profile='raw', crf=72))
        assert enc._stream_bit_depth == 16 and enc._native_profile == 7


class TestRawVideoExport:
    """M4-R7：TRAW MOV 编码 → native 解码 roundtrip + 容器元数据。"""

    def _cfa_planes(self, seed: int, w: int, h: int, bd: int = 12):
        rng = np.random.default_rng(seed)
        pw, ph = (w + 1) // 2, (h + 1) // 2
        full = (1 << bd) - 1
        base = rng.integers(0, 1 << (bd - 2), size=(ph, pw), dtype=np.uint16)
        # 相位平面间给出可分辨的直流偏移（full/8 阶梯），平面身份可对拍
        return [np.clip(base.astype(np.int32) + full * k // 8, 0, full)
                .astype('<u2') for k in (0, 1, 2, 6)]

    def test_traw_mov_roundtrip(self, require_lib, tmp_path) -> None:
        from src.shared.codec.topos_binding import ToposCodec, ToposMovieFile
        from src.shared.media.frame import DecodedFrame
        from src.shared.media.topos_source import ToposMediaSource

        w, h, frames, bd = 64, 40, 4, 12
        src_planes = [self._cfa_planes(i, w, h, bd) for i in range(frames)]
        path = tmp_path / 'traw.mov'
        enc = ToposVideoEncoder(str(path), _enc_config(
            pix_fmt='topos_cfa12le', profile='raw', crf=59))
        enc.open()
        for i in range(frames):
            enc.encode_frame(DecodedFrame(
                frame_index=i, pts=i, time_seconds=i / 24.0, data=None,
                planes=tuple(src_planes[i]), pixel_format='topos_cfa12le',
                dtype=np.dtype(np.uint16), width=w, height=h))
        enc.close()

        # 容器：tpcD tier 8 + label 'Topos RAW'；moov 摘要 pf=3
        movie = ToposMovieFile(ToposCodec(), str(path))
        try:
            info = movie.info()
            assert info.profile == 7 and info.pixel_format == 3
            assert info.tier_id == 8
        finally:
            movie.close()

        # 读侧（R9）：4 相位平面半几何 + 像素统计对拍
        src = ToposMediaSource(str(path))
        try:
            src.open()
            meta = src.probe()
            assert meta.codec_name == 'topos'
            assert meta.pixel_format == 'topos_cfa12le'
            assert meta.codec_profile == 'Topos RAW'
            assert meta.total_frames == frames
            for i in range(frames):
                frame = src.read_frame(i)
                assert frame.pixel_format == 'topos_cfa12le'
                assert len(frame.planes) == 4
                pw, ph = (w + 1) // 2, (h + 1) // 2
                assert frame.planes[0].shape == (ph, pw)
                for p in range(4):
                    got = float(np.asarray(frame.planes[p], dtype=np.float64).mean())
                    want = float(np.asarray(src_planes[i][p], dtype=np.float64).mean())
                    # qp59 有损：平面均值漂移须有界（平滑合成素材 ≪ 满刻度 1%）
                    assert abs(got - want) < 0.01 * (1 << bd), (i, p, got, want)
        finally:
            src.close()


class TestResolveExportColorMetadata:
    """2026-09-21 复查：G1 导出色彩标签——image 资产纳入 + 'gbr' 折算。"""

    def _exporter(self, asset):
        from types import SimpleNamespace

        from src.features.edit.core.timeline_export import TimelineExporter

        clip = SimpleNamespace(asset_id="a1")
        track = SimpleNamespace(track_type="video", clips=[clip])
        exp = object.__new__(TimelineExporter)
        exp._sequence = SimpleNamespace(all_tracks=[track])
        exp._project = SimpleNamespace(assets={"a1": asset})
        return exp

    def _asset(self, media_type, **color):
        from types import SimpleNamespace

        meta = {k: v for k, v in color.items() if v}
        return SimpleNamespace(is_video=(media_type == "video"),
                               metadata=meta)

    def test_image_asset_color_tags_consumed(self) -> None:
        """G1：.toos 静图/序列（media_type='image'）此前被 is_video 过滤
        跳过——现在 linear 标签透传到导出配置而非静默落 SDR。"""
        exp = self._exporter(self._asset(
            "image", color_primaries="bt709", color_transfer="linear",
            color_range="full"))
        cm = exp._resolve_export_color_metadata()
        assert cm.transfer == "linear"
        assert cm.range == "full"

    def test_gbr_matrix_coerced_to_bt709(self) -> None:
        """GBR（H.273 matrix 0 = identity）标签此前直传 ColorMetadataConfig
        → 编码器矩阵码表 ValueError；现折算 bt709（G4 同口径）。"""
        exp = self._exporter(self._asset(
            "video", color_primaries="bt709", color_transfer="bt709",
            color_matrix="gbr"))
        cm = exp._resolve_export_color_metadata()
        assert cm.matrix == "bt709"

    def test_raw_transfer_still_falls_back_sdr(self) -> None:
        """RC6 RAW 域 transfer 不透传（回归钉扎，行为不变）。"""
        exp = self._exporter(self._asset(
            "video", color_primaries="bt709", color_transfer="traw-log0"))
        cm = exp._resolve_export_color_metadata()
        from src.shared.export.video_encoder import ColorMetadataConfig
        assert cm.transfer == ColorMetadataConfig.sdr().transfer


# ---------------- C09（2026-09-27）：open 失败统一清理 ----------------

class TestOpenFailureCleanup:
    """C09：_create_mux 任一阶段失败 → mux/GOP/temp 全清零，原异常保留；
    monkeypatch 解除后同对象可重新 open。"""

    def _make(self, tmp_path, **cfg_overrides):
        from src.shared.export.topos_encoder import ToposVideoEncoder
        return ToposVideoEncoder(
            str(tmp_path / "out.mov"), _enc_config(**cfg_overrides))

    def _assert_clean(self, enc, tmp_path):
        assert enc._mux is None
        assert getattr(enc, '_gop_ctx', None) is None
        assert enc._is_open is False
        assert not os.path.exists(enc._temp_path)

    def test_mux_construction_failure_cleans_up(self, tmp_path,
                                                monkeypatch) -> None:
        from src.shared.codec import topos_binding
        enc = self._make(tmp_path)

        def boom(self_, *a, **kw):
            raise RuntimeError("injected mux failure")
        monkeypatch.setattr(topos_binding.ToposMuxFile, "__init__", boom)
        with pytest.raises(RuntimeError, match="injected mux failure"):
            enc.open()
        self._assert_clean(enc, tmp_path)

        # 解除注入后同对象行为明确：可重新 open
        monkeypatch.undo()
        enc.open()
        try:
            assert enc._is_open is True
        finally:
            enc.abort()

    def test_movie_meta_failure_cleans_up(self, tmp_path, monkeypatch) -> None:
        from src.shared.export.topos_encoder import ToposVideoEncoder
        enc = self._make(tmp_path)

        def boom(self_):
            raise RuntimeError("injected meta failure")
        monkeypatch.setattr(ToposVideoEncoder, "_apply_movie_meta", boom)
        with pytest.raises(RuntimeError, match="injected meta failure"):
            enc.open()
        self._assert_clean(enc, tmp_path)

    def test_timecode_failure_cleans_up(self, tmp_path, monkeypatch) -> None:
        from src.shared.export.topos_encoder import ToposVideoEncoder
        enc = self._make(tmp_path)

        def boom(self_):
            raise RuntimeError("injected timecode failure")
        monkeypatch.setattr(ToposVideoEncoder, "_apply_start_timecode", boom)
        with pytest.raises(RuntimeError, match="injected timecode failure"):
            enc.open()
        self._assert_clean(enc, tmp_path)

    def test_stems_failure_cleans_up(self, tmp_path, monkeypatch) -> None:
        from src.shared.export.topos_encoder import ToposVideoEncoder
        enc = self._make(tmp_path)

        def boom(self_):
            raise RuntimeError("injected stems failure")
        monkeypatch.setattr(ToposVideoEncoder, "_apply_stem_tracks", boom)
        with pytest.raises(RuntimeError, match="injected stems failure"):
            enc.open()
        self._assert_clean(enc, tmp_path)

    def test_gop_ctx_released_on_failure(self, tmp_path, monkeypatch) -> None:
        """ip2（GOP context）模式下声明阶段失败 → context 已释放。"""
        from src.shared.export.topos_encoder import ToposVideoEncoder
        enc = self._make(tmp_path, gop='ip2')

        def boom(self_):
            raise RuntimeError("injected meta failure")
        monkeypatch.setattr(ToposVideoEncoder, "_apply_movie_meta", boom)
        with pytest.raises(RuntimeError, match="injected meta failure"):
            enc.open()
        self._assert_clean(enc, tmp_path)


# ---------------- C12/C13（2026-09-27）：生命周期收口 ----------------

def _frame_2(i: int):
    import numpy as np
    from src.shared.media.frame import DecodedFrame
    W_, H_ = 64, 48
    data = np.zeros((H_, W_, 3), dtype=np.uint8)
    data[..., 0] = (i * 17 + 12) & 0xFF
    return DecodedFrame(frame_index=i, pts=i, time_seconds=i / 24.0,
                        data=data, pixel_format="bgr24", width=W_, height=H_)


class TestAudioRedeclarationLifecycle:
    """C12：首帧后音频重声明必须拒绝且不得破坏既有声明。"""

    def test_redeclare_after_first_frame_rejected_state_intact(
            self, tmp_path) -> None:
        from src.shared.export.topos_encoder import ToposVideoEncoder
        enc = ToposVideoEncoder(str(tmp_path / "out.mov"), _enc_config())
        enc.open()
        enc.add_audio_stream("pcm_s24le", 48000, 2)
        enc.encode_frame(_frame_2(0))
        old_decl = dict(enc._audio_declaration)

        with pytest.raises(RuntimeError, match="首帧编码前"):
            enc.add_audio_stream("pcm_f32le", 48000, 2)
        assert enc._audio_declaration == old_decl  # 未被覆盖

        # 旧音频声明仍可正确写/定稿
        samples = np.zeros((2, 4800), dtype=np.int32)
        enc.write_audio_samples(samples, 48000)
        enc.close()
        assert os.path.isfile(str(tmp_path / "out.mov"))

    def test_float_to_s16_redeclare_at_zero_frames(self, tmp_path) -> None:
        """0 帧重声明 = mux 重建，后声明生效（float→s16 方向）。"""
        import numpy as np
        from src.shared.export.topos_encoder import ToposVideoEncoder
        enc = ToposVideoEncoder(str(tmp_path / "out.mov"), _enc_config())
        enc.open()
        enc.add_audio_stream("pcm_f32le", 48000, 2)
        enc.add_audio_stream("pcm_s16le", 48000, 2)
        assert enc._audio_declaration['bits'] == 16
        enc.encode_frame(_frame_2(0))
        enc.write_audio_samples(
            np.zeros((2, 4800), dtype=np.int32), 48000)
        enc.close()
        assert os.path.isfile(str(tmp_path / "out.mov"))


class TestCloseGopCleanup:
    """C13：close() 所有退出路径都释放 GOP context。"""

    def test_finish_failure_releases_gop_ctx(self, tmp_path,
                                             monkeypatch) -> None:
        from src.shared.export.topos_encoder import ToposVideoEncoder
        enc = ToposVideoEncoder(str(tmp_path / "out.mov"), _enc_config(gop='ip2', gop_size=0))
        enc.open()
        enc.encode_frame(_frame_2(0))
        assert getattr(enc, '_gop_ctx', None) is not None

        def boom():
            raise RuntimeError("injected finish failure")
        monkeypatch.setattr(type(enc._mux), "finish", boom)
        with pytest.raises(RuntimeError, match="定稿失败"):
            enc.close()
        assert getattr(enc, '_gop_ctx', None) is None  # C13：已释放

    def test_success_path_releases_gop_ctx(self, tmp_path) -> None:
        from src.shared.export.topos_encoder import ToposVideoEncoder
        enc = ToposVideoEncoder(str(tmp_path / "out.mov"), _enc_config())
        enc.open()
        enc.encode_frame(_frame_2(0))
        enc.close()
        assert getattr(enc, '_gop_ctx', None) is None
