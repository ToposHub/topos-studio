"""§13.1 矩阵 + §13.2 时间场景：test_timebase_conversion。

覆盖：24/25/24000/1001/30000/1001；一小时边界；floor/ceil；drop-frame 显示。
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.edit_core

from src.features.edit.ai_editing.timebase import (
    frames_to_timecode,
    seconds_to_frame_range,
    timecode_to_frames,
)
from src.shared.media.metadata import Rational


FPS_24 = Rational(24, 1)
FPS_25 = Rational(25, 1)
FPS_23976 = Rational(24000, 1001)
FPS_2997 = Rational(30000, 1001)


# ================================================================
# seconds_to_frame_range: floor/ceil
# ================================================================


def test_floor_start_ceil_end_24fps():
    # [0.5s, 2.5s] @ 24fps -> floor(12)=12, ceil(60)=60
    r = seconds_to_frame_range(0.5, 2.5, FPS_24)
    assert r.start_frame == 12
    assert r.end_frame == 60
    assert r.duration_frames == 48


def test_exact_alignment_no_warning():
    r = seconds_to_frame_range(0.0, 1.0, FPS_24)
    assert r.start_frame == 0
    assert r.end_frame == 24
    assert not r.has_warnings


def test_non_aligned_warns():
    # 0.7s @ 24fps = 16.8（非整帧）-> floor 到 16，应 warn
    r = seconds_to_frame_range(0.7, 2.0, FPS_24)
    assert r.has_warnings
    assert r.start_frame == 16  # floor(16.8)


def test_minimum_one_frame():
    # 区间过短 -> 至少 1 帧 + warning
    r = seconds_to_frame_range(1.0, 1.001, FPS_24)
    assert r.duration_frames >= 1
    assert r.has_warnings


def test_negative_seconds_raises():
    with pytest.raises(ValueError):
        seconds_to_frame_range(-1, 1, FPS_24)


def test_end_before_start_raises():
    with pytest.raises(ValueError):
        seconds_to_frame_range(2.0, 1.0, FPS_24)


# ================================================================
# NTSC fps（23.976 / 29.97）
# ================================================================


def test_23_976_fps_conversion():
    # 1s @ 23.976 ≈ 23.976 帧；floor(23.976)=23
    r = seconds_to_frame_range(0.0, 1.0, FPS_23976)
    assert r.start_frame == 0
    # ceil(23.976) = 24
    assert r.end_frame == 24


def test_29_97_fps_conversion():
    r = seconds_to_frame_range(0.0, 1.0, FPS_2997)
    # ceil(29.97) = 30
    assert r.end_frame == 30


# ================================================================
# 一小时边界（§13.2）
# ================================================================


@pytest.mark.parametrize("fps,num", [(FPS_24, 24), (FPS_25, 25)])
def test_one_hour_boundary(fps, num):
    # 1 小时 = 3600s
    r = seconds_to_frame_range(0.0, 3600.0, fps)
    assert r.end_frame == 3600 * num
    assert r.duration_frames == 3600 * num


def test_one_hour_boundary_23_976():
    # 1h @ 23.976 ≈ 86400 * 24000/1001 帧
    r = seconds_to_frame_range(0.0, 3600.0, FPS_23976)
    expected = 3600 * 24000 / 1001  # ≈ 86313.6
    assert r.end_frame == pytest.approx(int(expected) + (1 if expected != int(expected) else 0), abs=1)


# ================================================================
# timecode 往返
# ================================================================


def test_timecode_round_trip_24fps():
    tc = "01:30:45:12"
    frames = timecode_to_frames(tc, FPS_24)
    assert frames == ((1 * 60 + 30) * 60 + 45) * 24 + 12
    assert frames_to_timecode(frames, FPS_24) == tc


def test_timecode_round_trip_25fps():
    tc = "00:01:00:00"
    frames = timecode_to_frames(tc, FPS_25)
    assert frames == 60 * 25
    assert frames_to_timecode(frames, FPS_25) == tc


def test_drop_frame_separator():
    # M03 DF/NDF：合法 DF 码（非 10 整分钟第 0 秒 FF≥2）墙钟映射不变
    # （TAEP §4.1 + AUD-14700 真实帧率换算）；非法码（被跳过的 FF 0/1）
    # 显式拒绝。
    frames = timecode_to_frames("00:01:00;02", FPS_2997)
    assert frames == round(60 * 30000 / 1001) + 2  # 1798 + 2 = 1800
    # 与秒路径对账：60s 起点的帧号体系一致
    r = seconds_to_frame_range(0.0, 60.0, FPS_2997)
    assert r.end_frame - (frames - 2) in (0, 1)  # ceil/round 边界差至多 1
    # 输出：真实 SMPTE DF 编号（内部 1800 显示 00:01:00;02——跳过的
    # 0/1 号不出现）
    out = frames_to_timecode(1800, FPS_2997, drop_frame=True)
    assert out == "00:01:00;02"
    with pytest.raises(ValueError):
        timecode_to_frames("00:01:00;00", FPS_2997)  # 被 DF 跳过的非法码


def test_invalid_timecode_raises():
    with pytest.raises(ValueError):
        timecode_to_frames("bad", FPS_24)
    with pytest.raises(ValueError):
        timecode_to_frames("01:02", FPS_24)


# ================================================================
# 相邻 clip（半开区间端点相接）
# ================================================================


def test_adjacent_clips_no_gap_no_overlap():
    # clip1 [0,24), clip2 [24,48)
    r1 = seconds_to_frame_range(0.0, 1.0, FPS_24)
    r2 = seconds_to_frame_range(1.0, 2.0, FPS_24)
    assert r1.end_frame == r2.start_frame  # 相邻


# ================================================================
# M03 · DF/NDF 丢帧换算（SMPTE drop-frame 显示编号 + 非法码拒绝）
# ================================================================

class TestDropFrameConversion:
    def test_df_display_anchors_2997(self):
        # 29.97：每分钟跳 2 帧（非 10 整分钟第 0 秒的 FF 0/1）
        assert frames_to_timecode(1800, FPS_2997, drop_frame=True) \
            == "00:01:00;02"
        assert frames_to_timecode(1798, FPS_2997, drop_frame=True) \
            == "00:00:59;28"
        assert frames_to_timecode(1799, FPS_2997, drop_frame=True) \
            == "00:00:59;29"
        # 10 整分钟不跳
        assert frames_to_timecode(17982, FPS_2997, drop_frame=True) \
            == "00:10:00;00"
        assert frames_to_timecode(17985, FPS_2997, drop_frame=True) \
            == "00:10:00;03"

    def test_df_roundtrip_wide_range(self):
        # 合法 DF 码全区间往返：display → frames → display 恒等
        for internal in (0, 1, 1797, 1798, 1800, 1801, 9000,
                         17981, 17982, 17983, 54000, 180000):
            tc = frames_to_timecode(internal, FPS_2997, drop_frame=True)
            back = timecode_to_frames(tc, FPS_2997)
            assert back == internal, (internal, tc, back)
            assert frames_to_timecode(back, FPS_2997, drop_frame=True) == tc

    def test_df_invalid_codes_rejected(self):
        # 非 10 整分钟第 0 秒的 FF 0/1 是被跳过的非法码
        for bad in ("00:01:00;00", "00:01:00;01", "00:02:00;00", "00:12:00;01"):
            with pytest.raises(ValueError):
                timecode_to_frames(bad, FPS_2997)
        # 10 整分钟第 0 秒与第 0 秒之外的 FF 0/1 合法
        assert timecode_to_frames("00:10:00;00", FPS_2997) == 17982
        # 00:01:01;00：名义 61s*30=1830 − 跳过 2 = 1828（DF 逆映射）
        assert timecode_to_frames("00:01:01;00", FPS_2997) == 1828

    def test_df_undefined_for_non_ntsc(self):
        from src.shared.media.metadata import Rational
        for fps in (FPS_25, Rational(24, 1)):
            with pytest.raises(ValueError):
                frames_to_timecode(100, fps, drop_frame=True)
            with pytest.raises(ValueError):
                timecode_to_frames("00:00:01;00", fps, drop_frame=True)

    def test_df_5994_drop4(self):
        from src.shared.media.metadata import Rational
        ntsc60 = Rational(60000, 1001)
        # 59.94：每分钟跳 4 帧；60 墙秒 = 3596.4 → 3596 内部帧 + 4 = 3600
        assert frames_to_timecode(3600, ntsc60, drop_frame=True) \
            == "00:01:00;04"
        assert timecode_to_frames("00:01:00;04", ntsc60) == 3600
