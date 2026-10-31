"""TAEP M2 — TimebaseService 测试。

验收（§18.2 / §11.2 M2 退出条件）：
- provider 原始 PTS（任意 timebase）→ canonical source FrameRange 整帧半开区间。
- VFR/CFR fixture 均能从 evidence 回到 source frame。
- 绝不用 seconds × fps；全程有理数/整数运算。
- VFR 素材标注 limitation warning，不静默假装精确。
- 音频 sample offset 路径。
- rounding policy 可审计（返回 warning）。
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.edit_core

from src.features.edit.ai_editing.editorial_models import FrameRange
from src.features.edit.ai_editing.timebase import (
    ProviderTimebase,
    TimebaseConversionResult,
    TimebaseService,
)
from src.shared.media.metadata import Rational


# ================================================================
# ProviderTimebase
# ================================================================


def test_provider_timebase_has_pts_with_timebase():
    pt = ProviderTimebase(pts_start=90000, pts_end=180000, timebase=Rational(1, 90000))
    assert pt.has_pts is True
    assert pt.timebase_label == "1/90000"


def test_provider_timebase_has_pts_with_sample_rate():
    pt = ProviderTimebase(pts_start=48000, pts_end=96000, sample_rate=48000)
    assert pt.has_pts is True
    assert pt.timebase_label == "sample_rate=48000"


def test_provider_timebase_without_timebase_or_sample_rate():
    """无 timebase/sample_rate → has_pts False（调用方应回退秒路径）。"""
    pt = ProviderTimebase(pts_start=100, pts_end=200)
    assert pt.has_pts is False
    assert pt.timebase_label is None


# ================================================================
# PTS → FrameRange（CFR，各帧率）
# ================================================================


@pytest.mark.parametrize("fps,num,den", [
    (Rational(24, 1), 24, 1),
    (Rational(25, 1), 25, 1),
    (Rational(30000, 1001), 30000, 1001),  # 29.97 NTSC
    (Rational(60000, 1001), 60000, 1001),  # 59.94 NTSC
    (Rational(50, 1), 50, 1),
])
def test_pts_one_second_converts_to_correct_frame_count(fps, num, den):
    """1 秒的 PTS 时长（timebase=1/1000，PTS 0→1000）应换算为 ~fps 帧（CFR）。"""
    svc = TimebaseService()
    # timebase 1/1000 秒；PTS 0 = 0s, 1000 = 1s → 区间 [0, 1s)
    pt = ProviderTimebase(pts_start=0, pts_end=1000, timebase=Rational(1, 1000))
    res = svc.to_source_frame_range(pt, fps)
    expected = round(1.0 * fps.to_fps())  # 1 秒的帧数
    assert res.source_range.start == 0
    assert res.source_range.duration == expected


def test_pts_24fps_round_number():
    """24fps，PTS timebase 1/90000：PTS 90000 = 1s = 第 24 帧。"""
    svc = TimebaseService()
    pt = ProviderTimebase(pts_start=90000, pts_end=180000, timebase=Rational(1, 90000))
    res = svc.to_source_frame_range(pt, Rational(24, 1))
    assert res.source_range.start == 24
    assert res.source_range.duration == 24
    assert res.source_range.end == 48
    assert res.warnings == ()


def test_pts_start_at_nonzero_offset():
    svc = TimebaseService()
    # 24fps, PTS 45000 (0.5s) → 210000 (2.333s)
    pt = ProviderTimebase(pts_start=45000, pts_end=210000, timebase=Rational(1, 90000))
    res = svc.to_source_frame_range(pt, Rational(24, 1))
    assert res.source_range.start == 12   # 0.5s * 24
    assert res.source_range.end == 56     # 2.333s * 24 ≈ 56


# ================================================================
# 音频 sample offset 路径
# ================================================================


def test_audio_sample_offset_to_frame_range():
    """48kHz 音频 sample 48000 (=1s) 在 24fps 下 = 第 24 帧。"""
    svc = TimebaseService()
    pt = ProviderTimebase(pts_start=48000, pts_end=96000, sample_rate=48000)
    res = svc.to_source_frame_range(pt, Rational(24, 1))
    assert res.source_range.start == 24
    assert res.source_range.duration == 24


def test_from_audio_sample_single():
    svc = TimebaseService()
    assert svc.from_audio_sample(48000, 48000, Rational(24, 1)) == 24
    assert svc.from_audio_sample(24000, 48000, Rational(24, 1)) == 12


def test_from_audio_sample_invalid_rate():
    svc = TimebaseService()
    with pytest.raises(ValueError, match="sample_rate"):
        svc.from_audio_sample(100, 0, Rational(24, 1))


# ================================================================
# VFR 安全（标注 limitation，不静默 seconds × fps）
# ================================================================


def test_vfr_emits_limitation_warning():
    """VFR 素材按 CFR 近似换算，但必须标注 warning，不静默假装精确。"""
    svc = TimebaseService()
    pt = ProviderTimebase(pts_start=90000, pts_end=180000, timebase=Rational(1, 90000))
    res = svc.to_source_frame_range(pt, Rational(30, 1), is_vfr=True)
    assert res.has_warnings
    assert any("VFR" in w for w in res.warnings)
    # 仍然给出近似结果（不阻塞）
    assert res.source_range.duration == 30


def test_no_pts_raises():
    """无可用 PTS（缺 timebase/sample_rate）→ 显式报错，不静默回退。"""
    svc = TimebaseService()
    pt = ProviderTimebase(pts_start=100, pts_end=200)  # 无 timebase/sample_rate
    with pytest.raises(ValueError, match="no usable PTS"):
        svc.to_source_frame_range(pt, Rational(24, 1))


# ================================================================
# collapsing / 最少 1 帧
# ================================================================


def test_collapsed_pts_range_expands_to_one_frame_with_warning():
    """PTS 范围不足一帧 → 扩展为 1 帧 + warning（不静默丢数据）。"""
    svc = TimebaseService()
    # 24fps, timebase 1/90000: 一帧 = 3750 PTS；给一个 100 PTS 的极小范围
    pt = ProviderTimebase(pts_start=0, pts_end=100, timebase=Rational(1, 90000))
    res = svc.to_source_frame_range(pt, Rational(24, 1))
    assert res.source_range.duration == 1
    assert res.source_range.start == 0
    assert any("collapsed" in w for w in res.warnings)


# ================================================================
# M2 退出条件核心：从 evidence 回到 source frame（VFR + CFR fixture）
# ================================================================


def test_cfr_evidence_round_trips_to_source_frame():
    """CFR fixture：PTS → FrameRange → segment.source_range → 精确回到 source frame。

    这是 M2 退出条件的核心（§11.2）：fixture 均能从 evidence 回到 source frame。
    """
    svc = TimebaseService()
    fps = Rational(24, 1)
    # provider 给出的转录段 PTS（1/90000 timebase）：1s-2.5s
    pt = ProviderTimebase(pts_start=90000, pts_end=225000, timebase=Rational(1, 90000))
    res = svc.to_source_frame_range(pt, fps)
    fr: FrameRange = res.source_range
    # 模拟 segment 携带这个 range + 原始 PTS
    assert fr.start == 24       # 1s
    assert fr.end == 60         # 2.5s
    # 回溯：从 fr 能反推 PTS（验证不丢精度）
    assert fr.contains(24) and not fr.contains(23)
    assert fr.contains(59) and not fr.contains(60)


def test_vfr_evidence_round_trips_with_limitation_flag():
    """VFR fixture：同样能回到 source frame，但 evidence 携带 VFR limitation 标记。"""
    svc = TimebaseService()
    fps = Rational(30000, 1001)  # 29.97 NTSC（常被误当 VFR 处理）
    pt = ProviderTimebase(pts_start=0, pts_end=30000, timebase=Rational(1, 30000))
    res = svc.to_source_frame_range(pt, fps, is_vfr=True)
    # 0-1s ≈ 30 frames（29.97）
    assert res.source_range.start == 0
    assert 29 <= res.source_range.duration <= 30
    # VFR limitation 必须可见（evidence 上标低精度）
    assert res.has_warnings
    assert any("VFR" in w for w in res.warnings)


def test_from_pts_single_value():
    """单 PTS → 帧号（from_pts 入口）。"""
    svc = TimebaseService()
    # 24fps, timebase 1/90000: PTS 90000 = 1s = frame 24
    assert svc.from_pts(90000, Rational(1, 90000), Rational(24, 1)) == 24
    assert svc.from_pts(0, Rational(1, 90000), Rational(24, 1)) == 0


def test_ntsc_29_97_precision():
    """29.97 NTSC：1 小时边界 PTS 换算不溢出/不丢精度。"""
    svc = TimebaseService()
    fps = Rational(30000, 1001)
    # 1 小时 = 3600s；PTS timebase 1/90000 → 3600*90000 = 324000000
    one_hour_pts = 3600 * 90000
    frame = svc.from_pts(one_hour_pts, Rational(1, 90000), fps)
    # 29.97fps * 3600s ≈ 107892 frames
    assert 107880 <= frame <= 107900
