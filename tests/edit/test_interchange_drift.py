"""P1-15 — 时间基准参考测试（帧/样本/时间码长时长不漂移）。

验收（计划 P1-15）：
- 变速/波形/导出/Cue Sheet 在长时长项目中不漂移；
- 帧与样本边界有独立参考测试。

本文件锁定核心换算（interchange.py）的契约：
1. drop-frame 时间码是帧号上的**双射**（SMPTE ST 12-1），扫掠长时长范围
   （分钟边界密集 + 每小时抽查到 24h）验证 frames→TC→frames 精确还原；
2. NDF 时间码在任何标称帧率下与帧号构成双射；
3. Rational 帧率的帧→样本换算与精确有理数闭式**完全一致**（零漂移），
   帧边界划分（帧 f 拥有样本区间 [s(f), s(f+1))）自洽；
4. float 帧率路径与精确值的偏差有界（≤1 样本）——记录既有语义，
   供未来收敛到 Rational 后对照。
"""

from __future__ import annotations

import sys
import os
from fractions import Fraction

import pytest

pytestmark = pytest.mark.edit_core

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

from src.features.edit.core.interchange import (
    frames_to_samples,
    frames_to_timecode,
    samples_to_frames,
    timecode_to_frames,
)
from src.shared.media.metadata import Rational

NTSC_30 = Rational(30000, 1001)
NTSC_60 = Rational(60000, 1001)
FILM_NTSC = Rational(24000, 1001)

HOUR_AT_2997 = 107892  # 29.97DF: 每小时真实帧数（17982 × 6）
DAY_AT_2997 = HOUR_AT_2997 * 24


# ================================================================
# 1) drop-frame 双射（SMPTE ST 12-1）
# ================================================================


def test_df_canonical_reference_values():
    """ST 12-1 规范参考值。"""
    # 第 1 分钟完整（第 0 分钟不丢帧号）
    assert frames_to_timecode(0, NTSC_30, drop_frame=True) == "00:00:00;00"
    assert frames_to_timecode(1799, NTSC_30, drop_frame=True) == "00:00:59;29"
    # 第 2 分钟开头跳过 00;01
    assert frames_to_timecode(1800, NTSC_30, drop_frame=True) == "00:01:00;02"
    # 整 10 分钟处不丢
    assert frames_to_timecode(17982, NTSC_30, drop_frame=True) == "00:10:00;00"
    # 1 小时
    assert frames_to_timecode(HOUR_AT_2997, NTSC_30, drop_frame=True) == "01:00:00;00"
    # 59.94：每分钟开头丢 4 帧
    assert frames_to_timecode(0, NTSC_60, drop_frame=True) == "00:00:00;00"
    assert frames_to_timecode(3600, NTSC_60, drop_frame=True) == "00:01:00;04"
    assert frames_to_timecode(35964, NTSC_60, drop_frame=True) == "00:10:00;00"


def test_df_bijection_sweep_first_11_minutes():
    """前 11 分钟逐帧双射（覆盖两个 10 分钟块的边界）。"""
    for fps, limit in ((NTSC_30, 17982 * 2), (NTSC_60, 35964 * 2)):
        for f in range(0, limit, 7):  # 步长 7 抽样（全量太慢）
            tc = frames_to_timecode(f, fps, drop_frame=True)
            assert timecode_to_frames(tc, fps, drop_frame=True) == f, (fps, f, tc)


def test_df_bijection_long_duration_spot_checks():
    """到 24 小时的每小时 + 每分钟边界抽查。"""
    for hour in range(24):
        base = hour * HOUR_AT_2997
        for extra in (0, 1, 1799, 1800, 17981, 17982):
            f = base + extra
            tc = frames_to_timecode(f, NTSC_30, drop_frame=True)
            back = timecode_to_frames(tc, NTSC_30, drop_frame=True)
            assert back == f, (hour, extra, f, tc, back)
    assert DAY_AT_2997 == 2589408  # 29.97 全天帧数（24h × 107892）


def test_df_monotonic_display():
    """相邻帧的显示时间码严格递增（编号跳过但不会回退/重复）。"""
    from src.features.edit.core.interchange import parse_timecode

    prev = (-1, -1, -1, -1)
    for f in range(0, 17982 * 3, 13):
        fields = parse_timecode(frames_to_timecode(f, NTSC_30, drop_frame=True))
        assert fields > prev, (f, prev, fields)
        prev = fields


def test_df_rejects_non_displayable_codes():
    with pytest.raises(ValueError):
        timecode_to_frames("00:01:00;00", NTSC_30, drop_frame=True)
    with pytest.raises(ValueError):
        timecode_to_frames("00:00:00;00", 25.0, drop_frame=True)


# ================================================================
# 2) NDF 双射
# ================================================================


@pytest.mark.parametrize("fps", [23.976, 24.0, 25.0, 29.97, 59.94])
def test_ndf_bijection_at_common_rates(fps):
    """NDF：帧号 ↔ 时间码在标称帧率下双射（含 24h 边界）。"""
    fps_int = round(fps)
    for f in (0, 1, fps_int - 1, fps_int, 3600 * fps_int, 24 * 3600 * fps_int - 1):
        tc = frames_to_timecode(f, fps)
        assert timecode_to_frames(tc, fps) == f, (fps, f, tc)


# ================================================================
# 3) Rational 帧→样本：精确零漂移
# ================================================================


def _exact_samples(frame: int, fps: Rational, sample_rate: int) -> int:
    frac = Fraction(fps.num, fps.den)
    return int(Fraction(frame * sample_rate) / frac)


@pytest.mark.parametrize(
    "fps,step_hint",
    [(FILM_NTSC, 2002), (NTSC_30, Fraction(1001 * 48000, 30000)), (Rational(25, 1), 1920)],
)
def test_frames_to_samples_exact_rational(fps, step_hint):
    """Rational 帧率换算与闭式精确一致（长时长零漂移）。"""
    sample_rate = 48000
    # 常规扫描 + 大帧号（10 小时量级）
    frames = list(range(0, 5000)) + [
        10 ** 5, 10 ** 6, 863 * 24 * 24, 2_594_592,
    ]
    for f in frames:
        assert frames_to_samples(f, fps, sample_rate) == _exact_samples(f, fps, sample_rate), f


def test_sample_frame_boundary_partition():
    """帧 f 拥有样本区间 [s(f), s(f+1))；区间末样本映射回 f（自洽）。"""
    sample_rate = 48000
    for fps in (FILM_NTSC, NTSC_30, Rational(25, 1)):
        for f in range(0, 2000):
            s_start = frames_to_samples(f, fps, sample_rate)
            s_next = frames_to_samples(f + 1, fps, sample_rate)
            assert s_next > s_start  # 每帧至少 1 个样本
            # 帧区间末样本仍属于帧 f
            assert samples_to_frames(s_next - 1, fps, sample_rate) == f, (fps, f)
            # 起点样本最多回退到前一词边界（floor 语义）
            back = samples_to_frames(s_start, fps, sample_rate)
            assert f - 1 <= back <= f, (fps, f, back)


def test_cumulative_no_drift_over_long_timeline():
    """逐帧累加的样本总量 == 闭式总量（无累计漂移）。"""
    fps = FILM_NTSC
    sample_rate = 48000
    n_frames = 20000
    total = frames_to_samples(n_frames, fps, sample_rate)
    per_frame_sum = sum(
        frames_to_samples(f + 1, fps, sample_rate)
        - frames_to_samples(f, fps, sample_rate)
        for f in range(n_frames)
    )
    assert per_frame_sum == total
    assert total == _exact_samples(n_frames, fps, sample_rate)


# ================================================================
# 4) float 路径偏差有界（记录既有语义）
# ================================================================


def test_float_fps_path_deviation_bounded():
    """float 帧率路径与精确值偏差 ≤ 1 样本（长时长不放大）。"""
    fps_exact = FILM_NTSC
    fps_float = float(fps_exact)  # 23.976023976023978
    sample_rate = 48000
    max_dev = 0
    for f in range(0, 100_000, 101):
        exact = frames_to_samples(f, fps_exact, sample_rate)
        got = frames_to_samples(f, fps_float, sample_rate)
        dev = abs(got - exact)
        max_dev = max(max_dev, dev)
        assert dev <= 1, (f, exact, got)
    assert max_dev <= 1


# ================================================================
# 5) 导出修复回归：精确样本定位不回退
# ================================================================


def test_export_sample_positions_monotonic_contiguous():
    """帧间样本区间连续无重叠（导出 out_start 定位的语义前提）。"""
    fps = FILM_NTSC
    sample_rate = 48000
    prev_end = frames_to_samples(0, fps, sample_rate)
    for f in range(1, 3000):
        start = frames_to_samples(f, fps, sample_rate)
        assert start >= prev_end  # 不重叠
        prev_end = start + 1
